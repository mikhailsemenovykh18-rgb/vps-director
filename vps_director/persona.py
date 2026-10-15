"""Who the bot is: its name, how it addresses the owner, its character, and what it remembers.

- The persona block lives in PROMPT.md between two marker lines, so a rename ("call yourself …",
  /name) can rewrite it without touching the rest of the owner's instruction.
- Memory is <conf>/memory.md: one fact per line, "- YYYY-MM-DD: text". It is added to the agent's
  system prompt on every task. Facts are masked with mask_secrets before they are stored: the system
  prompt is a command-line argument of the CLI, so a secret there would be visible in `ps`.
"""
import hashlib
import os
import re

from vps_director.texts import TEXTS

START = "<!-- persona: written by VPS Director, rewritten when you rename the bot -->"
END = "<!-- /persona -->"
DEFAULT_NAME = "VPS Director"
NAME_MAX = 40
FACT_MAX = 500
MAX_FACTS = 100
LINE = re.compile(r"- (\d{4}-\d{2}-\d{2}): (.+)")


def clean_name(text):
    """A name as typed ("«Jarvis»", '"Max"'), or None if it cannot be one: 1–40 chars, one line,
    no control characters, at most four words."""
    quotes = "«»\"'“”„`"
    text = text.strip().strip(quotes).strip().rstrip(".!").strip(quotes).strip()
    if not text or len(text) > NAME_MAX or re.search(r"[\x00-\x1f\x7f\x85  <>]", text):
        return None
    if len(text.split()) > 4:
        return None
    return text


def persona_block(name, owner, lang):
    text = TEXTS.get(lang, TEXTS["en"])
    who = text["persona_owner"].format(owner=owner) if owner else text["persona_no_owner"]
    return "%s\n%s\n%s" % (START, text["persona"].format(name=name or DEFAULT_NAME, owner_line=who).strip(), END)


def apply_persona(prompt, block):
    """The prompt with its persona block replaced (or put on top if it has none)."""
    start, end = prompt.find(START), prompt.find(END)
    if start >= 0 and end > start:
        return prompt[:start] + block + prompt[end + len(END):]
    return block + "\n\n" + prompt.lstrip("\n")


def fact_id(line):
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:10]


class Memory:
    def __init__(self, path, lock, mask):
        self.path, self.lock, self.mask = path, lock, mask

    def entries(self):
        """[(id, date, text)] in the order they were added; malformed lines are skipped."""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            match = LINE.fullmatch(line.strip())
            if match:
                out.append((fact_id(line.strip()), match.group(1), match.group(2)))
        return out[-MAX_FACTS:]

    def add(self, fact, day):
        """Store one fact; returns (stored text, True if something looked like a secret and was
        hidden), or (None, False) for an empty fact or a full memory."""
        fact = " ".join(fact.split())[:FACT_MAX]
        masked = self.mask(fact)
        if not masked:
            return None, False
        with self.lock:
            if len(self.entries()) >= MAX_FACTS:
                return None, False
            self._write(self._lines() + ["- %s: %s" % (day, masked)])
        return masked, masked != fact

    def delete(self, fid):
        with self.lock:
            lines = self._lines()
            keep = [ln for ln in lines if not (LINE.fullmatch(ln.strip()) and fact_id(ln.strip()) == fid)]
            if len(keep) == len(lines):
                return False
            self._write(keep)
            return True

    def prompt_block(self, lang):
        facts = self.entries()
        if not facts:
            return ""
        head = TEXTS.get(lang, TEXTS["en"])["memory_prompt"]
        return "\n\n%s\n%s\n" % (head, "\n".join("- %s: %s" % (day, text) for _, day, text in facts))

    def _lines(self):
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def _write(self, lines):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, self.path)


# ---------- phrases ----------

RENAME_SELF = re.compile(r"(?is)^(?:call yourself|your name is now|your name is|"
                         r"зови себя|называй себя|теперь тебя зовут|тебя зовут)\s+(.+)$")
RENAME_OWNER = re.compile(r"(?is)^(?:call me|my name is|зови меня|называй меня|меня зовут)\s+(.+)$")
REMEMBER = re.compile(r"(?is)^(?:please\s+)?(?:remember|запомни|запиши себе)(?=[\s:,—–-])\s*[:,—–-]?\s*"
                      r"(?:(?:that|что)\s+)?(\S.*)$")
# "call me when it's done" is a task, not a name
NOT_A_NAME = {"when", "if", "after", "back", "later", "once", "before", "at", "in", "tomorrow", "please",
              "когда", "если", "после", "потом", "как", "завтра", "через", "в", "перед", "пожалуйста"}


def parse_rename(text):
    """("self" | "owner", name) for "call yourself Jarvis" / "зови меня Миша", else None."""
    for who, pattern in (("self", RENAME_SELF), ("owner", RENAME_OWNER)):
        match = pattern.match(text.strip())
        if match:
            name = clean_name(match.group(1))
            words = (name or "").lower().split()
            if not name or len(words) > 3 or words[0] in NOT_A_NAME:
                return None
            return who, name
    return None


def parse_remember(text):
    """The fact in "remember: backups go to X" / "запомни, что …", else None."""
    match = REMEMBER.match(text.strip())
    return match.group(1).strip() if match else None
