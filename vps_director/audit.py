"""Append-only audit log: one JSON object per line in <conf>/audit.log (0600).

Fields: time (local, ISO 8601), from (Telegram id of the owner, or "scheduler" / "watchdog" / "webhook" / "notify"), kind
(task / command / allow / deny / mode / director / job / name / memory / alert / resolved / notify / github / web), text (short, secrets masked).
"""
import json
import os
import time

KINDS = ("task", "command", "allow", "deny", "mode", "director", "job", "name", "memory", "alert", "resolved", "notify", "github", "web")
TEXT_MAX = 300
BLOCK = 64 * 1024


class AuditLog:
    def __init__(self, path, lock, mask):
        self.path, self.lock, self.mask = path, lock, mask

    def write(self, sender, kind, text):
        """Append one entry; a failure to write never breaks the bot (returns False)."""
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from": str(sender), "kind": kind,
               "text": self.mask(" ".join(str(text).split()))[:TEXT_MAX]}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                return False
        return True

    def tail(self, count):
        """The last `count` well-formed entries, oldest first; reads the file from the end."""
        with self.lock:
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    pos, data = fh.tell(), b""
                    while pos > 0 and data.count(b"\n") <= count + 16:
                        step = min(BLOCK, pos)
                        pos -= step
                        fh.seek(pos)
                        data = fh.read(step) + data
            except OSError:
                return []
        lines = data.split(b"\n")
        if pos > 0:
            lines = lines[1:]  # the first piece may be the end of a longer line
        out = []
        for raw in reversed(lines):
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(rec, dict) and {"time", "from", "kind", "text"} <= set(rec):
                out.append(rec)
                if len(out) >= count:
                    break
        return out[::-1]
