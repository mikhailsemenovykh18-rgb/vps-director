"""Who is calling the web API: Telegram Mini App initData, or a dashboard session cookie.

Mini App: every API call carries Telegram.WebApp.initData in the `X-Init-Data` header. It is
checked as Telegram documents it — secret = HMAC_SHA256(key="WebAppData", msg=bot token), hash =
hex HMAC_SHA256(key=secret, msg=data-check-string of all other fields sorted by name) — with a
constant-time compare; `auth_date` at most 10 minutes old; `user.id` one of OWNER_IDS.

initData is only fresh for 10 minutes, so the Mini App swaps it once for a session: `POST /api/session`
(authenticated by initData) returns a random token for the `X-Vpsd-Session` header — bound to the
user, only its SHA-256 stored, sliding: one hour after the last call, at most 12 hours in all.
Without it the Mini App would stop working 10 minutes after it was opened.

Dashboard in a browser: `/dashboard` in the bot makes a one-time link — 32 random bytes, 10 minutes,
single use, only its SHA-256 is stored. `GET /login?t=<token>` swaps it for a session cookie
(HttpOnly, SameSite=Strict, Secure and the __Host- prefix when WEB_URL is https, 12 hours, bound to
the user id; again only the SHA-256 of the random id is stored) and redirects to "/".
`/logout_web` in the bot or `POST /api/logout` revoke it.

CSRF: a state-changing call needs a JSON object body (Content-Type application/json), the header
`X-Requested-With: vpsd`, and an Origin equal to WEB_URL's origin (or to the Host when WEB_URL is not
set). Failed authentication and /login are rate-limited per client IP (10 a minute) and overall.
Tokens and cookies are never logged and never put in a URL — apart from the login link itself.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse

from vps_director import web

FILE = "web-auth.json"
INIT_MAX_AGE = 600       # seconds an initData stays valid
INIT_MAX_LEN = 8192
LOGIN_TTL = 600          # one-time link
SESSION_TTL = 12 * 3600  # dashboard cookie
MINI_IDLE = 3600         # Mini App session: expires an hour after the last call…
MINI_MAX = 12 * 3600     # …and 12 hours after it was made, whatever happens
MINI_HEADER = "X-Vpsd-Session"
MAX_TOKENS = 20
MAX_SESSIONS = 50
PER_IP = 10              # failed authentications or /login hits per minute per IP
OVERALL = 100            # failed authentications per minute from all IPs together
CSRF_HEADER = "vpsd"
KINDS = ("tokens", "sessions", "mini")


def digest(secret):
    return hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()


def sign_init_data(fields, bot_token):
    """initData as Telegram signs it (tests and tools): fields → query string with `hash`."""
    check = "\n".join("%s=%s" % (k, fields[k]) for k in sorted(fields))
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(dict(fields, hash=sig))


def check_init_data(raw, bot_token, owners, now):
    """The owner's user id (str) when initData is genuine, fresh and from an owner; else None."""
    if not raw or len(raw) > INIT_MAX_LEN or not bot_token:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):  # a repeated field: refuse rather than guess which one was signed
        return None
    given = fields.pop("hash", "")
    if not given or not fields:
        return None
    check = "\n".join("%s=%s" % (k, fields[k]) for k in sorted(fields))
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want.encode(), given.encode("utf-8", "replace")):
        return None
    try:
        auth_date = int(fields.get("auth_date", ""))
        user = json.loads(fields.get("user", ""))
        uid = user["id"] if isinstance(user, dict) else None
    except (ValueError, KeyError):
        return None
    if not isinstance(uid, int) or isinstance(uid, bool) or not -60 <= now - auth_date <= INIT_MAX_AGE:
        return None
    return str(uid) if str(uid) in owners else None


def cookie_value(header, name):
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


class Auth:
    def __init__(self, bot_token, owners, conf_dir, web_url="", audit=lambda who, text: None,
                 clock=time.time, mono=time.monotonic):
        # a live container (team.Members) is kept as it is, so added / removed members count at once
        self.bot_token, self.owners = bot_token, owners if hasattr(owners, "__contains__") and not isinstance(owners, (list, tuple, str)) else set(owners)
        self.path = os.path.join(conf_dir, FILE)
        self.web_url, self.audit, self.clock = web_url, audit, clock
        self.secure = web_url.startswith("https://")
        self.cookie = "__Host-vpsd" if self.secure else "vpsd"
        self.lock = threading.Lock()
        self.per_ip = web.RateLimit(PER_IP, 60, mono)
        self.logins = web.RateLimit(PER_IP, 60, mono)
        self.overall = web.RateLimit(OVERALL, 60, mono)
        self.seen = {}  # (uid, auth_date) of Mini App opens already in the audit log
        self.data = self.load()

    # -- storage: {"tokens": {sha256: {uid, exp}}, "sessions"/"mini": {sha256: {uid, exp, created}}} --

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        return {k: data[k] if isinstance(data.get(k), dict) else {} for k in KINDS}

    def save(self):
        from vps_director.setup import write_private
        now = self.clock()
        for kind, cap in (("tokens", MAX_TOKENS), ("sessions", MAX_SESSIONS), ("mini", MAX_SESSIONS)):
            items = {k: v for k, v in self.data[kind].items() if isinstance(v, dict) and float(v.get("exp") or 0) > now}
            self.data[kind] = dict(sorted(items.items(), key=lambda kv: kv[1]["exp"])[-cap:])
        try:
            write_private(self.path, json.dumps(self.data, indent=1) + "\n")
        except OSError:
            pass

    # -- one-time links and sessions --

    def new_login(self, uid):
        """A one-time login token for this owner (the caller puts it in the link)."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.data["tokens"][digest(token)] = {"uid": str(uid), "exp": self.clock() + LOGIN_TTL}
            self.save()
        return token

    def login(self, token, ip, headers=None):
        """GET /login?t=…: a valid token becomes a session cookie and a redirect to "/"."""
        if not self.logins.hit(ip) or self.overall.blocked("*"):
            return web.error(429, "too many attempts, wait a minute")
        with self.lock:
            entry = self.data["tokens"].pop(digest(token), None) if token else None
            if entry is not None:
                self.save()
            ok = entry is not None and float(entry.get("exp") or 0) > self.clock() and entry.get("uid") in self.owners
            if ok:
                sid = secrets.token_urlsafe(32)
                self.data["sessions"][digest(sid)] = {"uid": entry["uid"], "exp": self.clock() + SESSION_TTL,
                                                      "created": self.clock()}
                self.save()
        if not ok:
            self.overall.hit("*")
            self.audit("web", "login refused (bad, used or expired link) from %s" % ip)
            return web.Response(403, b"This login link is invalid, used or expired. Ask the bot for a new one: /dashboard\n",
                                "text/plain; charset=utf-8")
        self.audit(entry["uid"], "login to the dashboard from %s" % ip)
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (
            self.cookie, sid, SESSION_TTL, "; Secure" if self.secure else "")
        return web.Response(303, b"", "text/plain; charset=utf-8", [("Location", "/"), ("Set-Cookie", cookie)])

    def new_mini(self, uid):
        """A Mini App session token for this owner (POST /api/session, after initData was checked)."""
        token, now = secrets.token_urlsafe(32), self.clock()
        with self.lock:
            self.data["mini"][digest(token)] = {"uid": str(uid), "exp": now + MINI_IDLE, "created": now}
            self.save()
        return token

    def mini_user(self, token):
        """The owner behind a Mini App session token, sliding its expiry forward; else None."""
        if not token or len(token) > 100:
            return None
        with self.lock:
            entry = self.data["mini"].get(digest(token))
            now = self.clock()
            if entry is None or float(entry.get("exp") or 0) <= now or entry.get("uid") not in self.owners:
                return None
            exp = min(float(entry.get("created") or 0) + MINI_MAX, now + MINI_IDLE)
            if exp - float(entry["exp"]) > 300:  # write the file at most every 5 minutes of activity
                entry["exp"] = exp
                self.save()
            return entry["uid"]

    def session_user(self, sid):
        if not sid:
            return None
        with self.lock:
            entry = self.data["sessions"].get(digest(sid))
            if entry is None or float(entry.get("exp") or 0) <= self.clock() or entry.get("uid") not in self.owners:
                return None
            return entry["uid"]

    def revoke(self, uid=None, sid=None):
        """Drop one session (sid) or every session and unused link of an owner (uid); returns how many."""
        with self.lock:
            if sid is not None:
                gone = 1 if self.data["sessions"].pop(digest(sid), None) else 0
            else:
                before = sum(len(self.data[k]) for k in KINDS)
                for kind in KINDS:
                    self.data[kind] = {k: v for k, v in self.data[kind].items() if v.get("uid") != str(uid)}
                gone = before - sum(len(self.data[k]) for k in KINDS)
            self.save()
        return gone

    # -- per request --

    def authenticate(self, headers, ip):
        """{"uid", "via", "sid"} of the caller, or a 401/429 Response."""
        if self.per_ip.blocked(ip) or self.overall.blocked("*"):
            return web.error(429, "too many failed attempts, wait a minute")
        init = headers.get("X-Init-Data") or ""
        mini = headers.get(MINI_HEADER) or ""
        if mini:
            uid = self.mini_user(mini)
            if uid:
                return {"uid": uid, "via": "telegram", "sid": None, "mini": True}
        elif init:
            uid = check_init_data(init, self.bot_token, self.owners, self.clock())
            if uid:
                self.note_open(uid, init)
                return {"uid": uid, "via": "telegram", "sid": None}
        else:
            sid = cookie_value(headers.get("Cookie"), self.cookie)
            uid = self.session_user(sid)
            if uid:
                return {"uid": uid, "via": "cookie", "sid": sid}
        self.per_ip.hit(ip)
        self.overall.hit("*")
        return web.error(401, "unauthorized")

    def note_open(self, uid, init):
        """One audit entry per Mini App launch (initData's auth_date), not one per API call."""
        stamp = dict(urllib.parse.parse_qsl(init)).get("auth_date", "")
        with self.lock:
            if (uid, stamp) in self.seen:
                return
            self.seen = {k: v for k, v in self.seen.items() if v > self.clock() - INIT_MAX_AGE * 2}
            self.seen[(uid, stamp)] = self.clock()
        self.audit(uid, "Mini App opened")

    def expected_origin(self, headers):
        if self.web_url:
            return web.origin_of(self.web_url)
        host = (headers.get("Host") or "").lower()
        return host and ("http://" + host, "https://" + host)

    def csrf(self, method, headers, body, who):
        """None when a state-changing call passes the CSRF checks, else a 403/415 Response."""
        if (headers.get("X-Requested-With") or "") != CSRF_HEADER:
            return web.error(403, "missing X-Requested-With")
        origin = (headers.get("Origin") or "").lower().rstrip("/")
        want = self.expected_origin(headers)
        if not origin or not want or origin not in (want if isinstance(want, tuple) else (want,)):
            return web.error(403, "bad origin")
        if not (headers.get("Content-Type") or "").lower().startswith("application/json"):
            return web.error(415, "JSON only")
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return web.error(415, "JSON object body required")
        return None
