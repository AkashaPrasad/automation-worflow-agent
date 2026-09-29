#!/usr/bin/env bash
# Shared helpers. Source this file; never prints secret values.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$INFRA_DIR/.." && pwd)"
STATE_DIR="$INFRA_DIR/state"
mkdir -p "$STATE_DIR"

APP_DOMAIN="aiml.spacesdrive.cc"
API_DOMAIN="api.aiml.spacesdrive.cc"
ZONE_NAME="spacesdrive.cc"
PAGES_PROJECT="adjutant-aiml"
NAME_TAG="adjutant"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Parse KEY=VALUE lines from .env without executing anything and without echoing.
load_env() {
  local f="${1:-$REPO_ROOT/.env}" line key val
  [ -f "$f" ] || die "env file not found: $f"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key#export }"; key="${key// /}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "$val" == \"*\" ]]; then val="${val#\"}"; val="${val%\"}"
    elif [[ "$val" == \'*\' ]]; then val="${val#\'}"; val="${val%\'}"; fi
    # real environment wins over the file
    if [ -z "${!key:-}" ]; then export "$key=$val"; fi
  done < "$f"
}

require_env() {
  local v
  for v in "$@"; do
    [ -n "${!v:-}" ] || die "required variable $v is missing or empty (set it in $REPO_ROOT/.env)"
  done
}

require_cmd() {
  local c
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"; done
}

# AWS: access keys from .env when both are set; otherwise a local CLI profile
# (AWS_PROFILE from .env or the environment, default "claude-dev").
aws_setup() {
  load_env
  export AWS_REGION="${AWS_REGION:-ap-south-1}"
  export AWS_DEFAULT_REGION="$AWS_REGION"
  export AWS_PAGER=""
  if [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    unset AWS_PROFILE AWS_SESSION_TOKEN || true
  else
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY || true
    export AWS_PROFILE="${AWS_PROFILE:-claude-dev}"
  fi
  require_cmd aws jq
  aws sts get-caller-identity --query Account --output text >/dev/null 2>&1 \
    || die "AWS credentials were rejected by STS (set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env, or AWS_PROFILE)"
}

cf_setup() {
  load_env
  require_env CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID
  require_cmd curl jq
}

# cf_api METHOD PATH [json-body]; prints response body, fails if success=false.
cf_api() {
  local method="$1" path="$2" body="${3:-}" out
  if [ -n "$body" ]; then
    out="$(curl -sS -X "$method" "https://api.cloudflare.com/client/v4$path" \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" --data "$body")"
  else
    out="$(curl -sS -X "$method" "https://api.cloudflare.com/client/v4$path" \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json")"
  fi
  if [ "$(printf '%s' "$out" | jq -r '.success // false')" != "true" ]; then
    printf '%s' "$out" | jq -c '.errors // .' >&2 || true
    return 1
  fi
  printf '%s' "$out"
}

cf_zone_id() {
  cf_api GET "/zones?name=$ZONE_NAME" | jq -r '.result[0].id // empty'
}

# state helpers (infra/state/aws.json)
state_get() { [ -f "$STATE_DIR/aws.json" ] && jq -r --arg k "$1" '.[$k] // empty' "$STATE_DIR/aws.json" || true; }
state_set() {
  local f="$STATE_DIR/aws.json" tmp
  [ -f "$f" ] || echo '{}' > "$f"
  tmp="$(mktemp)"; jq --arg k "$1" --arg v "$2" '.[$k]=$v' "$f" > "$tmp" && mv "$tmp" "$f"
  chmod 600 "$f"
}

SSH_KEY="$STATE_DIR/adjutant-key.pem"
ssh_opts() {
  printf '%s\n' -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE_DIR/known_hosts" \
    -o ServerAliveInterval=15 -o ConnectTimeout=10 -o LogLevel=ERROR
}
remote_host() { local ip; ip="$(state_get public_ip)"; [ -n "$ip" ] || die "no public_ip in infra/state/aws.json; run infra/aws/provision.sh first"; printf '%s' "$ip"; }
# ssh_run "cmd..."  (runs as ubuntu)
ssh_run() {
  local opts=(); while IFS= read -r o; do opts+=("$o"); done < <(ssh_opts)
  ssh "${opts[@]}" "ubuntu@$(remote_host)" "$@"
}
