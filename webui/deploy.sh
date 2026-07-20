#!/usr/bin/env bash
# Deploy the okforge web UI (PLAN.md step 7). Uses sudo for the
# Apache/systemd pieces. Idempotent — re-run after any frontend or backend
# change. NEVER run while an ingest job is running (it restarts the backend).
#
# All overrides are env vars — per-host examples:
#   SERVER_NAME=kb.example.lan ./deploy.sh
#   OKFORGE_WEBUI_ENDPOINTS="gpu1=http://gpu1:8080/v1,gpu2=http://gpu2:8081/v1" ./deploy.sh
# (APP_NAME renames the systemd unit, vhost conf, and docroot.) The
# backend env vars use the OKFORGE_WEBUI_* prefix; the pre-rebrand
# OPENKB_WEBUI_* names (and OPENKB_DIR for the engine dir) are still
# accepted here and written into the unit under the new names.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME=${APP_NAME:-okforge-webui}
DOCROOT=${DOCROOT:-/var/www/$APP_NAME}
SERVER_NAME=${SERVER_NAME:-okforge.local}
# Engine dir (holds .venv with the okforge engine + OCR scripts). New name
# OKFORGE_ENGINE_DIR; legacy OPENKB_DIR still honored.
ENGINE_DIR=${OKFORGE_ENGINE_DIR:-${OPENKB_DIR:-/opt/okforge/tooling}}
RUN_USER=${RUN_USER:-${SUDO_USER:-$USER}}
# KB_ROOT/INBOX always get written to the unit (explicit defaults); new
# OKFORGE_WEBUI_* name first, legacy OPENKB_WEBUI_* second.
OKFORGE_WEBUI_KB_ROOT=${OKFORGE_WEBUI_KB_ROOT:-${OPENKB_WEBUI_KB_ROOT:-/opt/okforge/kbs}}
OKFORGE_WEBUI_INBOX=${OKFORGE_WEBUI_INBOX:-${OPENKB_WEBUI_INBOX:-/opt/okforge/inbox}}
# Space-separated browser origins allowed to call /mcp cross-origin
# (browser-based MCP clients). Empty = CORS off.
MCP_CORS_ORIGINS=${MCP_CORS_ORIGINS:-}

echo "== static files -> $DOCROOT"
sudo mkdir -p "$DOCROOT"
sudo rsync -a --delete static/ "$DOCROOT"/

echo "== Apache vhost ($SERVER_NAME -> $APP_NAME.conf)"
# MCP_CORS_ORIGINS -> quoted ap_expr list; a dummy entry keeps the
# <If> blocks valid-but-inert when no origins are configured.
ORIGINS_EXPR="'cors-disabled'"
if [ -n "$MCP_CORS_ORIGINS" ]; then
    ORIGINS_EXPR=""
    for o in $MCP_CORS_ORIGINS; do ORIGINS_EXPR="$ORIGINS_EXPR'$o', "; done
    ORIGINS_EXPR=${ORIGINS_EXPR%, }
fi
TMP_CONF=$(mktemp)
sed -e "s/okforge\.local/$SERVER_NAME/g" \
    -e "s/okforge-webui/$APP_NAME/g" \
    -e "s#@@MCP_CORS_ORIGINS@@#$ORIGINS_EXPR#g" \
    deploy/okforge-webui.conf > "$TMP_CONF"
sudo install -m 644 "$TMP_CONF" "/etc/apache2/sites-available/$APP_NAME.conf"
rm -f "$TMP_CONF"
sudo a2enmod -q proxy proxy_http headers
sudo a2ensite -q "$APP_NAME"
sudo apachectl configtest
sudo systemctl reload apache2

echo "== systemd unit ($APP_NAME.service, user=$RUN_USER, dir=$ENGINE_DIR)"
TMP_UNIT=$(mktemp)
sed -e "s|/opt/okforge/tooling|$ENGINE_DIR|g" \
    -e "s/^User=.*/User=$RUN_USER/" \
    -e "s/^Group=.*/Group=$RUN_USER/" \
    -e "s/okforge-webui/$APP_NAME/g" \
    deploy/okforge-webui.service > "$TMP_UNIT"
# Backend config overrides ride the unit as Environment= lines, always
# under the new OKFORGE_WEBUI_* names. The engine dir is always written:
# the config default derives from the service user's $HOME, which is
# wrong for a /opt install.
sed -i "/^\[Service\]/a Environment=OKFORGE_WEBUI_ENGINE_DIR=$ENGINE_DIR" "$TMP_UNIT"
# For each passthrough var, take the new OKFORGE_WEBUI_* export if set,
# else the legacy OPENKB_WEBUI_* one, and write it under the new name.
for suffix in KB_ROOT INBOX MODEL ENDPOINTS DEFAULT_ENDPOINT \
              PUBLIC_SITE_HOST PUBLIC_SITE_DEST QUARTZ_DIR SITES_DIR NODE; do
    new="OKFORGE_WEBUI_$suffix"; old="OPENKB_WEBUI_$suffix"
    val=${!new:-${!old:-}}
    if [ -n "$val" ]; then
        sed -i "/^\[Service\]/a Environment=$new=$val" "$TMP_UNIT"
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
