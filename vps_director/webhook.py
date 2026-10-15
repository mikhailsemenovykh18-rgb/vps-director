"""Inbound webhooks: a small HTTP listener on 127.0.0.1 (off unless WEBHOOK_PORT is set).

    POST /notify   the text goes to the owners as a message
    POST /task     the text becomes an agent task, run like a scheduled one: own session, safe mode,
                   no Allow buttons (only when WEBHOOK_TASKS=1)

Every request must carry the shared secret: `X-Director-Secret: <WEBHOOK_SECRET>` (or
`Authorization: Bearer <WEBHOOK_SECRET>`). The body is JSON with "text" (also "message" or "msg",
which is what Uptime Kuma sends) or plain text. Bodies over 64 KB are refused; at most 30 requests
a minute are accepted. The listener never binds to anything but loopback: put a reverse proxy or a
tunnel in front of it if something outside the server must reach it.
"""
import hmac
import http.server
import json
import threading
import time

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024
MAX_TEXT = 3500
RATE = 30          # requests per minute
SECRET_MIN = 16


class Webhook:
    def __init__(self, secret, notify, task, tasks_enabled=False, log=lambda m: None, clock=time.monotonic):
        self.secret = secret.encode("utf-8")
        self.notify, self.task, self.tasks_enabled, self.log, self.clock = notify, task, tasks_enabled, log, clock
        self.lock = threading.Lock()
        self.recent = []

    def authorized(self, headers):
        given = headers.get("X-Director-Secret") or ""
        auth = headers.get("Authorization") or ""
        if not given and auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return hmac.compare_digest(given.encode("utf-8", "replace"), self.secret)

    def allow_rate(self):
        now = self.clock()
        with self.lock:
            self.recent = [t for t in self.recent if now - t < 60]
            if len(self.recent) >= RATE:
                return False
            self.recent.append(now)
            return True

    @staticmethod
    def text_of(body, content_type):
        raw = body.decode("utf-8", "replace").strip()
        if "json" in (content_type or "").lower() or raw[:1] == "{":
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                for key in ("text", "message", "msg"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        return data[key].strip()[:MAX_TEXT]
                return ""
        return raw[:MAX_TEXT]

    def handle(self, method, path, headers, body):
        """(HTTP status, JSON reply) for one request. The secret is checked before anything else."""
        if method != "POST":
            return 405, {"ok": False, "error": "POST only"}
        if not self.authorized(headers):
            return 401, {"ok": False, "error": "bad secret"}
        path = path.split("?", 1)[0].rstrip("/")
        if path not in ("/notify", "/task"):
            return 404, {"ok": False, "error": "use /notify or /task"}
        if path == "/task" and not self.tasks_enabled:
            return 403, {"ok": False, "error": "tasks are off (WEBHOOK_TASKS=1 turns them on)"}
        if not self.allow_rate():
            return 429, {"ok": False, "error": "too many requests"}
        text = self.text_of(body, headers.get("Content-Type"))
        if not text:
            return 400, {"ok": False, "error": "empty text"}
        try:
            accepted = (self.notify if path == "/notify" else self.task)(text)
        except Exception as exc:  # noqa: BLE001
            self.log("webhook %s: %r" % (path, exc))
            return 500, {"ok": False, "error": "internal error"}
        if accepted is False:
            return 429, {"ok": False, "error": "busy, try later"}
        return 200, {"ok": True}


def handler_for(hook):
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 10
        server_version = "vps-director"
        sys_version = ""

        def reply(self, code, data):
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self.reply(413, {"ok": False, "error": "body too large"})
            body = self.rfile.read(length) if length else b""
            self.reply(*hook.handle("POST", self.path, self.headers, body))

        def do_GET(self):  # noqa: N802
            self.reply(*hook.handle("GET", self.path, self.headers, b""))

        do_PUT = do_DELETE = do_PATCH = do_GET  # noqa: N815

        def log_message(self, fmt, *args):  # requests are not worth a line each; errors are
            pass

    return Handler


def serve(port, hook, host=HOST):
    """Start the listener in a daemon thread; returns the server (server_address has the real port)."""
    server = http.server.ThreadingHTTPServer((host, port), handler_for(hook))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="director-webhook").start()
    return server
