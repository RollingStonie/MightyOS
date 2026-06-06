#!/usr/bin/env bash
# Restore Mighty's cron jobs after VPS rebuild
set -e
HERMES_DIR="$HOME/hermes"
mkdir -p ~/.hermes/logs

crontab -l 2>/dev/null | grep -v "fleet_monitor\|lead_ingest\|MightyOS" > /tmp/crontab_existing || true
cat >> /tmp/crontab_existing << 'CRON'
0 * * * * cd ~/hermes && . ~/.hermes/.env && python3 sync/lead_ingest.py >> ~/.hermes/logs/lead_ingest.log 2>&1
*/10 * * * * cd ~/hermes && . ~/.hermes/.env && python3 sync/fleet_monitor.py --check >> ~/.hermes/logs/fleet_monitor.log 2>&1
0 0 * * * cd ~/hermes && . ~/.hermes/.env && python3 sync/fleet_monitor.py --digest >> ~/.hermes/logs/fleet_monitor.log 2>&1
*/5 * * * * git -C ~/MightyOS pull --quiet
CRON
crontab /tmp/crontab_existing
echo "Mighty cron jobs restored."
