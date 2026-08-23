# script_job_search

Automated job-search pipeline for Jobbyo candidates. Every night it sources candidate jobs, grades them against each user's profile with an LLM, posts the approved ones to the candidate's queue, and (separately) promotes and emails a daily digest. A FastAPI wrapper exposes the same pipeline over HTTP for on-demand use — a new subscriber, or a manual top-up — as well as the nightly schedule.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full diagram, schedule, component reference, and known operational gotchas. This file is the quick-start.

## Repo layout

| Path | What it is |
|---|---|
| `send_jobbyo.py` | Core pipeline — source candidates, AI-review, post approved jobs |
| `approve_jobs.py` | Promotes `pending_review` jobs, sends the candidate digest email + Slack report |
| `company_ingestion.py` | Best-effort: feeds newly-discovered ATS board links back into the sibling `jobbyo-job-crawler` project's bucket |
| `api.py` | FastAPI wrapper — runs the two scripts above as subprocesses, exposed over HTTP |
| `Dockerfile`, `docker-compose.yml` | Container build + the API/Caddy stack |
| `Caddyfile` | Reverse proxy config — public HTTPS entry point |
| `systemd/` | Unit + timer files for the nightly schedule and the container keeper |
| `scripts/run_full_cycle.sh` | What the systemd timers actually invoke — polls the API, posts Slack on failure |
| `personas/`, `search_contracts/` | Per-user LLM-generated artifacts, cached so they aren't rebuilt (and re-billed) every run |
| `run_logs/` | JSON output per run; auto-pruned after 7 days (only the newest file is ever read back) |

## Local usage

Requires `OPENAI_API_KEY` (or `--nogpt`) plus the keys in `env.example` — copy it to `.env` and fill in `JOBBYO_APIFY_TOKEN`, `JOBO_API_KEY`, `BREVO_API_KEY`, `SLACK_WEBHOOK_URL_DAILY_RUN` / `SLACK_WEBHOOK_URL_USER_DETAILS`, etc.

```bash
pip install -r requirements.txt

# Full run, every eligible paid user
python3 send_jobbyo.py

# One user only
python3 send_jobbyo.py --uid <firebase-uid>
python3 send_jobbyo.py --email user@example.com

# Preview without writing to a real queue
JOBBYO_DRY_RUN=1 python3 send_jobbyo.py

# Run as if it were a different day (affects daily-quota checks only)
JOBBYO_RUN_DATE_OFFSET_DAYS=1 python3 send_jobbyo.py

# Promote + email whatever's pending
python3 approve_jobs.py
```

## Deployment

Docker Compose runs two containers behind each other: `jobbyo-search` (the API, loopback-only on the host) and `jobbyo-search-caddy` (public HTTPS, auto-TLS via Let's Encrypt, reverse-proxying to the API). systemd drives everything else — a keeper service for the compose stack, and two nightly timers (19:00 / 22:00 Europe/Madrid) that hit the API over HTTP and retry on failure.

```bash
SSH_HOST=<server> ./deploy.sh
```

Full detail — ports, retry behavior, what each timer actually runs — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-daily-schedule).

## Before you assume a setting stuck

This pipeline calls out to a separate backend (Cloud Run, not in this repo) that is the actual source of truth for every candidate's job queue. It has been observed to silently reset a user's queue shortly after a legitimate write — `api_post_jobs()` now reads the queue back after every real post and re-tries once if anything's missing, logging loudly if it still doesn't stick. See [docs/ARCHITECTURE.md § Operational notes](docs/ARCHITECTURE.md#operational-notes) before assuming a "successful" post actually landed.
