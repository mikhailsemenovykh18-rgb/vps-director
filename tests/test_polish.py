"""Task 06b: output polish for the built-in skills. Offline, fakes only."""
import os
import sys
import tempfile
import time
import unittest
import zoneinfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import Base, director  # noqa: E402
from test_skills import (NOW, FakeCert, FakeHTTP, FakeRemote, INVENTORY, cert_date, rdap_record, sec_out,  # noqa: E402
                         t_for, write_json)

from vps_director import backups, remote, security, servers, sites  # noqa: E402


def with_addrs(out, addrs="", client=""):
    out = out.replace("@@logins", "client=%s\n@@logins" % client, 1)
    return out + "@@addrs\n%s\n" % addrs


class LoginAggregationTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:2])
        many = tuple(("root", "198.51.100.7", h * 0.1) for h in range(1, 13)) + (("app", "203.0.113.5", 5),)
        self.d.remote = FakeRemote({
            ("", security.PROBE): with_addrs(sec_out(logins=(), docker=False), "198.51.100.7 10.0.0.2"),
            ("web", security.PROBE): with_addrs(sec_out(logins=many), "192.0.2.50", client="198.51.100.7"),
            ("", servers.PROBE): "", ("web", servers.PROBE): ""})
        self.d.cert_connect = FakeCert({})

    def test_probe_logins_grouped_and_own_ip_labelled(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")  # first scan: baseline
            self.msg("/security")
        text = self.last()
        self.assertIn("SSH logins in 24 h: 13 from 2 IPs", text)
        self.assertIn("root ← main (main, this bot) ×12", text)
        self.assertEqual(text.count("root ← 198.51.100.7"), 0)
        self.assertIn("app ← 203.0.113.5", text)
        self.assertNotIn("new IP", text)  # the bot's own probes never alert
        self.assertNotIn("more", text)

    def test_own_ip_never_alerts_even_with_a_stale_baseline(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        base = security.load_baseline(self.tmp)
        base["servers"]["web"]["ips"] = []
        security.save_baseline(self.tmp, base)
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        text = self.last()
        self.assertIn("203.0.113.5", text)
        self.assertIn("new IP 203.0.113.5", text)
        self.assertNotIn("new IP 198.51.100.7", text)

    def test_other_server_ip_is_labelled_by_name(self):
        out = sec_out(logins=(("root", "192.0.2.50", 1),))
        self.d.remote.outputs[("", security.PROBE)] = with_addrs(out, "198.51.100.7")
        self.d.remote.outputs[("web", security.PROBE)] = with_addrs(sec_out(logins=()), "192.0.2.50")
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        self.assertIn("root ← web ×1", self.last())

    def test_aggregate_unit(self):
        logins = [(100, "root", "1.1.1.1", "key"), (300, "root", "1.1.1.1", "key"), (200, "a", "2.2.2.2", "password")]
        got = security.aggregate_logins(logins)
        self.assertEqual(got[0], (300, "root", "1.1.1.1", 2, "key"))
        self.assertEqual(got[1][3], 1)


class FooterTest(unittest.TestCase):
    def test_security_footer_uses_one_format(self):
        t = t_for("en")
        rows = [({"name": "a"}, security.parse(sec_out(password="yes", root="yes", ua="off"), NOW),
                 {"ips": [], "ports": [], "containers": [], "images": []}, None)]
        rows = [(s, scan, base, security.findings("a", scan, base, t)) for s, scan, base, _ in rows]
        text = security.report(rows, t, None, "stamp", [("bad", "Shop: certificate problem — expired")])
        footer = text.split("What to check:\n")[1].split("\n\n")[0].splitlines()  # then localhost-only info (07)
        self.assertTrue(all(line.startswith(("• ⚠️ ", "• ❌ ")) for line in footer), footer)
        self.assertIn("• ⚠️ a: automatic security updates are off", footer)
        self.assertIn("• ❌ a: root can log in over SSH with a password", footer)
        self.assertIn("• ❌ Shop: certificate problem — expired", footer)

    def test_helper(self):
        self.assertEqual(remote.footer_line("bad", "x"), "• ❌ x")
        self.assertEqual(remote.footer_line("warn", "x"), "• ⚠️ x")


class SiteDownCertTest(Base):
    def setUp(self):
        super().setUp()
        write_json(os.path.join(self.tmp, "sites.json"), [{"url": "https://shop.example.com/", "name": "shop"}])
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:1])
        self.d.remote = FakeRemote({("", servers.PROBE): "", ("", security.PROBE): sec_out()})
        self.d.cert_connect = FakeCert({"shop.example.com": TimeoutError("timed out")})
        self.d.http_fetch = FakeHTTP({"https://shop.example.com/": OSError("timeout")})

    def test_sites_down_has_no_certificate_item(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/sites")
        text = self.last()
        self.assertEqual(text.count("shop"), text.count("shop is down") + text.count("shop — "))
        self.assertNotIn("certificate", text)
        self.assertIn("• ❌ shop is down", text)

    def test_security_certs_skip_unreachable_sites(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        self.assertNotIn("certificate", self.last())
        self.assertNotIn("Site certificates", self.last())

    def test_real_certificate_problems_still_reported(self):
        import ssl
        err = ssl.SSLCertVerificationError(1, "certificate has expired")
        err.verify_message = "certificate has expired"
        self.d.cert_connect = FakeCert({"shop.example.com": err})
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/security")
        self.assertIn("• ❌ shop: certificate problem — certificate has expired", self.last())


WHOIS_RU = """% TCP/UDP whois
domain:        EXAMPLE.RU
state:         REGISTERED, DELEGATED, VERIFIED
paid-till:     2027-03-04T21:00:00Z
"""


class WhoisTest(unittest.TestCase):
    def test_parse_formats(self):
        ts = lambda s: sites.whois_expiry(s)  # noqa: E731
        self.assertIsNotNone(ts(WHOIS_RU))
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(ts(WHOIS_RU))), "2027-03-04")
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(ts("Registry Expiry Date: 2026-12-01T10:00:00Z\n"))), "2026-12-01")
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(ts("expires:  2026-05-06\n"))), "2026-05-06")
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(ts("Expiration Date: 2026.05.06\n"))), "2026-05-06")
        self.assertIsNone(ts("No match for domain\n"))
        self.assertIsNone(ts(""))

    def test_fallback_when_rdap_has_nothing_and_cache(self):
        tmp = tempfile.mkdtemp()
        calls = []
        lock = __import__("threading").RLock()

        def whois(domain):
            calls.append(domain)
            return WHOIS_RU
        rdap = sites.Rdap(tmp, lock, fetch_json=lambda d, timeout=15: {}, whois=whois)
        first = rdap.expiry("example.ru", NOW)
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(first)), "2027-03-04")
        self.assertEqual(rdap.expiry("example.ru", NOW + 3600), first)
        self.assertEqual(calls, ["example.ru"])  # cached for a day

    def test_nothing_works_is_silent(self):
        tmp = tempfile.mkdtemp()
        lock = __import__("threading").RLock()

        def boom(d, timeout=15):
            raise OSError("down")
        rdap = sites.Rdap(tmp, lock, fetch_json=boom, whois=lambda d: None)
        self.assertIsNone(rdap.expiry("example.ru", NOW))
        site = {"url": "https://example.ru", "name": "r", "host": "example.ru", "https": False, "port": 80, "expect": None}
        res = sites.check(site, NOW, lambda url, timeout: (200, b""), None, rdap)
        _, line, problems, rem = sites.assess(site, res, NOW, t_for("en"))
        self.assertNotIn("domain", line)
        self.assertEqual((problems, rem), ([], []))

    def test_whois_cli_missing_or_failing(self):
        with mock.patch.object(sites.shutil, "which", return_value=None):
            self.assertIsNone(sites.run_whois("example.ru"))
        with mock.patch.object(sites.shutil, "which", return_value="/usr/bin/whois"), \
                mock.patch.object(sites.subprocess, "run", side_effect=OSError("x")):
            self.assertIsNone(sites.run_whois("example.ru"))
        self.assertIsNone(sites.run_whois("bad domain; rm -rf"))

    def test_whois_argv_has_no_shell_and_punycode(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return mock.Mock(returncode=0, stdout=WHOIS_RU)
        with mock.patch.object(sites.shutil, "which", return_value="/usr/bin/whois"), \
                mock.patch.object(sites.subprocess, "run", fake_run):
            self.assertEqual(sites.run_whois("пример.рф"), WHOIS_RU)
        self.assertEqual(seen["argv"], ["/usr/bin/whois", "--", "xn--e1afmkfd.xn--p1ai"])
        self.assertNotIn("shell", seen["kw"])


class StampTest(Base):
    def stamp(self, tz, lang="en"):
        self.d.cfg.tz_name, self.d.cfg.tz, self.d.cfg.lang = tz, zoneinfo.ZoneInfo(tz) if tz else None, lang
        return self.d.stamp(NOW)

    def test_moscow(self):
        self.assertTrue(self.stamp("Europe/Moscow", "ru").endswith(" МСК"))
        self.assertTrue(self.stamp("Europe/Moscow", "en").endswith(" MSK"))

    def test_named_zone_and_numeric_abbreviation(self):
        self.assertTrue(self.stamp("Europe/Berlin").endswith((" CEST", " CET")))
        self.assertTrue(self.stamp("Asia/Dubai").endswith(" Asia/Dubai"))  # tzname is "+04"

    def test_headers_use_it(self):
        self.d.cfg.tz_name, self.d.cfg.tz, self.d.cfg.lang = "Europe/Moscow", zoneinfo.ZoneInfo("Europe/Moscow"), "ru"
        write_json(os.path.join(self.tmp, "servers.json"), INVENTORY[:1])
        self.d.remote = FakeRemote({("", servers.PROBE): ""})
        self.msg("/servers")
        self.assertRegex(self.last().splitlines()[0], r"^Серверы — \d\d\.\d\d \d\d:\d\d МСК$")


class DirBackupTest(unittest.TestCase):
    def test_probe_output_and_text(self):
        res = backups.check_dir("newest=1700000000.0 249 a.sql.gz\ncount=26\ntotal=13000000\n")
        self.assertEqual((res["size"], res["total"], res["count"]), (249, 13000000, 26))
        entry = {"type": "dir", "name": "dumps", "server": "", "max_age": 10 ** 9, "path": "/x", "pattern": "*"}
        _, line, _ = backups.assess(entry, dict(res, time=NOW), NOW, t_for("en"))
        self.assertIn("26 files · latest 249 B · total 12.4 MB", line)
        _, line, _ = backups.assess(entry, dict(res, time=NOW), NOW, t_for("ru"))
        self.assertIn("файлов: 26 · последний 249 B · всего 12.4 MB", line)

    def test_real_probe(self):
        folder = tempfile.mkdtemp()
        for name, size in (("a.sql.gz", 100), ("b.sql.gz", 300), ("c.txt", 5000)):
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(b"x" * size)
        os.utime(os.path.join(folder, "a.sql.gz"), (1700000000, 1700000000))
        res = backups.check_dir(remote.run("", backups.DIR_PROBE, stdin="%s\n*.sql.gz\n" % folder))
        self.assertEqual((res["count"], res["size"], res["total"], res["file"]), (2, 300, 400, "b.sql.gz"))
        res = backups.check_dir(remote.run("", backups.DIR_PROBE, stdin="%s\n*.zip\n" % folder))
        self.assertEqual(res, {"state": "none", "count": 0})


if __name__ == "__main__":
    unittest.main()


class OldFailureCacheTest(unittest.TestCase):
    def test_failure_cached_before_whois_is_retried(self):
        import json
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, sites.RDAP_FILE), "w") as fh:
            json.dump({"example.ru": {"checked": NOW, "expires": None}}, fh)
        rdap = sites.Rdap(tmp, __import__("threading").RLock(), fetch_json=lambda d, timeout=15: {}, whois=lambda d: WHOIS_RU)
        self.assertIsNotNone(rdap.expiry("example.ru", NOW + 60))
