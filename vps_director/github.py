"""GitHub: install commands from a public repository's README, and `gh` shortcuts.

A message that is just a github.com repository link → the README is fetched from
raw.githubusercontent.com (public repositories, no token) and candidate install commands are
pulled out of it: shell code blocks under headings like Install / Quick start / Getting started,
plus well-known install lines anywhere (`docker compose up`, `pip install`, `npm i -g`,
`curl … | sh`, `go install`, …). Each command gets risk notes. Nothing here runs anything: the bot
shows the commands and offers to hand them to the agent as a normal safe-mode task.
"""
import re
import urllib.error
import urllib.parse
import urllib.request

RAW = "https://raw.githubusercontent.com/%s/%s/HEAD/%s"
README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "README.rst", "README.txt", "README")
MAX_README = 512 * 1024
MAX_COMMANDS = 12
MAX_COMMAND = 300

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
NAME = r"[A-Za-z0-9._-]{1,100}"
REPO_LINK = re.compile(r"^\s*<?(?:https?://)?(?:www\.)?github\.com/(" + OWNER + r")/(" + NAME + r")"
                       r"(?:/(?:tree|blob)/[^\s>]*)?/?(?:#[\w.-]*)?>?\s*$", re.I)
REPO_ARG = re.compile(OWNER + "/" + NAME)
VERSION = re.compile(r"v?\d{1,4}(?:\.\d{1,6}){0,3}(?:[-+][0-9A-Za-z.-]{1,30})?")

INSTALL_HEADING = re.compile(r"(?i)\b(install|installation|installing|quick[\s-]?start|getting[\s-]started|"
                             r"set[\s-]?up|setup|how to run|deploy(?:ment)?|run(?:ning)? (?:it|locally|with docker))\b")
SHELL_LANGS = {"", "sh", "bash", "shell", "console", "zsh", "terminal", "shell-session", "shellsession", "fish"}
KNOWN = re.compile(r"(?i)^(?:sudo\s+)?(?:docker[\s-]compose\s+(?:-f\s+\S+\s+)?up|docker\s+run|pipx?3?\s+install|"
                   r"python3?\s+-m\s+pip\s+install|uv\s+(?:tool|pip)\s+install|npm\s+(?:i|install)\s+(?:-g|--global)|"
                   r"yarn\s+global\s+add|pnpm\s+(?:add|i|install)\s+-g|go\s+install|cargo\s+install|brew\s+install|"
                   r"apt(?:-get)?\s+install|git\s+clone|make\s+install|helm\s+install|snap\s+install|"
                   r"(?:curl|wget)\b.*\|\s*(?:sudo\s+)?(?:-E\s+)?(?:ba|z|da|k)?sh\b)")

RISKS = (  # (key, pattern): the key is a text in texts.py ("risk_<key>")
    ("pipe_shell", re.compile(r"(?i)\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:-\S+\s+)*(?:ba|z|da|k|fi)?sh\b|"
                              r"(?:ba|z)?sh\s+(?:-c\s+)?[\"']?\$\((?:curl|wget)|(?:ba|z)?sh\s+<\(\s*(?:curl|wget)")),
    ("sudo", re.compile(r"(?:^|[;&|(]\s*)sudo\b")),
    ("rm", re.compile(r"\brm\s+(?:-\w*[rRf]\w*\s+)+")),
    ("chmod", re.compile(r"\bchmod\s+(?:-R\s+)?0?777\b")),
    ("npm_global", re.compile(r"(?i)\bnpm\s+(?:i|install)\b.*\s(?:-g|--global)\b|\byarn\s+global\s+add|\bpnpm\s+\S+\s+-g\b")),
    ("pip_system", re.compile(r"(?i)\b(?:pip3?|python3?\s+-m\s+pip)\s+install\b(?!.*--user)")),
    ("break_system", re.compile(r"--break-system-packages")),
    ("docker", re.compile(r"(?i)\bdocker[\s-]compose\b.*\bup\b|\bdocker\s+run\b")),
    ("system_pkg", re.compile(r"(?i)\b(?:apt(?:-get)?|dnf|yum|pacman|apk|snap)\s+(?:-\S+\s+)*(?:install|add|-S)\b")),
    ("binary", re.compile(r"(?i)\b(?:go|cargo)\s+install\b")),
    ("wildcard", re.compile(r"\*")),
    ("chain", re.compile(r"&&|\|\||;")),
)


def parse_repo_link(text):
    """(owner, repo) when the whole message is a github.com repository link, else None."""
    match = REPO_LINK.match(text or "")
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not repo or repo in (".", "..") or owner.lower() in ("orgs", "settings", "topics", "marketplace", "features"):
        return None
    return owner, repo


def fetch_url(url, limit=MAX_README, timeout=15):
    """GET a raw.githubusercontent.com URL; bytes (at most `limit`), or raises."""
    if not url.startswith("https://raw.githubusercontent.com/"):
        raise ValueError("only raw.githubusercontent.com")
    req = urllib.request.Request(url, headers={"User-Agent": "vps-director"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit + 1)


def fetch_readme(owner, repo, fetch=fetch_url):
    """(file name, text) of the repository's README, or None (private, missing, network error)."""
    for name in README_NAMES:
        url = RAW % (urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe=""), name)
        try:
            data = fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            return None
        except (OSError, ValueError):
            return None
        if data is not None:
            return name, data[:MAX_README].decode("utf-8", "replace")
    return None


def block_commands(lines):
    """Commands of one shell code block: `$ ` prompts (if any line has one, other lines are output),
    backslash continuations joined, comments and blank lines skipped."""
    prompted = any(re.match(r"\s*[$%] ", ln) for ln in lines)
    out, current = [], ""
    for line in lines:
        line = line.rstrip()
        if prompted:
            match = re.match(r"\s*[$%] (.*)", line)
            if not match and not current:
                continue
            line = match.group(1) if match else line
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1].rstrip() + " "
            continue
        out.append((current + stripped).strip())
        current = ""
    if current.strip():
        out.append(current.strip())
    return out


def extract_commands(readme):
    """Candidate install commands, in README order, without duplicates (at most MAX_COMMANDS)."""
    found, headings, fence, lang, block = [], [], None, "", []  # headings: the (level, text) path to here
    for line in readme.splitlines():
        if fence:
            if line.strip().startswith(fence):
                if lang in SHELL_LANGS:
                    for cmd in block_commands(block):
                        if any(INSTALL_HEADING.search(h) for _, h in headings) or KNOWN.search(cmd):
                            found.append(cmd)
                fence, block = None, []
            else:
                block.append(line)
            continue
        match = re.match(r"\s{0,3}(```+|~~~+)\s*([\w+-]*)", line)
        if match:
            fence, lang = match.group(1), match.group(2).lower()
            continue
        head = re.match(r"\s{0,3}(#{1,6})\s+(.*)", line)
        if head:  # "### From source" under "## Installation" is still installation
            level = len(head.group(1))
            headings = [h for h in headings if h[0] < level] + [(level, head.group(2))]
            continue
    unique = []
    for cmd in found:
        cmd = cmd[:MAX_COMMAND]
        if cmd not in unique and not re.fullmatch(r"(?i)cd\s+\S+|ls.*|echo\b.*|pwd|exit", cmd):
            unique.append(cmd)
    return unique[:MAX_COMMANDS]


def risk_notes(command):
    """Keys of the risks of one shell command (see RISKS); "chain" only when not already a pipe-to-shell."""
    keys = [key for key, pattern in RISKS if pattern.search(command)]
    if "break_system" in keys and "pip_system" in keys:
        keys.remove("pip_system")
    return keys


def install_prompt(owner, repo, commands, workdir):
    steps = "\n".join("%d. %s" % (i, cmd) for i, cmd in enumerate(commands, 1))
    return ("Install the GitHub project %s/%s (https://github.com/%s/%s) on this server.\n"
            "Its README suggests these commands, in this order:\n%s\n\n"
            "Rules:\n"
            "- Run them one at a time, exactly as written, in this order. Skip the ones that clearly do not apply "
            "(another OS, an optional variant) and say which and why. If one has to change for this server "
            "(a path, a version, a missing dependency), say what and why before running the changed command.\n"
            "- If the project has to be cloned, clone it into %s/%s unless it is already there.\n"
            "- No sudo unless the command itself has it; nothing else that changes the system without saying why.\n"
            "- If a command pipes a downloaded script into a shell, download the script to a file first, tell the "
            "owner in two lines what it does, and only then run it.\n"
            "When done, report briefly: what was installed, where (paths, services, containers, ports), how to "
            "start it, and how to remove it completely."
            % (owner, repo, owner, repo, steps, workdir.rstrip("/"), repo))


def release_prompt(version, repo, workdir):
    where = "the GitHub repository %s (its clone in %s, or clone it there)" % (repo, workdir) if repo \
        else "the git repository in %s" % workdir
    return ("Publish release %s of %s.\n"
            "1. Check that the working tree is clean and on the default branch, and that tag %s does not exist yet.\n"
            "2. Show the commits since the previous tag and draft short release notes.\n"
            "3. STOP and ask the owner for an explicit yes before anything that leaves this server: git push, "
            "pushing tags, gh release create.\n"
            "4. Only after that yes: create the tag %s, push it, create the GitHub release with the notes "
            "(gh release create), and send the release URL."
            % (version, where, version, version))


GH_COMMANDS = {
    "status": ["repo", "view", "--json", "nameWithOwner,description,defaultBranchRef,pushedAt,isPrivate,url,"
               "stargazerCount,forkCount", "--template",
               "{{.nameWithOwner}}{{if .isPrivate}} (private){{end}}\n{{.description}}\n{{.url}}\n"
               "branch {{.defaultBranchRef.name}} · pushed {{timeago .pushedAt}} · ★ {{.stargazerCount}} · "
               "forks {{.forkCount}}\n"],
    "runs": ["run", "list", "-L", "8"],
    "prs": ["pr", "list", "-L", "10"],
    "issues": ["issue", "list", "-L", "10"],
}


def gh_argv(gh, sub, repo=None):
    """argv for a /gh shortcut (no shell); `repo` ("owner/name", checked by the caller) or the repo of WORKDIR."""
    if sub == "status":
        return [gh, "repo", "view"] + ([repo] if repo else []) + GH_COMMANDS["status"][2:]
    return [gh] + GH_COMMANDS[sub] + (["-R", repo] if repo else [])
