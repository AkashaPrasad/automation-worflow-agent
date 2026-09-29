#!/usr/bin/env bash
# Upsert api.aiml.spacesdrive.cc A record -> Elastic IP (DNS-only so Caddy can do ACME).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
cf_setup
IP="$(state_get public_ip)"; [ -n "$IP" ] || die "no public_ip in infra/state/aws.json; run infra/aws/provision.sh first"
ZONE="$(cf_zone_id)"; [ -n "$ZONE" ] || die "zone $ZONE_NAME not found for this token"

body="$(jq -nc --arg n "$API_DOMAIN" --arg ip "$IP" '{type:"A",name:$n,content:$ip,ttl:60,proxied:false,comment:"adjutant backend"}')"
existing="$(cf_api GET "/zones/$ZONE/dns_records?name=$API_DOMAIN" | jq -r '.result[0] // empty')"
if [ -z "$existing" ]; then
  cf_api POST "/zones/$ZONE/dns_records" "$body" >/dev/null
  log "created A $API_DOMAIN -> $IP (DNS-only)"
else
  id="$(printf '%s' "$existing" | jq -r .id)"
  type="$(printf '%s' "$existing" | jq -r .type)"
  [ "$type" = "A" ] || die "existing record for $API_DOMAIN is type $type; remove it manually first"
  if [ "$(printf '%s' "$existing" | jq -r .content)" = "$IP" ] && [ "$(printf '%s' "$existing" | jq -r .proxied)" = "false" ]; then
    log "A $API_DOMAIN already -> $IP"
  else
    cf_api PUT "/zones/$ZONE/dns_records/$id" "$body" >/dev/null
    log "updated A $API_DOMAIN -> $IP (DNS-only)"
  fi
fi
