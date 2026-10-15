"""Server inventory and the multi-server overview (/servers).

<conf>/servers.json is a list of servers:
    [{"ssh": "", "name": "main", "main": true, "where": "Frankfurt", "role": "bot, panel"},
     {"ssh": "web", "name": "web", "where": "Amsterdam", "role": "sites", "hosting_paid_until": "2026-11-01"}]
`ssh` is an alias from ~/.ssh/config ("" = the machine the bot runs on), `main` marks that machine.
Optional `ignore_ports: [10904, "10900-10999"]`: listening ports /security never tracks on that server.
Without the file the inventory is just this machine. Every probe is read-only and AI-free.
"""
import concurrent.futures
import datetime
import json
import os
import re
import socket

from vps_director import remote

FILE = "servers.json"
MAX_SERVERS = 30
LOAD_WARN = 1.0      # 5-minute load above cores × this is a problem
MEM_WARN = 90        # % used
DISK_WARN = 85       # % used
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# One pass per server, everything as key=value; nothing is changed on the server.
PROBE = r"""
D=docker; command -v docker >/dev/null 2>&1 || D=""
[ -n "$D" ] && ! docker ps >/dev/null 2>&1 && D="sudo -n docker"
echo "up=$(cut -d. -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f2 /proc/loadavg)"
# CPU utilization: two reads of /proc/stat half a second apart (busy share of all jiffies), not load average
awk '/^cpu /{t=0; for (i=2; i<=NF; i++) t+=$i; print t, $5+$6}' /proc/stat > /tmp/.vpsd-cpu.$$ 2>/dev/null
sleep 0.5
awk -v f=/tmp/.vpsd-cpu.$$ 'BEGIN{getline l < f; split(l, a, " ")} /^cpu /{t=0; for (i=2; i<=NF; i++) t+=$i; dt=t-a[1]; di=($5+$6)-a[2]; if (dt>0) printf "cpu_pct=%d\n", (100*(dt-di)+dt/2)/dt}' /proc/stat 2>/dev/null
rm -f /tmp/.vpsd-cpu.$$
echo "cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{print "mem_total="t; print "mem_avail="a}' /proc/meminfo
df -Pk / | awk 'NR==2{print "disk_total="$2; print "disk_used="$3}'
# fwupd-refresh always "fails" on virtual machines (no firmware): noise
echo "failed=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -v '^fwupd-refresh' | paste -sd, -)"
if [ -n "$D" ]; then
  $D ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | awk -F'|' '
    {n++}
    $2 ~ /^Up/ && $2 !~ /unhealthy/ {ok++; next}
    $2 ~ /^Exited \(0\)/ || $2 ~ /^Created/ {done_ = done_ (done_ ? "," : "") $1; next}
    {bad = bad (bad ? "," : "") $1; st = $2; gsub(/[,~]/, " ", st); why = why (why ? "," : "") $1 "~" st}
    END {print "dk_total=" n+0; print "dk_ok=" ok+0; print "dk_bad=" bad; print "dk_why=" why; print "dk_done=" done_}'
fi
"""

# The containers of one server, for the dashboard's server page (read-only).
CONTAINERS_PROBE = r"""
D=docker; command -v docker >/dev/null 2>&1 || exit 0
docker ps >/dev/null 2>&1 || D="sudo -n docker"
$D ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null | head -n 200
"""

# Restarting one container — only after the owner pressed Allow in Telegram. The name comes on
# stdin (never in the command line) and is checked again here.
RESTART_SCRIPT = r"""
read -r c
case "$c" in ''|-*|*[!A-Za-z0-9_.-]*) echo "bad container name"; echo "rc=2"; exit 0;; esac
D=docker; docker ps >/dev/null 2>&1 || D="sudo -n docker"
out=$($D restart "$c" 2>&1); rc=$?
printf '%s\n' "$out" | tail -n 3 | cut -c1-200
echo "rc=$rc"
"""
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def parse_containers(out):
    """[{"name", "image", "status", "state": "up" | "done" | "bad"}] from CONTAINERS_PROBE output."""
    found = []
    for line in (out or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not CONTAINER.fullmatch(parts[0]):
            continue
        status = parts[2].strip()[:80]
        state = ("up" if status.startswith("Up") and "unhealthy" not in status else
                 "done" if status.startswith(("Exited (0)", "Created")) else "bad")
        found.append({"name": parts[0], "image": parts[1].strip()[:120], "status": status, "state": state})
    return found


def local_name():
    return socket.gethostname().split(".")[0] or "this server"


def load(conf_dir):
    """(servers, warnings, configured). Bad entries are skipped with a warning; without servers.json the
    inventory is this machine only (configured=False)."""
    path = os.path.join(conf_dir, FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [{"ssh": "", "name": local_name(), "main": True, "where": "", "role": ""}], [], False
    except (OSError, ValueError) as exc:
        return ([{"ssh": "", "name": local_name(), "main": True, "where": "", "role": ""}],
                ["%s: %s" % (FILE, exc)], False)
    servers, warnings, names = [], [], set()
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            warnings.append("%s #%d: not an object" % (FILE, i + 1))
            continue
        ssh = item.get("ssh") or ""
        if ssh and not remote.valid_alias(ssh):
            warnings.append("%s #%d: bad ssh alias %r" % (FILE, i + 1, str(ssh)[:40]))
            continue
        name = " ".join(str(item.get("name") or ssh or local_name()).replace(":", "-").split())[:40]
        if name in names:
            warnings.append("%s #%d: duplicate name %s" % (FILE, i + 1, name))
            continue
        paid = str(item.get("hosting_paid_until") or "")
        if paid and not DATE.fullmatch(paid):
            warnings.append("%s %s: hosting_paid_until must be YYYY-MM-DD" % (FILE, name))
            paid = ""
        names.add(name)
        ignore, bad = port_ranges(item.get("ignore_ports"))
        warnings += ["%s %s: ignore_ports: bad item %r" % (FILE, name, str(b)[:20]) for b in bad]
        entry = {"ssh": ssh, "name": name, "main": bool(item.get("main")),
                 "where": " ".join(str(item.get("where") or "").split())[:60],
                 "role": " ".join(str(item.get("role") or "").split())[:120], "hosting_paid_until": paid}
        if "ignore_ports" in item:
            entry["ignore_ports"] = ignore
        servers.append(entry)
        if len(servers) >= MAX_SERVERS:
            break
    if not isinstance(raw, list):
        warnings.append("%s: expected a list of servers" % FILE)
    if not servers:
        servers = [{"ssh": "", "name": local_name(), "main": True, "where": "", "role": ""}]
    if not any(s["main"] for s in servers):  # the machine the bot runs on, when nobody is marked
        next((s for s in servers if not s["ssh"]), {})["main"] = True
    return servers, warnings, True


def port_ranges(value):
    """`ignore_ports` ([10904, "10900-10999", "22"]) → ([[lo, hi]], [bad items]). Not a list = one bad item."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [value]
    out, bad = [], []
    for item in value[:100]:
        lo = hi = None
        if isinstance(item, int) and not isinstance(item, bool):
            lo = hi = item
        elif isinstance(item, str):
            match = re.fullmatch(r"\s*(\d{1,5})\s*(?:-\s*(\d{1,5})\s*)?", item)
            if match:
                lo, hi = int(match.group(1)), int(match.group(2) or match.group(1))
        if lo is None or not 1 <= lo <= hi <= 65535:
            bad.append(item)
        else:
            out.append([lo, hi])
    return out, bad


def remotes(servers):
    return [s for s in servers if s.get("ssh")]


def ssh_config_hosts(path=None):
    """Concrete host aliases from ~/.ssh/config (wildcards and negations skipped), in file order."""
    path = path or os.path.expanduser("~/.ssh/config")
    hosts = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                words = line.split("#", 1)[0].split()
                if len(words) >= 2 and words[0].lower() == "host":
                    for alias in words[1:]:
                        if remote.valid_alias(alias) and alias not in hosts:
                            hosts.append(alias)
    except OSError:
        pass
    return hosts


def raw(conf_dir):
    """servers.json exactly as written (keys this version does not know are kept) — for edits.
    [] without the file; ValueError when it cannot be read as a list."""
    try:
        with open(os.path.join(conf_dir, FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError("%s: expected a list of servers" % FILE)
    return data


def write(conf_dir, servers):
    from vps_director.setup import write_private  # setup imports this module for the wizard
    write_private(os.path.join(conf_dir, FILE), json.dumps(servers, ensure_ascii=False, indent=1) + "\n")


def probe(server, runner):
    """key=value data of one server, or None when it did not answer."""
    out = runner(server.get("ssh") or "", PROBE)
    data = dict(line.split("=", 1) for line in (out or "").splitlines() if "=" in line)
    try:
        int(data["up"]), float(data["load"]), int(data["cores"])
        int(data["mem_total"]), int(data["mem_avail"]), int(data["disk_total"]), int(data["disk_used"])
    except (KeyError, ValueError):
        return None
    return data


def probe_all(servers, runner):
    """Probes in parallel; each one is capped by the runner's timeout (25 s)."""
    if not servers:
        return []
    with concurrent.futures.ThreadPoolExecutor(min(len(servers), 16)) as pool:
        return list(pool.map(lambda s: probe(s, runner), servers))


def gb(kb):
    return int(kb) / 1024 / 1024


def plural(n, forms, lang):
    """forms: "core|cores" or "ядро|ядра|ядер"."""
    words = forms.split("|")
    if lang == "ru" and len(words) == 3:
        if n % 10 == 1 and n % 100 != 11:
            return words[0]
        return words[1] if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else words[2]
    return words[0] if n == 1 else words[-1]


def uptime(sec, t):
    sec = int(sec)
    days, hours = sec // 86400, sec % 86400 // 3600
    return t("srv_up_days", d=days, h=hours) if days else t("srv_up_hours", h=hours, m=sec % 3600 // 60)


def split(value):
    return [v for v in (value or "").split(",") if v]


def block(server, data, t, lang):
    """(text block, [(key, problem text)]) for one server."""
    name = server["name"]
    icon = "⭐" if server.get("main") else "🖥"
    title = "%s %s%s%s" % (icon, name, t("srv_main") if server.get("main") else "",
                           " · " + server["where"] if server.get("where") else "")
    role = ["   " + server["role"]] if server.get("role") else []
    if data is None:
        return "\n".join([title, "   ❌ " + t("srv_offline")] + role), [("srv:%s:offline" % name, t("srv_p_offline", name=name))]
    problems = []
    load, cores = float(data["load"]), max(1, int(data["cores"]))
    mem_total = max(1, int(data["mem_total"]))
    mem_used_kb = max(0, mem_total - int(data["mem_avail"]))
    mem_pct = round(mem_used_kb * 100 / mem_total)
    disk_total = max(1, int(data["disk_total"]))
    disk_pct = round(int(data["disk_used"]) * 100 / disk_total)
    warn = lambda bad: "⚠️ " if bad else ""  # noqa: E731
    cores_word = plural(cores, t("srv_cores"), lang)
    if load > cores * LOAD_WARN:
        problems.append(("srv:%s:load" % name, t("srv_p_load", name=name, load=load, cores=cores, word=cores_word)))
    if mem_pct >= MEM_WARN:
        problems.append(("srv:%s:mem" % name, t("srv_p_mem", name=name, pct=mem_pct)))
    if disk_pct >= DISK_WARN:
        problems.append(("srv:%s:disk" % name, t("srv_p_disk", name=name, pct=disk_pct)))
    lines = [title] + role + [
        "   " + t("srv_line_up", up=uptime(data["up"], t), warn=warn(load > cores * LOAD_WARN), load=load, cores=cores,
                  word=cores_word),
        "   " + warn(mem_pct >= MEM_WARN) + t("srv_line_mem", used=gb(mem_used_kb), total=gb(mem_total), pct=mem_pct),
        "   " + warn(disk_pct >= DISK_WARN) + t("srv_line_disk", used=gb(data["disk_used"]), total=gb(disk_total),
                                                pct=disk_pct)]
    failed = split(data.get("failed"))
    if failed:
        lines.append("   ⚠️ " + t("srv_line_failed", units=", ".join(failed)))
        problems.append(("srv:%s:failed" % name, t("srv_p_failed", name=name, units=", ".join(failed))))
    if "dk_total" in data:
        try:
            total, ok = int(data["dk_total"]), int(data.get("dk_ok") or 0)
        except ValueError:
            total, ok = 0, 0
        bad = split(data.get("dk_bad"))
        if bad:
            why = "; ".join(container_issue(n, s, t) for n, s in bad_containers(data))
            lines.append("   ⚠️ " + t("srv_line_dk_bad", ok=ok, total=total, bad=why))
            problems.append(("srv:%s:containers" % name, t("srv_p_containers", name=name, bad=why)))
        elif total:
            lines.append("   " + t("srv_line_dk_ok", ok=ok) + (t("srv_line_dk_done", n=total - ok) if total > ok else ""))
    return "\n".join(lines), problems


def bad_containers(data):
    """[(name, docker status)] of the containers in trouble; status "" from an older probe."""
    why = dict(item.split("~", 1) for item in split(data.get("dk_why")) if "~" in item)
    return [(n, why.get(n, "")) for n in split(data.get("dk_bad"))]


def container_issue(name, status, t):
    """"worker: stopped (exit code 1)" — what is wrong with a container, in words, from `docker ps` status."""
    exited = re.match(r"Exited \((\d+)\)", status)
    if exited:
        what = t("dk_exited", code=exited.group(1))
    elif "unhealthy" in status:
        what = t("dk_unhealthy")
    elif status.startswith("Restarting"):
        what = t("dk_restarting")
    elif status.startswith("Dead"):
        what = t("dk_dead")
    elif status.startswith("Paused") or "(Paused)" in status:
        what = t("dk_paused")
    else:
        return name + (": " + status if status else "")
    return "%s: %s" % (name, what)


def paid_reminders(servers, now, t, days=7):
    """[(key, text)] for servers whose hosting is paid until a date at most `days` days away (or past)."""
    out = []
    today = datetime.date.fromtimestamp(now)
    for server in servers:
        paid = server.get("hosting_paid_until")
        if not paid:
            continue
        try:
            left = (datetime.date.fromisoformat(paid) - today).days
        except ValueError:
            continue
        if left <= days:
            out.append(("paid:%s:%s" % (server["name"], paid),
                        t("srv_paid_over", name=server["name"], date=paid) if left < 0
                        else t("srv_paid_soon", name=server["name"], date=paid, days=left)))
    return out


def report(servers, results, t, lang, stamp, warnings=()):
    """(the /servers message, [(key, problem)]): header, one block per server, footer 'What to check'."""
    blocks, problems = [], []
    for server, data in zip(servers, results):
        text, found = block(server, data, t, lang)
        blocks.append(text)
        problems += found
    online = sum(1 for r in results if r is not None)
    state = t("srv_all_fine") if not problems else t("srv_problems", n=len(problems))
    head = t("srv_head", stamp=stamp) + "\n" + t("srv_online", n=online, total=len(servers), state=state)
    text = head + "\n\n" + "\n\n".join(blocks)
    if problems:
        text += "\n\n" + t("what_to_check") + "\n" + "\n".join(
            remote.footer_line("bad" if key.endswith(":offline") else "warn", text) for key, text in problems)
    if warnings:
        text += "\n\n" + "\n".join("⚠️ " + w for w in warnings)
    return text, problems


def summary_line(servers, results, problems, t):
    """One line for the morning brief."""
    online = sum(1 for r in results if r is not None)
    if not problems:
        return t("brief_servers_ok", n=online, total=len(servers))
    return t("brief_servers_bad", n=online, total=len(servers), items="\n".join("  • " + p for _, p in problems[:10]))
