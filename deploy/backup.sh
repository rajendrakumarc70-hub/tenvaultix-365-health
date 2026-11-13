#!/usr/bin/env bash
# Copies the whole database (customers, licences, prices, reports) to deploy/backups and keeps 14 days.
# install.sh runs it every night; run it by hand any time:  sudo bash /opt/tenvaultix/deploy/backup.sh
# Also copy the backups folder off this server now and then (Azure Backup does this for an Azure VM).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
chmod 700 backups
file="backups/health-$(date +%Y-%m-%d-%H%M).db"
docker compose exec -T app python -m app.cli backup - > "$file"
chmod 600 "$file"
find backups -name 'health-*.db' -mtime +14 -delete
echo "$(date '+%F %T') backup saved: $file ($(du -h "$file" | cut -f1))"
