#!/bin/bash
# Push the current code to https://lytekeepr.xyz/app/ (Hetzner, SSH alias "hetzner-landing").
# Uploads code only: no .env, no data, no tokens, no recordings. The calendar database lives in
# /opt/lytekeepr-calendar/data/ on the server and is never touched by this script.
# Usage: scripts/deploy_calendar.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=hetzner-landing
DEST=/opt/lytekeepr-calendar/app/
FILTER=$(mktemp)
trap 'rm -f "$FILTER"' EXIT
cat > "$FILTER" <<'RULES'
- .git/
- .venv/
- __pycache__/
- *.pyc
- .env
- .DS_Store
- /profiles/data/
- /profiles/deleted/
- /profiles/active-profile.json
- /profiles/registry.json
- /profiles/directory.db*
- /google_oauth/accounts/
- /google_oauth/active-account.json
- /ocr_service/storage_data/
- /ocr_service/tests/
- /mcp-integrations/
- /landing-page/
- /deploy/
- /tests/
- /linkedin-leads/
- /linkedin-post-scrape-test/
- *-mockup.html
- /lk_calendar/calendar.db*
- *.mp4
- *.webm
- *.bak
RULES
if [ "${1:-}" = "--dry-run" ]; then
  rsync -azi --delete --dry-run --filter="merge $FILTER" ./ "$HOST:$DEST" | grep -v '^\.' || true
  exit 0
fi
# Never ship code that lets one workspace see another's data.
echo "Checking that workspaces are kept apart..."
if ! python3 scripts/check_isolation.py > /tmp/lk-isolation.log 2>&1; then
  grep -E "LEAK|CHANGED|FILE|BROKEN|FAILED|REFUSED|COULD NOT" /tmp/lk-isolation.log | head -20
  echo "Deploy stopped: the isolation check did not pass (full output in /tmp/lk-isolation.log)."
  exit 1
fi
grep "PASSED" /tmp/lk-isolation.log
# Never ship code that lets a team member see a brand they are blocked from.
echo "Checking brand access..."
if ! python3 scripts/check_brands.py > /tmp/lk-brands.log 2>&1; then
  grep -E "FAILED|REFUSED|Error" /tmp/lk-brands.log | head -20
  echo "Deploy stopped: the brand check did not pass (full output in /tmp/lk-brands.log)."
  exit 1
fi
grep "PASSED" /tmp/lk-brands.log
# Never ship a LinkedIn helper that puts messages in the wrong workspace or goes past its limits.
echo "Checking LinkedIn..."
if ! python3 scripts/check_linkedin.py > /tmp/lk-linkedin.log 2>&1; then
  grep -E "FAILED|REFUSED|Error" /tmp/lk-linkedin.log | head -20
  echo "Deploy stopped: the LinkedIn check did not pass (full output in /tmp/lk-linkedin.log)."
  exit 1
fi
grep "PASSED" /tmp/lk-linkedin.log
rsync -az --delete --filter="merge $FILTER" ./ "$HOST:$DEST"
ssh "$HOST" 'chown -R lkcal:lkcal /opt/lytekeepr-calendar/app &&
  install -o root -g root -m 700 /opt/lytekeepr-calendar/app/scripts/calendar_backup.py /opt/lytekeepr-calendar/backup.py &&
  install -o root -g root -m 700 /opt/lytekeepr-calendar/app/scripts/offsite_backup.py /opt/lytekeepr-calendar/offsite_backup.py &&
  su -s /bin/bash lkcal -c "cd /opt/lytekeepr-calendar && venv/bin/pip install -q --disable-pip-version-check -r app/requirements.txt" &&
  systemctl restart lytekeepr-calendar && sleep 3 && systemctl is-active lytekeepr-calendar'
echo "Deployed. Check https://lytekeepr.xyz/app/"
