"""Settings the owner can change from the Mini App. They are written to .env (other lines and comments
are kept, the old file is saved as .env.bak) and the bot restarts itself to apply them, after the task
that is running (if any) finishes.

Only safe settings are here. The bot token, ports, WEB_URL / WEB_BIND, webhooks, ALLOWED_ROOTS,
ALLOWED_TOOLS, COMMANDS and PROMPT_FILE stay hand-edited: a stolen dashboard session must not be able
to open the bot up wider than the owner made it.
"""
import datetime
import os
import re
import zoneinfo

from vps_director import schedule

# key → (kind, options); the order is the order on the settings page
FIELDS = [
    ("NAME", "text", {"min": 1, "max": 40}),
    ("OWNER_NAME", "text", {"min": 0, "max": 40}),
    ("LANG", "choice", {"choices": ["en", "ru", "es", "de", "fr", "pt"]}),
    ("TZ", "tz", {}),
    ("BRIEF", "time_or_off", {}),
    ("WORKDIR", "dir", {}),
    ("TIMEOUT", "int", {"min": 60, "max": 86400, "unit": "s"}),
    ("WATCH_INTERVAL", "int", {"min": 0, "max": 1440, "unit": "min"}),
    ("WATCH_DISK_PCT", "int", {"min": 50, "max": 99, "unit": "%"}),
    ("PROGRESS", "bool", {}),
    ("WEB_CONSOLE", "bool", {}),
    ("WEB_CONSOLE_AUTO", "bool", {}),
    ("CONSOLE_KEEP", "int", {"min": 5, "max": 720, "unit": "min"}),
    ("CONSOLE_TMUX", "bool", {}),
    ("AGENT_MODE", "choice", {"choices": ["safe", "full"]}),
]
KEYS = [k for k, _, _ in FIELDS]
BAD_TEXT = re.compile(r"[\x00-\x1f\x7f\"\\\\]")


def current(cfg):
    """{key: value} as the page shows it (what the running bot uses)."""
    return {
        "NAME": cfg.name, "OWNER_NAME": cfg.owner_name, "LANG": cfg.lang, "TZ": cfg.tz_name,
        "BRIEF": cfg.brief or "off", "WORKDIR": cfg.workdir, "TIMEOUT": cfg.timeout,
        "WATCH_INTERVAL": cfg.watch_interval, "WATCH_DISK_PCT": cfg.watch_disk_pct,
        "PROGRESS": cfg.progress, "WEB_CONSOLE": cfg.web_console,
        "WEB_CONSOLE_AUTO": bool(getattr(cfg, "web_console_auto", False)),
        "CONSOLE_KEEP": int(getattr(cfg, "console_keep", 1800)) // 60,
        "CONSOLE_TMUX": bool(getattr(cfg, "console_tmux", False)),
        "AGENT_MODE": getattr(cfg, "agent_mode", "safe"),
    }


ZONE_AREAS = ("Africa/", "America/", "Antarctica/", "Asia/", "Atlantic/", "Australia/", "Europe/", "Indian/", "Pacific/")


def utc_offset(name, now=None):
    """"UTC+03:00" — the zone's offset right now (summer time included)."""
    off = datetime.datetime.now(zoneinfo.ZoneInfo(name)).utcoffset() if now is None else \
        datetime.datetime.fromtimestamp(now, zoneinfo.ZoneInfo(name)).utcoffset()
    minutes = int(off.total_seconds() // 60)
    return "UTC%s%02d:%02d" % ("+" if minutes >= 0 else "−", abs(minutes) // 60, abs(minutes) % 60), minutes


def zones(now=None):
    """[{"offset": "UTC+03:00", "zones": ["Europe/Moscow", …]}] sorted from west to east: the time zone
    picker. Only real places (Area/City), plus UTC itself."""
    groups = {}
    for name in sorted(zoneinfo.available_timezones()):
        if name != "UTC" and (not name.startswith(ZONE_AREAS) or name.count("/") > 2):
            continue
        try:
            label, minutes = utc_offset(name, now)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            continue
        groups.setdefault((minutes, label), []).append(name)
    return [{"offset": label, "zones": names} for (_, label), names in sorted(groups.items())]


def schema(command_names):
    out = []
    for key, kind, opts in FIELDS:
        item = dict(opts, key=key, kind=kind)
        if kind == "tz":
            item["groups"] = zones()
        out.append(item)
    return out


def normalize(key, value, cfg, command_names):
    """The .env text for a new value, or ValueError with a short reason."""
    spec = next(((kind, opts) for k, kind, opts in FIELDS if k == key), None)
    if spec is None:
        raise ValueError("not editable here")
    kind, opts = spec
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return "1" if value else "0"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not opts["min"] <= value <= opts["max"]:
            raise ValueError("a whole number %d–%d" % (opts["min"], opts["max"]))
        return str(value)
    if not isinstance(value, str):
        raise ValueError("expected text")
    value = value.strip()
    if kind == "text":
        if BAD_TEXT.search(value) or not opts["min"] <= len(value) <= opts["max"]:
            raise ValueError("%d–%d characters, no quotes" % (opts["min"], opts["max"]))
        return value
    if kind == "choice":
        if value not in opts["choices"]:
            raise ValueError("one of " + ", ".join(opts["choices"]))
        return value
    if kind == "tz":
        if value and not schedule.zone(value):
            raise ValueError("a time zone like Europe/Moscow")
        return value
    if kind == "time_or_off":
        if value.lower() in ("", "off", "0", "no"):
            return "off"
        clock = schedule.clock_text(value)
        if not clock:
            raise ValueError("HH:MM or off")
        return clock
    if kind == "dir":
        path = os.path.expanduser(value)
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise ValueError("an existing folder (absolute path)")
        if not cfg.inside_roots(path):
            raise ValueError("outside ALLOWED_ROOTS")
        return path
    raise ValueError("not editable here")


def env_line(key, value):
    return '%s="%s"' % (key, value) if re.search(r"[\s#'\"]", value) else "%s=%s" % (key, value)


def write_env(conf_dir, updates):
    """Set (or, for None, remove) keys in conf_dir/.env, keeping every other line. Saves .env.bak first."""
    from vps_director.setup import write_private
    path = os.path.join(conf_dir, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    if lines:
        write_private(path + ".bak", "\n".join(lines) + "\n")
    left = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in left:
            value = left.pop(key)
            if value is not None:
                out.append(env_line(key, value))
            continue
        if key in updates:  # a second line for a key already written: drop it
            continue
        out.append(line)
    out += [env_line(k, v) for k, v in left.items() if v is not None]
    write_private(path, "\n".join(out) + "\n")
