"""Terminal sessions of Claude Code and Codex on this server, for /continue.

Claude Code keeps one JSONL file per session in ~/.claude/projects/<cwd with every non-alphanumeric
character replaced by '-'>/<session id>.jsonl; Codex keeps ~/.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl.
Only the head of each file is read; anything malformed is skipped.
"""
import json
import os
import re

LIMIT = 8                 # sessions per engine
HEAD_LINES = 400          # read at most this many lines of a session file…
LINE_BYTES = 256 * 1024   # …each at most this long (longer lines are cut and fail to parse — skipped)
LABEL = 40
SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")  # never starts with '-': it becomes an argv item
UUID_TAIL = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")


def claude_project_name(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 2 * 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def label(text):
    """First non-empty line, whitespace collapsed, at most LABEL characters."""
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= LABEL else line[:LABEL].rstrip() + "…"


def records(path):
    """Dicts from the head of a JSONL file; unreadable files give nothing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(HEAD_LINES):
                line = fh.readline(LINE_BYTES)
                if not line:
                    return
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def human_text(content):
    """Text the person typed: a string, or the text blocks of a content list. Wrapped context that
    the CLIs inject (<command-name>, <environment_context>, …) does not count."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") in ("text", "input_text") and isinstance(b.get("text"), str)]
    else:
        parts = []
    text = "\n".join(parts).strip()
    return "" if not text or text.startswith("<") or text.startswith("Caveat:") else text


def claude_first_message(path):
    for rec in records(path):
        msg = rec.get("message")
        if rec.get("type") != "user" or rec.get("isMeta") or not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = human_text(msg.get("content"))
        if text:
            return text
    return ""


def codex_info(path):
    """(session id, cwd, first user message) of a Codex rollout file; old and new formats."""
    sid, cwd, first = None, None, ""
    for rec in records(path):
        kind, payload = rec.get("type"), rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
        if kind == "session_meta" or (sid is None and "instructions" in rec and isinstance(rec.get("id"), str)):
            sid = payload.get("id") if isinstance(payload.get("id"), str) else sid
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
        elif not first and payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
            first = human_text(payload["message"])
        elif not first and payload.get("type") == "message" and payload.get("role") == "user":
            first = human_text(payload.get("content"))
        if sid and first:
            break
    if not sid:
        match = UUID_TAIL.search(os.path.basename(path))
        sid = match.group(1) if match else None
    return sid, cwd, first


def newest(paths):
    stamped = []
    for path in paths:
        try:
            stamped.append((os.stat(path).st_mtime, path))
        except OSError:
            continue
    return sorted(stamped, reverse=True)


def claude_sessions(workdir, home):
    base = os.path.join(home, ".claude", "projects")
    names = dict.fromkeys([claude_project_name(workdir), workdir.replace("/", "-")])
    files = []
    for name in names:
        try:
            files += [os.path.join(base, name, f) for f in os.listdir(os.path.join(base, name)) if f.endswith(".jsonl")]
        except OSError:
            continue
    found = []
    for mtime, path in newest(files):
        sid = os.path.basename(path)[:-len(".jsonl")]
        text = claude_first_message(path) if SID_RE.fullmatch(sid) else ""
        if text:
            found.append({"engine": "claude", "sid": sid, "label": label(text), "mtime": mtime})
            if len(found) >= LIMIT:
                break
    return found


def inside(path, root):
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def codex_sessions(workdir, home):
    files = []
    for top, _dirs, names in os.walk(os.path.join(home, ".codex", "sessions")):
        files += [os.path.join(top, n) for n in names if n.startswith("rollout-") and n.endswith(".jsonl")]
    found = []
    for mtime, path in newest(files):
        sid, cwd, text = codex_info(path)
        if not (sid and SID_RE.fullmatch(sid) and text) or (cwd and not inside(cwd, workdir)):
            continue
        found.append({"engine": "codex", "sid": sid, "label": label(text), "mtime": mtime})
        if len(found) >= LIMIT:
            break
    return found


def find_sessions(workdir, home):
    """The newest Claude Code sessions for this folder, then Codex sessions started in it (or below)."""
    return claude_sessions(workdir, home) + codex_sessions(workdir, home)


def codex_rate(home, newest=20):
    """Codex subscription usage from its own session logs — the last `token_count` event's
    rate_limits in the newest rollouts: {"five": %, "week": %, "five_reset": ts, "week_reset": ts, "at": mtime}
    or None. Codex has no usage command; this is what it recorded after its last answer."""
    files = []
    for top, _dirs, names in os.walk(os.path.join(home, ".codex", "sessions")):
        for name in names:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                path = os.path.join(top, name)
                try:
                    files.append((os.path.getmtime(path), path))
                except OSError:
                    pass
    for mtime, path in sorted(files, reverse=True)[:newest]:
        found = None
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line or '"rate_limits"' not in line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except ValueError:
                        continue
                    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
                    if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                        found = rl
        except OSError:
            continue
        if found:
            prim, sec = found.get("primary") or {}, found.get("secondary") or {}
            return {"five": prim.get("used_percent"), "week": sec.get("used_percent"),
                    "five_reset": prim.get("resets_at"), "week_reset": sec.get("resets_at"), "at": mtime,
                    "plan": found.get("plan_type") or ""}
    return None
