# Changelog

## 1.0.1 — 2026-10-15

- Python 3.10 (Ubuntu 22.04): SSH logins from journald (`+0200` offsets) were not read, so new-login
  alerts never fired; restic times with short fractions were dropped. Both fixed.
- `https.sh` leaves existing ufw rules alone.
- Tests run on GitHub Actions for Python 3.10–3.13.

## 1.0.0 — 2026-10-15

First public release.

- **Agent on the server**: tasks in text or voice go to Claude Code or Codex on your own subscription;
  safe mode by default with one-time *Allow / No* buttons; full access only by your choice.
- **Monitoring without AI**: CPU, load, memory, disk, services and containers of every server with
  24-hour charts; security (new SSH logins and ports, miners, password logins, updates); sites (status,
  TLS and domain expiry); backups (freshness, test restores); watchdog and morning brief.
- **Mini App**: servers, an overview of open problems, a page with fixes for every problem, tasks with a
  calendar, team roles (admin / read-only), every setting editable; light and dark themes; six languages
  (en, ru, es, de, fr, pt).
- **Terminal in Telegram**: a real shell on any server that survives a lost connection; optional tmux.
- **Install**: `./install.sh` (bot token, owner by first message, systemd user service) and
  `./https.sh` — HTTPS for the Mini App in one command (nginx + certbot or Caddy, sslip.io without a domain).
- An agent that stays silent for 90 seconds (usually not logged in) is stopped with a login hint.
- Python standard library only; 460+ tests.
