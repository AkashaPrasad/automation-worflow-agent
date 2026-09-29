#!/usr/bin/env bash
# Build the frontend, deploy to Cloudflare Pages, attach aiml.spacesdrive.cc, wait until it serves.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
cf_setup
require_cmd npm npx
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID
ACCT="$CLOUDFLARE_ACCOUNT_ID"
FRONTEND="$REPO_ROOT/frontend"
[ -f "$FRONTEND/package.json" ] || die "frontend/package.json not found"

# --- project ---------------------------------------------------------------
proj="$(cf_api GET "/accounts/$ACCT/pages/projects/$PAGES_PROJECT" 2>/dev/null || true)"
if [ -z "$proj" ]; then
  log "creating Pages project $PAGES_PROJECT"
  proj="$(cf_api POST "/accounts/$ACCT/pages/projects" \
    "$(jq -nc --arg n "$PAGES_PROJECT" '{name:$n,production_branch:"main"}')")"
fi
SUBDOMAIN="$(printf '%s' "$proj" | jq -r '.result.subdomain')"
[ -n "$SUBDOMAIN" ] && [ "$SUBDOMAIN" != "null" ] || die "could not read Pages subdomain"

# --- build -------------------------------------------------------------------
log "building frontend (VITE_API_BASE=https://$API_DOMAIN)"
(
  cd "$FRONTEND"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
  VITE_API_BASE="https://$API_DOMAIN" npm run build
)
[ -f "$FRONTEND/dist/index.html" ] || die "frontend/dist/index.html missing after build"
# SPA fallback so /runs/:id, /workspace etc. resolve on refresh
[ -f "$FRONTEND/dist/_redirects" ] || printf '/* /index.html 200\n' > "$FRONTEND/dist/_redirects"

# --- deploy ------------------------------------------------------------------
log "deploying to Pages"
(cd "$REPO_ROOT" && npx --yes wrangler@latest pages deploy frontend/dist --project-name "$PAGES_PROJECT" --branch main --commit-dirty=true)

# --- custom domain -----------------------------------------------------------
doms="$(cf_api GET "/accounts/$ACCT/pages/projects/$PAGES_PROJECT/domains")"
if [ "$(printf '%s' "$doms" | jq -r --arg d "$APP_DOMAIN" '[.result[]|select(.name==$d)]|length')" = "0" ]; then
  cf_api POST "/accounts/$ACCT/pages/projects/$PAGES_PROJECT/domains" "$(jq -nc --arg n "$APP_DOMAIN" '{name:$n}')" >/dev/null
  log "attached custom domain $APP_DOMAIN"
else
  log "custom domain $APP_DOMAIN already attached"
fi

# --- CNAME -------------------------------------------------------------------
ZONE="$(cf_zone_id)"; [ -n "$ZONE" ] || die "zone $ZONE_NAME not found"
body="$(jq -nc --arg n "$APP_DOMAIN" --arg t "$SUBDOMAIN" '{type:"CNAME",name:$n,content:$t,ttl:1,proxied:true}')"
existing="$(cf_api GET "/zones/$ZONE/dns_records?name=$APP_DOMAIN" | jq -r '.result[0] // empty')"
if [ -z "$existing" ]; then
  cf_api POST "/zones/$ZONE/dns_records" "$body" >/dev/null; log "created CNAME $APP_DOMAIN -> $SUBDOMAIN (proxied)"
else
  if [ "$(printf '%s' "$existing" | jq -r '.type')" = "CNAME" ] && [ "$(printf '%s' "$existing" | jq -r .content)" = "$SUBDOMAIN" ]; then
    log "CNAME already correct"
  else
    cf_api PUT "/zones/$ZONE/dns_records/$(printf '%s' "$existing" | jq -r .id)" "$body" >/dev/null; log "updated CNAME"
  fi
fi

# --- wait --------------------------------------------------------------------
log "waiting for https://$APP_DOMAIN (domain validation + cert can take a few minutes)"
for i in $(seq 1 72); do
  code="$(curl -sS -o "$STATE_DIR/site.html" -w '%{http_code}' --max-time 10 "https://$APP_DOMAIN/" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ] && grep -qi '<div id="root"\|<div id=.root' "$STATE_DIR/site.html"; then
    log "https://$APP_DOMAIN is live"; exit 0
  fi
  sleep 5
done
die "https://$APP_DOMAIN did not serve the app within 6 minutes (last HTTP $code); check Pages > Custom domains status"
