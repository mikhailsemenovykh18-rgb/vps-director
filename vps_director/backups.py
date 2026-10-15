"""Backups from <conf>/backups.json (/backups): is the newest good copy fresh enough?

    [{"type": "log", "name": "db dump", "path": "/var/log/db-backup.log", "ok": "backup (done|ok)",
      "fail": "error|failed", "max_age": "26h", "server": "web"},
     {"type": "dir", "name": "dumps", "path": "/srv/dumps", "pattern": "*.sql.gz", "max_age": "2d"},
     {"type": "restic", "name": "offsite", "repository": "sftp:backup:/srv/restic",
      "password_file": "/root/.restic-pass", "env_file": "/root/.restic.env", "max_age": "8d",
      "verify_paths": ["/etc"]}]

`log`: the last line matching `ok` or `fail` (regular expressions) decides; its time is a date in the
line or the file's mtime. `dir`: the newest file (2 levels deep, optional glob `pattern`). `log` and
`dir` work on this machine or on a server from servers.json (`server`), read-only; the path goes to
the probe on stdin, never into a command line. `restic` (on this machine): `restic snapshots
--latest 1 --json --no-lock` with RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE (and the RESTIC_* /
cloud keys of `env_file`) in the environment — never in argv, never shown. `/backups verify`
restores the latest snapshot's `verify_paths` into a temporary folder (`--no-lock`: the repository
is only read), counts the files and removes the folder.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from vps_director import remote
from vps_director.schedule import parse_duration

FILE = "backups.json"
TAIL_BYTES = 256 * 1024
DEFAULT_MAX_AGE = 26 * 3600
RESTIC_TIMEOUT = 120
VERIFY_TIMEOUT = 1800
ENV_KEYS = re.compile(r"(RESTIC|AWS|B2|AZURE|GOOGLE|OS|ST|RCLONE)_[A-Z0-9_]+")
DATE_IN_LINE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)")

LOG_PROBE = r"""
IFS= read -r p
S=""; [ -r "$p" ] || { sudo -n true 2>/dev/null && S="sudo -n"; }
m=$($S stat -c %Y -- "$p" 2>/dev/null) || { echo "@@meta"; echo "missing=1"; exit 0; }
echo "@@meta"; echo "mtime=$m"
echo "@@tail"; $S tail -c 262144 -- "$p" 2>/dev/null
"""

DIR_PROBE = r"""
IFS= read -r p; IFS= read -r pat
S=""; { [ -r "$p" ] && [ -x "$p" ]; } || { sudo -n true 2>/dev/null && S="sudo -n"; }
$S test -d "$p" || { echo "missing=1"; exit 0; }
L=$($S find "$p" -maxdepth 2 -type f -name "${pat:-*}" -printf '%T@ %s %P\n' 2>/dev/null)
echo "newest=$(printf '%s\n' "$L" | grep . | sort -n | tail -n 1)"
echo "count=$(printf '%s\n' "$L" | grep -c .)"
echo "total=$(printf '%s\n' "$L" | awk 'NF{s+=$2}END{print s+0}')"
"""


def max_age(value):
    """"8d", "26h", "1d12h" or a number of days → seconds; None if it makes no sense."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value * 86400)
    if isinstance(value, str):
        return parse_duration(value.replace(" ", ""))
    return None


def load(conf_dir):
    """(entries, warnings, configured)."""
    path = os.path.join(conf_dir, FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [], [], False
    except (OSError, ValueError) as exc:
        return [], ["%s: %s" % (FILE, exc)], False
    entries, warnings, names = [], [], set()
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        where = "%s #%d" % (FILE, i + 1)
        if not isinstance(item, dict) or item.get("type") not in ("log", "dir", "restic"):
            warnings.append("%s: type must be log, dir or restic" % where)
            continue
        name = " ".join(str(item.get("name") or item.get("path") or item.get("repository") or "").replace(":", "-").split())[:40]
        if not name or name in names:
            warnings.append("%s: missing or duplicate name" % where)
            continue
        entry = {"type": item["type"], "name": name, "server": str(item.get("server") or ""),
                 "max_age": max_age(item.get("max_age", DEFAULT_MAX_AGE / 86400)) or DEFAULT_MAX_AGE}
        if item.get("max_age") is not None and max_age(item.get("max_age")) is None:
            warnings.append("%s: max_age like 26h or 8d" % where)
        if item["type"] in ("log", "dir"):
            path = str(item.get("path") or "")
            if not path.startswith("/") or "\n" in path:
                warnings.append("%s: path must be absolute" % where)
                continue
            entry["path"] = path
        if item["type"] == "log":
            try:
                entry["ok"] = re.compile(str(item.get("ok") or r"(?i)\b(ok|success|succeeded|done|completed)\b"))
                entry["fail"] = re.compile(str(item["fail"])) if item.get("fail") else None
            except re.error as exc:
                warnings.append("%s: bad regular expression: %s" % (where, exc))
                continue
        if item["type"] == "dir":
            entry["pattern"] = str(item.get("pattern") or "*").replace("\n", "")
        if item["type"] == "restic":
            if not item.get("repository") or not item.get("password_file"):
                warnings.append("%s: restic needs repository and password_file" % where)
                continue
            entry.update(repository=str(item["repository"]), password_file=str(item["password_file"]),
                         env_file=str(item.get("env_file") or ""), host=str(item.get("host") or ""),
                         verify_paths=[str(p) for p in item.get("verify_paths") or [] if str(p).startswith("/")],
                         verify_dir=str(item.get("verify_dir") or ""))
        names.add(name)
        entries.append(entry)
    if not isinstance(raw, list):
        warnings.append("%s: expected a list" % FILE)
    return entries, warnings, True


def line_time(line, fallback, now):
    """A date-time written in the line (local time), else `fallback`."""
    match = DATE_IN_LINE.search(line)
    if match:
        text = "%s %s" % (match.group(1), match.group(2) if len(match.group(2)) == 8 else match.group(2) + ":00")
        try:
            ts = time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
            if ts <= now + 3600:
                return ts
        except (ValueError, OverflowError):
            pass
    return fallback


def check_log(entry, out, now, mask):
    """{state: ok|fail|none|missing|error, time, line}."""
    if out is None:
        return {"state": "error", "why": "no connection"}
    sec = remote.sections(out)
    meta = dict(line.split("=", 1) for line in sec.get("meta", []) if "=" in line)
    if meta.get("missing") or "mtime" not in meta:
        return {"state": "missing"}
    try:
        mtime = float(meta["mtime"])
    except ValueError:
        mtime = None
    for line in reversed(sec.get("tail", [])):
        failed = entry["fail"] is not None and entry["fail"].search(line)
        if failed or entry["ok"].search(line):
            return {"state": "fail" if failed else "ok", "time": line_time(line, mtime, now),
                    "line": mask(" ".join(line.split()))[:120]}
    return {"state": "none", "time": mtime}


def check_dir(out):
    if out is None:
        return {"state": "error", "why": "no connection"}
    data = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if data.get("missing"):
        return {"state": "missing"}
    newest = (data.get("newest") or "").split(" ", 2)
    try:
        count = int(data.get("count") or 0)
        if len(newest) < 3:
            return {"state": "none", "count": count}
        return {"state": "ok", "time": float(newest[0]), "size": int(newest[1]), "file": newest[2], "count": count,
                "total": int(data.get("total") or 0)}
    except ValueError:
        return {"state": "error", "why": "unexpected output"}


def restic_env(entry, base=None):
    """The environment for restic: the repository and password file, plus the allowed keys of env_file."""
    env = dict(base if base is not None else os.environ)
    if entry.get("env_file"):
        with open(entry["env_file"], encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                key, sep, value = line.partition("=")
                if sep and ENV_KEYS.fullmatch(key.strip()):
                    env[key.strip()] = value.strip().strip("'\"")
    env.update(RESTIC_REPOSITORY=entry["repository"], RESTIC_PASSWORD_FILE=entry["password_file"])
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    return env


def run_restic(argv, env, timeout=RESTIC_TIMEOUT):
    """(exit code, stdout, stderr); exit code None when it could not run or timed out."""
    try:
        res = subprocess.run(argv, env=env, capture_output=True, text=True, errors="replace", timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %d s" % timeout
    except OSError as exc:
        return None, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def snapshot_time(text):
    """restic's RFC 3339 time with nanoseconds → unix time."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?", str(text))
    if not match:
        return None
    frac = (match.group(2) or "")[:7]
    if frac:  # Python 3.10's fromisoformat takes only 3 or 6 digits
        frac = frac.ljust(7, "0")
    try:
        return datetime.datetime.fromisoformat(match.group(1) + frac + (match.group(3) or "Z").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def restic_error(stderr, mask):
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    text = re.sub(r"://[^/@\s]+@", "://***@", lines[-1] if lines else "restic failed")
    return mask(text)[:150]


def check_restic(entry, runner, restic, mask, detailed=False):
    if not restic:
        return {"state": "error", "why": "restic is not installed"}
    try:
        env = restic_env(entry)
    except OSError as exc:
        return {"state": "error", "why": "env_file: %s" % (exc.strerror or exc)}
    argv = [restic, "snapshots", "--latest", "1", "--json", "--no-lock"] + (["--host", entry["host"]] if entry["host"] else [])
    code, out, err = runner(argv, env, RESTIC_TIMEOUT)
    if code != 0:
        return {"state": "error", "why": restic_error(err, mask)}
    try:
        snaps = [s for s in json.loads(out or "[]") if isinstance(s, dict)]
    except ValueError:
        return {"state": "error", "why": "unexpected output from restic"}
    snaps = [(snapshot_time(s.get("time")), s) for s in snaps]
    snaps = [(ts, s) for ts, s in snaps if ts]
    if not snaps:
        return {"state": "none"}
    ts, snap = max(snaps, key=lambda x: x[0])
    summary = snap.get("summary") if isinstance(snap.get("summary"), dict) else {}
    size = summary.get("total_bytes_processed")
    if size is None and detailed:
        code, out, _ = runner([restic, "stats", "latest", "--json", "--no-lock"], env, RESTIC_TIMEOUT)
        try:
            size = json.loads(out).get("total_size") if code == 0 else None
        except (ValueError, AttributeError):
            size = None
    return {"state": "ok", "time": ts, "size": size if isinstance(size, int) else None,
            "id": str(snap.get("short_id") or snap.get("id") or "")[:12]}


def verify(entry, runner, restic, mask, tmp_root=None):
    """Restore the latest snapshot's verify_paths into a temp folder, count files and bytes, remove it.
    {state: ok|empty|error, files, bytes, seconds, why}."""
    if not restic:
        return {"state": "error", "why": "restic is not installed"}
    if not entry.get("verify_paths"):
        return {"state": "error", "why": "no verify_paths"}
    try:
        env = restic_env(entry)
    except OSError as exc:
        return {"state": "error", "why": "env_file: %s" % (exc.strerror or exc)}
    tmp = tempfile.mkdtemp(prefix="director-verify-", dir=entry.get("verify_dir") or tmp_root)
    start = time.monotonic()
    try:
        argv = [restic, "restore", "latest", "--no-lock", "--target", tmp] + (
            ["--host", entry["host"]] if entry["host"] else [])
        for path in entry["verify_paths"]:
            argv += ["--include", path]
        code, _, err = runner(argv, env, VERIFY_TIMEOUT)
        if code != 0:
            return {"state": "error", "why": restic_error(err, mask)}
        files, size = 0, 0
        for folder, _, names in os.walk(tmp):
            for name in names:
                files += 1
                try:
                    size += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
        return {"state": "ok" if files else "empty", "files": files, "bytes": size,
                "seconds": time.monotonic() - start}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%d B" % n


def age(seconds, t):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return t("age_min", n=max(1, seconds // 60))
    if seconds < 2 * 86400:
        return t("age_h", n=seconds // 3600)
    return t("age_d", n=seconds // 86400)


def assess(entry, res, now, t):
    """(icon, line, [(key, problem)])."""
    name, kind = entry["name"], entry["type"]
    tag = kind + (" · " + entry["server"] if entry.get("server") else "")
    limit = age(entry["max_age"], t)
    state = res.get("state")
    key = "backup:%s:" % name
    if state == "error":
        return "❌", "❌ %s — %s · %s" % (name, t("bk_error", why=res.get("why", "?")), tag), [
            (key + "error", t("bk_p_error", name=name, why=res.get("why", "?")))]
    if state == "missing":
        return "❌", "❌ %s — %s · %s" % (name, t("bk_missing", path=entry.get("path", "")), tag), [
            (key + "missing", t("bk_p_missing", name=name, path=entry.get("path", "")))]
    if state == "none":
        return "❌", "❌ %s — %s · %s" % (name, t("bk_none"), tag), [(key + "none", t("bk_p_none", name=name))]
    when = res.get("time")
    ago = age(now - when, t) if when else "?"
    if state == "fail":
        return "❌", "❌ %s — %s · %s\n   %s" % (name, t("bk_failed", ago=ago), tag, res.get("line", "")), [
            (key + "fail", t("bk_p_fail", name=name, ago=ago, line=res.get("line", "")))]
    details = []
    if kind == "dir":
        details.append(t("bk_files", n=res.get("count", 0)))
        if res.get("size") is not None:
            details.append(t("bk_latest", size=human_size(res["size"])))
        if res.get("total"):
            details.append(t("bk_total", size=human_size(res["total"])))
    elif res.get("size"):
        details.append(human_size(res["size"]))
    extra = "".join(" · " + d for d in details)
    if when is None or now - when > entry["max_age"]:
        return "⚠️", "⚠️ %s — %s%s · %s" % (name, t("bk_old", ago=ago, limit=limit), extra, tag), [
            (key + "stale", t("bk_p_stale", name=name, ago=ago, limit=limit))]
    return "✅", "✅ %s — %s%s · %s" % (name, t("bk_ok", ago=ago), extra, tag), []


def report(entries, results, now, t, stamp, warnings=()):
    lines, problems, fine = [], [], 0
    for entry, res in zip(entries, results):
        icon, line, found = assess(entry, res, now, t)
        fine += 1 if icon == "✅" else 0
        lines.append(line)
        problems += found
    state = t("srv_all_fine") if not problems else t("srv_problems", n=len(problems))
    out = [t("bk_head", stamp=stamp), t("bk_fine", n=fine, total=len(entries), state=state), ""] + lines
    if problems:
        out += ["", t("what_to_check")] + [
            remote.footer_line("warn" if key.endswith(":stale") else "bad", text) for key, text in problems]
    if any(e["type"] == "restic" and e.get("verify_paths") for e in entries):
        out += ["", t("bk_verify_hint")]
    if warnings:
        out += [""] + ["⚠️ " + w for w in warnings]
    return "\n".join(out)


def summary_line(entries, results, now, t):
    """One line for the morning brief."""
    bad = []
    for entry, res in zip(entries, results):
        icon, _, found = assess(entry, res, now, t)
        if found:
            bad.append("%s %s" % (icon, found[0][1]))
    if not bad:
        ages = [now - r["time"] for r in results if r.get("time")]
        return t("brief_backups_ok", n=len(entries), ago=age(max(ages), t) if ages else "?")
    return t("brief_backups_bad", n=len(bad), total=len(entries), items="\n".join("  • " + b for b in bad))
