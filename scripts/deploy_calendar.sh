#!/bin/bash
# Push the current code to https://lytekeepr.xyz/calendar/ (Hetzner, SSH alias "hetzner-landing").
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
+ /demo_state/
+ /demo_state/*.py
- /demo_state/**
- .git/
- .venv/
- __pycache__/
- *.pyc
- .env
- .DS_Store
- /profiles/data/
- /profiles/active-profile.json
- /profiles/registry.json
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
rsync -az --delete --filter="merge $FILTER" ./ "$HOST:$DEST"
ssh "$HOST" 'chown -R lkcal:lkcal /opt/lytekeepr-calendar/app &&
  su -s /bin/bash lkcal -c "cd /opt/lytekeepr-calendar && venv/bin/pip install -q --disable-pip-version-check -r app/requirements.txt" &&
  systemctl restart lytekeepr-calendar && sleep 3 && systemctl is-active lytekeepr-calendar'
echo "Deployed. Check https://lytekeepr.xyz/calendar/"
