#!/usr/bin/env bash
# cloud-init user-data: bootstrap the Adjutant host. Idempotent, contains no secrets.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
exec > >(tee -a /var/log/adjutant-bootstrap.log) 2>&1

apt-get update -y
apt-get install -y curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https \
  sqlite3 rsync git build-essential unzip

# Caddy (official apt repo)
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

# service user and directories
id adjutant >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/adjutant --shell /usr/sbin/nologin adjutant
install -d -o adjutant -g adjutant -m 0755 /opt/adjutant /opt/adjutant/backend
install -d -o adjutant -g adjutant -m 0750 /var/lib/adjutant
install -d -o adjutant -g adjutant -m 0750 /var/backups/adjutant
install -d -o root -g adjutant -m 0750 /etc/adjutant
# let the ssh user (ubuntu) write into the app dir during deploys
usermod -aG adjutant ubuntu || true
chmod 0775 /opt/adjutant /opt/adjutant/backend

# uv, installed system-wide
if [ ! -x /usr/local/bin/uv ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

touch /var/lib/cloud/adjutant-bootstrap-done
