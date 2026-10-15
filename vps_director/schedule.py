"""Scheduled jobs: parsing `/at` / `/every` arguments and computing the next run.

A job is a dict stored in state["jobs"][id]:
    id, kind ("task" → the agent, "command" → a script command or /status, "reminder" → a plain message),
    text, chat, next (unix time), every (seconds) or daily ("HH:MM", server local time) for repeating
    jobs, changes (send a command's output only when it changed), session (director session of a task
    job), last_hash, created.
Times of day are in the owner's time zone (TZ in .env, an IANA name; server local time when it is
not set); a repeating job that missed runs (bot stopped) runs once and keeps its phase — no burst of
catch-up runs.
"""
import datetime
import re
import time
import zoneinfo

MIN_INTERVAL = 5 * 60
MAX_INTERVAL = 7 * 86400
MAX_DELAY = 366 * 86400
UNITS = {"m": 60, "min": 60, "h": 3600, "d": 86400, "м": 60, "мин": 60, "ч": 3600, "д": 86400}
PART = re.compile(r"(\d{1,6})(min|мин|m|h|d|м|ч|д)")
CLOCK = re.compile(r"([01]?\d|2[0-3])[:.]([0-5]\d)")


def parse_duration(text):
    """'30m', '2h', '1d', '1h30m' (also м/ч/д) → seconds; None if it is not a positive duration."""
    text = text.strip().lower()
    pos, total = 0, 0
    for match in PART.finditer(text):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * UNITS[match.group(2)]
        pos = match.end()
    return total if text and pos == len(text) and total > 0 else None


def zone(name):
    """ZoneInfo for an IANA name, or None (server local time) for an empty or unknown one."""
    if not name or name.startswith(("/", ".")) or ".." in name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def local(ts, tz=None):
    """A datetime for `ts` in `tz` (server local time when tz is None)."""
    return datetime.datetime.fromtimestamp(ts, tz) if tz else datetime.datetime.fromtimestamp(ts)


def at_clock(day, hour, minute, tz=None):
    """Unix time of HH:MM on a date (a datetime.date) in `tz`."""
    if tz:
        return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).timestamp()
    return time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))


def next_clock(hhmm, now, tz=None):
    """The next moment strictly after `now` when the clock in `tz` (or the server's) shows HH:MM."""
    match = CLOCK.fullmatch(hhmm)
    hour, minute = int(match.group(1)), int(match.group(2))
    if tz:
        day = local(now, tz).date()
        for add in range(0, 3):
            ts = at_clock(day + datetime.timedelta(days=add), hour, minute, tz)
            if ts > now:
                return ts
        return now + 86400
    day = time.localtime(now)
    for add in range(0, 3):
        ts = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + add, hour, minute, 0, 0, 0, -1))
        if ts > now:
            return ts
    return now + 86400  # unreachable in practice


def clock_text(text):
    match = CLOCK.fullmatch(text.strip())
    return "%02d:%s" % (int(match.group(1)), match.group(2)) if match else None


def parse_when(text, now, tz=None):
    """'30m' / '2h' / '1d' / 'HH:MM' → unix time of the run. ValueError if it is neither."""
    hhmm = clock_text(text)
    if hhmm:
        return next_clock(hhmm, now, tz)
    delay = parse_duration(text)
    if delay is None or delay > MAX_DELAY:
        raise ValueError(text)
    return now + delay


def at_date(day, hhmm, now, tz=None):
    """"2026-10-17" + "09:30" in the owner's time zone → unix time; ValueError if it is malformed,
    already past, or more than MAX_DELAY ahead."""
    clock = clock_text(hhmm or "")
    if not clock or not isinstance(day, str):
        raise ValueError(day)
    try:
        date = datetime.date.fromisoformat(day)
    except ValueError:
        raise ValueError(day) from None
    h, m = (int(x) for x in clock.split(":"))
    moment = datetime.datetime(date.year, date.month, date.day, h, m, tzinfo=tz) if tz else \
        datetime.datetime(date.year, date.month, date.day, h, m)
    ts = moment.timestamp()
    if ts <= now or ts - now > MAX_DELAY:
        raise ValueError(day)
    return ts


def parse_every(text):
    """'15m'…'7d' → {"every": seconds}; 'HH:MM' → {"daily": "HH:MM"}. ValueError otherwise."""
    hhmm = clock_text(text)
    if hhmm:
        return {"daily": hhmm}
    every = parse_duration(text)
    if every is None or not MIN_INTERVAL <= every <= MAX_INTERVAL:
        raise ValueError(text)
    return {"every": every}


def first_run(spec, now, tz=None):
    return next_clock(spec["daily"], now, tz) if spec.get("daily") else now + spec["every"]


def next_run(job, now, tz=None):
    """When a job that is due now runs next; None for a one-shot job."""
    if job.get("daily"):
        return next_clock(job["daily"], now, tz)
    every = int(job.get("every") or 0)
    if every <= 0:
        return None
    nxt = float(job.get("next") or now) + every
    if nxt <= now:
        nxt += ((now - nxt) // every + 1) * every
    return nxt


def fmt_duration(seconds, lang="en"):
    out, seconds = "", int(seconds)
    names = ("d", "h", "m") if lang != "ru" else (" д ", " ч ", " мин ")
    for unit, size in zip(names, (86400, 3600, 60)):
        if seconds >= size:
            out += "%d%s" % (seconds // size, unit)
            seconds %= size
    return out.strip() or ("0m" if lang != "ru" else "0 мин")


def fmt_time(ts, tz=None):
    return local(float(ts or 0), tz).strftime("%d.%m %H:%M")


WORDS = {"en": ("daily at %s", "every %s", "once"), "ru": ("ежедневно в %s", "каждые %s", "один раз"),
         "es": ("cada día a las %s", "cada %s", "una vez"), "de": ("täglich um %s", "alle %s", "einmal"),
         "fr": ("chaque jour à %s", "toutes les %s", "une fois"), "pt": ("todo dia às %s", "a cada %s", "uma vez")}


def describe(job, lang="en"):
    daily, every, once = WORDS.get(lang, WORDS["en"])
    if job.get("daily"):
        return daily % job["daily"]
    if job.get("every"):
        return every % fmt_duration(job["every"])
    return once
