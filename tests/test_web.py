"""Task 07a: the Mini App / dashboard web server, its auth and its JSON API. Offline: the server
only ever listens on 127.0.0.1; Telegram initData is signed here with a fake bot token."""
import http.client
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_director import Base, make_cfg  # noqa: E402

from vps_director import web, webauth  # noqa: E402

FAKE_TOKEN = "123456:FAKE-test-token-not-real"
OWNER_ID = 111
T0 = 1_800_000_000.0
ORIGIN = "https://dash.example.com"


def init_data(uid=OWNER_ID, auth_date=T0, token=FAKE_TOKEN, **extra):
    fields = {"auth_date": str(int(auth_date)), "query_id": "AAH", "user": json.dumps({"id": uid, "first_name": "Ann"})}
    fields.update(extra)
    return webauth.sign_init_data(fields, token)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class EchoApi:
    def __init__(self):
        self.calls = []

    def call(self, method, path, query, body, who):
        self.calls.append((method, path, who["uid"], who["via"]))
        return web.Response.json(200, {"ok": True, "uid": who["uid"]})


def static_root():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "web", "css"))
    with open(os.path.join(root, "web", "index.html"), "w") as fh:
        fh.write("<p>home</p>")
    with open(os.path.join(root, "web", "css", "app.css"), "w") as fh:
        fh.write("body{}")
    with open(os.path.join(root, "web", ".env"), "w") as fh:
        fh.write("SECRET=1")
    with open(os.path.join(root, "web", "notes.bak"), "w") as fh:
        fh.write("x")
    with open(os.path.join(root, "secret.txt"), "w") as fh:
        fh.write("outside")
    os.symlink(os.path.join(root, "secret.txt"), os.path.join(root, "web", "link.txt"))
    return os.path.join(root, "web")


class StaticTest(unittest.TestCase):
    def setUp(self):
        self.app = web.WebApp(static_root())

    def get(self, path, method="GET"):
        return self.app.handle(method, path, {}, b"", "127.0.0.1")

    def test_index_and_mime(self):
        res = self.get("/")
        self.assertEqual((res.status, res.body, res.content_type), (200, b"<p>home</p>", "text/html; charset=utf-8"))
        res = self.get("/css/app.css?v=2")
        self.assertEqual((res.status, res.content_type), (200, "text/css; charset=utf-8"))

    def test_traversal_and_hidden_files_refused(self):
        for path in ("/../secret.txt", "/css/../../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt",
                     "/.env", "/notes.bak", "/link.txt", "/css", "/css/", "/x%00.html", "/..\\secret.txt",
                     "//etc/passwd", "/nope.html"):
            res = self.get(path)
            self.assertEqual(res.status, 404, path)
            self.assertNotIn(b"outside", res.body)
            self.assertNotIn(b"SECRET", res.body)

    def test_only_get(self):
        self.assertEqual(self.get("/", "POST").status, 405)
        self.assertEqual(self.get("/", "HEAD").status, 200)

    def test_security_headers(self):
        res = self.get("/")
        csp = res.header("Content-Security-Policy")
        self.assertIn("frame-ancestors https://web.telegram.org https://*.telegram.org", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertEqual(res.header("Referrer-Policy"), "no-referrer")
        self.assertEqual(res.header("X-Content-Type-Options"), "nosniff")
        self.assertIsNone(res.header("X-Frame-Options"))  # would block Telegram; frame-ancestors does the job
        self.assertEqual(self.get("/api/me").header("Cache-Control"), "no-store")

    def test_api_without_auth_is_refused(self):
        res = self.get("/api/me")
        self.assertEqual(res.status, 401)
        self.assertEqual(res.data, {"ok": False, "error": "unauthorized"})

    def test_errors_never_leak(self):
        app = web.WebApp(static_root(), auth=BoomAuth(), api=object())
        res = app.handle("GET", "/api/me", {}, b"", "127.0.0.1")
        self.assertEqual((res.status, res.data["error"]), (500, "internal error"))
        self.assertNotIn(b"boom", res.body)


class BoomAuth:
    def authenticate(self, headers, ip):
        raise RuntimeError("boom secret")


class HelpersTest(unittest.TestCase):
    def test_client_ip(self):
        self.assertEqual(web.client_ip("127.0.0.1", {"X-Forwarded-For": "198.51.100.1, 203.0.113.5"}), "203.0.113.5")
        self.assertEqual(web.client_ip("203.0.113.9", {"X-Forwarded-For": "198.51.100.1"}), "203.0.113.9")
        self.assertEqual(web.client_ip("127.0.0.1", {"X-Forwarded-For": "junk"}), "127.0.0.1")
        self.assertEqual(web.client_ip("127.0.0.1", {}), "127.0.0.1")

    def test_rate_limit(self):
        now = [0.0]
        limit = web.RateLimit(3, 60, clock=lambda: now[0], keys=2)
        self.assertEqual([limit.hit("a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(limit.blocked("a"))
        now[0] = 61
        self.assertTrue(limit.hit("a"))
        limit.hit("b"), limit.hit("c")
        self.assertEqual(len(limit.hits), 2)  # bounded memory

    def test_urls(self):
        self.assertTrue(web.valid_url("https://dash.example.com"))
        self.assertTrue(web.valid_url("https://dash.example.com:8443/"))
        self.assertFalse(web.valid_url("https://dash.example.com/app"))
        self.assertTrue(web.valid_url("http://127.0.0.1:8080"))
        for bad in ("http://dash.example.com", "ftp://x", "https://", "https://u:p@x.example.com", "https://x?a=1", "x"):
            self.assertFalse(web.valid_url(bad), bad)
        self.assertEqual(web.origin_of("https://Dash.Example.com:8443/app"), "https://dash.example.com:8443")


class ConfigTest(unittest.TestCase):
    def test_off_by_default(self):
        cfg = make_cfg(tempfile.mkdtemp())
        self.assertEqual((cfg.web_port, cfg.web_bind, cfg.web_url), (0, "127.0.0.1", ""))

    def test_values_checked(self):
        cfg = make_cfg(tempfile.mkdtemp(), WEB_PORT="8090", WEB_URL="https://dash.example.com/")
        cfg.validate()
        self.assertEqual(cfg.web_url, "https://dash.example.com")
        for env in ({"WEB_PORT": "70000"}, {"WEB_PORT": "8090", "WEB_BIND": "example.com"},
                    {"WEB_URL": "http://dash.example.com"}):
            with self.assertRaises(SystemExit):
                make_cfg(tempfile.mkdtemp(), **env).validate()

    def test_public_bind_warns(self):
        cfg = make_cfg(tempfile.mkdtemp(), WEB_PORT="8090", WEB_BIND="0.0.0.0")
        self.assertTrue(any("WEB_BIND" in w for w in cfg.warnings))


class LiveServerTest(Base):
    """A real socket on 127.0.0.1 with an ephemeral port."""

    def test_serves_on_loopback_only(self):
        self.d.cfg.web_port = 0  # ephemeral port; run() starts the server only when WEB_PORT is set
        server = self.d.start_web()
        try:
            host, port = server.server_address[:2]
            self.assertEqual(host, "127.0.0.1")
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertIn("frame-ancestors", res.getheader("Content-Security-Policy"))
            res.read()
            conn.request("GET", "/../director.py")
            res = conn.getresponse()
            self.assertEqual(res.status, 404)
            res.read()
            conn.request("POST", "/api/jobs", body=b"x" * (web.MAX_BODY + 1), headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            self.assertEqual(res.status, 413)
            self.assertEqual(json.loads(res.read())["error"], "body too large")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

class InitDataTest(unittest.TestCase):
    def check(self, raw, now=T0 + 5):
        return webauth.check_init_data(raw, FAKE_TOKEN, {"111"}, now)

    def test_valid(self):
        self.assertEqual(self.check(init_data()), "111")
        self.assertEqual(self.check(init_data(signature="abc")), "111")  # every field but hash is signed

    def test_bad_hash(self):
        raw = init_data()
        self.assertIsNone(self.check(raw.replace("hash=", "hash=0")))
        self.assertIsNone(self.check(init_data(token="999:other")))
        tampered = raw.replace("Ann", "Bob")
        self.assertIsNone(self.check(tampered))

    def test_expired_and_future(self):
        self.assertIsNone(self.check(init_data(auth_date=T0 - 601), now=T0))
        self.assertEqual(self.check(init_data(auth_date=T0 - 599), now=T0), "111")
        self.assertIsNone(self.check(init_data(auth_date=T0 + 3600), now=T0))

    def test_foreign_user(self):
        self.assertIsNone(self.check(init_data(uid=222)))

    def test_missing_and_malformed_fields(self):
        sign = lambda fields: webauth.sign_init_data(fields, FAKE_TOKEN)  # noqa: E731
        user = json.dumps({"id": OWNER_ID})
        for raw in ("", "junk", "hash=abc", sign({"user": user}), sign({"auth_date": str(int(T0))}),
                    sign({"auth_date": "x", "user": user}), sign({"auth_date": str(int(T0)), "user": "{bad"}),
                    sign({"auth_date": str(int(T0)), "user": json.dumps({"id": "111"})}),
                    sign({"auth_date": str(int(T0)), "user": json.dumps([1])}),
                    init_data() + "&user=" + user, "a" * 9000):
            self.assertIsNone(self.check(raw), raw[:80])
        self.assertIsNone(webauth.check_init_data(init_data(), "", {"111"}, T0))


class AuthBase(unittest.TestCase):
    web_url = ORIGIN

    def setUp(self):
        self.conf = tempfile.mkdtemp()
        self.clock, self.mono = Clock(), Clock(0.0)
        self.audited = []
        self.auth = webauth.Auth(FAKE_TOKEN, {"111"}, self.conf, self.web_url,
                                 audit=lambda who, text: self.audited.append((who, text)), clock=self.clock, mono=self.mono)
        self.api = EchoApi()
        self.app = web.WebApp(static_root(), auth=self.auth, api=self.api)

    def req(self, method, path, headers=None, body=b"", ip="203.0.113.1"):
        return self.app.handle(method, path, headers or {}, body, ip)

    def login(self, ip="203.0.113.1"):
        res = self.req("GET", "/login?t=" + self.auth.new_login("111"), ip=ip)
        self.assertEqual(res.status, 303)
        return res.header("Set-Cookie").split(";")[0]


class LoginLinkTest(AuthBase):
    def test_lifecycle(self):
        token = self.auth.new_login("111")
        self.assertGreaterEqual(len(token), 43)
        with open(os.path.join(self.conf, webauth.FILE)) as fh:
            stored = fh.read()
        self.assertNotIn(token, stored)  # only the hash is kept
        self.assertEqual(os.stat(os.path.join(self.conf, webauth.FILE)).st_mode & 0o777, 0o600)
        res = self.req("GET", "/login?t=" + token)
        self.assertEqual((res.status, res.header("Location")), (303, "/"))
        cookie = res.header("Set-Cookie")
        self.assertTrue(cookie.startswith("__Host-vpsd="), cookie)
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(flag, cookie)
        self.assertEqual(res.header("Cache-Control"), "no-store")
        self.assertEqual(res.header("Referrer-Policy"), "no-referrer")
        self.assertIn(("111", "login to the dashboard from 203.0.113.1"), self.audited)
        self.assertNotIn(token, json.dumps(self.audited))
        self.assertEqual(self.req("GET", "/login?t=" + token).status, 403)  # single use

    def test_expired_and_wrong(self):
        token = self.auth.new_login("111")
        self.clock.now += webauth.LOGIN_TTL + 1
        self.assertEqual(self.req("GET", "/login?t=" + token).status, 403)
        self.assertEqual(self.req("GET", "/login?t=wrong").status, 403)
        self.assertEqual(self.req("GET", "/login").status, 403)
        self.assertEqual(self.req("POST", "/login?t=x").status, 405)
        self.assertTrue(any("login refused" in text for _, text in self.audited))

    def test_link_of_a_removed_owner(self):
        token = self.auth.new_login("111")
        self.auth.owners = {"222"}
        self.assertEqual(self.req("GET", "/login?t=" + token).status, 403)

    def test_rate_limited_per_ip(self):
        codes = [self.req("GET", "/login?t=bad%d" % i).status for i in range(12)]
        self.assertEqual(codes[:10], [403] * 10)
        self.assertEqual(codes[10:], [429, 429])
        good = self.auth.new_login("111")
        self.assertEqual(self.req("GET", "/login?t=" + good).status, 429)  # even a good link waits
        self.assertEqual(self.req("GET", "/login?t=" + good, ip="198.51.100.2").status, 303)  # other IPs are not blocked
        self.mono.now += 61
        self.assertEqual(self.req("GET", "/login?t=" + self.auth.new_login("111")).status, 303)

    def test_failed_logins_count_overall(self):
        for i in range(webauth.OVERALL):
            self.req("GET", "/login?t=bad", ip="10.1.%d.%d" % (i // 200, i % 200))
        self.assertEqual(self.req("GET", "/login?t=" + self.auth.new_login("111"), ip="192.0.2.77").status, 429)

    def test_http_url_gets_plain_cookie(self):
        auth = webauth.Auth(FAKE_TOKEN, {"111"}, tempfile.mkdtemp(), "http://127.0.0.1:8090")
        res = auth.login(auth.new_login("111"), "127.0.0.1")
        cookie = res.header("Set-Cookie")
        self.assertTrue(cookie.startswith("vpsd="))
        self.assertNotIn("Secure", cookie)


class SessionTest(AuthBase):
    def test_cookie_session(self):
        cookie = self.login()
        res = self.req("GET", "/api/me", {"Cookie": "theme=dark; " + cookie})
        self.assertEqual((res.status, res.data["uid"]), (200, "111"))
        self.assertEqual(self.api.calls[-1], ("GET", "/api/me", "111", "cookie"))
        with open(os.path.join(self.conf, webauth.FILE)) as fh:
            self.assertNotIn(cookie.split("=", 1)[1], fh.read())

    def test_tampered_and_missing(self):
        cookie = self.login()
        name, value = cookie.split("=", 1)
        for bad in (name + "=" + value[:-1] + ("A" if value[-1] != "A" else "B"), name + "=", "vpsd=" + value, ""):
            self.assertEqual(self.req("GET", "/api/me", {"Cookie": bad}).status, 401, bad)

    def test_expiry(self):
        cookie = self.login()
        self.clock.now += webauth.SESSION_TTL + 1
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}).status, 401)

    def test_survives_restart_and_revocation(self):
        cookie = self.login()
        again = webauth.Auth(FAKE_TOKEN, {"111"}, self.conf, ORIGIN, clock=self.clock)
        self.assertEqual(again.session_user(cookie.split("=", 1)[1]), "111")
        self.assertEqual(self.auth.revoke(uid="111"), 1)
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}).status, 401)

    def test_logout(self):
        cookie = self.login()
        headers = {"Cookie": cookie, "X-Requested-With": "vpsd", "Origin": ORIGIN, "Content-Type": "application/json"}
        res = self.req("POST", "/api/logout", headers, b"{}")
        self.assertEqual(res.status, 200)
        self.assertIn("Max-Age=0", res.header("Set-Cookie"))
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}).status, 401)
        self.assertIn(("111", "logout from the dashboard"), self.audited)

    def test_init_data_header(self):
        self.clock.now = T0 + 5
        res = self.req("GET", "/api/me", {"X-Init-Data": init_data()})
        self.assertEqual((res.status, self.api.calls[-1][3]), (200, "telegram"))
        self.req("GET", "/api/servers", {"X-Init-Data": init_data()})
        self.assertEqual(self.audited, [("111", "Mini App opened")])  # once per launch, not per call
        self.assertEqual(self.req("GET", "/api/me", {"X-Init-Data": init_data(uid=222)}).status, 401)
        self.clock.now = T0 + 700
        self.assertEqual(self.req("GET", "/api/me", {"X-Init-Data": init_data()}).status, 401)

    def test_bad_init_data_does_not_fall_back_to_cookie(self):
        cookie = self.login()
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie, "X-Init-Data": "junk"}).status, 401)

    def test_failed_auth_rate_limit(self):
        codes = [self.req("GET", "/api/me", {"Cookie": "__Host-vpsd=nope"}).status for _ in range(11)]
        self.assertEqual(codes, [401] * 10 + [429])
        cookie = self.login(ip="198.51.100.9")
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}).status, 429)  # this IP waits
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}, ip="198.51.100.9").status, 200)
        self.mono.now += 61
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}).status, 200)

    def test_overall_failed_limit(self):
        cookie = self.login(ip="192.0.2.50")
        for i in range(webauth.OVERALL):
            self.req("GET", "/api/me", ip="10.0.%d.%d" % (i // 200, i % 200))
        self.assertEqual(self.req("GET", "/api/me", {"Cookie": cookie}, ip="192.0.2.50").status, 429)


class MiniSessionTest(AuthBase):
    """The Mini App swaps initData (fresh 10 minutes) for a sliding session — it no longer dies after 10 minutes."""

    def post_session(self, headers):
        h = {"X-Requested-With": "vpsd", "Origin": ORIGIN, "Content-Type": "application/json"}
        h.update(headers)
        return self.req("POST", "/api/session", h, b"{}")

    def session(self):
        self.clock.now = T0 + 5
        res = self.post_session({"X-Init-Data": init_data()})
        self.assertEqual(res.status, 200)
        token = json.loads(res.body)["session"]
        self.assertGreater(len(token), 30)
        return token

    def test_session_outlives_init_data_and_slides(self):
        token = self.session()
        for minute in (9, 30, 80, 130):  # each call within the idle hour moves the expiry forward
            self.clock.now = T0 + minute * 60
            res = self.req("GET", "/api/me", {webauth.MINI_HEADER: token})
            self.assertEqual((res.status, self.api.calls[-1][3]), (200, "telegram"), minute)
        self.clock.now += webauth.MINI_IDLE + 1
        self.assertEqual(self.req("GET", "/api/me", {webauth.MINI_HEADER: token}).status, 401)

    def test_session_has_a_hard_cap(self):
        token = self.session()
        t = T0
        while t < T0 + webauth.MINI_MAX - 1800:
            t += 1800
            self.clock.now = t
            self.assertEqual(self.req("GET", "/api/me", {webauth.MINI_HEADER: token}).status, 200)
        self.clock.now = T0 + webauth.MINI_MAX + 10
        self.assertEqual(self.req("GET", "/api/me", {webauth.MINI_HEADER: token}).status, 401)

    def test_only_init_data_can_make_a_session(self):
        token = self.session()
        self.assertEqual(self.post_session({webauth.MINI_HEADER: token}).status, 403)
        self.assertEqual(self.post_session({"X-Init-Data": init_data(uid=222)}).status, 401)
        self.assertEqual(self.req("GET", "/api/session", {"X-Init-Data": init_data()}).status, 405)
        cookie = self.login()
        self.assertEqual(self.post_session({"Cookie": cookie}).status, 403)

    def test_session_needs_csrf_and_is_stored_hashed(self):
        self.clock.now = T0 + 5
        res = self.req("POST", "/api/session", {"X-Init-Data": init_data(), "Content-Type": "application/json"}, b"{}")
        self.assertEqual(res.status, 403)
        token = self.session()
        with open(os.path.join(self.conf, webauth.FILE)) as fh:
            self.assertNotIn(token, fh.read())

    def test_revoke_kills_mini_sessions_and_survives_restart(self):
        token = self.session()
        again = webauth.Auth(FAKE_TOKEN, {"111"}, self.conf, ORIGIN, clock=self.clock)
        self.assertEqual(again.mini_user(token), "111")
        self.auth.revoke(uid="111")
        self.assertEqual(self.req("GET", "/api/me", {webauth.MINI_HEADER: token}).status, 401)

    def test_bad_session_counts_as_failed_auth(self):
        codes = [self.req("GET", "/api/me", {webauth.MINI_HEADER: "nope"}).status for _ in range(11)]
        self.assertEqual(codes, [401] * 10 + [429])


class CsrfTest(AuthBase):
    def setUp(self):
        super().setUp()
        self.cookie = self.login()

    def post(self, drop=(), body=b'{"a": 1}', method="POST", **over):
        headers = {"Cookie": self.cookie, "X-Requested-With": "vpsd", "Origin": ORIGIN, "Content-Type": "application/json"}
        headers.update(over)
        for key in drop:
            headers.pop(key)
        return self.req(method, "/api/jobs", headers, body)

    def test_good_call_passes(self):
        self.assertEqual(self.post().status, 200)
        self.assertEqual(self.post(method="DELETE", body=b"{}").status, 200)

    def test_refusals(self):
        self.assertEqual(self.post(drop=["X-Requested-With"]).status, 403)
        self.assertEqual(self.post(**{"X-Requested-With": "XMLHttpRequest"}).status, 403)
        self.assertEqual(self.post(drop=["Origin"]).status, 403)
        self.assertEqual(self.post(Origin="https://evil.example.com").status, 403)
        self.assertEqual(self.post(Origin="http://dash.example.com").status, 403)
        self.assertEqual(self.post(**{"Content-Type": "text/plain"}).status, 415)
        self.assertEqual(self.post(body=b"a=1").status, 415)
        self.assertEqual(self.post(body=b"[1]").status, 415)
        self.assertEqual(self.post(body=b"").status, 415)
        self.assertEqual(self.post(method="DELETE", body=b"").status, 415)
        self.assertEqual(len(self.api.calls), 0)

    def test_without_web_url_origin_must_match_host(self):
        auth = webauth.Auth(FAKE_TOKEN, {"111"}, tempfile.mkdtemp(), "")
        base = {"X-Requested-With": "vpsd", "Content-Type": "application/json", "Host": "127.0.0.1:8090"}
        self.assertIsNone(auth.csrf("POST", dict(base, Origin="http://127.0.0.1:8090"), b"{}", {}))
        self.assertIsNotNone(auth.csrf("POST", dict(base, Origin="http://evil.example.com"), b"{}", {}))
        self.assertIsNotNone(auth.csrf("POST", base, b"{}", {}))


if __name__ == "__main__":
    unittest.main()


class StaticCacheTest(unittest.TestCase):
    def test_etag_304_and_gzip(self):
        import gzip as gz
        app = web.WebApp()
        first = app.handle("GET", "/app.js", {"Accept-Encoding": "gzip, br"})
        self.assertEqual(first.status, 200)
        self.assertEqual(first.header("Content-Encoding"), "gzip")
        with open(os.path.join(web.WEB_DIR, "app.js"), "rb") as fh:
            self.assertEqual(gz.decompress(first.body), fh.read())
        etag = first.header("ETag")
        again = app.handle("GET", "/app.js", {"If-None-Match": etag})
        self.assertEqual((again.status, again.body), (304, b""))
        plain = app.handle("GET", "/app.js", {})
        self.assertIsNone(plain.header("Content-Encoding"))
        font = app.handle("GET", "/vendor/fonts/Geist-Variable.woff2", {"Accept-Encoding": "gzip"})
        self.assertIsNone(font.header("Content-Encoding"))  # already compressed
