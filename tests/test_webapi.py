"""Task 07a: the JSON API with a fake inventory (recorded probe output), through the real WebApp and
auth — signed with a fake bot token, nothing leaves the process."""
import json
import os
import sys
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import OWNER, Base  # noqa: E402
from test_skills import GB, INVENTORY, FakeRemote, kv, sec_out, write_json  # noqa: E402
from test_web import FAKE_TOKEN, ORIGIN, init_data  # noqa: E402

from vps_director import backups, fixes, security, servers, webapi  # noqa: E402

CONTAINERS = "shop|nginx:1.27|Up 3 days\nworker|app:2|Restarting (1) 5 seconds ago\nmigrate|app:2|Exited (0) 2 days ago\n"


class ApiBase(Base):
    def setUp(self):
        super().setUp()
        self.d.cfg.token = FAKE_TOKEN
        self.d.cfg.web_url = ORIGIN
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:2])
        self.d.remote = FakeRemote({
            ("", servers.PROBE): kv(docker={"total": 3, "ok": 1, "bad": "worker", "done": "migrate"}),
            ("web", servers.PROBE): kv(disk_used=70 * GB),
            ("", security.PROBE): sec_out(logins=()), ("web", security.PROBE): sec_out(),
            ("", servers.CONTAINERS_PROBE): CONTAINERS,
            ("", servers.RESTART_SCRIPT): lambda stdin: "worker\nrc=0\n" if stdin == "worker\n" else "rc=1\n"})
        self.app = self.d.web_app()

    def call(self, method, path, body=None, headers=None):
        h = {"X-Init-Data": init_data(auth_date=time.time())}
        if method != "GET":
            h.update({"X-Requested-With": "vpsd", "Origin": ORIGIN, "Content-Type": "application/json"})
        h.update(headers or {})
        raw = json.dumps(body if body is not None else {}).encode() if method != "GET" else b""
        return self.app.handle(method, path, h, raw, "203.0.113.1")


class ReadOnlyTest(ApiBase):
    def test_me(self):
        res = self.call("GET", "/api/me")
        self.assertEqual(res.status, 200)
        self.assertEqual((res.data["user"], res.data["via"], res.data["lang"]), (OWNER, "telegram", "en"))
        self.assertEqual(res.header("Cache-Control"), "no-store")

    def test_servers_and_cache(self):
        res = self.call("GET", "/api/servers")
        rows = res.data["servers"]
        self.assertEqual([r["name"] for r in rows], ["main", "web"])
        self.assertTrue(rows[0]["main"])
        self.assertEqual(rows[0]["containers"], {"total": 3, "ok": 1, "bad": ["worker"], "done": ["migrate"]})
        self.assertEqual(rows[1]["disk_pct"], 88)
        self.assertIn("web: disk 88%", " ".join(res.data["problems"]))
        probes = len(self.d.remote.calls)
        self.call("GET", "/api/servers")
        self.assertEqual(len(self.d.remote.calls), probes)  # cached for 30 s
        self.d.web_api().cache["servers"] = (time.time() - 6, rows)
        self.call("GET", "/api/servers?refresh=1")
        self.assertGreater(len(self.d.remote.calls), probes)

    def test_offline_server(self):
        del self.d.remote.outputs[("web", servers.PROBE)]
        rows = self.call("GET", "/api/servers").data["servers"]
        self.assertEqual((rows[1]["online"], "load" in rows[1]), (False, False))

    def test_one_server(self):
        res = self.call("GET", "/api/servers/main")
        self.assertEqual(res.data["server"]["name"], "main")
        self.assertEqual([(c["name"], c["state"]) for c in res.data["containers"]],
                         [("shop", "up"), ("worker", "bad"), ("migrate", "done")])
        self.assertEqual(self.call("GET", "/api/servers/nope").status, 404)

    def test_security(self):
        rows = self.call("GET", "/api/security").data["servers"]
        self.assertEqual([(r["name"], r["first"], r["online"]) for r in rows], [("main", True, True), ("web", True, True)])
        self.assertEqual((rows[1]["ssh_password"], rows[1]["root_login"], rows[1]["fail2ban"]), ("no", "keys", "active"))
        self.assertIn("logins", rows[1])
        self.assertNotIn("SHA256", json.dumps(rows))

    def test_sites_and_backups(self):
        write_json(os.path.join(self.tmp, "sites.json"), [{"url": "https://shop.example.com/", "name": "shop"}])
        self.d.http_fetch = lambda url, timeout=15: (502, b"")
        rows = self.call("GET", "/api/sites").data["sites"]
        self.assertEqual((rows[0]["name"], rows[0]["up"], rows[0]["status"]), ("shop", False, 502))
        self.assertTrue(rows[0]["problems"])
        write_json(os.path.join(self.tmp, "backups.json"), [
            {"type": "dir", "name": "dumps", "path": "/srv/dumps", "max_age": "26h", "server": "web"}])
        self.d.remote.outputs[("web", backups.DIR_PROBE)] = "newest=%d 10 a.gz\ncount=3\n" % (time.time() - 3600)
        rows = self.call("GET", "/api/backups").data["backups"]
        self.assertEqual((rows[0]["name"], rows[0]["fine"]), ("dumps", True))

    def test_unknown_and_wrong_method(self):
        self.assertEqual(self.call("GET", "/api/nothing").status, 404)
        self.assertEqual(self.call("DELETE", "/api/servers").status, 405)

    def test_every_endpoint_needs_auth(self):
        for path in ("/api/me", "/api/servers", "/api/servers/main", "/api/security", "/api/sites", "/api/backups",
                     "/api/jobs", "/api/restarts/0123abcd"):
            res = self.app.handle("GET", path, {}, b"", "198.51.100.%d" % len(path))
            self.assertEqual(res.status, 401, path)
        res = self.app.handle("POST", "/api/servers/main/containers/worker/restart",
                              {"X-Requested-With": "vpsd", "Origin": ORIGIN, "Content-Type": "application/json"},
                              b"{}", "198.51.100.200")
        self.assertEqual(res.status, 401)
        self.assertEqual(self.d.tg.sent, [])


class JobsTest(ApiBase):
    def test_add_list_delete(self):
        res = self.call("POST", "/api/jobs", {"text": "pay the hosting", "when": "2h"})
        self.assertEqual(res.status, 201)
        job = res.data["job"]
        self.assertEqual((job["kind"], job["text"], job["repeat"]), ("reminder", "pay the hosting", False))
        stored = self.d.state.get()["jobs"][job["id"]]
        self.assertEqual((stored["chat"], stored["kind"]), (OWNER, "reminder"))
        res = self.call("POST", "/api/jobs", {"text": "check logs", "when": "09:00", "every": True})
        self.assertEqual((res.status, res.data["job"]["when"]), (201, "daily at 09:00"))
        listed = self.call("GET", "/api/jobs").data["jobs"]
        self.assertEqual([j["text"] for j in listed], ["pay the hosting", "check logs"])
        self.assertEqual(self.call("DELETE", "/api/jobs/" + job["id"]).status, 200)
        self.assertEqual(self.call("DELETE", "/api/jobs/" + job["id"]).status, 404)
        self.assertEqual(len(self.call("GET", "/api/jobs").data["jobs"]), 1)
        kinds = [(r["kind"], r["text"]) for r in self.d.audit.tail(10)]
        self.assertIn(("job", "web: delete #%s" % job["id"]), kinds)

    def test_bad_input(self):
        for body in ({"text": "x"}, {"when": "2h"}, {"text": "", "when": "2h"}, {"text": "x", "when": "soon"},
                     {"text": "x" * 501, "when": "2h"}, {"text": "a\x00b", "when": "2h"}, {"text": 5, "when": "2h"},
                     {"text": "x", "when": "1m", "every": True}):
            self.assertEqual(self.call("POST", "/api/jobs", body).status, 400, body)
        self.assertEqual(self.d.state.get().get("jobs") or {}, {})

    def test_edit(self):
        job = self.call("POST", "/api/jobs", {"text": "pay the hosting", "when": "2h"}).data["job"]
        self.assertTrue(job["editable"])
        res = self.call("POST", "/api/jobs/" + job["id"], {"text": "pay the hosting and the domain", "when": "3h", "every": True})
        self.assertEqual(res.status, 200, res.data)
        self.assertEqual((res.data["job"]["text"], res.data["job"]["repeat"], res.data["job"]["when_input"]),
                         ("pay the hosting and the domain", True, "3h"))
        stored = self.d.state.get()["jobs"][job["id"]]
        self.assertEqual((stored["every"], stored["chat"], stored["kind"]), (3 * 3600, OWNER, "reminder"))
        res = self.call("POST", "/api/jobs/" + job["id"], {"text": "once", "when": "10:30"})
        stored = self.d.state.get()["jobs"][job["id"]]
        self.assertNotIn("every", stored)
        self.assertEqual(res.data["job"]["when_input"], "10:30")
        stored["next"] += 3 * 86400  # a one-shot run days ahead: a text-only edit keeps it
        self.d.state.put(jobs=dict(self.d.state.get()["jobs"], **{job["id"]: stored}))
        self.call("POST", "/api/jobs/" + job["id"], {"text": "renamed", "when": "10:30"})
        self.assertEqual(self.d.state.get()["jobs"][job["id"]]["next"], stored["next"])
        self.assertEqual(self.d.state.get()["jobs"][job["id"]]["text"], "renamed")
        self.assertEqual(self.call("POST", "/api/jobs/" + job["id"], {"text": "x", "when": "soon"}).status, 400)
        self.assertEqual(self.call("POST", "/api/jobs/99", {"text": "x", "when": "2h"}).status, 404)
        self.assertIn(("job", "web: change #%s → 10:30 once" % job["id"]), [(r["kind"], r["text"]) for r in self.d.audit.tail(5)])

    def test_calendar_date(self):
        import datetime
        listed = self.call("GET", "/api/jobs").data
        day = (datetime.date.fromisoformat(listed["today"]) + datetime.timedelta(days=3)).isoformat()
        res = self.call("POST", "/api/jobs", {"text": "renew the domain", "on": day, "when": "09:30"})
        self.assertEqual(res.status, 201, res.data)
        self.assertEqual((res.data["job"]["next_date"], res.data["job"]["next_time"], res.data["job"]["repeat"]), (day, "09:30", False))
        jid = res.data["job"]["id"]
        later = (datetime.date.fromisoformat(day) + datetime.timedelta(days=2)).isoformat()
        res = self.call("POST", "/api/jobs/" + jid, {"text": "renew the domain", "on": later, "when": "18:05"})
        self.assertEqual((res.data["job"]["next_date"], res.data["job"]["next_time"]), (later, "18:05"))
        for bad in ({"on": "2020-01-01", "when": "09:00"}, {"on": "nope", "when": "09:00"}, {"on": day, "when": "25:00"},
                    {"on": "2099-01-01", "when": "09:00"}):
            self.assertEqual(self.call("POST", "/api/jobs", dict(bad, text="x")).status, 400, bad)

    def test_csrf_applies(self):
        res = self.call("POST", "/api/jobs", {"text": "x", "when": "2h"}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(res.status, 403)
        self.assertEqual(self.d.state.get().get("jobs") or {}, {})


class RestartTest(ApiBase):
    def request(self, container="worker", server="main"):
        return self.call("POST", "/api/servers/%s/containers/%s/restart" % (server, container))

    def buttons(self):
        return self.d.tg.sent[-1][2][0]

    def press(self, data, sender=OWNER):
        self.d.callback({"id": "1", "from": {"id": int(sender)}, "message": {"chat": {"id": int(OWNER)}}, "data": data})

    def test_needs_allow_in_telegram(self):
        res = self.request()
        self.assertEqual(res.status, 202)
        rid = res.data["pending"]
        restarts = [c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT]
        self.assertEqual(restarts, [])  # nothing happened yet
        chat, text, buttons = self.d.tg.sent[-1]
        self.assertEqual(chat, OWNER)
        self.assertIn("Restart container worker on main?", text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], ["ctrok:" + rid, "ctrno:" + rid])
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "pending")
        self.press("ctrok:" + rid)
        restarts = [c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT]
        self.assertEqual(restarts, [("", servers.RESTART_SCRIPT, "worker\n")])  # the name on stdin only
        self.assertIn("✅ Container worker on main restarted.", self.d.tg.sent[-1][1])
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "done")
        self.press("ctrok:" + rid)  # a second press does nothing
        self.assertEqual(len([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT]), 1)
        self.assertEqual(self.d.tg.sent[-1][1], "This button has expired.")
        kinds = [(r["kind"], r["text"]) for r in self.d.audit.tail(10)]
        self.assertIn(("web", "restart worker on main requested (#%s)" % rid), kinds)
        self.assertIn(("allow", "restart worker on main (#%s)" % rid), kinds)

    def test_no_means_no(self):
        rid = self.request().data["pending"]
        self.press("ctrno:" + rid)
        self.assertEqual(self.d.tg.sent[-1][1], "✖️ Declined")
        self.assertEqual([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT], [])
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "denied")
        self.press("ctrok:" + rid)
        self.assertEqual([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT], [])

    def test_the_request_message_shows_progress(self):
        edits, calls = [], []
        self.d.tg.edit = lambda chat, mid, text: edits.append((mid, text))
        real = self.d.tg.api
        self.d.tg.api = lambda method, payload=None, **kw: calls.append((method, payload)) or real(method, payload, **kw)
        rid = self.request().data["pending"]
        question = self.d.tg.sent[-1][1]
        self.d.callback({"id": "1", "from": {"id": int(OWNER)}, "data": "ctrok:" + rid,
                         "message": {"chat": {"id": int(OWNER)}, "message_id": 77, "text": question}})
        removed = [p for m, p in calls if m == "editMessageReplyMarkup"]
        self.assertEqual(removed[0], {"chat_id": OWNER, "message_id": 77, "reply_markup": {"inline_keyboard": []}})
        self.assertEqual(edits[0], (77, question + "\n\n⏳ Allowed — running…"))  # at once, buttons gone
        self.assertEqual(edits[-1], (77, question + "\n\n✅ Done"))  # and the outcome when it is over
        self.d.callback({"id": "2", "from": {"id": int(OWNER)}, "data": "ctrok:" + rid,
                         "message": {"chat": {"id": int(OWNER)}, "message_id": 77, "text": question}})
        self.assertEqual(edits[-1], (77, question + "\n\n⌛ This request has expired"))

    def test_stranger_cannot_press(self):
        rid = self.request().data["pending"]
        self.press("ctrok:" + rid, sender="999")
        self.assertEqual([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT], [])

    def test_expired_request(self):
        rid = self.request().data["pending"]
        items = self.d.state.get()["web_restarts"]
        items[rid]["created"] -= webapi.RESTART_TTL + 1
        self.d.state.put(web_restarts=items)
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "expired")
        self.press("ctrok:" + rid)
        self.assertEqual([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT], [])

    def test_failure_reported(self):
        self.d.remote.outputs[("", servers.CONTAINERS_PROBE)] = CONTAINERS + "shop2|x|Up\n"
        self.d.web_api().cache.clear()
        rid = self.request("shop2").data["pending"]
        self.press("ctrok:" + rid)
        self.assertIn("Could not restart shop2 on main", self.d.tg.sent[-1][1])
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "failed")

    def test_refused_requests(self):
        self.assertEqual(self.request(server="nope").status, 404)
        self.assertEqual(self.request(container="-rm").status, 400)
        self.assertEqual(self.request(container="ghost").status, 404)
        self.assertEqual(self.d.tg.sent, [])
        for _ in range(webapi.MAX_PENDING):
            self.assertEqual(self.request().status, 202)
        self.assertEqual(self.request().status, 429)
        self.assertEqual(self.call("GET", "/api/restarts/zz").status, 404)


class ProblemsTest(ApiBase):
    def press(self, data, sender=OWNER):
        self.d.callback({"id": "1", "from": {"id": int(sender)}, "message": {"chat": {"id": int(OWNER)}}, "data": data})

    def keys(self):
        return [p["key"] for p in self.call("GET", "/api/problems?refresh=1").data["problems"]]

    def page(self, key):
        return self.call("GET", "/api/problem?key=" + urllib.parse.quote(key)).data

    def fix(self, key, fix, arg=""):
        return self.call("POST", "/api/fix", {"key": key, "fix": fix, "arg": arg})

    def test_list_has_keys(self):
        self.assertEqual(self.keys(), ["srv:main:containers", "srv:web:disk"])
        rows = self.call("GET", "/api/servers").data["servers"]
        self.assertEqual(rows[1]["issues"][0]["key"], "srv:web:disk")

    def test_disk_page_shows_the_server_and_fixes(self):
        self.d.remote.outputs[("web", fixes.EV_DISK)] = "/dev/sda1  80G  70G  10G  88% /\n"
        d = self.page("srv:web:disk")
        self.assertFalse(d["gone"])
        self.assertEqual((d["level"], d["area"], d["kind"], d["server"]), ("warn", "server", "disk", "web"))
        self.assertIn("Docker", d["about"])
        self.assertEqual(d["seen"], ["/dev/sda1  80G  70G  10G  88% /"])
        self.assertEqual([f["id"] for f in d["fixes"]], ["ask"])
        self.d.cfg.web_console = True
        self.assertEqual([f["id"] for f in self.page("srv:web:disk")["fixes"]], ["ask", "console"])

    def test_ask_makes_a_task(self):
        submitted = []
        self.d.submit = lambda chat, key, prompt, *a, **kw: submitted.append((chat, prompt))
        res = self.fix("srv:web:disk", "ask")
        self.assertEqual((res.status, res.data["task"]), (202, True))
        chat, prompt = submitted[0]
        self.assertEqual(chat, OWNER)
        self.assertIn("ssh alias: web", prompt)
        self.assertIn("disk", prompt.lower())
        self.assertIn("Taking it as a task", self.d.tg.sent[-1][1])

    def test_container_restart_needs_allow(self):
        d = self.page("srv:main:containers")
        self.assertEqual([(f["id"], f["kind"], f["arg"]) for f in d["fixes"] if f["kind"] == "allow"],
                         [("restart", "allow", "worker")])
        self.assertIn(("", fixes.EV_CONTAINER, "worker\n"), self.d.remote.calls)  # its log, read-only
        rid = self.fix("srv:main:containers", "restart", "worker").data["pending"]
        self.assertEqual([c for c in self.d.remote.calls if c[1] == servers.RESTART_SCRIPT], [])
        self.press("ctrok:" + rid)
        self.assertIn(("", servers.RESTART_SCRIPT, "worker\n"), self.d.remote.calls)

    def test_failed_unit(self):
        self.d.remote.outputs[("web", servers.PROBE)] = kv(failed="nginx.service")
        self.d.remote.outputs[("web", fixes.ACT_UNIT_RESTART)] = lambda stdin: "now: active\nrc=0\n"
        self.assertIn("srv:web:failed", self.keys())
        d = self.page("srv:web:failed")
        self.assertEqual([f["arg"] for f in d["fixes"] if f["id"] == "unit"], ["nginx.service"])
        rid = self.fix("srv:web:failed", "unit", "nginx.service").data["pending"]
        self.assertIn("nginx.service on web?", self.d.tg.sent[-1][1])
        self.press("ctrok:" + rid)
        self.assertIn(("web", fixes.ACT_UNIT_RESTART, "nginx.service\n"), self.d.remote.calls)
        self.assertIn("✅ Done", self.d.tg.sent[-1][1])
        self.assertEqual(self.call("GET", "/api/restarts/" + rid).data["state"], "done")

    def test_refused(self):
        self.assertEqual(self.fix("srv:web:disk", "restart", "worker").status, 400)  # not a fix of this problem
        self.assertEqual(self.fix("srv:main:containers", "restart", "shop").status, 400)  # shop is fine
        self.assertEqual(self.fix("srv:web:load", "ask").status, 404)  # no such problem now
        self.assertEqual(self.fix("srv:web:disk", "console").status, 400)  # nav fixes run in the app
        self.assertEqual(self.call("POST", "/api/fix", {"key": 1, "fix": "ask"}).status, 400)
        self.assertTrue(self.page("srv:web:load")["gone"])
        self.assertTrue(self.page("nonsense")["gone"])

    def test_pages_without_a_problem(self):
        self.d.remote.outputs[("web", fixes.EV_TOP_CPU)] = "0.10 0.20 0.30\n"
        d = self.page("metric:web:load")
        self.assertEqual((d["gone"], d["kind"], d["seen"]), (False, "metric", ["0.10 0.20 0.30"]))
        d = self.page("ctr:main:shop")
        self.assertEqual([f["arg"] for f in d["fixes"] if f["id"] == "restart"], ["shop"])
        self.assertTrue(self.page("ctr:main:ghost")["gone"])

    def test_security_accept(self):
        self.call("GET", "/api/security")  # the first scan records the baseline
        self.d.remote.outputs[("web", security.PROBE)] = sec_out(ports=("0.0.0.0:22 sshd", "0.0.0.0:8080 python3"), sec=2)
        self.d.web_api().cache.clear()
        keys = self.keys()
        self.assertIn("sec:web:port:0.0.0.0:8080", keys)
        self.assertIn("sec:web:updates", keys)
        d = self.page("sec:web:port:0.0.0.0:8080")
        self.assertEqual([f["id"] for f in d["fixes"]], ["accept", "ask", "server"])
        rid = self.fix("sec:web:port:0.0.0.0:8080", "accept", "0.0.0.0:8080").data["pending"]
        self.press("ctrok:" + rid)
        self.assertIn("0.0.0.0:8080", security.load_baseline(self.tmp)["servers"]["web"]["ports"])
        self.d.web_api().cache.clear()
        self.assertNotIn("sec:web:port:0.0.0.0:8080", self.keys())
        self.assertEqual([f["id"] for f in self.page("sec:web:updates")["fixes"]], ["updates", "ask", "server"])


class RemoveServerTest(ApiBase):
    def press(self, data, sender=OWNER):
        self.d.callback({"id": "1", "from": {"id": int(sender)}, "message": {"chat": {"id": int(OWNER)}}, "data": data})

    def names(self):
        with open(os.path.join(self.tmp, "servers.json"), encoding="utf-8") as fh:
            return [s["name"] for s in json.load(fh)]

    def test_remove_and_undo(self):
        with open(os.path.join(self.tmp, "servers.json"), encoding="utf-8") as fh:
            rows = json.load(fh)
        rows[1]["custom"] = "kept"  # a key this version does not know survives the round trip
        write_json(os.path.join(self.tmp, "servers.json"), rows)
        base = {"servers": {"main": {"ips": []}, "web": {"ips": ["198.51.100.7"]}}}
        security.save_baseline(self.tmp, base)
        self.d.state.put(watch=["srv:web:offline", "sec:web:updates", "disk / is 95% full"],
                         watch_text={"srv:web:offline": "x", "sec:web:updates": "y", "disk / is 95% full": "z"})
        self.assertEqual([r["name"] for r in self.call("GET", "/api/servers").data["servers"]], ["main", "web"])
        res = self.call("DELETE", "/api/servers/web")
        self.assertEqual((res.status, res.data["deleted"]), (200, "web"))
        self.assertEqual(self.names(), ["main"])
        self.assertEqual([r["name"] for r in self.call("GET", "/api/servers").data["servers"]], ["main"])
        self.assertNotIn("web", security.load_baseline(self.tmp)["servers"])
        st = self.d.state.get()
        self.assertEqual(st["watch"], ["disk / is 95% full"])  # not reported as "fixed" later
        self.assertEqual(list(st["watch_text"]), ["disk / is 95% full"])
        chat, text, buttons = self.d.tg.sent[-1]
        self.assertEqual(chat, OWNER)
        self.assertIn("Server web removed from the list", text)
        undo = buttons[0][0]["callback_data"]
        self.assertTrue(undo.startswith("srvback:"))
        self.press(undo, sender="999")  # not an owner: nothing
        self.assertEqual(self.names(), ["main"])
        self.press(undo)
        self.assertEqual(self.names(), ["main", "web"])
        with open(os.path.join(self.tmp, "servers.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[1]["custom"], "kept")
        self.assertEqual(security.load_baseline(self.tmp)["servers"]["web"], {"ips": ["198.51.100.7"]})
        self.assertIn("Server web is back in the list.", self.d.tg.sent[-1][1])
        self.press(undo)  # a second Undo does nothing
        self.assertEqual(self.names(), ["main", "web"])
        self.assertEqual(self.d.tg.sent[-1][1], "This button has expired.")
        kinds = [r["text"] for r in self.d.audit.tail(10)]
        self.assertTrue(any(k.startswith("server web removed") for k in kinds))
        self.assertTrue(any(k.startswith("server web restored") for k in kinds))

    def test_refused(self):
        self.assertEqual(self.call("DELETE", "/api/servers/main").status, 400)  # the machine the bot runs on
        self.assertEqual(self.call("DELETE", "/api/servers/nope").status, 404)
        self.assertEqual(self.names(), ["main", "web"])
        self.assertEqual(self.d.tg.sent, [])

    def test_undo_expires(self):
        self.call("DELETE", "/api/servers/web")
        undo = self.d.tg.sent[-1][2][0][0]["callback_data"]
        items = self.d.state.get()["web_removed"]
        for v in items.values():
            v["time"] -= webapi.UNDO_TTL + 1
        self.d.state.put(web_removed=items)
        self.press(undo)
        self.assertEqual(self.names(), ["main"])

    def test_undo_when_name_taken(self):
        self.call("DELETE", "/api/servers/web")
        undo = self.d.tg.sent[-1][2][0][0]["callback_data"]
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:2])  # added again by hand meanwhile
        self.press(undo)
        self.assertEqual(self.names(), ["main", "web"])
        self.assertIn("Could not put web back", self.d.tg.sent[-1][1])


class BotIntegrationTest(ApiBase):
    def test_dashboard_link_lifecycle(self):
        self.d.cfg.web_port = 8090
        self.msg("/dashboard")
        chat, text, buttons = self.d.tg.sent[-1]
        self.assertIn("one-time login link", text)
        url = buttons[0][0]["url"]
        self.assertTrue(url.startswith(ORIGIN + "/login?t="), url)
        token = url.split("t=", 1)[1]
        self.assertNotIn(token, text)
        with open(os.path.join(self.tmp, "audit.log")) as fh:
            self.assertNotIn(token, fh.read())  # the command is audited, the token is not
        res = self.app.handle("GET", "/login?t=" + token, {}, b"", "203.0.113.7")
        self.assertEqual(res.status, 303)
        cookie = res.header("Set-Cookie").split(";")[0]
        self.assertEqual(self.app.handle("GET", "/api/me", {"Cookie": cookie}, b"", "203.0.113.7").data["via"], "cookie")
        self.assertEqual(self.app.handle("GET", "/login?t=" + token, {}, b"", "203.0.113.7").status, 403)
        self.msg("/logout_web")
        self.assertIn("1 sessions and links revoked", self.last())
        self.assertEqual(self.app.handle("GET", "/api/me", {"Cookie": cookie}, b"", "203.0.113.7").status, 401)

    def test_dashboard_without_setup_explains(self):
        self.d.cfg.web_url = ""
        self.msg("/dashboard")
        self.assertIn("WEB_URL=https://", self.last())
        self.assertIsNone(self.d.tg.sent[-1][2])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "web-auth.json")))

    def test_dashboard_in_help(self):
        self.msg("/help")
        self.assertIn("/dashboard — dashboard in the browser", self.last())

    def test_menu_button(self):
        calls = []
        self.d.tg.api = lambda method, payload=None, **kw: calls.append((method, payload)) or {"ok": True}
        self.d.cfg.web_port = 8090
        self.d.set_menu_button()
        self.assertEqual(calls[-1], ("setChatMenuButton", {"menu_button": {
            "type": "web_app", "text": "Open", "web_app": {"url": ORIGIN + "/"}}}))
        self.d.cfg.web_url = ""
        self.d.set_menu_button()
        self.assertEqual(calls[-1], ("setChatMenuButton", {"menu_button": {"type": "commands"}}))
        calls.clear()
        self.d.set_menu_button()
        self.assertEqual(calls, [])  # never touches a menu button it did not set
        self.d.cfg.lang, self.d.cfg.web_url = "ru", ORIGIN
        self.d.set_menu_button()
        self.assertEqual(calls[-1][1]["menu_button"]["text"], "Открыть")


class ContainersParseTest(unittest.TestCase):
    def test_parse(self):
        rows = servers.parse_containers(CONTAINERS + "bad name|x|Up\n-x|y|Up\njunk\n")
        self.assertEqual([r["name"] for r in rows], ["shop", "worker", "migrate"])


if __name__ == "__main__":
    unittest.main()
