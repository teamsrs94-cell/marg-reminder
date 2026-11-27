#!/usr/bin/env bash
# One-time install on the VPS that already runs Enquira (Docker + nginx). Run as root from the repo folder:
#   bash deploy/install.sh reminder.srs-engineering.in you@example.com
set -euo pipefail
DOMAIN="${1:?usage: install.sh <domain> <certbot-email>}"; CERT_EMAIL="${2:?usage: install.sh <domain> <certbot-email>}"
PORT="${APP_PORT:-3200}"
cd "$(dirname "$0")/.."
[ -f .env ] || { cp .env.example .env; sed -i "s#^SECRET_KEY=.*#SECRET_KEY=$(openssl rand -hex 32)#; s#^COOKIE_SECURE=.*#COOKIE_SECURE=true#" .env
  echo "Created .env — edit it now (ADMIN_*, SMTP_*, TWILIO_*) then re-run this script."; exit 0; }
if ss -ltn | grep -q "127.0.0.1:${PORT} "; then docker compose ps | grep -q marg || { echo "Port ${PORT} is used by something else; set APP_PORT"; exit 1; }; fi
grep -q "^APP_URL=" .env || echo "APP_URL=https://${DOMAIN}" >> .env
APP_PORT="$PORT" docker compose up -d --build
CONF="/etc/nginx/sites-available/${DOMAIN}.conf"; [ -d /etc/nginx/sites-available ] || CONF="/etc/nginx/conf.d/${DOMAIN}.conf"
if [ ! -f "$CONF" ]; then
  sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__PORT__/${PORT}/g" deploy/nginx-site.conf > "$CONF"
  [ -d /etc/nginx/sites-enabled ] && ln -sf "$CONF" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
fi
nginx -t && systemctl reload nginx
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERT_EMAIL" --redirect
echo "Done: https://${DOMAIN}"
