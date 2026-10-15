#!/usr/bin/env bash
# Puts the Mini App on HTTPS: a free certificate from Let's Encrypt and a reverse proxy to the bot's
# local web port (127.0.0.1). Run it as the bot's user; it asks for sudo for the web-server part.
#
#   ./https.sh                              https://vpsd-main.<your-ip>.sslip.io (no domain needed)
#   ./https.sh --domain bot.example.com     your own domain (its A record must point to this server)
#   ./https.sh --port 8443                  another HTTPS port (when 443 belongs to something else)
#   ./https.sh --instance shop              for a second bot
#   sudo ./https.sh --user alice            as root, for the bot that runs under the user alice
#   ./https.sh --rollback [--instance NAME] remove the site and WEB_URL again
#
# Uses nginx + certbot when nginx already runs here (your other sites keep working: a separate
# server block, checked with `nginx -t` before reload), otherwise Caddy (installed from your distro).
# Port 80 must reach this server for the certificate check; ufw, if active, gets 80 and the HTTPS port.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="main"; DOMAIN=""; HPORT=""; ROLLBACK=0; RUNAS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --instance) INSTANCE="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --port) HPORT="${2:-}"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --user) RUNAS="${2:-}"; shift 2 ;;
    -h|--help) sed -n 2,15p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]{1,31}$ ]] || { echo "instance name: 2-32 chars, a-z 0-9 -" >&2; exit 2; }
[ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || { echo "bad domain: $DOMAIN" >&2; exit 2; }
[ -z "$HPORT" ] || { [[ "$HPORT" =~ ^[0-9]+$ ]] && [ "$HPORT" -ge 1 ] && [ "$HPORT" -le 65535 ]; } || { echo "bad port: $HPORT" >&2; exit 2; }

BOT_HOME="$HOME"; AS=()
if [ -n "$RUNAS" ]; then
  [ "$(id -u)" = 0 ] || { echo "--user needs root (run it with sudo)" >&2; exit 2; }
  BOT_HOME="$(getent passwd "$RUNAS" | cut -d: -f6)"
  [ -n "$BOT_HOME" ] || { echo "no such user: $RUNAS" >&2; exit 2; }
  AS=(runuser -u "$RUNAS" -- env "HOME=$BOT_HOME" "XDG_RUNTIME_DIR=/run/user/$(id -u "$RUNAS")")
fi
CONF="$BOT_HOME/.config/vps-director/$INSTANCE"
ENV="$CONF/.env"
UNIT="vps-director@$INSTANCE.service"
NAME="vps-director-$INSTANCE"
NGINX_SITE="/etc/nginx/sites-available/$NAME.conf"
NGINX_LINK="/etc/nginx/sites-enabled/$NAME.conf"
CADDY_SITE="/etc/caddy/$NAME.caddy"
[ -f "$ENV" ] || { echo "$ENV not found: install the bot first (./install.sh --instance $INSTANCE)" >&2; exit 1; }
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"

env_set() {  # KEY VALUE (empty VALUE removes the key)
  "${AS[@]}" python3 - "$HERE" "$ENV" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from vps_director.setup import update_env
update_env(sys.argv[2], {sys.argv[3]: sys.argv[4] or None})
PY
}
env_get() { "${AS[@]}" sed -n "s/^$1=//p" "$ENV" | tail -1 | tr -d '"'"'"; }
active() { systemctl is-active --quiet "$1" 2>/dev/null; }

if [ "$ROLLBACK" = 1 ]; then
  if [ -e "$NGINX_SITE" ] || [ -L "$NGINX_LINK" ]; then
    $SUDO rm -f "$NGINX_LINK" "$NGINX_SITE"
    $SUDO nginx -t && $SUDO systemctl reload nginx
  fi
  if [ -e "$CADDY_SITE" ]; then
    $SUDO rm -f "$CADDY_SITE"
    $SUDO systemctl reload caddy || true
  fi
  env_set WEB_URL ""
  "${AS[@]}" systemctl --user restart "$UNIT"
  echo "Removed: the Mini App site is gone and WEB_URL is cleared (firewall rules and certificates were left as they are)."
  exit 0
fi

# 1. the bot's local web port
WPORT="$(env_get WEB_PORT)"
if [ -z "$WPORT" ]; then
  WPORT=8790
  while ss -Hltn "sport = :$WPORT" | grep -q .; do WPORT=$((WPORT + 1)); done
fi

# 2. the address
if [ -z "$DOMAIN" ]; then
  IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me || true)"
  [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "could not find this server's public IPv4; pass --domain" >&2; exit 1; }
  DOMAIN="vpsd-$INSTANCE.${IP//./-}.sslip.io"
fi

# 3. the web server
if active nginx; then
  KIND=nginx
elif active caddy || command -v caddy >/dev/null; then
  KIND=caddy
else
  busy="$(ss -Hltn '( sport = :80 or sport = :443 )' | awk '{print $4}' | tr '\n' ' ')"
  if [ -n "$busy" ]; then
    echo "Ports 80/443 are taken by something that is neither nginx nor Caddy ($busy)." >&2
    echo "Put a reverse proxy to http://127.0.0.1:$WPORT in front yourself, then set WEB_PORT=$WPORT and WEB_URL in $ENV." >&2
    exit 1
  fi
  KIND=caddy
  echo "== installing Caddy"
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq caddy
fi
HPORT="${HPORT:-443}"
URL="https://$DOMAIN"; [ "$HPORT" = 443 ] || URL="$URL:$HPORT"
echo "== $URL → 127.0.0.1:$WPORT via $KIND"

# a port someone else (not our web server) already listens on cannot be ours
owner="$(ss -Hltnp "sport = :$HPORT" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2 || true)"
if [ -z "$owner" ] && ss -Hltn "sport = :$HPORT" | grep -q .; then owner="$($SUDO ss -Hltnp "sport = :$HPORT" | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2 || true)"; fi
if [ -n "$owner" ] && [ "$owner" != "$KIND" ]; then
  echo "Port $HPORT belongs to $owner. Choose another one: ./https.sh --port 8443" >&2
  exit 1
fi

if active ufw && $SUDO ufw status | grep -q "Status: active"; then
  echo "== ufw: 80 and $HPORT"
  rules="$($SUDO ufw status)"  # an existing rule is left alone: "allow" again would overwrite its comment
  echo "$rules" | grep -qE "^80/tcp +ALLOW" || $SUDO ufw allow 80/tcp comment "acme $NAME" >/dev/null
  echo "$rules" | grep -qE "^$HPORT/tcp +ALLOW" || $SUDO ufw allow "$HPORT"/tcp comment "$NAME" >/dev/null
fi

if [ "$KIND" = nginx ]; then
  command -v certbot >/dev/null || { echo "== installing certbot"; $SUDO apt-get install -y -qq certbot; }
  VAR="vpsd_conn_${INSTANCE//-/_}"
  ver="$(nginx -v 2>&1 | grep -o '[0-9][0-9.]*' | head -1)"
  if [ "$(printf '%s\n1.25.1\n' "$ver" | sort -V | head -1)" = 1.25.1 ]; then H2_ON="    http2 on;"; else H2_ON=""; fi  # older nginx: an "http2" listen flag would switch every site on that port
  echo "== nginx: port 80 for the certificate check"
  $SUDO mkdir -p /var/www/acme
  $SUDO tee "$NGINX_SITE" >/dev/null <<EOF
# VPS Director Mini App ($INSTANCE), written by https.sh; remove with ./https.sh --rollback --instance $INSTANCE
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 301 $URL\$request_uri; }
}
EOF
  $SUDO ln -sf "$NGINX_SITE" "$NGINX_LINK"
  $SUDO nginx -t && $SUDO systemctl reload nginx
  echo "== certificate"
  $SUDO certbot certonly --webroot -w /var/www/acme -d "$DOMAIN" --non-interactive --agree-tos \
    --register-unsafely-without-email --keep-until-expiring --deploy-hook "systemctl reload nginx"
  $SUDO tee -a "$NGINX_SITE" >/dev/null <<EOF

map \$http_upgrade \$$VAR { default upgrade; '' close; }
server {
    listen $HPORT ssl;
    listen [::]:$HPORT ssl;
$H2_ON
    server_name $DOMAIN;
    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 60m;
    access_log off;
    location / {
        proxy_pass http://127.0.0.1:$WPORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$$VAR;
        proxy_read_timeout 1h;
        proxy_buffering off;
    }
}
EOF
  if ! $SUDO nginx -t; then
    $SUDO rm -f "$NGINX_LINK"; $SUDO nginx -t && $SUDO systemctl reload nginx
    echo "nginx rejected the site; it was disabled again, your other sites are untouched" >&2
    exit 1
  fi
  $SUDO systemctl reload nginx
else
  echo "== Caddy"
  $SUDO tee "$CADDY_SITE" >/dev/null <<EOF
# VPS Director Mini App ($INSTANCE), written by https.sh; remove with ./https.sh --rollback --instance $INSTANCE
$DOMAIN:$HPORT {
    reverse_proxy 127.0.0.1:$WPORT
}
EOF
  if ! $SUDO grep -q '^import /etc/caddy/vps-director-\*\.caddy' /etc/caddy/Caddyfile 2>/dev/null; then
    # the distro's stock Caddyfile serves a demo page on :80, which would take port 80 from the certificate check
    if $SUDO grep -q 'root \* /usr/share/caddy' /etc/caddy/Caddyfile 2>/dev/null; then
      $SUDO mv /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig-vps-director
      echo "# the stock demo site was moved to Caddyfile.orig-vps-director by VPS Director's https.sh" | $SUDO tee /etc/caddy/Caddyfile >/dev/null
    fi
    echo 'import /etc/caddy/vps-director-*.caddy' | $SUDO tee -a /etc/caddy/Caddyfile >/dev/null
  fi
  $SUDO caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
  $SUDO systemctl enable --now caddy >/dev/null 2>&1 || true
  $SUDO systemctl reload caddy
fi

# 4. the bot
env_set WEB_PORT "$WPORT"
env_set WEB_URL "$URL"
"${AS[@]}" systemctl --user restart "$UNIT"
echo "== checking $URL"
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/" || true)"
  [ "$code" = 200 ] && break
  sleep 3
done
if [ "$code" = 200 ]; then
  echo
  echo "Done: $URL"
  echo "In Telegram the bot's menu button now opens the Mini App; /dashboard gives a one-time link for a browser."
else
  echo "The site does not answer yet (HTTP ${code:-none}). Check: journalctl --user -u $UNIT -n 50" >&2
  exit 1
fi
