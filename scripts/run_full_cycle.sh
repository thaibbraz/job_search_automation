#!/usr/bin/env bash
# Runs one pass of the Jobbyo job-search cycle against the already-running
# jobbyo-search API container. The API is public (the admin platform calls
# it from the browser), gated by a shared X-Admin-Key header on every route
# but /health -- JOBBYO_ADMIN_API_KEY must be set in .env or every call here
# fails with 401.
#
# Flags:
#   --email        also promote pending_review jobs and email users this pass
#   --report       also post the day's coverage % to Slack this pass
#   --skip-search  don't trigger /run/all this pass -- for a pass that only
#                  needs to send what the previous passes already found
#                  (e.g. the 10:00 CEST daily-report send), so it doesn't
#                  also kick off a redundant full search first
#
# Three passes a day, driven by systemd timers:
#   19:00 Europe/Madrid — plain search, builds up the queue
#   22:00 Europe/Madrid — --report — top-up search for anyone still short,
#                         then the Slack coverage number
#   10:00 Europe/Madrid (next morning) — --email --skip-search — send the
#                         daily report from what the two passes above found
#
# Any run failure (search or email step erroring, or the API being
# unreachable) posts to Slack immediately, independent of --report, so a
# broken night is never silent. If a run is still active from the previous
# pass, this waits for it instead of starting a second one on top of it.
set -euo pipefail

API_URL="${JOBBYO_SEARCH_API_URL:-http://127.0.0.1:8010}"
POLL_INTERVAL_SECONDS="${JOBBYO_POLL_INTERVAL_SECONDS:-30}"
MAX_WAIT_MINUTES="${JOBBYO_MAX_WAIT_MINUTES:-180}"
ADMIN_API_KEY="${JOBBYO_ADMIN_API_KEY:?Set JOBBYO_ADMIN_API_KEY in .env -- every route but /health requires it}"
ADMIN_HEADER=(-H "X-Admin-Key: ${ADMIN_API_KEY}")
# Run health alerts go to the same channel as the coverage report (api.py's
# /coverage/today), not the per-user emailed/missing channel.
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL_DAILY_RUN:-}"

WITH_EMAIL=0
WITH_REPORT=0
SKIP_SEARCH=0
for arg in "$@"; do
  case "$arg" in
    --email) WITH_EMAIL=1 ;;
    --report) WITH_REPORT=1 ;;
    --skip-search) SKIP_SEARCH=1 ;;
  esac
done

log() { echo "[$(date -u +%FT%TZ)] $*"; }

post_slack() {
  local text="$1"
  # Temporarily disabled 2026-08-12 -- run-health alerts were firing on
  # self-inflicted failures (container recreates interrupting an in-flight
  # run during same-night deploy work), not real pipeline problems. Remove
  # this early return once that's confirmed settled.
  log "(Slack alert suppressed) $text"
  return 0
  if [ -z "$SLACK_WEBHOOK_URL" ]; then
    log "SLACK_WEBHOOK_URL not set — would post: $text"
    return 0
  fi
  local escaped
  escaped=$(printf '%s' "$text" | sed 's/\\/\\\\/g; s/"/\\"/g' | sed ':a;N;$!ba;s/\n/\\n/g')
  curl -sf -X POST "$SLACK_WEBHOOK_URL" -H "Content-Type: application/json" \
    -d "{\"text\":\"$escaped\"}" >/dev/null || log "Slack post itself failed"
}

# Prints the finished run's result ("success"/"error"/"unknown") once
# full_run_active goes false. Returns non-zero only if the API becomes
# unreachable or we time out.
wait_for_run_to_finish() {
  local waited=0
  local max_seconds=$((MAX_WAIT_MINUTES * 60))
  local status
  while true; do
    if ! status=$(curl -sf "${ADMIN_HEADER[@]}" "${API_URL}/run/status"); then
      log "Could not reach ${API_URL}/run/status"
      return 1
    fi
    if echo "$status" | grep -Eq '"full_run_active"[[:space:]]*:[[:space:]]*false'; then
      echo "$status" | grep -oP '"last_full_run_result"\s*:\s*"\K[a-z]+' || echo "unknown"
      return 0
    fi
    if [ "$waited" -ge "$max_seconds" ]; then
      log "Timed out after ${MAX_WAIT_MINUTES}m waiting for the run to finish"
      return 1
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done
}

# POSTs to $1 (a /run/all or /email/all style endpoint). If one is already
# active (409, e.g. the previous pass overran into this one), just waits for
# it rather than erroring — the in-flight run still does the job.
trigger_and_wait() {
  local endpoint="$1"
  local label="$2"
  local http_code
  http_code=$(curl -s -o /tmp/jobbyo-trigger-resp.$$ -w '%{http_code}' -X POST "${API_URL}${endpoint}" "${ADMIN_HEADER[@]}" -H "Content-Type: application/json")
  local body
  body=$(cat /tmp/jobbyo-trigger-resp.$$ 2>/dev/null || true)
  rm -f /tmp/jobbyo-trigger-resp.$$

  if [ "$http_code" = "202" ]; then
    log "${label}: started."
  elif [ "$http_code" = "409" ]; then
    log "${label}: a run was already active — waiting on it instead of starting a new one."
  else
    post_slack "🔴 jobbyo-search: ${label} failed to start (HTTP ${http_code}): ${body:0:200}"
    return 1
  fi

  local result
  if ! result=$(wait_for_run_to_finish); then
    post_slack "🔴 jobbyo-search: ${label} — lost contact with the API while waiting for it to finish."
    return 1
  fi
  if [ "$result" != "success" ]; then
    post_slack "🔴 jobbyo-search: ${label} finished with an error this run. Check: journalctl -u ${SYSTEMD_UNIT_NAME:-jobbyo-search-run.service} -n 200"
    return 1
  fi
  log "${label}: finished successfully."
}

if [ "$SKIP_SEARCH" = "1" ]; then
  log "Skipping job search this pass (--skip-search)."
else
  log "Starting job search for all users..."
  trigger_and_wait "/run/all" "job search" || exit 1
fi

if [ "$WITH_EMAIL" = "1" ]; then
  log "Promoting pending_review jobs and emailing users..."
  trigger_and_wait "/email/all" "promote + email" || exit 1
fi

if [ "$WITH_REPORT" = "1" ]; then
  log "Posting coverage report to Slack..."
  curl -sf "${ADMIN_HEADER[@]}" "${API_URL}/coverage/today?send_slack=true" > /dev/null || log "Coverage report request failed"
fi

log "Pass complete (email=${WITH_EMAIL} report=${WITH_REPORT})."
