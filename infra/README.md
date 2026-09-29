# Adjutant infra runbook

Frontend: Cloudflare Pages `adjutant-aiml` at https://aiml.spacesdrive.cc.
Backend: one EC2 host (ap-south-1, Ubuntu 24.04 arm64, t4g.medium, 20 GB gp3, Elastic IP) running Caddy (auto HTTPS) -> uvicorn (1 worker) under systemd, SQLite on EBS, at https://api.aiml.spacesdrive.cc.

## Prerequisites
- Root `.env` (gitignored) with `MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_NAME`, `TYPESAFE_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION=ap-south-1`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`. Optional integration vars (`GOOGLE_*`, `NOTION_*`, `SLACK_BOT_TOKEN`, `FIREFLIES_API_KEY`, `MCP_SERVERS`, ...) are forwarded when set.
- Local tools: AWS CLI v2, `jq`, `curl`, `rsync`, `ssh`, `uv`, Node 20+ (`npm`, `npx`).
- The AWS user needs EC2 (instances, EIP, SG, key pairs, tags) and `ssm:GetParameter`. Scripts ignore any local AWS profile and use the `.env` keys only.

## One-command deploy
```
infra/deploy-all.sh        # provision -> dns -> backend deploy + health wait -> Pages deploy + domain
```
Or step by step (all idempotent, safe to re-run):
```
infra/aws/provision.sh     # key pair, SG (SSH from your current IP only), instance, EIP; writes infra/state/aws.json
infra/cloudflare/dns.sh    # api.aiml A record -> EIP (DNS-only, required for ACME)
infra/aws/deploy.sh        # rsync backend, write /etc/adjutant/env, uv sync, Caddy + systemd, poll /api/health
infra/cloudflare/pages.sh  # build with VITE_API_BASE, wrangler deploy, attach domain, CNAME, wait
```
Redeploy after code changes: `infra/aws/deploy.sh` (backend) or `infra/cloudflare/pages.sh` (frontend).
If your public IP changes, re-run `infra/aws/provision.sh` to move the SSH rule.

## Operate
- Shell: `infra/aws/ssh.sh [cmd]`; logs: `infra/aws/logs.sh` (journalctl -u adjutant -f). Caddy logs: `infra/aws/ssh.sh sudo journalctl -u caddy -n 50`.
- Status: `infra/aws/ssh.sh systemctl status adjutant caddy`.
- Bootstrap log: `/var/log/adjutant-bootstrap.log` on the host.
- Single uvicorn worker is deliberate (in-process event bus); do not raise `--workers`.

## Secrets
Live only in `.env` (local, gitignored), `/etc/adjutant/env` (0600 root) and `infra/state/` (gitignored: SSH key, ids). To rotate: edit `.env`, run `infra/aws/deploy.sh` (rewrites the env file and restarts). Rotate the SSH key by deleting the `adjutant-key` key pair and `infra/state/adjutant-key.pem`, then teardown/reprovision (or add a new key to `~ubuntu/.ssh/authorized_keys` manually).

## Backups
`adjutant-backup.timer` runs daily 03:30 UTC: `sqlite3 .backup` to `/var/backups/adjutant/adjutant-<ts>.db.gz`, 7-day retention. Run now: `infra/aws/ssh.sh sudo systemctl start adjutant-backup.service`. Restore: stop `adjutant`, `gunzip -c <file> > /var/lib/adjutant/adjutant.db`, chown adjutant, start. Backups sit on the same EBS volume; copy them off-host for real durability.

## Cost (approximate, verify on the AWS pricing page)
- t4g.medium on-demand: about $0.034-0.04/h -> roughly $25-29/month.
- EBS gp3 20 GB: about $2/month.
- Public IPv4 / Elastic IP: $0.005/h -> about $3.6/month.
- Total about $31-35/month plus data transfer. Cloudflare Pages/DNS free tier. Stop the instance to save compute (EIP and EBS still bill).

## Teardown (manual only)
`infra/aws/teardown.sh --yes` terminates the instance and deletes its EBS volume (all data), releases the EIP, deletes the SG and key pair. Then remove the `api.aiml` DNS record, and the Pages project/domain in Cloudflare if wanted.

## E2E tests
```
cd e2e && npm install && npx playwright install chromium
npm test                                                            # local (Vite :5173, API :8000)
E2E_BASE_URL=https://aiml.spacesdrive.cc E2E_API_BASE=https://api.aiml.spacesdrive.cc npm test   # production
```
Workflow specs run real agent loops (minutes each, LLM cost). Report: `npx playwright show-report`.
