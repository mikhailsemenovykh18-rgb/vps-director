# Why choose VPS Director (and when not to)

There are already a dozen ways to talk to Claude Code or Codex from your phone. Anthropic ships
Remote Control and Channels; the community has Telegram bridges with thousands of stars. So why
another one? Because almost all of them answer the question *"how do I keep coding when I'm away
from my laptop?"*. VPS Director answers a different one: *"who looks after my server when I'm not
looking at it?"*

Here is what it is for, what is actually different, and when another project will serve you better.

## What it is for

VPS Director is ChatOps for a one-person (or small-team) server. It is a Telegram bot that lives on
your VPS, next to the services it looks after, and hands your messages to the Claude Code or Codex
CLI already installed and logged in there. You write *"why is the site slow?"* from your phone; the
agent reads the nginx logs, checks load and containers on the machine itself, and answers with the
cause and a proposed fix. If the fix means restarting something, you get a button.

The mental model is a colleague who sits inside the server, not a remote terminal you type into.
It has a name you choose, it remembers what you told it ("backups go to /mnt/backup"), it texts you
when a disk fills up, it sends a short brief in the morning, and it asks before it does anything
that cannot be undone.

## What makes it different

**A server-ops layer that works without AI.** `/status` answers instantly: load, memory, disk,
failed systemd units, unhealthy containers — no model call, no tokens. The watchdog checks the same
things every few minutes and writes only when something breaks and when it recovers. Any executable
you drop into a folder becomes a command (`disk.sh` → `/disk`) and can be triggered by a phrase.
Scripts and cron jobs can message you with `director.py notify`, and Uptime Kuma or CI can do the same
through a local webhook. In the competitor research behind this page, none of the popular bridges
had this layer; they are built around coding sessions.

**A colleague that asks before acting.** In the default safe mode every action that is not
explicitly allowed comes back as a button. *Allow* grants exactly that command or that file for one
turn — not "Bash" in general. A command containing `*` gets no Allow button at all, because a
permission rule would treat it as a wildcard and allow more than you were shown. Scheduled and
webhook-triggered tasks run unattended, so they never get buttons and never get full access: if
they need your OK, they tell you what to run by hand. Even the GitHub installer — send a repository
link and it pulls the install commands out of the README — runs as a normal safe-mode task, so each
command still needs your click, with a warning when it pipes a remote script into a shell.

**Personality and memory, on purpose.** This sounds like decoration; it is not. A bot that reports
the conclusion first, admits when it is unsure, remembers that the shop runs in Docker and that
backups go to a particular disk, and greets you with *"while you were away: two alerts, both
resolved"* after a long weekend is a bot you actually keep using. The persona is a block in your
`PROMPT.md`; memory is a plain `memory.md` file you can read and edit.

**Zero dependencies.** One Python entry point and a small package, standard library only, running
as a systemd user service. No pip, Poetry, uv, Node or Go toolchain to keep in sync, nothing that
breaks on the next upgrade of some library. For software that can run commands on your server, a
small audit surface is a feature.

**Subscription first.** It drives the official CLIs you already pay for — no API keys, no per-token
bill. `/limits` shows how much of your Claude 5-hour and weekly windows is used and warns at 80 % and
90 %; most competitors track API cost, which is irrelevant on a subscription.

**Russian and English.** Every message, the setup wizard and the plain-language reminders work in
both (*"remind me in 2 hours to call the bank"*, *"напомни завтра в 10:00 продлить домен"* — parsed
locally, no model call). We did not find another bridge with a Russian interface.

## When to pick something else

Be honest with yourself about what you need. These are good projects, and for some jobs they are
simply better.

- **You mainly want to code from your phone.** Look at
  [Untether](https://github.com/littlebearapps/untether). It supports more engines (Claude, Codex,
  OpenCode, Pi), has git worktrees, `/steer` into a running turn, cost budgets, file transfer and a
  real documentation site. As a general remote-coding bridge it is more complete than VPS Director.
- **You want a polished install and more engines or messengers.**
  [ductor](https://github.com/PleasePrompto/ductor) installs with `pipx`, has a Docker option and
  speaks to Gemini and Grok as well, over Telegram, Matrix or Slack.
- **You live in tmux and want the very same session on laptop and phone.**
  [ccgram](https://github.com/alexei-led/ccgram) bridges Telegram topics to tmux windows. It is
  fragile by design (it scrapes the terminal), but nothing else gives you literally the same session.
- **Your team is on Feishu, DingTalk, Slack or Discord.**
  [cc-connect](https://github.com/chenhg5/cc-connect) supports eight-plus messengers and many agents.
- **You want maturity above all.**
  [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) is the category
  leader: thousands of stars, a long issue history, a directory sandbox and an audit log. VPS
  Director is new; it has tests, but it has not yet been through what a widely used project has.
- **You just want Claude on your phone and you keep a terminal session open anyway.** Anthropic's
  own Remote Control gives you the native app with zero setup, and Channels brings Telegram into a
  running session. No bot to maintain.

VPS Director also has real limits. It speaks only Telegram. It supports Claude Code and Codex, not
Gemini or OpenCode. Codex in safe mode is a read-only sandbox, so there is no per-command Allow for
it — only full access for a session. It does not do worktrees or parallel coding sessions on one
repository. And `ALLOWED_ROOTS` fences what the bot itself touches, not what the agent can reach in
full-access mode.

## Who it is for

If you run your own server — a shop, a few sites, a VPN, a home lab — and you want something that
watches it, tells you when it matters, does routine work on request and never surprises you with an
action you did not approve, this is what VPS Director was built for. Install takes about five
minutes: `./install.sh`, paste a BotFather token, send the bot one message, answer a few questions
(its name, what to call you, your time zone, whether you want the morning brief).

If you mostly want to write code from a phone, pick one of the projects above. We would rather you
end up with the right tool than with ours.
