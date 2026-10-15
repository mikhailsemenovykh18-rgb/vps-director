"""Task 06: built-in skills (servers, security, sites, backups). Offline: ssh output, RDAP JSON and
TLS certificates are recorded fakes; nothing touches the network or another server."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import OWNER, Base, director, write_exe  # noqa: E402
from test_features import TOKEN, FakeBotAPI, private  # noqa: E402

from vps_director import remote, servers  # noqa: E402
from vps_director import setup as vsetup  # noqa: E402

GB = 1024 * 1024  # kB


def kv(up=200000, load="0.52", cores=4, mem_total=4 * GB, mem_avail=3 * GB, disk_total=80 * GB, disk_used=20 * GB,
       failed="", docker=None):
    lines = ["up=%d" % up, "load=%s" % load, "cores=%d" % cores, "mem_total=%d" % mem_total,
             "mem_avail=%d" % mem_avail, "disk_total=%d" % disk_total, "disk_used=%d" % disk_used, "failed=" + failed]
    if docker is not None:
        lines += ["dk_%s=%s" % pair for pair in docker.items()]
    return "\n".join(lines) + "\n"


class FakeRemote:
    """Recorded probe output per (ssh alias, probe script). A missing entry = no connection."""

    def __init__(self, outputs=None):
        self.outputs, self.calls = dict(outputs or {}), []

    def __call__(self, ssh, script, stdin="", timeout=25):
        self.calls.append((ssh, script, stdin))
        value = self.outputs.get((ssh, script))
        return value(stdin) if callable(value) else value


def t_for(lang):
    return lambda key, **kw: director.TEXTS[lang][key].format(**kw) if kw else director.TEXTS[lang][key]


def write_json(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)


# ---------- 0. inventory and /servers ----------

class RemoteTest(unittest.TestCase):
    def test_argv_local_and_ssh(self):
        self.assertEqual(remote.argv("", "echo hi"), ["sh", "-c", "echo hi"])
        args = remote.argv("web-1", "echo 'hi'")
        self.assertEqual(args[:7], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", "web-1"])
        self.assertEqual(args[7], "sh -c 'echo '\"'\"'hi'\"'\"''")

    def test_alias_can_never_be_an_option(self):
        for bad in ("-oProxyCommand=sh", "a b", "a;b", "", "x" * 200, "$(id)"):
            self.assertFalse(remote.valid_alias(bad), bad)
        with self.assertRaises(ValueError):
            remote.argv("-oProxyCommand=sh", "true")
        self.assertIsNone(remote.run("-oProxyCommand=sh", "true"))

    def test_run_with_fake_ssh(self):
        bindir = tempfile.mkdtemp()
        write_exe(bindir, "ssh", '#!/bin/sh\ncase "$6" in down) echo "no route" >&2; exit 255 ;; esac\n'
                                 'cat; echo "argc=$#"\nexit 3\n')
        with mock.patch.dict(os.environ, {"PATH": bindir + ":" + os.environ["PATH"]}):
            self.assertIsNone(remote.run("down", "true"))
            self.assertEqual(remote.run("up", "true", stdin="/var/log/x\n"), "/var/log/x\nargc=7\n")
        self.assertEqual(remote.run("", "echo local; exit 1"), "local\n")  # exit codes do not matter

    def test_sections(self):
        self.assertEqual(remote.sections("junk\n@@a\n1\n2\n@@b\n@@a\n3"), {"a": ["1", "2", "3"], "b": []})


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_default_is_this_machine(self):
        found, warnings, configured = servers.load(self.tmp)
        self.assertEqual((len(found), found[0]["ssh"], found[0]["main"], warnings, configured), (1, "", True, [], False))

    def test_entries_are_checked(self):
        write_json(os.path.join(self.tmp, "servers.json"), [
            {"ssh": "", "name": "main", "where": "Frankfurt", "role": "bot"},
            {"ssh": "web", "name": "web", "hosting_paid_until": "2026-11-01"},
            {"ssh": "-oProxyCommand=x", "name": "evil"},
            {"ssh": "db", "name": "web"},
            {"ssh": "db", "name": "db", "hosting_paid_until": "soon"},
            "junk"])
        found, warnings, configured = servers.load(self.tmp)
        self.assertTrue(configured)
        self.assertEqual([(s["name"], s["ssh"], s["main"]) for s in found],
                         [("main", "", True), ("web", "web", False), ("db", "db", False)])
        self.assertEqual(found[1]["hosting_paid_until"], "2026-11-01")
        self.assertEqual(found[2]["hosting_paid_until"], "")
        self.assertEqual(len(warnings), 4)
        self.assertTrue(any("bad ssh alias" in w for w in warnings))

    def test_broken_file(self):
        with open(os.path.join(self.tmp, "servers.json"), "w") as fh:
            fh.write("{oops")
        found, warnings, configured = servers.load(self.tmp)
        self.assertEqual((len(found), configured), (1, False))
        self.assertIn("servers.json", warnings[0])

    def test_ssh_config_hosts(self):
        path = os.path.join(self.tmp, "config")
        with open(path, "w") as fh:
            fh.write("Host *\n  User x\nHost web web-alt # two names\n  HostName 192.0.2.1\n"
                     "host db\nHost !bad *.example.com dev?\nMatch host x\nHost web\n")
        self.assertEqual(servers.ssh_config_hosts(path), ["web", "web-alt", "db"])
        self.assertEqual(servers.ssh_config_hosts(os.path.join(self.tmp, "none")), [])


INVENTORY = [{"ssh": "", "name": "main", "main": True, "where": "Frankfurt", "role": "bot and panel"},
             {"ssh": "web", "name": "web", "where": "Amsterdam", "role": "sites"},
             {"ssh": "old", "name": "old", "where": "Sofia", "role": "archive"}]


class ServersReportTest(unittest.TestCase):
    def fake(self, **over):
        outputs = {("", servers.PROBE): kv(docker={"total": 3, "ok": 2, "bad": "", "done": "migrate"}),
                   ("web", servers.PROBE): kv(up=5000, load="6.10", cores=2, mem_avail=GB // 10, disk_used=70 * GB,
                                              failed="nginx.service", docker={"total": 2, "ok": 1, "bad": "db", "done": ""})}
        outputs.update(over)
        return FakeRemote(outputs)

    def test_russian_layout_matches_the_prototype(self):
        run = self.fake()
        results = servers.probe_all(INVENTORY, run)
        text, problems = servers.report(INVENTORY, results, t_for("ru"), "ru", "10.10 14:05 MSK")
        self.assertTrue(text.startswith("Серверы — 10.10 14:05 MSK\nНа связи 2 из 3 · ⚠️ есть проблемы: 6\n\n"), text)
        self.assertIn("⭐ main · Main · Frankfurt\n   bot and panel\n   Работает 2 дн 7 ч · нагрузка 0.52 (4 ядра)\n"
                      "   Память 1.0 из 4 ГБ (25%)\n   Диск 20 из 80 ГБ (25%)\n"
                      "   Контейнеры: 2 работают, 1 остановлены штатно", text)
        self.assertIn("🖥 web · Amsterdam\n   sites\n   Работает 1 ч 23 мин · ⚠️ нагрузка 6.10 (2 ядра)\n"
                      "   ⚠️ Память 3.9 из 4 ГБ (98%)\n   ⚠️ Диск 70 из 80 ГБ (88%)", text)
        self.assertIn("   ⚠️ Упавшие сервисы: nginx.service", text)
        self.assertIn("   ⚠️ Контейнеры: 1 из 2 в порядке, проблемы: db", text)
        self.assertIn("🖥 old · Sofia\n   ❌ нет связи по SSH\n   archive", text)
        self.assertIn("\n\nЧто проверить:\n• ⚠️ web: нагрузка 6.1 за 5 минут при 2 ядра\n• ⚠️ web: память 98%", text)
        self.assertIn("• ❌ нет связи с old", text)
        self.assertEqual([k for k, _ in problems], ["srv:web:load", "srv:web:mem", "srv:web:disk", "srv:web:failed",
                                                     "srv:web:containers", "srv:old:offline"])

    def test_english_all_fine(self):
        hosts = INVENTORY[:1]
        text, problems = servers.report(hosts, servers.probe_all(hosts, self.fake()), t_for("en"), "en", "10.10 14:05 UTC")
        self.assertEqual(problems, [])
        self.assertIn("Online 1 of 1 · ✅ all fine", text)
        self.assertIn("Up 2 d 7 h · load 0.52 (4 cores)", text)
        self.assertNotIn("What to check", text)

    def test_garbage_output_is_offline_and_plurals(self):
        run = FakeRemote({("web", servers.PROBE): "Welcome to Ubuntu!\nup=abc\n"})
        self.assertIsNone(servers.probe(INVENTORY[1], run))
        self.assertEqual([servers.plural(n, "ядро|ядра|ядер", "ru") for n in (1, 2, 5, 11, 21, 22, 12)],
                         ["ядро", "ядра", "ядер", "ядер", "ядро", "ядра", "ядер"])
        self.assertEqual(servers.plural(1, "core|cores", "en"), "core")

    def test_probes_run_in_parallel(self):
        def slow(ssh, script, stdin="", timeout=25):
            time.sleep(0.3)
            return kv()
        hosts = [{"ssh": "h%d" % i, "name": "h%d" % i} for i in range(6)]
        start = time.monotonic()
        self.assertEqual(len([r for r in servers.probe_all(hosts, slow) if r]), 6)
        self.assertLess(time.monotonic() - start, 1.2)

    def test_paid_reminders(self):
        now = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))
        hosts = [{"name": "a", "hosting_paid_until": "2026-10-15"}, {"name": "b", "hosting_paid_until": "2026-12-01"},
                 {"name": "c", "hosting_paid_until": "2026-10-01"}, {"name": "d"}]
        found = servers.paid_reminders(hosts, now, t_for("en"))
        self.assertEqual([k for k, _ in found], ["paid:a:2026-10-15", "paid:c:2026-10-01"])
        self.assertIn("5 d left", found[0][1])
        self.assertIn("overdue", found[1][1])


class ServersDirectorTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:2])
        self.d.remote = FakeRemote({("", servers.PROBE): kv(), ("web", servers.PROBE): kv(disk_used=70 * GB)})

    def test_command_and_phrase(self):
        self.msg("/servers")
        self.assertIn("Online 2 of 2 · ⚠️ problems: 1", self.last())
        self.assertIn("What to check:\n• ⚠️ web: disk 88%", self.last())
        self.d.cfg.lang = "ru"
        self.msg("Серверы")
        self.assertIn("На связи 2 из 2", self.last())
        self.assertEqual(self.submitted, [])  # no AI

    def test_every_can_schedule_servers(self):
        self.msg("/every 1h /servers")
        self.assertIn("Job #1", self.last())
        job = self.d.state.get()["jobs"]["1"]
        self.d.run_job_command(OWNER, job, "#1 /servers")
        self.assertIn("web: disk 88%", self.last())

    def test_watchdog_probes_other_servers_with_stable_keys(self):
        sent = []
        self.d.broadcast = sent.append
        with mock.patch.object(director, "server_problems", return_value=[]):
            self.d.watch_once(time.time())
            self.assertEqual(len(sent), 1)
            self.assertIn("web: disk 88%", sent[0])
            self.d.remote.outputs[("web", servers.PROBE)] = kv(disk_used=71 * GB)  # 89 %: same problem
            self.d.watch_once(time.time())
            self.assertEqual(len(sent), 1)
            del self.d.remote.outputs[("web", servers.PROBE)]
            self.d.watch_once(time.time())
            self.assertIn("🚨 Server check:\n• 🖥 web: no SSH connection", sent[-2])
            self.assertIn("✅ Resolved:\n• 🖥 web: disk 88%", sent[-1])
        self.assertNotIn("", [c[0] for c in self.d.remote.calls])  # this machine: server_problems(), not ssh
        self.assertEqual([(r["kind"], r["text"]) for r in self.d.audit.tail(2)],
                         [("alert", "🖥 web: no SSH connection"), ("resolved", "🖥 web: disk 88%")])

    def test_hosting_reminder_once_a_day(self):
        write_json(os.path.join(self.tmp, "servers.json"), [{"ssh": "", "name": "main", "hosting_paid_until": "2026-10-12"}])
        sent = []
        self.d.broadcast = sent.append
        now = time.mktime((2026, 10, 10, 9, 0, 0, 0, 0, -1))
        with mock.patch.object(director, "server_problems", return_value=[]):
            self.d.watch_once(now)
            self.d.watch_once(now + 3600)
            self.assertEqual(len(sent), 1)
            self.assertIn("📅 Reminder:\n• main: hosting is paid until 2026-10-12 — 2 d left", sent[0])
            self.d.watch_once(now + 86400)
            self.assertEqual(len(sent), 2)

    def test_away_digest_shows_texts_not_keys(self):
        self.d.state.put(watch=["srv:web:disk"], watch_text={"srv:web:disk": "web: disk 88%"})
        self.assertIn("web: disk 88%", self.d.away_digest(time.time() - 4 * 86400, time.time()))


class SetupServersTest(unittest.TestCase):
    def run_setup(self, answers, ssh_config_text):
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "ssh_config")
        with open(cfg_path, "w") as fh:
            fh.write(ssh_config_text)
        api = FakeBotAPI(polls=[[private(5, 1)]])
        conf = os.path.join(tmp, "main")
        answers, out = list(answers), []
        wiz = vsetup.Setup(conf, api, ask=lambda p: (out.append(p), answers.pop(0) if answers else "")[1],
                           secret=lambda p: TOKEN, out=out.append, clock=lambda: api.now, sleep=lambda s: None,
                           has_module=lambda name: False, ssh_config=cfg_path)
        wiz.run()
        return conf, out

    def test_owner_ticks_hosts(self):
        # owner confirm, name, owner name, tz, brief, then the hosts: a bad pick is asked again
        conf, out = self.run_setup(["y", "", "", "", "n", "9", "3 1"], "Host web\nHost db\nHost backup\n")
        self.assertTrue(any("1. web" in line and "3. backup" in line for line in out))
        with open(os.path.join(conf, "servers.json")) as fh:
            saved = json.load(fh)
        self.assertEqual([(s["ssh"], s["main"]) for s in saved], [("", True), ("backup", False), ("web", False)])
        self.assertEqual(os.stat(os.path.join(conf, "servers.json")).st_mode & 0o777, 0o600)

    def test_no_hosts_no_question(self):
        conf, out = self.run_setup(["y"], "Host *\n")
        self.assertFalse(os.path.exists(os.path.join(conf, "servers.json")))
        self.assertFalse(any("~/.ssh/config" in line for line in out))

    def test_enter_imports_nothing(self):
        conf, _ = self.run_setup(["y"], "Host web\n")
        self.assertFalse(os.path.exists(os.path.join(conf, "servers.json")))


if __name__ == "__main__":
    unittest.main()


# ---------- 1. security ----------

from vps_director import security, sites  # noqa: E402

NOW = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))


def iso(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW - hours_ago * 3600))


def sec_out(logins=(("root", "198.51.100.7", 2),), ports=("0.0.0.0:22 sshd", "127.0.0.1:5432 postgres"),
            containers=("shop|nginx:1.27|Up 3 days",), images=("nginx",), ps=(), exe=(), password="no", root="prohibit-password",
            logsrc="journal", sshd_src="effective", f2b="active", ua="on", updates=3, sec=0, docker=True):
    lines = ["@@facts", "logsrc=" + logsrc, "sshd_src=" + sshd_src]
    if sshd_src == "effective":
        lines += ["sshd_passwordauthentication=" + password, "sshd_permitrootlogin=" + root]
    lines += ["f2b=" + f2b, "ua=" + ua, "updates=%d" % updates, "sec_updates=%d" % sec] + (["docker=1"] if docker else [])
    lines.append("@@logins")
    for user, ip, ago in logins:
        lines.append("%s web sshd-session[42]: Accepted publickey for %s from %s port 50022 ssh2: ED25519 SHA256:AAAA"
                     % (iso(ago), user, ip))
    lines.append("@@ports")
    for item in ports:
        addr, proc = item.split()
        lines.append('LISTEN 0      4096   %s  0.0.0.0:* users:(("%s",pid=1,fd=3))   ' % (addr, proc))
    lines += ["@@containers"] + list(containers) + ["@@images"] + list(images) + ["@@ps"]
    lines += ["      1       0 systemd         /sbin/init", "      2       0 kthreadd        [kthreadd]",
              "     35       2 kswapd0         [kswapd0]"] + list(ps)
    lines += ["@@exe", "/proc/1 /usr/lib/systemd/systemd", "/proc/2 "] + list(exe)
    return "\n".join(lines) + "\n"


class SecurityParseTest(unittest.TestCase):
    def test_logins_keep_only_time_user_ip(self):
        lines = ["%s h sshd[1]: Accepted password for deploy from 2001:db8::1 port 2 ssh2" % iso(1),
                 "%s h sshd[1]: Accepted publickey for root from 198.51.100.7 port 2 ssh2: RSA SHA256:x" % iso(30),
                 "Oct 10 11:00:00 h sshd[1]: Accepted publickey for ann from 192.0.2.9 port 2 ssh2",
                 "%s h sshd[1]: Accepted publickey for x from not-an-ip port 2 ssh2" % iso(1),
                 "%s h sshd[1]: Failed password for root from 203.0.113.5 port 2 ssh2" % iso(1)]
        found = security.parse_logins(lines, NOW)
        self.assertEqual([(u, ip, m) for _, u, ip, m in found], [("deploy", "2001:db8::1", "password"), ("ann", "192.0.2.9", "key")])
        self.assertAlmostEqual(found[0][0], NOW - 3600, delta=1)

    def test_ports_skip_loopback_ephemeral(self):
        ports = security.parse_ports([
            'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))', "LISTEN 0 4096 [::]:443 [::]:*",
            "LISTEN 0 4096 127.0.0.1:41234 0.0.0.0:*", "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*", "garbage"])
        self.assertEqual(ports, {"0.0.0.0:22": "sshd", "[::]:443": "", "127.0.0.53%lo:53": ""})

    def test_suspicious_processes(self):
        ps = ["  100     1 kswapd0  [kswapd0]", "  101     1 Cli      /app/Cli start accept",
              "  102     1 defunct  defunct", "  103     1 python3  python3 /tmp/x.py",
              "  104     1 nginx    nginx: worker", "  105  4000 bash     [bash] <defunct>",
              "  106     1 gs-dbus  gs-dbus", "  107     1 sh       /dev/shm/.x/run"]
        exe = ["/proc/100 /tmp/.X11/kswapd0", "/proc/102 /root/.config/htop/defunct", "/proc/103 /tmp/x (deleted)",
               "/proc/104 /usr/sbin/nginx (deleted)", "/proc/106 /usr/bin/gs-dbus", "/proc/107 /dev/shm/.x/run"]
        found = security.suspicious(ps, exe)
        self.assertEqual(sorted(found), ["bad:gs-dbus", "deleted:/tmp/x", "fake:[kswapd0]", "path:/dev/shm/.x/run",
                                         "path:/root/.config/htop/defunct"])
        self.assertEqual(found["fake:[kswapd0]"], ("fake", "[kswapd0]", "/tmp/.X11/kswapd0"))

    def test_parse_and_first_findings(self):
        scan = security.parse(sec_out(), NOW)
        self.assertEqual(security.observed(scan), {"ips": ["198.51.100.7"], "ports": ["0.0.0.0:22", "127.0.0.1:5432"],
                                                   "containers": ["shop"], "images": ["nginx"]})
        self.assertEqual(security.findings("web", scan, None, t_for("en")), [])
        self.assertIsNone(security.parse(None, NOW))
        self.assertIsNone(security.parse("Welcome!\n", NOW))

    def test_findings_against_baseline(self):
        base = {"ips": ["198.51.100.7"], "ports": ["0.0.0.0:22"], "containers": ["shop"], "images": ["nginx"]}
        scan = security.parse(sec_out(
            logins=(("root", "198.51.100.7", 1), ("root", "203.0.113.9", 2), ("deploy", "203.0.113.9", 3)),
            ports=("0.0.0.0:22 sshd", "0.0.0.0:8443 xray"),
            containers=("shop|nginx|Up", "earn|earnfm/earnfm-client:latest|Up 2 hours"), images=("nginx", "earnfm/earnfm-client"),
            ps=["  900     1 kworker  [kworker/0:2]"], exe=["/proc/900 /var/tmp/.k/kworker"],
            password="yes", root="yes", f2b="inactive", ua="off", sec=4), NOW)
        found = security.findings("web", scan, base, t_for("en"))
        keys = [k for _, k, _ in found]
        self.assertEqual(keys, ["sec:web:ip:203.0.113.9", "sec:web:port:0.0.0.0:8443", "sec:web:container:earn",
                                "sec:web:image:earnfm/earnfm-client", "sec:web:badimage:earn",
                                "sec:web:proc:fake:[kworker/0:2]", "sec:web:password", "sec:web:rootpw", "sec:web:f2b",
                                "sec:web:ua", "sec:web:updates"])
        texts = dict((k, v) for _, k, v in found)
        self.assertEqual(texts["sec:web:ip:203.0.113.9"], "web: SSH login from a new IP 203.0.113.9 (deploy, root)")
        self.assertEqual(texts["sec:web:port:0.0.0.0:8443"], "web: new listening port 0.0.0.0:8443 (xray)")
        self.assertIn("known miner", texts["sec:web:badimage:earn"])

    def test_sshd_from_files_uses_defaults(self):
        scan = security.parse(sec_out(sshd_src="files"), NOW)
        self.assertEqual(security.ssh_settings(scan["facts"]), ("yes", "keys"))
        self.assertEqual([k for _, k, _ in security.findings("web", scan, None, t_for("en"))], ["sec:web:password"])

    def test_merge_adds_ips_replaces_the_rest(self):
        merged = security.merge({"ips": ["a"], "ports": ["x"]}, {"ips": ["b"], "ports": ["y"], "containers": [], "images": []}, 5)
        self.assertEqual((merged["ips"], merged["ports"], merged["time"]), (["a", "b"], ["y"], 5))


class FakeCert:
    """The TLS layer: host → notAfter string, or an exception to raise."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, host, port, timeout=10):
        self.calls.append((host, port))
        value = self.table.get(host)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise OSError("connection refused")
        return {"notAfter": value}


def cert_date(days_from_now, now=None):
    return time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime((now or time.time()) + days_from_now * 86400))


class SecurityDirectorTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:2])
        self.d.remote = FakeRemote({("", security.PROBE): sec_out(logins=()), ("web", security.PROBE): sec_out(),
                                    ("", servers.PROBE): kv(), ("web", servers.PROBE): kv()})
        self.d.cert_connect = FakeCert({})

    def baseline(self):
        with open(os.path.join(self.tmp, security.FILE)) as fh:
            return json.load(fh)

    def test_first_run_stores_baseline_then_flags_new_items(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        first = self.last()
        self.assertIn("Security — ", first)
        self.assertIn("Online 2 of 2 · ✅ all fine", first)
        self.assertIn("🆕 first check", first)
        self.assertIn("✅ SSH logins in 24 h: 1 from 1 IPs", first)
        self.assertIn("root ← 198.51.100.7", first)
        self.assertNotIn("SHA256", first)  # never the log line itself
        self.assertIn("✅ SSH passwords: off · root: keys only", first)
        self.assertIn("✅ fail2ban · ✅ automatic updates · ✅ updates: 3, security: 0", first)
        self.assertEqual(sorted(self.baseline()["servers"]), ["main", "web"])
        self.assertEqual(os.stat(os.path.join(self.tmp, security.FILE)).st_mode & 0o777, 0o600)
        self.d.remote.outputs[("web", security.PROBE)] = sec_out(ports=("0.0.0.0:22 sshd", "0.0.0.0:8443 xray"),
                                                                containers=("shop|nginx|Up", "miner|xmrig/xmrig|Up"))
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        text = self.last()
        self.assertIn("⚠️ problems: 3", text)
        self.assertIn("⚠️ Listening ports: 2, new: 1", text)
        self.assertIn("❌ Containers: 2, images: 1, new: 1", text)
        self.assertIn("What to check:\n• ⚠️ web: new listening port 0.0.0.0:8443 (xray)", text)
        self.assertIn("❌ web: container miner runs xmrig/xmrig", text)
        self.assertTrue(text.endswith("/security accept — I'll take them as the new normal."))
        self.msg("/security accept")
        self.assertIn("New normal saved for 2 servers", self.last())
        self.assertEqual(self.baseline()["servers"]["web"]["ports"], ["0.0.0.0:22", "0.0.0.0:8443"])
        self.assertEqual(self.d.audit.tail(2)[0]["text"], "security accept: main, web")
        self.msg("/security")
        self.assertIn("⚠️ problems: 1", self.last())  # a miner image is never "normal"
        self.assertNotIn("/security accept", self.last())  # accepting would not change that

    def test_unreachable_and_unreadable(self):
        del self.d.remote.outputs[("web", security.PROBE)]
        self.d.remote.outputs[("", security.PROBE)] = sec_out(logsrc="none", sshd_src="none", docker=False)
        self.d.cfg.lang = "ru"
        self.msg("/security")
        text = self.last()
        self.assertIn("На связи 1 из 2", text)
        self.assertIn("🔐 web · Amsterdam\n   ❌ нет связи по SSH", text)
        self.assertIn("⚠️ Входы по SSH: не могу прочитать журнал", text)
        self.assertIn("⚠️ Настройки SSH: не могу прочитать", text)
        self.assertNotIn("Контейнеров", text)
        self.assertEqual(sorted(self.baseline()["servers"]), ["main"])  # no baseline for a server that did not answer

    def test_certificates_of_sites_are_in_the_report(self):
        write_json(os.path.join(self.tmp, "sites.json"), [{"url": "https://shop.example.com", "name": "shop"},
                                                          {"url": "https://old.example.com", "name": "old"},
                                                          {"url": "https://ok.example.com", "name": "ok"},
                                                          {"url": "http://plain.example.com", "name": "plain"}])
        import ssl
        err = ssl.SSLCertVerificationError("certificate verify failed")
        err.verify_message = "certificate has expired"
        self.d.cert_connect = FakeCert({"shop.example.com": cert_date(5), "old.example.com": err,
                                        "ok.example.com": cert_date(60)})
        self.msg("/security")
        text = self.last()
        self.assertIn("🔒 Site certificates\n   ⚠️ shop: the certificate expires in ", text)
        self.assertIn("❌ old: certificate problem — certificate has expired", text)
        self.assertNotIn("ok:", text)
        self.assertNotIn("plain.example.com", [h for h, _ in self.d.cert_connect.calls])

    def test_watchdog_alerts_once_and_login_ips_never_resolve_loudly(self):
        sent = []
        self.d.broadcast = sent.append
        with mock.patch.object(director, "server_problems", return_value=[]):
            self.d.watch_once(NOW)  # no baseline yet: security is not watched
            self.assertEqual(sent, [])
            self.assertNotIn(("web", security.PROBE), [(c[0], c[1]) for c in self.d.remote.calls])
            self.msg("/security")
            self.d.remote.outputs[("web", security.PROBE)] = sec_out(logins=(("root", "203.0.113.9", 1),))
            with mock.patch.object(director.time, "time", return_value=NOW):
                self.d.watch_once(NOW)
                self.d.watch_once(NOW)
            self.assertEqual(sent, ["🚨 Server check:\n• 🔐 web: SSH login from a new IP 203.0.113.9 (root)"])
            self.d.remote.outputs[("web", security.PROBE)] = sec_out(logins=())
            self.d.watch_once(NOW)
        self.assertEqual(len(sent), 1)  # the IP left the 24-hour window: nothing to announce
        self.assertEqual(self.d.audit.tail(1)[0]["kind"], "resolved")


# ---------- 2. sites ----------

class FakeHTTP:
    """url → (status, body) or an exception."""

    def __init__(self, table):
        self.table, self.calls = dict(table), []

    def __call__(self, url, timeout=15):
        self.calls.append(url)
        value = self.table.get(url, OSError("connection refused"))
        if isinstance(value, Exception):
            raise value
        return value


def rdap_record(days_from_now, now=None):
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) + days_from_now * 86400))
    return {"objectClassName": "domain", "events": [{"eventAction": "registration", "eventDate": "2001-01-01T00:00:00Z"},
                                                    {"eventAction": "expiration", "eventDate": when}]}


class SitesUnitTest(unittest.TestCase):
    def test_load_checks_entries(self):
        tmp = tempfile.mkdtemp()
        write_json(os.path.join(tmp, "sites.json"), [
            {"url": "https://shop.example.com/", "name": "shop", "expect": 200, "server": "web"},
            {"url": "http://example.org:8080/health", "expect": "ok"},
            {"url": "ftp://x.example"}, {"url": "https://a b.example"}, {"url": "https://-x.example"},
            {"url": "https://dup.example", "name": "shop"}, {"url": "https://e.example", "expect": True}])
        found, warnings, configured = sites.load(tmp)
        self.assertTrue(configured)
        self.assertEqual([(s["name"], s["host"], s["port"], s["https"], s["expect"]) for s in found],
                         [("shop", "shop.example.com", 443, True, 200), ("example.org", "example.org", 8080, False, "ok"),
                          ("e.example", "e.example", 443, True, None)])
        self.assertEqual(len(warnings), 4)
        self.assertEqual(sites.load(tempfile.mkdtemp()), ([], [], False))

    def test_registrable_domain(self):
        self.assertEqual(sites.registrable("shop.example.com"), "example.com")
        self.assertEqual(sites.registrable("a.b.example.co.uk"), "example.co.uk")
        self.assertIsNone(sites.registrable("192.0.2.1"))
        self.assertIsNone(sites.registrable("localhost"))

    def test_rdap_expiry_and_daily_cache(self):
        import threading
        now = time.time()
        calls = []

        def fetch_json(domain, timeout=15):
            calls.append(domain)
            if domain == "broken.example":
                raise OSError("rdap down")
            return rdap_record(40, now) if domain == "example.com" else {}
        tmp = tempfile.mkdtemp()
        rdap = sites.Rdap(tmp, threading.RLock(), fetch_json)
        self.assertAlmostEqual(rdap.expiry("example.com", now), now + 40 * 86400, delta=2)
        self.assertIsNone(rdap.expiry("example.ru", now))
        self.assertIsNone(rdap.expiry("broken.example", now))
        for _ in range(3):
            rdap.expiry("example.com", now + 3600)
            rdap.expiry("broken.example", now + 3600)
        self.assertEqual(calls, ["example.com", "example.ru", "broken.example"])
        rdap.expiry("example.com", now + 86401)
        self.assertEqual(calls[-1], "example.com")
        self.assertEqual(os.stat(os.path.join(tmp, "rdap-cache.json")).st_mode & 0o777, 0o600)
        self.assertIsNone(sites.rdap_expiry({"events": "junk"}))

    def test_reasons(self):
        import socket
        import ssl
        import urllib.error
        err = ssl.SSLCertVerificationError()
        err.verify_message = "certificate has expired"
        self.assertEqual(sites.reason(urllib.error.URLError(err)), "certificate has expired")
        self.assertEqual(sites.reason(urllib.error.URLError(socket.timeout("timed out"))), "timeout")
        self.assertEqual(sites.reason(TimeoutError()), "timeout")
        self.assertEqual(sites.reason(urllib.error.URLError(socket.gaierror(-2, "x"))), "DNS: no such host")
        self.assertEqual(sites.reason(urllib.error.URLError(ConnectionRefusedError())), "connection refused")

    def test_real_fetch_reads_http_errors_on_loopback(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = 503 if self.path == "/down" else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"maintenance" if code == 503 else b"hello ok")

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        self.assertEqual(sites.fetch(base + "/"), (200, b"hello ok"))
        self.assertEqual(sites.fetch(base + "/down"), (503, b"maintenance"))


class SitesDirectorTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "sites.json"), [
            {"url": "https://shop.example.com/", "name": "shop", "expect": 200, "server": "web"},
            {"url": "https://api.example.org/health", "name": "api", "expect": "ok"},
            {"url": "http://192.0.2.10/", "name": "legacy"}])
        write_json(os.path.join(self.tmp, "servers.json"), [
            {"ssh": "", "name": "main", "hosting_paid_until": "2027-01-01"},
            {"ssh": "web", "name": "web", "hosting_paid_until": "2026-10-14"}])
        self.now = NOW
        self.d.http_fetch = FakeHTTP({"https://shop.example.com/": (200, b"<html>shop</html>"),
                                      "https://api.example.org/health": (200, b'{"status": "ok"}'),
                                      "http://192.0.2.10/": (301, b"")})
        self.d.cert_connect = FakeCert({"shop.example.com": cert_date(64, NOW), "api.example.org": cert_date(9, NOW)})
        self.rdap_calls = []

        def rdap(domain, timeout=15):
            self.rdap_calls.append(domain)
            return rdap_record({"example.com": 210, "example.org": 20}[domain], NOW)
        self.d.rdap_fetch = rdap
        self.d.remote = FakeRemote({("", servers.PROBE): kv(), ("web", servers.PROBE): kv()})

    def test_report(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/sites")
        text = self.last()
        self.assertIn("Sites — 10.10 12:00", text)
        self.assertIn("Up 3 of 3 · ⚠️ problems: 2", text)
        self.assertRegex(text, r"✅ shop — 200 · \d\.\d\d s · cert 64 d · domain 210 d")
        self.assertRegex(text, r"⚠️ api — 200 · \d\.\d\d s · cert 9 d · domain 20 d")
        self.assertRegex(text, r"✅ legacy — 301 · \d\.\d\d s\n")
        self.assertIn("🏠 Hosting\n   ⚠️ web: paid until 2026-10-14 (4 d)\n   ✅ main: paid until 2027-01-01 (83 d)", text)
        self.assertIn("What to check:\n• ⚠️ api: the certificate expires in 9 d\n"
                      "• ⚠️ example.org: the domain expires in 20 d — renew it", text)
        self.assertEqual(sorted(self.rdap_calls), ["example.com", "example.org"])

    def test_down_and_content_mismatch(self):
        import urllib.error
        import socket
        self.d.http_fetch.table["https://shop.example.com/"] = urllib.error.URLError(socket.timeout())
        self.d.http_fetch.table["https://api.example.org/health"] = (200, b"maintenance")
        self.d.cfg.lang = "ru"
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("сайты")
        text = self.last()
        self.assertIn("Работают 1 из 3", text)
        self.assertIn("❌ shop — не отвечает: timeout · домен 210 дн.", text)
        self.assertRegex(text, r"❌ api — 200 · \d\.\d\d с · на странице нет «ok»")
        self.assertIn("• ❌ shop не работает: timeout (сервер web)", text)
        self.assertIn("• ❌ api не работает: на странице нет «ok»", text)

    def test_no_sites_file_explains(self):
        os.unlink(os.path.join(self.tmp, "sites.json"))
        self.msg("/sites")
        self.assertIn("No sites to check yet", self.last())
        self.assertIn('"expect": 200', self.last())

    def test_watchdog_down_twice_then_back_and_daily_reminders(self):
        sent = []
        self.d.broadcast = sent.append
        with mock.patch.object(director, "server_problems", return_value=[]):
            self.d.watch_once(NOW)
            self.assertEqual(len(sent), 1)  # only the daily reminders
            self.assertTrue(sent[0].startswith("📅 Reminder:\n"))
            self.assertIn("api: the certificate expires in 9 d", sent[0])
            self.assertIn("example.org: the domain expires in 20 d", sent[0])
            self.assertIn("web: hosting is paid until 2026-10-14 — 4 d left", sent[0])
            self.d.http_fetch.table["https://shop.example.com/"] = (502, b"Bad Gateway")
            self.d.watch_once(NOW + 600)
            self.assertEqual(len(sent), 1)  # one failed check is not "down" yet
            self.d.watch_once(NOW + 1200)
            self.assertEqual(sent[-1], "🚨 Server check:\n• 🌐 shop is down: HTTP 502 (server web)")
            self.d.watch_once(NOW + 1800)
            self.assertEqual(len(sent), 2)
            self.d.http_fetch.table["https://shop.example.com/"] = (200, b"")
            self.d.watch_once(NOW + 2400)
            self.assertEqual(sent[-1], "✅ Resolved:\n• 🌐 shop is back")
            self.d.http_fetch.table["https://shop.example.com/"] = (502, b"")
            self.d.watch_once(NOW + 3000)
            self.assertEqual(len(sent), 3)  # the counter started again
            self.d.watch_once(NOW + 86400)
            self.assertTrue(sent[-1].startswith("📅 Reminder:"))  # next day: reminders again
        self.assertEqual(self.d.http_fetch.calls.count("https://shop.example.com/"), 7)
        self.assertEqual(len(self.rdap_calls), 4)  # cached: once a day per domain


# ---------- 3. backups ----------

from vps_director import backups  # noqa: E402


def mask(text):
    return director.mask_secrets(text)


class BackupsUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_load_and_max_age(self):
        write_json(os.path.join(self.tmp, "backups.json"), [
            {"type": "log", "name": "db", "path": "/var/log/db.log", "ok": "done", "fail": "error", "max_age": "26h"},
            {"type": "dir", "name": "dumps", "path": "/srv/dumps", "pattern": "*.gz", "max_age": 2},
            {"type": "restic", "name": "off", "repository": "sftp:b:/r", "password_file": "/p", "verify_paths": ["/etc", "rel"]},
            {"type": "log", "name": "rel", "path": "var/log/x"}, {"type": "zip", "name": "z"},
            {"type": "log", "name": "re", "path": "/x", "ok": "("}, {"type": "restic", "name": "nopw", "repository": "r"},
            {"type": "dir", "name": "dumps", "path": "/y"}, {"type": "dir", "name": "age", "path": "/z", "max_age": "weekly"}])
        entries, warnings, configured = backups.load(self.tmp)
        self.assertTrue(configured)
        self.assertEqual([(e["name"], e["max_age"]) for e in entries],
                         [("db", 26 * 3600), ("dumps", 2 * 86400), ("off", 26 * 3600), ("age", 26 * 3600)])
        self.assertEqual(entries[2]["verify_paths"], ["/etc"])
        self.assertEqual(len(warnings), 6)
        self.assertEqual(backups.max_age("1d12h"), 129600)
        self.assertIsNone(backups.max_age(True))

    def test_log_probe_on_this_machine(self):
        log = os.path.join(self.tmp, "backup's log.txt")  # a quote and a space: the path goes on stdin
        with open(log, "w") as fh:
            fh.write("2026-10-08 04:30:01 backup done: 2.1 GB\n2026-10-09 04:30:00 backup error: disk full password=hunter2\n"
                     "2026-10-09 04:31 unrelated line\n")
        entries = [{"type": "log", "name": "db", "path": log, "ok": __import__("re").compile("done"),
                    "fail": __import__("re").compile("error"), "max_age": 86400, "server": ""}]
        out = remote.run("", backups.LOG_PROBE, stdin=log + "\n")
        res = backups.check_log(entries[0], out, NOW, mask)
        self.assertEqual(res["state"], "fail")
        self.assertEqual(res["time"], time.mktime((2026, 10, 9, 4, 30, 0, 0, 0, -1)))
        self.assertNotIn("hunter2", res["line"])
        entries[0]["fail"] = None
        res = backups.check_log(entries[0], out, NOW, mask)
        self.assertEqual((res["state"], res["time"]), ("ok", time.mktime((2026, 10, 8, 4, 30, 1, 0, 0, -1))))
        self.assertEqual(backups.check_log(entries[0], remote.run("", backups.LOG_PROBE, stdin="/no/such\n"), NOW, mask),
                         {"state": "missing"})
        self.assertEqual(backups.check_log(entries[0], None, NOW, mask)["state"], "error")

    def test_log_line_without_date_uses_mtime(self):
        entry = {"ok": __import__("re").compile("OK"), "fail": None}
        out = "@@meta\nmtime=%d\n@@tail\nbackup OK\nother\n" % (NOW - 7200)
        self.assertEqual(backups.check_log(entry, out, NOW, mask)["time"], NOW - 7200)
        self.assertEqual(backups.check_log(entry, "@@meta\nmtime=5\n@@tail\nnothing\n", NOW, mask)["state"], "none")

    def test_dir_probe_on_this_machine(self):
        folder = os.path.join(self.tmp, "dumps")
        os.makedirs(os.path.join(folder, "sub"))
        for name, ago in (("a.sql.gz", 3), ("sub/b.sql.gz", 1), ("c.txt", 0)):
            path = os.path.join(folder, name)
            with open(path, "w") as fh:
                fh.write("x" * 10)
            os.utime(path, (NOW - ago * 3600, NOW - ago * 3600))
        res = backups.check_dir(remote.run("", backups.DIR_PROBE, stdin="%s\n*.sql.gz\n" % folder))
        self.assertEqual((res["state"], res["file"], res["size"], res["count"]), ("ok", "sub/b.sql.gz", 10, 2))
        self.assertAlmostEqual(res["time"], NOW - 3600, delta=1)
        self.assertEqual(backups.check_dir(remote.run("", backups.DIR_PROBE, stdin="/no/such\n*\n")), {"state": "missing"})
        self.assertEqual(backups.check_dir(remote.run("", backups.DIR_PROBE, stdin="%s\n*.zip\n" % folder)),
                         {"state": "none", "count": 0})

    def test_restic_env_never_in_argv(self):
        env_file = os.path.join(self.tmp, "restic.env")
        with open(env_file, "w") as fh:
            fh.write("export AWS_ACCESS_KEY_ID=AKIA123\nAWS_SECRET_ACCESS_KEY='s3cr3t'\nPATH=/evil\nRESTIC_PASSWORD=x\n")
        entry = {"repository": "s3:host/bucket", "password_file": "/root/.pw", "env_file": env_file, "host": ""}
        env = backups.restic_env(entry, {"PATH": "/usr/bin", "RESTIC_PASSWORD_COMMAND": "cat x"})
        self.assertEqual(env, {"PATH": "/usr/bin", "AWS_ACCESS_KEY_ID": "AKIA123", "AWS_SECRET_ACCESS_KEY": "s3cr3t",
                               "RESTIC_REPOSITORY": "s3:host/bucket", "RESTIC_PASSWORD_FILE": "/root/.pw"})
        calls = []

        def runner(argv, env, timeout):
            calls.append((argv, env))
            return 0, json.dumps([{"time": "2026-10-10T04:30:01.123456789+03:00", "short_id": "ab12cd34",
                                   "summary": {"total_bytes_processed": 3 * 1024 ** 3}},
                                  {"time": "2026-10-09T04:30:00Z", "short_id": "old"}]), ""
        res = backups.check_restic(dict(entry, env_file=""), runner, "/usr/bin/restic", mask)
        self.assertEqual(res["id"], "ab12cd34")
        self.assertEqual(res["size"], 3 * 1024 ** 3)
        self.assertAlmostEqual(res["time"], backups.snapshot_time("2026-10-10T01:30:01Z"), delta=1)
        self.assertEqual(calls[0][0], ["/usr/bin/restic", "snapshots", "--latest", "1", "--json", "--no-lock"])
        self.assertNotIn("s3:host/bucket", " ".join(calls[0][0]))
        res = backups.check_restic(entry, lambda a, e, t: (1, "", "Fatal: unable to open repo at rest:https://u:pw@h/: 401\n"),
                                   "/usr/bin/restic", mask)
        self.assertEqual(res, {"state": "error", "why": "Fatal: unable to open repo at rest:https://***@h/: 401"})
        self.assertEqual(backups.check_restic(entry, runner, "", mask)["why"], "restic is not installed")

    def test_verify_with_a_fake_restic(self):
        bindir = tempfile.mkdtemp()
        log = os.path.join(self.tmp, "argv.txt")
        write_exe(bindir, "restic", '#!/bin/sh\necho "$@ | $RESTIC_REPOSITORY" > %s\n'
                                    'while [ $# -gt 0 ]; do [ "$1" = --target ] && t=$2; shift; done\n'
                                    'mkdir -p "$t/etc/nginx"; echo a > "$t/etc/hosts"; echo bb > "$t/etc/nginx/n.conf"\n' % log)
        entry = {"name": "off", "repository": "sftp:b:/r", "password_file": "/p", "env_file": "", "host": "",
                 "verify_paths": ["/etc"], "verify_dir": self.tmp}
        res = backups.verify(entry, backups.run_restic, os.path.join(bindir, "restic"), mask)
        self.assertEqual((res["state"], res["files"], res["bytes"]), ("ok", 2, 5))
        with open(log) as fh:
            self.assertEqual(fh.read().split(" | ")[1].strip(), "sftp:b:/r")
        with open(log) as fh:
            argv = fh.read().split(" | ")[0].split()
        self.assertEqual(argv[:3], ["restore", "latest", "--no-lock"])
        self.assertEqual(argv[-2:], ["--include", "/etc"])
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("director-verify-")], [])  # removed
        failing = backups.verify(entry, lambda a, e, t: (1, "", "Fatal: wrong password\n"), "/usr/bin/restic", mask)
        self.assertEqual(failing, {"state": "error", "why": "Fatal: wrong password"})
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("director-verify-")], [])
        self.assertEqual(backups.verify(dict(entry, verify_paths=[]), None, "/x", mask)["why"], "no verify_paths")


class BackupsDirectorTest(Base):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "db.log")
        with open(self.log, "w") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 5 * 3600)) + " backup done\n")
        write_json(os.path.join(self.tmp, "servers.json"), [{"ssh": "", "name": "main"}, {"ssh": "web", "name": "web"}])
        write_json(os.path.join(self.tmp, "backups.json"), [
            {"type": "log", "name": "db", "path": self.log, "ok": "done", "fail": "error", "max_age": "26h"},
            {"type": "dir", "name": "web dumps", "path": "/srv/dumps", "max_age": "2d", "server": "web"},
            {"type": "restic", "name": "offsite", "repository": "sftp:b:/r", "password_file": "/p", "max_age": "8d",
             "verify_paths": ["/etc"]}])
        self.dir_out = "newest=%d 1048576 2026-10-10.sql.gz\ncount=14\n" % (NOW - 3 * 86400)
        self.d.remote = FakeRemote({("", backups.LOG_PROBE): lambda stdin: remote.run("", backups.LOG_PROBE, stdin=stdin),
                                    ("web", servers.PROBE): kv(),
                                    ("web", backups.DIR_PROBE): lambda stdin: self.dir_out if stdin == "/srv/dumps\n*\n" else None})
        self.d.restic = "/usr/bin/restic"
        self.snapshot_age = 86400
        self.d.restic_run = lambda argv, env, timeout: (0, json.dumps([{
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - self.snapshot_age)), "short_id": "ab12",
            "summary": {"total_bytes_processed": 5 * 1024 ** 3}}]), "")

    def test_report(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/backups")
        text = self.last()
        self.assertIn("Backups — 10.10 12:00", text)
        self.assertIn("Fine 2 of 3 · ⚠️ problems: 1", text)
        self.assertIn("✅ db — 5 h ago · log\n", text)
        self.assertIn("⚠️ web dumps — last good copy 3 d ago — older than 2 d · 14 files · latest 1.0 MB · dir · web", text)
        self.assertIn("✅ offsite — 24 h ago · 5.0 GB · restic", text)
        self.assertIn("What to check:\n• ⚠️ web dumps: the last good copy is 3 d old (max 2 d)", text)
        self.assertIn("/backups verify", text)
        self.assertEqual(self.submitted, [])

    def test_failures_in_russian(self):
        with open(self.log, "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 3600)) + " backup error: no space left\n")
        self.dir_out = None
        self.d.restic_run = lambda argv, env, timeout: (1, "", "Fatal: repository does not exist\n")
        self.d.cfg.lang = "ru"
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("бэкапы")
        text = self.last()
        self.assertIn("В порядке 0 из 3", text)
        self.assertIn("❌ db — последний запуск УПАЛ, 1 ч назад · log\n   ", text)
        self.assertIn("backup error: no space left", text)
        self.assertIn("❌ web dumps — не могу проверить: no connection · dir · web", text)
        self.assertIn("❌ offsite — не могу проверить: Fatal: repository does not exist · restic", text)

    def test_unknown_server_and_no_file(self):
        write_json(os.path.join(self.tmp, "backups.json"), [{"type": "dir", "name": "x", "path": "/x", "server": "nope"}])
        self.msg("/backups")
        self.assertIn("cannot check: unknown server nope", self.last())
        os.unlink(os.path.join(self.tmp, "backups.json"))
        self.msg("/backups")
        self.assertIn("No backups to watch yet", self.last())

    def test_verify_command(self):
        calls = []

        def restic(argv, env, timeout):
            calls.append(argv)
            target = argv[argv.index("--target") + 1]
            with open(os.path.join(target, "hosts"), "w") as fh:
                fh.write("127.0.0.1 localhost\n")
            return 0, "", ""
        self.d.restic_run = restic
        self.msg("/backups verify offsite")
        self.assertIn("⏳ Restoring a test copy of offsite (/etc)", self.d.tg.sent[-2][1])
        self.assertIn("✅ offsite: restored 1 files, 20 B, from the latest snapshot", self.last())
        self.assertEqual(self.d.audit.tail(2)[0]["text"], "backups verify offsite")
        self.msg("/backups verify nope")
        self.assertIn("no restic backup with verify_paths «nope»", self.last())

    def test_watchdog(self):
        sent = []
        self.d.broadcast = sent.append
        with mock.patch.object(director, "server_problems", return_value=[]):
            self.d.watch_once(NOW)
            self.assertEqual(sent, ["🚨 Server check:\n• 💾 web dumps: the last good copy is 3 d old (max 2 d)"])
            self.dir_out = "newest=%d 10 x.gz\ncount=1\n" % (NOW - 3600)
            self.snapshot_age = 10 * 86400
            self.d.watch_once(NOW)
        self.assertEqual(sent[-2], "🚨 Server check:\n• 💾 offsite: the last good copy is 10 d old (max 8 d)")
        self.assertEqual(sent[-1], "✅ Resolved:\n• 💾 web dumps: the last good copy is 3 d old (max 2 d)")


# ---------- 4. morning brief ----------

class BriefSkillsTest(Base):
    def setUp(self):
        super().setUp()
        from vps_director import schedule
        self.d.cfg.tz, self.d.cfg.tz_name = schedule.zone("Europe/Moscow"), "Europe/Moscow"
        self.d.cfg.owner_name = "Ann"
        for p in (mock.patch.object(director, "server_problems", return_value=["disk /data is 93% full"]),
                  mock.patch.object(director, "disk_usage", return_value=[("41%", "/")])):
            p.start()
            self.addCleanup(p.stop)
        write_json(os.path.join(self.tmp, "servers.json"), [
            {"ssh": "", "name": "main", "main": True}, {"ssh": "web", "name": "web", "hosting_paid_until": "2026-10-13"}])
        write_json(os.path.join(self.tmp, "sites.json"), [{"url": "https://shop.example.com/", "name": "shop"},
                                                          {"url": "https://api.example.com/", "name": "api"}])
        write_json(os.path.join(self.tmp, "backups.json"), [
            {"type": "dir", "name": "dumps", "path": "/srv/dumps", "max_age": "26h", "server": "web"}])
        self.d.remote = FakeRemote({("", servers.PROBE): kv(), ("web", servers.PROBE): kv(disk_used=70 * GB),
                                    ("", security.PROBE): sec_out(logins=()), ("web", security.PROBE): sec_out(),
                                    ("web", backups.DIR_PROBE): "newest=%d 10 a.gz\ncount=3\n" % (NOW - 5 * 3600)})
        self.d.http_fetch = FakeHTTP({"https://shop.example.com/": (200, b""), "https://api.example.com/": (502, b"")})
        self.d.cert_connect = FakeCert({"shop.example.com": cert_date(10, NOW), "api.example.com": cert_date(90, NOW)})
        self.d.rdap_fetch = lambda domain, timeout=15: rdap_record(200, NOW)

    def test_brief_has_every_skill(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")  # stores the baseline: security joins the brief from now on
        self.d.remote.outputs[("web", security.PROBE)] = sec_out(ports=("0.0.0.0:22 sshd", "0.0.0.0:6379 redis"),
                                                                password="yes")
        text = self.d.brief_text(OWNER, NOW)
        self.assertTrue(text.startswith("☀️ Good afternoon, Ann! Here's Sat 10.10:\n"), text)
        self.assertIn("🖥 Servers: 2 of 2 online, problems:\n  • web: disk 88%\n  • main: disk /data is 93% full\n", text)
        self.assertIn("🔐 Security — new: 1\n  • web: new listening port 0.0.0.0:6379 (redis)\n", text)
        self.assertNotIn("accepts passwords", text)  # settings are /security's business, the brief shows changes
        self.assertIn("🌐 Sites — problems: 1 (of 2 sites)\n  • api is down: HTTP 502\n", text)
        self.assertIn("💾 Backups: all 1 fine, the oldest copy is 5 h old.\n", text)
        self.assertIn("📅 Coming up:\n  • web: hosting is paid until 2026-10-13 — 3 d left, renew it\n"
                      "  • shop: the certificate expires in 10 d\n", text)
        self.assertIn("🚨 No alerts in the last 24 h.", text)

    def test_all_fine_in_russian_and_one_broken_skill(self):
        self.d.cfg.lang = "ru"
        self.d.remote.outputs[("web", servers.PROBE)] = kv()
        self.d.http_fetch.table["https://api.example.com/"] = (200, b"")
        os.unlink(os.path.join(self.tmp, "backups.json"))
        with mock.patch.object(director, "server_problems", return_value=[]), \
                mock.patch.object(self.d, "sites_check", side_effect=RuntimeError("boom")):
            text = self.d.brief_text(OWNER, NOW)
        self.assertIn("🖥 Серверы: на связи 2 из 2, всё в порядке.", text)
        self.assertNotIn("Безопасность", text)  # no baseline yet: not watched
        self.assertNotIn("Сайты", text)  # the broken skill is left out, the brief still comes
        self.assertIn("💾 Бэкапы: не отслеживаются", text)
        self.assertIn("📅 Скоро:\n  • web: хостинг оплачен до 2026-10-13 — осталось 3 дн., продли", text)

    def test_checks_run_side_by_side(self):
        def slow(*args, **kw):
            time.sleep(0.3)
            return [], [], []
        self.msg("/security")
        with mock.patch.object(self.d, "sites_check", side_effect=slow), \
                mock.patch.object(self.d, "backups_check", side_effect=slow), \
                mock.patch.object(self.d, "security_check", side_effect=lambda now: time.sleep(0.3) or []):
            start = time.monotonic()
            self.d.brief_text(OWNER, NOW)
        self.assertLess(time.monotonic() - start, 0.8)

    def test_scheduled_brief_needs_no_ai(self):
        self.d.cfg.brief = "09:00"
        self.d.sync_jobs()
        job = next(j for j in self.d.state.get()["jobs"].values() if j["kind"] == "brief")
        self.d.run_due_jobs(job["next"] + 1)
        self.assertIn("🌐 Sites — problems: 1", self.last())
        self.assertEqual(self.submitted, [])


# ---------- 07 controller note: localhost listeners are info, ignore_ports ----------

class LocalPortsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_ignore_ports_parsed_and_checked(self):
        write_json(os.path.join(self.tmp, "servers.json"), [
            {"ssh": "", "name": "main", "ignore_ports": [10904, "10900-10999", "22"]},
            {"ssh": "web", "name": "web", "ignore_ports": ["x", "9-3", 70000, True]},
            {"ssh": "db", "name": "db", "ignore_ports": "22"}])
        found, warnings, _ = servers.load(self.tmp)
        self.assertEqual(found[0]["ignore_ports"], [[10904, 10904], [10900, 10999], [22, 22]])
        self.assertEqual(found[1]["ignore_ports"], [])
        self.assertEqual(found[2]["ignore_ports"], [])
        self.assertEqual(len([w for w in warnings if "ignore_ports" in w]), 5)

    def test_ignored_ports_dropped_from_the_scan(self):
        scan = security.parse(sec_out(ports=("0.0.0.0:22 sshd", "127.0.0.1:10904 xray", "[::]:10950 x")), NOW)
        scan = security.drop_ignored(scan, [[10900, 10999]])
        self.assertEqual(sorted(scan["ports"]), ["0.0.0.0:22"])
        self.assertIs(security.drop_ignored(scan, []), scan)

    def test_localhost_listener_is_info(self):
        base = {"ips": ["198.51.100.7"], "ports": ["0.0.0.0:22"], "containers": ["shop"], "images": ["nginx"]}
        scan = security.parse(sec_out(ports=("0.0.0.0:22 sshd", "127.0.0.1:5432 postgres", "[::1]:6379 redis",
                                             "0.0.0.0:8443 xray")), NOW)
        found = security.findings("web", scan, base, t_for("en"))
        levels = {k: lv for lv, k, _ in found}
        self.assertEqual(levels["sec:web:port:127.0.0.1:5432"], "info")
        self.assertEqual(levels["sec:web:port:[::1]:6379"], "info")
        self.assertEqual(levels["sec:web:port:0.0.0.0:8443"], "warn")
        self.assertTrue(security.is_local("127.0.0.53%lo:53"))
        self.assertFalse(security.is_local("0.0.0.0:22"))
        text = security.report([({"name": "web"}, scan, base, found)], t_for("en"), None, "now")
        self.assertIn("⚠️ problems: 1", text)
        self.assertIn("• ⚠️ web: new listening port 0.0.0.0:8443 (xray)", text)
        self.assertIn("• ℹ️ web: new port on localhost only 127.0.0.1:5432 (postgres)", text)
        self.assertNotIn("• ⚠️ web: new listening port 127.0.0.1", text)


class LocalPortsDirectorTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "servers.json"),
                   [INVENTORY[0], dict(INVENTORY[1], ignore_ports=["10900-10999"])])
        self.d.remote = FakeRemote({("", security.PROBE): sec_out(logins=()), ("web", security.PROBE): sec_out(),
                                    ("", servers.PROBE): kv(), ("web", servers.PROBE): kv()})
        self.d.cert_connect = FakeCert({})

    def test_info_and_ignored_ports_raise_no_alert(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        self.d.remote.outputs[("web", security.PROBE)] = sec_out(
            ports=("0.0.0.0:22 sshd", "127.0.0.1:5432 postgres", "127.0.0.1:7000 app", "127.0.0.1:10904 xray"))
        items, _ = self.d.watch_skills(NOW)
        self.assertFalse([k for k in items if k.startswith("sec:")], items)
        text = self.d.brief_text(OWNER, NOW)
        self.assertNotIn("7000", text)
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        text = self.last()
        self.assertIn("ℹ️ web: new port on localhost only 127.0.0.1:7000 (app)", text)
        self.assertNotIn("10904", text)


class ContainerIssueTest(unittest.TestCase):
    """«Containers worker» said nothing: the problem now says what is wrong, in words."""

    def test_status_in_words(self):
        from vps_director import servers as srv
        from vps_director.texts import TEXTS
        t = lambda key, **kw: TEXTS["ru"][key].format(**kw)  # noqa: E731
        data = {"dk_total": "4", "dk_ok": "0", "dk_bad": "worker,db,web,old",
                "dk_why": "worker~Exited (1) 3 hours ago,db~Up 2 days (unhealthy),web~Restarting (1) 5 seconds ago,old~Dead"}
        self.assertEqual([srv.container_issue(n, s, t) for n, s in srv.bad_containers(data)],
                         ["worker: остановлен (код 1)", "db: работает, но не проходит проверку здоровья",
                          "web: перезапускается по кругу", "old: мёртв"])
        # an older probe without dk_why still works (names only)
        self.assertEqual(srv.bad_containers({"dk_bad": "worker"}), [("worker", "")])
        self.assertEqual(srv.container_issue("worker", "", t), "worker")


class OldPythonTimeFormatsTest(unittest.TestCase):
    """Formats Python 3.10's fromisoformat rejects: journald's +0200 and short fractions from restic."""

    def test_journald_offset_without_colon(self):
        from vps_director import security
        self.assertEqual(security.login_time("2026-10-10T12:00:00+0200 h sshd[1]: Accepted", 0),
                         security.login_time("2026-10-10T12:00:00+02:00 h sshd[1]: Accepted", 0))
        self.assertIsNotNone(security.login_time("2026-10-10T12:00:00+0200 h sshd[1]: Accepted", 0))

    def test_restic_short_fraction(self):
        from vps_director import backups
        self.assertAlmostEqual(backups.snapshot_time("2026-10-10T12:00:00.5Z"),
                               backups.snapshot_time("2026-10-10T12:00:00Z") + 0.5)
