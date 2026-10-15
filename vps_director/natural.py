"""Reminders and scheduled tasks from plain phrases, parsed locally (no AI).

    напомни через 2 часа позвонить в банк        remind me in 30 minutes to stretch
    напомни завтра в 10:00 продлить домен        remind me at 18:00 call the bank
    напомни позвонить маме в 19                  remind me tomorrow at 9am check the invoices
    каждый день в 9:00 проверь бэкапы            every day at 9:00 check the backups
    каждые 2 часа /disk                          every hour /status

parse() returns None when the message is not such a phrase (it then goes to the agent as usual), a
dict {"error": …} when it clearly is one but something is missing, or a plan:
    {"kind": "reminder" | "task" | "command", "text", "next": unix time, "every" | "daily"}.
A plan is a "task" when its text starts with a verb like "проверь" / "check" / "run", a "command"
when it starts with "/", otherwise a reminder. Times of day are in `tz` (see schedule.py).
"""
import datetime
import re

from vps_director import schedule

UNIT = (r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|минут[ауы]?|мин|час(?:а|ов)?|день|дня|дней|сут(?:ки|ок)|"
        r"[mhd]|м|ч|д)")
UNIT_SECONDS = (("min", 60), ("мин", 60), ("m", 60), ("м", 60), ("h", 3600), ("час", 3600), ("ч", 3600),
                ("d", 86400), ("ден", 86400), ("дн", 86400), ("сут", 86400), ("д", 86400))
COUNT = r"(?P<count>\d{1,4}|an?|one|a couple of|couple of|один|одну|одна|пару|пять|два|две|три|четыре)"
WORD_COUNTS = {"a": 1, "an": 1, "one": 1, "один": 1, "одну": 1, "одна": 1, "a couple of": 2, "couple of": 2,
               "пару": 2, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5}
CLOCK = r"(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.|утра|дня|вечера|ночи)?"
WEEKDAYS = (("monday", "понедельник"), ("tuesday", "вторник"), ("wednesday", "среду", "среда"),
            ("thursday", "четверг"), ("friday", "пятницу", "пятница"), ("saturday", "субботу", "суббота"),
            ("sunday", "воскресенье"))
DAY = (r"(?:(?:on|во|в)\s+)?(?P<day>today|tomorrow|the day after tomorrow|сегодня|завтра|послезавтра|"
       + "|".join(name for names in WEEKDAYS for name in names) + ")")
DAY_OFFSET = {"today": 0, "сегодня": 0, "tomorrow": 1, "завтра": 1, "the day after tomorrow": 2, "послезавтра": 2}

END = r"(?=$|[\s,.;:!?—–-])"
REMIND = re.compile(r"(?i)^\s*(?:напомни(?:\s+мне)?|remind\s+me|remind)" + END + r"[\s,:—–-]*")
DAILY = re.compile(r"(?i)(?:every\s+day|everyday|daily|each\s+day|каждый\s+день|ежедневно)(?:\s+(?:at|в))?\s+"
                   + CLOCK + END)
EVERY = re.compile(r"(?i)(?:every|each|каждые|каждый|каждую|каждое)\s+(?:" + COUNT + r"\s+)?" + UNIT + END)
RELATIVE = re.compile(r"(?i)(?:через|in|after)\s+(?:(?P<half>полчаса|half\s+an\s+hour)|(?:" + COUNT + r"\s*)?"
                      + UNIT + ")" + END)
ABSOLUTE = re.compile(r"(?i)(?:" + DAY + r"\s+)?(?:at|в|к|by)\s+" + CLOCK + END + r"(?:\s+" + DAY.replace("day>", "day2>") + END + ")?")
DAY_ONLY = re.compile(r"(?i)(?<!\w)" + DAY + END)
STARTS_EVERY = re.compile(r"(?i)^\s*(?:every|each|daily|everyday|каждые|каждый|каждую|каждое|ежедневно)" + END)

LEAD = re.compile(r"(?i)^(?:[\s,:;—–-]+|to\s+|that\s+|about\s+|что(?:бы)?\s+|о\s+том,?\s+что\s+|про\s+|о\s+)+")
# the agent's work ("check the backups"), not a note to a human ("send the invoice", "renew the domain")
VERBS = {
    "check", "run", "look", "see", "restart", "show", "find", "update", "upgrade", "clean", "verify", "test",
    "build", "deploy", "summarize", "summarise", "count", "list", "rotate", "backup",
    "compare", "analyze", "analyse", "measure", "fix", "collect", "monitor", "investigate", "review", "scan",
    "проверь", "посмотри", "глянь", "запусти", "перезапусти", "выполни", "сделай", "собери", "обнови", "почисти",
    "очисти", "найди", "покажи", "пришли", "подсчитай", "посчитай", "сравни", "проанализируй", "измерь",
    "почини", "исправь", "просканируй", "забэкапь", "составь", "сверь", "подготовь",
}

MAX_TEXT = 1000
# "remind me how the backup works" / "напомни, как настроить nginx" is a question for the agent
QUESTION = re.compile(r"(?i)^[\s,:—–-]*(?:how|what|why|where|which|who|when|whether|if|как|что|чем|почему|зачем|где|"
                      r"куда|откуда|какой|какая|какое|какие|кто|когда|сколько|ли)" + END)


def unit_seconds(unit):
    unit = unit.lower()
    for prefix, size in UNIT_SECONDS:
        if unit.startswith(prefix):
            return size
    return None


def count_value(raw):
    if not raw:
        return 1
    raw = " ".join(raw.lower().split())
    return int(raw) if raw.isdigit() else WORD_COUNTS.get(raw, 1)


def clock(match):
    """(hour, minute) from a CLOCK match, with am/pm and утра/дня/вечера/ночи; None if impossible."""
    hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm in ("pm", "вечера", "дня") and 1 <= hour < 12:
        hour += 12
    elif ampm in ("am", "ночи") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or (ampm in ("am", "pm") and not 0 <= hour <= 23):
        return None
    return hour, minute


def day_offset(word, today):
    """Days from `today` (a date) to "tomorrow" / "в пятницу" …; a weekday means its next occurrence
    (today counts if the time has not passed yet — the caller moves it a week on otherwise)."""
    word = " ".join(word.lower().split())
    if word in DAY_OFFSET:
        return DAY_OFFSET[word]
    for index, names in enumerate(WEEKDAYS):
        if word in names:
            return (index - today.weekday()) % 7
    return 0


def is_weekday(word):
    return any(word.lower() in names for names in WEEKDAYS)


def kind_of(text):
    if text.startswith("/"):
        return "command"
    first = re.split(r"[\s,.:;!?]+", text.lower(), maxsplit=1)[0]
    return "task" if first in VERBS else "reminder"


def body(rest, span):
    text = (rest[:span[0]] + " " + rest[span[1]:]).strip()
    text = LEAD.sub("", text).strip(" ,.;:—–-")
    return " ".join(text.split())[:MAX_TEXT]


def parse(text, now, tz=None, implicit=False):
    """See the module docstring. implicit=True: the text is the part after "remind me" (used when the
    owner edits a job: they send just the new time and text)."""
    text = text.strip()
    remind = REMIND.match(text)
    if remind:
        rest = text[remind.end():]
    elif implicit:
        rest = text
    elif STARTS_EVERY.match(text):
        rest = text
    else:
        return None
    strict_start = not remind and not implicit  # "every …" must open the message, or it is just a task

    found = []  # the earliest time expression wins ("…через час, что каждый день…" is not a daily job)
    for prio, (pattern, kind) in enumerate(((DAILY, "daily"), (EVERY, "every"), (RELATIVE, "relative"),
                                            (ABSOLUTE, "absolute"))):
        match = pattern.search(rest)
        if match:
            found.append((match.start(), prio, kind, match))
    if found:
        start, _, kind, match = min(found, key=lambda f: f[:2])
        if not strict_start or (start == 0 and kind in ("daily", "every")):
            plan = when(kind, match, now, tz)
            if "error" in plan:
                return plan
            plan["text"] = body(rest, match.span())
            if not plan["text"]:
                return {"error": "text"}
            plan["kind"] = kind_of(plan["text"])
            return plan
    if strict_start:
        return None  # "every time I deploy …": not a schedule
    day = DAY_ONLY.search(rest)
    if day:  # "напомни завтра продлить домен", "remind me on friday …": that day at 09:00
        today = schedule.local(now, tz).date()
        offset = day_offset(day.group("day"), today)
        if offset == 0 and is_weekday(day.group("day")):
            offset = 7
        if offset:
            date = today + datetime.timedelta(days=offset)
            plan = {"next": schedule.at_clock(date, 9, 0, tz), "text": body(rest, day.span())}
            if not plan["text"]:
                return {"error": "text"}
            plan["kind"] = kind_of(plan["text"])
            return plan
    if QUESTION.match(rest):
        return None
    return {"error": "when"}


def when(kind, match, now, tz):
    if kind == "daily":
        hm = clock(match)
        return {"daily": "%02d:%02d" % hm, "next": schedule.next_clock("%02d:%02d" % hm, now, tz)} if hm \
            else {"error": "when"}
    if kind in ("every", "relative"):
        if match.groupdict().get("half"):
            seconds = 1800
        else:
            size = unit_seconds(match.group("unit"))
            seconds = count_value(match.group("count")) * size if size else 0
        if kind == "every":
            if not schedule.MIN_INTERVAL <= seconds <= schedule.MAX_INTERVAL:
                return {"error": "every"}
            return {"every": seconds, "next": now + seconds}
        if not 0 < seconds <= schedule.MAX_DELAY:
            return {"error": "when"}
        return {"next": now + seconds}
    hm = clock(match)
    if not hm:
        return {"error": "when"}
    day = match.group("day") or match.group("day2")
    if not day:
        return {"next": schedule.next_clock("%02d:%02d" % hm, now, tz)}
    today = schedule.local(now, tz).date()
    date = today + datetime.timedelta(days=day_offset(day, today))
    ts = schedule.at_clock(date, hm[0], hm[1], tz)
    if ts <= now and is_weekday(day):  # "в субботу в 10" said on Saturday at noon: next Saturday
        ts = schedule.at_clock(date + datetime.timedelta(days=7), hm[0], hm[1], tz)
    return {"next": ts} if ts > now else {"error": "past"}
