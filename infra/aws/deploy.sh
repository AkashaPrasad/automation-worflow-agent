#!/usr/bin/env bash
# Deploy the backend to the provisioned host and wait for https://api.aiml.spacesdrive.cc/api/health.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
load_env
require_env MODEL_API_KEY TYPESAFE_API_KEY
require_cmd rsync ssh curl jq
[ -f "$SSH_KEY" ] || die "missing $SSH_KEY; run infra/aws/provision.sh first"
HOST="$(remote_host)"
BACKEND="$REPO_ROOT/backend"

# --- lockfile ----------------------------------------------------------
if [ ! -f "$BACKEND/uv.lock" ]; then
  require_cmd uv
  log "uv.lock missing, generating"
  (cd "$BACKEND" && uv lock)
fi

SSH_CMD="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts -o LogLevel=ERROR"

# --- code ----------------------------------------------------------------
log "syncing backend/ to $HOST:/opt/adjutant/backend"
rsync -az --delete --no-owner --no-group \
  --exclude '.venv/' --exclude '*.db' --exclude '*.db-*' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'tests/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.env' --exclude '.env.*' \
  -e "$SSH_CMD" "$BACKEND/" "ubuntu@$HOST:/home/ubuntu/adjutant-src/"
# Stage in the deploy user's home, then install into the service-owned tree on the host
# (keeps the host's .venv; macOS openrsync lacks --chmod and can't set times on dirs it doesn't own).
ssh_run 'sudo install -d -o adjutant -g adjutant /opt/adjutant/backend && sudo rsync -a --delete --exclude ".venv/" --exclude "*.db" --exclude "*.db-*" /home/ubuntu/adjutant-src/ /opt/adjutant/backend/'

# --- secrets: streamed over ssh stdin, never printed or written locally ---
dq() { local v="$1"; v="${v//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }
emit_env() {
  local k
  # fixed deploy values
  printf 'DATABASE_PATH=%s\n' "$(dq /var/lib/adjutant/adjutant.db)"
  printf 'PUBLIC_BASE_URL=%s\n' "$(dq "https://$API_DOMAIN")"
  printf 'FRONTEND_URL=%s\n' "$(dq "https://$APP_DOMAIN")"
  printf 'CORS_ORIGINS=%s\n' "$(dq "https://$APP_DOMAIN,https://$PAGES_PROJECT.pages.dev")"
  # required + optional passthrough (only if set)
  for k in MODEL_API_KEY MODEL_BASE_URL MODEL_NAME TYPESAFE_API_KEY \
           MODEL_PRICE_IN MODEL_PRICE_OUT JEV_MODEL JEV_PRICE_IN LAYA_ENABLED \
           RUNS_PER_HOUR_PER_WORKSPACE MAX_CONCURRENT_RUNS \
           GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET NOTION_TOKEN NOTION_PARENT_PAGE_ID \
           SLACK_BOT_TOKEN FIREFLIES_API_KEY MCP_SERVERS; do
    if [ -n "${!k:-}" ]; then printf '%s=%s\n' "$k" "$(dq "${!k}")"; fi
  done
}
log "writing /etc/adjutant/env (0600, values not shown)"
emit_env | ssh_run 'umask 077; sudo install -d -m 0750 -o root -g adjutant /etc/adjutant; sudo tee /etc/adjutant/env.new >/dev/null && sudo chown root:root /etc/adjutant/env.new && sudo chmod 0600 /etc/adjutant/env.new && sudo mv /etc/adjutant/env.new /etc/adjutant/env'

# --- host install --------------------------------------------------------
log "installing dependencies, Caddyfile, systemd units"
ssh_run 'bash -s' <<'REMOTE'
set -euo pipefail
sudo chown -R adjutant:adjutant /opt/adjutant && sudo chmod -R u+rwX,g+rwX,o+rX /opt/adjutant
sudo -u adjutant env HOME=/home/adjutant UV_CACHE_DIR=/home/adjutant/.cache/uv bash -c 'cd /opt/adjutant/backend && /usr/local/bin/uv sync --frozen --no-dev'
D=/opt/adjutant/backend/deploy
caddy validate --config "$D/Caddyfile" --adapter caddyfile >/dev/null 2>&1 || { echo "Caddyfile failed validation" >&2; caddy validate --config "$D/Caddyfile" --adapter caddyfile >&2; exit 1; }
sudo install -m 0644 "$D/Caddyfile" /etc/caddy/Caddyfile
sudo install -m 0755 "$D/adjutant-backup.sh" /usr/local/bin/adjutant-backup
sudo install -m 0644 "$D/adjutant.service" "$D/adjutant-backup.service" "$D/adjutant-backup.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable adjutant.service adjutant-backup.timer caddy >/dev/null 2>&1
sudo systemctl restart adjutant.service
sudo systemctl start adjutant-backup.timer
sudo systemctl reload-or-restart caddy
sleep 2
systemctl is-active adjutant.service || { sudo journalctl -u adjutant -n 40 --no-pager -o cat >&2; exit 1; }
REMOTE

# --- DNS (needed for ACME) -------------------------------------------------
"$INFRA_DIR/cloudflare/dns.sh"

# --- health ----------------------------------------------------------------
URL="https://$API_DOMAIN/api/health"
log "waiting for $URL (first run includes Let's Encrypt issuance, up to ~5 min)"
for i in $(seq 1 60); do
  # --resolve bypasses stale local DNS caches
  if body="$(curl -fsS --max-time 10 --resolve "$API_DOMAIN:443:$HOST" "$URL" 2>/dev/null)" \
     && [ "$(printf '%s' "$body" | jq -r '.ok // false' 2>/dev/null)" = "true" ]; then
    printf '%s\n' "$body" | jq .
    log "backend healthy"
    exit 0
  fi
  sleep 5
done
ssh_run 'sudo journalctl -u caddy -n 20 --no-pager -o cat; sudo journalctl -u adjutant -n 20 --no-pager -o cat' >&2 || true
die "health check did not pass within 5 minutes"
