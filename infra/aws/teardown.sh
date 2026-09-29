#!/usr/bin/env bash
# MANUAL USE ONLY. Destroys the instance, Elastic IP, security group and key pair. Requires --yes.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
[ "${1:-}" = "--yes" ] || die "this destroys the Adjutant EC2 host and its data. Re-run with --yes to confirm."
aws_setup

IDS="$(aws ec2 describe-instances --filters Name=tag:Project,Values=adjutant Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ -n "$IDS" ]; then
  log "terminating $IDS"
  # shellcheck disable=SC2086
  aws ec2 terminate-instances --instance-ids $IDS >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --instance-ids $IDS
fi
for alloc in $(aws ec2 describe-addresses --filters Name=tag:Project,Values=adjutant --query 'Addresses[].AllocationId' --output text); do
  log "releasing Elastic IP $alloc"
  aws ec2 release-address --allocation-id "$alloc" >/dev/null || true
done
SG="$(aws ec2 describe-security-groups --filters Name=group-name,Values=adjutant-sg --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -n "$SG" ] && [ "$SG" != "None" ]; then log "deleting security group $SG"; aws ec2 delete-security-group --group-id "$SG" || true; fi
aws ec2 delete-key-pair --key-name adjutant-key >/dev/null 2>&1 || true
rm -f "$STATE_DIR/aws.json" "$SSH_KEY" "$STATE_DIR/known_hosts"
log "done. DNS record api.aiml.spacesdrive.cc still points at the old IP; remove it in Cloudflare if desired."
