"""The web console: a terminal in the Mini App / dashboard (the most dangerous feature — read this).

Off unless WEB_CONSOLE=1. Opening a console takes three steps, and a stolen browser session alone
cannot get past the second one:

1. `POST /api/console {"server": name}` (authenticated and CSRF-checked like every API call, see
   webauth.py) → the bot asks the user in Telegram "Open console on <server>? [Allow] [No]" and
   the API answers 202 {"pending": id}. The server is looked up by name in servers.json; the
   client never gives an ssh alias or a command.
2. The client polls `GET /api/console/<id>` (only the user who asked can see it). After Allow it
   gets a one-time ticket: 32 random bytes, valid 60 s, bound to that user, only its SHA-256 kept.
   Unanswered requests expire after 2 minutes.
3. `GET /ws/console?ticket=…` (WebSocket) consumes the ticket — a second use, an expired ticket, a
   ticket of another user (when the request carries a dashboard cookie), a foreign Origin, or
   WEB_CONSOLE off are refused; failed tickets count toward the per-IP auth limit.

The terminal itself: `pty.fork()` in a new session (so a new process group); on the main server
the user's login shell in WORKDIR, on another server `ssh -tt -o BatchMode=yes -- <alias>` with the
alias from servers.json (validated again). The argv is fixed — no shell string ever contains data
from the client — and the environment is a short clean list (the bot's own environment is not
passed on). Messages: text frames {"t": "in", "d": "..."} are keystrokes, {"t": "resize", "cols",
"rows"} sets the window size (TIOCSWINSZ); output goes out as binary frames (1 MB/s throttle).

A lost connection does not end a console (see Session): it waits CONSOLE_KEEP minutes for its user,
who comes back with the resume token + a signed-in call → a one-time ticket, and gets the kept output.
Limits: 30 minutes with no input and no output, 8 hours in all, at most 3 consoles at once. When a
console ends (the × button, nobody came back, a limit, the bot stops) every process of its session
gets SIGHUP, then SIGKILL after 3 s, and the shell is reaped. The audit log gets the start (server,
user) and the end (duration, reason); what was typed or shown is never stored. The user is told
in Telegram when a console opens and when it closes.
"""
import atexit
import errno
import fcntl
import hashlib
import json
import os
import pty
import pwd
import re
import secrets
import select
import shlex
import signal
import socket
import struct
import termios
import threading
import time
import urllib.parse
import warnings

from vps_director import remote, web, webauth, ws
from vps_director.engines import mask_secrets

WAIT_TTL = 120          # an unanswered Allow / No request expires after this
TICKET_TTL = 60         # an issued ticket must be used within this
IDLE = 30 * 60          # seconds with no input AND no output (a build that prints keeps it alive)
HARD = 8 * 3600         # seconds in all
KEEP = 30 * 60          # a console with nobody connected waits this long for its user to come back (CONSOLE_KEEP)
BUFFER = 256 * 1024     # the newest output kept for the replay after a reconnect
MAX_CONSOLES = 3
MAX_WAITING = 5
KILL_GRACE = 3          # SIGHUP, then SIGKILL after this many seconds
PING_EVERY = 30         # a ping when nothing was sent for this long (keeps proxies from dropping us)
READ_CHUNK = 16 * 1024
ID = re.compile(r"[0-9a-f]{8}")
SSH_OPTS = ("-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=30")
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSH_AUTH_SOCK")
MAX_EXECUTABLES = 5000  # names returned by /api/console/commands
HISTORY_LINES = 500     # /api/console/history: the newest distinct lines
HISTORY_READ = 256 * 1024
EXECUTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
CLOSE_CODES = {"idle": 4000, "time limit": 4001, "exited": 1000, "closed": 1000, "disconnected": 1001,
               "bot stopped": 1001, "detached": 4003}
# CONSOLE_TMUX=1: the shell runs inside tmux (screen when there is no tmux, a plain shell when neither), so
# what runs there survives even the bot's restart and a broken ssh link; the next console attaches to it.
# A fixed string: the session name is built from digits only (the user's Telegram id).
MUX = ('if command -v tmux >/dev/null 2>&1; then exec tmux new-session -A -s {name}; '
       'elif command -v screen >/dev/null 2>&1; then exec screen -D -R -S {name}; '
       'else exec "${{SHELL:-/bin/sh}}" -l; fi')


def digest(ticket):
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def login_shell():
    """The user's login shell from the password database, /bin/sh when it is missing or not executable."""
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    return shell if shell and os.path.isabs(shell) and os.access(shell, os.X_OK) else "/bin/sh"


def shell_argv(server, shell=None, mux=None):
    """The fixed argv of a console on `server` (an entry from servers.json). Raises ValueError for an
    alias that could be read as an ssh option. `mux` (digits only): run inside tmux / screen session
    "vpsd-<mux>" (CONSOLE_TMUX=1)."""
    alias = server.get("ssh") or ""
    script = MUX.format(name="vpsd-%s" % mux) if mux and str(mux).isdigit() else None
    if not alias:
        sh = shell or login_shell()
        return [sh, "-lc", script] if script else [sh, "-l"]
    if not remote.valid_alias(alias):
        raise ValueError("bad ssh alias")
    return ["ssh", *SSH_OPTS, "--", alias] + ([script] if script else [])


def clean_env(shell):
    try:
        user = pwd.getpwuid(os.getuid())
        home, name = user.pw_dir, user.pw_name
    except KeyError:
        home, name = os.path.expanduser("~"), str(os.getuid())
    env = {k: os.environ[k] for k in ENV_KEEP if os.environ.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin")
    env.update(HOME=home, USER=name, LOGNAME=name, SHELL=shell, TERM="xterm-256color")
    return env


def path_executables(path):
    """Sorted names of the executables found in a PATH string (for the assistant's typo check)."""
    names = set()
    for folder in (path or "").split(os.pathsep):
        if not os.path.isabs(folder):
            continue
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for name in entries:
            if len(names) >= MAX_EXECUTABLES:
                break
            full = os.path.join(folder, name)
            if EXECUTABLE.fullmatch(name) and os.path.isfile(full) and os.access(full, os.X_OK):
                names.add(name)
    return sorted(names)


# Read-only probes for the console assistant on another server (run through remote.run over ssh):
# executable names in the login PATH, and the tail of ~/.bash_history. Constant scripts, no input.
REMOTE_EXECUTABLES = ('IFS=:; for d in $PATH; do [ -d "$d" ] && for f in "$d"/*; do '
                      '[ -f "$f" ] && [ -x "$f" ] && echo "${f##*/}"; done; done 2>/dev/null | head -n 20000')
REMOTE_HISTORY = "tail -c %d \"$HOME/.bash_history\" 2>/dev/null" % HISTORY_READ


def executable_names(lines):
    """Valid, distinct executable names from probe output, sorted, at most MAX_EXECUTABLES."""
    return sorted({n for n in (line.strip() for line in lines) if EXECUTABLE.fullmatch(n)})[:MAX_EXECUTABLES]


def history_lines(path, limit=HISTORY_LINES):
    """The newest `limit` distinct commands of a bash history file, oldest first. Timestamp lines,
    control characters, very long lines and anything that looks like it holds a secret are dropped."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - HISTORY_READ))
            raw = fh.read(HISTORY_READ)
    except OSError:
        return []
    return history_from_text(raw.decode("utf-8", "replace"), size > HISTORY_READ, limit)


def history_from_text(text, cut=True, limit=HISTORY_LINES):
    lines = text.splitlines()
    if cut and lines:
        lines = lines[1:]  # the first line was probably cut in half
    out, seen = [], set()
    for line in reversed(lines):
        line = line.strip()
        if (not line or len(line) > 500 or re.fullmatch(r"#\d+", line) or re.search(r"[\x00-\x1f\x7f]", line)
                or mask_secrets(line) != line or line in seen):
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= limit:
            break
    return out[::-1]


def session_members(sid):
    """Live (not zombie) processes whose session or process group is `sid`."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "rb") as fh:
                stat = fh.read().decode("ascii", "replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) > 3 and fields[0] != "Z" and sid in (int(fields[2]), int(fields[3])):
            found.append(int(entry))
    return found


class Pty:
    """A process in a new session with a pseudo-terminal."""

    def __init__(self, argv, cwd, env, cols=80, rows=24):
        self.argv, self.cwd, self.env = list(argv), cwd, dict(env)
        self.pid, self.fd = None, None
        self.cols, self.rows = cols, rows
        self.status = None

    def start(self):
        argv, cwd, env = self.argv, self.cwd, self.env
        with warnings.catch_warnings():  # the child only chdir()s and exec()s
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:  # child: a new session leader with the pty as its controlling terminal
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.resize(self.cols, self.rows)
        return self

    def resize(self, cols, rows):
        cols, rows = max(1, min(int(cols), 1000)), max(1, min(int(rows), 1000))
        self.cols, self.rows = cols, rows
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    def write(self, data):
        while data and self.fd is not None:
            try:
                n = os.write(self.fd, data)
            except InterruptedError:
                continue
            except OSError:
                return
            data = data[n:]

    def read(self, timeout):
        """Output bytes; b"" when the terminal is gone; None when nothing came within `timeout`."""
        fd = self.fd
        if fd is None:
            return b""
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return None
        try:
            return os.read(fd, READ_CHUNK)
        except OSError as exc:  # EIO: the other side of the pty is closed
            return None if exc.errno in (errno.EAGAIN, errno.EINTR) else b""

    def signal_all(self, sig):
        for pid in set(session_members(self.pid)) | {self.pid}:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        try:
            os.killpg(self.pid, sig)
        except OSError:
            pass

    def alive(self):
        """True while any process of the session runs. The shell is not reaped here (WNOWAIT): a
        zombie leader keeps its pid — and so the session id — from being reused while we look."""
        if self.status is not None:
            return bool(session_members(self.pid))
        try:
            leader = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
        except ChildProcessError:
            leader = False
        return leader or bool(session_members(self.pid))

    def kill(self, grace=KILL_GRACE):
        """SIGHUP to the whole session, SIGKILL after `grace` seconds, then reap the shell."""
        if self.pid is None or self.status is not None:
            return
        self.signal_all(signal.SIGHUP)
        self.signal_all(signal.SIGCONT)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and self.alive():
            time.sleep(0.05)
        if self.alive():
            self.signal_all(signal.SIGKILL)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and session_members(self.pid):
                time.sleep(0.02)
        try:
            self.status = os.waitpid(self.pid, 0)[1]
        except ChildProcessError:
            self.status = 0

    def close(self):
        """Close the master side (after the output pump has stopped reading it)."""
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def duration_text(seconds, lang):
    seconds = int(seconds)
    if seconds < 60:
        return ("%d с" if lang == "ru" else "%d s") % seconds
    if seconds < 3600:
        return ("%d мин" if lang == "ru" else "%d min") % (seconds // 60)
    return ("%d ч %d мин" if lang == "ru" else "%d h %d min") % (seconds // 3600, seconds // 60 % 60)


class Session:
    """One console: the pty, its output buffer and (while someone looks) a WebSocket.

    Like VS Code's remote terminals the session does not depend on the connection: when the phone
    loses the network or Telegram is put away, the shell keeps running, its output collects in a
    buffer (the newest BUFFER bytes), and the same user can come back within KEEP seconds — the
    buffer is replayed and typing goes on where it was. A resume needs the session's resume token
    (given only over its own WebSocket, rotated on every attach) AND a signed-in API call, which
    turns it into a one-time ticket — so neither a leaked token nor a stolen session alone opens it."""

    def __init__(self, owner, cid, server, uid, proc):
        self.owner, self.cid, self.server, self.uid, self.proc = owner, cid, server, uid, proc
        self.started = self.last_input = self.last_sent = self.last_output = time.monotonic()
        self.conn = None
        self.detached = time.monotonic()  # not attached until the first WebSocket arrives
        self.buffer = bytearray()
        self.total = 0  # bytes of output so far (the buffer keeps only the newest)
        self.resume_hash = None
        self.reason = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.pump_thread = None

    # -- attach / detach --

    def run(self, conn):
        """Serve one WebSocket (the HTTP handler's thread). The session outlives it unless the user ends it."""
        with self.lock:
            if self.reason is not None:  # stopped between the ticket check and the 101 reply
                conn.close(CLOSE_CODES.get(self.reason, 1000), self.reason)
                return
            old, self.conn, self.detached = self.conn, None, None
            token = secrets.token_urlsafe(32)
            self.resume_hash = digest(token)
            replay, mark = bytes(self.buffer), self.total
        if old is not None:  # the same user took the console over from another tab / an older connection
            old.close(4002, "taken over")
        conn.send_text(json.dumps({"t": "session", "id": self.cid, "resume": token, "keep": self.owner.keep}))
        if replay:
            conn.send_text(json.dumps({"t": "replay"}))
            conn.send_binary(replay)
        with self.lock:  # what came while the replay was going out — sent before the pump can send newer output
            stopped = self.reason
            if stopped is None:
                missed = self.total - mark
                if 0 < missed <= len(self.buffer):
                    conn.send_binary(bytes(self.buffer[-missed:]))
                self.conn = conn
        if stopped is not None:  # ended while the replay was going out
            conn.close(CLOSE_CODES.get(stopped, 1000), stopped)
            return
        if self.pump_thread is None:
            self.pump_thread = threading.Thread(target=self.pump, daemon=True, name="console-out-%s" % self.cid)
            self.pump_thread.start()
        ended = False
        try:
            while self.reason is None:
                msg = conn.recv()
                if msg is None:
                    break
                if self.message(msg) == "close":
                    ended = True
                    break
        finally:
            if ended:
                self.stop("closed")
            elif self.reason is not None:  # stopped elsewhere (limit, bot stop): the close frame goes out here too
                conn.close(CLOSE_CODES.get(self.reason, 1000), self.reason)
            else:
                self.detach(conn)

    def detach(self, conn):
        with self.lock:
            if self.conn is not conn:
                return  # a newer connection took over
            self.conn, self.detached = None, time.monotonic()

    def message(self, msg):
        kind, data = msg
        if kind != ws.TEXT:
            return None
        try:
            obj = json.loads(data)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("t") == "in" and isinstance(obj.get("d"), str):
            self.last_input = time.monotonic()
            self.proc.write(obj["d"].encode("utf-8", "replace"))
        elif obj.get("t") == "resize":
            cols, rows = obj.get("cols"), obj.get("rows")
            if isinstance(cols, int) and isinstance(rows, int) and not isinstance(cols, bool) \
                    and not isinstance(rows, bool):
                self.proc.resize(cols, rows)
        elif obj.get("t") == "close":  # the user ended the console (the × button), not a lost connection
            return "close"
        return None

    def remember(self, out):
        with self.lock:
            self.buffer += out
            self.total += len(out)
            if len(self.buffer) > self.owner.buffer_max:
                del self.buffer[:len(self.buffer) - self.owner.buffer_max]
            return self.conn

    def pump(self):
        """The output loop for the whole life of the session, attached or not; also enforces the limits."""
        while self.reason is None:
            out = self.proc.read(min(1.0, self.owner.idle / 4, self.owner.keep / 4))
            now = time.monotonic()
            if out == b"":
                self.stop("exited")
                return
            if out:
                self.last_output = now
                conn = self.remember(out)
                if conn is not None:
                    if conn.send_binary(out):
                        self.last_sent = now
                    else:
                        self.detach(conn)
            else:
                with self.lock:
                    conn = self.conn
                if conn is not None and now - self.last_sent >= PING_EVERY:
                    conn.ping()
                    self.last_sent = now
            with self.lock:
                detached = self.detached
            if detached is not None and now - detached >= self.owner.keep:
                self.stop("detached")
            elif now - max(self.last_input, self.last_output) >= self.owner.idle:
                self.stop("idle")  # nobody typed and nothing printed: a long build keeps it alive
            elif now - self.started >= self.owner.hard:
                self.stop("time limit")

    def info(self):
        now = time.monotonic()
        with self.lock:
            detached = self.detached
        return {"id": self.cid, "server": self.server, "attached": detached is None,
                "age": int(now - self.started), "away": int(now - detached) if detached is not None else 0,
                "left": max(0, int(self.owner.keep - (now - detached))) if detached is not None else None}

    def stop(self, reason):
        """End the console once: kill the session's processes, close the WebSocket, tell the owner."""
        with self.lock:
            if self.reason is not None:
                return
            self.reason = reason
        self.proc.kill(self.owner.grace)
        with self.lock:
            conn = self.conn
        if conn is not None:
            conn.close(CLOSE_CODES.get(reason, 1000), reason)
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)  # wakes up the reader thread
                except OSError:
                    pass
        if self.pump_thread is not None and self.pump_thread is not threading.current_thread():
            self.pump_thread.join(self.owner.grace + 5)
        self.proc.close()
        self.done.set()
        self.owner.ended(self, time.monotonic() - self.started)


class Consoles:
    """Requests, Allow / No, tickets and the open consoles. `d` is the Director (cfg, tg, audit,
    inventory(), web_auth())."""

    def __init__(self, director, clock=time.time):
        self.d, self.clock = director, clock
        self.lock = threading.Lock()
        self.waiting = {}   # request id → {server, uid, created, state, ticket}
        self.tickets = {}   # sha256(ticket) → {uid, server, expires, rid}
        self.active = {}    # console id → Session
        self.reserved = 0   # consoles being started
        self.idle, self.hard, self.grace = IDLE, HARD, KILL_GRACE
        self.keep = max(60, int(getattr(director.cfg, "console_keep", KEEP) or KEEP))
        self.buffer_max = BUFFER
        self.argv_for = shell_argv
        self.executables_by = {}  # ssh alias ( = here) → executable names, on the first /api/console/commands
        atexit.register(self.close_all)

    @property
    def enabled(self):
        return bool(getattr(self.d.cfg, "web_console", False))

    def refuse_off(self):
        return web.error(403, "the web console is off: the bot owner can turn it on with WEB_CONSOLE=1 in .env "
                              "(and a restart); every console still needs Allow in Telegram")

    # -- read-only helpers for the command assistant --

    def target(self, name):
        """The ssh alias of the console's server ("" = this machine), or None for an unknown server.
        The assistant must suggest what exists THERE, not on the machine the bot runs on."""
        if not name:
            return ""
        server = self.find_server(name)
        return None if server is None else (server.get("ssh") or "")

    def commands(self, server=""):
        """GET /api/console/commands[?server=]: executable names in that server's PATH (cached)."""
        if not self.enabled:
            return self.refuse_off()
        alias = self.target(server)
        if alias is None:
            return web.error(404, "no such server")
        if alias not in self.executables_by:
            if alias:
                out = remote.run(alias, "bash -lc " + shlex.quote(REMOTE_EXECUTABLES))
                names = executable_names((out or "").splitlines())
                if out is None:
                    return web.Response.json(200, {"ok": True, "commands": []})  # unreachable: no cache
            else:
                names = path_executables(clean_env(login_shell())["PATH"])
            self.executables_by[alias] = names
        return web.Response.json(200, {"ok": True, "commands": self.executables_by[alias]})

    def history(self, server=""):
        """GET /api/console/history[?server=]: the newest distinct lines of that server's ~/.bash_history."""
        if not self.enabled:
            return self.refuse_off()
        alias = self.target(server)
        if alias is None:
            return web.error(404, "no such server")
        if alias:
            return web.Response.json(200, {"ok": True, "history": history_from_text(remote.run(alias, REMOTE_HISTORY) or "")})
        home = clean_env(login_shell())["HOME"]
        return web.Response.json(200, {"ok": True, "history": history_lines(os.path.join(home, ".bash_history"))})

    def find_server(self, name):
        if not isinstance(name, str):
            return None
        return next((s for s in self.d.inventory()[0] if s["name"] == name), None)

    def prune(self, now):
        self.waiting = {k: v for k, v in self.waiting.items() if now - v["created"] < 3600}
        self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > now}

    # -- step 1: the request --

    def request(self, data, who):
        if not self.enabled:
            return self.refuse_off()
        server = self.find_server(data.get("server"))
        if server is None:
            return web.error(404, "no such server")
        now = self.clock()
        with self.lock:
            self.prune(now)
            if len(self.active) + self.reserved >= MAX_CONSOLES:
                return web.error(429, "%d consoles are open already — close one first" % MAX_CONSOLES)
            waiting = [v for v in self.waiting.values() if v["state"] == "pending" and now - v["created"] < WAIT_TTL]
            if len(waiting) >= MAX_WAITING:
                return web.error(429, "too many requests waiting for an answer in Telegram")
            rid = secrets.token_hex(4)
            while rid in self.waiting:
                rid = secrets.token_hex(4)
            self.waiting[rid] = {"server": server["name"], "uid": str(who["uid"]), "created": now, "state": "pending"}
        self.d.audit.write(who["uid"], "console", "console on %s requested (#%s)" % (server["name"], rid))
        t = self.d.cfg.t
        if getattr(self.d.cfg, "web_console_auto", False) and who.get("via") == "telegram":
            # the owner chose this in the settings: only inside Telegram (initData / its session), never via a link
            self.answer(who["uid"], who["uid"], rid, True)
            self.d.spawn(self.d.tg.send, who["uid"], t("console_auto", server=server["name"]))
            return web.Response.json(202, {"ok": True, "pending": rid, "expires_in": WAIT_TTL})
        self.d.tg.send(who["uid"], t("console_ask", server=server["name"]),
                       buttons=[[{"text": t("web_allow"), "callback_data": "conok:" + rid},
                                 {"text": t("web_deny"), "callback_data": "conno:" + rid}]])
        return web.Response.json(202, {"ok": True, "pending": rid, "expires_in": WAIT_TTL})

    # -- consoles that keep running without a connection: list, come back, end --

    def mine(self, who):
        with self.lock:
            return [s for s in self.active.values() if s.uid == str(who["uid"]) and s.reason is None]

    def sessions(self, who):
        """GET /api/console/sessions: the caller's consoles (attached or waiting for them)."""
        if not self.enabled:
            return self.refuse_off()
        return web.Response.json(200, {"ok": True, "keep": self.keep, "sessions": [s.info() for s in self.mine(who)]})

    def resume(self, cid, data, who):
        """POST /api/console/<id>/resume {"token"}: the resume token from the session's own WebSocket plus
        this signed-in call → a one-time ticket for /ws/console (no new Allow)."""
        if not self.enabled:
            return self.refuse_off()
        token = data.get("token") if isinstance(data, dict) else None
        session = next((s for s in self.mine(who) if s.cid == cid), None)
        if session is None:
            return web.error(404, "no such console (it ended)")
        if not isinstance(token, str) or not token or session.resume_hash is None \
                or not secrets.compare_digest(digest(token), session.resume_hash):
            return web.error(403, "bad resume token")
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets[digest(ticket)] = {"uid": session.uid, "server": session.server,
                                            "expires": self.clock() + TICKET_TTL, "rid": None, "resume": cid}
        return web.Response.json(200, {"ok": True, "ticket": ticket, "ws": "/ws/console?ticket=" + ticket,
                                       "server": session.server})

    def end(self, cid, who):
        """DELETE /api/console/<id>: end one of the caller's consoles."""
        if not self.enabled:
            return self.refuse_off()
        session = next((s for s in self.mine(who) if s.cid == cid), None)
        if session is None:
            return web.error(404, "no such console")
        threading.Thread(target=session.stop, args=("closed",), daemon=True).start()
        return web.Response.json(200, {"ok": True, "ended": cid})

    # -- step 2: Allow / No in Telegram, polling --

    def answer(self, chat, sender, rid, allow):
        """The user pressed Allow / No under a console request (Director.callback). Returns how it went
        ("console" / "denied" / "stale"); the caller shows it in the request message itself."""
        t = self.d.cfg.t
        now = self.clock()
        with self.lock:
            entry = self.waiting.get(rid) if isinstance(rid, str) else None
            fresh = (entry is not None and entry["state"] == "pending" and now - entry["created"] < WAIT_TTL
                     and str(sender) == entry["uid"])
            if fresh:
                entry["state"] = "allowed" if allow else "denied"
                if allow:
                    ticket = secrets.token_urlsafe(32)
                    entry["ticket"] = ticket
                    self.tickets[digest(ticket)] = {"uid": entry["uid"], "server": entry["server"],
                                                    "expires": now + TICKET_TTL, "rid": rid}
        if not fresh:
            return "stale"
        self.d.audit.write(sender, "allow" if allow else "deny", "console on %s (#%s)" % (entry["server"], rid))
        return "console" if allow else "denied"

    def status(self, rid, who):
        now = self.clock()
        with self.lock:
            entry = self.waiting.get(rid) if ID.fullmatch(rid or "") else None
            if entry is None or entry["uid"] != str(who["uid"]):
                return web.error(404, "no such request")
            state, out = entry["state"], {}
            if state == "pending" and now - entry["created"] >= WAIT_TTL:
                state = "expired"
            elif state == "allowed":
                ticket = entry.get("ticket")
                live = ticket and self.tickets.get(digest(ticket))
                if live and live["expires"] > now:
                    out = {"ticket": ticket, "ws": "/ws/console?ticket=" + ticket,
                           "ticket_expires_in": int(live["expires"] - now)}
                else:
                    state = "opened" if entry.get("used") else "expired"
        return web.Response.json(200, dict(ok=True, id=rid, state=state, server=entry["server"], **out))

    # -- step 3: the WebSocket --

    def socket(self, query, headers, ip):
        """Route for /ws/console: a refusal Response or the session's runner."""
        if not self.enabled:
            return self.refuse_off()
        auth = self.d.web_auth()
        if auth.per_ip.blocked(ip) or auth.overall.blocked("*"):
            return web.error(429, "too many failed attempts, wait a minute")
        want = auth.expected_origin(headers)
        origin = (headers.get("Origin") or "").lower().rstrip("/")
        ticket = urllib.parse.parse_qs(query).get("ticket", [""])[0]
        if not origin or not want or origin not in (want if isinstance(want, tuple) else (want,)):
            return self.failed(auth, ip, "bad origin")
        cookie_uid = auth.session_user(webauth.cookie_value(headers.get("Cookie"), auth.cookie))
        now = self.clock()
        with self.lock:
            self.prune(now)
            entry = self.tickets.pop(digest(ticket), None) if ticket else None  # burnt on any use
            resumed = None
            if entry is None or entry["expires"] <= now or (cookie_uid and str(cookie_uid) != entry["uid"]):
                entry = None
            elif entry.get("resume"):  # back into a console that kept running (no new Allow: it was allowed)
                resumed = self.active.get(entry["resume"])
                if resumed is None or resumed.reason is not None or resumed.uid != entry["uid"]:
                    entry = None
            elif len(self.active) + self.reserved >= MAX_CONSOLES:
                return web.error(429, "%d consoles are open already" % MAX_CONSOLES)
            else:
                self.reserved += 1
                if entry["rid"] in self.waiting:
                    self.waiting[entry["rid"]]["used"] = True
        if entry is None:
            return self.failed(auth, ip, "bad or used ticket")
        if resumed is not None:
            self.d.audit.write(entry["uid"], "console", "console on %s resumed (#%s)" % (resumed.server, resumed.cid))
            return resumed.run
        try:
            return self.open(entry)
        finally:
            with self.lock:
                self.reserved -= 1

    def failed(self, auth, ip, why):
        auth.per_ip.hit(ip)
        auth.overall.hit("*")
        return web.error(403, why)

    def open(self, entry):
        server = self.find_server(entry["server"])
        if server is None:
            return web.error(404, "no such server")
        try:
            argv = self.argv_for(server, mux=entry["uid"]) if getattr(self.d.cfg, "console_tmux", False) \
                else self.argv_for(server)
        except ValueError:
            return web.error(400, "bad ssh alias in servers.json")
        try:
            proc = Pty(argv, self.d.cfg.workdir, clean_env(login_shell())).start()
        except OSError as exc:
            self.d.log("console on %s: %r" % (server["name"], exc))
            return web.error(500, "could not start the terminal")
        cid = secrets.token_hex(4)
        session = Session(self, cid, server["name"], entry["uid"], proc)
        with self.lock:
            self.active[cid] = session
        self.d.audit.write(entry["uid"], "console", "console on %s opened by %s (#%s)" % (server["name"], entry["uid"], cid))
        # in the background: the 101 reply must not wait for Telegram (a slow API left the browser on "Connecting…")
        self.d.spawn(self.d.tg.send, entry["uid"], self.d.cfg.t("console_opened", server=server["name"]))
        return session.run

    def ended(self, session, seconds):
        with self.lock:
            self.active.pop(session.cid, None)
        self.d.audit.write(session.uid, "console", "console on %s closed after %d s: %s (#%s)"
                           % (session.server, seconds, session.reason, session.cid))
        t, lang = self.d.cfg.t, self.d.cfg.lang
        try:
            self.d.tg.send(session.uid, t("console_closed", server=session.server, time=duration_text(seconds, lang),
                                          why=t("console_why_" + session.reason.replace(" ", "_"))))
        except Exception as exc:  # noqa: BLE001 — the processes are gone already; a failed message is only logged
            self.d.log("console closed message: %r" % exc)

    def close_all(self, reason="bot stopped"):
        with self.lock:
            sessions = list(self.active.values())
        for session in sessions:
            session.stop(reason)
