#!/usr/bin/env bash
# Full deploy: provision EC2 -> deploy backend (includes DNS upsert + health wait) -> Cloudflare Pages.
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$D/aws/provision.sh"
"$D/cloudflare/dns.sh"
"$D/aws/deploy.sh"
"$D/cloudflare/pages.sh"
echo "Done: https://aiml.spacesdrive.cc (API https://api.aiml.spacesdrive.cc/api/health)"
