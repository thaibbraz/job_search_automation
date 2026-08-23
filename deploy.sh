#!/usr/bin/env bash
# Deploy script_job_search to the Hetzner box that already runs
# jobbyo-job-crawler, as its own docker-compose stack + systemd timer.
#
# Usage:
#   SSH_HOST=1.2.3.4 SSH_USER=root ./deploy.sh
#
# What it does:
#   1. rsync's the repo to $APP_DIR on the server (excluding local data/.env)
#   2. copies local .env to the server (chmod 600) unless SKIP_ENV=1
#   3. installs the systemd service + timer (twice-daily schedule)
#   4. docker compose build && up -d
set -euo pipefail

SSH_HOST="${SSH_HOST:?Set SSH_HOST to the server IP/hostname}"
SSH_USER="${SSH_USER:-root}"
# A plain string, not an array: macOS's stock bash (3.2) throws "unbound
# variable" under `set -u` when expanding an empty array's [@]/[*], even
# guarded by -n checks elsewhere. Word-splitting a scalar sidesteps that —
# fine here since SSH_KEY is a key path, not arbitrary user input.
SSH_KEY_OPT=""
[ -n "${SSH_KEY:-}" ] && SSH_KEY_OPT="-i $SSH_KEY"
APP_DIR="${APP_DIR:-/opt/script_job_search}"
SKIP_ENV="${SKIP_ENV:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_TARGET="${SSH_USER}@${SSH_HOST}"

echo "==> Syncing repo to ${SSH_TARGET}:${APP_DIR}"
ssh $SSH_KEY_OPT "$SSH_TARGET" "mkdir -p '$APP_DIR'"
rsync -az --delete \
  --exclude ".git/" \
  --exclude ".env" \
  --exclude "personas/" \
  --exclude "search_contracts/" \
  --exclude "run_logs/" \
  --exclude "strategy_reports/" \
  --exclude "__pycache__/" \
  --exclude ".claude/" \
  -e "ssh $SSH_KEY_OPT" \
  "$SCRIPT_DIR"/ "$SSH_TARGET:$APP_DIR/"

if [ "$SKIP_ENV" != "1" ]; then
  if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "!! No local .env found and SKIP_ENV != 1 — aborting. Create .env or set SKIP_ENV=1 to skip." >&2
    exit 1
  fi
  echo "==> Copying .env (chmod 600)"
  scp $SSH_KEY_OPT "$SCRIPT_DIR/.env" "$SSH_TARGET:$APP_DIR/.env"
  ssh $SSH_KEY_OPT "$SSH_TARGET" "chmod 600 '$APP_DIR/.env'"
fi

echo "==> Installing systemd units, building and starting containers"
ssh $SSH_KEY_OPT "$SSH_TARGET" bash -s <<REMOTE
set -euo pipefail
APP_DIR="$APP_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found, installing..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

for unit in jobbyo-search.service jobbyo-search-run.service jobbyo-search-run.timer jobbyo-search-run-final.service jobbyo-search-run-final.timer jobbyo-search-daily-report.service jobbyo-search-daily-report.timer jobbyo-boardlinks-topup.service; do
  cp "\$APP_DIR/systemd/\$unit" "/etc/systemd/system/\$unit"
done
systemctl daemon-reload
systemctl enable jobbyo-search.service
systemctl enable --now jobbyo-search-run.timer
systemctl enable --now jobbyo-search-run-final.timer
systemctl enable --now jobbyo-search-daily-report.timer

cd "\$APP_DIR"
echo "Building image..."
\$COMPOSE build
echo "Starting stack..."
\$COMPOSE up -d
systemctl start jobbyo-search.service || true

sleep 5
echo "Container status:"
docker ps --filter "name=jobbyo-search" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# boardsLinks backfill -- deploy-triggered only (no timer, no schedule): a
# new ATS shows up here because a deploy just happened (a new crawler was
# registered in jobbyo-job-crawler and that repo redeployed, publishing an
# updated manifest), so checking on every deploy is the natural cadence,
# not a calendar one. Blocking (not backgrounded) so its result is visible
# in this deploy's own log instead of racing the rest of the script.
echo ""
echo "Running boardsLinks backfill..."
systemctl start --wait jobbyo-boardlinks-topup.service || echo "boardsLinks backfill failed (non-fatal) -- check: journalctl -u jobbyo-boardlinks-topup.service"
echo ""
echo "Next scheduled runs:"
systemctl list-timers 'jobbyo-search*.timer' --no-pager
REMOTE

echo ""
echo "==> Deploy complete."
echo "The API is public on :8010 — every route but /health requires X-Admin-Key (JOBBYO_ADMIN_API_KEY in .env)."
echo "  curl http://${SSH_HOST}:8010/health"
echo "Trigger pass 1 (search only):        ssh $SSH_TARGET systemctl start jobbyo-search-run.service"
echo "Trigger pass 2 (top-up+report):       ssh $SSH_TARGET systemctl start jobbyo-search-run-final.service"
echo "Trigger daily report (email only):    ssh $SSH_TARGET systemctl start jobbyo-search-daily-report.service"
echo "Test one user's email (safe):         ssh $SSH_TARGET curl -s -X POST http://127.0.0.1:8010/email/user -H 'X-Admin-Key: <JOBBYO_ADMIN_API_KEY>' -H 'Content-Type: application/json' -d '{\"email\":\"hello@jobbyo.ai\"}'"
echo "Watch a run:                          ssh $SSH_TARGET journalctl -u jobbyo-search-run-final.service -f"
