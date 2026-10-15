"""The Mini App / dashboard web server: static files from web/ and a JSON API under /api/.

Off unless WEB_PORT is set. Binds 127.0.0.1 only (WEB_BIND can change that, which is dangerous:
the server is meant to sit behind the owner's HTTPS reverse proxy or tunnel, which is where
WEB_URL points). Standard library only: http.server.ThreadingHTTPServer.

Every request goes through WebApp.handle(), a plain function of (method, path, headers, body,
client ip) → Response, so the whole thing is testable without sockets. What it guarantees:
- static files: only regular files inside web/ with a known extension (no "..", no dot files,
  no symlinks out of the folder, no directory listing);
- every /api/ call is authenticated (see webauth.py) before anything else is looked at, apart
  from the rate limit; state-changing calls also pass the CSRF checks;
- security headers on every reply: CSP (self + telegram.org for the WebApp script; no inline
  scripts or styles, no CDN), frame-ancestors limited to Telegram, Referrer-Policy no-referrer, nosniff, and
  Cache-Control no-store on the API and the login.
"""
import gzip
import hashlib
import http.server
import ipaddress
import json
import os
import threading
import time
import urllib.parse

from vps_director import ws

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
FRAME_ANCESTORS = "https://web.telegram.org https://*.telegram.org"


def csp(connect=""):
    """The Content-Security-Policy. Scripts and styles come from the app itself (xterm.js is vendored
    under web/vendor/) plus Telegram's WebApp script; `connect` adds the wss:// origin of WEB_URL for
    the console (CSP 'self' does not cover WebSockets in every WebView)."""
    return ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'%s; font-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors %s" % ((" " + connect) if connect else "", FRAME_ANCESTORS))


CSP = csp()
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".webp": "image/webp", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
        ".webmanifest": "application/manifest+json"}


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def valid_bind(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def valid_url(url):
    """WEB_URL: https://host[:port] (the dashboard lives at the root of its host); plain http only for a
    loopback host (local testing)."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 — raises on a bad port
    except ValueError:
        return False
    if not parts.hostname or parts.path.strip("/") or parts.query or parts.fragment or parts.username or parts.password:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and loopback(parts.hostname))


def origin_of(url):
    """scheme://host[:port] of a URL, lower-case (what browsers send as Origin)."""
    parts = urllib.parse.urlsplit(url)
    return "%s://%s" % (parts.scheme.lower(), parts.netloc.lower())


class Response:
    def __init__(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.status, self.body, self.content_type = status, body, content_type
        self.headers = list(headers or [])

    @classmethod
    def json(cls, status, data, headers=None):
        return cls(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), headers=headers)

    def header(self, name):
        """The first header value with this name (tests), or None."""
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    @property
    def data(self):
        return json.loads(self.body.decode("utf-8"))


def error(status, text, headers=None):
    return Response.json(status, {"ok": False, "error": text}, headers)


class RateLimit:
    """At most `limit` hits per `window` seconds per key; at most `keys` keys are tracked (the oldest go)."""

    def __init__(self, limit, window=60.0, clock=time.monotonic, keys=10000):
        self.limit, self.window, self.clock, self.keys = limit, window, clock, keys
        self.lock = threading.Lock()
        self.hits = {}

    def hit(self, key):
        """Count one hit; False when the key is over the limit (the hit is not counted then)."""
        now = self.clock()
        with self.lock:
            recent = [t for t in self.hits.get(key, ()) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits.pop(key, None)
            self.hits[key] = recent  # re-inserted: dict order = least recently hit first
            while len(self.hits) > self.keys:
                self.hits.pop(next(iter(self.hits)))
            return True

    def blocked(self, key):
        now = self.clock()
        with self.lock:
            return sum(1 for t in self.hits.get(key, ()) if now - t < self.window) >= self.limit


def client_ip(peer, headers):
    """The client's address. Behind a local reverse proxy (peer on loopback) the proxy's own
    X-Forwarded-For entry — the last one — is used; a remote peer's header is never trusted."""
    try:
        loop = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loop = False
    forwarded = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    if loop and forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def static_path(root, url_path):
    """The real path of a file under `root` for a URL path, or None (traversal, dot files, unknown type)."""
    path = urllib.parse.unquote(url_path)
    if "\x00" in path or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        parts = ["index.html"]
    if any(p in (".", "..") or p.startswith(".") for p in parts):
        return None
    if os.path.splitext(parts[-1])[1].lower() not in MIME:
        return None
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, *parts))
    if not real.startswith(real_root + os.sep) or not os.path.isfile(real):
        return None
    return real


def security_headers(api, policy=CSP):
    out = [("Content-Security-Policy", policy), ("Referrer-Policy", "no-referrer"),
           ("X-Content-Type-Options", "nosniff"), ("Cross-Origin-Opener-Policy", "same-origin"),
           ("Permissions-Policy", "camera=(), microphone=(), geolocation=()")]
    out.append(("Cache-Control", "no-store" if api else "no-cache"))
    return out


class WebApp:
    """Routing. `auth` (webauth.Auth) answers who is calling; `api` (an object with route methods,
    see api_routes) does the work. Without them every /api/ call is refused."""

    def __init__(self, root=WEB_DIR, auth=None, api=None, log=lambda m: None, sockets=None, web_url=""):
        self.root, self.auth, self.api, self.log = root, auth, api, log
        url = web_url if web_url and valid_url(web_url) else ""
        self.csp = csp("%s://%s" % ("wss" if url.startswith("https:") else "ws", urllib.parse.urlsplit(url).netloc.lower())
                       if url else "")
        self.sockets = sockets or {}
        self.files = {}  # (path, mtime, size) → (bytes, gzip bytes, ETag, type): static files served from memory  # WebSocket routes: path → fn(query, headers, ip) → Response | runner(conn)

    def websocket(self, path, headers, ip):
        """A WebSocket upgrade request: a refusal Response, or (Sec-WebSocket-Accept, runner) where
        runner(ws.Connection) serves the connection after the 101 reply."""
        parsed = urllib.parse.urlsplit(path)
        route = self.sockets.get(parsed.path)
        if route is None:
            res = error(404, "not found")
        else:
            accept = ws.handshake(headers)
            if accept is None:
                res = error(400, "WebSocket upgrade required")
            else:
                try:
                    res = route(parsed.query, headers, ip)
                except Exception as exc:  # noqa: BLE001
                    self.log("web ws %s: %r" % (parsed.path[:80], exc))
                    res = error(500, "internal error")
                if not isinstance(res, Response):
                    return accept, res
        res.headers = security_headers(True, self.csp) + res.headers
        return res

    def handle(self, method, path, headers, body=b"", ip="127.0.0.1"):
        try:
            res = self.route(method, path, headers, body, ip)
        except Exception as exc:  # noqa: BLE001 — never a traceback (or anything in it) to the client
            self.log("web %s %s: %r" % (method, path.split("?", 1)[0][:80], exc))
            res = error(500, "internal error")
        api = path.startswith(("/api/", "/login", "/logout"))
        if api and res.status >= 400 and res.status != 500:
            # refusals are logged (path without the query, and the reason) so "the button did nothing" can be traced
            try:
                why = json.loads(res.body.decode("utf-8")).get("error", "")
            except ValueError:
                why = ""
            self.log("web %s %s → %d %s" % (method, path.split("?", 1)[0][:80], res.status, str(why)[:120]))
        res.headers = security_headers(api, self.csp) + res.headers
        return res

    def route(self, method, path, headers, body, ip):
        parsed = urllib.parse.urlsplit(path)
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            if self.auth is None or self.api is None:
                return error(401, "unauthorized")
            return self.api_call(method, parsed, headers, body, ip)
        if parsed.path == "/login":
            if self.auth is None or method != "GET":
                return error(404 if self.auth is None else 405, "not found" if self.auth is None else "GET only")
            return self.auth.login(urllib.parse.parse_qs(parsed.query).get("t", [""])[0], ip, headers)
        if method not in ("GET", "HEAD"):
            return error(405, "GET only")
        real = static_path(self.root, parsed.path)
        if real is None:
            return Response(404, b"not found\n", "text/plain; charset=utf-8")
        return self.static(real, headers)

    def static(self, real, headers):
        """A file from web/ with an ETag (a repeat visit gets 304, no body) and gzip for text types; both are
        computed once per file version and kept in memory."""
        try:
            stat = os.stat(real)
        except OSError:
            return Response(404, b"not found\n", "text/plain; charset=utf-8")
        key = (real, stat.st_mtime_ns, stat.st_size)
        entry = self.files.get(key)
        if entry is None:
            with open(real, "rb") as fh:
                data = fh.read()
            ctype = MIME[os.path.splitext(real)[1].lower()]
            packed = gzip.compress(data, 6, mtime=0) if (ctype.startswith(("text/", "application/json", "image/svg"))
                                                          and len(data) > 1024) else None
            entry = (data, packed, '"%s"' % hashlib.sha256(data).hexdigest()[:20], ctype)
            if len(self.files) > 200:
                self.files.clear()
            self.files[key] = entry
        data, packed, etag, ctype = entry
        if etag in (headers.get("If-None-Match") or ""):
            return Response(304, b"", ctype, [("ETag", etag)])
        extra = [("ETag", etag), ("Vary", "Accept-Encoding")]
        if packed is not None and "gzip" in (headers.get("Accept-Encoding") or ""):
            return Response(200, packed, ctype, extra + [("Content-Encoding", "gzip")])
        return Response(200, data, ctype, extra)

    def api_call(self, method, parsed, headers, body, ip):
        who = self.auth.authenticate(headers, ip)
        if isinstance(who, Response):
            return who
        if method not in ("GET", "HEAD"):
            refused = self.auth.csrf(method, headers, body, who)
            if refused is not None:
                return refused
        if parsed.path == "/api/session":
            # the Mini App swaps its initData (fresh for 10 minutes) for a sliding session token
            if method != "POST":
                return error(405, "POST only")
            if (headers.get("X-Init-Data") or "") == "" or who.get("mini"):
                return error(403, "Telegram initData required")
            return Response.json(200, {"ok": True, "session": self.auth.new_mini(who["uid"])})
        if parsed.path == "/api/logout":
            if method != "POST":
                return error(405, "POST only")
            if who.get("sid"):
                self.auth.revoke(sid=who["sid"])
                self.auth.audit(who["uid"], "logout from the dashboard")
            return Response.json(200, {"ok": True}, [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict%s"
                                                      % (self.auth.cookie, "; Secure" if self.auth.secure else ""))])
        return self.api.call(method, parsed.path, parsed.query, body, who)


def handler_for(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 15
        server_version = "vps-director"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def respond(self, res, head=False):
            self.send_response(res.status)
            self.send_header("Content-Type", res.content_type)
            self.send_header("Content-Length", str(len(res.body)))
            for name, value in res.headers:
                self.send_header(name, value)
            self.end_headers()
            if not head:
                self.wfile.write(res.body)

        def peer(self):
            return client_ip(self.client_address[0], self.headers)

        def body_or_refuse(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self.respond(_with_headers(error(413, "body too large")))
                return None
            return self.rfile.read(length) if length else b""

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/ws/"):
                self.upgrade()
                return
            self.respond(app.handle("GET", self.path, self.headers, b"", self.peer()))

        def upgrade(self):
            self.close_connection = True
            res = app.websocket(self.path, self.headers, self.peer())
            if isinstance(res, Response):
                self.respond(res)
                return
            accept, runner = res
            self.wfile.write(ws.response_101(accept))
            self.connection.settimeout(None)  # the session has its own idle and hard limits
            runner(ws.Connection(self.rfile, self.wfile, sock=self.connection))

        def do_HEAD(self):  # noqa: N802
            self.respond(app.handle("HEAD", self.path, self.headers, b"", self.peer()), head=True)

        def with_body(self):
            body = self.body_or_refuse()
            if body is not None:
                self.respond(app.handle(self.command, self.path, self.headers, body, self.peer()))

        do_POST = do_DELETE = do_PUT = do_PATCH = with_body  # noqa: N815

        def log_message(self, fmt, *args):  # never logs URLs: the login link carries a token
            pass

    return Handler


def _with_headers(res):
    res.headers = security_headers(True) + res.headers
    return res


def serve(port, app, host=HOST):
    """Start the server in a daemon thread; returns it (server_address has the real port)."""
    server = http.server.ThreadingHTTPServer((host, port), handler_for(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="director-web").start()
    return server
