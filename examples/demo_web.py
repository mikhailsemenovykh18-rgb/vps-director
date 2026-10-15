#!/usr/bin/env python3
"""Preview the Mini App / dashboard with made-up servers — no Telegram, no SSH, nothing leaves 127.0.0.1.

    python3 examples/demo_web.py [--port 8099] [--console]

It starts the real web server (vps_director/web.py, the real auth and API) on 127.0.0.1 with a
throwaway config folder, a fake bot token and recorded probe output for three servers, two sites and
two backups, then prints a one-time sign-in link for the browser. "Allow" in Telegram is simulated:
every restart or console request is answered "Allow" after 3 seconds.

--console turns the web console on. It is a REAL shell (bash --norc in a temporary folder) of the user
who runs this script, still only on 127.0.0.1 and only behind the one-time link; leave it off unless
you want to try the terminal and the command assistant.
"""
import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import director  # noqa: E402
from test_skills import GB, FakeRemote, kv, sec_out  # noqa: E402

from vps_director import backups, fixes, security, servers  # noqa: E402

FAKE_TOKEN = "123456:DEMO-not-a-real-token"
OWNER = "111"

INVENTORY = [{"ssh": "", "name": "edge", "main": True, "where": "Frankfurt", "role": "bot, panel, proxy"},
             {"ssh": "web", "name": "web", "where": "Amsterdam", "role": "sites and shop"},
             {"ssh": "old", "name": "archive", "where": "Sofia", "role": "old backups"}]
CONTAINERS = ("caddy|caddy:2.8|Up 6 days\nshop|ghcr.io/acme/shop:1.4|Up 6 days (healthy)\n"
              "worker|ghcr.io/acme/worker:1.4|Restarting (1) 12 seconds ago\npostgres|postgres:16|Up 6 days\n"
              "migrate|ghcr.io/acme/shop:1.4|Exited (0) 6 days ago\n")


class DemoTelegram:
    """Prints what the bot would send and presses Allow after 3 s."""

    def __init__(self):
        self.director = None

    def send(self, chat, text, buttons=None, raw=False):
        print("  [telegram → %s] %s" % (chat, " ".join(str(text).split())[:120]))
        for row in buttons or []:
            for button in row:
                data = button.get("callback_data", "")
                if data.startswith(("ctrok:", "conok:")):
                    threading.Timer(3, self.allow, (chat, data)).start()
        return 1

    def allow(self, chat, data):
        print("  [demo] pressing Allow")
        self.director.callback({"id": "1", "from": {"id": int(OWNER)}, "data": data,
                                "message": {"chat": {"id": int(chat)}, "message_id": 1}})

    def api(self, method, payload=None, **kw):
        return {"ok": True, "result": {"username": "demo_bot"}}

    def edit(self, *a, **kw):
        pass

    def delete(self, *a, **kw):
        pass

    def typing(self, *a, **kw):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--lang", default="en", choices=("en", "ru"))
    ap.add_argument("--console", action="store_true", help="turn the web console on (a real local shell)")
    args = ap.parse_args()

    conf = tempfile.mkdtemp(prefix="vpsd-demo-")
    url = "http://127.0.0.1:%d" % args.port
    env = {"BOT_TOKEN": FAKE_TOKEN, "OWNER_IDS": OWNER, "LANG": args.lang, "WEB_PORT": str(args.port), "WEB_URL": url,
           "WEB_CONSOLE": "1" if args.console else "0", "TZ": "Europe/Berlin", "BRIEF": "09:00", "NAME": "VPS Director", "OWNER_NAME": "Alex"}
    with open(os.path.join(conf, ".env"), "w") as fh:
        fh.write("\n".join("%s=%s" % kv_ for kv_ in env.items()) + "\n")
    for name, data in (("servers.json", INVENTORY),
                       ("sites.json", [{"name": "shop", "url": "https://shop.example.com/", "server": "web"},
                                       {"name": "blog", "url": "https://blog.example.org/", "server": "web"}]),
                       ("backups.json", [{"type": "dir", "name": "db dumps", "path": "/srv/dumps", "max_age": "26h", "server": "web"},
                                         {"type": "dir", "name": "panel", "path": "/srv/panel-backup", "max_age": "26h", "server": "edge"}])):
        with open(os.path.join(conf, name), "w") as fh:
            json.dump(data, fh)

    cfg = director.Config(conf)
    # a day of made-up history for the charts (5-minute points), like the bot collects by itself
    import math
    now0 = time.time()
    def wave(base, amp, phase, i):
        return round(max(0.0, base + amp * math.sin(i / 14.0 + phase) + amp * 0.35 * math.sin(i / 3.1 + phase * 2)), 1)
    hist = {"edge": [[int(now0 - (287 - i) * 300), round(wave(0.9, 0.35, 0.3, i), 2), wave(61, 3, 1.1, i), 39, int(wave(21, 8, 0.3, i))]
                     for i in range(288)],
            "web": [[int(now0 - (287 - i) * 300), round(wave(1.9, 0.8, 2.0, i), 2), wave(80, 5, 0.4, i), 86 + i // 144,
                     int(min(100, wave(64, 22, 2.0, i)))] for i in range(288)]}
    with open(os.path.join(conf, "metrics.json"), "w") as fh:
        json.dump(hist, fh)
    d = director.Director(cfg)
    d.tg = DemoTelegram()
    d.tg.director = d
    d.log = lambda m: print("  [log] " + m)
    now = time.time()
    d.remote = FakeRemote({
        ("", servers.PROBE): kv(up=6 * 86400 + 3600, load="0.84", cores=4, mem_avail=GB + GB // 2, disk_used=31 * GB,
                                docker={"total": 5, "ok": 3, "bad": "worker", "why": "worker~Restarting (1) 12 seconds ago", "done": "migrate"})
        + "cpu_pct=23\n",
        ("web", servers.PROBE): kv(up=41 * 86400, load="2.61", cores=2, mem_total=2 * GB, mem_avail=GB // 3,
                                   disk_total=40 * GB, disk_used=35 * GB, failed="certbot.service") + "cpu_pct=71\n",
        ("", servers.CONTAINERS_PROBE): CONTAINERS,
        ("web", servers.CONTAINERS_PROBE): "",
        ("", servers.RESTART_SCRIPT): lambda stdin: stdin + "rc=0\n",
        ("", security.PROBE): sec_out(logins=(("root", "198.51.100.7", 2),)),
        ("web", security.PROBE): sec_out(password="yes", f2b="inactive", sec=2,
                                         ports=("0.0.0.0:22 sshd", "0.0.0.0:8080 python3", "127.0.0.1:5432 postgres")),
        ("web", backups.DIR_PROBE): "newest=%d 10 dump.sql.gz\ncount=14\n" % (now - 5 * 3600),
        ("", backups.DIR_PROBE): "newest=%d 10 panel.tar\ncount=3\n" % (now - 3 * 86400),
        # problem pages: what the server "shows", and the fixes behind Allow
        ("web", fixes.EV_DISK): "Filesystem      Size  Used Avail Use% Mounted on\n/dev/vda1        40G   35G  4.6G  89% /\n\n"
                                "TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\nImages          14        4         11.2GB    8.9GB (79%)\n"
                                "Build Cache     62        0         9.4GB     9.4GB\n\n35G\t/\n21G\t/var\n9.8G\t/home\n2.1G\t/usr\n",
        ("web", fixes.EV_TOP_CPU): "2.61 2.40 2.12 — load 1/5/15 min, cores: 2\n\n    PID USER         %CPU %MEM     ELAPSED COMMAND\n"
                                   "   4121 www-data     71.3  9.1    02:11:09 php-fpm8.2\n    812 mysql        38.0 22.4  41-02:13:55 mysqld\n",
        ("web", fixes.EV_TOP_MEM): "              total   used   free\nMem:   2048   1706   120\n",
        ("web", fixes.EV_UNIT): lambda stdin: "× certbot.service - Certbot\n     Active: failed (Result: exit-code)\n\n"
                                              "Oct 15 03:12:01 web certbot[991]: Challenge failed for domain shop.example\n",
        ("web", fixes.ACT_UNIT_RESTART): lambda stdin: "now: active\nrc=0\n",
        ("web", fixes.ACT_F2B): "now: active\nrc=0\n",
        ("web", fixes.ACT_UPDATES): "security updates left: 0\nrc=0\n",
        ("", fixes.EV_CONTAINER): lambda stdin: "status: restarting  exit code: 1  restarts: 37\n\n"
                                                "Error: connect ECONNREFUSED 127.0.0.1:6379\n    at TCPConnectWrap.afterConnect\n"})
    d.http_fetch = lambda u, timeout=15: (200, b"<html>ok</html>") if "shop" in u else (502, b"")
    d.cert_connect = lambda host, port, timeout=10: {"notAfter": time.strftime("%b %d %H:%M:%S %Y GMT",
                                                                               time.gmtime(now + (9 if "shop" in host else 70) * 86400))}
    d.rdap_fetch = lambda domain, timeout=15: {"events": [{"eventAction": "expiration",
                                                           "eventDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 200 * 86400))}]}
    d.whois_run = lambda domain: None
    # the command assistant: made-up history and commands, never this machine's own ~/.bash_history
    from vps_director import web as webmod
    demo_history = ["systemctl status nginx", "systemctl restart nginx", "docker ps", "docker compose logs -f worker",
                    "df -h", "journalctl -u nginx -n 50", "tail -f /var/log/nginx/error.log", "free -h"]
    d.consoles().history = lambda server="": webmod.Response.json(200, {"ok": True, "history": demo_history})
    d.consoles().commands = lambda server="": webmod.Response.json(200, {"ok": True, "commands": [
        "apt", "bash", "cat", "curl", "df", "docker", "free", "grep", "journalctl", "less", "ls", "nginx", "ps", "systemctl", "tail", "top"]})
    work = tempfile.mkdtemp(prefix="vpsd-demo-shell-")
    if args.console:
        d.consoles().argv_for = lambda server, shell=None: ["bash", "--norc", "-c", "cd %s && HISTFILE=/dev/null exec bash --norc -i" % work] \
            if server.get("main") else ["sh", "-c", "echo 'demo: only the main server has a shell here'; sleep 2"]
    d.add_job({"kind": "reminder", "text": "Renew the shop certificate", "chat": OWNER, "next": now + 3 * 86400, "created": now,
               "engine": "claude", "changes": False})
    d.add_job({"kind": "brief", "text": "☀️ brief", "chat": OWNER, "daily": "09:00", "next": now + 12 * 3600, "created": now})
    d.add_job({"kind": "task", "text": "Check disk usage on web and clean old logs", "chat": OWNER, "every": 86400,
               "next": now + 7200, "created": now, "engine": "claude", "changes": False})

    server = d.start_web()
    token = d.web_auth().new_login(OWNER)
    print("\nVPS Director demo on %s (config in %s)" % (url, conf))
    print("Open this one-time link in a browser (valid 10 minutes, single use):\n\n  %s/login?t=%s\n" % (url, token))
    print("Ctrl+C to stop.")
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        d.consoles().close_all()
        shutil.rmtree(conf, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
