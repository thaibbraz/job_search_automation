#!/usr/bin/env bash
# Daily first-touch nudge for hot leads (see api.py POST /leads/hot/nudge-due).
# Sends for real by default, since that's what the systemd timer runs; pass
# --dry-run to only print what would be sent (no emails, no queue changes).
set -euo pipefail

API_URL="${JOBBYO_SEARCH_API_URL:-http://127.0.0.1:8010}"
ADMIN_API_KEY="${JOBBYO_ADMIN_API_KEY:?Set JOBBYO_ADMIN_API_KEY in .env}"

DRY_RUN=false
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=true
done

echo "[$(date -u +%FT%TZ)] Nudging due hot leads (dry_run=${DRY_RUN})..."
# Long timeout: each send is one LLM call plus a PDF render, run sequentially.
curl -sf --max-time 1800 -X POST "${API_URL}/leads/hot/nudge-due?dry_run=${DRY_RUN}" \
  -H "X-Admin-Key: ${ADMIN_API_KEY}"
echo
echo "[$(date -u +%FT%TZ)] Done."
