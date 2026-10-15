"""Task 07b: the web console — requests, Allow / No in a fake Telegram, one-time tickets, the PTY
with harmless fake shells (/bin/sh -c 'cat' and friends) over a real WebSocket on 127.0.0.1,
limits and cleanup. Nothing here opens ssh to any server."""
import json
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import OWNER, make_cfg  # noqa: E402
from test_web import FAKE_TOKEN, ORIGIN, init_data  # noqa: E402
from test_webapi import ApiBase  # noqa: E402
from test_ws import WSClient  # noqa: E402

from vps_director import console, web, ws  # noqa: E402

CAT = ["/bin/sh", "-c", "cat"]


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class ConsoleBase(ApiBase):
    def setUp(self):
        super().setUp()
        self.d.cfg.web_console = True
        self.cons = self.d.consoles()
        self.cons.argv_for = lambda server: list(self.fake_shell)
        self.cons.grace = 1
        self.fake_shell = CAT
        self.server = None

    def tearDown(self):
        self.cons.close_all("closed")
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def press(self, data, sender=OWNER):
        self.d.callback({"id": "1", "from": {"id": int(sender)}, "message": {"chat": {"id": int(sender)}}, "data": data})

    def ask(self, server="main"):
        return self.call("POST", "/api/console", {"server": server})

    def allowed_ticket(self, server="main"):
        rid = self.ask(server).data["pending"]
        self.press("conok:" + rid)
        return rid, self.call("GET", "/api/console/" + rid).data["ticket"]

    def live(self):
        if self.server is None:
            self.server = web.serve(0, self.app)
        return self.server.server_address[1]

    def connect(self, ticket, origin=ORIGIN, **headers):
        h = {"Origin": origin} if origin else {}
        h.update(headers)
        return WSClient(self.live(), "/ws/console?ticket=" + ticket, headers=h)

    def session(self):
        self.assertTrue(wait_for(lambda: len(self.cons.active) == 1))
        return next(iter(self.cons.active.values()))


class RequestFlowTest(ConsoleBase):
    def test_off_unless_web_console(self):
        self.d.cfg.web_console = False
        res = self.ask()
        self.assertEqual(res.status, 403)
        self.assertIn("WEB_CONSOLE=1", res.data["error"])
        self.assertEqual(self.d.tg.sent, [])
        self.assertEqual(self.cons.socket("ticket=x", {"Origin": ORIGIN}, "203.0.113.1").status, 403)
        self.assertFalse(make_cfg(self.tmp).web_console)
        self.assertTrue(make_cfg(self.tmp, WEB_CONSOLE="1").web_console)

    def test_auto_allow_only_inside_telegram(self):
        self.d.cfg.web_console_auto = True
        rid = self.ask().data["pending"]
        state = self.call("GET", "/api/console/" + rid).data
        self.assertEqual(state["state"], "allowed")  # no Allow needed
        self.assertIn("opened from the app", self.d.tg.sent[-1][1])  # but the chat is told
        self.assertIsNone(self.d.tg.sent[-1][2])  # no buttons
        self.assertIn(("allow", "console on main (#%s)" % rid), [(r["kind"], r["text"]) for r in self.d.audit.tail(5)])
        self.assertTrue(make_cfg(self.tmp, WEB_CONSOLE_AUTO="1").web_console_auto)

    def test_allow_issues_a_ticket(self):
        res = self.ask()
        self.assertEqual(res.status, 202)
        rid = res.data["pending"]
        chat, text, buttons = self.d.tg.sent[-1]
        self.assertEqual(chat, OWNER)
        self.assertIn("Open a console on main?", text)
        self.assertEqual([b["callback_data"] for b in buttons[0]], ["conok:" + rid, "conno:" + rid])
        state = self.call("GET", "/api/console/" + rid).data
        self.assertEqual((state["state"], "ticket" in state), ("pending", False))
        self.press("conok:" + rid)
        self.assertIn("Allowed", self.d.tg.sent[-1][1])
        state = self.call("GET", "/api/console/" + rid).data
        self.assertEqual(state["state"], "allowed")
        self.assertGreaterEqual(len(state["ticket"]), 40)
        self.assertEqual(state["ws"], "/ws/console?ticket=" + state["ticket"])
        self.assertNotIn(state["ticket"], json.dumps(list(self.cons.tickets)))  # only the hash is kept
        kinds = [(r["kind"], r["text"]) for r in self.d.audit.tail(10)]
        self.assertIn(("console", "console on main requested (#%s)" % rid), kinds)
        self.assertIn(("allow", "console on main (#%s)" % rid), kinds)
        self.press("conok:" + rid)  # a second press: stale
        self.assertEqual(self.d.tg.sent[-1][1], "This button has expired.")
        self.assertEqual(len(self.cons.tickets), 1)

    def test_no_means_no(self):
        rid = self.ask().data["pending"]
        self.press("conno:" + rid)
        self.assertEqual(self.d.tg.sent[-1][1], "✖️ Declined")
        self.assertEqual(self.call("GET", "/api/console/" + rid).data["state"], "denied")
        self.press("conok:" + rid)
        self.assertEqual(self.cons.tickets, {})

    def test_request_expires(self):
        rid = self.ask().data["pending"]
        self.cons.waiting[rid]["created"] -= console.WAIT_TTL + 1
        self.assertEqual(self.call("GET", "/api/console/" + rid).data["state"], "expired")
        self.press("conok:" + rid)
        self.assertEqual(self.cons.tickets, {})

    def test_stranger_and_other_owner_cannot_allow(self):
        self.d.cfg.owners.add("222")
        rid = self.ask().data["pending"]
        self.press("conok:" + rid, sender="999")   # not an owner: ignored
        self.press("conok:" + rid, sender="222")   # another owner: not the one who asked
        self.assertEqual(self.cons.tickets, {})
        self.assertEqual(self.call("GET", "/api/console/" + rid).data["state"], "pending")

    def test_status_only_for_the_user_who_asked(self):
        rid = self.ask().data["pending"]
        self.press("conok:" + rid)
        self.d.cfg.owners.add("222")
        self.d.web_auth().owners.add("222")
        other = self.app.handle("GET", "/api/console/" + rid, {"X-Init-Data": init_data(uid=222, auth_date=time.time())},
                                b"", "203.0.113.1")
        self.assertEqual(other.status, 404)
        self.assertEqual(self.call("GET", "/api/console/zzz").status, 404)
        self.assertEqual(self.call("GET", "/api/console/" + "0" * 8).status, 404)

    def test_server_only_from_servers_json(self):
        for bad in ("nope", "-oProxyCommand=x", "web; rm -rf /", None, 5):
            res = self.ask(bad)
            self.assertEqual(res.status, 404, bad)
        self.assertEqual(self.d.tg.sent, [])

    def test_needs_auth_and_csrf(self):
        self.assertEqual(self.app.handle("POST", "/api/console", {}, b'{"server":"main"}', "203.0.113.1").status, 401)
        res = self.call("POST", "/api/console", {"server": "main"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status, 403)
        self.assertEqual(self.d.tg.sent, [])

    def test_waiting_cap(self):
        for _ in range(console.MAX_WAITING):
            self.assertEqual(self.ask().status, 202)
        self.assertEqual(self.ask().status, 429)


class TicketTest(ConsoleBase):
    def socket(self, ticket, origin=ORIGIN, ip="203.0.113.1", **headers):
        h = {"Origin": origin} if origin else {}
        h.update(headers)
        return self.cons.socket("ticket=" + ticket, h, ip)

    def started(self, res):
        self.assertTrue(callable(res), getattr(res, "body", res))
        session = self.session()
        session.stop("closed")
        return session

    def test_single_use(self):
        rid, ticket = self.allowed_ticket()
        session = self.started(self.socket(ticket))
        self.assertFalse(alive(session.proc.pid))
        self.assertEqual(self.socket(ticket).status, 403)
        self.assertEqual(self.call("GET", "/api/console/" + rid).data["state"], "opened")

    def test_expired_ticket(self):
        rid, ticket = self.allowed_ticket()
        next(iter(self.cons.tickets.values()))["expires"] = time.time() - 1
        self.assertEqual(self.call("GET", "/api/console/" + rid).data["state"], "expired")
        self.assertEqual(self.socket(ticket).status, 403)
        self.assertEqual(self.cons.active, {})

    def test_ticket_ttl_is_60_seconds(self):
        _, ticket = self.allowed_ticket()
        entry = next(iter(self.cons.tickets.values()))
        self.assertAlmostEqual(entry["expires"] - time.time(), console.TICKET_TTL, delta=2)
        self.assertEqual(console.TICKET_TTL, 60)

    def test_wrong_user_cookie_burns_the_ticket(self):
        _, ticket = self.allowed_ticket()
        auth = self.d.web_auth()
        auth.owners.add("222")
        sid = self.cookie_for("222")
        res = self.socket(ticket, Cookie="%s=%s" % (auth.cookie, sid))
        self.assertEqual(res.status, 403)
        self.assertEqual(self.socket(ticket).status, 403)  # burnt by the wrong attempt

    def test_right_user_cookie(self):
        _, ticket = self.allowed_ticket()
        sid = self.cookie_for(OWNER)
        self.started(self.socket(ticket, Cookie="%s=%s" % (self.d.web_auth().cookie, sid)))

    def cookie_for(self, uid):
        auth = self.d.web_auth()
        res = auth.login(auth.new_login(uid), "203.0.113.9")
        return res.header("Set-Cookie").split(";")[0].split("=", 1)[1]

    def test_bad_origin_and_garbage(self):
        _, ticket = self.allowed_ticket()
        self.assertEqual(self.socket(ticket, origin="https://evil.example").status, 403)
        self.assertEqual(self.socket(ticket, origin=None).status, 403)
        self.assertEqual(self.socket("").status, 403)
        self.assertEqual(self.socket("x" * 50).status, 403)

    def test_failed_tickets_are_rate_limited(self):
        _, ticket = self.allowed_ticket()
        for _ in range(10):
            self.socket("wrong", ip="198.51.100.7")
        self.assertEqual(self.socket(ticket, ip="198.51.100.7").status, 429)
        self.started(self.socket(ticket, ip="198.51.100.8"))  # another address is fine

    def test_max_concurrent(self):
        self.fake_shell = ["/bin/sh", "-c", "sleep 30"]
        n = console.MAX_CONSOLES
        tickets = [self.allowed_ticket()[1] for _ in range(n + 1)]
        runners = [self.socket(t) for t in tickets[:n]]
        self.assertTrue(all(callable(r) for r in runners))
        self.assertEqual(len(self.cons.active), n)
        self.assertEqual(self.ask().status, 429)  # no new request while the maximum is open
        self.assertEqual(self.socket(tickets[n]).status, 429)
        pids = [s.proc.pid for s in self.cons.active.values()]
        self.cons.close_all("closed")
        self.assertEqual(self.cons.active, {})
        self.assertFalse(any(alive(p) for p in pids))

    def test_remote_alias_validation(self):
        self.assertEqual(console.shell_argv({"ssh": "web"}),
                         ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=30",
                          "--", "web"])
        for bad in ("-oProxyCommand=sh", "web host", "a;b", "$(x)", "web\n"):
            with self.assertRaises(ValueError):
                console.shell_argv({"ssh": bad})
        self.assertEqual(console.shell_argv({"ssh": ""}, shell="/bin/zsh"), ["/bin/zsh", "-l"])

    def test_alias_comes_from_servers_json_only(self):
        seen = []
        self.cons.argv_for = lambda server: seen.append(dict(server)) or list(CAT)
        _, ticket = self.allowed_ticket("web")
        self.started(self.cons.socket("ticket=%s&ssh=evil&server=main" % ticket, {"Origin": ORIGIN}, "203.0.113.1"))
        self.assertEqual(seen[0]["ssh"], "web")
        self.assertEqual(seen[0]["name"], "web")

    def test_bad_alias_in_servers_json_is_refused(self):
        self.cons.argv_for = console.shell_argv
        _, ticket = self.allowed_ticket("web")
        self.cons.find_server = lambda name: {"name": "web", "ssh": "-oProxyCommand=sh"}
        self.assertEqual(self.socket(ticket).status, 400)
        self.assertEqual(self.cons.active, {})
        self.assertEqual(self.cons.reserved, 0)

    def test_clean_environment(self):
        os.environ["DIRECTOR_TEST_SECRET"] = "s3cret"
        try:
            env = console.clean_env("/bin/sh")
        finally:
            del os.environ["DIRECTOR_TEST_SECRET"]
        self.assertNotIn("DIRECTOR_TEST_SECRET", env)
        self.assertEqual((env["TERM"], env["SHELL"]), ("xterm-256color", "/bin/sh"))
        self.assertNotIn(FAKE_TOKEN, json.dumps(env))


class PtyTest(ConsoleBase):
    def open_console(self, shell=CAT):
        self.fake_shell = shell
        _, ticket = self.allowed_ticket()
        c = self.connect(ticket)
        self.assertEqual(c.status, 101, getattr(c, "body", b""))
        self.addCleanup(c.close)
        return c, self.session()

    def test_echo_and_notifications(self):
        c, session = self.open_console()
        self.assertEqual(self.d.tg.sent[-1][1], "🖥 Console on main opened.")
        c.send_json({"t": "in", "d": "hello console\n"})
        out, closed = c.read_until(b"hello console")
        self.assertIn(b"hello console", out)
        pid = session.proc.pid
        c.send_json({"t": "close"})  # the × button: ends it (a lost connection does not)
        self.assertTrue(wait_for(lambda: self.cons.active == {}))
        self.assertTrue(wait_for(lambda: session.proc.status is not None))
        self.assertFalse(alive(pid))
        self.assertTrue(wait_for(lambda: "closed after" in self.d.tg.sent[-1][1]))
        self.assertIn("🖥 Console on main closed after 0 s (closed in the dashboard).", self.d.tg.sent[-1][1])
        audit = [r["text"] for r in self.d.audit.tail(20) if r["kind"] == "console"]
        self.assertTrue(any(t.startswith("console on main opened by %s" % OWNER) for t in audit))
        self.assertTrue(any(t.startswith("console on main closed after 0 s: closed") for t in audit))
        self.assertFalse(any("hello" in t for t in audit))  # what was typed is never stored

    def test_lost_connection_keeps_it_then_the_keep_window_ends_it(self):
        shell = ["/bin/sh", "-c", "sleep 300 & /bin/sh -c 'sleep 301; true' & set -m; sleep 302 & cat"]
        c, session = self.open_console(shell)
        self.assertTrue(wait_for(lambda: len(console.session_members(session.proc.pid)) >= 4))
        members = console.session_members(session.proc.pid)
        self.cons.keep = 1.0
        c.sock.close()  # the browser goes away without a close frame
        self.assertTrue(wait_for(lambda: session.detached is not None))
        self.assertIn(session.cid, self.cons.active)  # still running: the user may come back
        self.assertTrue(all(alive(p) for p in members))
        self.assertTrue(wait_for(lambda: self.cons.active == {}))  # nobody came back within `keep`
        self.assertTrue(wait_for(lambda: not any(alive(p) for p in members)))
        self.assertEqual(console.session_members(session.proc.pid), [])
        with self.assertRaises(ChildProcessError):
            os.waitpid(session.proc.pid, os.WNOHANG)  # reaped: no zombie
        self.assertTrue(wait_for(lambda: "nobody came back" in self.d.tg.sent[-1][1]))

    def test_hup_ignored_gets_killed(self):
        c, session = self.open_console(["/bin/sh", "-c", "trap '' HUP; sleep 300 & trap '' HUP; cat"])
        self.assertTrue(wait_for(lambda: len(console.session_members(session.proc.pid)) >= 2))
        members = console.session_members(session.proc.pid)
        started = time.monotonic()
        session.stop("closed")
        self.assertGreaterEqual(time.monotonic() - started, self.cons.grace - 0.1)  # waited, then SIGKILL
        self.assertFalse(any(alive(p) for p in members))

    def test_shell_exit_closes_the_socket(self):
        c, session = self.open_console(["/bin/sh", "-c", "read x; echo bye-$x"])
        c.send_json({"t": "in", "d": "now\n"})
        out, closed = c.read_until(None)
        self.assertIn(b"bye-now", out)
        self.assertEqual(struct.unpack("!H", closed[:2])[0], 1000)
        self.assertTrue(wait_for(lambda: self.cons.active == {}))
        self.assertTrue(wait_for(lambda: "the shell exited" in self.d.tg.sent[-1][1]))

    def test_resize(self):
        c, session = self.open_console(["/bin/sh", "-c", "read x; stty size; cat"])
        c.send_json({"t": "resize", "cols": 132, "rows": 43})
        c.send_json({"t": "resize", "cols": "99", "rows": True})  # ignored
        c.send_json({"t": "in", "d": "go\n"})
        out, _ = c.read_until(b"43 132")
        self.assertIn(b"43 132", out)
        self.assertEqual((session.proc.cols, session.proc.rows), (132, 43))

    def test_garbage_messages_are_ignored(self):
        c, session = self.open_console()
        c.send(ws.TEXT, b"not json")
        c.send_json([1, 2])
        c.send_json({"t": "in", "d": 5})
        c.send(ws.BINARY, b"\x00\x01")
        c.send_json({"t": "in", "d": "still-here\n"})
        out, _ = c.read_until(b"still-here")
        self.assertIn(b"still-here", out)

    def test_idle_timeout(self):
        self.cons.idle = 0.6
        c, session = self.open_console()
        pid = session.proc.pid
        out, closed = c.read_until(None)
        self.assertEqual(struct.unpack("!H", closed[:2])[0], 4000)
        self.assertTrue(wait_for(lambda: self.cons.active == {}))
        self.assertFalse(alive(pid))
        self.assertTrue(wait_for(lambda: "30 minutes with no input or output" in self.d.tg.sent[-1][1]))
        audit = [r["text"] for r in self.d.audit.tail(5) if r["kind"] == "console"]
        self.assertTrue(audit[-1].startswith("console on main closed after 0 s: idle"), audit)

    def test_input_keeps_it_open_until_the_hard_cap(self):
        self.cons.idle, self.cons.hard = 0.8, 1.6
        c, session = self.open_console()
        start = time.monotonic()
        closed = None
        while closed is None and time.monotonic() - start < 6:
            try:
                c.send_json({"t": "in", "d": "x"})
            except OSError:  # the server closed it: read what is left, the close frame among it
                c.sock.settimeout(2)
                closed = c.read_until(None)[1]
                break
            c.sock.settimeout(0.3)
            try:
                op, data = c.recv()
                if op == ws.CLOSE:
                    closed = data
            except (OSError, EOFError):
                pass
        self.assertIsNotNone(closed)
        self.assertEqual(struct.unpack("!H", closed[:2])[0], 4001)
        self.assertGreaterEqual(time.monotonic() - start, 1.5)
        self.assertTrue(wait_for(lambda: self.cons.active == {}))
        self.assertTrue(wait_for(lambda: "the 8-hour limit" in self.d.tg.sent[-1][1]))

    def test_limits(self):
        self.assertEqual((console.IDLE, console.HARD, console.MAX_CONSOLES, console.KILL_GRACE, console.KEEP),
                         (1800, 28800, 3, 3, 1800))

    def test_ticket_cannot_be_reused_over_the_socket(self):
        _, ticket = self.allowed_ticket()
        c = self.connect(ticket)
        self.addCleanup(c.close)
        self.assertEqual(c.status, 101)
        again = self.connect(ticket)
        self.addCleanup(again.close)
        self.assertEqual(again.status, 403)
        wrong_origin = self.connect(self.allowed_ticket()[1], origin="https://evil.example")
        self.addCleanup(wrong_origin.close)
        self.assertEqual(wrong_origin.status, 403)

    def test_bot_stop_closes_everything(self):
        c, session = self.open_console()
        pid = session.proc.pid
        self.cons.close_all()
        self.assertFalse(alive(pid))
        out, closed = c.read_until(None)
        self.assertEqual(struct.unpack("!H", closed[:2])[0], 1001)


class ResumeTest(ConsoleBase):
    """Like VS Code's remote terminals: a lost connection does not end the console."""

    open_console = PtyTest.open_console

    def first_session_frame(self, c):
        for _ in range(20):
            op, data = c.recv()
            if op == ws.TEXT:
                obj = json.loads(data)
                if obj.get("t") == "session":
                    return obj
        self.fail("no session frame")

    def resume(self, sid, token, **kw):
        return self.call("POST", "/api/console/%s/resume" % sid, {"token": token}, **kw)

    def test_reconnect_replays_and_goes_on(self):
        c, session = self.open_console()
        info = self.first_session_frame(c)
        self.assertEqual(info["id"], session.cid)
        c.send_json({"t": "in", "d": "before-drop\n"})
        c.read_until(b"before-drop\r\n" + b"before-drop")
        c.sock.close()  # the phone lost the network
        self.assertTrue(wait_for(lambda: session.detached is not None))
        session.proc.write(b"while-away\n")  # the shell keeps working: its output is kept
        self.assertTrue(wait_for(lambda: b"while-away" in bytes(session.buffer)))
        self.assertEqual([x["attached"] for x in self.call("GET", "/api/console/sessions").data["sessions"]], [False])
        self.assertEqual(self.resume(session.cid, "wrong").status, 403)
        res = self.resume(session.cid, info["resume"])
        self.assertEqual(res.status, 200, res.data)
        c2 = self.connect(res.data["ticket"])
        self.addCleanup(c2.close)
        self.assertEqual(c2.status, 101)
        info2 = self.first_session_frame(c2)
        self.assertNotEqual(info2["resume"], info["resume"])  # rotated
        out, _ = c2.read_until(b"while-away")
        self.assertIn(b"before-drop", out)  # the replay: what was on the screen and what came meanwhile
        c2.send_json({"t": "in", "d": "after-back\n"})
        self.assertIn(b"after-back", c2.read_until(b"after-back")[0])
        self.assertEqual(self.resume(session.cid, info["resume"]).status, 403)  # the old token is gone
        self.assertEqual(len(self.cons.active), 1)  # the same console, no Allow asked again
        self.assertEqual(sum(1 for m in self.d.tg.sent if "Open a console" in m[1]), 1)
        audit = [r["text"] for r in self.d.audit.tail(10) if r["kind"] == "console"]
        self.assertTrue(any(t.startswith("console on main resumed") for t in audit))

    def test_take_over_and_strangers(self):
        c, session = self.open_console()
        info = self.first_session_frame(c)
        stranger = {"X-Init-Data": init_data(uid=999, auth_date=time.time())}
        self.assertEqual(self.resume(session.cid, info["resume"], headers=stranger).status, 401)  # not a member
        ticket = self.resume(session.cid, info["resume"]).data["ticket"]
        c2 = self.connect(ticket)  # a second tab: it takes over, the first one is told
        self.addCleanup(c2.close)
        self.assertEqual(c2.status, 101)
        _, closed = c.read_until(None)
        self.assertEqual(struct.unpack("!H", closed[:2])[0], 4002)
        self.assertEqual(len(self.cons.active), 1)

    def test_end_from_the_list(self):
        c, session = self.open_console()
        self.assertEqual(self.call("DELETE", "/api/console/" + session.cid).status, 200)
        self.assertTrue(wait_for(lambda: self.cons.active == {}))
        self.assertEqual(self.call("DELETE", "/api/console/" + session.cid).status, 404)

    def test_tmux_argv(self):
        self.assertEqual(console.shell_argv({"ssh": "web"}, mux="111")[-1],
                         console.MUX.format(name="vpsd-111"))
        self.assertEqual(console.shell_argv({"ssh": ""}, shell="/bin/bash", mux="111")[:2], ["/bin/bash", "-lc"])
        self.assertEqual(console.shell_argv({"ssh": "web"}, mux="1; rm -rf /")[-1], "web")  # digits only, else plain


class LifecycleTest(ConsoleBase):
    def fds(self):
        return len(os.listdir("/proc/self/fd"))

    def test_no_leaks_over_many_sessions(self):
        self.live()
        before = self.fds()
        pids = []
        for i in range(4):
            _, ticket = self.allowed_ticket()
            c = self.connect(ticket)
            self.assertEqual(c.status, 101)
            session = self.session()
            pids.append(session.proc.pid)
            c.send_json({"t": "in", "d": "round-%d\n" % i})
            self.assertIn(b"round-%d" % i, c.read_until(b"round-%d" % i)[0])
            c.send_json({"t": "close"})
            c.read_until(None)
            c.close()
            self.assertTrue(wait_for(lambda: self.cons.active == {} and session.proc.fd is None))
        self.assertFalse(any(alive(p) for p in pids))
        for pid in pids:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        self.assertTrue(wait_for(lambda: self.fds() <= before))
        closes = [r for r in self.d.audit.tail(50) if r["kind"] == "console" and " closed after " in r["text"]]
        self.assertEqual(len(closes), 4)

    def test_off_over_the_socket(self):
        _, ticket = self.allowed_ticket()
        self.d.cfg.web_console = False
        c = self.connect(ticket)
        self.addCleanup(c.close)
        self.assertEqual(c.status, 403)
        self.assertIn(b"WEB_CONSOLE=1", c.body)
        self.assertEqual(self.cons.active, {})

    def test_plain_http_and_wrong_methods(self):
        self.assertEqual(self.app.websocket("/ws/console?ticket=x", {"Origin": ORIGIN}, "203.0.113.1").status, 400)
        self.assertEqual(self.call("GET", "/api/console").status, 405)
        self.assertEqual(self.call("DELETE", "/api/console/abcdef12").status, 404)  # no such console
        self.assertEqual(self.call("POST", "/api/console", {"server": "main"}, headers={"X-Requested-With": ""}).status, 403)


class DurationTest(unittest.TestCase):
    def test_text(self):
        self.assertEqual(console.duration_text(42, "en"), "42 s")
        self.assertEqual(console.duration_text(7 * 60 + 5, "en"), "7 min")
        self.assertEqual(console.duration_text(7 * 60, "ru"), "7 мин")
        self.assertEqual(console.duration_text(2 * 3600 + 60, "en"), "2 h 1 min")


if __name__ == "__main__":
    unittest.main()
