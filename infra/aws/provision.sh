#!/usr/bin/env bash
# Idempotent provisioning of the Adjutant EC2 host (ap-south-1, Ubuntu 24.04 arm64, t4g.medium).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"
aws_setup

INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.medium}"
KEY_NAME="adjutant-key"
SG_NAME="adjutant-sg"
TAGS_SPEC() { printf 'ResourceType=%s,Tags=[{Key=Project,Value=adjutant},{Key=Name,Value=%s}]' "$1" "$2"; }

# --- deployer IP ---------------------------------------------------------
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not determine public IP (got '$MY_IP')"
log "deployer public IP: $MY_IP"

# --- key pair ------------------------------------------------------------
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  [ -f "$SSH_KEY" ] || die "key pair $KEY_NAME exists in AWS but $SSH_KEY is missing; delete the key pair in AWS or restore the pem"
  log "key pair $KEY_NAME exists"
else
  log "creating key pair $KEY_NAME"
  umask 077
  aws ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 \
    --tag-specifications "$(TAGS_SPEC key-pair "$KEY_NAME")" \
    --query KeyMaterial --output text > "$SSH_KEY"
  chmod 600 "$SSH_KEY"
fi

# --- security group (default VPC) ------------------------------------------
VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
[ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ] || die "no default VPC in $AWS_REGION"
SG_ID="$(aws ec2 describe-security-groups --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
  log "creating security group $SG_NAME"
  SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" --description "Adjutant backend" --vpc-id "$VPC_ID" \
    --tag-specifications "$(TAGS_SPEC security-group "$SG_NAME")" --query GroupId --output text)"
fi
# 80/443 from anywhere (ignore duplicates)
for port in 80 443; do
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port "$port" --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
done
# SSH: only the deployer's current IP; drop stale SSH rules
aws ec2 describe-security-groups --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' --output text | tr '\t' '\n' | while read -r cidr; do
    [ -n "$cidr" ] && [ "$cidr" != "$MY_IP/32" ] && aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$cidr" >/dev/null
  done || true
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP/32" >/dev/null 2>&1 || true
log "security group $SG_ID (22 from $MY_IP/32, 80/443 open)"

# --- instance ------------------------------------------------------------
INSTANCE_ID="$(aws ec2 describe-instances \
  --filters Name=tag:Project,Values=adjutant Name=tag:Name,Values=adjutant-backend \
            Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId | [0]' --output text)"
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  AMI_ID="$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)"
  log "launching $INSTANCE_TYPE from $AMI_ID"
  INSTANCE_ID="$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options 'HttpTokens=required,HttpEndpoint=enabled' \
    --user-data "file://$INFRA_DIR/aws/user-data.sh" \
    --tag-specifications "$(TAGS_SPEC instance adjutant-backend)" "$(TAGS_SPEC volume adjutant-backend)" \
    --query 'Instances[0].InstanceId' --output text)"
else
  log "instance $INSTANCE_ID exists"
  aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 || true
fi
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# --- elastic IP ------------------------------------------------------------
ALLOC_ID="$(aws ec2 describe-addresses --filters Name=tag:Project,Values=adjutant \
  --query 'Addresses[0].AllocationId' --output text)"
if [ -z "$ALLOC_ID" ] || [ "$ALLOC_ID" = "None" ]; then
  log "allocating Elastic IP"
  ALLOC_ID="$(aws ec2 allocate-address --domain vpc --tag-specifications "$(TAGS_SPEC elastic-ip adjutant-eip)" \
    --query AllocationId --output text)"
fi
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" --allow-reassociation >/dev/null
PUBLIC_IP="$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)"

state_set instance_id "$INSTANCE_ID"
state_set public_ip "$PUBLIC_IP"
state_set allocation_id "$ALLOC_ID"
state_set security_group_id "$SG_ID"
state_set region "$AWS_REGION"
log "instance $INSTANCE_ID, Elastic IP $PUBLIC_IP (saved to infra/state/aws.json)"

# --- wait for SSH and bootstrap ------------------------------------------
log "waiting for SSH..."
for i in $(seq 1 60); do
  if ssh_run true >/dev/null 2>&1; then break; fi
  [ "$i" -eq 60 ] && die "SSH did not become available"
  sleep 5
done
log "waiting for cloud-init bootstrap (installs uv, caddy, sqlite3)..."
ssh_run 'cloud-init status --wait >/dev/null 2>&1 || true; test -f /var/lib/cloud/adjutant-bootstrap-done' \
  || die "bootstrap did not finish; inspect with: infra/aws/ssh.sh 'tail -50 /var/log/adjutant-bootstrap.log'"
log "host ready: $PUBLIC_IP"
