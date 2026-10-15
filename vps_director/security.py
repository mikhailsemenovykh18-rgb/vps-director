"""Read-only security checks per server (/security) against a stored baseline.

Per server: SSH logins of the last 24 h (only time, user and source IP are kept — never the log
lines), password and root login settings, listening TCP ports, Docker containers and images,
suspicious processes (names dressed up as kernel threads, well-known miners / bandwidth sellers /
backdoors, deleted binaries running from temp folders), fail2ban, automatic updates and pending
updates. Nothing is installed or changed on the server; `sudo -n` is used only when it works
without a password, and only for reading.

Baseline: <conf>/security-baseline.json, {"servers": {name: {"ips", "ports", "containers", "images",
"time"}}}. The first scan of a server stores it; `/security accept` takes the last scan as the new
normal (login IPs are added to the known ones, the rest is replaced).
"""
import datetime
import ipaddress
import json
import os
import re
import time

from vps_director import remote

FILE = "security-baseline.json"
MAX_IPS = 500
SHOW_LOGINS = 5
ACCEPTABLE = ("ip", "port", "container", "image")  # finding kinds that /security accept can make normal
EPHEMERAL = 32768  # loopback listeners at or above this port come and go (IDE servers, …): not tracked

# Process / image names of known miners, "earn money for your bandwidth" containers and backdoors.
BAD_NAMES = ("xmrig", "xmr-stak", "kdevtmpfsi", "kinsing", "minerd", "cpuminer", "nbminer", "lolminer",
             "earnfm", "earnapp", "traffmonetizer", "honeygain", "packetstream", "psclient", "repocket",
             "peer2profit", "pawns-cli", "iproyal", "bitping", "gs-netcat", "gsocket", "gs-dbus")
BAD_PATHS = ("/.config/htop/", "/dev/shm/", "/tmp/.", "/var/tmp/.")  # gsocket's default hiding place, dot dirs in temp
TEMP_DIRS = ("/tmp/", "/var/tmp/", "/dev/shm/", "/run/user/", "/home/", "/root/", "memfd:")

PROBE = r"""
S=""; sudo -n true 2>/dev/null && S="sudo -n"
D=docker; command -v docker >/dev/null 2>&1 || D=""
[ -n "$D" ] && ! docker ps >/dev/null 2>&1 && D="sudo -n docker"
src=none; L=""
if [ -n "$(journalctl -u ssh -u sshd -n 1 --no-pager -q 2>/dev/null)" ]; then
  src=journal; L=$(journalctl -u ssh -u sshd --since -24h --no-pager -q -o short-iso 2>/dev/null | grep -a "Accepted ")
elif [ -n "$S" ] && [ -n "$($S journalctl -u ssh -u sshd -n 1 --no-pager -q 2>/dev/null)" ]; then
  src=journal; L=$($S journalctl -u ssh -u sshd --since -24h --no-pager -q -o short-iso 2>/dev/null | grep -a "Accepted ")
else
  for f in /var/log/auth.log /var/log/secure; do
    if [ -r "$f" ]; then src=file; L=$(grep -a "Accepted " "$f" | tail -n 500); break
    elif [ -n "$S" ] && [ -e "$f" ]; then src=file; L=$($S grep -a "Accepted " "$f" | tail -n 500); break; fi
  done
fi
echo "@@facts"
echo "logsrc=$src"
echo "client=${SSH_CONNECTION%% *}"
X=$(command -v sshd || echo /usr/sbin/sshd)
C=$($X -T 2>/dev/null); [ -z "$C" ] && [ -n "$S" ] && C=$($S $X -T 2>/dev/null)
if [ -n "$C" ]; then echo "sshd_src=effective"
else echo "sshd_src=files"; C=$(for f in /etc/ssh/sshd_config.d/*.conf /etc/ssh/sshd_config; do [ -r "$f" ] && awk 'tolower($1)=="match"{exit} {print}' "$f"; done); fi
printf '%s\n' "$C" | awk 'tolower($1) ~ /^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication)$/ && !seen[tolower($1)]++ {print "sshd_" tolower($1) "=" tolower($2)}'
if command -v fail2ban-server >/dev/null 2>&1 || [ -x /usr/bin/fail2ban-server ]; then echo "f2b=$(systemctl is-active fail2ban 2>/dev/null || echo inactive)"; else echo "f2b=absent"; fi
if command -v apt-config >/dev/null 2>&1; then
  ua=$(apt-config dump 2>/dev/null | sed -n 's/^APT::Periodic::Unattended-Upgrade "\(.*\)";/\1/p' | tail -n 1)
  if [ -x /usr/bin/unattended-upgrade ] && [ "${ua:-0}" != "0" ]; then echo "ua=on"; else echo "ua=off"; fi
  U=$(apt-get -s -o Debug::NoLocking=true upgrade 2>/dev/null | grep '^Inst ')
  echo "updates=$(printf '%s' "$U" | grep -c .)"
  echo "sec_updates=$(printf '%s' "$U" | grep -ci security)"
elif systemctl is-enabled dnf-automatic.timer dnf-automatic-install.timer 2>/dev/null | grep -q enabled; then echo "ua=on"
fi
[ -n "$D" ] && echo "docker=1"
echo "@@logins"
printf '%s\n' "$L" | tail -n 300 | cut -c1-300
echo "@@ports"
$S ss -tlnpH 2>/dev/null || ss -tlnH 2>/dev/null
echo "@@containers"
[ -n "$D" ] && $D ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null
echo "@@images"
[ -n "$D" ] && $D images --format '{{.Repository}}' 2>/dev/null
echo "@@addrs"
hostname -I 2>/dev/null
echo "@@ps"
ps -eo pid=,ppid=,comm=,args= 2>/dev/null | cut -c1-300
echo "@@exe"
E=$($S find /proc -mindepth 2 -maxdepth 2 -name exe -printf '%h %l\n' 2>/dev/null)
[ -z "$E" ] && E=$(for e in /proc/[0-9]*/exe; do l=$(readlink "$e" 2>/dev/null) && echo "${e%/exe} $l"; done)
printf '%s\n' "$E"
"""

ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")
SYSLOG = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}:\d{2}:\d{2})")
ACCEPTED = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port \d+")
USER = re.compile(r"[A-Za-z0-9._@-]{1,32}")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def login_time(line, now):
    """Unix time of a journal (short-iso) or syslog line, or None."""
    match = ISO.match(line)
    if match:
        rest = line[match.end():]
        tz = re.match(r"(?:\.\d+)?([+-]\d{2}:?\d{2}|Z)?", rest).group(1)
        tz = (tz or "").replace("Z", "+00:00")
        if len(tz) == 5:  # journald writes +0200; Python 3.10's fromisoformat wants +02:00
            tz = tz[:3] + ":" + tz[3:]
        text = "%sT%s%s" % (match.group(1), match.group(2), tz)
        try:
            when = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        return when.timestamp() if when.tzinfo else time.mktime(when.timetuple())
    match = SYSLOG.match(line)
    if match and match.group(1) in MONTHS:  # no year: the most recent such date not in the future
        year = time.localtime(now).tm_year
        for y in (year, year - 1):
            try:
                ts = time.mktime(time.strptime("%d %s %s %s" % (y, match.group(1), match.group(2), match.group(3)),
                                               "%Y %b %d %H:%M:%S"))
            except ValueError:
                return None
            if ts <= now + 86400:
                return ts
    return None


def parse_logins(lines, now, since=86400):
    """[(time, user, ip, method)] of the last `since` seconds; nothing else of the line is kept."""
    out = []
    for line in lines:
        match = ACCEPTED.search(line)
        if not match:
            continue
        when = login_time(line, now)
        try:
            ip = str(ipaddress.ip_address(match.group(3)))
        except ValueError:
            continue
        user = match.group(2) if USER.fullmatch(match.group(2)) else "?"
        if when is not None and now - since <= when <= now + 300:
            out.append((when, user, ip, "password" if match.group(1) in ("password", "keyboard-interactive/pam") else "key"))
    return out


def parse_ports(lines):
    """{local address: process name or ""} of listening TCP sockets (ss -tlnH[p])."""
    ports = {}
    for line in lines:
        cols = line.split()
        if len(cols) < 4 or cols[0] != "LISTEN":
            continue
        addr = cols[3]
        port = addr.rsplit(":", 1)[-1]
        if not port.isdigit():
            continue
        host = addr.rsplit(":", 1)[0]
        if int(port) >= EPHEMERAL and (host.startswith("127.") or host in ("[::1]", "localhost")):
            continue
        proc = re.search(r'users:\(\("([^"]{1,40})"', line)
        ports[addr] = proc.group(1) if proc else ""
    return ports


def is_local(addr):
    """A listener bound to loopback only (127.x, ::1, localhost): not reachable from outside."""
    host = addr.rsplit(":", 1)[0].split("%", 1)[0]
    return host.startswith("127.") or host in ("[::1]", "localhost", "[::ffff:127.0.0.1]")


def port_of(addr):
    port = addr.rsplit(":", 1)[-1]
    return int(port) if port.isdigit() else None


def drop_ignored(scan, ranges):
    """The scan without listeners whose port is in `ranges` ([[lo, hi]], servers.json ignore_ports)."""
    if not ranges or scan is None:
        return scan
    keep = {a: p for a, p in scan["ports"].items()
            if not any(lo <= (port_of(a) or 0) <= hi for lo, hi in ranges)}
    return dict(scan, ports=keep)


def kernel_like(args):
    return bool(re.fullmatch(r"\[[^\]]{1,40}\]", args.strip()))


def suspicious(ps_lines, exe_lines):
    """[(stable key, text)] of processes that look like malware. ps lines: "pid ppid comm args"."""
    exe = {}
    for line in exe_lines:
        path, _, target = line.partition(" ")
        if path.startswith("/proc/") and target.strip():
            exe[path[6:]] = target.strip()
    found = {}
    for line in ps_lines:
        cols = line.split(None, 3)
        if len(cols) < 3 or not cols[0].isdigit() or not cols[1].isdigit():
            continue
        pid, ppid, comm = cols[0], cols[1], cols[2]
        args = cols[3] if len(cols) > 3 else ""
        path = exe.get(pid, "")
        deleted = path.endswith(" (deleted)")
        clean = path[:-10] if deleted else path
        names = {comm.lower(), os.path.basename(clean).lower(), os.path.basename(args.split()[0]).lower() if args.split() else ""}
        bad = next((b for b in BAD_NAMES if any(b in n for n in names if n)), None)
        if bad:
            found["bad:" + bad] = ("known", bad, clean or comm)
        elif kernel_like(args) and ppid not in ("0", "2") and pid != "2" and "<defunct>" not in args:
            found["fake:" + args.strip()] = ("fake", args.strip(), clean or "?")
        elif clean and any(p in clean for p in BAD_PATHS):
            found["path:" + clean] = ("path", comm, clean)
        elif deleted and clean.startswith(TEMP_DIRS):
            found["deleted:" + clean] = ("deleted", comm, clean)
    return found


def parse_addrs(lines):
    """The server's own addresses (hostname -I): no loopback or link-local, at most 20."""
    found = []
    for word in " ".join(lines).split():
        try:
            ip = ipaddress.ip_address(word)
        except ValueError:
            continue
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified) and str(ip) not in found:
            found.append(str(ip))
    return found[:20]


def aggregate_logins(logins):
    """[(last time, user, ip, count, method)] grouped by (user, source IP), newest first."""
    groups = {}
    for when, user, ip, method in logins:
        last, count, password = groups.get((user, ip), (0, 0, False))
        groups[(user, ip)] = (max(last, when), count + 1, password or method == "password")
    return sorted(((last, user, ip, count, "password" if password else "key")
                   for (user, ip), (last, count, password) in groups.items()), reverse=True)


def update_own(known, servers, scans):
    """Addresses that belong to our own servers: {server name: [ip]}, plus "" (the bot) for the address
    the servers saw the probe come from. Remembered between runs; a server that did not answer keeps its old list."""
    known = {k: list(v) for k, v in (known or {}).items() if isinstance(v, list)}
    main = next((s["name"] for s in servers if s.get("main")), "")
    for server, scan in zip(servers, scans):
        if scan is None:
            continue
        known[server["name"]] = list(scan.get("addrs") or [])
        client = scan["facts"].get("client", "")
        try:
            client = str(ipaddress.ip_address(client))
        except ValueError:
            continue
        if server.get("ssh") and client not in known.get("", []):
            known[""] = (known.get("", []) + [client])[-5:]
    return known, main


def own_labels(known, main, t):
    """{ip: label}: "web", "main (main, this bot)" or "this bot"."""
    labels = {}
    for name, ips in sorted((known or {}).items()):
        for ip in ips:
            if name:
                labels[ip] = t("sec_own_main", name=name) if name == main else name
    for ip in (known or {}).get("", []):
        labels.setdefault(ip, t("sec_own_main", name=main) if main else t("sec_own_bot"))
    return labels


def parse(out, now):
    """A probe's output → scan dict, or None when the server did not answer."""
    if out is None:
        return None
    sec = remote.sections(out)
    if "facts" not in sec:
        return None
    facts = dict(line.split("=", 1) for line in sec.get("facts", []) if "=" in line)
    containers = {}
    for line in sec.get("containers", []):
        parts = line.split("|")
        if len(parts) >= 2 and parts[0]:
            containers[parts[0]] = parts[1]
    return {
        "facts": facts,
        "logins": parse_logins(sec.get("logins", []), now),
        "ports": parse_ports(sec.get("ports", [])),
        "containers": containers,
        "addrs": parse_addrs(sec.get("addrs", [])),
        "images": sorted({i.strip() for i in sec.get("images", []) if i.strip() and i.strip() != "<none>"}),
        "suspicious": suspicious(sec.get("ps", []), sec.get("exe", [])),
        "docker": facts.get("docker") == "1",
    }


def observed(scan):
    """What goes into the baseline."""
    return {"ips": sorted({ip for _, _, ip, _ in scan["logins"]}), "ports": sorted(scan["ports"]),
            "containers": sorted(scan["containers"]), "images": scan["images"]}


def load_baseline(conf_dir):
    try:
        with open(os.path.join(conf_dir, FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and isinstance(data.get("servers"), dict) else {"servers": {}}
    except (OSError, ValueError):
        return {"servers": {}}


def save_baseline(conf_dir, data):
    from vps_director.setup import write_private
    write_private(os.path.join(conf_dir, FILE), json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def merge(old, new, now):
    """Accepting a scan: login IPs are added to the known ones, everything else is replaced."""
    ips = list(dict.fromkeys(list(old.get("ips") or []) + new["ips"]))[-MAX_IPS:]
    return dict(new, ips=ips, time=now)


def bad_container(name, image):
    return next((b for b in BAD_NAMES if b in image.lower() or b in name.lower()), None)


def findings(name, scan, base, t, own=()):
    """[(level, key, text)] for one server; level is "bad" (❌), "warn" (⚠️) or "info" (ℹ️: a new listener on
    localhost only — shown in /security, never an alert or a brief item). `base` None = first scan."""
    out = []
    facts = scan["facts"]
    if base is not None:
        known_ips = set(base.get("ips") or [])
        new_ips = {}
        for when, user, ip, _ in scan["logins"]:
            if ip not in known_ips and ip not in own:
                new_ips.setdefault(ip, set()).add(user)
        for ip, users in sorted(new_ips.items()):
            out.append(("warn", "sec:%s:ip:%s" % (name, ip), t("sec_p_ip", name=name, ip=ip, users=", ".join(sorted(users)))))
        for addr in sorted(set(scan["ports"]) - set(base.get("ports") or [])):
            proc = scan["ports"][addr]
            local = is_local(addr)
            out.append(("info" if local else "warn", "sec:%s:port:%s" % (name, addr),
                        t("sec_p_port_local" if local else "sec_p_port", name=name, addr=addr,
                          proc=" (%s)" % proc if proc else "")))
        for cname in sorted(set(scan["containers"]) - set(base.get("containers") or [])):
            out.append(("warn", "sec:%s:container:%s" % (name, cname),
                        t("sec_p_container", name=name, container=cname, image=scan["containers"][cname])))
        for image in sorted(set(scan["images"]) - set(base.get("images") or [])):
            out.append(("warn", "sec:%s:image:%s" % (name, image), t("sec_p_image", name=name, image=image)))
    for cname, image in sorted(scan["containers"].items()):
        if bad_container(cname, image):
            out.append(("bad", "sec:%s:badimage:%s" % (name, cname), t("sec_p_bad_container", name=name, container=cname,
                                                                        image=image)))
    for key, (kind, what, path) in sorted(scan["suspicious"].items()):
        out.append(("bad", "sec:%s:proc:%s" % (name, key), t("sec_p_proc_" + kind, name=name, what=what, path=path)))
    password, root = ssh_settings(facts)
    if password == "yes":
        out.append(("warn", "sec:%s:password" % name, t("sec_p_password", name=name)))
        if root == "yes":
            out.append(("bad", "sec:%s:rootpw" % name, t("sec_p_root_password", name=name)))
    if facts.get("f2b") in ("inactive", "failed", "unknown"):
        out.append(("warn", "sec:%s:f2b" % name, t("sec_p_f2b", name=name)))
    if facts.get("ua") == "off":
        out.append(("warn", "sec:%s:ua" % name, t("sec_p_ua", name=name)))
    if facts.get("sec_updates", "0").isdigit() and int(facts["sec_updates"]) > 0:
        out.append(("warn", "sec:%s:updates" % name, t("sec_p_updates", name=name, n=int(facts["sec_updates"]))))
    return out


def fmt_time(ts, tz):
    when = datetime.datetime.fromtimestamp(ts, tz) if tz else datetime.datetime.fromtimestamp(ts)
    return when.strftime("%d.%m %H:%M")


def ssh_settings(facts):
    """(password auth "yes"/"no"/"", root login "yes"/"no"/"keys"/"") — sshd defaults when read from files."""
    files = facts.get("sshd_src") == "files"
    password = facts.get("sshd_passwordauthentication", "yes" if files else "")
    root = facts.get("sshd_permitrootlogin", "prohibit-password" if files else "")
    return password, root if root in ("yes", "no", "") else "keys"


def block(server, scan, base, found, t, tz, own=None):
    """The text block of one server; `base` None = this scan became the baseline."""
    name = server["name"]
    title = "🔐 %s%s" % (name, " · " + server["where"] if server.get("where") else "")
    if scan is None:
        return "\n".join([title, "   ❌ " + t("srv_offline")])
    levels = {}
    info = sum(1 for level, _, _ in found if level == "info")
    found = [f for f in found if f[0] != "info"]
    for level, key, _ in found:
        kind = key.split(":")[2]
        levels[kind] = "bad" if level == "bad" or levels.get(kind) == "bad" else "warn"
    icon = lambda *kinds: "❌" if any(levels.get(k) == "bad" for k in kinds) else (  # noqa: E731
        "⚠️" if any(k in levels for k in kinds) else "✅")
    count = lambda *kinds: sum(1 for _, key, _ in found if key.split(":")[2] in kinds)  # noqa: E731
    news = lambda *kinds: t("sec_new", n=count(*kinds)) if count(*kinds) else t("sec_nothing_new") if base is not None else ""  # noqa: E731
    lines = [title]
    if base is None:
        obs = observed(scan)
        lines.append("   🆕 " + t("sec_first", ips=len(obs["ips"]), ports=len(obs["ports"]), containers=len(obs["containers"])))
    facts, logins = scan["facts"], scan["logins"]
    if facts.get("logsrc") == "none":
        lines.append("   ⚠️ " + t("sec_logins_unknown"))
    else:
        lines.append("   %s %s%s" % (icon("ip"), t("sec_logins", n=len(logins), ips=len({ip for _, _, ip, _ in logins})),
                                     t("sec_logins_new", n=count("ip")) if count("ip") else ""))
        own = own or {}
        groups = aggregate_logins(logins)
        mine = [g for g in groups if g[2] in own]
        if mine:
            lines.append("      • " + t("sec_own", items="; ".join(
                "%s ← %s ×%d" % (user, own[ip], n) for _, user, ip, n, _ in mine),
                last=fmt_time(mine[0][0], tz)))
        others = [g for g in groups if g[2] not in own]
        for when, user, ip, n, method in others[:SHOW_LOGINS]:
            lines.append("      • %s %s ← %s%s%s" % (fmt_time(when, tz), user, ip, " ×%d" % n if n > 1 else "",
                                                    " (%s)" % t("sec_by_password") if method == "password" else ""))
        if len(others) > SHOW_LOGINS:
            lines.append("      " + t("sec_more", n=len(others) - SHOW_LOGINS))
    password, root = ssh_settings(facts)
    if not password:
        lines.append("   ⚠️ " + t("sec_sshd_unknown"))
    else:
        root_text = t("sec_root_password") if "rootpw" in levels else t("sec_root_no") if root == "no" else t("sec_root_keys")
        lines.append("   %s %s" % (icon("password", "rootpw"),
                                   t("sec_ssh_line", password=t("sec_on") if password == "yes" else t("sec_off"), root=root_text)))
    lines.append("   %s %s%s%s" % (icon("port"), t("sec_ports", n=len(scan["ports"])), news("port"),
                                   t("sec_new_local", n=info) if info else ""))
    if scan["docker"]:
        lines.append("   %s %s%s" % (icon("container", "image", "badimage"),
                                     t("sec_containers", n=len(scan["containers"]), images=len(scan["images"])),
                                     news("container", "image")))
    lines.append("   %s %s" % (icon("proc"), t("sec_procs_bad", n=count("proc")) if count("proc") else t("sec_procs_ok")))
    extras = []
    if facts.get("f2b") == "absent":
        extras.append("· " + t("sec_f2b_absent"))
    elif facts.get("f2b"):
        extras.append("%s fail2ban" % icon("f2b"))
    if facts.get("ua"):
        extras.append("%s %s" % (icon("ua"), t("sec_ua")))
    if facts.get("updates", "").isdigit():
        extras.append("%s %s" % (icon("updates"), t("sec_updates", n=int(facts["updates"]),
                                                    sec=int(facts.get("sec_updates") or 0))))
    if extras:
        lines.append("   " + " · ".join(extras))
    return "\n".join(lines)


def report(rows, t, tz, stamp, certs=(), own=None):
    """The /security message. rows: [(server, scan, base, found)]; certs: [(level, text)] of sites."""
    problems = [(level, text) for _, _, _, found in rows for level, _, text in found if level != "info"] + list(certs)
    notes = [text for _, _, _, found in rows for level, _, text in found if level == "info"]
    online = sum(1 for _, scan, _, _ in rows if scan is not None)
    state = t("srv_all_fine") if not problems else t("srv_problems", n=len(problems))
    lines = [t("sec_head", stamp=stamp), t("srv_online", n=online, total=len(rows), state=state)]
    for server, scan, base, found in rows:
        lines += ["", block(server, scan, base, found, t, tz, own)]
    if certs:
        lines += ["", t("sec_certs_head")] + ["   %s %s" % ("❌" if level == "bad" else "⚠️", text) for level, text in certs]
    if problems:
        lines += ["", t("what_to_check")] + [remote.footer_line(level, text) for level, text in problems]
    if notes:
        lines += ["", t("sec_info_head")] + ["• ℹ️ " + text for text in notes]
    if problems or notes:
        if any(key.split(":")[2] in ACCEPTABLE for _, _, _, found in rows for _, key, _ in found):
            lines.append(t("sec_accept_hint"))
    return "\n".join(lines)


def scan_all(servers, runner, now):
    """One probe per server, in parallel (each capped by the runner's timeout)."""
    import concurrent.futures
    if not servers:
        return []
    with concurrent.futures.ThreadPoolExecutor(min(len(servers), 16)) as pool:
        return list(pool.map(lambda s: parse(runner(s.get("ssh") or "", PROBE), now), servers))
