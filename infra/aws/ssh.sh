#!/usr/bin/env bash
# Open a shell on the host, or run a command: infra/aws/ssh.sh [command...]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
opts=(); while IFS= read -r o; do opts+=("$o"); done < <(ssh_opts)
exec ssh "${opts[@]}" "ubuntu@$(remote_host)" "$@"
