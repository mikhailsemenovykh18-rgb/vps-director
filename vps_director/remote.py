"""Running read-only probe scripts on this machine or on another server over ssh.

A probe is a constant shell script from this package. On another server it runs through the system
`ssh` binary with BatchMode (never a password prompt) and a short connect timeout; the remote command
is always `sh -c '<constant script>'`. Data that comes from config files (a backup log path, …) is
never put into the command line: it goes on stdin, one item per line, and the script reads it with
`read -r`. The ssh alias must look like a host name, so it can never be read as an ssh option.
"""
import re
import shlex
import subprocess

SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8")
ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}")
TIMEOUT = 25


def valid_alias(alias):
    return isinstance(alias, str) and bool(ALIAS.fullmatch(alias))


def argv(ssh, script):
    """argv that runs `script` here (ssh == "") or on the host `ssh` (an alias from ~/.ssh/config)."""
    if not ssh:
        return ["sh", "-c", script]
    if not valid_alias(ssh):
        raise ValueError("bad ssh alias %r" % (ssh,))
    return ["ssh", *SSH_OPTS, "--", ssh, "sh -c " + shlex.quote(script)]


def run(ssh, script, stdin="", timeout=TIMEOUT):
    """The script's stdout, or None when it could not run: no ssh connection (exit 255), timeout,
    missing binary, bad alias. The script's own exit code does not matter — probes print what they
    found and skip what they cannot read."""
    try:
        res = subprocess.run(argv(ssh, script), input=stdin, capture_output=True, text=True, errors="replace",
                             timeout=timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if ssh and res.returncode == 255:
        return None
    return res.stdout


def sections(out):
    """'@@name' lines split a probe's output into {name: [lines]}."""
    found, current = {}, None
    for line in (out or "").splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return found


def footer_line(level, text):
    """One line of a "What to check" footer: "• ❌ text" (level "bad") or "• ⚠️ text" (anything else)."""
    return "• %s %s" % ("❌" if level == "bad" else "⚠️", text)
