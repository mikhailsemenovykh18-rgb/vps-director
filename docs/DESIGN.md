# VPS Director — design notes

How the pieces fit together and why. For using it, see the README; for the HTTP API, docs/API.md.

## Goal

A Telegram bot that is your eyes and hands on your own server: messages go to Claude Code or
Codex CLI running on that server under your existing subscription (no API keys), answers come
back to the chat. Zero Python dependencies (stdlib only), one-command install.

## Design choices

1. **Generic install** — `install.sh` asks for the bot token and owner Telegram id, writes
   `~/.config/vps-director/<instance>/.env`, installs a systemd user unit `vps-director@<instance>`.
2. **Directors** — `/director` wizard in chat: name, what it is responsible for, working folder,
   rules → generates the instance prompt; the owner creates a bot in @BotFather and sends the
   token → the wizard writes the instance config and starts its unit.
3. **Nothing personal in the repo** — no paths, chat ids, project lists, server names. Example
   prompts live in `examples/`; real prompts stay in each instance's config dir.
4. **Live progress** — while Claude works, one status message is edited every few seconds with
   the last steps ("Bash: df -h", "Read: /etc/nginx/…"), so the owner is not waiting blind.
5. **Watchdog** — optional periodic check (disk, memory, failed systemd units, unhealthy docker
   containers); writes only when the state changes (no spam).
6. **Notify CLI** — `vps-director notify "text"` (or stdin / `--file`) lets any script message the
   owner through the bot.
7. **Limits** (later) — show subscription usage; needs a stable source from the CLIs.
8. **Docker image** (later).

## Script commands

`<instance>/commands/` holds the owner's own executable files; each whose name (sans extension)
matches `[a-z0-9_]{1,32}` is a command `/name` that never touches the AI. Header lines in the
first 10 lines: `# desc:` (menu text), `# phrases:` (whole-message triggers, normalised like
INTENTS and checked before them). Run without a shell: argv = `[path]` (+ the typed argument),
cwd = `WORKDIR`, 120 s timeout, stdout else stderr is the reply. Always allowed for owners
(`COMMANDS` does not restrict them). Built-in names win; shadowed scripts are logged once at
start. The Telegram menu is built in `MENU` order from built-ins (if enabled) and scripts.

## Config (.env per instance)

| key | meaning | default |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token | required |
| `OWNER_IDS` | allowed Telegram user ids, comma-separated | required |
| `TITLE` | how the bot introduces itself | `VPS Director` |
| `PROMPT_FILE` | instruction for the agent | `<instance>/PROMPT.md` |
| `WORKDIR` | where the agent runs | `$HOME` |
| `COMMANDS` | allowed built-in commands (empty = all); script commands are always allowed | empty |
| `STATUS_CMD` | custom /status script | built-in |
| `MENU` | commands in the Telegram menu, in this order (built-ins and scripts) | `status,new,stop,limits` |
| `ALLOWED_TOOLS` | Claude tools allowed without asking, `;`-separated | empty |
| `TIMEOUT` | max seconds per task | 3600 |
| `VOICE_CMD` | command that prints a transcript of an audio file | empty = voice off |
| `WATCH_INTERVAL` | watchdog period, minutes | 0 = off |
| `PROGRESS` | live progress message | 1 |
| `ALLOWED_ROOTS` | `:`-separated folders for WORKDIR, /director folders and /get | empty = no limit |
| `NAME` / `OWNER_NAME` | the bot's name / how it addresses the owner (TITLE = NAME) | `VPS Director` / empty |
| `TZ` | owner's IANA time zone for reminders, /at HH:MM, daily jobs, the brief | server time |
| `BRIEF` / `BRIEF_CMD` | morning brief time `HH:MM` / extra command for it | off / empty |
| `WEBHOOK_PORT` / `WEBHOOK_SECRET` / `WEBHOOK_TASKS` | inbound webhooks on 127.0.0.1 | 0 / required with a port / 0 |
| `WEB_PORT` / `WEB_URL` / `WEB_BIND` | Mini App + dashboard server; public HTTPS address; listen address | 0 / empty / 127.0.0.1 |
| `WEB_CONSOLE` | web console (PTY over WebSocket, each one after Allow in Telegram) | 0 |

## Safety model

- Only `OWNER_IDS` are served; everything else is ignored and logged.
- Default session mode `safe`: Claude runs with `--permission-mode default`; denied actions come
  back as buttons Allow / No. Allow grants exactly the denied action for one turn
  (`Bash(<command>)`, `Edit(<path>)`, `WebFetch(domain:<host>)`; a bare tool name only when there
  is no narrower rule, and the button says so). A rule whose argument contains `*` is never offered
  (it would be a wildcard); the owner is told to use /full or run it manually. Codex safe mode has no Allow path (read-only
  sandbox; `/full` is the switch).
- The task text reaches the CLIs on stdin; the engine runs in its own process group so `/stop`
  and the timeout kill everything it started. `full` mode exists per session for owners who want it.
- The bot never prints secrets; the README states plainly that a leaked bot token + a missing
  owner check would equal shell access, which is why the owner check is mandatory and the
  bot refuses to start without `OWNER_IDS`.

## Package layout

`director.py` is the entry point (`run`, `notify`, `setup`) and holds Config, the Telegram client,
State and the Director bot. `vps_director/` holds the parts that do not need the bot:
`texts.py` (EN/RU), `engines.py` (CLI argv, stream parsers, permission rules, masking, process
control), `setup.py` (first-run wizard, a list of steps), `terminal.py` (/continue discovery),
`schedule.py` (parsing and next-run computation for /at /every /remind). Names used by tests are
re-exported from `director`.

## Scheduled jobs

`state["jobs"][id]`: kind (`task` → agent, `command` → script command or /status, `reminder` →
plain message), text, chat, next, every (s) or daily (`HH:MM`), changes, session, last_hash.
A scheduler thread checks every 20 s; the next run is stored before firing. Task jobs run in their
own session, forced to safe mode, with no extra grants; denials are reported, never offered as
buttons. `Director.add_job()` is the entry point for other features (reminders, the brief).

## Audit log

`<conf>/audit.log`, 0600, append-only JSON lines `{time, from, kind, text}`; kinds task / command /
allow / deny / mode / director / job; text masked with `mask_secrets`, whitespace collapsed, 300
chars. Written under the state lock; handled slash commands are logged after dispatch, wizard
answers never. `/audit [N]` reads the tail from the end of the file. No rotation (a few hundred
bytes per action).

## Personality and memory

`vps_director/persona.py`. `NAME` / `OWNER_NAME` in `.env` (TITLE = NAME; old configs with only
TITLE keep working). The persona block (EN/RU text in `texts.py`) sits between marker lines in
PROMPT.md; setup writes it on top of the example instruction, renames rewrite only the block
(`update_env` keeps the rest of `.env`, files are written atomically 0600). Without a PROMPT.md the
default prompt is built with the persona. Memory: `<conf>/memory.md`, lines `- YYYY-MM-DD: fact`,
max 100 facts × 500 chars, masked with `mask_secrets` before storing, appended to `Config.prompt()`
(so both engines get it). Buttons `memdel:<sha256[:10] of the line>`. Phrases are parsed locally
(`parse_rename`, `parse_remember`) after slash commands, scripts and INTENTS; "call me when …" is
not a rename (stop words, ≤ 3 words). `state["seen"][chat]` is the owner's last message; after
3 days the digest is built from the audit tail (`alert` entries written by the watchdog, scheduler
`job` runs), `state["watch"]` and `state["rate"]`.

## Reminders from phrases, TZ, morning brief

`vps_director/natural.py` parses "напомни …" / "remind me …" / "every …" / "каждый …" locally into a
plan (`kind`, `text`, `next`, `every` | `daily`) or `{"error": when|text|past|every}`; `None` means
"not a schedule" and the message goes to the agent. "every …" must open the message. The earliest
time expression wins. Jobs go through `Director.add_job` like `/at`. Buttons `jobdel:<id>` and
`jobedit:<id>`; Change stores `state["editing"]` (15 min) and the next text is parsed with
`implicit=True` and replaces the job. `TZ` → `schedule.zone()` (ZoneInfo; unknown names and paths
rejected) is passed to `parse_when`, `next_clock`, `first_run`, `next_run`, `fmt_time`.
`sync_jobs()` (start and `/brief`) keeps one `brief` job per owner at `BRIEF` and re-times daily
jobs when `TZ` changed (`state["jobs_tz"]`). The brief (`brief_text`) uses the skills (
below), the audit tail (alerts), `state["rate"]`, today's jobs and `BRIEF_CMD` (shlex, no shell).

## Inbound webhooks

`vps_director/webhook.py`: `Webhook.handle(method, path, headers, body)` is pure (secret first via
`hmac.compare_digest`, then path, `/task` gate, rate limit 30/min, text from JSON
text|message|msg or plain), the HTTP part is a `ThreadingHTTPServer` on 127.0.0.1 in a daemon
thread (10 s socket timeout, 64 KB body cap answered with 413 before reading). `Config.validate`
refuses a port without a ≥ 16-char secret. `/notify` → owners (`🔔`), audit `webhook/notify`.
`/task` → `submit(..., job={"id": "webhook", "icon": "🔔", …})` in a persistent non-active
session (`state["webhook_session"]`), so `run_task` forces safe mode and denials are reported
without buttons; 429 while `WEBHOOK_QUEUE` (5) tasks wait.

## GitHub

`vps_director/github.py`. A message that is only a repo link → `fetch_readme` (README names in
order, `https://raw.githubusercontent.com/<owner>/<repo>/HEAD/<name>`, 512 KB cap; `Director.fetch`
is injectable, tests never touch the network) → `extract_commands` (shell fences under a heading
path containing Install/Quick start/Getting started/Setup, or KNOWN install lines anywhere; `$ `
prompts, continuations, comments; ≤ 12) → offer stored in `state["gh_offers"]` (one use) with
buttons `ghinst:` / `ghshow:`. Install = a fresh claude session in safe mode (active) with
`install_prompt`; every Bash command is a denial → exact Allow. `risk_notes` keys map to `risk_*`
texts and are also appended to permission requests for any Bash command (`denial_line`).
`/gh` runs fixed argv (`gh_argv`, `-R owner/repo` validated) with GH_PROMPT_DISABLED, 30 s;
release = `release_prompt` to the active session (the prompt requires a yes before pushing).

## Built-in skills

`vps_director/remote.py` runs a constant probe script here (`sh -c`) or on a server
(`ssh -o BatchMode=yes -o ConnectTimeout=8 -- <alias> "sh -c '<script>'"`); the alias must match
`[A-Za-z0-9_][A-Za-z0-9._@-]*`, so it can never be an ssh option. Config values (paths) never go
into the command line: they are passed on stdin and read with `read -r`. `None` = no connection
(exit 255, timeout). `Director.remote` is that function; tests replace it with recorded outputs.

**Servers** (`servers.py`): `<conf>/servers.json` (see README), loaded with validation (bad alias,
duplicate name, bad `hosting_paid_until` → skipped with a warning; no file → this machine only).
`PROBE` prints key=value lines ; `block()` renders one server
and returns problems as `(stable key, text)`, e.g. `("srv:web:disk", "web: disk 88%")`. Probes run in
a thread pool; each is capped at 25 s. The setup step `step_servers` offers `~/.ssh/config` hosts.

**Watchdog** (`Director.watch_once`): items are `{key: text}` from `server_problems()` (this machine;
key = text) and the skills (`watch_skills`). Only a change of the key set is reported, so a number
inside a text does not cause a new alert; `state["watch"]` holds the keys, `state["watch_text"]` the
texts (the away digest shows texts). Daily reminders (`{key: text}`) are sent once per day per key
(`state["reminded"]`).

**Security** (`security.py`): one `PROBE` per server prints `@@facts` (key=value: log source, sshd
settings from `sshd -T` or the config files with sshd defaults, fail2ban, unattended upgrades, apt
simulation counts), `@@logins` (only "Accepted" lines; Python keeps time, user, IP, method), `@@ports`,
`@@containers`, `@@images`, `@@ps`, `@@exe` (`/proc/*/exe` targets). `sudo -n` only when it works
without a password, only for reading. `findings(name, scan, base)` → `(level, key, text)`; keys are
`sec:<server>:<kind>:<value>`. Baseline `<conf>/security-baseline.json` (0600): the first scan of a
server, then `/security accept` (`merge`: IPs added, the rest replaced) from `state["security_last"]`
(≤ 1 h old, else a fresh scan). The watchdog runs it only once the baseline file exists; a new
login IP item disappearing (24-hour window) is not announced as "resolved". Site certificates come
from `sites.cert_lines` (TLS layer injectable as `Director.cert_connect`).

**Sites** (`sites.py`): `<conf>/sites.json` (http/https URLs with a plain host name, `expect` int or
text). `check()` = `fetch` (urllib, 15 s, 512 KB, HTTP error statuses are results) + `cert_expiry`
(own TLS handshake, verified; `Director.cert_connect`) + `Rdap.expiry` (registrable domain, rdap.org,
`<conf>/rdap-cache.json` 0600, one request per domain per day, failures cached too; network outside
the lock). `assess()` → problems `site:<name>:down|cert` and reminders `cert:<name>`,
`domain:<domain>`. The watchdog keeps `state["site_fails"]` and reports `down` only after
`DOWN_AFTER` (2) failures in a row; `back_text` turns a resolved `down` into "<name> is back".

**Backups** (`backups.py`): `<conf>/backups.json`, types `log` / `dir` / `restic`, `max_age` via
`schedule.parse_duration` (or days). `log` and `dir` use `LOG_PROBE` / `DIR_PROBE` through
`Director.remote` (this machine or a `servers.json` name → ssh alias); the path (and glob) are the
probe's stdin. `restic` runs locally through `Director.restic_run(argv, env, timeout)`: repository and
password file only in the environment (`restic_env`, allowed `env_file` keys, `RESTIC_PASSWORD*`
dropped), `--no-lock` everywhere; errors are masked (`mask_secrets`, URL userinfo). `verify()` restores
`verify_paths` into `mkdtemp(prefix="director-verify-")`, counts with `os.walk`, removes it in
`finally`. `assess()` → problems `backup:<name>:fail|stale|none|missing|error`.

**Morning brief** (`brief_checks` / `brief_skill_lines`): the configured skills (servers.json,
security baseline, sites.json, backups.json) run side by side in a pool; a skill that raises is
logged and left out. Lines: servers (`servers.summary_line` + this machine's `server_problems`
prefixed with the main server's name; without servers.json the old load/disk line), security
changes (finding kinds `ip|port|container|image|proc|badimage` — settings stay in `/security`), sites
down or with a broken certificate, `backups.summary_line` (or "not watched"), "Coming up" (hosting,
certificate and domain reminders). The audit-text guess for backups is gone.

**Scheduling**: `SKILL_COMMANDS` (servers, security, sites, backups) can be scheduled like
`/status` (`/every 1d /backups`); `skill_output()` renders them without AI.

## Mini App backend

`vps_director/web.py`: `WebApp.handle(method, path, headers, body, ip)` → `Response`, pure (tests call it
directly); `serve()` runs it on a `ThreadingHTTPServer` in a daemon thread, `WEB_BIND` (default
127.0.0.1). Static files from `web/` via `static_path()` (realpath inside the root, no dot files, known
extensions only, no listing). Every reply gets CSP (`'self'`, telegram.org for its WebApp script, `wss://` of WEB_URL in connect-src — no CDN since 07c;
`frame-ancestors https://web.telegram.org https://*.telegram.org`), `Referrer-Policy: no-referrer`,
nosniff; `/api`, `/login` get `Cache-Control: no-store`. Bodies over 64 KB → 413. Request lines are
never logged (the login URL carries a token). Behind a local proxy the client IP is the last
`X-Forwarded-For` entry (only when the peer is loopback).

`vps_director/webauth.py`: `check_init_data()` per Telegram docs (`hmac.compare_digest`, duplicate
fields refused, `auth_date` within 600 s, `user.id` ∈ owners); `Auth` keeps one-time login tokens
and session ids as SHA-256 in `<conf>/web-auth.json` (0600, capped, expired entries pruned).
`authenticate()` → `{uid, via, sid}` or 401/429: a present `X-Init-Data` must validate (no fallback
to the cookie). `csrf()` for non-GET: `X-Requested-With: vpsd`, Origin = WEB_URL's origin (Host when
unset), `application/json` object body. Rate limits (`web.RateLimit`, bounded key table): 10/min per IP
for failed auth and for `/login`, 100/min failed overall. Audit kind `web`.

`vps_director/webapi.py`: `Api.call()` routes `/api/*`; views reuse `servers_check`, `security_check`,
`sites_check`, `backups_check` (30 s cache, `?refresh=1` after 5 s). Jobs go through `add_job` (kind
`reminder`) and the same state. A container restart creates `state["web_restarts"][rid]` and sends
Allow / No (`ctrok:` / `ctrno:` callbacks, owners only, 10 min TTL, at most 5 waiting); only Allow runs
`servers.RESTART_SCRIPT` with the name on stdin. The bot: `/dashboard` (URL button with the one-time
link), `/logout_web`, `setChatMenuButton` (web_app) when WEB_URL and WEB_PORT are set, reset to
`commands` only if we set it.

## Web console

`vps_director/ws.py`: RFC 6455 server side. `handshake(headers)` → Sec-WebSocket-Accept or None;
`web.WebApp.websocket()` routes `/ws/*` (`sockets={path: fn(query, headers, ip)}` → refusal `Response`
or `runner(conn)`), the HTTP handler writes the 101 and hands the socket over (timeout off: the
console has its own limits). `Connection.recv()`: masked frames only (1002), RSV/unknown opcode/bad
control frames 1002, frame or reassembled message > 64 KB 1009 (checked from the header), invalid
UTF-8 1007, ping → pong, close echoed once; `send_*` thread-safe, unmasked, ≤ 64 KB frames, through
`Throttle` (1 MB/s, one-second burst).

`vps_director/console.py`: `Consoles` (one per bot, `Director.consoles()`) holds in memory only —
nothing survives a restart: `waiting` (request id → server, uid, state; 2 min, ≤ 5), `tickets`
(SHA-256 → uid, server, expiry 60 s), `active` sessions (≤ 2, `reserved` covers starts in flight).
`POST /api/console` → Allow / No (`conok:` / `conno:`, only the user who asked); `GET
/api/console/<id>` returns the ticket to that user while it is unused. `socket()` (the `/ws/console`
route): WEB_CONSOLE, per-IP limit, Origin = WEB_URL origin, ticket popped on any attempt (burnt), a
dashboard cookie, when present, must belong to the ticket's user. `Pty` = `pty.fork()` (new session;
the child only chdir()s and exec()s), `shell_argv()` = `[login shell, -l]` or `ssh -tt -o
BatchMode=yes … -- <alias>` (alias re-validated), `clean_env()`. `Session.run()` reads frames in the
HTTP thread, a pump thread sends output and checks idle (input only) / hard limits. `stop()` once:
`Pty.kill()` signals every process of the session (found by session id / process group in /proc)
with SIGHUP, SIGKILL after 3 s, keeping the shell unreaped (`waitid(WNOWAIT)`) until the rest is gone
so the session id cannot be reused, then reaps it; close frame (4000 idle, 4001 time limit, 1000
exit/close, 1001 disconnect / bot stop), socket shutdown, audit (kind `console`) and a Telegram note.
`atexit` closes every console.

## Mini App frontend and assistant

`web/` is served as-is (no build): `index.html` (no inline script or style — CSP `'self'`),
`app.css` (tokens from Telegram `--tg-theme-*` with light/dark fallbacks, safe areas from
`env(safe-area-inset-*)` and `--tg-*-safe-area-inset-*`), `i18n.js` (EN/RU, RU plurals), `icons.js`
(SVG via createElementNS), `app.js` (hash router `#/`, `#/server/<n>`, `#/console/<n>`, `#/jobs`,
`#/health/<security|sites|backups>`, `#/settings`; API client adds `X-Init-Data` inside Telegram and
`X-Requested-With: vpsd` always, JSON bodies on every non-GET; every server value goes in through
`textContent`/attributes — a test greps the JS for HTML sinks), `console.js` (Allow flow → WebSocket,
xterm.js from `web/vendor/xterm/` with the canvas renderer loaded *before* `open()` so the DOM renderer
never injects `<style>`; LineModel follows the typed line from keystrokes and goes "unknown" after
arrows / Tab / Ctrl keys until Enter), `assistant.js` (pure, UMD; node tests in `web/tests/`),
`commands.json` (curated: `{version, commands: {name: {en, ru, sub, flags, examples}}}`, structure
tested in Python).

API additions: `/api/me` gains `brief`, `tz_label`, `console`; `GET /api/console/commands`
(`console.path_executables` over the console's clean PATH, computed once per process) and
`GET /api/console/history` (`console.history_lines`: tail 256 KB of `~/.bash_history`, drops
timestamps, control characters, > 500 chars and anything `mask_secrets` would change, newest 500
distinct). Both 403 unless WEB_CONSOLE=1. Routed before `/api/console/<id>` (ids are 8 hex chars).

Known limit: Telegram's initData is valid 10 minutes (07a) and is not refreshed by the client, so a
Mini App left open longer gets 401 and shows "close and reopen".

