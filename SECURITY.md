# Security policy

VPS Director is a remote control for a server: whoever controls the bot can run commands as the
user it runs under. Please read the "Security — read this" section of the README before installing.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's private
"Report a vulnerability" button on this repository (Security → Advisories). You will get an answer
within a few days; a fix comes with a release note that credits you unless you prefer otherwise.

Useful in a report: the version (commit), the affected command or page, steps to reproduce, and
what an attacker gains.

## Threat model in short

| Who | Can | Cannot |
|---|---|---|
| Anyone on Telegram | message the bot | get any answer: every update from a user not in `OWNER_IDS` / `VIEWER_IDS` (and every group chat) is ignored — except `/start inv_<code>` with a live invite |
| Someone holding an invite link | join once, with the role it was made for | reuse it, or use it after 24 h; every admin is told who joined, with a Remove button |
| A read-only member (`VIEWER_IDS`) | read-only commands, alerts, every GET of the Mini App except the console | run tasks, press Allow, open a console, change settings, the team or servers (403 / "read-only") |
| The agent (Claude / Codex), e.g. misled by a README it read | ask for permissions | get anything you did not see in full: Allow grants exactly the rule shown, and requests that cannot be shown in full (long, padded, invisible characters, secrets) get no Allow button; in safe mode Codex is read-only |
| The agent | drop files into the outbox | make the bot send a file through a symlink |
| Someone with the Mini App URL | load the static page | call the API: every call needs Telegram-signed initData of an owner, a session made from it, or a dashboard cookie from a one-time link; failures are rate-limited |
| A website you visit | — | use your dashboard session: SameSite=Strict cookie, `X-Requested-With` header, Origin check, JSON-only bodies |
| An admin, from the Mini App | change safe settings, manage the team, press fix buttons | change the bot token, ports, URLs, webhooks, allowed roots / tools (hand-edited only); run a fix script without Allow in Telegram; remove the last admin |
| You, from the Mini App | open a web console | open it silently: each console needs Allow in Telegram, a one-time ticket that lives 60 s, and at most 3 consoles are open at once; coming back to a console that kept running needs its resume token (rotated on every connect) *and* a signed-in call |
| Another local user | see process arguments (so: `PROMPT.md` contents) | read the config, state, logs, outbox or engine token (all 0600 / 0700) |

What is **not** protected: a stolen bot token together with access to an owner's Telegram account;
`/full` mode and `AGENT_MODE=full` (the agent can do anything your user can, without asking);
`WEB_CONSOLE_AUTO=1` (a console opens without Allow — still only from inside Telegram, never via a
browser link, and each one is reported in the chat); anything your user can already do.

## Hardening checklist

- Run the bot as a dedicated user with only the rights it needs; `ALLOWED_ROOTS` for the bot itself.
- Keep `WEB_BIND=127.0.0.1` and publish the Mini App only through an HTTPS reverse proxy or tunnel.
- `WEB_CONSOLE` stays `0` unless you want a browser shell; `WEB_CONSOLE_AUTO` and `AGENT_MODE=full`
  stay off unless you accept what they give up.
- Give people `viewer` unless they really need to run things; invite links are one-time, 24 h.
- Log the engines in with `claude setup-token` → `<instance>/engine.env` (0600).
- Watch `/audit` and the watchdog's `/security` alerts.
