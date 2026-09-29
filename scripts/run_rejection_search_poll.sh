#!/usr/bin/env bash
# Polls jobbyo-fastapi-server's POST /automations/rejection-searches/run-due
# every couple of minutes (see the .timer alongside this script). That
# endpoint fires one top-up search per user whose reject-batch window has
# elapsed (app/automations/service.py's _on_job_rejected sets the window the
# moment a job is rejected in the webapp) -- this script is just the clock
# that wakes it up; all the actual state (who's due, dedup) lives on the
# automation doc in Firestore, not here.
#
# Same admin key already used for the reverse direction (this box calling
# script_job_search's own /run/* routes) -- see JOBBYO_ADMIN_API_KEY below.
set -euo pipefail

MAIN_API_URL="${JOBBYO_MAIN_API_URL:-https://fastapi-service-03-160893319817.europe-southwest1.run.app}"
ADMIN_API_KEY="${JOBBYO_ADMIN_API_KEY:?Set JOBBYO_ADMIN_API_KEY in .env}"

resp=$(curl -sf --max-time 60 -X POST "${MAIN_API_URL}/automations/rejection-searches/run-due" \
  -H "X-Admin-Key: ${ADMIN_API_KEY}") || {
  echo "[$(date -u +%FT%TZ)] Poll request failed (non-fatal, next timer tick retries)."
  exit 0
}

echo "[$(date -u +%FT%TZ)] ${resp}"
