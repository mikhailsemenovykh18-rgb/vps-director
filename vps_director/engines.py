"""Claude Code / Codex CLI: argv, stream parsers, permission rules, secret masking, process control."""
import json
import os
import re
import unicodedata
import select
import signal
import threading
import urllib.parse
import uuid


def json_obj(line):
    """One JSON line as a dict, or None for anything else (garbage, null, lists, numbers)."""
    try:
        ev = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return ev if isinstance(ev, dict) else None


def as_dict(value):
    return value if isinstance(value, dict) else {}


def claude_args(cfg, sess, extra_tools=()):
    """argv for `claude -p`; the task itself goes on stdin (never a CLI option, never in `ps`)."""
    args = [cfg.claude, "-p", "--output-format", "stream-json", "--verbose",
            "--append-system-prompt=" + cfg.prompt(),
            "--permission-mode", "bypassPermissions" if sess["mode"] == "full" else "default"]
    tools = list(cfg.allowed_tools) + list(extra_tools)
    if tools:
        args += ["--allowedTools"] + tools
    if sess.get("sid"):
        args += ["--resume", sess["sid"]]
    else:
        sess["sid"] = str(uuid.uuid4())
        args += ["--session-id", sess["sid"]]
    return args


SECRET_PATTERNS = [
    (re.compile(r"(?<![\w-])-p(?=[^\s-])\S+"), "-p***"),  # mysql -pSECRET
    (re.compile(r"(?i)(--?(?:password|passwd|pass|token|secret|api-?key)(?:=|\s+))(?:'[^']*'|\"[^\"]*\"|\S+)"), r"\1***"),
    (re.compile(r"(?i)\b((?:password|passwd|pwd|token|secret|api_?key|access_?key)\w*\s*[=:]\s*)(?:'[^']*'|\"[^\"]*\"|\S+)"),
     r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{30,}"), "***"),  # Telegram bot tokens
]


def mask_secrets(text):
    """Hide obvious secrets in text shown in the chat or the log (progress, permission requests)."""
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def describe_tool(name, inp):
    inp = as_dict(inp)
    what = (inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern")
            or inp.get("url") or inp.get("description") or "")
    lines = str(what).splitlines()
    return ("%s: %s" % (name, mask_secrets(lines[0])[:80])) if lines else name


class ClaudeStream:
    """Collects the stream-json events of one `claude -p` run."""

    def __init__(self):
        self.steps, self.result, self.denials, self.rate = [], None, [], None

    def feed(self, line):
        ev = json_obj(line)
        if ev is None:
            return False
        kind = ev.get("type")
        if kind == "assistant":
            content = as_dict(ev.get("message")).get("content")
            for block in content if isinstance(content, list) else []:
                if as_dict(block).get("type") == "tool_use":
                    self.steps.append(describe_tool(str(block.get("name")), block.get("input")))
                    return True
        elif kind == "rate_limit_event":
            self.rate = ev.get("rate_limit_info")
        elif kind == "result":
            res = ev.get("result")
            self.result = (res if isinstance(res, str) else "" if res is None else json.dumps(res)).strip()
            denials = ev.get("permission_denials")
            self.denials = [d for d in denials if isinstance(d, dict)] if isinstance(denials, list) else []
        return False


def codex_args(cfg, sess, last_file):
    """argv for `codex exec`; '-' makes it read the prompt from stdin (see codex_prompt)."""
    sandbox = (["--dangerously-bypass-approvals-and-sandbox"] if sess["mode"] == "full"
               else ["-c", 'sandbox_mode="read-only"'])
    common = ["--json", "--skip-git-repo-check", "-o", last_file] + sandbox
    if sess.get("sid"):
        return [cfg.codex, "exec", "resume", sess["sid"]] + common + ["-"]
    return [cfg.codex, "exec", "-C", cfg.workdir] + common + ["-"]


def codex_prompt(cfg, sess, prompt):
    """Codex has no system-prompt flag: the first turn carries the instruction before the task."""
    if sess.get("sid"):
        return prompt
    return "%s\n\n---\nTask from the owner:\n%s" % (cfg.prompt(), prompt)


class CodexStream:
    def __init__(self):
        self.steps, self.thread, self.answer, self.error = [], None, "", ""

    def feed(self, line):
        ev = json_obj(line)
        if ev is None:
            return False
        kind, item = ev.get("type"), as_dict(ev.get("item"))
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            self.thread = ev["thread_id"]
        elif kind == "item.started" and item.get("type") == "command_execution":
            self.steps.append("shell: %s" % str(item.get("command", ""))[:80])
            return True
        elif kind == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
            self.answer = item.get("text") if isinstance(item.get("text"), str) and item["text"] else self.answer
        elif kind in ("error", "turn.failed"):
            err = ev.get("error")
            msg = ev.get("message") or (err if isinstance(err, str) else as_dict(err).get("message"))
            self.error = str(msg) if msg else self.error
        return False


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def permission_rule(denial):
    """The narrowest --allowedTools rule that covers exactly this denied action.
    Bash → Bash(<command>); file edits → Tool(<path>) (absolute paths as //path, Claude Code's
    syntax for the filesystem root); WebFetch → WebFetch(domain:<host>); else the bare tool name."""
    name = str(denial.get("tool_name") or "")
    inp = denial.get("tool_input") if isinstance(denial.get("tool_input"), dict) else {}
    if name == "Bash" and isinstance(inp.get("command"), str) and inp["command"].strip():
        return "Bash(%s)" % inp["command"].strip()
    if name in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        if isinstance(path, str) and path.strip():
            path = path.strip()
            return "%s(%s)" % (name, "/" + path if path.startswith("/") else path)
    if name == "WebFetch" and isinstance(inp.get("url"), str):
        host = urllib.parse.urlsplit(inp["url"].strip()).hostname
        if host:
            return "WebFetch(domain:%s)" % host
    return name


def is_pattern_rule(rule):
    """True when the rule's argument contains '*': in Claude's rule syntax that is a wildcard, so the
    granted rule would be broader than the exact action the owner was shown."""
    return "(" in rule and "*" in rule[rule.index("(") + 1:]


APPROVE_MAX = 1000  # longer rules get no Allow button: the owner could not read them in full


def unsafe_to_approve(rule):
    """Why a rule must not get an Allow button, or None. Allow grants the rule verbatim, so the owner
    has to see it verbatim: no masked secrets, nothing pushed out of view by length, line breaks or
    whitespace runs, no invisible or direction-flipping characters."""
    if len(rule) > APPROVE_MAX:
        return "long"
    if "\n" in rule or "\r" in rule or re.search(r"\s{6,}", rule):
        return "hidden"
    if any(unicodedata.category(ch) in ("Cc", "Cf") and ch != "\t" for ch in rule):
        return "hidden"
    if mask_secrets(rule) != rule:
        return "secret"
    return None


def describe_denial(d):
    inp = as_dict(d.get("tool_input"))
    what = inp.get("command") or inp.get("file_path") or inp.get("url") or json.dumps(inp, ensure_ascii=False)
    return "%s: %s" % (d.get("tool_name"), mask_secrets(str(what))[:300])


# ---------- engine process ----------

def kill_tree(proc, killed=None, grace=5):
    """SIGTERM the engine's whole process group (it runs in its own session), SIGKILL after `grace` s."""
    if killed is not None:
        killed.set()

    def signal_group(sig):
        try:
            os.killpg(proc.pid, sig)
        except OSError:  # already gone
            pass
    signal_group(signal.SIGTERM)
    hard = threading.Timer(grace, signal_group, (signal.SIGKILL,))
    hard.daemon = True
    hard.start()


def read_lines(proc):
    """Lines of the child's stdout. Stops at EOF, or once the child has exited and the pipe is quiet —
    a daemonized grandchild that inherited stdout must not keep the worker blocked forever."""
    fd, buf = proc.stdout.fileno(), b""
    while True:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                yield line.decode("utf-8", "replace")
        elif proc.poll() is not None:
            break
    if buf:
        yield buf.decode("utf-8", "replace")


def feed_stdin(proc, text):
    try:
        proc.stdin.write(text.encode("utf-8"))
    except OSError:  # the child exited without reading
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
