#!/usr/bin/env bash
# Sets up Tenvaultix 365 Health Check on a fresh Ubuntu 22.04 or 24.04 server (an Azure VM or a Hyper-V VM).
#
#   curl -fsSL https://raw.githubusercontent.com/rajendrakumarc70-hub/tenvaultix-365-health/main/deploy/install.sh -o install.sh
#   bash install.sh
#
# It installs Docker and git, downloads the app to /opt/tenvaultix, creates deploy/.env with a random session
# secret, and adds a daily database backup. It doesn't start the app: fill in deploy/.env first.
set -euo pipefail

REPO="${TVX_REPO:-https://github.com/rajendrakumarc70-hub/tenvaultix-365-health.git}"
DIR="${TVX_DIR:-/opt/tenvaultix}"

echo "==> Installing Docker and git"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git openssl docker.io docker-compose-v2
sudo systemctl enable --now docker

echo "==> Downloading the app to $DIR"
if [ -d "$DIR/.git" ]; then
  sudo git -C "$DIR" pull --ff-only
else
  sudo git clone "$REPO" "$DIR"
fi

cd "$DIR/deploy"
if [ ! -f .env ]; then
  echo "==> Creating $DIR/deploy/.env"
  secret="$(openssl rand -base64 48 | tr -d '\n/+=')"
  sudo cp env.example .env
  sudo sed -i "s|^TVX_SESSION_SECRET=.*|TVX_SESSION_SECRET=${secret}|" .env
  sudo chmod 600 .env
fi
sudo chmod +x install.sh update.sh backup.sh

echo "==> Daily database backup at 02:30 (kept for 14 days in $DIR/deploy/backups)"
cron_line="30 2 * * * $DIR/deploy/backup.sh >> /var/log/tenvaultix-backup.log 2>&1"
( sudo crontab -l 2>/dev/null | grep -v 'tenvaultix.*backup.sh' || true; echo "$cron_line" ) | sudo crontab -

cat <<NEXT

Done. Next:
  1. sudo nano $DIR/deploy/.env        (fill in TVX_DOMAIN, TVX_CLIENT_ID, TVX_CLIENT_SECRET and the rest)
  2. sudo bash $DIR/deploy/update.sh   (builds and starts the app; run it again after every change)
  3. Open https://<your domain>/setup  and check every row says "set"
NEXT
