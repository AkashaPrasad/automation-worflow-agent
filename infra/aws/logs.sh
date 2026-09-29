#!/usr/bin/env bash
# Follow backend logs (pass extra journalctl args, e.g. --since "1 hour ago").
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
opts=(); while IFS= read -r o; do opts+=("$o"); done < <(ssh_opts)
exec ssh -t "${opts[@]}" "ubuntu@$(remote_host)" sudo journalctl -u adjutant -f -o cat "$@"
