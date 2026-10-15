# VPS Director web API

The JSON API behind the Telegram Mini App and the browser dashboard (`vps_director/webapi.py`,
`webauth.py`, `console.py`). It is meant to stay stable so other clients (a phone app) can use it.

- Base URL: `WEB_URL` (e.g. `https://vps.example.com`); the server itself listens on
  `127.0.0.1:WEB_PORT` behind your HTTPS proxy or tunnel.
- Everything is JSON (UTF-8). Successful replies have `"ok": true`; errors are
  `{"ok": false, "error": "<text for a human>"}` with a meaningful HTTP status.
- `Cache-Control: no-store` on every `/api/*` and `/login` reply.
- Times are Unix seconds (float or int); `*_text` fields are already formatted in the owner's time zone
  and language.
- Text fields (problems, findings, job descriptions) come in the bot's language (`LANG`), ready to
  show; clients must display them as text, never as HTML.

## Authentication

Every `/api/*` call must be authenticated in one of two ways. Nothing in `/api/*` is public.

### 1. Telegram Mini App — `X-Init-Data`

Send `Telegram.WebApp.initData` verbatim in the `X-Init-Data` header on every call. The server checks
it as Telegram documents: `secret = HMAC_SHA256(key="WebAppData", msg=<bot token>)`,
`hash = hex(HMAC_SHA256(key=secret, msg=<data-check-string>))`, constant-time compare, `auth_date` at
most **600 s** old (and not more than 60 s in the future), `user.id` in `OWNER_IDS`, no repeated
fields. A bad `X-Init-Data` is a 401 even if a valid cookie is also sent. initData is not refreshed
by Telegram while the app stays open: after 10 minutes the client gets 401 and must be reopened.

### 2. Browser / app session — one-time link → cookie

1. The owner sends `/dashboard` to the bot and gets a button with
   `WEB_URL/login?t=<token>` (32 random bytes, valid **10 minutes**, **single use**; only its
   SHA-256 is stored).
2. `GET /login?t=<token>` →
   - `303 See Other`, `Location: /`, `Set-Cookie: __Host-vpsd=<session>; Path=/; Max-Age=43200;
     HttpOnly; SameSite=Strict; Secure` (the cookie is called `vpsd`, without `Secure`, when
     `WEB_URL` is plain `http://` on loopback);
   - `403` (text/plain) for a bad, used or expired link; `429` when rate-limited.
3. Send the cookie on every call. It lasts **12 hours**, is bound to that user and is revoked by
   `POST /api/logout` (this session) or `/logout_web` in the bot (all sessions and unused links).

A native client can do the same: open the link with redirects disabled, keep the `Set-Cookie`
value, send it as `Cookie:` and set `Origin` (below) itself.

### CSRF rules for anything that is not GET

`POST` / `DELETE` calls additionally need:

- `X-Requested-With: vpsd`, else `403 missing X-Requested-With`;
- `Origin` equal to the origin of `WEB_URL` (or to `Host` when `WEB_URL` is unset), else
  `403 bad origin`;
- `Content-Type: application/json` and a JSON **object** body — also for DELETE (`{}`), else `415`.

Bodies are limited to 64 KB (`413`).

### Rate limits

Failed authentications and `/login` hits: 10 a minute per client address, 100 a minute in all.
Over the limit: `429` (`"too many failed attempts, wait a minute"`). The client address is the
proxy's `X-Forwarded-For` (last entry) only when the peer is loopback.

### Common errors

| Status | When |
|---|---|
| 400 | bad input (the `error` text says what) |
| 401 | not authenticated |
| 403 | CSRF check failed; the web console is off (`WEB_CONSOLE` not `1`) |
| 404 | no such server / container / job / request; unknown API path |
| 405 | wrong method for an existing path |
| 409 | too many jobs (50) |
| 413 | body over 64 KB |
| 415 | not a JSON object body |
| 429 | rate limit; too many requests waiting for Allow; too many consoles |
| 500 | internal error (details only in the bot's log) |

## Read-only views

Views run the bot's own checks over SSH; they are cached **30 s**. `?refresh=1` asks for fresh data,
but at most once per 5 s per view (otherwise the cached copy is returned).

### `GET /api/me`

```json
{"ok": true, "user": "111", "via": "telegram", "bot": "Director", "owner": "Alex",
 "lang": "en", "tz": "Europe/Berlin", "tz_label": "Europe/Berlin", "brief": "09:00", "console": false}
```

`via`: `telegram` (initData) or `cookie`. `brief`: `HH:MM` or `""` when the morning brief is off.
`console`: whether `WEB_CONSOLE=1`.

### `GET /api/servers`

```json
{"ok": true, "time": 1791000000.0,
 "problems": ["web: disk 88%", "archive: no SSH connection"],
 "servers": [
  {"name": "edge", "where": "Frankfurt", "role": "bot, panel", "main": true, "online": true,
   "problems": ["edge: containers worker"],
   "uptime": 522000, "load": 0.84, "cores": 4,
   "mem_pct": 62, "mem_used_gb": 2.5, "mem_total_gb": 4.0,
   "disk_pct": 39, "disk_used_gb": 31.0, "disk_total_gb": 80.0,
   "failed": [],
   "containers": {"total": 5, "ok": 3, "bad": ["worker"], "done": ["migrate"]}},
  {"name": "archive", "where": "Sofia", "role": "", "main": false, "online": false,
   "problems": ["archive: no SSH connection"]}]}
```

An offline server has only `name, where, role, main, online, problems`. `containers` is present
only where Docker is installed; `bad` = restarting / unhealthy / failed, `done` = exited with 0.
`load` is the 5-minute load average; the bot calls it a problem above `cores`, memory at 90 %,
disk at 85 %.

### `GET /api/servers/<name>`

```json
{"ok": true, "time": 1791000000.0, "server": { …one item of /api/servers… },
 "containers": [{"name": "shop", "image": "nginx:1.27", "status": "Up 3 days", "state": "up"},
                {"name": "worker", "image": "app:2", "status": "Restarting (1) 5 seconds ago", "state": "bad"},
                {"name": "migrate", "image": "app:2", "status": "Exited (0) 2 days ago", "state": "done"}]}
```

`state`: `up`, `bad` or `done`. `404` for a name that is not in `servers.json`.

### `GET /api/security`

```json
{"ok": true, "time": 1791000000.0, "servers": [
 {"name": "web", "online": true, "first": false,
  "findings": [{"level": "warn", "kind": "password", "text": "web: SSH accepts passwords — keys only is safer"},
               {"level": "info", "kind": "port", "text": "web: new local listener 127.0.0.1:5432 (postgres)"}],
  "logins": 3, "login_ips": 1, "ports": 4, "containers": 2, "images": 2,
  "ssh_password": "yes", "root_login": "keys", "fail2ban": "inactive",
  "auto_updates": "on", "updates": "3", "security_updates": "2"}]}
```

`level`: `bad` (❌), `warn` (⚠️), `info` (ℹ️, not a problem). `first`: this scan became the baseline.
`ssh_password`: `yes` / `no` / `""` (unknown); `root_login`: `yes` / `no` / `keys` / `""`.

### `GET /api/sites`

```json
{"ok": true, "time": 1791000000.0, "sites": [
 {"name": "shop", "url": "https://shop.example.com/", "server": "web", "up": true, "status": 200,
  "seconds": 0.21, "cert_days": 8, "domain_days": 199, "line": "…",
  "problems": [], "coming": ["shop: the certificate expires in 8 d"]}]}
```

`problems` make a site broken (down, bad certificate); `coming` are dates coming up. `cert_days`,
`domain_days` may be `null`.

### `GET /api/backups`

```json
{"ok": true, "time": 1791000000.0, "backups": [
 {"name": "db dumps", "type": "dir", "server": "web", "fine": true, "time": 1790982000, "line": "…", "problems": []}]}
```

## Jobs (reminders and scheduled tasks)

### `GET /api/jobs`

```json
{"ok": true, "tz": "Europe/Berlin", "jobs": [
 {"id": "3", "kind": "reminder", "text": "Renew the certificate", "when": "once", "next": 1791259200,
  "next_text": "13.10 11:05", "repeat": false, "changes": false}]}
```

`kind`: `reminder`, `task` (runs the agent), `command` (a script command), `brief` (the morning
brief).

### `POST /api/jobs`

Body: `{"text": "Call the hoster", "when": "2h", "every": false}` → `201 {"ok": true, "job": {…}}`.

- `text`: 1–500 characters, no control characters;
- `when`: `30m`, `2h`, `1d` (from now) or `HH:MM` (next such time in the owner's zone);
- `every: true` repeats it at that interval (`30m`, `2h`, `1d`) or daily at `HH:MM`.

`400` for bad input, `409` when 50 jobs exist already. Only reminders can be created over the API.

### `DELETE /api/jobs/<id>`

Body `{}` → `{"ok": true, "deleted": "3"}`; `404` if there is no such job.

## Problems: a page per problem, with ways to fix it

### `GET /api/problems`

Every open problem (servers, security, sites, backups) as `{"key", "text", "level", "area", "server"}`;
the key is the watchdog's stable key (`srv:web:disk`, `sec:web:port:0.0.0.0:8080`, `site:shop:down`, …).

### `GET /api/problem?key=…`

`{"gone": true}` when it is no longer a problem; otherwise `title`, `level`, `about` (what it means),
`seen` (read-only probe output from the server: top processes, `df` / `docker system df`, a container's log,
a unit's journal, ssh log lines of a new IP, …) and `fixes`: `[{"id", "kind": "task" | "allow" | "nav",
"label", "arg"?, "target"?, "hint"?}]`. Also `ctr:<server>:<container>` and `metric:<server>:load|mem|disk`.

### `POST /api/fix`

`{"key", "fix", "arg"}`. Only a fix the page offered for that key is accepted (`400` otherwise).
`ask` → `202 {"task": true}`: the agent gets the problem as a task in the chat. Every `allow` fix
(restart a container or a unit, install security updates, start fail2ban, accept a new IP / port /
container / image into the security baseline) → `202 {"pending": id}`; it runs only after **Allow** in
Telegram, its state is read with `GET /api/restarts/<id>`.

## Settings and the team (admins only; read-only members get `403` for anything but GET)

### `GET /api/settings`, `POST /api/settings`

`{"changes": {KEY: value}}` for `NAME, OWNER_NAME, LANG, TZ, BRIEF, WORKDIR, TIMEOUT, WATCH_INTERVAL,
WATCH_DISK_PCT, MENU, PROGRESS, WEB_CONSOLE`. All are checked first (`400` with `fields` = reasons, nothing
written); then `.env` is rewritten (comments and other keys kept, `.env.bak` saved) and the bot re-executes
itself once no task is running. The token, ports, URLs, webhooks, allowed roots / tools stay hand-edited.

### `GET /api/team`, `POST /api/team`, `POST|DELETE /api/team/<uid>`

Members are `OWNER_IDS` (role `admin`) and `VIEWER_IDS` (role `viewer`). Adding by id: `{"uid", "role", "name"}`;
changing the role: `{"role"}`. The last admin can be neither removed nor made read-only.

### `POST /api/team/invite`, `DELETE /api/team/invite/<id>`, `POST /api/team/pick`

An invite is a one-time `https://t.me/<bot>?start=inv_<code>` link for one role, valid 24 hours; the admins
get a note with **Remove** when someone joins. `pick` makes the bot send the admin a keyboard button that
opens Telegram's own "choose a person" list (`request_users`).

## Removing a server from the list

### `DELETE /api/servers/<name>`

→ `{"deleted": name}`. The server leaves `servers.json` (other entries and unknown keys are kept as
written) and the security baseline; the watchdog forgets its open alerts. Nothing on the server itself
is touched. The owner gets a Telegram note with **Undo**, valid 7 days, which puts the entry back in its
old place. `400` for the machine the bot runs on (`main` or an empty `ssh`), `404` for an unknown name,
`409` when `servers.json` cannot be read.

## Container restart (needs Allow in Telegram)

### `POST /api/servers/<name>/containers/<container>/restart`

Body `{}` → `202 {"ok": true, "pending": "a1b2c3d4"}`. Nothing restarts yet: the owner gets
"Restart <container> on <server>? [Allow] [No]" in Telegram. The request expires after **10 minutes**;
at most 5 can wait at once (`429`). `404` for an unknown server or container, `400` for a name that
is not a valid container name.

### `GET /api/restarts/<id>`

```json
{"ok": true, "id": "a1b2c3d4", "state": "done", "server": "edge", "container": "worker", "result": "worker"}
```

`state`: `pending` → `allowed` → `done` | `failed` (`result` holds the error), or `denied`,
`expired`. Poll every ~2 s.

## Web console (only with `WEB_CONSOLE=1`, else every call is `403`)

### `POST /api/console`

Body `{"server": "edge"}` (a name from `servers.json`; the client never sends a command or an ssh
alias) → `202 {"ok": true, "pending": "0f1e2d3c", "expires_in": 120}`. The user gets
"Open a console on edge? [Allow] [No]" in Telegram. `429` when 2 consoles are open or 5 requests wait.

### `GET /api/console/<id>`

Only for the user who asked (`404` for anyone else).

```json
{"ok": true, "id": "0f1e2d3c", "state": "allowed", "server": "edge",
 "ticket": "<43 chars>", "ws": "/ws/console?ticket=<43 chars>", "ticket_expires_in": 57}
```

`state`: `pending`, `allowed` (with the ticket), `denied`, `expired`, `opened` (the ticket was
used). Poll every 1–2 s.

### `GET /ws/console?ticket=…` (WebSocket, RFC 6455)

Open `wss://<host>` + `ws` within **60 s**. The ticket works once (any attempt burns it), only for
that user (a dashboard cookie, when sent, must belong to the same user), and only with an `Origin`
equal to `WEB_URL`'s origin. Refusals are plain HTTP (`403`, `429`).

- client → server, **text** frames: `{"t": "in", "d": "ls -la\r"}` (keystrokes, UTF-8),
  `{"t": "resize", "cols": 120, "rows": 40}` and `{"t": "close"}` (end the console — a lost
  connection does not); client frames must be masked, at most 64 KB;
- server → client, **text** frames `{"t": "session", "id", "resume", "keep"}` on every connect (the
  resume token, rotated each time) and `{"t": "replay"}` before the kept output; **binary** frames:
  terminal output (throttled to 1 MB/s);
- the server pings after 30 s of silence; input or output resets the idle timer;
- close codes: `1000` the shell exited or the console was ended, `1001` the bot stopped,
  `4000` 30 minutes with no input and no output, `4001` the 8-hour limit, `4002` taken over by
  another window, `4003` nobody came back within `CONSOLE_KEEP`.

Coming back: `POST /api/console/<id>/resume {"token"}` → a one-time ticket for this socket (no new
Allow); `GET /api/console/sessions` lists your consoles, `DELETE /api/console/<id>` ends one.

### `GET /api/console/commands`

`{"ok": true, "commands": ["apt", "bash", "docker", …]}` — executable names in the console's `PATH`
on the bot's machine (computed once per bot process). Used by the assistant's typo check.

### `GET /api/console/history`

`{"ok": true, "history": ["docker ps", "git status", …]}` — the newest 500 distinct lines of the
bot user's `~/.bash_history`, oldest first, read-only. Timestamp lines, lines with control
characters, lines over 500 characters and lines that look like they contain a secret are dropped.

## Session

### `POST /api/logout`

Body `{}` → `{"ok": true}` and a `Set-Cookie` that clears the session cookie. With initData it does
nothing but answer `ok`.

## Static files

Everything else under `/` is a file from `web/` (`index.html`, `app.js`, `commands.json`, …) with
a strict CSP. Unknown paths are `404`.
