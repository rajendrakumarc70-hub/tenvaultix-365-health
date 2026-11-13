#!/usr/bin/env bash
# Gets the latest version from GitHub and restarts the app. Customers, licences and reports are kept.
#   sudo bash /opt/tenvaultix/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f deploy/.env ]; then
  echo "deploy/.env is missing. Run install.sh first." >&2
  exit 1
fi
if grep -qE '^TVX_CLIENT_ID=$|^TVX_DOMAIN=$' deploy/.env; then
  echo "Fill in TVX_DOMAIN and TVX_CLIENT_ID in deploy/.env first (sudo nano deploy/.env)." >&2
  exit 1
fi

git pull --ff-only
export TVX_VERSION="$(git rev-parse --short HEAD)"
cd deploy
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
echo "Running version $TVX_VERSION. Check https://$(grep -E '^TVX_DOMAIN=' .env | cut -d= -f2)/setup"
