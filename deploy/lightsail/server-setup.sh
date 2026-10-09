#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu 24.04 Lightsail instance. Run as root;
# `lightsail.sh setup` pipes this over SSH. Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl ufw unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades   # security updates install themselves

echo "== docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker ubuntu
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{"log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "3"}}
JSON
systemctl restart docker

echo "== swap (helps the 8 GB plan through index builds)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== firewall (Lightsail's own firewall must also allow 443; see README.md)"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "== app and backup folders, nightly database backup"
install -d -o ubuntu -g ubuntu /opt/food /opt/food-backups
touch /var/log/food-backup.log && chown ubuntu:ubuntu /var/log/food-backup.log
cat > /etc/cron.d/food-backup <<'CRON'
# Nightly dump of the food database at 03:30 server time (UTC); keeps the newest 3.
30 3 * * * ubuntu bash /opt/food/deploy/lightsail/backup.sh >> /var/log/food-backup.log 2>&1
CRON

echo "== done. Docker $(docker --version | cut -d' ' -f3 | tr -d ,) is installed."
