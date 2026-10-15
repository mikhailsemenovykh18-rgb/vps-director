"""Task 07c: the Mini App's own API helpers (assistant data, /api/me fields), the CSP, the static
frontend files and commands.json structure."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_webapi import ApiBase  # noqa: E402

from vps_director import console, web  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class AssistantDataTest(ApiBase):
    def test_off_by_default(self):
        self.assertEqual(self.call("GET", "/api/console/commands").status, 403)
        self.assertEqual(self.call("GET", "/api/console/history").status, 403)

    def test_commands_from_path(self):
        self.d.cfg.web_console = True
        tmp = tempfile.mkdtemp()
        for name, mode in (("dokku", 0o755), ("notes.txt", 0o644), (".hidden", 0o755), ("bad name", 0o755)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(path, mode)
        with mock.patch.object(console, "clean_env", lambda shell: {"PATH": tmp + ":relative:/nonexistent", "HOME": tmp}):
            res = self.call("GET", "/api/console/commands")
            self.assertEqual(res.data["commands"], ["dokku"])
            os.remove(os.path.join(tmp, "dokku"))
            self.assertEqual(self.call("GET", "/api/console/commands").data["commands"], ["dokku"])  # cached

    def test_history_read_only_dedup_and_no_secrets(self):
        self.d.cfg.web_console = True
        home = tempfile.mkdtemp()
        lines = ["ls", "#1700000000", "docker ps", "ls", "export TOKEN=abc123secretvalue", "curl -H 'Authorization: Bearer xyz' x",
                 "x" * 600, "", "git status"]
        with open(os.path.join(home, ".bash_history"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        with mock.patch.object(console, "clean_env", lambda shell: {"PATH": "/bin", "HOME": home}):
            hist = self.call("GET", "/api/console/history").data["history"]
        self.assertEqual(hist, ["docker ps", "ls", "git status"])

    def test_assistant_data_comes_from_the_consoles_server(self):
        """A console on another server suggests ITS commands and history (over ssh), not this machine's."""
        self.d.cfg.web_console = True
        calls = []

        def fake_run(alias, script, stdin="", timeout=25):
            calls.append((alias, script))
            if "bash_history" in script:
                return "cut-in-half\nsystemctl status nginx\nexport TOKEN=abc123secret\nsystemctl status nginx\n"
            return "nginx\npsql\nbad name\n\nnginx\n"

        servers = [{"name": "web", "ssh": "web-nl"}, {"name": "main"}]
        with mock.patch.object(type(self.d), "inventory", lambda d: (servers, [])), \
                mock.patch.object(console.remote, "run", fake_run):
            self.assertEqual(self.call("GET", "/api/console/commands?server=web").data["commands"], ["nginx", "psql"])
            self.assertEqual(self.call("GET", "/api/console/history?server=web").data["history"], ["systemctl status nginx"])
            self.assertEqual(self.call("GET", "/api/console/commands?server=nope").status, 404)
            self.assertEqual(self.call("GET", "/api/console/history?server=nope").status, 404)
            self.call("GET", "/api/console/commands?server=web")  # cached per server
        self.assertEqual([a for a, _ in calls], ["web-nl", "web-nl"])

    def test_history_limit_and_missing_file(self):
        path = os.path.join(tempfile.mkdtemp(), "h")
        self.assertEqual(console.history_lines(path), [])
        with open(path, "w") as fh:
            fh.write("\n".join("echo %d" % i for i in range(2000)))
        out = console.history_lines(path)
        self.assertEqual((len(out), out[-1], out[0]), (500, "echo 1999", "echo 1500"))

    def test_console_status_ids_still_work(self):
        self.d.cfg.web_console = True
        self.assertEqual(self.call("GET", "/api/console/abcdef12").status, 404)

    def test_me_has_settings_fields(self):
        self.d.cfg.brief = "09:00"
        data = self.call("GET", "/api/me").data
        self.assertEqual((data["brief"], data["console"]), ("09:00", False))
        self.assertIn("tz_label", data)


class CspTest(unittest.TestCase):
    def test_no_inline_no_cdn(self):
        policy = web.WebApp(web_url="https://dash.example.com").csp
        self.assertIn("script-src 'self' https://telegram.org;", policy)
        self.assertIn("style-src 'self';", policy)
        self.assertIn("connect-src 'self' wss://dash.example.com;", policy)
        self.assertNotIn("unsafe", policy)
        self.assertNotIn("jsdelivr", policy)
        self.assertIn("connect-src 'self';", web.WebApp().csp)

    def test_header_uses_app_policy(self):
        app = web.WebApp(web_url="https://dash.example.com")
        self.assertIn("wss://dash.example.com", app.handle("GET", "/", {}).header("Content-Security-Policy"))


class StaticFilesTest(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as fh:
            return fh.read()

    def test_no_inline_scripts_or_styles(self):
        html = self.read("index.html")
        for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
            self.assertEqual(tag.strip(), "")
        self.assertNotRegex(html, r"<style\b|\sstyle=|\son\w+=")
        for src in re.findall(r'<script src="([^"]+)"', html):
            self.assertTrue(src == "https://telegram.org/js/telegram-web-app.js" or os.path.isfile(os.path.join(WEB, src)), src)

    def test_no_html_injection_sinks(self):
        for name in os.listdir(WEB):
            if name.endswith(".js"):
                code = self.read(name)
                self.assertNotRegex(code, r"\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", name)

    def test_strings_have_both_languages(self):
        code = self.read("i18n.js")
        en = re.search(r"en: \{(.*?)\n    \},\n    ru:", code, re.S).group(1)
        ru = re.search(r"ru: \{(.*?)\n    \}\n  \};", code, re.S).group(1)
        keys = lambda block: set(re.findall(r"(\w+): [\"\[]", block))  # noqa: E731
        self.assertEqual(keys(en), keys(ru))
        self.assertGreater(len(keys(en)), 50)


if __name__ == "__main__":
    unittest.main()


class VendorTest(unittest.TestCase):
    def test_vendored_xterm_matches_the_table(self):
        import hashlib
        folder = os.path.join(WEB, "vendor", "xterm")
        with open(os.path.join(folder, "VERSIONS.txt"), encoding="utf-8") as fh:
            rows = re.findall(r"^\S+\s+\S+\s+(\S+\.(?:js|css))\s+([0-9a-f]{64})$", fh.read(), re.M)
        self.assertEqual(len(rows), 4)
        for name, digest in rows:
            with open(os.path.join(folder, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, name)
        self.assertTrue(os.path.isfile(os.path.join(folder, "LICENSE.txt")))

    def test_served_with_the_right_type(self):
        app = web.WebApp()
        res = app.handle("GET", "/vendor/xterm/xterm.js", {})
        self.assertEqual((res.status, res.content_type), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(app.handle("GET", "/vendor/xterm/xterm.js.map", {}).status, 404)


class FontsTest(unittest.TestCase):
    def test_vendored_fonts_match_the_table(self):
        import hashlib
        folder = os.path.join(WEB, "vendor", "fonts")
        with open(os.path.join(folder, "VERSIONS.txt"), encoding="utf-8") as fh:
            rows = re.findall(r"^([0-9a-f]{64})\s+(\S+\.woff2)$", fh.read(), re.M)
        self.assertEqual(len(rows), 2)
        for digest, name in rows:
            with open(os.path.join(folder, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, name)
        self.assertTrue(os.path.isfile(os.path.join(folder, "LICENSE.txt")))
        res = web.WebApp().handle("GET", "/vendor/fonts/Geist-Variable.woff2", {})
        self.assertEqual((res.status, res.content_type), (200, "font/woff2"))


class CommandsJsonTest(unittest.TestCase):
    """web/commands.json is hand-curated: the structure the assistant relies on is checked here."""

    def setUp(self):
        with open(os.path.join(WEB, "commands.json"), encoding="utf-8") as fh:
            self.data = json.load(fh)

    def check_node(self, where, node, depth=0):
        self.assertIsInstance(node, dict, where)
        self.assertLessEqual(set(node), {"en", "ru", "sub", "flags", "examples"}, where)
        for lang in ("en", "ru"):
            self.assertIsInstance(node.get(lang), str, where)
            self.assertTrue(0 < len(node[lang]) <= 120, where)
        if lang == "ru":
            self.assertRegex(node["ru"], r"[А-Яа-яЁё]|^[\w\s:/'.,+→()-]+$", where)
        for name, sub in node.get("sub", {}).items():
            self.assertRegex(name, r"^[\w.:+=-]+$", where)
            self.assertLess(depth, 4, where)
            self.check_node(where + " " + name, sub, depth + 1)
        for flag, info in node.get("flags", {}).items():
            self.assertRegex(flag, r"^[-+]?[\w%=,.+-]+$", where + " " + flag)
            self.assertEqual(set(info), {"en", "ru"}, where + " " + flag)
            self.assertTrue(info["en"] and info["ru"], where + " " + flag)
        for ex in node.get("examples", []):
            self.assertEqual(set(ex), {"line", "en", "ru"}, where)
            self.assertTrue(ex["line"].startswith(where.split()[0]), ex["line"])

    def test_structure(self):
        self.assertEqual(self.data["version"], 1)
        commands = self.data["commands"]
        self.assertGreaterEqual(len(commands), 40)
        for name in ("systemctl", "docker", "journalctl", "git", "apt", "ufw", "nginx", "caddy", "ss", "df", "du", "tail",
                     "grep", "restic", "crontab"):
            self.assertIn(name, commands)
        self.assertIn("compose", commands["docker"]["sub"])
        for name, node in commands.items():
            self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
            self.check_node(name, node)

    def test_size_stays_small(self):
        self.assertLess(os.path.getsize(os.path.join(WEB, "commands.json")), 128 * 1024)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeTestsTest(unittest.TestCase):
    def test_assistant_unit_tests(self):
        files = sorted(os.path.join(WEB, "tests", f) for f in os.listdir(os.path.join(WEB, "tests")) if f.endswith(".test.js"))
        res = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])


class ExecutableTest(unittest.TestCase):
    def test_entry_points_are_executable_in_git(self):
        import subprocess
        root = os.path.dirname(WEB)
        out = subprocess.run(["git", "ls-files", "-s", "install.sh", "director.py"], cwd=root, capture_output=True, text=True).stdout
        if out:  # not a git checkout (an archive): nothing to check
            self.assertTrue(all(line.startswith("100755") for line in out.splitlines()), out)
