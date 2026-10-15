"""Task 05: a colleague, not a tool. Offline like the other tests: no Telegram, no network, no CLI."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import OWNER, Base, FakeTelegram, director, make_cfg  # noqa: E402,F401
from test_features import TOKEN, FakeBotAPI, button_data, press, private  # noqa: E402

from vps_director import persona  # noqa: E402
from vps_director import setup as vsetup  # noqa: E402


def run_wizard(answers, polls=None, lang="en", first="Ann", has_module=lambda name: False):
    api = FakeBotAPI(polls=polls if polls is not None else [[private(5, 1, first=first, lang=lang)]])
    conf = os.path.join(tempfile.mkdtemp(), "main")
    answers, out = list(answers), []
    wiz = vsetup.Setup(conf, api, ask=lambda p: (out.append(p), answers.pop(0) if answers else "")[1],
                       secret=lambda p: TOKEN, out=out.append, clock=lambda: api.now, sleep=lambda s: None,
                       has_module=has_module)
    wiz.run()
    return wiz, director.read_env(os.path.join(conf, ".env")), out, conf


# ---------- 1. name and personality ----------

class PersonaTextTest(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(persona.clean_name(" «Jarvis» "), "Jarvis")
        self.assertEqual(persona.clean_name('"Max Payne".'), "Max Payne")
        for bad in ("", "   ", "a" * 41, "one two three four five", "x\ny", "<b>"):
            self.assertIsNone(persona.clean_name(bad), bad)

    def test_rename_phrases(self):
        self.assertEqual(persona.parse_rename("call yourself Jarvis"), ("self", "Jarvis"))
        self.assertEqual(persona.parse_rename("Зови себя «Пятница»"), ("self", "Пятница"))
        self.assertEqual(persona.parse_rename("зови меня Миша"), ("owner", "Миша"))
        self.assertEqual(persona.parse_rename("Call me Mike"), ("owner", "Mike"))
        for task in ("call me when the backup is done", "зови меня когда закончишь", "call me back",
                     "call yourself a taxi and then restart nginx please", "check who calls me"):
            self.assertIsNone(persona.parse_rename(task), task)

    def test_remember_phrases(self):
        self.assertEqual(persona.parse_remember("remember: backups go to /mnt/b"), "backups go to /mnt/b")
        self.assertEqual(persona.parse_remember("Запомни, что бэкапы лежат в /mnt/b"), "бэкапы лежат в /mnt/b")
        self.assertEqual(persona.parse_remember("remember that the shop runs in docker"), "the shop runs in docker")
        for other in ("remembering things is hard", "what do you remember", "remember"):
            self.assertIsNone(persona.parse_remember(other), other)

    def test_persona_block_is_replaced_not_duplicated(self):
        body = "# My server\nshop in /srv/shop\n"
        once = persona.apply_persona(body, persona.persona_block("Jarvis", "Ann", "en"))
        twice = persona.apply_persona(once, persona.persona_block("Friday", "", "en"))
        self.assertEqual(twice.count(persona.START), 1)
        self.assertIn("Friday", twice)
        self.assertNotIn("Jarvis", twice)
        self.assertTrue(twice.endswith(body))
        ru = persona.persona_block("Пятница", "Миша", "ru")
        self.assertIn("Тебя зовут Пятница", ru)
        self.assertIn("Владельца зовут Миша", ru)


class PersonaSetupTest(unittest.TestCase):
    def test_defaults_name_and_telegram_first_name(self):
        wiz, env, out, _ = run_wizard(["y"])
        self.assertEqual((env["NAME"], env["OWNER_NAME"]), ("VPS Director", "Ann"))
        self.assertNotIn("TITLE", env)  # TITLE derives from NAME
        self.assertTrue(any("Enter = Ann" in line for line in out))

    def test_custom_answers_in_russian(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "ru_RU.UTF-8"}):
            wiz, env, out, _ = run_wizard(["y", "Пятница", "Миша"], lang="ru", first="Ivan")
        self.assertEqual((env["LANG"], env["NAME"], env["OWNER_NAME"]), ("ru", "Пятница", "Миша"))
        self.assertTrue(any("Как мне себя называть" in line for line in out))
        self.assertEqual(director.Config(os.path.dirname(os.path.join(wiz.conf, ".env"))).title, "Пятница")

    def test_bad_name_is_asked_again(self):
        _, env, out, _ = run_wizard(["y", "a" * 50, "Jarvis #1", ""])
        self.assertEqual(env["NAME"], "Jarvis #1")  # quoted in .env, so " #1" is not a comment
        self.assertTrue(any("40 characters" in line for line in out))

    def test_setup_writes_prompt_with_persona_and_keeps_an_existing_one(self):
        _, env, _, conf = run_wizard(["y", "Jarvis", ""])
        with mock.patch("sys.stdout"):
            director.write_setup_prompt(conf, env)
        path = os.path.join(conf, "PROMPT.md")
        with open(path) as fh:
            text = fh.read()
        self.assertTrue(text.startswith(persona.START))
        self.assertIn("Your name is Jarvis", text)
        self.assertIn("The owner's name is Ann", text)
        self.assertIn("What is on this server", text)  # the example instruction follows
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, "w") as fh:
            fh.write("my own words\n")
        director.write_setup_prompt(conf, dict(env, NAME="Friday"))
        with open(path) as fh:
            text = fh.read()
        self.assertIn("Your name is Friday", text)
        self.assertTrue(text.endswith("my own words\n"))


class NameAndMemoryTest(Base):
    def setUp(self):
        super().setUp()
        self.d.tg.api = mock.MagicMock(return_value={"ok": True})
        with open(self.cfg.prompt_file, "w") as fh:
            fh.write(persona.apply_persona("server notes\n", persona.persona_block("VPS Director", "", "en")))

    def env(self):
        return director.read_env(os.path.join(self.tmp, ".env"))

    def prompt_file(self):
        with open(self.cfg.prompt_file) as fh:
            return fh.read()

    def test_config_name_title_and_old_title(self):
        cfg = make_cfg(tempfile.mkdtemp(), NAME="Jarvis", OWNER_NAME="Ann", TITLE="old")
        self.assertEqual((cfg.name, cfg.title, cfg.owner_name), ("Jarvis", "Jarvis", "Ann"))
        self.assertEqual(make_cfg(tempfile.mkdtemp(), TITLE="Shop bot").title, "Shop bot")
        self.assertEqual(make_cfg(tempfile.mkdtemp()).title, "VPS Director")
        bare = make_cfg(tempfile.mkdtemp(), NAME="Jarvis", OWNER_NAME="Ann")  # no PROMPT.md: default + persona
        self.assertIn("Your name is Jarvis", bare.prompt())
        self.assertIn("The owner's name is Ann", bare.prompt())

    def test_call_yourself_renames_everywhere(self):
        self.msg("call yourself Jarvis")
        self.assertEqual(self.env()["NAME"], "Jarvis")
        self.assertEqual(self.d.cfg.title, "Jarvis")
        self.assertIn("Your name is Jarvis", self.prompt_file())
        self.assertTrue(self.prompt_file().endswith("server notes\n"))
        self.assertEqual(os.stat(os.path.join(self.tmp, ".env")).st_mode & 0o777, 0o600)
        self.d.tg.api.assert_any_call("setMyName", {"name": "Jarvis"}, timeout=15)
        self.assertIn("Jarvis", self.last())
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.env()["BOT_TOKEN"], "t")  # the rest of .env is kept

    def test_slash_name_and_call_me(self):
        self.msg("/name")
        self.assertIn("I'm VPS Director", self.last())
        self.msg("/name Friday")
        self.assertEqual(self.env()["NAME"], "Friday")
        self.msg("зови меня Миша")
        self.assertEqual(self.env()["OWNER_NAME"], "Миша")
        self.assertIn("Миша", self.prompt_file())
        self.msg("/name")
        self.assertIn("I call you Миша", self.last())
        self.msg("/name " + "x" * 60)
        self.assertIn("40 characters", self.last())
        self.msg("call me when the backup is done")
        self.assertEqual(self.submitted[-1][1], "call me when the backup is done")

    def test_start_greets_by_name(self):
        self.d.message({"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER), "first_name": "Ann"},
                        "message_id": 1, "text": "/start"})
        self.assertTrue(self.last().startswith("Hi, Ann! VPS Director here"))
        self.d.cfg.owner_name = "Boss"
        self.msg("/start")
        self.assertTrue(self.last().startswith("Hi, Boss!"))
        self.msg("/help")
        self.assertFalse(self.last().startswith("Hi"))

    def test_remember_show_and_delete(self):
        self.msg("remember: backups go to /mnt/backup")
        self.assertIn("Noted: backups go to /mnt/backup", self.last())
        self.msg("Запомни, что сайт магазина в /srv/shop")
        self.assertEqual(self.submitted, [])
        path = os.path.join(self.tmp, "memory.md")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as fh:
            self.assertIn("- %s: backups go to /mnt/backup" % time.strftime("%Y-%m-%d"), fh.read())
        prompt = self.d.cfg.prompt()
        self.assertIn("## Memory", prompt)
        self.assertIn("сайт магазина в /srv/shop", prompt)
        self.msg("/memory")
        text, buttons = self.d.tg.sent[-1][1], self.d.tg.sent[-1][2]
        self.assertIn("1. ", text)
        self.assertIn("2. ", text)
        datas = button_data(buttons)
        self.assertEqual(len(datas), 2)
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas))
        self.d.callback({"id": "1", "from": {"id": 999}, "data": datas[0]})  # a stranger: ignored
        self.assertEqual(len(self.d.memory.entries()), 2)
        press(self.d, datas[0])
        self.assertEqual([t for _, _, t in self.d.memory.entries()], ["сайт магазина в /srv/shop"])
        press(self.d, datas[0])
        self.assertIn("already gone", self.last())
        kinds = [r["kind"] for r in self.d.audit.tail(20)]
        self.assertIn("memory", kinds)

    def test_secrets_are_masked_before_they_reach_memory(self):
        self.msg("remember: db password=hunter2 for the shop")
        self.assertIn("hid what looked like a secret", self.last())
        self.assertNotIn("hunter2", self.d.cfg.prompt())
        with open(os.path.join(self.tmp, "memory.md")) as fh:
            self.assertNotIn("hunter2", fh.read())

    def test_memory_is_capped(self):
        for i in range(persona.MAX_FACTS):
            self.d.memory.add("fact %d" % i, "2026-01-01")
        self.msg("remember: one more")
        self.assertIn("full", self.last())
        self.assertEqual(len(self.d.memory.entries()), persona.MAX_FACTS)

    def test_hand_edited_memory_lines_survive(self):
        path = os.path.join(self.tmp, "memory.md")
        with open(path, "w") as fh:
            fh.write("# my notes\n- 2026-01-01: keep me\n- 2026-01-02: drop me\n")
        fid = [f for f, _, t in self.d.memory.entries() if t == "drop me"][0]
        self.assertTrue(self.d.memory.delete(fid))
        with open(path) as fh:
            self.assertEqual(fh.read(), "# my notes\n- 2026-01-01: keep me\n")


class AwayDigestTest(Base):
    def test_digest_after_three_days_of_silence(self):
        self.msg("first contact")  # first message ever: no digest
        self.assertEqual(self.d.tg.sent, [])
        self.d.state.put(seen={OWNER: time.time() - 5 * 86400})
        self.d.audit.write("watchdog", "alert", "disk / is 95% full")
        self.d.audit.write("scheduler", "job", "run #1 command: /disk")
        self.d.audit.write("scheduler", "job", "run #1 command: /disk")
        self.d.state.put(watch=["disk / is 95% full"], rate={"five_hour": 12, "seven_day": 40})
        self.msg("how is the server?")
        digest = self.d.tg.sent[0][1]
        self.assertIn("While you were away (5 d)", digest)
        self.assertIn("server alerts (1): disk / is 95% full", digest)
        self.assertIn("still not fixed", digest)
        self.assertIn("scheduled runs: 2", digest)
        self.assertIn("12% of the 5-hour window", digest)
        self.assertEqual(self.submitted[-1][1], "how is the server?")  # the message itself still goes on
        sent = len(self.d.tg.sent)
        self.msg("and now?")
        self.assertEqual(len(self.d.tg.sent), sent)  # once per absence

    def test_quiet_digest_in_russian(self):
        self.d.cfg.lang = "ru"
        self.d.state.put(seen={OWNER: time.time() - 4 * 86400})
        self.msg("привет")
        self.assertIn("Пока тебя не было (4 дн.)", self.d.tg.sent[0][1])
        self.assertIn("всё спокойно", self.d.tg.sent[0][1])

    def test_watchdog_alerts_go_to_the_audit_log(self):
        self.d.cfg.watch_interval = 1
        rounds = [["disk / is 95% full"], []]

        class Done(BaseException):
            pass

        def problems(pct):
            if not rounds:
                raise Done()
            return rounds.pop(0)
        self.d.broadcast = lambda text: None
        with mock.patch.object(director, "server_problems", problems), \
                mock.patch.object(director.time, "sleep", lambda s: None):
            with self.assertRaises(Done):
                self.d.watchdog()
        kinds = [(r["kind"], r["text"]) for r in self.d.audit.tail(5)]
        self.assertEqual(kinds, [("alert", "disk / is 95% full"), ("resolved", "disk / is 95% full")])


if __name__ == "__main__":
    unittest.main()


# ---------- 2. reminders from phrases, TZ, morning brief ----------

import datetime  # noqa: E402

from vps_director import natural, schedule  # noqa: E402

MSK = schedule.zone("Europe/Moscow")
NOW = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=MSK).timestamp()  # Saturday noon in Moscow


def msk(day, hour, minute=0):
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=MSK).timestamp()


class NaturalParseTest(unittest.TestCase):
    def parse(self, text):
        return natural.parse(text, NOW, MSK)

    def test_one_shot_reminders(self):
        cases = {
            "напомни через 2 часа позвонить в банк": (NOW + 7200, "позвонить в банк", "reminder"),
            "Напомни мне через 15 мин проверить сайт": (NOW + 900, "проверить сайт", "reminder"),
            "напомни через полчаса выключить чайник": (NOW + 1800, "выключить чайник", "reminder"),
            "напомни завтра в 10:00 продлить домен": (msk(11, 10), "продлить домен", "reminder"),
            "напомни позвонить маме в 19": (msk(10, 19), "позвонить маме", "reminder"),
            "напомни в 7 вечера забрать посылку": (msk(10, 19), "забрать посылку", "reminder"),
            "напомни в 9:30 о том, что надо оплатить хостинг": (msk(11, 9, 30), "надо оплатить хостинг", "reminder"),
            "напомни завтра продлить домен": (msk(11, 9), "продлить домен", "reminder"),
            "remind me at 18:00 call the bank": (msk(10, 18), "call the bank", "reminder"),
            "remind me in an hour to stretch": (NOW + 3600, "stretch", "reminder"),
            "remind me tomorrow at 9am check the invoices": (msk(11, 9), "check the invoices", "task"),
            "remind me at 6pm to /disk": (msk(10, 18), "/disk", "command"),
            "напомни через 3 дня проверь сертификаты": (NOW + 3 * 86400, "проверь сертификаты", "task"),
            "remind me on friday at 18:00 to send the invoice": (msk(16, 18), "send the invoice", "reminder"),
            "напомни в субботу в 10 сменить пароли": (msk(17, 10), "сменить пароли", "reminder"),
            "напомни в среду оплатить сервер": (msk(14, 9), "оплатить сервер", "reminder"),
        }
        for text, (when, body, kind) in cases.items():
            plan = self.parse(text)
            self.assertEqual((plan.get("next"), plan.get("text"), plan.get("kind")), (when, body, kind), text)
            self.assertNotIn("every", plan)
            self.assertNotIn("daily", plan)

    def test_repeating(self):
        plan = self.parse("каждый день в 9:00 проверь бэкапы")
        self.assertEqual((plan["daily"], plan["next"], plan["text"], plan["kind"]), ("09:00", msk(11, 9), "проверь бэкапы", "task"))
        plan = self.parse("every day at 21:30 drink water")
        self.assertEqual((plan["daily"], plan["kind"]), ("21:30", "reminder"))
        plan = self.parse("каждые 2 часа /disk")
        self.assertEqual((plan["every"], plan["kind"], plan["text"]), (7200, "command", "/disk"))
        self.assertEqual(self.parse("every hour /status")["every"], 3600)
        self.assertEqual(self.parse("напомни каждый день в 8 утра выпить таблетку")["daily"], "08:00")

    def test_not_a_schedule_or_incomplete(self):
        for text in ("every time I deploy, check the logs", "check nginx every day at 9",
                     "what did you do in 2 hours", "напоминание: бэкап упал"):
            self.assertIsNone(self.parse(text), text)
        self.assertEqual(self.parse("напомни купить молоко"), {"error": "when"})
        for question in ("remind me how the backup script works", "напомни, как настроить nginx",
                         "напомни, что мы делали с сертификатами", "remind me what port the shop uses"):
            self.assertIsNone(self.parse(question), question)
        self.assertEqual(self.parse("напомни, что завтра в 10 созвон")["text"], "созвон")
        self.assertEqual(self.parse("remind me in 2h"), {"error": "text"})
        self.assertEqual(self.parse("каждую минуту /disk"), {"error": "every"})
        self.assertEqual(self.parse("remind me at 25:00 x"), {"error": "when"})
        self.assertEqual(self.parse("напомни сегодня в 9:00 полить цветы"), {"error": "past"})

    def test_earliest_time_wins(self):
        plan = self.parse("напомни через час, что каждый день надо пить воду")
        self.assertEqual((plan["next"], plan["text"]), (NOW + 3600, "каждый день надо пить воду"))
        self.assertNotIn("daily", plan)

    def test_edit_text_without_trigger(self):
        plan = natural.parse("tomorrow at 10:00 call the bank", NOW, MSK, implicit=True)
        self.assertEqual((plan["next"], plan["text"]), (msk(11, 10), "call the bank"))


class TimeZoneTest(unittest.TestCase):
    def test_next_clock_in_a_zone_and_over_dst(self):
        berlin = schedule.zone("Europe/Berlin")
        before = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=berlin).timestamp()
        nxt = schedule.next_clock("09:00", before, berlin)
        self.assertEqual(datetime.datetime.fromtimestamp(nxt, berlin).strftime("%d %H:%M %Z"), "25 09:00 CET")
        self.assertEqual(schedule.next_clock("09:00", NOW, MSK), msk(11, 9))
        self.assertEqual(schedule.next_run({"daily": "13:00"}, NOW, MSK), msk(10, 13))

    def test_zone_names(self):
        self.assertIsNotNone(schedule.zone("Asia/Yekaterinburg"))
        for bad in ("", "Mars/Olympus", "../../etc/passwd", "/etc/localtime"):
            self.assertIsNone(schedule.zone(bad), bad)
        cfg = make_cfg(tempfile.mkdtemp(), TZ="Mars/Olympus", BRIEF="25:00")
        self.assertIsNone(cfg.tz)
        self.assertEqual(cfg.brief, "")
        self.assertEqual(len(cfg.warnings), 2)
        cfg = make_cfg(tempfile.mkdtemp(), TZ="Europe/Moscow", BRIEF="8:30")
        self.assertEqual((cfg.tz_name, cfg.brief), ("Europe/Moscow", "08:30"))

    def test_server_tz(self):
        tmp = tempfile.mkdtemp()
        tzfile = os.path.join(tmp, "timezone")
        with open(tzfile, "w") as fh:
            fh.write("Europe/Berlin\n")
        self.assertEqual(vsetup.server_tz({"TZ": ":Asia/Tokyo"}, os.path.join(tmp, "none"), tzfile), "Asia/Tokyo")
        self.assertEqual(vsetup.server_tz({}, os.path.join(tmp, "none"), tzfile), "Europe/Berlin")
        self.assertEqual(vsetup.server_tz({}, os.path.join(tmp, "none"), os.path.join(tmp, "none")), "")

    def test_setup_asks_tz_and_brief(self):
        with mock.patch.object(vsetup, "server_tz", return_value="Europe/Berlin"):
            _, env, out, _ = run_wizard(["y", "", "", ""])
        self.assertEqual(env["TZ"], "Europe/Berlin")
        self.assertNotIn("BRIEF", env)  # opt-in
        with mock.patch.object(vsetup, "server_tz", return_value="Europe/Berlin"):
            _, env, out, _ = run_wizard(["y", "", "", "Mars/Base", "Europe/Moscow", "y", "7:5", "7:45"])
        self.assertEqual((env["TZ"], env["BRIEF"]), ("Europe/Moscow", "07:45"))
        self.assertTrue(any("Europe/Berlin or" in line for line in out))
        with mock.patch.object(vsetup, "server_tz", return_value=""):
            _, env, _, _ = run_wizard(["y", "", "", "", "y", ""])
        self.assertEqual((env["TZ"], env["BRIEF"]), ("UTC", "09:00"))


class NaturalJobsTest(Base):
    def setUp(self):
        super().setUp()
        cdir = os.path.join(self.tmp, "commands")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "disk.sh"), "w") as fh:
            fh.write("#!/bin/sh\necho disk ok\n")
        os.chmod(os.path.join(cdir, "disk.sh"), 0o755)
        self.d.cfg.tz, self.d.cfg.tz_name = MSK, "Europe/Moscow"

    def jobs(self):
        return self.d.state.get().get("jobs") or {}

    def test_phrase_becomes_a_reminder_with_delete_and_change(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("напомни через 2 часа позвонить в банк")
        job = self.jobs()["1"]
        self.assertEqual((job["kind"], job["text"], job["next"]), ("reminder", "позвонить в банк", NOW + 7200))
        _, text, buttons = self.d.tg.sent[-1]
        self.assertIn("Reminder #1: today at 14:00 — позвонить в банк", text)
        self.assertEqual(button_data(buttons), ["jobdel:1", "jobedit:1"])
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.d.audit.tail(1)[0]["kind"], "job")
        press(self.d, "jobdel:1")
        self.assertEqual(self.jobs(), {})

    def test_daily_task_and_command_and_rejections(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("каждый день в 9:00 проверь бэкапы")
            self.msg("every 2 hours /disk")
            self.msg("every hour /reboot")
            self.msg("напомни купить молоко")
        jobs = self.jobs()
        self.assertEqual((jobs["1"]["kind"], jobs["1"]["daily"], jobs["1"]["next"]), ("task", "09:00", msk(11, 9)))
        self.assertIn("every day at 09:00", self.d.tg.sent[0][1])
        self.assertIn("safe mode", self.d.tg.sent[0][1])
        self.assertEqual((jobs["2"]["kind"], jobs["2"]["every"], jobs["2"]["changes"]), ("command", 7200, True))
        self.assertIn("every 2h, first today at 14:00", self.d.tg.sent[1][1])
        self.assertIn("/reboot is not one", self.d.tg.sent[2][1])
        self.assertIn("When?", self.d.tg.sent[3][1])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.submitted, [])
        self.msg("every time I deploy, check the logs")  # not a schedule: goes to the agent
        self.assertEqual(self.submitted[-1][1], "every time I deploy, check the logs")

    def test_russian_confirmation(self):
        self.d.cfg.lang = "ru"
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("напомни завтра в 10:00 продлить домен")
            self.msg("каждые 30 минут /disk")
            self.msg("напомни в понедельник в 10 позвонить")
        self.assertIn("Напоминание #1: завтра в 10:00 — продлить домен", self.d.tg.sent[0][1])
        self.assertIn("раз в 30 мин, первый раз сегодня в 12:30", self.d.tg.sent[1][1])
        self.assertIn("пн 12.10 в 10:00", self.d.tg.sent[2][1])

    def test_change_button_takes_the_next_message(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("remind me in 2 hours to call the bank")
            press(self.d, "jobedit:1")
            self.assertIn("Send the new time and text for #1", self.last())
            self.msg("tomorrow at 10:00 call the bank about the card")
        self.assertNotIn("1", self.jobs())
        job = self.jobs()["2"]
        self.assertEqual((job["text"], job["next"]), ("call the bank about the card", msk(11, 10)))
        self.assertTrue(self.last().startswith("Changed. ⏰ Reminder #2: tomorrow at 10:00"))
        self.assertIsNone(self.d.state.get().get("editing"))
        with mock.patch.object(director.time, "time", return_value=NOW):
            press(self.d, "jobedit:2")
            self.msg("cancel")
        self.assertIn("#2 stays as it was", self.last())
        self.assertIn("2", self.jobs())
        self.msg("how is the disk?")  # editing is over: a normal task again
        self.assertEqual(self.submitted[-1][1], "how is the disk?")

    def test_change_expires_and_ignores_strangers(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("remind me in 2 hours to call the bank")
        self.d.callback({"id": "1", "from": {"id": 999}, "data": "jobedit:1"})
        self.assertIsNone(self.d.state.get().get("editing"))
        with mock.patch.object(director.time, "time", return_value=NOW):
            press(self.d, "jobedit:1")
        with mock.patch.object(director.time, "time", return_value=NOW + director.EDIT_TTL + 1):
            self.msg("tomorrow at 10:00 something else")
        self.assertEqual(self.jobs()["1"]["text"], "call the bank")
        self.assertEqual(self.submitted[-1][1], "tomorrow at 10:00 something else")

    def test_at_clock_and_jobs_use_tz(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/at 13:00 check the queue")
            self.msg("/jobs")
        self.assertEqual(self.jobs()["1"]["next"], msk(10, 13))
        self.assertIn("Scheduled jobs (Europe/Moscow)", self.last())
        self.assertIn("10.10 13:00", self.last())

    def test_daily_jobs_are_retimed_when_tz_changes(self):
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("every day at 9:00 check the backups")
            self.d.sync_jobs()
            self.d.cfg.tz, self.d.cfg.tz_name = schedule.zone("Asia/Tokyo"), "Asia/Tokyo"
            self.d.sync_jobs()
        tokyo = schedule.zone("Asia/Tokyo")
        self.assertEqual(self.jobs()["1"]["next"], datetime.datetime(2026, 10, 11, 9, 0, tzinfo=tokyo).timestamp())


class BriefTest(Base):
    def setUp(self):
        super().setUp()
        self.d.cfg.tz, self.d.cfg.tz_name = MSK, "Europe/Moscow"
        self.d.cfg.owner_name = "Ann"
        self.patches = [mock.patch.object(director, "server_problems", return_value=[]),
                        mock.patch.object(director, "disk_usage", return_value=[("41%", "/"), ("90%", "/data")])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def briefs(self):
        return {jid: j for jid, j in (self.d.state.get().get("jobs") or {}).items() if j["kind"] == "brief"}

    def test_brief_jobs_follow_the_setting(self):
        self.d.cfg.brief = "09:00"
        self.d.cfg.owners = {OWNER, "222"}
        self.d.sync_jobs()
        self.d.sync_jobs()  # idempotent
        self.assertEqual(sorted(j["chat"] for j in self.briefs().values()), ["111", "222"])
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("/brief 8:30")
        self.assertEqual(director.read_env(os.path.join(self.tmp, ".env"))["BRIEF"], "08:30")
        self.assertEqual({j["daily"] for j in self.briefs().values()}, {"08:30"})
        self.assertEqual({j["next"] for j in self.briefs().values()}, {msk(11, 8, 30)})
        self.assertIn("every day at 08:30 (Europe/Moscow)", self.last())
        self.msg("/brief off")
        self.assertEqual(self.briefs(), {})
        self.assertNotIn("BRIEF", director.read_env(os.path.join(self.tmp, ".env")))
        self.msg("/brief 9")
        self.assertIn("/brief HH:MM", self.last())

    def test_brief_content(self):
        self.d.audit.write("watchdog", "alert", "failed service nginx.service")
        self.d.audit.write("notify", "notify", "backup ok: 2.1 GB to /mnt/backup")
        self.d.state.put(rate={"five_hour": 7, "seven_day": 31})
        with mock.patch.object(director.time, "time", return_value=NOW):
            self.msg("remind me at 18:00 call the bank")
            self.msg("remind me tomorrow at 10:00 renew the domain")
        text = self.d.brief_text(OWNER, NOW)
        self.assertTrue(text.startswith("☀️ Good afternoon, Ann! Here's Sat 10.10:"), text)
        self.assertIn("🖥 Server: all fine — load ", text)
        self.assertIn("disk / 41%", text)
        self.assertIn("Alerts in the last 24 h: 1 — failed service nginx.service", text)
        self.assertNotIn("backup ok", text)  # task 06: backups come from backups.json, not from notify texts
        self.assertIn("💾 Backups: not watched", text)
        self.assertIn("7% of the 5-hour window, 31% of the week", text)
        self.assertIn("18:00 call the bank", text)
        self.assertNotIn("renew the domain", text)  # tomorrow is not today

    def test_brief_with_problems_in_russian_and_brief_cmd(self):
        self.d.cfg.lang = "ru"
        self.d.cfg.brief_cmd = "echo 'certs: 3 ok'"
        with mock.patch.object(director, "server_problems", return_value=["disk /data is 95% full"]):
            text = self.d.brief_text(OWNER, msk(10, 8))
        self.assertIn("☀️ Доброе утро, Ann! Сводка на сб 10.10:", text)
        self.assertIn("проблем: 1", text)
        self.assertIn("disk /data is 95% full", text)
        self.assertIn("За сутки тревог не было", text)
        self.assertIn("На сегодня больше ничего не запланировано", text)
        self.assertTrue(text.endswith("certs: 3 ok"))

    def test_brief_job_fires_and_slash_brief_sends_now(self):
        self.d.cfg.brief = "09:00"
        self.d.sync_jobs()
        nxt = next(iter(self.briefs().values()))["next"]
        self.d.run_due_jobs(nxt + 1)
        self.assertIn("Server: all fine", self.last())
        self.assertEqual(len(self.briefs()), 1)  # daily: still there
        self.msg("/brief")
        self.assertIn("Server: all fine", self.last())
        self.assertEqual(self.submitted, [])  # no AI involved

    def test_notify_goes_to_the_audit_log(self):
        with mock.patch.object(director.Telegram, "send", return_value=1):
            self.assertTrue(director.notify(self.cfg, "backup failed: disk full"))
        self.assertEqual(self.d.audit.tail(1)[0]["kind"], "notify")


# ---------- 3. inbound webhooks ----------

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from vps_director import webhook  # noqa: E402

SECRET = "s3cret-0123456789abcdef"


class WebhookHandleTest(unittest.TestCase):
    def setUp(self):
        self.notes, self.tasks = [], []
        self.now = 0.0
        self.hook = webhook.Webhook(SECRET, self.notes.append, lambda t: self.tasks.append(t) or True,
                                    tasks_enabled=True, clock=lambda: self.now)

    def call(self, path, body=b'{"text": "hi"}', secret=SECRET, method="POST", **headers):
        if secret is not None:
            headers.setdefault("X-Director-Secret", secret)
        return self.hook.handle(method, path, headers, body)

    def test_secret_is_required_and_compared_exactly(self):
        for secret in (None, "", "wrong", SECRET + "x", SECRET[:-1]):
            self.assertEqual(self.call("/notify", secret=secret)[0], 401, secret)
        self.assertEqual(self.call("/notify", secret=None, Authorization="Bearer " + SECRET)[0], 200)
        self.assertEqual(self.notes, ["hi"])

    def test_paths_methods_bodies(self):
        self.assertEqual(self.call("/notify", method="GET")[0], 405)
        self.assertEqual(self.call("/other")[0], 404)
        self.assertEqual(self.call("/notify", body=b"")[0], 400)
        self.assertEqual(self.call("/notify", body=b'{"heartbeat": {}}')[0], 400)
        self.assertEqual(self.call("/notify/", body=b"plain text alert")[0], 200)
        self.assertEqual(self.call("/notify", body=json.dumps({"msg": "[Up] shop"}).encode(),
                                   **{"Content-Type": "application/json"})[0], 200)  # Uptime Kuma
        self.assertEqual(self.call("/task", body=b'{"text": "check the deploy"}')[0], 200)
        self.assertEqual(self.notes, ["plain text alert", "[Up] shop"])
        self.assertEqual(self.tasks, ["check the deploy"])

    def test_tasks_are_off_unless_enabled_and_busy_is_429(self):
        hook = webhook.Webhook(SECRET, self.notes.append, lambda t: False)
        self.assertEqual(hook.handle("POST", "/task", {"X-Director-Secret": SECRET}, b"x")[0], 403)
        hook.tasks_enabled = True
        self.assertEqual(hook.handle("POST", "/task", {"X-Director-Secret": SECRET}, b"x")[0], 429)

    def test_rate_limit(self):
        codes = [self.call("/notify")[0] for _ in range(webhook.RATE + 1)]
        self.assertEqual(codes[-2:], [200, 429])
        self.now += 61
        self.assertEqual(self.call("/notify")[0], 200)


class WebhookDirectorTest(Base):
    def test_config_needs_a_secret(self):
        cfg = make_cfg(tempfile.mkdtemp(), WEBHOOK_PORT="8787")
        with self.assertRaises(SystemExit) as ctx:
            cfg.validate()
        self.assertIn("WEBHOOK_SECRET", str(ctx.exception))
        cfg = make_cfg(tempfile.mkdtemp(), WEBHOOK_PORT="70000", WEBHOOK_SECRET=SECRET)
        with self.assertRaises(SystemExit):
            cfg.validate()
        cfg = make_cfg(tempfile.mkdtemp(), WEBHOOK_PORT="8787", WEBHOOK_SECRET=SECRET, WEBHOOK_TASKS="1")
        cfg.validate()
        self.assertEqual((cfg.webhook_port, cfg.webhook_tasks), (8787, True))
        self.assertEqual(make_cfg(tempfile.mkdtemp()).webhook_port, 0)  # off by default

    def test_notify_and_task_follow_the_unattended_rules(self):
        active, _ = self.d.state.new_session("claude")
        self.assertTrue(self.d.webhook_notify("deploy finished"))
        self.assertEqual(self.last(), "🔔 deploy finished")
        jobs = []
        self.d.submit = lambda chat, key, prompt, extra_tools=(), job=None: jobs.append((chat, key, prompt, job))
        self.assertTrue(self.d.webhook_task("CI failed on main: look at the last run"))
        self.assertTrue(self.d.webhook_task("again"))
        chat, key, prompt, job = jobs[0]
        self.assertEqual((chat, prompt, job["icon"]), (OWNER, "CI failed on main: look at the last run", "🔔"))
        self.assertNotEqual(key, active)  # never the owner's active session
        self.assertEqual(jobs[1][1], key)  # one webhook session, reused
        self.assertEqual(self.d.state.get()["active"], active)
        self.assertEqual(self.d.state.sessions()[key]["mode"], "safe")
        self.d.queue.extend([None] * director.WEBHOOK_QUEUE)
        self.assertFalse(self.d.webhook_task("one too many"))
        froms = [(r["from"], r["kind"]) for r in self.d.audit.tail(5)]
        self.assertIn(("webhook", "notify"), froms)
        self.assertIn(("webhook", "task"), froms)

    def test_webhook_task_result_has_its_label_and_no_buttons(self):
        key, _ = self.d.state.new_session("claude", activate=False)

        def fake_stream(chat, args, col, stdin_text=""):
            col.result = "the build broke on a missing env var"
            col.denials = [{"tool_name": "Bash", "tool_input": {"command": "gh run rerun 42"}}]
            return 0, ""
        self.d.stream = fake_stream
        self.d.run_task(OWNER, key, "CI failed", (), job={"id": "webhook", "label": "webhook: CI failed", "icon": "🔔"})
        self.assertEqual(self.d.tg.sent[0][1], "🔔 webhook: CI failed\nthe build broke on a missing env var")
        self.assertIn("gh run rerun 42", self.d.tg.sent[1][1])
        self.assertTrue(all(b is None for _, _, b in self.d.tg.sent))
        self.assertEqual(self.d.state.get().get("pending") or {}, {})

    def test_real_listener_on_loopback(self):
        self.d.cfg.webhook_port, self.d.cfg.webhook_secret, self.d.cfg.webhook_tasks = 0, SECRET, False
        server = self.d.start_webhook()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        self.assertEqual(host, "127.0.0.1")

        def post(path, body, secret=SECRET):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                         headers={"X-Director-Secret": secret, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        self.assertEqual(post("/notify", b'{"text": "backup ok"}'), (200, {"ok": True}))
        self.assertEqual(post("/notify", b'{"text": "x"}', secret="nope")[0], 401)
        self.assertEqual(post("/task", b'{"text": "x"}')[0], 403)
        self.assertEqual(post("/notify", b"x" * (webhook.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.last(), "🔔 backup ok")


# ---------- 4. GitHub ----------

from vps_director import github  # noqa: E402

README = """# Tool

Some intro with `pip install nothing` inline.

## Installation

```bash
# with pip
$ pip install tool-cli
Collecting tool-cli
Successfully installed tool-cli-1.0
```

```python
import tool  # not a shell block
```

### From source

```sh
git clone https://github.com/acme/tool.git
cd tool
make build \\
  PREFIX=$HOME/.local
make install
```

## Quick start

```console
$ docker compose up -d
$ pip install tool-cli
```

## Other ways

```bash
curl -fsSL https://get.example.com/install.sh | sudo bash
npm i -g tool-cli
```

## Usage

```bash
tool --help
rm -rf ~/.cache/tool/*
```
"""


class GithubParseTest(unittest.TestCase):
    def test_repo_links(self):
        for link in ("https://github.com/acme/tool", "github.com/acme/tool.git", "https://github.com/acme/tool/",
                     "<https://github.com/acme/tool>", "https://www.github.com/acme/tool/tree/main/docs",
                     "  https://github.com/acme/tool#readme "):
            self.assertEqual(github.parse_repo_link(link), ("acme", "tool"), link)
        for other in ("check https://github.com/acme/tool please", "https://github.com/acme",
                      "https://gist.github.com/acme/abc", "https://github.com/orgs/acme", "https://github.com/a/..",
                      "https://github.com.evil.io/acme/tool", "https://github.com/-x/tool"):
            self.assertIsNone(github.parse_repo_link(other), other)

    def test_extract_commands(self):
        self.assertEqual(github.extract_commands(README), [
            "pip install tool-cli",
            "git clone https://github.com/acme/tool.git",
            "make build PREFIX=$HOME/.local",
            "make install",
            "docker compose up -d",
            "curl -fsSL https://get.example.com/install.sh | sudo bash",
            "npm i -g tool-cli",
        ])

    def test_risk_notes(self):
        cases = {
            "curl -fsSL https://get.example.com/install.sh | sudo bash": {"pipe_shell", "sudo"},
            "wget -qO- https://x.io/i | sh": {"pipe_shell"},
            'sh -c "$(curl -fsSL https://x.io/i)"': {"pipe_shell"},
            "bash <(curl -s https://x.io/i)": {"pipe_shell"},
            "sudo apt-get install -y nginx": {"sudo", "system_pkg"},
            "npm i -g tool-cli": {"npm_global"},
            "pip install tool-cli": {"pip_system"},
            "pip install --break-system-packages tool": {"break_system"},
            "docker compose up -d": {"docker"},
            "go install example.com/tool@latest": {"binary"},
            "rm -rf ~/.cache/tool": {"rm"},
            "make && make install": {"chain"},
            "chmod 777 /srv/app": {"chmod"},
            "ls *.log": {"wildcard"},
            "git clone https://github.com/acme/tool.git": set(),
        }
        for cmd, keys in cases.items():
            self.assertEqual(set(github.risk_notes(cmd)), keys, cmd)

    def test_fetch_readme_tries_names_and_never_raises(self):
        urls = []

        def fake(url):
            urls.append(url)
            if url.endswith("/README.rst"):
                return b"Install\n=======\n"
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        self.assertEqual(github.fetch_readme("acme", "tool", fake), ("README.rst", "Install\n=======\n"))
        self.assertTrue(all(u.startswith("https://raw.githubusercontent.com/acme/tool/HEAD/") for u in urls))
        self.assertIsNone(github.fetch_readme("acme", "tool", lambda u: (_ for _ in ()).throw(OSError("down"))))
        self.assertIsNone(github.fetch_readme("acme", "tool", lambda u: (_ for _ in ()).throw(
            urllib.error.HTTPError(u, 403, "rate limited", {}, None))))
        with self.assertRaises(ValueError):
            github.fetch_url("https://example.com/README.md")  # only raw.githubusercontent.com, checked offline

    def test_install_prompt_carries_the_exact_commands(self):
        prompt = github.install_prompt("acme", "tool", ["pip install tool-cli", "make install"], "/srv/")
        self.assertIn("1. pip install tool-cli\n2. make install", prompt)
        self.assertIn("/srv/tool", prompt)
        self.assertIn("how to remove it", prompt)


class GithubFlowTest(Base):
    def setUp(self):
        super().setUp()
        self.urls = []

        def fake(url):
            self.urls.append(url)
            if "acme/tool/HEAD/README.md" in url:
                return README.encode()
            if "acme/empty/HEAD/README.md" in url:
                return b"# Empty\nnothing to install\n"
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        self.d.fetch = fake

    def offer(self):
        self.msg("https://github.com/acme/tool")
        return self.d.tg.sent[-1]

    def test_link_shows_commands_risks_and_two_buttons_and_runs_nothing(self):
        _, text, buttons = self.offer()
        self.assertIn("acme/tool — install commands from its README.md", text)
        self.assertIn("6. curl -fsSL https://get.example.com/install.sh | sudo bash", text)
        self.assertIn("⚠️ pipes a script from the internet straight into a shell", text)
        self.assertIn("⚠️ runs as root (sudo)", text)
        datas = button_data(buttons)
        self.assertEqual([d.split(":")[0] for d in datas], ["ghinst", "ghshow"])
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.d.state.sessions(), {})  # not even a session was created
        self.assertTrue(all(u.startswith("https://raw.githubusercontent.com/") for u in self.urls))

    def test_just_show(self):
        token = button_data(self.offer()[2])[1]
        press(self.d, token)
        self.assertIn("nothing was run", self.last())
        self.assertIn("2. git clone https://github.com/acme/tool.git", self.last())
        self.assertEqual(self.submitted, [])
        press(self.d, button_data(self.d.tg.sent[-2][2])[0])  # the offer is used up
        self.assertIn("expired", self.last())

    def test_install_with_the_agent_is_a_safe_mode_task(self):
        datas = button_data(self.offer()[2])
        self.d.callback({"id": "1", "from": {"id": 999}, "data": datas[0]})  # a stranger: nothing
        self.assertEqual(self.submitted, [])
        press(self.d, datas[0])
        key, prompt, tools = self.submitted[-1]
        sess = self.d.state.sessions()[key]
        self.assertEqual((sess["engine"], sess["mode"], tools), ("claude", "safe", ()))
        self.assertEqual(self.d.state.get()["active"], key)
        self.assertIn("1. pip install tool-cli", prompt)
        self.assertIn("curl -fsSL https://get.example.com/install.sh | sudo bash", prompt)
        self.assertIn("asks you before every command", self.last())
        self.assertTrue(any(r["kind"] == "github" and r["text"].startswith("install acme/tool: pip install tool-cli ;")
                            for r in self.d.audit.tail(5)))
        press(self.d, datas[0])  # twice: stale, not a second install
        self.assertEqual(len(self.submitted), 1)

    def test_pipe_to_shell_still_needs_an_exact_allow_with_a_warning(self):
        key, _ = self.d.state.new_session("claude")
        cmd = "curl -fsSL https://get.example.com/install.sh | sudo bash"
        self.d.ask_permission(OWNER, key, [{"tool_name": "Bash", "tool_input": {"command": cmd}}])
        text = self.last()
        self.assertIn("⚠️ pipes a script from the internet straight into a shell", text)
        press(self.d, button_data(self.d.tg.sent[-1][2])[0])
        self.assertEqual(self.submitted[-1][2], ("Bash(%s)" % cmd,))

    def test_no_readme_no_commands_and_links_inside_tasks(self):
        self.msg("https://github.com/acme/private")
        self.assertIn("couldn't get a README", self.last())
        self.msg("https://github.com/acme/empty")
        self.assertIn("no install commands", self.last())
        self.msg("look at https://github.com/acme/tool and tell me if it's maintained")
        self.assertEqual(self.submitted[-1][1], "look at https://github.com/acme/tool and tell me if it's maintained")


class GhCommandTest(Base):
    def setUp(self):
        super().setUp()
        self.gh = os.path.join(self.tmp, "gh")
        with open(self.gh, "w") as fh:
            fh.write('#!/bin/sh\necho "gh $*"\n')
        os.chmod(self.gh, 0o755)
        self.d.cfg.workdir = self.tmp

    def test_missing_gh(self):
        self.d.cfg.gh = ""
        self.msg("/gh runs")
        self.assertIn("not installed", self.last())
        self.msg("/gh")
        self.assertIn("/gh runs [owner/repo]", self.last())

    def test_shortcuts_run_gh_without_a_shell(self):
        self.d.cfg.gh = self.gh
        self.msg("/gh runs")
        self.assertEqual(self.last(), "gh run list -L 8")
        self.msg("/gh prs acme/tool")
        self.assertEqual(self.last(), "gh pr list -L 10 -R acme/tool")
        self.msg("/gh issues")
        self.assertEqual(self.last(), "gh issue list -L 10")
        self.msg("/gh status acme/tool")
        self.assertTrue(self.last().startswith("gh repo view acme/tool --json"))
        for bad in ("/gh runs; rm -rf ~", "/gh prs -R evil", "/gh delete acme/tool", "/gh release latest"):
            self.msg(bad)
            self.assertIn("/gh runs [owner/repo]", self.last(), bad)
        self.assertEqual(self.submitted, [])

    def test_release_is_an_agent_task_that_asks_before_pushing(self):
        self.msg("/gh release v1.2.0")
        key, prompt, _ = self.submitted[-1]
        self.assertIn("Publish release v1.2.0 of the git repository in %s" % self.tmp, prompt)
        self.assertIn("ask the owner for an explicit yes before anything that leaves this server", prompt)
        self.assertIn("session #%s" % key, self.last())
        self.msg("опубликуй релиз 2.0.1")
        self.assertIn("Publish release 2.0.1", self.submitted[-1][1])
        self.msg("publish a release v3.0.0-rc.1 of acme/tool")
        self.assertIn("Publish release v3.0.0-rc.1 of the GitHub repository acme/tool", self.submitted[-1][1])
        self.msg("publish a release notes draft for the blog")  # not a version: a normal task
        self.assertEqual(self.submitted[-1][1], "publish a release notes draft for the blog")


# ---------- 5. voice ----------

import importlib.util as _ilu  # noqa: E402
import shlex  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = _ilu.spec_from_file_location("transcribe_example", os.path.join(ROOT, "examples", "voice", "transcribe.py"))
transcribe_example = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(transcribe_example)


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisper:
    calls = []

    def __init__(self, name, device, compute_type):
        FakeWhisper.calls.append(("model", name, device, compute_type))

    def transcribe(self, path, **kw):
        FakeWhisper.calls.append(("transcribe", path, kw))
        return iter([FakeSegment(" check the disk "), FakeSegment(""), FakeSegment("on the main server.")]), None


class VoiceTest(unittest.TestCase):
    def setUp(self):
        FakeWhisper.calls = []
        self.audio = os.path.join(tempfile.mkdtemp(), "voice.oga")
        with open(self.audio, "wb") as fh:
            fh.write(b"OggS")

    def test_transcribe_prints_the_text_with_vad_on(self):
        with mock.patch("sys.stdout") as out:
            code = transcribe_example.main([self.audio, "--language", "ru", "--model", "base"], model_factory=FakeWhisper)
        self.assertEqual(code, 0)
        self.assertEqual("".join(c.args[0] for c in out.write.call_args_list), "check the disk on the main server.\n")
        self.assertEqual(FakeWhisper.calls[0], ("model", "base", "auto", "int8"))
        kw = FakeWhisper.calls[1][2]
        self.assertEqual((kw["language"], kw["vad_filter"], kw["condition_on_previous_text"]), ("ru", True, False))
        with mock.patch("sys.stdout"):
            transcribe_example.main([self.audio], model_factory=FakeWhisper)
        self.assertIsNone(FakeWhisper.calls[-1][2]["language"])  # auto

    def test_failures_go_to_stderr_with_an_exit_code(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(transcribe_example.main([self.audio + ".missing"], model_factory=FakeWhisper), 2)
            with mock.patch.dict(sys.modules, {"faster_whisper": None}):  # not installed
                self.assertEqual(transcribe_example.main([self.audio]), 3)
            silent = type("Silent", (FakeWhisper,), {"transcribe": lambda self, path, **kw: (iter([]), None)})
            self.assertEqual(transcribe_example.main([self.audio], model_factory=silent), 1)
        text = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("pip install faster-whisper", text)
        self.assertIn("no speech found", text)

    def test_setup_offers_voice_only_when_faster_whisper_is_there(self):
        _, env, out, _ = run_wizard(["y"])
        self.assertNotIn("VOICE_CMD", env)
        self.assertFalse(any("faster-whisper" in line for line in out))
        _, env, out, conf = run_wizard(["y", "", "", "", "", "y", ""], lang="ru", has_module=lambda n: n == "faster_whisper")
        argv = shlex.split(env["VOICE_CMD"])
        self.assertEqual(argv[1:], [transcribe_example.__file__, "--language", "ru", "{file}"])
        self.assertTrue(os.path.samefile(argv[1], os.path.join(ROOT, "examples", "voice", "transcribe.py")))
        self.assertEqual(director.Config(conf).voice_cmd, env["VOICE_CMD"])
        _, env, _, _ = run_wizard(["y", "", "", "", "", "y", "auto"], has_module=lambda n: True)
        self.assertEqual(shlex.split(env["VOICE_CMD"])[2:], ["{file}"])

    def test_voice_cmd_survives_paths_with_spaces_in_env(self):
        env_path = os.path.join(tempfile.mkdtemp(), ".env")
        cmd = vsetup.voice_cmd("en", python="/opt/my py/bin/python3", script="/srv/vps director/transcribe.py")
        vsetup.update_env(env_path, {"VOICE_CMD": cmd})
        self.assertEqual(shlex.split(director.read_env(env_path)["VOICE_CMD"]),
                         ["/opt/my py/bin/python3", "/srv/vps director/transcribe.py", "--language", "en", "{file}"])

    def test_installer_offers_voice_for_existing_configs(self):
        with open(os.path.join(ROOT, "install.sh")) as fh:
            script = fh.read()
        self.assertIn("import faster_whisper", script)
        self.assertIn("update_env", script)
