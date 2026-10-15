"""Sites from <conf>/sites.json: HTTP status and time, content match, TLS certificate expiry and
domain expiry via RDAP (rdap.org, cached for a day).

    [{"url": "https://shop.example.com", "name": "shop", "expect": 200, "server": "web"},
     {"url": "https://example.org/health", "name": "api", "expect": "ok"}]
`expect` is a status code (default: any 2xx/3xx) or a text that must be in the page.
"""
import concurrent.futures
import datetime
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from vps_director import remote

FILE = "sites.json"
MAX_SITES = 50
CERT_WARN = 14   # days
DOMAIN_WARN = 30  # days
HTTP_TIMEOUT = 15
MAX_BODY = 512 * 1024
RDAP_URL = "https://rdap.org/domain/"
RDAP_TTL = 86400
RDAP_FILE = "rdap-cache.json"
DOWN_AFTER = 2   # failed checks in a row before the watchdog says a site is down
# two-label public suffixes, so that shop.example.co.uk is checked as example.co.uk
SECOND_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au",
                "co.nz", "co.jp", "ne.jp", "com.br", "com.tr", "com.ua", "co.il", "co.za", "com.cn", "com.mx", "co.in",
                "com.ru", "msk.ru", "spb.ru", "com.pl", "co.kr", "com.sg", "com.hk", "com.tw", "com.ar"}
HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def load(conf_dir):
    """(sites, warnings, configured). Only http(s) URLs with a plain host name or IP are taken."""
    path = os.path.join(conf_dir, FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [], [], False
    except (OSError, ValueError) as exc:
        return [], ["%s: %s" % (FILE, exc)], False
    sites, warnings, names = [], [], set()
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            warnings.append("%s #%d: not an object" % (FILE, i + 1))
            continue
        url = str(item.get("url") or "").strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not HOST.fullmatch(parts.hostname) \
                or re.search(r"\s", url):
            warnings.append("%s #%d: bad url %r" % (FILE, i + 1, url[:80]))
            continue
        name = " ".join(str(item.get("name") or parts.hostname).replace(":", "-").split())[:40]
        if name in names:
            warnings.append("%s #%d: duplicate name %s" % (FILE, i + 1, name))
            continue
        expect = item.get("expect")
        if isinstance(expect, bool) or not isinstance(expect, (int, str)) or isinstance(expect, int) and not 100 <= expect <= 599:
            expect = None
        names.add(name)
        sites.append({"url": url, "name": name, "host": parts.hostname.lower(), "https": parts.scheme == "https",
                      "port": parts.port or (443 if parts.scheme == "https" else 80), "expect": expect or None,
                      "server": str(item.get("server") or "")})
        if len(sites) >= MAX_SITES:
            break
    if not isinstance(raw, list):
        warnings.append("%s: expected a list of sites" % FILE)
    return sites, warnings, True


def peer_cert(host, port, timeout=10):
    """The server's certificate (verified against the system CAs and the host name) as a dict."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert()


def cert_probe(host, port=443, connect=peer_cert):
    """(expires, error, reached): `reached` is False when there was no TLS conversation at all
    (timeout, refused, DNS) — then the site is down and the certificate is not the problem."""
    try:
        cert = connect(host, port)
        return ssl.cert_time_to_seconds(cert["notAfter"]), None, True
    except ssl.SSLCertVerificationError as exc:
        return None, (exc.verify_message or "verification failed")[:120], True
    except ssl.SSLError as exc:
        return None, (str(exc) or exc.__class__.__name__)[:120], True
    except OSError as exc:
        return None, (str(exc) or exc.__class__.__name__)[:120], False
    except (KeyError, ValueError, TypeError) as exc:
        return None, (str(exc) or exc.__class__.__name__)[:120], True


def cert_expiry(host, port=443, connect=peer_cert):
    """(unix time the certificate expires, None) or (None, short error) — an expired or untrusted
    certificate fails verification and comes back as an error. A site that cannot be reached at all
    gives (None, None): that is "down", not a certificate problem."""
    expires, err, reached = cert_probe(host, port, connect)
    return (expires, err if reached else None)


def cert_lines(sites, now, t, connect=peer_cert, warn_days=CERT_WARN):
    """[(level, key, text)] for https sites whose certificate expires within `warn_days` or is broken."""
    out = []
    for site in sites:
        if not site["https"]:
            continue
        expires, err = cert_expiry(site["host"], site["port"], connect)
        if err:
            out.append(("bad", "cert:%s:error" % site["name"], t("site_p_cert_error", name=site["name"], err=err)))
        elif expires is not None and (expires - now) / 86400 < warn_days:
            days = int((expires - now) // 86400)
            out.append(("bad" if days < 3 else "warn", "cert:%s:soon" % site["name"],
                        t("site_p_cert_soon", name=site["name"], days=max(days, 0))))
    return out


# ---------- HTTP ----------

def fetch(url, timeout=HTTP_TIMEOUT):
    """(status, first bytes of the body) after redirects; an HTTP error status is a result, not an
    exception. Raises OSError (URLError, timeouts, TLS) when there is no HTTP answer at all."""
    req = urllib.request.Request(url, headers={"User-Agent": "VPS-Director site check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                body = exc.read(MAX_BODY)
            except OSError:
                body = b""
        return exc.code, body


def reason(exc):
    """A short, readable reason for a failed request."""
    inner = getattr(exc, "reason", None)
    if isinstance(inner, ssl.SSLCertVerificationError):
        return inner.verify_message or "certificate verify failed"
    if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(inner, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(inner, socket.gaierror) or isinstance(exc, socket.gaierror):
        return "DNS: no such host"
    if isinstance(inner, ConnectionRefusedError) or isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    return str(inner or exc or exc.__class__.__name__)[:120]


# ---------- domains (RDAP) ----------

def registrable(host):
    """example.com for shop.example.com (example.co.uk for a.example.co.uk); None for IPs."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and ".".join(labels[-2:]) in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def fetch_rdap(domain, timeout=15):
    """The RDAP record of a domain via rdap.org (it redirects to the registry), {} when there is none."""
    req = urllib.request.Request(RDAP_URL + urllib.parse.quote(domain, safe=""),
                                 headers={"Accept": "application/rdap+json", "User-Agent": "VPS-Director"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(MAX_BODY).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:  # the registry has no RDAP (e.g. some country domains) or no such domain
            return {}
        raise


def rdap_expiry(record):
    """Unix time of the "expiration" event of an RDAP record, or None."""
    events = record.get("events") if isinstance(record, dict) else None
    for event in events if isinstance(events, list) else []:
        if isinstance(event, dict) and event.get("eventAction") == "expiration":
            try:
                return datetime.datetime.fromisoformat(str(event.get("eventDate")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def whois_expiry(text):
    """Unix time from a whois answer (paid-till: / Registry Expiry Date: / Expiration Date: / expires:), or None."""
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() not in ("paid-till", "registry expiry date", "registrar registration expiration date",
                                       "expiration date", "expires", "expire", "expiry date"):
            continue
        match = re.match(r"\s*(\d{4})[-.](\d{2})[-.](\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2}))?", value)
        if match:
            y, mo, d, h, mi, s = (int(x or 0) for x in match.groups())
            try:
                return datetime.datetime(y, mo, d, h, mi, s, tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                continue
    return None


def run_whois(domain, timeout=15):
    """The whois CLI's answer for a domain (no shell, IDN as punycode), or None when whois is missing or fails."""
    try:
        name = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    exe = shutil.which("whois")
    if not exe or not HOST.fullmatch(name):
        return None
    try:
        res = subprocess.run([exe, "--", name], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout[:65536] if res.returncode == 0 else None


class Rdap:
    """Domain expiry dates, cached in <conf>/rdap-cache.json for a day (failures too: rdap.org is
    asked at most once a day per domain). The network call happens outside the lock."""

    def __init__(self, conf_dir, lock, fetch_json=fetch_rdap, whois=None, ttl=RDAP_TTL):
        self.path, self.lock, self.fetch_json, self.ttl = os.path.join(conf_dir, RDAP_FILE), lock, fetch_json, ttl
        self.whois = whois

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def expiry(self, domain, now):
        with self.lock:
            hit = self.read().get(domain)
        if isinstance(hit, dict) and now - float(hit.get("checked") or 0) < self.ttl and (
                hit.get("expires") is not None or hit.get("whois") or self.whois is None):  # an old failure predates whois
            return hit.get("expires")
        try:
            expires = rdap_expiry(self.fetch_json(domain))
        except (OSError, ValueError) as exc:
            expires = None
            hit = {"checked": now, "expires": None, "error": reason(exc)}
        else:
            hit = {"checked": now, "expires": expires}
        if expires is None and self.whois is not None:  # no RDAP for this TLD (.ru, .рф, .su): ask whois, quietly
            try:
                expires = whois_expiry(self.whois(domain))
            except (OSError, ValueError, subprocess.SubprocessError):
                expires = None
            if expires is not None:
                hit = {"checked": now, "expires": expires}
        hit["whois"] = self.whois is not None
        with self.lock:
            data = self.read()
            data[domain] = hit
            from vps_director.setup import write_private
            try:
                write_private(self.path, json.dumps(data, indent=1) + "\n")
            except OSError:
                pass
        return expires


# ---------- checks ----------

def check(site, now, fetcher=fetch, connect=peer_cert, rdap=None):
    """One site: {status, time, error, found (text match or None), cert (expiry), cert_error, domain, domain_exp}."""
    res = {"status": None, "time": None, "error": None, "found": None, "cert": None, "cert_error": None,
           "domain": registrable(site["host"]), "domain_exp": None}
    start = time.monotonic()
    try:
        res["status"], body = fetcher(site["url"], HTTP_TIMEOUT)
    except (OSError, ValueError) as exc:
        res["error"], body = reason(exc), b""
    res["time"] = time.monotonic() - start
    if isinstance(site.get("expect"), str) and res["status"] is not None:
        res["found"] = site["expect"] in body.decode("utf-8", "replace")
    if site["https"]:
        res["cert"], res["cert_error"] = cert_expiry(site["host"], site["port"], connect)
    if rdap is not None and res["domain"]:
        res["domain_exp"] = rdap.expiry(res["domain"], now)
    return res


def is_up(site, res):
    status, expect = res["status"], site.get("expect")
    if status is None:
        return False
    if isinstance(expect, int):
        return status == expect
    return 200 <= status < 400 and res["found"] is not False


def check_all(sites, now, fetcher=fetch, connect=peer_cert, rdap=None):
    if not sites:
        return []
    with concurrent.futures.ThreadPoolExecutor(min(len(sites), 8)) as pool:
        return list(pool.map(lambda s: check(s, now, fetcher, connect, rdap), sites))


def days(ts, now):
    return int((ts - now) // 86400)


def assess(site, res, now, t):
    """(icon, line, problems, reminders): problems are [(key, text)] that make a site broken
    (down, bad certificate), reminders [(key, text)] are dates coming up (certificate, domain)."""
    name, problems, reminders, parts = site["name"], [], [], []
    up = is_up(site, res)
    where = t("site_on_server", server=site["server"]) if site.get("server") else ""
    if res["status"] is None:
        parts.append(t("site_down", why=res["error"] or "?"))
        problems.append(("site:%s:down" % name, t("site_p_down", name=name, why=res["error"] or "?") + where))
    else:
        parts.append(t("site_status", code=res["status"], secs=res["time"] or 0))
        if res["found"] is False:
            parts.append(t("site_no_text", text=str(site["expect"])[:40]))
        if not up:
            why = t("site_no_text", text=str(site["expect"])[:40]) if res["found"] is False else "HTTP %s" % res["status"]
            problems.append(("site:%s:down" % name, t("site_p_down", name=name, why=why) + where))
    warn = False
    if res["status"] is None:  # down: the certificate is not checked (and not reported separately)
        pass
    elif res["cert_error"]:
        parts.append(t("site_cert_bad", err=res["cert_error"]))
        problems.append(("site:%s:cert" % name, t("site_p_cert_error", name=name, err=res["cert_error"])))
    elif res["cert"] is not None:
        left = days(res["cert"], now)
        parts.append(t("site_cert", days=left))
        if left < CERT_WARN:
            warn = True
            reminders.append(("cert:%s" % name, t("site_p_cert_soon", name=name, days=max(left, 0))))
    if res["domain_exp"] is not None:
        left = days(res["domain_exp"], now)
        parts.append(t("site_domain", days=left))
        if left < DOMAIN_WARN:
            warn = True
            reminders.append(("domain:%s" % res["domain"], t("site_p_domain", domain=res["domain"], days=max(left, 0))))
    icon = "❌" if problems else "⚠️" if warn else "✅"
    return icon, "%s %s — %s" % (icon, name, " · ".join(parts)), problems, reminders


def hosting_lines(servers, now, t, soon=7):
    """Servers with hosting_paid_until, soonest first."""
    rows = []
    today = datetime.date.fromtimestamp(now)
    for server in servers:
        try:
            paid = datetime.date.fromisoformat(server.get("hosting_paid_until") or "")
        except ValueError:
            continue
        left = (paid - today).days
        rows.append((left, "%s %s" % ("❌" if left < 0 else "⚠️" if left <= soon else "✅",
                                      t("site_hosting_line", name=server["name"], date=paid.isoformat(), days=left))))
    return [line for _, line in sorted(rows)]


def report(sites, results, now, t, stamp, hosting=(), reminders_extra=(), warnings=()):
    """The /sites message: header, one line per site, hosting dates, footer 'What to check'."""
    lines, problems, reminders = [], [], list(reminders_extra)
    up = 0
    for site, res in zip(sites, results):
        icon, line, found, rem = assess(site, res, now, t)
        up += 1 if is_up(site, res) else 0
        lines.append(line)
        problems += found
        reminders += rem
    state = t("srv_all_fine") if not problems and not reminders else t("srv_problems", n=len(problems) + len(reminders))
    out = [t("site_head", stamp=stamp), t("site_up", n=up, total=len(sites), state=state), ""] + lines
    if hosting:
        out += ["", t("site_hosting_head")] + ["   " + h for h in hosting]
    if problems or reminders:
        out += ["", t("what_to_check")] + [remote.footer_line("bad", text) for _, text in problems] + [
            remote.footer_line("warn", text) for _, text in reminders]
    if warnings:
        out += [""] + ["⚠️ " + w for w in warnings]
    return "\n".join(out)
