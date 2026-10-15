#!/usr/bin/env bash
# VPS Director installer: one bot instance as a systemd user service.
#
#   ./install.sh                      interactive: asks for the bot token, then you message the bot
#   ./install.sh --instance shop      a second bot (a "director") with its own config
#   ./install.sh --uninstall [--instance NAME]
#
# Needs: Linux with systemd, python3 ≥ 3.10, and `claude` (Claude Code) and/or `codex`
# logged in under this user. Installs nothing system-wide and asks for no sudo.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="main"
UNINSTALL=0
ME="${USER:-$(id -un)}"
while [ $# -gt 0 ]; do
  case "$1" in
    --instance) INSTANCE="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]{1,31}$ ]] || { echo "instance name: 2-32 chars, a-z 0-9 -" >&2; exit 2; }

CONF="$HOME/.config/vps-director/$INSTANCE"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="vps-director@$INSTANCE.service"

if [ "$UNINSTALL" = 1 ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  echo "Stopped $UNIT. Config kept in $CONF (delete it yourself if you want)."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "python3 >= 3.10 is required" >&2; exit 1; }
command -v claude >/dev/null || command -v codex >/dev/null || [ -x "$HOME/.local/bin/claude" ] \
  || echo "warning: neither 'claude' nor 'codex' found in PATH — install and log in before chatting"

echo "== tests"
(cd "$HERE" && python3 -m unittest discover -q -s tests)

# the bot runs as a systemd *user* service: make sure that works before writing anything
systemctl --user show-environment >/dev/null 2>&1 || {
  echo "systemctl --user does not work here (no systemd user session for $ME)." >&2
  echo "Log in directly as $ME over SSH (not via su/sudo), or run 'sudo loginctl enable-linger $ME', then retry." >&2
  exit 1
}

mkdir -p "$CONF" && chmod 700 "$CONF"
if [ ! -f "$CONF/.env" ]; then
  echo
  # asks for the token (hidden), then waits for your first message to the bot: no ids to look up;
  # also the bot's name, your time zone, the morning brief and (if faster-whisper is installed) voice
  DIRECTOR_CONF="$CONF" python3 "$HERE/director.py" setup --instance "$INSTANCE" || exit 1
elif ! grep -q '^VOICE_CMD=' "$CONF/.env" && python3 -c 'import faster_whisper' 2>/dev/null; then
  # an existing config: setup did not run, so offer voice here
  ans=""
  read -r -p "faster-whisper is installed: transcribe voice messages with it on this server? [y/N] " ans || ans=""
  case "$ans" in
    y|Y|yes|д|да)
      python3 - "$HERE" "$CONF/.env" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from vps_director.setup import update_env, voice_cmd
update_env(sys.argv[2], {"VOICE_CMD": voice_cmd()})
print("VOICE_CMD added to " + sys.argv[2])
PY
      ;;
  esac
fi
[ -f "$CONF/PROMPT.md" ] || cp "$HERE/examples/PROMPT.example.md" "$CONF/PROMPT.md"

echo "== service"
mkdir -p "$UNIT_DIR"
NODE_DIR="$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)")"
# Paths may contain spaces, quotes, % or $: quote them the way systemd unit files need (no sed).
python3 - "$HERE/vps-director@.service" "$UNIT_DIR/vps-director@.service" "$HERE/director.py" \
  "$HOME/.local/bin:$NODE_DIR:/usr/local/bin:/usr/bin:/bin" <<'PY'
import sys
src, dst, director, path = sys.argv[1:]
if "\n" in director + path:
    sys.exit("a path contains a line break")


def word(value, dollar=True):
    """One double-quoted systemd word: \\ and " escaped, % is a specifier, $ a variable in ExecStart."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"%s"' % (value.replace("$", "$$") if dollar else value)


with open(src, encoding="utf-8") as fh:
    unit = fh.read()
unit = unit.replace("__DIRECTOR__", word(director)).replace("__PATH__", word("PATH=" + path, dollar=False))
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(unit)
PY
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
loginctl enable-linger "$ME" 2>/dev/null || echo "note: run 'sudo loginctl enable-linger $ME' so the bot survives logout"
sleep 3
if ! systemctl --user is-active "$UNIT"; then
  echo "The service did not start. Its log: journalctl --user -u $UNIT -n 50" >&2
  exit 1
fi
echo
echo "Done. Open your bot in Telegram and press Start."
echo "Logs: journalctl --user -u $UNIT -f    Instruction for the agent: $CONF/PROMPT.md"
