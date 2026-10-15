"""The JSON API behind the Mini App and the dashboard (documented in docs/API.md).

Read-only views reuse the bot's own checks (servers / security / sites / backups), cached for 30 s
so the UI stays quick; `?refresh=1` asks for fresh data (at most once per 5 s per view).

    GET    /api/me                         who is signed in, bot name, language
    GET    /api/servers                    every server: online, load, memory, disk, containers, problems
    GET    /api/servers/<name>             one server, with its containers
    DELETE /api/servers/<name>             forget a server (not this machine): it leaves servers.json and the
                                           security baseline; the owner gets a Telegram note with Undo (7 days)
    GET    /api/security                   per server: what is new since the baseline, SSH settings, …
    GET    /api/sites                      status, certificate and domain dates
    GET    /api/backups                    freshness of every backup
    GET    /api/problems                   every open problem (servers, security, sites, backups) with its key
    GET    /api/problem?key=               one problem: what it means, what the server shows now (read-only
                                           probes), ways to fix it (fixes.py); also ctr:<server>:<c> and
                                           metric:<server>:load|mem|disk pages
    POST   /api/fix                        {"key", "fix", "arg"} → "ask": a task for the agent in the chat;
                                           other fixes → 202 {"pending": id}, run only on Allow in Telegram
    GET    /api/settings                   the editable settings, their current values and what each accepts
    POST   /api/settings                   {"changes": {KEY: value}} → written to .env, the bot restarts to apply
    GET    /api/team                       admins and read-only members, open invite links
    POST   /api/team                       {"uid", "role", "name"} add by Telegram id (or change the role)
    POST   /api/team/<uid>                 {"role"}; DELETE /api/team/<uid> removes (never the last admin)
    POST   /api/team/invite                {"role"} → a one-time t.me link (24 h); DELETE /api/team/invite/<id>
    POST   /api/team/pick                  {"role"} → the bot sends a "choose a person" button to the chat
    Read-only members (VIEWER_IDS) get every GET except the console; anything else is 403.
    GET    /api/jobs                       reminders and scheduled jobs
    POST   /api/jobs                       {"text", "when", "every": false} → a new reminder
    POST   /api/jobs/<id>                  {"text", "when", "every"} → the job with a new text and time
    DELETE /api/jobs/<id>
    POST   /api/servers/<name>/containers/<c>/restart
                                           → 202 {"pending": id}: the owner gets Allow / No in Telegram,
                                             the restart happens only on Allow
    GET    /api/restarts/<id>              state of such a request: pending / allowed / done / failed /
                                           denied / expired
    POST   /api/console                    {"server": name} → 202 {"pending": id}; only with WEB_CONSOLE=1, the
                                           owner gets Allow / No in Telegram (console.py)
    GET    /api/console/commands?server=   executable names in that server's PATH (assistant typo check)
    GET    /api/console/history?server=    the newest 500 distinct lines of its ~/.bash_history (read-only)
    GET    /api/console/<id>               pending / allowed (+ one-time ticket and the /ws/console URL) /
                                           denied / expired / opened
    GET    /api/console/sessions           the caller's consoles: attached, or kept running while away
    POST   /api/console/<id>/resume        {"token"} (from the session's WebSocket) → a one-time ticket, no Allow
    DELETE /api/console/<id>               end one of them
    GET    /ws/console?ticket=…            (WebSocket) the terminal itself
    POST   /api/logout                     (web.py) ends the dashboard session
"""
import json
import re
import secrets
import threading
import time
import urllib.parse

import os

from vps_director import backups, fixes, metrics, schedule, security, servers, settings, sites, team, web

CACHE_TTL = 30
REFRESH_MIN = 5
STALE_MAX = 15 * 60  # older data is still served at once while a fresh copy is fetched in the background
RESTART_TTL = 600     # an unanswered Allow / No request expires after this
MAX_PENDING = 5
MAX_TEXT = 500
UNDO_TTL = 7 * 86400  # a removed server can be put back from its Telegram note this long
RID = re.compile(r"[0-9a-f]{8}")


def gb(kb):
    return round(int(kb) / 1024 / 1024, 1)


def server_json(server, data, t, lang):
    out = {"name": server["name"], "where": server.get("where", ""), "role": server.get("role", ""),
           "main": bool(server.get("main")), "online": data is not None,
           "problems": [text for _, text in servers.block(server, data, t, lang)[1]],
           "issues": [{"key": key, "text": text, "level": "bad" if data is None else "warn"}
                      for key, text in servers.block(server, data, t, lang)[1]]}
    if data is None:
        return out
    mem_total = max(1, int(data["mem_total"]))
    mem_used = max(0, mem_total - int(data["mem_avail"]))
    disk_total = max(1, int(data["disk_total"]))
    cpu = data.get("cpu_pct", "")
    out["cpu_pct"] = max(0, min(100, int(cpu))) if cpu.isdigit() else None  # utilization; None = not measured
    out.update(uptime=int(data["up"]), load=float(data["load"]), cores=max(1, int(data["cores"])),
               mem_pct=round(mem_used * 100 / mem_total), mem_used_gb=gb(mem_used), mem_total_gb=gb(mem_total),
               disk_pct=round(int(data["disk_used"]) * 100 / disk_total), disk_used_gb=gb(data["disk_used"]),
               disk_total_gb=gb(disk_total), failed=servers.split(data.get("failed")))
    if "dk_total" in data:
        try:
            total, ok = int(data["dk_total"]), int(data.get("dk_ok") or 0)
        except ValueError:
            total, ok = 0, 0
        out["containers"] = {"total": total, "ok": ok, "bad": servers.split(data.get("dk_bad")),
                             "done": servers.split(data.get("dk_done"))}
    return out


class Api:
    def __init__(self, director, clock=time.time):
        self.d, self.clock = director, clock
        self.lock = threading.Lock()
        self.edit_lock = threading.Lock()  # servers.json / security baseline edits
        self.cache = {}

    # -- plumbing --

    def call(self, method, path, query, body, who):
        parts = [urllib.parse.unquote(p) for p in path.split("/")[2:] if p]
        refresh = urllib.parse.parse_qs(query).get("refresh", [""])[0] == "1"
        data = json.loads(body.decode("utf-8")) if body else {}
        route = (method if method != "HEAD" else "GET", parts[0] if parts else "", len(parts))
        who = dict(who, role=team.role_of(self.d.cfg, who["uid"]) or "viewer")
        if who["role"] != "admin" and (route[0] != "GET" or route[1] == "console"):
            return web.error(403, "read-only access")
        if route[1] == "settings":
            return self.settings_get(who) if route == ("GET", "settings", 1) else \
                self.settings_post(data, who) if route == ("POST", "settings", 1) else web.error(405, "method not allowed")
        if route[1] == "team":
            return self.team_call(route, parts, data, who)
        if route == ("GET", "me", 1):
            return self.me(who)
        if route == ("GET", "servers", 1):
            return self.servers(refresh)
        if route == ("GET", "servers", 2):
            return self.server(parts[1], refresh)
        if route == ("DELETE", "servers", 2):
            return self.delete_server(parts[1], who)
        if route[:2] == ("POST", "servers") and len(parts) == 5 and parts[2] == "containers" and parts[4] == "restart":
            return self.restart_request(parts[1], parts[3], who)
        if route == ("GET", "security", 1):
            return self.security(refresh)
        if route == ("GET", "sites", 1):
            return self.sites(refresh)
        if route == ("GET", "backups", 1):
            return self.backups(refresh)
        if route == ("POST", "health", 2) and parts[1] == "groups":
            return self.set_health_groups(data, who)
        if route == ("GET", "problems", 1):
            return self.ok(time=self.clock(), problems=self.problem_list(refresh))
        if route == ("GET", "problem", 1):
            return self.problem(urllib.parse.parse_qs(query).get("key", [""])[0], refresh, who)
        if route == ("POST", "fix", 1):
            return self.fix(data if isinstance(data, dict) else {}, who)
        if route == ("GET", "jobs", 1):
            return self.jobs()
        if route == ("POST", "jobs", 1):
            return self.add_job(data, who)
        if route == ("POST", "jobs", 2):
            return self.update_job(parts[1], data if isinstance(data, dict) else {}, who)
        if route == ("DELETE", "jobs", 2):
            return self.delete_job(parts[1], who)
        if route == ("GET", "restarts", 2):
            return self.restart_state(parts[1])
        if route == ("POST", "console", 1):
            return self.d.consoles().request(data if isinstance(data, dict) else {}, who)
        if route == ("GET", "console", 2) and parts[1] == "sessions":
            return self.d.consoles().sessions(who)
        if route == ("POST", "console", 3) and parts[2] == "resume":
            return self.d.consoles().resume(parts[1], data if isinstance(data, dict) else {}, who)
        if route == ("DELETE", "console", 2):
            return self.d.consoles().end(parts[1], who)
        if route == ("GET", "console", 2) and parts[1] == "commands":
            return self.d.consoles().commands(urllib.parse.parse_qs(query).get("server", [""])[0])
        if route == ("GET", "console", 2) and parts[1] == "history":
            return self.d.consoles().history(urllib.parse.parse_qs(query).get("server", [""])[0])
        if route == ("GET", "console", 2):
            return self.d.consoles().status(parts[1], who)
        if parts and parts[0] in ("me", "servers", "security", "sites", "backups", "jobs", "restarts", "console",
                                  "problems", "problem", "fix", "health"):
            return web.error(405, "method not allowed")
        return web.error(404, "no such API")

    def cached(self, key, fn, refresh=False):
        now = self.clock()
        with self.lock:
            hit = self.cache.get(key)
        if hit and (now - hit[0] < (REFRESH_MIN if refresh else CACHE_TTL)):
            return hit[1]
        if hit and not refresh and now - hit[0] < STALE_MAX:
            # stale-while-revalidate: the app opens at once with the last data (its time is shown),
            # the probes (ssh to every server, a few seconds) run in the background
            self.refresh_later(key, fn)
            return hit[1]
        value = fn()
        with self.lock:
            self.cache[key] = (self.clock(), value)
        return value

    def refresh_later(self, key, fn):
        with self.lock:
            busy = getattr(self, "_refreshing", set())
            self._refreshing = busy
            if key in busy:
                return
            busy.add(key)

        def run():
            try:
                value = fn()
                with self.lock:
                    self.cache[key] = (self.clock(), value)
            except Exception as exc:  # noqa: BLE001
                self.d.log("refresh %s: %r" % (key, exc))
            finally:
                with self.lock:
                    busy.discard(key)
        threading.Thread(target=run, daemon=True).start()

    def cached_at(self, key):
        with self.lock:
            hit = self.cache.get(key)
        return hit[0] if hit else self.clock()

    def ok(self, status=200, **data):
        return web.Response.json(status, dict(ok=True, **data))

    # -- views --

    def me(self, who):
        cfg = self.d.cfg
        return self.ok(user=who["uid"], via=who["via"], role=who.get("role", "admin"), bot=cfg.name,
                       owner=self.d.owner_name(who["uid"]),
                       lang=cfg.lang, tz=cfg.tz_name or "", tz_label=self.d.tz_label(), brief=cfg.brief or "",
                       console=bool(getattr(cfg, "web_console", False)),
                       health_hidden=self.health_hidden())

    HEALTH_GROUPS = ("security", "sites", "backups")

    def health_hidden(self):
        hidden = self.d.state.get().get("health_hidden")
        return [g for g in hidden if g in self.HEALTH_GROUPS] if isinstance(hidden, list) else []

    def set_health_groups(self, data, who):
        """POST /api/health/groups {"hidden": [...]}: which groups the Health tab leaves out (a view
        setting for everyone; the watchdog keeps checking them)."""
        hidden = data.get("hidden") if isinstance(data, dict) else None
        if not isinstance(hidden, list) or any(g not in self.HEALTH_GROUPS for g in hidden) \
                or len(set(hidden)) >= len(self.HEALTH_GROUPS):
            return web.error(400, "hidden: a list of security / sites / backups, at least one group stays")
        self.d.state.put(health_hidden=sorted(set(hidden)))
        self.d.audit.write(who["uid"], "settings", "health groups hidden: %s" % (", ".join(sorted(set(hidden))) or "none"))
        return self.ok(hidden=sorted(set(hidden)))

    def servers_data(self, refresh=False):
        def fetch():
            found, results, _, _ = self.d.servers_check()
            rows = [server_json(s, data, self.d.cfg.t, self.d.cfg.lang) for s, data in zip(found, results)]
            self.history().add(self.clock(), rows)  # every probe feeds the charts (at most one point per 4 min)
            return rows
        return self.cached("servers", fetch, refresh)

    def history(self):
        with self.lock:
            if getattr(self, "_history", None) is None:
                self._history = metrics.History(self.d.cfg.dir)
            return self._history

    def sample_forever(self, every=metrics.SAMPLE_EVERY):
        """Background: probe the servers every `every` seconds — the charts get points even when nobody looks,
        and every view of the app is already warm when it opens (the first round runs right at start)."""
        first = True
        while True:
            if not first:
                time.sleep(every)
            first = False
            for name, fn in (("servers", lambda: self.servers_data(refresh=True)),
                             ("security", lambda: self.security_rows(True) if os.path.exists(
                                 os.path.join(self.d.cfg.dir, security.FILE)) else None),
                             ("sites", lambda: self.sites_rows(True)), ("backups", lambda: self.backups_rows(True))):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self.d.log("warm %s: %r" % (name, exc))

    def servers(self, refresh):
        rows = [dict(r, history=self.history().series(r["name"], 48)) for r in self.servers_data(refresh)]
        return self.ok(time=self.cached_at("servers"), servers=rows, problems=[p for r in rows for p in r["problems"]])

    def find_server(self, name):
        return next((s for s in self.d.inventory()[0] if s["name"] == name), None)

    def containers(self, server, refresh=False):
        return self.cached("containers:" + server["name"], lambda: servers.parse_containers(
            self.d.remote(server.get("ssh") or "", servers.CONTAINERS_PROBE)), refresh)

    def server(self, name, refresh):
        server = self.find_server(name)
        if server is None:
            return web.error(404, "no such server")
        row = next((r for r in self.servers_data(refresh) if r["name"] == name), None)
        if row is not None:
            row = dict(row, history=self.history().series(name))
        return self.ok(time=self.clock(), server=row, containers=self.containers(server, refresh))

    def security(self, refresh):
        return self.ok(time=self.clock(), servers=self.security_rows(refresh))

    def security_rows(self, refresh=False):
        def fetch():
            out = []
            for server, scan, base, found in self.d.security_check(self.clock()):
                row = {"name": server["name"], "online": scan is not None, "first": base is None,
                       "findings": [{"level": lv, "kind": key.split(":")[2], "text": text, "key": key}
                                    for lv, key, text in found]}
                if scan is not None:
                    password, root = security.ssh_settings(scan["facts"])
                    facts = scan["facts"]
                    row.update(logins=len(scan["logins"]), login_ips=len({ip for _, _, ip, _ in scan["logins"]}),
                               ports=len(scan["ports"]), containers=len(scan["containers"]), images=len(scan["images"]),
                               ssh_password=password, root_login=root, fail2ban=facts.get("f2b", ""),
                               auto_updates=facts.get("ua", ""), updates=facts.get("updates", ""),
                               security_updates=facts.get("sec_updates", ""))
                out.append(row)
            return out
        return self.cached("security", fetch, refresh)

    def sites(self, refresh):
        return self.ok(time=self.clock(), sites=self.sites_rows(refresh))

    def sites_rows(self, refresh=False):
        def fetch():
            now = self.clock()
            found, results, _ = self.d.sites_check(now)
            out = []
            for site, res in zip(found, results):
                icon, line, problems, reminders = sites.assess(site, res, now, self.d.cfg.t)
                out.append({"name": site["name"], "url": site.get("url", ""), "server": site.get("server", ""),
                            "up": sites.is_up(site, res), "status": res.get("status"), "seconds": res.get("time"),
                            "cert_days": sites.days(res["cert"], now) if res.get("cert") else None,
                            "domain_days": sites.days(res["domain_exp"], now) if res.get("domain_exp") else None,
                            "line": line, "problems": [t for _, t in problems], "coming": [t for _, t in reminders],
                            "issues": [{"key": k, "text": t, "level": "bad"} for k, t in problems] +
                                      [{"key": k, "text": t, "level": "warn"} for k, t in reminders]})
            return out
        return self.cached("sites", fetch, refresh)

    def backups(self, refresh):
        return self.ok(time=self.clock(), backups=self.backups_rows(refresh))

    def backups_rows(self, refresh=False):
        def fetch():
            now = self.clock()
            entries, results, _ = self.d.backups_check(now)
            out = []
            for entry, res in zip(entries, results):
                icon, line, problems = backups.assess(entry, res, now, self.d.cfg.t)
                out.append({"name": entry["name"], "type": entry["type"], "server": entry.get("server", ""),
                            "fine": not problems, "time": res.get("time"), "line": line,
                            "problems": [t for _, t in problems],
                            "issues": [{"key": k, "text": t, "level": "bad" if icon == "❌" else "warn"} for k, t in problems]})
            return out
        return self.cached("backups", fetch, refresh)

    # -- problems: a page per problem with ways to fix it --

    def problem_list(self, refresh=False):
        """[{"key", "text", "level", "area", "server"?}] — what the watchdog would alert on, from the views'
        caches. One broken check does not hide the others."""
        out = []

        def part(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.d.log("problems: %r" % exc)

        def srv():
            for row in self.servers_data(refresh):
                out.extend(dict(i, area="server", server=row["name"]) for i in row["issues"])

        def sec():
            if not os.path.exists(os.path.join(self.d.cfg.dir, security.FILE)):
                return  # security starts with the owner's first /security (it records the baseline)
            for row in self.security_rows(refresh):
                out.extend({"key": f["key"], "text": f["text"], "level": f["level"], "area": "security",
                            "server": row["name"]} for f in row["findings"] if f["level"] != "info")

        def web_sites():
            for row in self.sites_rows(refresh):
                out.extend(dict(i, area="site", site=row["name"], server=row.get("server", "")) for i in row["issues"])

        def bk():
            for row in self.backups_rows(refresh):
                out.extend(dict(i, area="backup", backup=row["name"], server=row.get("server", "")) for i in row["issues"])

        for fn in (srv, sec, web_sites, bk):
            part(fn)
        rank = {"bad": 0, "warn": 1}
        out.sort(key=lambda i: rank.get(i["level"], 2))
        return out

    def problem_context(self, key, refresh=False):
        """Everything the problem page and its fixes need, or None for a key that is not a problem (any more)."""
        p = fixes.parse(key)
        if p is None:
            return None
        ctx = {"p": p, "text": "", "level": "info", "server": None, "row": None, "bad": [], "units": [],
               "backup": None, "url": ""}
        if p["kind"] in ("container", "metric", "secinfo"):
            ctx["server"] = self.find_server(p["server"])
            if ctx["server"] is None:
                return None
        elif p["kind"] in ("siteinfo", "bkinfo"):
            pass  # a page about one site / backup: it exists when its config entry does (below)
        else:
            item = next((i for i in self.problem_list(refresh) if i["key"] == key), None)
            if item is None:
                return None
            ctx.update(text=item["text"], level=item["level"])
            name = item.get("server") or p.get("server") or ""
            ctx["server"] = self.find_server(name) if name else None
        if p["area"] == "site":
            site = next((s for s in sites.load(self.d.cfg.dir)[0] if s["name"] == p.get("site")), None)
            if site is None and p["kind"] == "siteinfo":
                return None
            if site:
                ctx["url"] = site.get("url", "")
                if ctx["server"] is None and site.get("server"):
                    ctx["server"] = self.find_server(site["server"])
        if p["area"] == "backup":
            ctx["backup"] = next((b for b in backups.load(self.d.cfg.dir)[0] if b["name"] == p.get("backup")), None)
            if ctx["backup"] is None and p["kind"] == "bkinfo":
                return None
            if ctx["backup"] and ctx["backup"].get("server"):
                ctx["server"] = self.find_server(ctx["backup"]["server"])
        p["url"] = ctx["url"]
        server = ctx["server"]
        if server is not None and p["area"] == "server":
            ctx["row"] = next((r for r in self.servers_data() if r["name"] == server["name"]), None)
            ctx["units"] = [u for u in (ctx["row"] or {}).get("failed", []) if fixes.UNIT.fullmatch(u)]
            if p["kind"] == "containers":
                ctx["bad"] = [c["name"] for c in self.containers(server, refresh) if c["state"] == "bad"]
            if p["kind"] == "container" and p["arg"] not in {c["name"] for c in self.containers(server, refresh)}:
                return None
        ctx["fixes"] = fixes.fixes(p, self.d.cfg.lang, self.d.cfg.title, server, ctx["bad"], ctx["units"],
                                   bool(getattr(self.d.cfg, "web_console", False)))
        return ctx

    def problem(self, key, refresh, who=None):
        ctx = self.problem_context(key, refresh)
        if ctx is None:
            return self.ok(key=key, gone=True)
        p, server = ctx["p"], ctx["server"]
        t = fixes.tx(self.d.cfg.lang)
        seen = []
        # backups without a server are on this machine; an offline server has nothing to show
        # logs and process lists can hold secrets: read-only members get the explanation, not the raw output
        admin = (who or {}).get("role", "admin") == "admin"
        reachable = admin and (server is not None or p["area"] == "backup") and p["kind"] != "offline"
        if reachable:
            ssh = (server or {}).get("ssh") or ""
            for script, stdin in fixes.evidence(p, ctx["row"], ctx["bad"], ctx["backup"]):
                out = self.d.remote(ssh, script, stdin, 30)
                if out and out.strip():
                    seen.append(fixes.clip(out))
        title = ctx["text"]
        related = []
        if p["kind"] in ("secinfo", "siteinfo", "bkinfo"):
            mine = {"secinfo": lambda i: i["area"] == "security" and i.get("server") == p.get("server"),
                    "siteinfo": lambda i: i["area"] == "site" and i.get("site") == p.get("site"),
                    "bkinfo": lambda i: i["area"] == "backup" and i.get("backup") == p.get("backup")}[p["kind"]]
            related = [{"key": i["key"], "text": i["text"], "level": i["level"]} for i in self.problem_list() if mine(i)]
            title = p.get("server") or p.get("site") or p.get("backup")
            if related:
                ctx["level"] = "bad" if any(i["level"] == "bad" for i in related) else "warn"
        if p["kind"] == "container":
            title = "%s · %s" % (p["arg"], p["server"])
        elif p["kind"] == "metric":
            title = "%s · %s" % ({"load": "CPU", "mem": "RAM", "disk": "Disk"}[p["arg"]], p["server"])
        return self.ok(key=key, gone=False, title=title, level=ctx["level"], area=p["area"], kind=p["kind"],
                       server=server["name"] if server else "", url=ctx["url"],
                       about=t["about"].get(p["kind"]) or t["about"]["unknown"], seen_title=t["seen"],
                       seen=seen, fixes=ctx["fixes"] if admin else [], related=related)

    def fix(self, data, who):
        key, fid, arg = data.get("key"), data.get("fix"), data.get("arg") or ""
        if not all(isinstance(v, str) for v in (key, fid, arg)):
            return web.error(400, "key, fix and arg must be strings")
        ctx = self.problem_context(key)
        if ctx is None:
            return web.error(404, "no such problem (already fixed?)")
        match = next((f for f in ctx["fixes"] if f["id"] == fid and f.get("arg", "") == arg and f["kind"] != "nav"), None)
        if match is None:
            return web.error(400, "this fix does not apply to this problem")
        p, server = ctx["p"], ctx["server"]
        t = self.d.cfg.t
        if fid == "ask":
            prompt = fixes.ask_prompt(p, ctx["text"] or key, self.d.cfg.lang, server, match.get("look"))
            session, _ = self.d.state.active()
            self.d.audit.write(who["uid"], "task", "web: %s %s" % ("look into" if match.get("look") else "fix", key))
            self.d.tg.send(who["uid"], t("web_fix_task", text=ctx["text"] or match["label"]))
            self.d.submit(who["uid"], session, prompt)
            return self.ok(202, task=True)
        if server is None:
            return web.error(400, "this problem has no server")
        if fid == "restart":
            return self.restart_request(server["name"], arg, who)
        return self.action_request(server["name"], fid, arg, match["label"], key, who)

    def action_request(self, name, action, arg, label, key, who):
        """A fixed action on a server (fixes.ACTIONS or "accept"), run only after Allow in Telegram."""
        now = self.clock()
        with self.d.state.lock:
            items = self.pending()
            waiting = [v for v in items.values() if v.get("state") == "pending" and now - v["created"] < RESTART_TTL]
            if len(waiting) >= MAX_PENDING:
                return web.error(429, "too many requests waiting for an answer in Telegram")
            rid = secrets.token_hex(4)
            items[rid] = {"server": name, "container": arg, "action": action, "arg": arg, "label": label, "key": key,
                          "uid": who["uid"], "created": now, "state": "pending"}
            self.d.state.put(web_restarts=items)
        self.d.audit.write(who["uid"], "web", "%s on %s requested (#%s)" % (label, name, rid))
        t = self.d.cfg.t
        self.d.tg.send(who["uid"], t("web_fix_ask", what=label, server=name),
                       buttons=[[{"text": t("web_allow"), "callback_data": "ctrok:" + rid},
                                 {"text": t("web_deny"), "callback_data": "ctrno:" + rid}]])
        return self.ok(202, pending=rid)

    def action_run(self, chat, rid, entry):
        """An allowed action_request: the script on the server (or the baseline edit for "accept")."""
        action, arg, name = entry["action"], entry.get("arg", ""), entry["server"]
        server = self.find_server(name)
        ok, result = False, ""
        if server is None:
            result = "no such server"
        elif action == "accept":
            p = fixes.parse(entry.get("key", ""))
            if p and p["kind"] in ("sec_ip", "sec_port", "sec_container", "sec_image"):
                with self.edit_lock:
                    base = security.load_baseline(self.d.cfg.dir)
                    base["servers"][name] = fixes.accept_one(base["servers"].get(name), p["kind"], arg, self.clock())
                    security.save_baseline(self.d.cfg.dir, base)
                ok = True
            else:
                result = "nothing to accept"
        elif action in fixes.ACTIONS:
            script, timeout = fixes.ACTIONS[action]
            out = self.d.remote(server.get("ssh") or "", script, arg + "\n" if arg else "", timeout)
            lines = (out or "").strip().splitlines()
            ok = bool(lines) and lines[-1] == "rc=0"
            result = " ".join(lines[:-1] if lines and lines[-1].startswith("rc=") else lines)[:300]
        else:
            result = "unknown action"
        with self.d.state.lock:
            items = self.pending()
            if rid in items:
                items[rid].update(state="done" if ok else "failed", result=result)
                self.d.state.put(web_restarts=items)
        with self.lock:
            for k in ("servers", "security", "containers:" + name):
                self.cache.pop(k, None)
        self.d.audit.write("web", "command", "%s on %s: %s" % (entry.get("label", action), name, "ok" if ok else "failed"))
        t = self.d.cfg.t
        self.finish_note(chat, entry, ok, result)
        self.d.tg.send(chat, t("web_fix_done" if ok else "web_fix_failed", what=entry.get("label", action), server=name,
                               why=result or "?", out=(" — " + result) if ok and result else ""))

    # -- settings and the team --

    def settings_get(self, who=None):
        cfg = self.d.cfg
        fixed = {"WEB_URL": cfg.web_url, "WEB_PORT": cfg.web_port, "ALLOWED_ROOTS": ":".join(cfg.roots_raw),
                 "COMMANDS": ",".join(cfg.commands), "PROMPT_FILE": cfg.prompt_file}
        admin = (who or {}).get("role", "admin") == "admin"
        return self.ok(fields=settings.schema(self.d.menu_choices()), values=settings.current(cfg),
                       fixed={k: str(v) for k, v in fixed.items() if v} if admin else {}, conf_dir=cfg.dir if admin else "")

    def settings_post(self, data, who):
        changes = data.get("changes") if isinstance(data, dict) else None
        if not isinstance(changes, dict) or not changes or len(changes) > len(settings.FIELDS):
            return web.error(400, "changes: {KEY: value}")
        errors = self.d.apply_settings(changes, who["uid"])
        if errors:
            return web.Response.json(400, {"ok": False, "error": "; ".join("%s: %s" % kv for kv in sorted(errors.items())),
                                           "fields": errors})
        return self.ok(applied=sorted(changes), restarting=errors is None)

    def bot_username(self):
        if not getattr(self, "_bot_username", None):
            try:
                self._bot_username = (self.d.tg.api("getMe", timeout=15).get("result") or {}).get("username") or ""
            except Exception:  # noqa: BLE001
                self._bot_username = ""
        return self._bot_username

    def invite_link(self, code):
        user = self.bot_username()
        return "https://t.me/%s?start=inv_%s" % (user, code) if user else ""

    def team_get(self, who):
        cfg, people = self.d.cfg, self.d.people()
        members = []
        for uid in sorted(cfg.owners | cfg.viewers, key=lambda x: (x not in cfg.owners, x)):
            p = people.get(uid) if isinstance(people.get(uid), dict) else {}
            members.append({"uid": uid, "role": team.role_of(cfg, uid), "name": p.get("name") or self.d.owner_name(uid) or "",
                            "via": p.get("via", ""), "added": p.get("added"), "me": uid == who["uid"]})
        now = self.clock()
        # open invite links are for admins only: a read-only member could pass an admin link on
        invites = [{"id": code[:8], "role": v["role"], "created": v["created"], "expires": v["created"] + team.INVITE_TTL,
                    "link": self.invite_link(code)}
                   for code, v in sorted(team.live(self.d.state.get().get("invites"), now, team.INVITE_TTL).items(),
                                         key=lambda kv: kv[1]["created"])] if who.get("role") == "admin" else []
        return self.ok(members=members, invites=invites, admins=len(cfg.owners))

    def team_call(self, route, parts, data, who):
        method, n = route[0], route[2]
        data = data if isinstance(data, dict) else {}
        role = data.get("role")
        t = self.d.cfg.t
        if method == "GET" and n == 1:
            return self.team_get(who)
        if method == "POST" and n == 1:
            uid = str(data.get("uid") or "").strip()
            err = self.d.team_set(uid, role, who["uid"], via="id", name=data.get("name") or "")
            return web.error(400, t(err)) if err else self.ok(201, uid=uid)
        if n == 2 and parts[1] == "invite" and method == "POST":
            if role not in team.ROLES:
                return web.error(400, t("team_bad"))
            code = self.d.invite_create(role, who["uid"])
            if code is None:
                return web.error(429, "too many open invites")
            return self.ok(201, link=self.invite_link(code), expires=self.clock() + team.INVITE_TTL)
        if n == 3 and parts[1] == "invite" and method == "DELETE":
            with self.d.state.lock:
                invites = team.live(self.d.state.get().get("invites"), self.clock(), team.INVITE_TTL)
                gone = [c for c in invites if c[:8] == parts[2]]
                for c in gone:
                    invites.pop(c)
                self.d.state.put(invites=invites)
            return self.ok(deleted=len(gone)) if gone else web.error(404, "no such invite")
        if n == 2 and parts[1] == "pick" and method == "POST":
            if role not in team.ROLES:
                return web.error(400, t("team_bad"))
            self.d.pick_request(who["uid"], role)
            return self.ok(202, sent=True)
        if n == 2 and method == "POST":
            err = self.d.team_set(parts[1], role, who["uid"]) if parts[1] in self.d.cfg.owners | self.d.cfg.viewers \
                else "team_unknown"
            return web.error(400, t(err)) if err else self.ok(uid=parts[1], role=role)
        if n == 2 and method == "DELETE":
            err = self.d.team_remove(parts[1], who["uid"])
            return web.error(400, t(err)) if err else self.ok(deleted=parts[1])
        return web.error(405, "method not allowed")

    # -- forgetting a server (with Undo in Telegram) --

    def forget_cache(self, name):
        with self.lock:
            for key in ("servers", "security", "containers:" + name):
                self.cache.pop(key, None)

    def forget_watch(self, name):
        """Drop the watchdog's open items of a removed server, so it does not report them as fixed."""
        with self.d.state.lock:
            st = self.d.state.get()
            mine = lambda k: isinstance(k, str) and k.startswith(("srv:%s:" % name, "sec:%s:" % name))  # noqa: E731
            watch = st.get("watch") or []
            if any(mine(k) for k in watch):
                texts = st.get("watch_text") if isinstance(st.get("watch_text"), dict) else {}
                self.d.state.put(watch=[k for k in watch if not mine(k)],
                                 watch_text={k: v for k, v in texts.items() if not mine(k)})

    def removed(self, now):
        items = self.d.state.get().get("web_removed")
        return {k: v for k, v in items.items() if isinstance(v, dict) and now - v.get("time", 0) < UNDO_TTL} \
            if isinstance(items, dict) else {}

    def delete_server(self, name, who):
        server = self.find_server(name)
        if server is None:
            return web.error(404, "no such server")
        if server.get("main") or not server.get("ssh"):
            return web.error(400, "this machine cannot be removed")
        conf, now = self.d.cfg.dir, self.clock()
        with self.edit_lock:
            try:
                rows = servers.raw(conf)
            except ValueError:
                return web.error(409, "servers.json cannot be read")
            index = next((i for i, s in enumerate(rows) if isinstance(s, dict) and s.get("name") == name), None)
            if index is None:
                return web.error(404, "no such server")
            entry = rows.pop(index)
            servers.write(conf, rows)
            base = security.load_baseline(conf)
            baseline = base["servers"].pop(name, None)
            if baseline is not None:
                security.save_baseline(conf, base)
        with self.d.state.lock:
            items = self.removed(now)
            rid = secrets.token_hex(4)
            items[rid] = {"entry": entry, "index": index, "baseline": baseline, "time": now}
            self.d.state.put(web_removed=items)
        self.forget_cache(name)
        self.forget_watch(name)
        self.d.audit.write(who["uid"], "web", "server %s removed (#%s)" % (name, rid))
        t = self.d.cfg.t
        self.d.tg.send(who["uid"], t("web_server_removed", server=name),
                       buttons=[[{"text": t("web_server_undo"), "callback_data": "srvback:" + rid}]])
        return self.ok(deleted=name)

    def restore_server(self, chat, sender, rid):
        """The owner pressed Undo under a "server removed" note (Director.callback)."""
        t = self.d.cfg.t
        with self.d.state.lock:
            items = self.removed(self.clock())
            item = items.pop(rid, None) if RID.fullmatch(rid or "") else None
            self.d.state.put(web_removed=items)
        if item is None:
            self.d.tg.send(chat, t("stale"))
            return
        entry = item["entry"]
        name = entry.get("name") if isinstance(entry, dict) else None
        with self.edit_lock:
            try:
                rows = servers.raw(self.d.cfg.dir)
            except ValueError:
                rows = None
            if rows is None or not name or any(isinstance(s, dict) and s.get("name") == name for s in rows):
                self.d.tg.send(chat, t("web_server_not_restored", server=name or "?"))
                return
            rows.insert(min(int(item.get("index") or 0), len(rows)), entry)
            servers.write(self.d.cfg.dir, rows)
            if isinstance(item.get("baseline"), dict):
                base = security.load_baseline(self.d.cfg.dir)
                base["servers"].setdefault(name, item["baseline"])
                security.save_baseline(self.d.cfg.dir, base)
        self.forget_cache(name)
        self.d.audit.write(sender, "web", "server %s restored (#%s)" % (name, rid))
        self.d.tg.send(chat, t("web_server_restored", server=name))

    # -- jobs --

    def schedule_of(self, data, now):
        """(spec, first run) from {"when", "every"} or {"on": "YYYY-MM-DD", "when": "HH:MM"} (the calendar);
        ValueError when it does not parse."""
        when = data.get("when")
        if not isinstance(when, str):
            raise ValueError("when")
        if data.get("every") is True:
            spec = schedule.parse_every(when.strip())
            return spec, schedule.first_run(spec, now, self.d.cfg.tz)
        if data.get("on") is not None:
            return {}, schedule.at_date(data.get("on"), when.strip(), now, self.d.cfg.tz)
        return {}, schedule.parse_when(when.strip(), now, self.d.cfg.tz)

    def when_input(self, job):
        """What the edit form starts with: "09:00" for daily, "2h" for every, the time of a one-shot run."""
        if job.get("daily"):
            return str(job["daily"])
        if job.get("every"):
            return schedule.fmt_duration(int(job["every"])).replace(" ", "")
        return schedule.local(float(job.get("next") or self.clock()), self.d.cfg.tz).strftime("%H:%M")

    def job_json(self, job):
        nxt = schedule.local(float(job.get("next") or self.clock()), self.d.cfg.tz)
        return {"id": job["id"], "kind": job.get("kind", "task"), "text": str(job.get("text", ""))[:MAX_TEXT],
                "when_input": self.when_input(job), "editable": job.get("kind") != "brief",
                "next_date": nxt.strftime("%Y-%m-%d"), "next_time": nxt.strftime("%H:%M"),
                "every": int(job.get("every") or 0), "daily": job.get("daily") or "",
                "when": schedule.describe(job, self.d.cfg.lang), "next": job.get("next"),
                "next_text": schedule.fmt_time(job.get("next"), self.d.cfg.tz), "repeat": bool(job.get("every") or job.get("daily")),
                "changes": bool(job.get("changes"))}

    def jobs(self):
        jobs = [j for j in (self.d.state.get().get("jobs") or {}).values() if isinstance(j, dict) and j.get("id")]
        jobs.sort(key=lambda j: int(j["id"]) if str(j["id"]).isdigit() else 0)
        today = schedule.local(self.clock(), self.d.cfg.tz)
        return self.ok(jobs=[self.job_json(j) for j in jobs], tz=self.d.tz_label(),
                       today=today.strftime("%Y-%m-%d"), now_time=today.strftime("%H:%M"))

    def add_job(self, data, who):
        text, when = data.get("text"), data.get("when")
        if not isinstance(text, str) or not isinstance(when, str) or not text.strip() or len(text) > MAX_TEXT \
                or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            return web.error(400, "text (1-%d characters) and when are required" % MAX_TEXT)
        now = self.clock()
        try:
            spec, first = self.schedule_of(data, now)
        except ValueError:
            return web.error(400, "when: 30m, 2h, 1d or HH:MM (with \"on\": a future date)")
        job = self.d.add_job(dict(spec, kind="reminder", text=text.strip(), chat=who["uid"], next=first, created=now,
                                  engine="claude", changes=False))
        if not job:
            return web.error(409, "too many jobs")
        self.d.audit.write(who["uid"], "job", "web: reminder #%s %s %s" % (job["id"], when.strip()[:20], text.strip()))
        return self.ok(201, job=self.job_json(job))

    def update_job(self, job_id, data, who):
        """A new text and / or time for a job (not the morning brief: that is BRIEF in the settings)."""
        text, when = data.get("text"), data.get("when")
        if not isinstance(text, str) or not isinstance(when, str) or not text.strip() or len(text) > MAX_TEXT \
                or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            return web.error(400, "text (1-%d characters) and when are required" % MAX_TEXT)
        now = self.clock()
        old = (self.d.state.get().get("jobs") or {}).get(job_id)
        same_time = isinstance(old, dict) and when.strip() == self.when_input(old) \
            and (data.get("every") is True) == bool(old.get("every") or old.get("daily"))
        try:
            if same_time and data.get("on") is None:  # only the text changed: the run stays where it was
                spec, first = None, None
            else:
                spec, first = self.schedule_of(data, now)
        except ValueError:
            return web.error(400, "when: 30m, 2h, 1d or HH:MM (with \"on\": a future date)")
        with self.d.state.lock:
            jobs = self.d.state.get().get("jobs") or {}
            job = jobs.get(job_id) if isinstance(jobs, dict) else None
            if not isinstance(job, dict):
                return web.error(404, "no such job")
            if job.get("kind") == "brief":
                return web.error(400, "the morning brief time is BRIEF in the settings")
            if spec is None:
                job = dict(job, text=text.strip())
            else:
                job = {k: v for k, v in job.items() if k not in ("every", "daily")}
                job.update(spec, text=text.strip(), next=first)
            jobs[job_id] = job
            self.d.state.put(jobs=jobs)
        self.d.audit.write(who["uid"], "job", "web: change #%s → %s %s" % (job_id, when.strip()[:20], text.strip()))
        return self.ok(job=self.job_json(job))

    def delete_job(self, job_id, who):
        with self.d.state.lock:
            jobs = self.d.state.get().get("jobs") or {}
            gone = jobs.pop(job_id, None) if isinstance(jobs, dict) else None
            if gone:
                self.d.state.put(jobs=jobs)
        if not gone:
            return web.error(404, "no such job")
        self.d.audit.write(who["uid"], "job", "web: delete #%s" % job_id)
        return self.ok(deleted=job_id)

    # -- container restart: only after Allow in Telegram --

    def pending(self):
        now = self.clock()
        items = self.d.state.get().get("web_restarts") or {}
        return {k: v for k, v in items.items() if isinstance(v, dict) and now - float(v.get("created") or 0) < 86400}

    def restart_request(self, name, container, who):
        server = self.find_server(name)
        if server is None:
            return web.error(404, "no such server")
        if not servers.CONTAINER.fullmatch(container):
            return web.error(400, "bad container name")
        if container not in {c["name"] for c in self.containers(server)}:
            return web.error(404, "no such container")
        now = self.clock()
        with self.d.state.lock:
            items = self.pending()
            waiting = [v for v in items.values() if v.get("state") == "pending" and now - v["created"] < RESTART_TTL]
            if len(waiting) >= MAX_PENDING:
                return web.error(429, "too many requests waiting for an answer in Telegram")
            rid = secrets.token_hex(4)
            items[rid] = {"server": name, "container": container, "uid": who["uid"], "created": now, "state": "pending"}
            self.d.state.put(web_restarts=items)
        self.d.audit.write(who["uid"], "web", "restart %s on %s requested (#%s)" % (container, name, rid))
        t = self.d.cfg.t
        self.d.tg.send(who["uid"], t("web_restart_ask", container=container, server=name),
                       buttons=[[{"text": t("web_allow"), "callback_data": "ctrok:" + rid},
                                 {"text": t("web_deny"), "callback_data": "ctrno:" + rid}]])
        return self.ok(202, pending=rid)

    def restart_state(self, rid):
        entry = self.pending().get(rid) if RID.fullmatch(rid) else None
        if entry is None:
            return web.error(404, "no such request")
        state = entry["state"]
        if state == "pending" and self.clock() - entry["created"] >= RESTART_TTL:
            state = "expired"
        return self.ok(id=rid, state=state, server=entry["server"], container=entry["container"],
                       result=entry.get("result", ""))

    def restart_answer(self, chat, sender, rid, allow, mid=None, text=""):
        """The owner pressed Allow / No under a restart / fix request (Director.callback). Returns
        "running" / "denied" / "stale"; `mid` + `text` (the request message) get the final result later."""
        t = self.d.cfg.t
        with self.d.state.lock:
            items = self.pending()
            entry = items.get(rid)
            fresh = entry is not None and entry.get("state") == "pending" and self.clock() - entry["created"] < RESTART_TTL
            if fresh:
                entry["state"] = "allowed" if allow else "denied"
                if mid:
                    entry.update(mid=mid, text=text[:3000])
                self.d.state.put(web_restarts=items)
        if not fresh:
            return "stale"
        what = "%s on %s (#%s)" % (entry.get("label") or "restart " + entry["container"], entry["server"], rid)
        self.d.audit.write(sender, "allow" if allow else "deny", what)
        if not allow:
            return "denied"
        # "running" goes into the request message before the work starts, so a quick result never gets overwritten
        if mid:
            self.d.tg.edit(chat, mid, (entry.get("text", "") + "\n\n" + t("cb_running")).strip())
        else:
            self.d.tg.send(chat, t("cb_running"))
        self.d.spawn(self.restart_run if entry.get("action", "restart") == "restart" else self.action_run, chat, rid, entry)
        return "handled"

    def finish_note(self, chat, entry, ok, result):
        """The request message itself ends with the outcome (a separate message still notifies)."""
        if entry.get("mid"):
            t = self.d.cfg.t
            status = t("cb_done") if ok else t("cb_failed", why=(result or "?")[:200])
            self.d.tg.edit(chat, entry["mid"], (entry.get("text", "") + "\n\n" + status).strip())

    def restart_run(self, chat, rid, entry):
        server = self.find_server(entry["server"])
        out = None if server is None else self.d.remote(server.get("ssh") or "", servers.RESTART_SCRIPT,
                                                       entry["container"] + "\n", 90)
        lines = (out or "").strip().splitlines()
        ok = bool(lines) and lines[-1] == "rc=0"
        result = " ".join(lines[:-1] if lines and lines[-1].startswith("rc=") else lines)[:300]
        with self.d.state.lock:
            items = self.pending()
            if rid in items:
                items[rid].update(state="done" if ok else "failed", result=result)
                self.d.state.put(web_restarts=items)
        with self.lock:
            self.cache.pop("containers:" + entry["server"], None)
            self.cache.pop("servers", None)
        self.d.audit.write("web", "command", "restart %s on %s: %s" % (entry["container"], entry["server"],
                                                                         "ok" if ok else "failed"))
        t = self.d.cfg.t
        self.finish_note(chat, entry, ok, result)
        self.d.tg.send(chat, t("web_restart_done" if ok else "web_restart_failed", container=entry["container"],
                               server=entry["server"], why=result or "?"))
