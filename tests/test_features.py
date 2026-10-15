"""Task 04 features: wildcard rule, setup, /continue, scheduler, audit log. Offline like test_director."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import OWNER, Base, FakeTelegram, director, make_cfg  # noqa: E402,F401


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def press(d, data):
    d.callback({"id": "1", "from": {"id": int(OWNER)}, "message": {"chat": {"id": int(OWNER)}}, "data": data})


def button_data(buttons):
    return [b["callback_data"] for row in buttons or [] for b in row]


class WildcardTest(Base):
    """Item 0: a Bash command with * never gets an Allow button — the rule would be a pattern."""

    def test_star_command_gets_no_allow_button(self):
        key, _ = self.d.state.new_session("claude")
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": "rm /var/log/app/*.log"}}])
        _, text, buttons = self.d.tg.sent[-1]
        self.assertIn("*", text)
        self.assertIn("/full", text)
        self.assertFalse([b for b in button_data(buttons) if b.startswith("allow:")])
        self.assertEqual(self.d.state.get().get("pending") or {}, {})

    def test_mixed_denials_allow_only_the_exact_ones(self):
        key, _ = self.d.state.new_session("claude")
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": "ls *"}},
                                           {"tool_name": "Bash", "tool_input": {"command": "df -h"}}])
        texts = [t for _, t, _ in self.d.tg.sent]
        self.assertTrue(any("ls *" in t and "/full" in t for t in texts))
        press(self.d, button_data(self.d.tg.sent[-1][2])[0])
        self.assertEqual(self.submitted[-1][2], ("Bash(df -h)",))

    def test_permission_text_shows_the_command_literally(self):
        key, _ = self.d.state.new_session("claude")
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": "python3 -m my__mod --x"}}])
        self.assertIn("Bash(python3 -m my__mod --x)", self.last())  # Markdown cleanup would eat the "__"
        tg = director.Telegram(self.cfg, lambda m: None)
        with mock.patch.object(tg, "api", return_value={"result": {"message_id": 1}}) as api:
            tg.send(OWNER, "a **b** c", raw=True)
            tg.send(OWNER, "a **b** c")
        self.assertEqual([c.args[1]["text"] for c in api.call_args_list], ["a **b** c", "a b c"])

    def test_star_in_an_edit_path_is_also_refused(self):
        key, _ = self.d.state.new_session("claude")
        self.d.ask_permission(OWNER, key, [{"tool_name": "Write", "tool_input": {"file_path": "/srv/*.conf"}}])
        self.assertFalse([b for b in button_data(self.d.tg.sent[-1][2]) if b.startswith("allow:")])



# ---------- item 1: setup ----------

from vps_director import setup as vsetup  # noqa: E402

TOKEN = "123456:secretTOKENvalue_xyz"


class FakeBotAPI:
    """Telegram as seen by `director.py setup`: getMe and a scripted getUpdates."""

    def __init__(self, polls, backlog=(), conflict=False):
        self.polls, self.backlog, self.conflict, self.calls, self.now = list(polls), list(backlog), conflict, [], 0.0

    def __call__(self, method, payload, token, timeout):
        self.calls.append((method, dict(payload or {}), token))
        if token != TOKEN:
            raise director.urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)
        if method == "getMe":
            return {"ok": True, "result": {"username": "my_vps_bot"}}
        if method == "getUpdates":
            if self.conflict:
                raise director.urllib.error.HTTPError("https://x", 409, "Conflict", {}, None)
            if payload.get("offset") == -1:
                return {"ok": True, "result": self.backlog}
            if payload.get("timeout"):
                self.now += 30  # a long poll takes time
                return {"ok": True, "result": self.polls.pop(0) if self.polls else []}
            return {"ok": True, "result": []}
        raise AssertionError(method)


def private(uid, upd, first="Ann", username=None, lang="en"):
    user = {"id": uid, "is_bot": False, "first_name": first, "language_code": lang}
    if username:
        user["username"] = username
    return {"update_id": upd, "message": {"message_id": 1, "chat": {"id": uid, "type": "private"}, "from": user,
                                          "text": "hi"}}


class SetupTest(unittest.TestCase):
    def run_setup(self, api, answers, secrets=(TOKEN,), force=False, conf=None):
        self.conf = conf or os.path.join(tempfile.mkdtemp(), "main")
        self.out = []
        answers, secrets = list(answers), list(secrets)
        wiz = vsetup.Setup(self.conf, api, ask=lambda p: (self.out.append(p), answers.pop(0) if answers else "")[1],
                           secret=lambda p: (self.out.append(p), secrets.pop(0))[1],
                           out=self.out.append, clock=lambda: api.now, sleep=lambda s: None, force=force)
        wiz.run()
        return director.read_env(os.path.join(self.conf, ".env"))

    def test_token_then_first_private_message_becomes_the_owner(self):
        group = {"update_id": 11, "message": {"chat": {"id": -5, "type": "group"}, "from": {"id": 7}, "text": "x"}}
        api = FakeBotAPI(polls=[[group, private(201, 12, "Eve")], [private(202, 13, "Mike", "mike_k", lang="ru")]],
                         backlog=[private(999, 10, "Old")])
        env = self.run_setup(api, answers=["n", "y"], secrets=["not a token", "1:wrong", TOKEN])
        self.assertEqual((env["BOT_TOKEN"], env["OWNER_IDS"], env["LANG"]), (TOKEN, "202", "ru"))
        text = "\n".join(self.out)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("secretTOKEN", text)
        self.assertIn("@my_vps_bot", text)
        self.assertIn("Mike (@mike_k)", text)
        self.assertNotIn("Old", text)  # messages sent before the setup started are skipped
        self.assertEqual(api.calls[-1][:2], ("getUpdates", {"offset": 14, "timeout": 0}))  # acknowledged
        self.assertEqual(os.stat(os.path.join(self.conf, ".env")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.conf).st_mode & 0o777, 0o700)

    def test_bot_speaks_the_owners_telegram_language(self):
        env = self.run_setup(FakeBotAPI(polls=[[private(5, 1, lang="de")]]), answers=["yes"])
        self.assertEqual((env["OWNER_IDS"], env["LANG"]), ("5", "de"))

    def test_english_for_languages_we_do_not_have(self):
        env = self.run_setup(FakeBotAPI(polls=[[private(5, 1, lang="ja")]]), answers=["yes"])
        self.assertEqual(env["LANG"], "en")

    def test_wizard_language_follows_the_server_locale(self):
        from vps_director import setup
        self.assertEqual(setup.ui_lang({"LANG": "ru_RU.UTF-8"}), "ru")
        self.assertEqual(setup.ui_lang({"LANG": "C.UTF-8"}), "en")
        self.assertEqual(setup.ui_lang({"LC_ALL": "en_US.UTF-8", "LANG": "ru_RU.UTF-8"}), "en")
        self.assertEqual(setup.bot_lang("pt-br"), "pt")

    def test_no_message_in_time_falls_back_to_typing_the_id(self):
        env = self.run_setup(FakeBotAPI(polls=[]), answers=["abc", "42", "ru"])
        self.assertEqual((env["OWNER_IDS"], env["LANG"]), ("42", "ru"))
        self.assertTrue(any("did not" in line.lower() or "no message" in line.lower() for line in self.out))

    def test_existing_config_is_kept_without_force(self):
        conf = os.path.join(tempfile.mkdtemp(), "main")
        os.makedirs(conf)
        with open(os.path.join(conf, ".env"), "w") as fh:
            fh.write("BOT_TOKEN=old\nOWNER_IDS=1\n")
        with self.assertRaises(vsetup.SetupError):
            self.run_setup(FakeBotAPI(polls=[]), answers=[], conf=conf)
        self.assertEqual(director.read_env(os.path.join(conf, ".env"))["BOT_TOKEN"], "old")
        env = self.run_setup(FakeBotAPI(polls=[[private(3, 1)]]), answers=["y"], conf=conf, force=True)
        self.assertEqual(env["OWNER_IDS"], "3")

    def test_another_poller_is_explained(self):
        with self.assertRaises(vsetup.SetupError) as ctx:
            self.run_setup(FakeBotAPI(polls=[], conflict=True), answers=[])
        self.assertIn("another", str(ctx.exception).lower())

    def test_steps_are_extensible(self):
        """Task 05 adds questions (bot name, how to address the owner, …) as extra steps."""
        def ask_name(wiz):
            wiz.env["NAME"] = wiz.question("What should I call myself?", default="VPS Director")
        api = FakeBotAPI(polls=[[private(5, 1, first="Ann")]])
        conf = os.path.join(tempfile.mkdtemp(), "main")
        answers = ["y", ""]
        wiz = vsetup.Setup(conf, api, ask=lambda p: answers.pop(0) if answers else "", secret=lambda p: TOKEN, out=lambda s: None,
                           clock=lambda: api.now, sleep=lambda s: None)
        wiz.steps = wiz.steps + [ask_name]
        wiz.run()
        self.assertEqual(wiz.facts["owner"]["first_name"], "Ann")
        self.assertEqual(director.read_env(os.path.join(conf, ".env"))["NAME"], "VPS Director")

    def test_main_runs_setup_before_any_config_check(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"DIRECTOR_CONF": tmp}), \
                mock.patch.object(director, "run_setup", return_value=0) as run:
            self.assertEqual(director.main(["setup", "--force"]), 0)
        run.assert_called_once_with(tmp, True)

    def test_run_setup_reports_errors_without_a_traceback(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(vsetup.Setup, "run", side_effect=vsetup.SetupError("no owner")), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(director.run_setup(tmp), 1)
        self.assertIn("no owner", "".join(c.args[0] for c in err.write.call_args_list))

    def test_installer_uses_setup_instead_of_asking_for_the_id(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh")) as fh:
            script = fh.read()
        self.assertIn("director.py\" setup", script)
        self.assertNotIn("userinfobot", script)



# ---------- item 2: /continue ----------

from vps_director import terminal  # noqa: E402

SID = "0b9f6f2e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
CODEX_SID = "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


def jsonl(path, records, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
            fh.write("\n")
    os.utime(path, (mtime, mtime))


class TerminalSessionsTest(Base):
    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        self.work = os.path.join(self.home, "my.site_x")
        os.makedirs(self.work)
        self.d.cfg.workdir = self.work
        self.now = time.time()
        proj = os.path.join(self.home, ".claude", "projects", terminal.claude_project_name(self.work))
        jsonl(os.path.join(proj, SID + ".jsonl"), [
            {"type": "summary", "summary": "x"},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<command-name>/init</command-name>"}},
            "not json at all {",
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "Fix the nginx config for the shop and reload it\nsecond line"}]}},
        ], self.now - 3 * 3600)
        newer = "1a2b3c4d-0000-4000-8000-000000000001"
        jsonl(os.path.join(proj, newer + ".jsonl"), [
            [1, 2], None, 7, {"type": "user", "message": "weird"},
            {"type": "user", "message": {"role": "user", "content": "why is the disk full?"}}], self.now - 120)
        jsonl(os.path.join(proj, "2a2b3c4d-0000-4000-8000-000000000002.jsonl"), ["garbage", "{}"], self.now - 60)
        jsonl(os.path.join(proj, "--resume-evil.jsonl"),
              [{"type": "user", "message": {"role": "user", "content": "evil"}}], self.now)
        sessions = os.path.join(self.home, ".codex", "sessions", "2026", "10", "09")
        jsonl(os.path.join(sessions, "rollout-2026-10-09T10-00-00-%s.jsonl" % CODEX_SID), [
            {"type": "session_meta", "payload": {"id": CODEX_SID, "cwd": self.work}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Upgrade postgres to 17"}},
        ], self.now - 2 * 86400)
        jsonl(os.path.join(sessions, "rollout-2026-10-09T11-00-00-9c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f.jsonl"), [
            {"type": "session_meta", "payload": {"id": "9c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f", "cwd": "/elsewhere"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "other project"}}], self.now)
        p = mock.patch.object(director, "HOME", self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_project_dir_name_like_claude_code(self):
        self.assertEqual(terminal.claude_project_name("/home/a.b/c_d"), "-home-a-b-c-d")

    def test_lists_newest_first_with_labels_and_skips_junk(self):
        found = terminal.find_sessions(self.work, self.home)
        self.assertEqual([(f["engine"], f["sid"]) for f in found], [
            ("claude", "1a2b3c4d-0000-4000-8000-000000000001"), ("claude", SID), ("codex", CODEX_SID)])
        self.assertEqual(found[0]["label"], "why is the disk full?")
        self.assertEqual(found[1]["label"], "Fix the nginx config for the shop and re…")
        self.assertEqual(found[2]["label"], "Upgrade postgres to 17")
        self.assertEqual([terminal.age(self.now - f["mtime"]) for f in found], ["2m", "3h", "2d"])

    def test_old_codex_format(self):
        old = os.path.join(self.home, ".codex", "sessions", "2025", "05", "01",
                           "rollout-2025-05-01-5d1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f.jsonl")
        jsonl(old, [{"id": "5d1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f", "timestamp": "2025", "instructions": ""},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old task"}]}],
              self.now - 5 * 86400)
        found = terminal.find_sessions(self.work, self.home)
        self.assertEqual(found[-1]["sid"], "5d1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f")
        self.assertEqual(found[-1]["label"], "old task")

    def test_at_most_eight_per_engine(self):
        proj = os.path.join(self.home, ".claude", "projects", terminal.claude_project_name(self.work))
        for i in range(12):
            jsonl(os.path.join(proj, "aaaaaaaa-0000-4000-8000-%012d.jsonl" % i),
                  [{"type": "user", "message": {"role": "user", "content": "task %d" % i}}], self.now - 10 - i)
        found = terminal.find_sessions(self.work, self.home)
        self.assertEqual(len([f for f in found if f["engine"] == "claude"]), 8)

    def test_continue_creates_a_session_with_that_sid(self):
        self.msg("/continue")
        text, buttons = self.d.tg.sent[-1][1], self.d.tg.sent[-1][2]
        self.assertIn("why is the disk full?", str(buttons))
        datas = button_data(buttons)
        self.assertEqual(len(datas), 3)
        self.assertTrue(all(len(x.encode()) <= 64 for x in datas))
        self.d.callback({"id": "1", "from": {"id": 999}, "data": datas[2]})  # a stranger: nothing happens
        self.assertNotIn("sessions", self.d.state.get())
        press(self.d, datas[2])
        key, sess = self.d.state.active()
        self.assertEqual((sess["engine"], sess["sid"], sess["mode"]), ("codex", CODEX_SID, "safe"))
        self.assertIn("#%s" % key, self.last())
        self.msg("and now 18?")
        self.assertEqual(self.submitted[-1][:2], (key, "and now 18?"))
        args = director.codex_args(self.d.cfg, sess, "/tmp/x")
        self.assertEqual(args[1:4], ["exec", "resume", CODEX_SID])

    def test_nothing_found(self):
        self.d.cfg.workdir = tempfile.mkdtemp()
        with mock.patch.object(director, "HOME", tempfile.mkdtemp()):
            self.msg("/continue")
        self.assertIn("No Claude Code or Codex sessions", self.last())



# ---------- item 3: scheduled tasks ----------

from vps_director import schedule  # noqa: E402


def local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class ScheduleParsingTest(unittest.TestCase):
    def test_durations(self):
        for text, sec in {"30m": 1800, "2h": 7200, "1d": 86400, "1h30m": 5400, "45min": 2700, "30м": 1800,
                          "2ч": 7200, "1д": 86400}.items():
            self.assertEqual(schedule.parse_duration(text), sec, text)
        for bad in ("90", "0m", "abc", "", "1x", "m", "-5m", "1h-"):
            self.assertIsNone(schedule.parse_duration(bad), bad)

    def test_when(self):
        now = local(2026, 10, 10, 10, 0)
        self.assertEqual(schedule.parse_when("30m", now), now + 1800)
        self.assertEqual(schedule.parse_when("14:30", now), local(2026, 10, 10, 14, 30))
        self.assertEqual(schedule.parse_when("9:05", now), local(2026, 10, 11, 9, 5))  # already past: tomorrow
        self.assertEqual(schedule.parse_when("10:00", now), local(2026, 10, 11, 10, 0))
        for bad in ("24:00", "9:5", "12:60", "soon", "400d"):
            with self.assertRaises(ValueError, msg=bad):
                schedule.parse_when(bad, now)

    def test_every(self):
        self.assertEqual(schedule.parse_every("5m"), {"every": 300})
        self.assertEqual(schedule.parse_every("7d"), {"every": 7 * 86400})
        self.assertEqual(schedule.parse_every("09:00"), {"daily": "09:00"})
        for bad in ("4m", "8d", "x", "25:00"):
            with self.assertRaises(ValueError, msg=bad):
                schedule.parse_every(bad)

    def test_next_run(self):
        now = local(2026, 10, 10, 10, 0)
        self.assertIsNone(schedule.next_run({"next": now - 5}, now))
        nxt = schedule.next_run({"next": now - 3 * 3600 - 60, "every": 3600}, now)  # missed runs: no burst
        self.assertEqual(nxt, now + 3600 - 60)
        self.assertEqual(schedule.next_run({"next": now, "daily": "09:00"}, now), local(2026, 10, 11, 9, 0))

    def test_describe(self):
        self.assertEqual(schedule.describe({"every": 5400}), "every 1h30m")
        self.assertEqual(schedule.describe({"daily": "09:00"}), "daily at 09:00")
        self.assertEqual(schedule.describe({}), "once")


class JobsTest(Base):
    def setUp(self):
        super().setUp()
        cdir = os.path.join(self.tmp, "commands")
        os.makedirs(cdir)
        self.counter = os.path.join(self.tmp, "out.txt")
        with open(self.counter, "w") as fh:
            fh.write("all good")
        with open(os.path.join(cdir, "disk.sh"), "w") as fh:
            fh.write("#!/bin/sh\ncat %s\n" % self.counter)
        os.chmod(os.path.join(cdir, "disk.sh"), 0o755)
        self.d.cfg.workdir = self.tmp

    def jobs(self):
        return self.d.state.get().get("jobs") or {}

    def test_at_and_every_store_jobs(self):
        before = time.time()
        self.msg("/at 2h check that the backup ran")
        job = self.jobs()["1"]
        self.assertEqual((job["kind"], job["text"], job["chat"]), ("task", "check that the backup ran", OWNER))
        self.assertAlmostEqual(job["next"], before + 7200, delta=5)
        self.assertIn("#1", self.last())
        self.msg("/every 1h /disk")
        self.assertEqual((self.jobs()["2"]["kind"], self.jobs()["2"]["every"], self.jobs()["2"]["changes"]),
                         ("command", 3600, True))
        self.msg("/every 1h --always /disk")
        self.assertFalse(self.jobs()["3"]["changes"])
        self.msg("/every 2h look at the nginx errors --changes")
        self.assertEqual((self.jobs()["4"]["text"], self.jobs()["4"]["changes"]), ("look at the nginx errors", True))
        self.assertEqual(self.submitted, [])  # nothing runs yet

    def test_invalid_input_is_explained(self):
        for text, expect in (("/every 3m x", "5m"), ("/at 25:00 x", "HH:MM"), ("/at 1h", "/at"),
                             ("/every 1h /new", "script"), ("/at", "/at"), ("/every", "/every")):
            self.msg(text)
            self.assertIn(expect, self.last(), text)
        self.assertEqual(self.jobs(), {})

    def test_jobs_list_and_delete(self):
        self.msg("/every 1h /disk")
        self.msg("/remind 10m drink water")
        self.msg("/jobs")
        text, buttons = self.d.tg.sent[-1][1], self.d.tg.sent[-1][2]
        self.assertIn("/disk", text)
        self.assertIn("drink water", text)
        datas = button_data(buttons)
        self.assertEqual(datas, ["jobdel:1", "jobdel:2"])
        self.d.callback({"id": "1", "from": {"id": 999}, "data": "jobdel:1"})
        self.assertIn("1", self.jobs())
        press(self.d, "jobdel:1")
        self.assertEqual(list(self.jobs()), ["2"])
        self.msg("/jobs")
        self.assertNotIn("/disk", self.last())

    def test_due_task_goes_to_its_own_session_and_one_shot_is_removed(self):
        key, _ = self.d.state.new_session("claude")
        self.msg("/at 30m check the certificates")
        self.d.run_due_jobs(time.time() + 1801)
        self.assertEqual(self.jobs(), {})
        job_key, prompt, _ = self.submitted[-1]
        self.assertEqual(prompt, "check the certificates")
        self.assertNotEqual(job_key, key)
        self.assertEqual(self.d.state.get()["active"], key)  # the owner's session is untouched
        self.assertEqual(self.d.state.sessions()[job_key]["mode"], "safe")

    def test_repeating_job_moves_on_and_survives_restart(self):
        self.msg("/every 1h check the queue")
        first = self.jobs()["1"]["next"]
        again = director.Director(self.cfg)  # a restart: state comes from disk
        again.tg, again.log, runs = FakeTelegram(), (lambda m: None), []
        again.submit = lambda chat, key, prompt, extra_tools=(), job=None: runs.append((key, prompt, job))
        again.run_due_jobs(first + 10)
        self.assertEqual(runs[0][1], "check the queue")
        self.assertEqual(runs[0][2]["id"], "1")
        self.assertEqual(again.state.get()["jobs"]["1"]["next"], first + 3600)
        again.run_due_jobs(first + 20)
        self.assertEqual(len(runs), 1)  # not due again
        again.run_due_jobs(first + 3600)
        self.assertEqual(runs[1][0], runs[0][0])  # the same job session

    def test_script_job_reports_only_changes(self):
        self.msg("/every 1h /disk")
        nxt = self.jobs()["1"]["next"]
        self.d.run_due_jobs(nxt)
        self.assertEqual(self.last(), "⏰ #1 /disk\nall good")
        sent = len(self.d.tg.sent)
        self.d.run_due_jobs(nxt + 3600)
        self.assertEqual(len(self.d.tg.sent), sent)  # same output: silence
        with open(self.counter, "w") as fh:
            fh.write("disk 95%")
        self.d.run_due_jobs(nxt + 7200)
        self.assertEqual(self.last(), "⏰ #1 /disk\ndisk 95%")
        self.assertEqual(self.submitted, [])  # scripts never reach the AI

    def test_task_job_with_changes_is_silent_when_the_answer_repeats(self):
        self.msg("/every 1h are there new errors in nginx --changes")
        job = {"id": "1", "label": "#1 errors", "changes": True}
        self.d.say(OWNER, "no new errors", job)
        self.d.say(OWNER, "no new errors", job)
        self.d.say(OWNER, "3 new 502s", job)
        self.assertEqual([t for _, t, _ in self.d.tg.sent[1:]], ["⏰ #1 errors\nno new errors", "⏰ #1 errors\n3 new 502s"])

    def test_real_queue_carries_the_job_to_the_runner(self):
        d = director.Director(make_cfg(tempfile.mkdtemp()))
        d.tg, d.log, ran = FakeTelegram(), (lambda m: None), []
        done = threading.Event()
        d.run_task = lambda *a: (ran.append(a), done.set())
        threading.Thread(target=d.worker, daemon=True).start()
        d.submit(OWNER, "1", "job text", job={"id": "4", "label": "#4 x"})
        self.assertTrue(done.wait(5))
        self.assertEqual(ran[0], (OWNER, "1", "job text", (), {"id": "4", "label": "#4 x"}))
        self.assertEqual(d.tg.sent, [])  # no "queued" chatter for unattended runs

    def test_reminder(self):
        self.msg("/remind 10m drink water")
        self.d.run_due_jobs(time.time() + 601)
        self.assertEqual(self.last(), "⏰ drink water")
        self.assertEqual((self.submitted, self.jobs()), ([], {}))

    def test_jobs_of_a_removed_owner_do_not_run(self):
        self.msg("/remind 10m drink water")
        self.d.cfg.owners = {"222"}
        self.d.run_due_jobs(time.time() + 601)
        self.assertNotIn("⏰ drink water", [t for _, t, _ in self.d.tg.sent])

    def test_unattended_run_is_safe_and_never_offers_allow(self):
        key, _ = self.d.state.new_session("claude", activate=False)
        self.d.state.merge_session(key, mode="full")  # even if someone switched the job session to full
        seen = {}

        def fake_stream(chat, args, col, stdin_text=""):
            seen["args"] = args
            col.result = "checked"
            col.denials = [{"tool_name": "Bash", "tool_input": {"command": "systemctl restart nginx"}}]
            return 0, ""
        self.d.stream = fake_stream
        self.d.run_task(OWNER, key, "restart nginx if needed", (), job={"id": "7", "label": "#7 restart nginx"})
        self.assertIn("default", seen["args"])
        self.assertNotIn("bypassPermissions", seen["args"])
        texts = [t for _, t, _ in self.d.tg.sent]
        self.assertEqual(texts[0], "⏰ #7 restart nginx\nchecked")
        self.assertIn("needs your approval", texts[1])
        self.assertIn("systemctl restart nginx", texts[1])
        self.assertTrue(all(b is None for _, _, b in self.d.tg.sent))
        self.assertEqual(self.d.state.get().get("pending") or {}, {})



# ---------- item 4: audit log and ALLOWED_ROOTS ----------

from vps_director import audit  # noqa: E402


class AuditLogTest(Base):
    def entries(self):
        path = os.path.join(self.tmp, "audit.log")
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_owner_actions_are_logged_masked_and_private(self):
        self.msg("check mysql -pS3cret works")
        self.msg("/full")
        self.msg("/safe")
        key, _ = self.d.state.active()
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": "rm x"}},
                                           {"tool_name": "Bash", "tool_input": {"command": "rm y"}}])
        press(self.d, button_data(self.d.tg.sent[-1][2])[0])
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": "rm z"}}])
        press(self.d, button_data(self.d.tg.sent[-1][2])[1])
        self.msg("/remind 10m stretch")
        self.d.run_due_jobs(time.time() + 601)
        kinds = [(e["kind"], e["from"]) for e in self.entries()]
        self.assertEqual(kinds, [("task", OWNER), ("mode", OWNER), ("mode", OWNER), ("allow", OWNER),
                                 ("deny", OWNER), ("job", OWNER), ("job", "scheduler")])
        entries = self.entries()
        self.assertNotIn("S3cret", json.dumps(entries))
        self.assertIn("Bash(rm x)", entries[3]["text"])
        self.assertTrue(all(set(e) == {"time", "from", "kind", "text"} for e in entries))
        self.assertEqual(os.stat(os.path.join(self.tmp, "audit.log")).st_mode & 0o777, 0o600)

    def test_commands_and_director_wizard_without_the_token(self):
        root = tempfile.mkdtemp()
        with mock.patch.object(director, "CONF_ROOT", root), \
                mock.patch.object(director, "start_unit", return_value=(True, "")):
            self.msg("/director")
            self.msg("shop")
            self.msg("the shop")
            self.msg(self.tmp)
            self.msg("123456:good-token_x")
        text = json.dumps(self.entries())
        self.assertNotIn("good-token", text)
        self.assertEqual([e["kind"] for e in self.entries()], ["command", "director"])
        self.assertIn("shop", self.entries()[-1]["text"])

    def test_audit_command_shows_the_last_entries(self):
        for i in range(20):
            self.d.audit.write(OWNER, "task", "task number %d" % i)
        self.msg("/audit 3")
        out = self.last()
        self.assertEqual(out.count("\n"), 2)
        self.assertIn("task number 19", out)
        self.assertNotIn("task number 16", out)
        self.msg("/audit")
        self.assertEqual(self.last().count("\n"), 14)  # default 15
        self.msg("/audit x")
        self.assertEqual(self.last().count("\n"), 14)

    def test_tail_skips_garbage_and_reads_from_the_end(self):
        path = os.path.join(self.tmp, "big.log")
        log = audit.AuditLog(path, threading.RLock(), director.mask_secrets)
        with open(path, "w") as fh:
            fh.write("not json\n" + ("x" * 100 + "\n") * 20000)
        for i in range(5):
            log.write("1", "task", "t%d" % i)
        with open(path, "a") as fh:
            fh.write("{broken\n")
        self.assertEqual([e["text"] for e in log.tail(3)], ["t2", "t3", "t4"])
        self.assertEqual(audit.AuditLog(os.path.join(self.tmp, "none"), threading.RLock(), str).tail(5), [])


class AllowedRootsTest(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.outside = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(self.root, "ok.txt"), "w") as fh:
            fh.write("ok")
        with open(os.path.join(self.outside, "secret.txt"), "w") as fh:
            fh.write("no")
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.root, "link.txt"))
        self.d.cfg = make_cfg(self.tmp, ALLOWED_ROOTS=self.root + ":/nonexistent/x", WORKDIR=self.root)
        self.files = []
        self.d.tg.send_file = lambda chat, path, caption="": self.files.append(path)

    def test_config_parses_roots_and_checks_workdir(self):
        self.assertEqual(self.d.cfg.allowed_roots, [self.root, "/nonexistent/x"])
        self.d.cfg.validate()
        with self.assertRaises(SystemExit) as ctx:
            make_cfg(tempfile.mkdtemp(), ALLOWED_ROOTS=self.root, WORKDIR=self.outside).validate()
        self.assertIn("ALLOWED_ROOTS", str(ctx.exception))
        with self.assertRaises(SystemExit):
            make_cfg(tempfile.mkdtemp(), ALLOWED_ROOTS="relative/dir", WORKDIR=self.root).validate()
        make_cfg(tempfile.mkdtemp(), WORKDIR=self.outside).validate()  # not set: no limit

    def test_get_serves_only_files_inside(self):
        self.msg("/get ok.txt")
        self.assertEqual(self.files, [os.path.join(self.root, "ok.txt")])
        for bad in (os.path.join(self.outside, "secret.txt"), "link.txt", "../" + os.path.basename(self.outside) + "/secret.txt"):
            self.msg("/get " + bad)
            self.assertIn("ALLOWED_ROOTS", self.last(), bad)
        self.msg("/get " + os.path.join(self.outside, "no-such-file"))  # outside: no hint whether it exists
        self.assertIn("ALLOWED_ROOTS", self.last())
        self.assertEqual(len(self.files), 1)

    def test_get_without_roots_is_unchanged(self):
        self.d.cfg = make_cfg(self.tmp, WORKDIR=self.root)
        self.msg("/get " + os.path.join(self.outside, "secret.txt"))
        self.assertEqual(self.files, [os.path.join(self.outside, "secret.txt")])

    def test_director_wizard_folder_must_be_inside(self):
        conf_root = tempfile.mkdtemp()
        with mock.patch.object(director, "CONF_ROOT", conf_root), \
                mock.patch.object(director, "start_unit", return_value=(True, "")):
            self.msg("/director")
            self.msg("shop")
            self.msg("the shop")
            self.msg(self.outside)
            self.assertIn("ALLOWED_ROOTS", self.last())
            self.assertEqual(self.d.state.get()["wizard"]["step"], "dir")
            self.msg(self.root)
            self.assertEqual(self.d.state.get()["wizard"]["step"], "token")
            self.msg("123456:good-token_x")
        env = director.read_env(os.path.join(conf_root, "shop", ".env"))
        self.assertEqual(env["ALLOWED_ROOTS"], self.root + ":/nonexistent/x")  # the new director inherits the fence


if __name__ == "__main__":
    unittest.main()


class EngineEnvTest(unittest.TestCase):
    """engine.env: a long-lived engine login (claude setup-token) that only claude/codex get."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, director.ENGINE_ENV)

    def write(self, text, mode=0o600):
        with open(self.path, "w") as fh:
            fh.write(text)
        os.chmod(self.path, mode)

    def test_only_login_keys_and_only_private_file(self):
        self.write("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\nPATH=/evil\nLD_PRELOAD=/x.so\n# c\n")
        self.assertEqual(director.engine_env(self.tmp), {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
        logged = []
        os.chmod(self.path, 0o640)
        self.assertEqual(director.engine_env(self.tmp, logged.append), {})
        self.assertIn("chmod 600", logged[0])
        os.remove(self.path)
        self.assertEqual(director.engine_env(self.tmp), {})

    def test_engine_gets_it_but_not_the_service_environment(self):
        self.write("CLAUDE_CODE_OAUTH_TOKEN=tok123\n")
        d = director.Director.__new__(director.Director)
        seen = {}

        class P:
            stdin = stdout = stderr = None
        real = director.subprocess.Popen

        def fake(args, **kw):
            seen.update(kw.get("env") or {})
            raise OSError("stop here")
        director.subprocess.Popen = fake
        try:
            cfg = type("C", (), {"dir": self.tmp, "workdir": self.tmp, "timeout": 5})()
            d.cfg, d.log, d.cv, d.proc = cfg, lambda m: None, __import__("threading").Condition(), None
            with self.assertRaises(OSError):
                d.stream("1", ["true"], None)
        finally:
            director.subprocess.Popen = real
        self.assertEqual(seen.get("CLAUDE_CODE_OAUTH_TOKEN"), "tok123")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)


class OutboxTest(unittest.TestCase):
    """The agent sends files by dropping them into <instance>/outbox; links are never followed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, ".env"), "w") as fh:
            fh.write("BOT_TOKEN=t\nOWNER_IDS=1\nLANG=en\n")
        self.d = director.Director(director.Config(self.tmp))
        self.sent, self.msgs = [], []
        self.d.tg.send_file = lambda chat, path, caption="": self.sent.append((os.path.basename(path), read_text(path))) or True
        self.d.tg.send = lambda chat, text, buttons=None, raw=False: self.msgs.append(text)

    def test_files_are_sent_then_deleted_links_never(self):
        box = self.d.cfg.outbox
        self.assertEqual(oct(os.stat(box).st_mode & 0o777), "0o700")
        with open(os.path.join(box, "report.txt"), "w") as fh:
            fh.write("hello")
        secret = os.path.join(self.tmp, "id_ed25519")
        with open(secret, "w") as fh:
            fh.write("PRIVATE KEY")
        os.symlink(secret, os.path.join(box, "innocent.txt"))
        os.mkdir(os.path.join(box, "dir"))
        self.d.flush_outbox("1")
        self.assertEqual(self.sent, [("report.txt", "hello")])
        self.assertFalse(os.path.exists(os.path.join(box, "report.txt")))
        self.assertFalse(os.path.lexists(os.path.join(box, "innocent.txt")))
        self.assertTrue(os.path.exists(secret))
        self.assertEqual(len(self.msgs), 2)

    def test_big_file_is_kept_and_reported_once(self):
        box = self.d.cfg.outbox
        with open(os.path.join(box, "big.bin"), "wb") as fh:
            fh.truncate(director.OUTBOX_MAX_BYTES + 1)
        self.d.flush_outbox("1")
        self.d.flush_outbox("1")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.msgs), 1)
        self.assertTrue(os.path.exists(os.path.join(box + "-kept", "big.bin")))

    def test_prompt_tells_the_agent_where(self):
        self.assertIn(self.d.cfg.outbox, self.d.cfg.prompt())


class CodexRateTest(unittest.TestCase):
    """/limits also shows Codex usage, read from its own session logs (token_count events)."""

    def test_last_rate_limits_of_the_newest_rollout(self):
        from vps_director import terminal
        home = tempfile.mkdtemp()
        day = os.path.join(home, ".codex", "sessions", "2026", "10", "14")
        os.makedirs(day)
        ev = lambda pct: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {  # noqa: E731
            "primary": {"used_percent": pct, "window_minutes": 300, "resets_at": 1}, "plan_type": "plus",
            "secondary": {"used_percent": 57.0, "window_minutes": 10080, "resets_at": 2}}}})
        with open(os.path.join(day, "rollout-a.jsonl"), "w") as fh:
            fh.write("\n".join([ev(10.0), "not json", ev(64.0), json.dumps({"type": "x"})]) + "\n")
        got = terminal.codex_rate(home)
        self.assertEqual((got["five"], got["week"], got["plan"]), (64.0, 57.0, "plus"))
        self.assertIsNone(terminal.codex_rate(tempfile.mkdtemp()))


class SilentStartTest(Base):
    """An engine that prints nothing (e.g. not logged in) is stopped after START_WAIT with a login hint."""

    def test_silent_engine_is_stopped(self):
        with mock.patch.object(director, "START_WAIT", 0.5):
            code, _ = self.d.stream(OWNER, ["sleep", "30"], director.ClaudeStream(), "")
        self.assertEqual(code, director.NO_START)

    def test_talking_engine_is_not_cut(self):
        with mock.patch.object(director, "START_WAIT", 0.5):
            code, _ = self.d.stream(OWNER, ["sh", "-c", "echo '{}'; sleep 1"], director.ClaudeStream(), "")
        self.assertEqual(code, 0)

    def test_hint_names_the_login_command(self):
        self.assertIn("claude setup-token", self.d.cfg.t("no_start", engine="Claude", sec=90, login="claude setup-token"))
