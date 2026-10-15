"""`director.py setup`: first-run configuration in the terminal, without hunting for ids.

The wizard is a list of steps; each step is a function `step(wizard)` that asks questions through
`wizard.question()` / `wizard.confirm()` and fills `wizard.env` (the .env keys, in order) and
`wizard.facts` (what was learned on the way: the bot's username, the owner's Telegram user).
More questions (bot name, how to address the owner, morning brief time, …) are added by appending
steps to `Setup.steps` — the owner step leaves the owner's Telegram profile in `facts["owner"]`
for defaults such as their first name.

The token is read with hidden input and is never printed; .env is written 0600 in a 0700 folder.
"""
import getpass
import importlib.util
import os
import re
import shlex
import sys
import time
import urllib.error

from vps_director import servers
from vps_director.persona import DEFAULT_NAME, clean_name
from vps_director.schedule import clock_text, zone
from vps_director.texts import TEXTS

TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
WAIT_SECONDS = 180  # how long to wait for the owner's first message
POLL_SECONDS = 25

ENV_TAIL = """TITLE=VPS Director
# Where the agent works:
# WORKDIR={home}
# Folders WORKDIR, /director folders and /get must stay inside (':'-separated; empty = no limit):
# ALLOWED_ROOTS={home}
# Check the server every 10 minutes and write only when something breaks:
# WATCH_INTERVAL=10
# A command that prints the transcript of an audio file ({{file}} is its path; no shell):
# VOICE_CMD=/path/to/transcribe {{file}}
# A morning brief every day at this time (TZ above), or BRIEF=off:
# BRIEF=09:00
# Inbound webhooks on 127.0.0.1 (POST /notify, and /task with WEBHOOK_TASKS=1), see the README:
# WEBHOOK_PORT=8787
# WEBHOOK_SECRET=<a long random string, e.g. from: openssl rand -hex 24>
"""


def env_line(key, value):
    """KEY=value as read_env() reads it back: quoted when the value has ' #', quotes or edge spaces."""
    value = str(value)
    if re.search(r"\s#|^[\s'\"]|[\s'\"]$", value) or "#" in value[:1]:
        value = '"%s"' % value.replace('"', "'")
    return "%s=%s" % (key, value)


def write_private(path, text):
    """Atomic write, 0600 (also when the file existed with other permissions)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def update_env(path, values):
    """Set KEY=value lines in an existing .env (replaced in place, or appended); other lines and
    comments are kept. A value of None removes the key. Values must be one line."""
    for key, value in values.items():
        if value is not None and re.search(r"[\x00-\x1f\x7f\x85\u2028\u2029]", str(value)):
            raise ValueError("%s: one line, no control characters" % key)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    todo, out = dict(values), []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in todo:
            if todo[key] is not None:
                out.append(env_line(key, todo[key]))
            todo.pop(key)
            continue
        out.append(line)
    out += [env_line(k, v) for k, v in todo.items() if v is not None]
    write_private(path, "\n".join(out) + "\n")


class SetupError(Exception):
    """A reason to stop the setup; the message is safe to print (never contains the token)."""


def http_code(exc):
    return exc.code if isinstance(exc, urllib.error.HTTPError) else None


def describe_user(user):
    name = " ".join(str(user.get(k)) for k in ("first_name", "last_name") if user.get(k)) or "?"
    if user.get("username"):
        name += " (@%s)" % user["username"]
    return "%s, id %s" % (name, user.get("id"))


# The wizard speaks one language from start to end: the server's locale (Russian or English). The bot's own
# language (LANG) is a separate thing: it follows the owner's Telegram app.
UI = {
    "en": {"create": "Create a bot: open @BotFather in Telegram, send /newbot and copy the token it gives you.",
           "token": "Bot token (input is hidden): ", "not_token": "This does not look like a bot token (digits, a colon, letters).",
           "rejected": "Telegram did not accept this token.", "found": "Bot @%s found.",
           "send": "Now open @%s in Telegram and send it any message (waiting up to %d minutes)…",
           "from": "Message from %s.", "is_you": "Is this you? [y/N] ",
           "late": "No message arrived in time. You can type your numeric Telegram id instead "
                   "(@userinfobot tells it); an empty answer stops the setup.",
           "id": "Your Telegram id: ", "id_bad": "The id is a number.",
           "lang": "Language of the bot's messages [en/ru/es/de/fr/pt] (en): ", "lang_bad": "One of: en ru es de fr pt.",
           "saved": "Saved %s (owner id %s, bot language %s)."},
    "ru": {"create": "Создайте бота: откройте @BotFather в Telegram, отправьте /newbot и скопируйте токен, который он выдаст.",
           "token": "Токен бота (ввод скрыт): ", "not_token": "Не похоже на токен бота (цифры, двоеточие, буквы).",
           "rejected": "Telegram не принял этот токен.", "found": "Бот @%s найден.",
           "send": "Теперь откройте @%s в Telegram и отправьте ему любое сообщение (жду до %d минут)…",
           "from": "Сообщение от %s.", "is_you": "Это вы? [y/N] ",
           "late": "Сообщение не пришло. Можно ввести свой числовой id в Telegram (его скажет @userinfobot); "
                   "пустой ответ остановит установку.",
           "id": "Ваш id в Telegram: ", "id_bad": "id — это число.",
           "lang": "Язык сообщений бота [en/ru/es/de/fr/pt] (ru): ", "lang_bad": "Одно из: en ru es de fr pt.",
           "saved": "Сохранено: %s (владелец %s, язык бота %s)."},
}
BOT_LANGS = ("en", "ru", "es", "de", "fr", "pt")


def ui_lang(environ=None):
    """ru when the server's locale is Russian, otherwise en."""
    environ = os.environ if environ is None else environ
    loc = environ.get("LC_ALL") or environ.get("LC_MESSAGES") or environ.get("LANG") or ""
    return "ru" if loc.lower().startswith("ru") else "en"


def bot_lang(code):
    """The bot's language from a Telegram language_code ("pt-br" → pt); English if we do not have it."""
    code = str(code or "").lower()[:2]
    return code if code in BOT_LANGS else "en"


def step_token(wiz):
    """Bot token: hidden input, checked with getMe; three tries."""
    u = UI[wiz.ui]
    wiz.say(u["create"])
    for _ in range(3):
        token = wiz.secret(u["token"]).strip()
        if not TOKEN_RE.fullmatch(token):
            wiz.say(u["not_token"])
            continue
        try:
            me = wiz.api("getMe", {}, token, 15)
        except urllib.error.HTTPError:
            wiz.say(u["rejected"])
            continue
        except OSError as exc:  # no network: the reason never contains the URL (and so the token)
            raise SetupError("Could not reach api.telegram.org: %s" % getattr(exc, "reason", exc.__class__.__name__))
        username = ((me or {}).get("result") or {}).get("username")
        if not username:
            wiz.say(u["rejected"])
            continue
        wiz.token, wiz.facts["bot"] = token, username
        wiz.env["BOT_TOKEN"] = token
        wiz.say(u["found"] % username)
        return
    raise SetupError("No valid token — run the setup again when you have it.")


def step_owner(wiz):
    """The owner is whoever sends the bot a private message now and confirms it here."""
    u = UI[wiz.ui]
    offset = wiz.skip_backlog()
    wiz.say(u["send"] % (wiz.facts.get("bot", "your bot"), wiz.wait // 60))
    deadline = wiz.clock() + wiz.wait
    while wiz.clock() < deadline:
        poll = int(max(1, min(POLL_SECONDS, deadline - wiz.clock())))
        payload = {"timeout": poll, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset
        try:
            res = wiz.api("getUpdates", payload, wiz.token, poll + 10)
        except Exception as exc:  # noqa: BLE001
            wiz.check_conflict(exc)
            wiz.sleep(3)
            continue
        for upd in (res or {}).get("result") or []:
            if not isinstance(upd, dict) or not isinstance(upd.get("update_id"), int):
                continue
            offset = upd["update_id"] + 1
            msg = upd.get("message") if isinstance(upd.get("message"), dict) else {}
            user = msg.get("from") if isinstance(msg.get("from"), dict) else {}
            chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
            if chat.get("type") != "private" or user.get("is_bot") or not isinstance(user.get("id"), int):
                continue
            wiz.say(u["from"] % describe_user(user))
            if wiz.confirm(u["is_you"]):
                wiz.facts["owner"] = user
                wiz.env["OWNER_IDS"] = str(user["id"])
                wiz.env["LANG"] = bot_lang(user.get("language_code"))
                wiz.acknowledge(offset)
                return
    if offset:
        wiz.acknowledge(offset)
    wiz.say(u["late"])
    owner = wiz.question(u["id"], check=lambda v: v.isdigit(), retry=u["id_bad"])
    if not owner:
        raise SetupError("Setup stopped: no owner.")
    wiz.env["OWNER_IDS"] = owner
    wiz.env["LANG"] = wiz.question(u["lang"], default=wiz.ui, check=lambda v: v in BOT_LANGS, retry=u["lang_bad"])


def step_persona(wiz):
    """The bot's name and how it addresses the owner (default: their Telegram first name)."""
    t = TEXTS[wiz.ui]
    name = wiz.question(t["setup_name"], default=DEFAULT_NAME, check=clean_name, retry=t["setup_name_bad"])
    wiz.env["NAME"] = clean_name(name) or DEFAULT_NAME
    default = clean_name(str((wiz.facts.get("owner") or {}).get("first_name") or "")) or ""
    owner = wiz.question(t["setup_owner"].format(default=default) if default else t["setup_owner_none"],
                         default=default, check=clean_name, retry=t["setup_name_bad"])
    if clean_name(owner):
        wiz.env["OWNER_NAME"] = clean_name(owner)


def server_tz(environ=os.environ, localtime="/etc/localtime", timezone_file="/etc/timezone"):
    """The server's IANA time zone name, or "" when it cannot be told."""
    candidates = [environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath(localtime)
        if "zoneinfo/" in link:
            candidates.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        with open(timezone_file, encoding="utf-8") as fh:
            candidates.append(fh.read().strip())
    except OSError:
        pass
    return next((c for c in candidates if c and zone(c)), "")


def step_tz(wiz):
    """TZ for reminders, /at HH:MM, daily jobs and the brief; Enter = the server's zone."""
    t = TEXTS[wiz.ui]
    default = wiz.facts.get("server_tz", server_tz()) or "UTC"
    value = wiz.question(t["setup_tz"].format(default=default), default=default,
                         check=lambda v: zone(v) is not None, retry=t["setup_tz_bad"])
    wiz.env["TZ"] = value if zone(value) else default


def step_brief(wiz):
    """The morning brief is opt-in; Enter on the time = 09:00."""
    t = TEXTS[wiz.ui]
    if wiz.confirm(t["setup_brief"]):
        value = wiz.question(t["setup_brief_time"], default="09:00", check=clock_text, retry=t["setup_brief_bad"])
        wiz.env["BRIEF"] = clock_text(value) or "09:00"


TRANSCRIBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "voice", "transcribe.py")


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def voice_cmd(language="auto", python=sys.executable, script=TRANSCRIBE):
    """VOICE_CMD for examples/voice/transcribe.py (shlex-quoted: paths may contain spaces)."""
    return shlex.join([python, script] + (["--language", language] if language != "auto" else []) + ["{file}"])


def step_voice(wiz):
    """Offered only when faster-whisper is importable by this Python: voice messages via
    examples/voice/transcribe.py."""
    if not wiz.has_module("faster_whisper"):
        return
    t = TEXTS[wiz.ui]
    if not wiz.confirm(t["setup_voice"]):
        return
    lang = wiz.env.get("LANG", "en")
    language = wiz.question(t["setup_voice_lang"].format(default=lang), default=lang,
                            check=lambda v: re.fullmatch(r"auto|[a-z]{2,3}", v.lower()) is not None,
                            retry=t["setup_voice_lang_bad"]).lower()
    wiz.env["VOICE_CMD"] = voice_cmd(language)


def parse_picks(answer, count):
    """"1 3, 4" → [1, 3, 4]; None when a number is not on the list."""
    picks = []
    for word in re.split(r"[\s,;]+", answer.strip()):
        if not word:
            continue
        if not word.isdigit() or not 1 <= int(word) <= count:
            return None
        if int(word) not in picks:
            picks.append(int(word))
    return picks


def step_servers(wiz):
    """Offer the hosts of ~/.ssh/config for servers.json (the owner ticks which ones); skipped when
    servers.json exists or there are no hosts. The file is written with .env."""
    if os.path.exists(os.path.join(wiz.conf, servers.FILE)):
        return
    hosts = servers.ssh_config_hosts(wiz.ssh_config)
    if not hosts:
        return
    t = TEXTS[wiz.ui]
    wiz.say(t["setup_ssh_hosts"].format(hosts="\n".join("  %d. %s" % (i, h) for i, h in enumerate(hosts, 1))))
    picks = parse_picks(wiz.question(t["setup_ssh_pick"], default="", check=lambda v: parse_picks(v, len(hosts)) is not None,
                                     retry=t["setup_ssh_bad"]), len(hosts)) or []
    if picks:
        wiz.facts["servers"] = [{"ssh": "", "name": servers.local_name(), "main": True, "where": "", "role": ""}] + [
            {"ssh": hosts[i - 1], "name": hosts[i - 1], "main": False, "where": "", "role": ""} for i in picks]


class Setup:
    steps = [step_token, step_owner, step_persona, step_tz, step_brief, step_voice, step_servers]

    def __init__(self, conf_dir, api, ask=input, secret=getpass.getpass, out=print, clock=time.monotonic,
                 sleep=time.sleep, force=False, wait=WAIT_SECONDS, has_module=has_module, ssh_config=None):
        self.conf, self.api, self.ask, self.secret, self.say = conf_dir, api, ask, secret, out
        self.clock, self.sleep, self.force, self.wait, self.has_module = clock, sleep, force, wait, has_module
        self.ssh_config = ssh_config or os.path.expanduser("~/.ssh/config")
        self.env, self.facts, self.token = {}, {}, ""
        self.ui = ui_lang()

    def question(self, prompt, default="", check=None, retry="Please try again."):
        """One line of input; Enter gives `default`; `check(value)` False asks again (three times)."""
        for _ in range(3):
            value = self.ask(prompt).strip()
            if not value:
                return default
            if re.search(r"[\x00-\x1f\x7f]", value):  # every answer becomes one .env line
                self.say(retry)
                continue
            if check is None or check(value):
                return value
            self.say(retry)
        return default

    def confirm(self, prompt):
        return self.ask(prompt).strip().lower() in ("y", "yes", "д", "да")

    def skip_backlog(self):
        """Offset past the updates sent before the setup started: only a message sent now counts."""
        try:
            res = self.api("getUpdates", {"offset": -1, "timeout": 0}, self.token, 15)
        except Exception as exc:  # noqa: BLE001
            self.check_conflict(exc)
            return None
        ids = [u.get("update_id") for u in (res or {}).get("result") or [] if isinstance(u, dict)]
        ids = [i for i in ids if isinstance(i, int)]
        return max(ids) + 1 if ids else None

    def acknowledge(self, offset):
        """Confirm the updates up to `offset` so the bot does not replay them after it starts."""
        try:
            self.api("getUpdates", {"offset": offset, "timeout": 0}, self.token, 15)
        except Exception:  # noqa: BLE001  — at worst the bot answers that first message once
            pass

    @staticmethod
    def check_conflict(exc):
        if http_code(exc) == 409:
            raise SetupError("Another process is reading this bot's updates (a running instance or a webhook). "
                             "Stop it first, e.g. systemctl --user stop vps-director@<instance>, then retry.")
        if http_code(exc) in (401, 404):
            raise SetupError("Telegram no longer accepts this token.")

    def run(self):
        env_path = os.path.join(self.conf, ".env")
        if os.path.exists(env_path) and not self.force:
            raise SetupError("%s already exists; run the setup with --force to replace it." % env_path)
        for step in self.steps:
            step(self)
        self.write(env_path)
        self.say(UI[self.ui]["saved"] % (env_path, self.env.get("OWNER_IDS"), self.env.get("LANG")))
        return env_path

    def write(self, env_path):
        os.makedirs(self.conf, mode=0o700, exist_ok=True)
        os.chmod(self.conf, 0o700)
        lines = [env_line(*kv) for kv in self.env.items()]
        tail = ENV_TAIL.format(home=os.path.expanduser("~"))
        if "TITLE" in self.env or "NAME" in self.env:
            tail = tail.replace("TITLE=VPS Director\n", "")
        write_private(env_path, "\n".join(lines) + "\n" + tail)
        if self.facts.get("servers"):
            servers.write(self.conf, self.facts["servers"])
            t = TEXTS[self.ui]
            self.say(t["setup_ssh_saved"].format(path=os.path.join(self.conf, servers.FILE), n=len(self.facts["servers"])))
