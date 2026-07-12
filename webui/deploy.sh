#!/usr/bin/env bash
# Deploy the okforge web UI (PLAN.md step 7). Uses sudo for the
# Apache/systemd pieces. Idempotent — re-run after any frontend or backend
# change. NEVER run while an ingest job is running (it restarts the backend).
#
# All overrides are env vars — per-host examples:
#   SERVER_NAME=kb.example.lan ./deploy.sh
#   OPENKB_WEBUI_ENDPOINTS="gpu1=http://gpu1:8080/v1,gpu2=http://gpu2:8081/v1" ./deploy.sh
# (APP_NAME renames the systemd unit, vhost conf, and docroot; the
# OPENKB_WEBUI_* env var names are code-level and stay, like .openkb/.)
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME=${APP_NAME:-okforge-webui}
DOCROOT=${DOCROOT:-/var/www/$APP_NAME}
SERVER_NAME=${SERVER_NAME:-okforge.local}
OPENKB_DIR=${OPENKB_DIR:-/opt/okforge/tooling}
RUN_USER=${RUN_USER:-${SUDO_USER:-$USER}}
OPENKB_WEBUI_KB_ROOT=${OPENKB_WEBUI_KB_ROOT:-/opt/okforge/kbs}
OPENKB_WEBUI_INBOX=${OPENKB_WEBUI_INBOX:-/opt/okforge/inbox}

echo "== static files -> $DOCROOT"
sudo mkdir -p "$DOCROOT"
sudo rsync -a --delete static/ "$DOCROOT"/

echo "== Apache vhost ($SERVER_NAME -> $APP_NAME.conf)"
TMP_CONF=$(mktemp)
sed -e "s/okforge\.local/$SERVER_NAME/g" \
    -e "s/okforge-webui/$APP_NAME/g" \
    deploy/okforge-webui.conf > "$TMP_CONF"
sudo install -m 644 "$TMP_CONF" "/etc/apache2/sites-available/$APP_NAME.conf"
rm -f "$TMP_CONF"
sudo a2enmod -q proxy proxy_http headers
sudo a2ensite -q "$APP_NAME"
sudo apachectl configtest
sudo systemctl reload apache2

echo "== systemd unit ($APP_NAME.service, user=$RUN_USER, dir=$OPENKB_DIR)"
TMP_UNIT=$(mktemp)
sed -e "s|/opt/okforge/tooling|$OPENKB_DIR|g" \
    -e "s/^User=.*/User=$RUN_USER/" \
    -e "s/^Group=.*/Group=$RUN_USER/" \
    -e "s/okforge-webui/$APP_NAME/g" \
    deploy/okforge-webui.service > "$TMP_UNIT"
# Backend config overrides ride the unit as Environment= lines.
# OPENKB_WEBUI_OPENKB_DIR always: the config default derives from the
# service user's $HOME, which is wrong for a /opt install.
sed -i "/^\[Service\]/a Environment=OPENKB_WEBUI_OPENKB_DIR=$OPENKB_DIR" "$TMP_UNIT"
for var in OPENKB_WEBUI_KB_ROOT OPENKB_WEBUI_INBOX OPENKB_WEBUI_MODEL \
           OPENKB_WEBUI_ENDPOINTS OPENKB_WEBUI_DEFAULT_ENDPOINT \
           OPENKB_WEBUI_PUBLIC_SITE_HOST OPENKB_WEBUI_PUBLIC_SITE_DEST \
           OPENKB_WEBUI_QUARTZ_DIR OPENKB_WEBUI_SITES_DIR OPENKB_WEBUI_NODE; do
    val=${!var:-}
    if [ -n "$val" ]; then
        sed -i "/^\[Service\]/a Environment=$var=$val" "$TMP_UNIT"
    fi
done
sudo install -m 644 "$TMP_UNIT" "/etc/systemd/system/$APP_NAME.service"
rm -f "$TMP_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now "$APP_NAME"
# pick up code changes when the unit was already running
sudo systemctl restart "$APP_NAME"

echo "== smoke test"
sleep 2
curl -fsS http://127.0.0.1:8500/api/kbs >/dev/null && echo "backend OK (:8500)"
curl -fsS -H "Host: $SERVER_NAME" http://127.0.0.1/ | grep -qi '<title>ok' \
    && echo "Apache static OK ($SERVER_NAME)"
curl -fsS -H "Host: $SERVER_NAME" http://127.0.0.1/api/kbs >/dev/null \
    && echo "Apache proxy OK (/api/)"

echo
echo "Done. Browse http://$SERVER_NAME/ (clients need '<server-ip> $SERVER_NAME' in /etc/hosts)."
