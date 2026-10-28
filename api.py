"""
api.py — FastAPI app to trigger job-search and email workflows.

Endpoints
---------
GET  /health                  Health check
GET  /run/status              Current run state + last run info

POST /run/all                 Trigger send_jobbyo.py for all users
POST /run/user                Trigger send_jobbyo.py for one user  (body: {uid?, email?})
POST /run/user/subscribed     Run for one user, block until done, then send the welcome
                               email (body: {uid?, email?}) — called from the Stripe
                               successful-checkout webhook, once payment is confirmed. The
                               welcome email is outreach.send_paid_welcome_email: a personal
                               note (persona coaching, one alternative angle, a market
                               snapshot), all of that run's real matches, and a strategy PDF
                               attached — no trial pitch, they're already a customer.

POST /email/all               Trigger approve_jobs.py for all users (promote + email)
POST /email/user              Trigger approve_jobs.py for one user  (body: {email})

POST /approve/all             Alias for /email/all
POST /approve/user            Alias for /email/user

GET  /coverage/today          Per-user job counts today + coverage %  (query: ?send_slack=true)
GET  /coverage/missing        Paid users not yet emailed today (per approve_jobs.py's send log)
POST /email/missing           Force-send the daily report to everyone not yet emailed today

POST /leads/hot                Score a just-created automation's salary/location; if it clears
                               the hot-lead bar (see lead_scoring.py), record it.
                               Called from jobbyo-fastapi-server's create_automation_job for
                               anyone not already paid/trialing — no-ops otherwise.
POST /leads/hot/nudge-due      Daily job: first-touch email to leads older than 24h who still
                               haven't started a trial (query: ?dry_run=false to actually send;
                               defaults to a dry run that only returns previews).

Usage
-----
    uvicorn api:app --host 0.0.0.0 --port 8000

Set JOBBYO_TARGET_JOBS in .env to control the per-user daily target (default 9).
"""

import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from date_utils import parse_utc_timestamp
import team_corrections

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable
RUN_LOG_DIR = SCRIPT_DIR / "run_logs"
SYNC_RUN_TIMEOUT_SECONDS = 600

# /run/user/subscribed's caller (jobbyo-fastapi-server's Stripe successful-
# checkout webhook) fires it as a detached background task with no read
# timeout of its own — it's happy to wait however long the search takes. So
# give it a much longer budget than the other synchronous endpoints, which a
# frontend is waiting on for a loading state.
SUBSCRIBED_RUN_TIMEOUT_SECONDS = 1800

# Tasks kept alive here so they aren't garbage-collected mid-run: used when a
# subscribed run outlives SUBSCRIBED_RUN_TIMEOUT_SECONDS — asyncio.shield
# keeps the underlying subprocess going instead of abandoning it, but nothing
# else holds a reference to that task once the request handler returns.
_background_keepalive: set = set()

# While testing, redirect every first-matches email here instead of to the
# candidate. Unset in production so real candidates receive their own.
TOP_JOBS_TEST_RECIPIENT = os.getenv("JOBBYO_TOP_JOBS_TEST_RECIPIENT", "").strip()
# /run/user/subscribed runs the whole pipeline, so each call takes minutes
# and costs real money. Allow one per candidate per window. (Env var name
# kept from this endpoint's now-removed sibling, /run/user/top-jobs, to
# avoid a server .env change.)
SUBSCRIBED_COOLDOWN_SECONDS = int(os.getenv("JOBBYO_TOP_JOBS_COOLDOWN_SECONDS", "86400"))
_subscribed_last_run = {} # normalized uid/email -> datetime of last accepted /run/user/subscribed call

BACKEND_BASE = os.getenv("JOBBYO_BACKEND_URL", "https://fastapi-service-03-160893319817.europe-southwest1.run.app")
# Run health / coverage % — as opposed to per-user emailed/missing detail,
# which approve_jobs.py posts to SLACK_WEBHOOK_URL_USER_DETAILS instead.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL_DAILY_RUN", "")
# Posted to both #operations (its home channel) and #job-library, per
# request. Deduped so a misconfiguration pointing both at the same webhook
# doesn't double-post.
SLACK_WEBHOOK_URLS = list(dict.fromkeys(
    url for url in (SLACK_WEBHOOK_URL, os.getenv("SLACK_WEBHOOK_URL_NEW_ATS", "")) if url
))
# Slack member IDs (the "Copy member ID" value from a person's Slack
# profile, not their @handle) for the zero-match escalation below. Rendered
# as a real <@ID> mention (pings them) when set; falls back to a plain,
# non-pinging "@name" if the env var isn't configured yet.
SLACK_USER_ID_GALIH = os.getenv("SLACK_USER_ID_GALIH", "").strip()
SLACK_USER_ID_NADIA = os.getenv("SLACK_USER_ID_NADIA", "").strip()
SLACK_MENTION_GALIH = f"<@{SLACK_USER_ID_GALIH}>" if SLACK_USER_ID_GALIH else "@galih"
SLACK_MENTION_NADIA = f"<@{SLACK_USER_ID_NADIA}>" if SLACK_USER_ID_NADIA else "@nadia"
# ---------------------------------------------------------------------------
# Run state — in-memory, resets on restart
# ---------------------------------------------------------------------------

class _State:
    full_run_active: bool = False
    full_run_started_at: Optional[datetime] = None
    full_run_script: Optional[str] = None
    last_full_run_at: Optional[datetime] = None
    last_full_run_result: Optional[str] = None   # "success" | "error"
    last_full_run_exit_code: Optional[int] = None

_state = _State()

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class UserTarget(BaseModel):
    uid:   Optional[str] = None
    email: Optional[str] = None

class RunResponse(BaseModel):
    accepted: bool
    message:  str


class TopJob(BaseModel):
    title:    Optional[str] = None
    company:  Optional[str] = None
    url:      Optional[str] = None
    location: Optional[str] = None
    grade:    Optional[int] = None
    reason:   Optional[str] = None


class SubscribedJobsResponse(BaseModel):
    accepted:        bool
    message:         str
    duration_seconds: float
    jobs_found:      int
    emailed:         bool = False
    jobs:            list[TopJob] = []

class StatusResponse(BaseModel):
    full_run_active:            bool
    full_run_script:            Optional[str]
    full_run_started_at:        Optional[str]
    last_full_run_at:           Optional[str]
    last_full_run_result:       Optional[str]
    last_full_run_exit_code:    Optional[int]

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Search settings (admin Job Search page): which AI model the search uses and
# the quality/volume profile. Saved in run_logs/ (a persistent volume) and
# applied to every run, scheduled or manual, unless a run overrides them.
# ---------------------------------------------------------------------------

SEARCH_SETTINGS_PATH = RUN_LOG_DIR / "search_settings.json"

SEARCH_MODELS = [
    {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini", "provider": "openai", "price": [0.40, 1.60], "note": "Current default. Cheap, good enough for most users."},
    {"id": "gpt-4.1", "label": "GPT-4.1", "provider": "openai", "price": [2.00, 8.00], "note": "Stronger reviews, ~5x the AI cost."},
    {"id": "gpt-4.1-nano", "label": "GPT-4.1 nano", "provider": "openai", "price": [0.10, 0.40], "note": "Cheapest. Reviews get noticeably looser. Web search stays on GPT-4.1 mini."},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "provider": "anthropic", "price": [0.10, 0.50], "note": "Very cheap Claude option."},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "provider": "anthropic", "price": [2.00, 10.00], "note": "Strong reviews at a mid price."},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "anthropic", "price": [4.00, 20.00], "note": "Best quality, slowest and most expensive."},
]
FIRST_MATCH_MODELS = [m for m in SEARCH_MODELS if m["provider"] == "anthropic"]

SEARCH_PROFILES = [
    {"id": "quality", "label": "Better batches, fewer jobs",
     "note": "Only strong matches (Tier A/B). Skips the loosened last round, so some users end the day under 10."},
    {"id": "balanced", "label": "Balanced", "note": "Today's behaviour: strong matches first, safe fallbacks to reach the daily target."},
    {"id": "volume", "label": "More jobs, looser",
     "note": "Bigger pools from Hiring Cafe/Jobo and an extra AI search batch. Fills more queues; more weak matches."},
]
_PROFILE_ENV = {
    "quality": {"JOBBYO_PROFILE": "quality"},
    "balanced": {"JOBBYO_PROFILE": "balanced"},
    "volume": {"JOBBYO_PROFILE": "volume", "JOBBYO_HC_MAX_ITEMS": "160", "JOBBYO_JOBO_MAX_ITEMS": "140",
               "JOBBYO_JFE_MAX_ITEMS": "50", "JOBBYO_OPENAI_MAX_BATCHES_PER_USER": "2"},
}
_SETTINGS_DEFAULTS = {"model": "gpt-4.1-mini", "profile": "balanced", "first_match_model": "claude-opus-5-5"}


def _load_search_settings() -> dict:
    try:
        saved = json.loads(SEARCH_SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        saved = {}
    return {**_SETTINGS_DEFAULTS, **{k: v for k, v in saved.items() if v}}


def _search_env(model: Optional[str] = None, profile: Optional[str] = None) -> dict:
    """Env for a send_jobbyo.py run: saved settings, overridden per run."""
    settings = _load_search_settings()
    model = model or settings["model"]
    profile = profile or settings["profile"]
    env = dict(_PROFILE_ENV.get(profile, _PROFILE_ENV["balanced"]))
    meta = next((m for m in SEARCH_MODELS if m["id"] == model), SEARCH_MODELS[0])
    if meta["provider"] == "anthropic":
        env.update({"JOBBYO_LLM_PROVIDER": "anthropic", "JOBBYO_LLM_FALLBACK_PROVIDER": "openai",
                    "JOBBYO_ANTHROPIC_MODEL": meta["id"]})
    else:
        # nano has no web search tool: keep search on mini, use nano for the rest.
        search_model = "gpt-4.1-mini" if meta["id"] == "gpt-4.1-nano" else meta["id"]
        env.update({"JOBBYO_LLM_PROVIDER": "openai", "JOBBYO_LLM_FALLBACK_PROVIDER": "anthropic",
                    "JOBBYO_SEARCH_MODEL": search_model, "JOBBYO_REVIEW_MODEL": meta["id"],
                    "JOBBYO_RESOLUTION_MODEL": search_model})
    return env


# Live view for the admin's Job Search page: the last lines of every run this
# process started (scheduled runs come through /run/all too), in memory only.
_LIVE_RUNS: dict = {}
_LIVE_MAX_LINES = 1500
_LIVE_KEEP_FINISHED = 25


def _live_start(label: str, cmd: list[str]) -> dict:
    from collections import deque
    run = {"label": label, "cmd": " ".join(cmd[1:]), "started_at": datetime.now(timezone.utc).isoformat(),
           "finished_at": None, "exit_code": None, "lines": deque(maxlen=_LIVE_MAX_LINES), "line_count": 0}
    _LIVE_RUNS[f"{label}@{run['started_at']}"] = run
    finished = sorted((k for k, r in _LIVE_RUNS.items() if r["finished_at"]), key=lambda k: _LIVE_RUNS[k]["finished_at"])
    for k in finished[:-_LIVE_KEEP_FINISHED]:
        _LIVE_RUNS.pop(k, None)
    return run


async def _run_subprocess(cmd: list[str], label: str, env: Optional[dict] = None) -> int:
    print(f"[{label}] Starting: {' '.join(cmd)}")
    live = _live_start(label, cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(SCRIPT_DIR),
        env={**os.environ, **env} if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for line in proc.stdout:
        text = line.decode(errors='replace').rstrip()
        print(f"[{label}] {text}")
        live["lines"].append(text)
        live["line_count"] += 1
    await proc.wait()
    code = proc.returncode or 0
    print(f"[{label}] Finished — exit code {code}")
    live["finished_at"] = datetime.now(timezone.utc).isoformat()
    live["exit_code"] = code
    return code


async def _full_run(script: str, args: list[str], label: str, env: Optional[dict] = None):
    """Background task for full (all-user) runs. Sets _state flags."""
    _state.full_run_active = True
    _state.full_run_started_at = datetime.now(timezone.utc)
    _state.full_run_script = script
    try:
        code = await _run_subprocess([PYTHON, script, *args], label, env=env)
        _state.last_full_run_result = "success" if code == 0 else "error"
        _state.last_full_run_exit_code = code
    except Exception as exc:
        print(f"[{label}] Exception: {exc}")
        _state.last_full_run_result = "error"
        _state.last_full_run_exit_code = -1
    finally:
        _state.full_run_active = False
        _state.last_full_run_at = datetime.now(timezone.utc)
        _state.full_run_script = None


async def _single_run(script: str, args: list[str], label: str, env: Optional[dict] = None):
    """Background task for single-user runs. Does NOT lock full-run state."""
    try:
        await _run_subprocess([PYTHON, script, *args], label, env=env)
    except Exception as exc:
        print(f"[{label}] Exception: {exc}")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Jobbyo Runner API", version="1.0.0")

# Called directly from the browser (jobbyo-webapp-frontend), so the browser's
# CORS preflight (OPTIONS) needs an explicit answer — FastAPI returns a bare
# 405 for OPTIONS on any route unless this is registered. No cookies/auth
# tokens go through this endpoint (just an email in the body), so a wildcard
# origin is fine here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# This API is reachable from the internet (the admin platform calls it
# directly from the browser) and /run/* triggers real LLM spend, so every
# route but /health requires a shared secret. Fails closed if the key isn't
# configured, rather than silently running open.
ADMIN_API_KEY = os.getenv("JOBBYO_ADMIN_API_KEY", "")
PUBLIC_PATHS = {
    "/health",
    # Left public -- keeping a shared key in sync between the two services
    # has repeatedly drifted (see git history). Safe to leave open because
    # --assume-paid no longer just trusts the caller's word for it:
    # resolve_requested_user (send_jobbyo.py) verifies against /users/paid
    # for real, with a short retry, before treating anyone as paid -- a
    # request for a uid that genuinely isn't paid gets rejected regardless
    # of who sent it.
    "/run/user/subscribed",
}

@app.middleware("http")
async def require_admin_key(request, call_next):
    if request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        if not ADMIN_API_KEY or request.headers.get("x-admin-key") != ADMIN_API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-Admin-Key header."})
    return await call_next(request)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/run/status", response_model=StatusResponse)
async def run_status():
    return StatusResponse(
        full_run_active=_state.full_run_active,
        full_run_script=_state.full_run_script,
        full_run_started_at=_state.full_run_started_at.isoformat() if _state.full_run_started_at else None,
        last_full_run_at=_state.last_full_run_at.isoformat() if _state.last_full_run_at else None,
        last_full_run_result=_state.last_full_run_result,
        last_full_run_exit_code=_state.last_full_run_exit_code,
    )


# --- Job search runs -------------------------------------------------------

@app.post("/run/all", response_model=RunResponse, status_code=202)
async def run_all(background_tasks: BackgroundTasks):
    """Trigger a top-up run for all users."""
    if _state.full_run_active:
        raise HTTPException(
            status_code=409,
            detail=f"A full run is already active (started {_state.full_run_started_at.isoformat() if _state.full_run_started_at else 'unknown'}). Check /run/status.",
        )
    background_tasks.add_task(_full_run, "send_jobbyo.py", [], "run:all", _search_env())
    return RunResponse(accepted=True, message="Top-up run started for all users. Check /run/status.")


@app.post("/run/user", response_model=RunResponse, status_code=202)
async def run_user(target: UserTarget, background_tasks: BackgroundTasks):
    """Trigger a top-up run for a single user (by uid or email)."""
    if not target.uid and not target.email:
        raise HTTPException(status_code=422, detail="Provide uid or email.")
    if target.uid:
        args, label = ["--uid", target.uid], f"run:user:{target.uid}"
    else:
        args, label = ["--email", target.email], f"run:user:{target.email}"
    background_tasks.add_task(_single_run, "send_jobbyo.py", args, label, _search_env())
    return RunResponse(accepted=True, message=f"Top-up run started for {target.uid or target.email}.")


async def _finish_subscribed_run(
    target: "UserTarget", label: str, existing_logs: set, started_at: float, code: int
) -> SubscribedJobsResponse:
    """Read the completed run's jobs_added and email the user. Shared by the
    normal (fast) path and the timeout-continuation path in
    run_user_subscribed below — raises RuntimeError on a non-zero exit so
    each caller can decide how to surface that (HTTP 500 vs. just logging it,
    since a background continuation has no request to respond to)."""
    duration_seconds = round(asyncio.get_event_loop().time() - started_at, 1)

    if code != 0:
        raise RuntimeError(f"Run failed with exit code {code}. Check server logs for '{label}'.")

    # A single-user run can go through up to 3 rounds (first/second/minimum-
    # viable) if the first round falls short, and each round writes its own
    # job_run_*.json snapshot — with each later snapshot being a cumulative
    # superset of the ones before it (send_jobbyo.py's main() re-saves the
    # full accumulated result list after every round it runs). So the newest
    # new log file already has everything; merge every entry it has for this
    # user (one per round they were part of), deduped by job_url.
    new_logs = sorted(set(RUN_LOG_DIR.glob("job_run_*.json")) - existing_logs)
    jobs_added = []
    user_entries = []
    resolved_uid, resolved_email = None, None
    if new_logs:
        try:
            with open(new_logs[-1], encoding="utf-8") as f:
                results = json.load(f)
        except Exception:
            results = []
        seen_urls = set()
        for r in results:
            if not ((target.uid and r.get("uid") == target.uid) or (target.email and r.get("email") == target.email)):
                continue
            resolved_uid = resolved_uid or r.get("uid")
            resolved_email = resolved_email or r.get("email")
            user_entries.append(r)
            for j in (r.get("jobs_added") or []):
                url = j.get("job_url")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                jobs_added.append(j)

    jobs_added.sort(key=lambda j: j.get("grade") or 0, reverse=True)

    if not jobs_added:
        who = None
        reason = "no obvious reason from their stated preferences, worth a manual look"
        if resolved_uid:
            try:
                import send_jobbyo
                who = (send_jobbyo.get_user_profile(resolved_uid) or {}).get("displayName")
                automation = send_jobbyo.get_user_automation(resolved_uid) or {}
                prefs = (automation.get("settings") or {}).get("jobPreferences") or {}
                reason = _guess_zero_match_reason(prefs)
            except Exception:
                pass
        who = who or resolved_email or target.email or target.uid
        tags = " ".join(t for t in (SLACK_MENTION_GALIH, SLACK_MENTION_NADIA) if t)
        await asyncio.to_thread(
            _send_ops_only_slack_sync,
            f"The automated search came up completely empty for {who} (full pipeline "
            f"already ran, this isn't a first-pass miss). Likely reason: {reason}. "
            f"{tags} can you take a manual look and find them something?".strip(),
        )
        return SubscribedJobsResponse(
            accepted=True,
            message=f"Run finished for {target.uid or target.email}, but no jobs were found this run.",
            duration_seconds=duration_seconds,
            jobs_found=0,
            emailed=False,
        )

    # The signup run was a first-match dry run (see run_user_subscribed):
    # nothing is in their queue yet. Curate, then post only what survives.
    import send_jobbyo, approve_jobs, outreach, first_match

    profile = send_jobbyo.get_user_profile(resolved_uid) or {}
    recipient = TOP_JOBS_TEST_RECIPIENT or resolved_email or target.email
    who = profile.get("displayName") or recipient
    job_status = next((r.get("job_status") for r in user_entries if r.get("job_status")), "waiting_approval")
    report = None
    first_match.FIRST_MATCH_CLAUDE_MODEL = _load_search_settings()["first_match_model"]
    try:
        report = await asyncio.to_thread(first_match.curate, resolved_uid, user_entries, profile)
    except Exception as exc:
        print(f"[{label}] first-match curation failed, falling back to the plain first email: {exc}")

    if report is not None:
        kept = report["kept"]
        try:
            await asyncio.to_thread(first_match.post_kept, report, job_status)
        except Exception as exc:
            print(f"[{label}] posting curated jobs failed: {exc}")
            await asyncio.to_thread(
                _send_ops_only_slack_sync,
                f"Curated {len(kept)} first matches for {who} but couldn't save them to their queue ({exc}). "
                f"Nothing was emailed. Saved in run_logs/first_match_{resolved_uid}.json.",
            )
            kept = []
            emailed = False
        else:
            emailed = False
            if len(kept) >= first_match.MIN_JOBS_TO_EMAIL:
                try:
                    emailed = await asyncio.to_thread(first_match.send_first_match_email, report, recipient)
                except Exception as exc:
                    print(f"[{label}] first-match email failed: {exc}")
                _mark_first_match_emailed(resolved_uid, emailed)
                slack_text = (
                    f"First matches for {who}: kept {len(kept)} of {report['stats']['checked']} checked"
                    + (" and sent the welcome email." if emailed else ", but the email failed to send — worth a look.")
                )
            else:
                # Never send a thin first email: hold it for the team to top up.
                tags = " ".join(t for t in (SLACK_MENTION_GALIH, SLACK_MENTION_NADIA) if t)
                drops = Counter(d.get("drop_category") for d in report["dropped"]).most_common(3)
                slack_text = (
                    f"Only {len(kept)} strong first match(es) for {who} out of {report['stats']['checked']} checked "
                    f"(main drops: {', '.join(f'{c} {n}' for c, n in drops) or 'none'}). Saved to their queue; "
                    f"welcome email is held. {tags} can you find a few more by hand? Then send it with "
                    f"`python3 first_match.py --uid {resolved_uid} --send`.".strip()
                )
            if str(report.get("method", "")).startswith("previous version"):
                slack_text += f" — used the {report['method']}"
            await asyncio.to_thread(_send_ops_only_slack_sync, slack_text)

        response_jobs = [
            TopJob(title=j.get("title"), company=j.get("company"), url=j.get("job_url"),
                   location=j.get("location"), grade=j.get("grade"), reason=j.get("fit_line"))
            for j in kept
        ]
        return SubscribedJobsResponse(
            accepted=True,
            message=f"Kept {len(kept)} first match(es) for {target.uid or target.email}."
            + (" Emailed." if emailed else " Not emailed."),
            duration_seconds=duration_seconds,
            jobs_found=len(kept),
            jobs=response_jobs,
            emailed=emailed,
        )

    # Fallback (curation crashed): save what the pipeline found and send the
    # plain first email, so a new user is never left with nothing.
    try:
        jobs_added = jobs_added[:send_jobbyo.TARGET_JOBS_PER_USER]
        send_jobbyo.api_post_jobs(resolved_email, jobs_added, default_status=job_status, uid=resolved_uid)
    except Exception as exc:
        print(f"[{label}] fallback posting failed: {exc}")
    emailed = False
    try:
        # The welcome email itself IS the persona-coaching + strategy-PDF
        # note now (was a separate, later follow-up) -- build_context's own
        # sample_jobs caps at 3 for the follow-up-note use case, so override
        # it with the full, just-found jobs_added list here instead of
        # re-deriving a capped one from automation.selectedJobs.
        context = await asyncio.to_thread(outreach.build_context, resolved_uid)
        context["sample_jobs"] = jobs_added
        body_text = await asyncio.to_thread(outreach.generate_paid_welcome_message, context)
        try:
            pdf_bytes = await asyncio.to_thread(outreach.generate_strategy_pdf, context)
        except Exception as e:
            print(f"[{label}] welcome PDF generation failed (non-fatal, sends without it): {e}")
            pdf_bytes = None

        emailed = bool(
            await asyncio.to_thread(
                outreach.send_paid_welcome_email, context, body_text, pdf_bytes, recipient,
            )
        )
    except Exception as exc:
        print(f"[{label}] email send failed: {exc}")

    recipient_for_slack = TOP_JOBS_TEST_RECIPIENT or resolved_email or target.email
    who = profile.get("displayName") or recipient_for_slack
    if emailed:
        job_word = "job" if len(jobs_added) == 1 else "jobs"
        slack_text = f"Found {len(jobs_added)} {job_word} for {who} and sent them a personal note with their strategy PDF."
    else:
        slack_text = f"Found {len(jobs_added)} job(s) for {who}, but the email failed to send — worth a look."
    await asyncio.to_thread(_send_ops_only_slack_sync, slack_text)

    response_jobs = [
        TopJob(
            title=j.get("title"),
            company=j.get("company"),
            url=j.get("job_url"),
            location=j.get("location"),
            grade=j.get("grade"),
            reason=approve_jobs._job_reason(j),
        )
        for j in jobs_added
    ]
    return SubscribedJobsResponse(
        accepted=True,
        message=f"Found {len(jobs_added)} job(s) for {target.uid or target.email}."
        + (" Emailed." if emailed else " Email failed."),
        duration_seconds=duration_seconds,
        jobs_found=len(jobs_added),
        jobs=response_jobs,
        emailed=emailed,
    )


class SetupBriefRequest(BaseModel):
    uid: str
    prefs: Optional[dict] = None
    cv: Optional[dict] = None  # the resume the webapp is showing; wins over the saved one


@app.post("/setup/titles")
async def setup_titles(target: SetupBriefRequest):
    """Job title variations (12-20), seniority and location from the user's parsed resume, for the
    webapp's new-automation flow. See setup_brief.py. Cached per uid + CV for 24h."""
    if not target.uid:
        raise HTTPException(status_code=422, detail="Provide uid.")
    import setup_brief
    try:
        return await asyncio.to_thread(setup_brief.suggest_titles, target.uid, target.cv)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/setup/brief")
async def setup_brief_endpoint(req: SetupBriefRequest):
    """Strategy brief for the end of setup: coaching analysis, web-search-grounded market
    snapshot, and advice on the location/titles/salary they just chose. Takes up to a minute or
    two on a cold run; cached per uid + inputs for 24h. Never touches personas/ or
    search_contracts/."""
    import setup_brief
    try:
        return await asyncio.to_thread(setup_brief.build_brief, req.uid, req.prefs or {}, req.cv)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/setup/calibration")
async def setup_calibration(req: SetupBriefRequest):
    """Example roles (AI-written, not real postings) for the user to rate while their first search
    runs, each varying company size, industry, culture, workplace, seniority and pay. Cached per
    uid + inputs for 24h. See setup_brief.py."""
    import setup_brief
    try:
        return await asyncio.to_thread(setup_brief.calibration_jobs, req.uid, req.prefs or {}, req.cv)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class SetupRecountRequest(BaseModel):
    uid: str
    base_prefs: Optional[dict] = None  # the search the brief's estimate was made for
    prefs: Optional[dict] = None       # the search now, after "grow your pool" changes
    market: Optional[dict] = None      # the brief's market snapshot
    open_roles: int = 0                # the brief's estimate for base_prefs


@app.post("/setup/recount")
async def setup_recount(req: SetupRecountRequest):
    """Re-estimated open roles after the candidate toggles a pool change on the brief, anchored on
    the brief's own estimate so the number stays consistent. Quick (no web search). See
    setup_brief.py."""
    import setup_brief
    return await asyncio.to_thread(setup_brief.recount_open_roles, req.uid, req.base_prefs or {},
                                   req.prefs or {}, req.market or {}, req.open_roles)


class SetupProfileRequest(BaseModel):
    uid: str
    ratings: Optional[list] = None
    prefs: Optional[dict] = None
    cv: Optional[dict] = None


@app.post("/setup/calibration/profile")
async def setup_calibration_profile(req: SetupProfileRequest):
    """A short read on the candidate plus industries / company sizes / culture / levels, from their
    calibration ratings and resume. Cached per uid + inputs for 24h. See setup_brief.py."""
    import setup_brief
    try:
        return await asyncio.to_thread(setup_brief.calibration_profile, req.uid, req.ratings or [], req.prefs or {}, req.cv)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/run/user/subscribed", response_model=SubscribedJobsResponse)
async def run_user_subscribed(target: UserTarget):
    """Call this the moment a user's payment is confirmed (jobbyo-fastapi-
    server's Stripe successful-checkout webhook). Runs the full pipeline for
    that one user, blocking until done (can take a few minutes), then emails
    them their first batch with a reason per job. The email is only sent
    once at least MIN_JOBS_TO_NOTIFY_SUBSCRIBED (3) jobs were found; below
    the daily target it tells the candidate more are on the way instead of
    implying the search is finished, and above/at target it doesn't.

    The caller fires this as a detached background task with no read
    timeout of its own, so a slow run is never abandoned here either: if it
    outlives SUBSCRIBED_RUN_TIMEOUT_SECONDS, asyncio.shield keeps the subprocess
    running and this returns a "still processing" response instead of
    failing the call outright — the user still gets their jobs + email once
    it finishes, just without a synchronous response to report it."""
    if not target.uid and not target.email:
        raise HTTPException(status_code=422, detail="Provide uid or email.")

    key = (target.uid or target.email or "").strip().lower()
    last = _subscribed_last_run.get(key)
    now = datetime.now(timezone.utc)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < SUBSCRIBED_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=429,
                detail=f"Already ran for {key} recently. Try again in {int(SUBSCRIBED_COOLDOWN_SECONDS - elapsed)}s.",
            )
    _subscribed_last_run[key] = now

    if target.uid:
        args, label = ["--uid", target.uid, "--assume-paid"], f"run:subscribed:{target.uid}"
    else:
        args, label = ["--email", target.email, "--assume-paid"], f"run:subscribed:{target.email}"

    existing_logs = set(RUN_LOG_DIR.glob("job_run_*.json"))
    started_at = asyncio.get_event_loop().time()

    # First match: gather a wider pool and post nothing -- first_match.py
    # curates it in _finish_subscribed_run before anything reaches the user.
    first_match_env = {
        **_search_env(),
        "JOBBYO_DRY_RUN": "1",
        "JOBBYO_FIRST_MATCH": "1",
        "JOBBYO_TARGET_JOBS": os.getenv("JOBBYO_FIRST_MATCH_POOL", "25"),
        "JOBBYO_DRY_RUN_FILE": f"run_logs/dry_run_first_match_{re.sub(r'[^A-Za-z0-9]+', '_', key)}.json",
    }
    task = asyncio.ensure_future(_run_subprocess([PYTHON, "send_jobbyo.py", *args], label, env=first_match_env))
    _background_keepalive.add(task)
    task.add_done_callback(_background_keepalive.discard)

    try:
        code = await asyncio.wait_for(asyncio.shield(task), timeout=SUBSCRIBED_RUN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        async def _finish_in_background():
            try:
                bg_code = await task
                await _finish_subscribed_run(target, label, existing_logs, started_at, bg_code)
            except Exception as exc:
                print(f"[{label}] background completion failed: {exc}")

        bg_task = asyncio.ensure_future(_finish_in_background())
        _background_keepalive.add(bg_task)
        bg_task.add_done_callback(_background_keepalive.discard)

        return SubscribedJobsResponse(
            accepted=True,
            message=(
                f"Run for {target.uid or target.email} is still processing after "
                f"{SUBSCRIBED_RUN_TIMEOUT_SECONDS}s — it will keep running and email "
                "them once it finishes."
            ),
            duration_seconds=round(asyncio.get_event_loop().time() - started_at, 1),
            jobs_found=0,
            emailed=False,
        )

    try:
        return await _finish_subscribed_run(target, label, existing_logs, started_at, code)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# --- Hot leads ---------------------------------------------------------


class HotLeadCandidate(BaseModel):
    uid: str
    email: Optional[str] = None
    name: Optional[str] = None
    salary: Optional[str] = None
    salary_currency: Optional[str] = None
    locations: list[str] = []


class HotLeadResponse(BaseModel):
    accepted: bool
    qualified: bool
    already_recorded: bool = False
    message: str


@app.post("/leads/hot", response_model=HotLeadResponse)
async def add_hot_lead(candidate: HotLeadCandidate):
    """Call this right after an automation is created for someone who isn't
    already paid/trialing. Scores their salary/location (lead_scoring.py);
    if they clear the hot-lead bar, records them to ./hot_leads/{uid}.json
    and posts a Slack notification. Not yet wired to any outreach — a
    separate daily job (not built yet) will read these records and decide
    when to actually nudge someone, per the bounded-touches cadence design.
    """
    import lead_scoring

    if await asyncio.to_thread(lead_scoring.already_recorded, candidate.uid):
        return HotLeadResponse(
            accepted=True, qualified=True, already_recorded=True,
            message=f"{candidate.uid} was already recorded as a hot lead.",
        )

    lead = lead_scoring.qualifies(
        uid=candidate.uid,
        email=candidate.email,
        name=candidate.name,
        salary=candidate.salary,
        salary_currency=candidate.salary_currency,
        locations=candidate.locations,
    )
    if lead is None:
        return HotLeadResponse(
            accepted=True, qualified=False,
            message=f"{candidate.uid} did not clear the salary/region bar.",
        )

    # Recorded quietly: the per-lead Slack post to #marketing-team was removed.
    await asyncio.to_thread(lead_scoring.record_lead, lead)

    return HotLeadResponse(
        accepted=True, qualified=True,
        message=f"{candidate.uid} recorded as tier-{lead.tier} hot lead.",
    )


class NudgeResponse(BaseModel):
    accepted: bool
    sent: bool
    message: str


@app.post("/leads/hot/{uid}/nudge", response_model=NudgeResponse)
async def send_hot_lead_nudge(uid: str):
    """Send the prospect nudge email to a recorded hot lead. Called per lead
    by /leads/hot/nudge-due (the daily job); also usable manually. Does NOT
    check paid status itself -- nudge-due does that first. Once it sends:
    removes the lead from hot_leads/ (a lead only sits there while its first
    touch is still pending -- ongoing cadence tracking lives in
    outreach_log/, which this also writes to). No Slack post."""
    import lead_scoring, outreach_log, outreach

    lead = await asyncio.to_thread(lead_scoring.load_lead, uid)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No recorded hot lead for {uid}.")

    context = await asyncio.to_thread(outreach.build_prospect_context, uid)
    body_text = await asyncio.to_thread(outreach.generate_prospect_message, context)
    try:
        pdf_bytes = await asyncio.to_thread(outreach.generate_strategy_pdf, context)
    except Exception as e:
        print(f"[hot_lead_nudge] PDF generation failed (non-fatal, sends without it): {e}")
        pdf_bytes = None

    sent = await asyncio.to_thread(
        outreach.send_prospect_outreach_email, context, body_text, pdf_bytes,
    )

    if sent:
        await asyncio.to_thread(lead_scoring.remove_lead, uid)
        summary = (body_text.strip().split("\n\n")[0] if body_text else "")[:180]
        await asyncio.to_thread(
            outreach_log.record_touch, uid, "hot_lead_nudge", lead.get("tier"), summary,
        )

    return NudgeResponse(
        accepted=True, sent=sent,
        message=f"Nudge {'sent' if sent else 'failed'} for {uid}.",
    )


class NudgeDueResponse(BaseModel):
    dry_run: bool
    considered: int
    too_recent: int
    converted: int
    no_email: int
    already_nudged: int
    sent: int
    failed: int
    previews: list[dict] = []


NUDGE_MAX_PER_RUN = 25


@app.post("/leads/hot/nudge-due", response_model=NudgeDueResponse)
async def nudge_due_hot_leads(dry_run: bool = True, min_age_hours: float = 24.0):
    """The daily outreach job's body: first-touch nudge for every recorded
    hot lead older than min_age_hours who still hasn't started a trial.
    Defaults to dry_run so a stray call never emails anyone -- the systemd
    script passes dry_run=false explicitly. Anyone now in /users/paid
    (trialing counts) is treated as converted: dropped from the queue, not
    emailed. If the paid list can't be fetched this refuses to run rather
    than risk emailing someone who already signed up."""
    import lead_scoring, outreach_log, outreach, send_jobbyo

    paid_users = await asyncio.to_thread(send_jobbyo.get_paid_users)
    paid_uids = {u for u in (_uid_of(p) for p in paid_users) if u}
    if not paid_uids:
        raise HTTPException(status_code=503, detail="Could not load /users/paid; refusing to send nudges blind.")

    leads = await asyncio.to_thread(lead_scoring.list_leads)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    counts = dict(too_recent=0, converted=0, no_email=0, already_nudged=0, sent=0, failed=0)
    previews: list[dict] = []
    attempts = 0

    for lead in leads:
        uid = lead.get("uid")
        added_at = parse_utc_timestamp(lead.get("added_at"))
        if not uid or added_at is None or added_at > cutoff:
            counts["too_recent"] += 1
            continue
        if uid in paid_uids:
            counts["converted"] += 1
            if not dry_run:
                await asyncio.to_thread(lead_scoring.remove_lead, uid)
            continue
        if not lead.get("email"):
            counts["no_email"] += 1
            continue
        history = await asyncio.to_thread(outreach_log.get_touch_history, uid)
        if any(t.get("campaign") == "hot_lead_nudge" for t in history):
            counts["already_nudged"] += 1
            if not dry_run:
                await asyncio.to_thread(lead_scoring.remove_lead, uid)
            continue

        if dry_run:
            context = await asyncio.to_thread(outreach.build_prospect_context, uid)
            body_text = await asyncio.to_thread(outreach.generate_prospect_message, context)
            previews.append({
                "uid": uid, "email": lead.get("email"), "name": lead.get("name"),
                "tier": lead.get("tier"), "added_at": lead.get("added_at"),
                "message": body_text,
            })
            continue

        if attempts >= NUDGE_MAX_PER_RUN:
            break
        attempts += 1
        try:
            result = await send_hot_lead_nudge(uid)
            counts["sent" if result.sent else "failed"] += 1
        except Exception as e:
            print(f"[nudge_due] {uid} failed: {e}")
            counts["failed"] += 1

    return NudgeDueResponse(
        dry_run=dry_run, considered=len(leads), previews=previews, **counts,
    )


# --- Approve + email -------------------------------------------------------

@app.post("/email/all", response_model=RunResponse, status_code=202)
@app.post("/approve/all", response_model=RunResponse, status_code=202)
async def email_all(background_tasks: BackgroundTasks):
    """Promote pending_review jobs and send daily email to all users."""
    if _state.full_run_active:
        raise HTTPException(
            status_code=409,
            detail="A full run is in progress — wait for it to finish before sending emails.",
        )
    background_tasks.add_task(_full_run, "approve_jobs.py", [], "email:all")
    return RunResponse(accepted=True, message="Approval + email started for all users.")


@app.post("/email/user", response_model=RunResponse, status_code=202)
@app.post("/approve/user", response_model=RunResponse, status_code=202)
async def email_user(target: UserTarget, background_tasks: BackgroundTasks):
    """Promote pending_review jobs and send email to a single user."""
    if not target.email:
        raise HTTPException(status_code=422, detail="Provide email.")
    background_tasks.add_task(
        _single_run, "approve_jobs.py", ["--email", target.email], f"email:user:{target.email}"
    )
    return RunResponse(accepted=True, message=f"Approval + email started for {target.email}.")


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------

_TODAY_STATUSES = {"applied", "approved", "pending", "waiting_approval", "pending_review", "legacy"}
_APPLIED_STATUSES = {"applied", "approved"}

# "Covered" = at least 80% of the daily per-user job target (JOBBYO_TARGET_JOBS,
# default 10 in send_jobbyo.py). Goal: this fraction of users should be covered.
COVERAGE_TARGET_JOBS = int(os.getenv("JOBBYO_COVERAGE_TARGET_JOBS", "8"))
COVERAGE_GOAL_PCT = float(os.getenv("JOBBYO_COVERAGE_GOAL_PCT", "0.8"))

def _parse_job_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 or RFC 2822 date strings into UTC-aware datetime."""
    return parse_utc_timestamp(date_str)


def _fetch_paid_users_sync() -> list[dict]:
    resp = requests.get(f"{BACKEND_BASE}/users/paid", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("users", [])


def _fetch_user_automation_sync(uid: str) -> dict:
    resp = requests.get(f"{BACKEND_BASE}/automations/users/{uid}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def _send_slack_sync(text: str) -> bool:
    """Posted as "Laras" (our Ops & Reporting Analyst persona) -- same
    username/icon override every automated Slack post from this project
    uses, so it reads as one consistent reporting voice. Posts to both
    #operations and #job-library (SLACK_WEBHOOK_URLS), per request.
    """
    if not SLACK_WEBHOOK_URLS:
        print("[slack] No Slack webhooks configured — skipping notification.")
        return False
    ok = True
    for url in SLACK_WEBHOOK_URLS:
        resp = requests.post(
            url,
            json={"text": text, "username": "Laras", "icon_emoji": ":bar_chart:"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[slack] Webhook returned {resp.status_code}: {resp.text[:200]}")
            ok = False
    return ok


def _send_ops_only_slack_sync(text: str) -> bool:
    """Same Laras persona as _send_slack_sync, but #operations only -- for
    per-user run confirmations, which #job-library doesn't need to see."""
    if not SLACK_WEBHOOK_URL:
        print("[slack] No ops Slack webhook configured — skipping notification.")
        return False
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": text, "username": "Laras", "icon_emoji": ":bar_chart:"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[slack] Webhook returned {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def _uid_of(user: dict) -> Optional[str]:
    return user.get("uid") or user.get("id") or user.get("_id")


def _guess_zero_match_reason(prefs: dict) -> str:
    """Heuristic, not an LLM call -- this only feeds an internal Slack
    escalation for a human to look into, so a rough plain-language guess is
    enough; not worth a model call for every zero-match run."""
    prefs = prefs or {}
    reasons = []

    salary = prefs.get("minimumAcceptableSalary") or prefs.get("currentSalary")
    try:
        if salary is not None and int(str(salary).replace(",", "")) >= 250_000:
            reasons.append("salary floor looks very high for the target title")
    except (TypeError, ValueError):
        pass

    titles = prefs.get("jobTitles") or []
    if len(titles) == 1:
        reasons.append(f'only one job title ("{titles[0]}"), narrows the pool a lot')
    elif not titles:
        reasons.append("no job title set at all")

    location = prefs.get("location") or {}
    places = location.get("places") or []
    remote_ok = "remote" in (location.get("type") or [])
    if len(places) <= 1 and not remote_ok:
        reasons.append("single location with no remote option")

    return "; ".join(reasons) or "no obvious reason from their stated preferences, worth a manual look"


# ---------------------------------------------------------------------------
# Coverage endpoints
# ---------------------------------------------------------------------------


async def _compute_coverage_today() -> dict:
    """Shared by /coverage/today, /coverage/missing and /email/missing so
    they all agree on the same per-user job counts for today.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    users = await asyncio.to_thread(_fetch_paid_users_sync)

    result_users: list[dict] = []
    covered = partial = missing = 0

    for user in users:
        uid = _uid_of(user)
        if not uid:
            continue

        try:
            automation = await asyncio.to_thread(_fetch_user_automation_sync, uid)
        except Exception as exc:
            print(f"[coverage] Could not fetch automation for {uid}: {exc}")
            continue

        jobs = automation.get("selectedJobs") or []
        total = applied_c = pending_c = waiting_c = 0

        for job in jobs:
            status = job.get("status", "")
            if status not in _TODAY_STATUSES:
                continue
            added = _parse_job_date(job.get("addedAt", ""))
            if added is None or now_utc - added >= timedelta(hours=24):
                continue
            total += 1
            if status == "applied":
                applied_c += 1
            elif status == "pending":
                pending_c += 1
            elif status == "waiting_approval":
                waiting_c += 1

        if total >= COVERAGE_TARGET_JOBS:
            covered += 1
        elif total >= 1:
            partial += 1
        else:
            missing += 1

        result_users.append({
            "uid": uid,
            "name": user.get("displayName") or user.get("name") or "",
            "email": user.get("email", ""),
            "total": total,
            "applied": applied_c,
            "pending": pending_c,
            "waiting_approval": waiting_c,
        })

    total_users = len(result_users)
    coverage_pct = round(covered / total_users, 4) if total_users else 0.0
    meets_goal = coverage_pct >= COVERAGE_GOAL_PCT

    return {
        "date": today_utc.isoformat(),
        "total_users": total_users,
        "coverage_target_jobs": COVERAGE_TARGET_JOBS,
        "coverage_pct": coverage_pct,
        "coverage_goal_pct": COVERAGE_GOAL_PCT,
        "meets_goal": meets_goal,
        "covered": covered,
        "partial": partial,
        "missing": missing,
        "users": result_users,
    }


@app.get("/coverage/today")
async def coverage_today(send_slack: bool = False):
    """Count jobs in active statuses added TODAY per paid user.

    Statuses counted: applied, approved, pending, waiting_approval, pending_review, legacy.
    A user is "covered" once they hit COVERAGE_TARGET_JOBS (default 8, i.e. 80%
    of the 10-job daily target). Goal: COVERAGE_GOAL_PCT (default 80%) of users
    covered.
    Query ?send_slack=true posts a summary to Slack.
    """
    report = await _compute_coverage_today()

    if send_slack:
        warn_lines = [
            f"  • {u['name']} ({u['email']}): {u['total']} jobs"
            for u in report["users"] if u["total"] < COVERAGE_TARGET_JOBS
        ]
        status_icon = "✅" if report["meets_goal"] else "⚠️"
        slack_text = (
            f"Laras here with today's coverage report ({report['date']}):\n"
            f"{status_icon} {report['coverage_pct']:.0%} of users at {COVERAGE_TARGET_JOBS}+ jobs "
            f"(goal: {COVERAGE_GOAL_PCT:.0%})\n"
            f"Total: {report['total_users']} | ✅ Covered (≥{COVERAGE_TARGET_JOBS}): {report['covered']} | "
            f"⚠️ Partial (1-{COVERAGE_TARGET_JOBS - 1}): {report['partial']} | ❌ Missing (0): {report['missing']}\n"
        )
        if warn_lines:
            slack_text += f"\n⚠️ *Below {COVERAGE_TARGET_JOBS} jobs:*\n" + "\n".join(warn_lines)
        await asyncio.to_thread(_send_slack_sync, slack_text)

    return report


def _emailed_today_uids_sync() -> set:
    """Reads approve_jobs.py's own dedup log (run_logs/emailed_<date>.json,
    written by save_emailed_today()) — the ground truth of who actually got
    an email today, as opposed to the job-count proxy in _compute_coverage_today.
    """
    path = Path("run_logs") / f"emailed_{datetime.now(timezone.utc).date().isoformat()}.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()).get("uids", []))
    except Exception as exc:
        print(f"[coverage/missing] Could not read {path}: {exc}")
        return set()


@app.get("/coverage/missing")
async def coverage_missing():
    """Paid users who have NOT been emailed today (per approve_jobs.py's own
    send log) — the list the admin platform's "not covered" table and
    bulk-send button read from.
    """
    report = await _compute_coverage_today()
    emailed_uids = await asyncio.to_thread(_emailed_today_uids_sync)
    missing_users = [u for u in report["users"] if u.get("uid") not in emailed_uids]
    return {
        "date": report["date"],
        "coverage_target_jobs": COVERAGE_TARGET_JOBS,
        "count": len(missing_users),
        "users": missing_users,
    }


@app.post("/email/missing", response_model=RunResponse, status_code=202)
async def email_missing(background_tasks: BackgroundTasks):
    """Force-send the daily report to every paid user not yet emailed today,
    bypassing MIN_JOBS_TO_EMAIL — the "send all of them" admin-platform
    button. approve_jobs.py itself still skips anyone with zero emailable
    jobs (nothing to send).
    """
    if _state.full_run_active:
        raise HTTPException(
            status_code=409,
            detail="A full run is in progress — wait for it to finish before sending emails.",
        )
    report = await _compute_coverage_today()
    emailed_uids = await asyncio.to_thread(_emailed_today_uids_sync)
    missing_emails = [u["email"] for u in report["users"] if u.get("uid") not in emailed_uids and u["email"]]
    for email in missing_emails:
        background_tasks.add_task(
            _single_run, "approve_jobs.py", ["--email", email, "--force"], f"email:missing:{email}"
        )
    return RunResponse(
        accepted=True,
        message=f"Force-send started for {len(missing_emails)} user(s) not yet emailed today.",
    )


@app.get("/coverage/applied")
async def coverage_applied(send_slack: bool = False):
    """Count jobs with status applied or approved added TODAY per paid user.

    Query ?send_slack=true posts counts to Slack.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    users = await asyncio.to_thread(_fetch_paid_users_sync)

    result_users: list[dict] = []

    for user in users:
        uid = _uid_of(user)
        if not uid:
            continue

        try:
            automation = await asyncio.to_thread(_fetch_user_automation_sync, uid)
        except Exception as exc:
            print(f"[coverage/applied] Could not fetch automation for {uid}: {exc}")
            continue

        jobs = automation.get("selectedJobs") or []
        applied_today = 0
        for job in jobs:
            if job.get("status") not in _APPLIED_STATUSES:
                continue
            added = _parse_job_date(job.get("addedAt", ""))
            if added is not None and now_utc - added < timedelta(hours=24):
                applied_today += 1

        result_users.append({
            "name": user.get("displayName") or user.get("name") or "",
            "email": user.get("email", ""),
            "applied_today": applied_today,
        })

    if send_slack:
        has_applied = [u for u in result_users if u["applied_today"] > 0]
        no_applied = [u for u in result_users if u["applied_today"] == 0]
        slack_text = f"Laras here with today's applied-jobs report ({today_utc.isoformat()}):\n"
        if has_applied:
            lines = [f"  • {u['name']} ({u['email']}): {u['applied_today']}" for u in has_applied]
            slack_text += "\n✅ *Users with applied jobs today:*\n" + "\n".join(lines)
        if no_applied:
            lines = [f"  • {u['name']} ({u['email']})" for u in no_applied]
            slack_text += "\n\n⚠️ *No applied jobs yet:*\n" + "\n".join(lines)
        await asyncio.to_thread(_send_slack_sync, slack_text)

    return {
        "date": today_utc.isoformat(),
        "users": result_users,
    }


@app.get("/coverage/complete")
async def coverage_complete(send_email: bool = False, warn_stale: bool = False):
    """Check if every actionable user (has job titles) has ≥10 jobs today.

    Also surfaces users with waiting_approval jobs from YESTERDAY (stale approvals).

    Query params:
      ?send_email=true  — if all covered, POST /api/reports/daily for each user.
      ?warn_stale=true  — POST /api/notifications/incomplete-profile for users
                          with stale waiting_approval jobs from yesterday.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    yesterday_utc = today_utc - timedelta(days=1)

    users = await asyncio.to_thread(_fetch_paid_users_sync)

    covered_count = missing_count = actionable_count = 0
    stale_approvals: list[dict] = []

    for user in users:
        uid = _uid_of(user)
        if not uid:
            continue

        try:
            automation = await asyncio.to_thread(_fetch_user_automation_sync, uid)
        except Exception as exc:
            print(f"[coverage/complete] Could not fetch automation for {uid}: {exc}")
            continue

        # Only count users that have job titles configured (actionable)
        job_titles = automation.get("jobTitles") or automation.get("job_titles") or []
        if not job_titles:
            continue

        actionable_count += 1
        jobs = automation.get("selectedJobs") or []

        today_total = 0
        stale_count = 0
        for job in jobs:
            status = job.get("status", "")
            added = _parse_job_date(job.get("addedAt", ""))
            if added is None:
                continue
            job_date = added.date()
            if now_utc - added < timedelta(hours=24) and status in _TODAY_STATUSES:
                today_total += 1
            if job_date == yesterday_utc and status == "waiting_approval":
                stale_count += 1

        if today_total >= 10:
            covered_count += 1
        else:
            missing_count += 1

        if stale_count > 0:
            stale_approvals.append({
                "name": user.get("displayName") or user.get("name") or "",
                "email": user.get("email", ""),
                "count": stale_count,
            })

    all_covered = actionable_count > 0 and missing_count == 0

    if send_email and all_covered:
        for user in users:
            email = user.get("email", "")
            name = user.get("name", "")
            if not email:
                continue
            try:
                payload = {"email": email, "name": name}
                await asyncio.to_thread(
                    lambda p=payload: requests.post(
                        f"{BACKEND_BASE}/api/reports/daily", json=p, timeout=15
                    )
                )
            except Exception as exc:
                print(f"[coverage/complete] Failed to send daily report for {email}: {exc}")

    if warn_stale and stale_approvals:
        for entry in stale_approvals:
            try:
                payload = {
                    "email": entry["email"],
                    "name": entry["name"],
                    "reason": "stale_approvals",
                    "stale_count": entry["count"],
                }
                await asyncio.to_thread(
                    lambda p=payload: requests.post(
                        f"{BACKEND_BASE}/api/notifications/incomplete-profile",
                        json=p,
                        timeout=15,
                    )
                )
            except Exception as exc:
                print(f"[coverage/complete] Failed to warn stale for {entry['email']}: {exc}")

    return {
        "all_covered": all_covered,
        "covered_count": covered_count,
        "missing_count": missing_count,
        "stale_approvals": stale_approvals,
    }


# ---------------------------------------------------------------------------
# Admin: Job Search page (jobbyo-internal-admin-system /job-search)
# Read-only views of what the search is doing, plus sending a held first email.
# ---------------------------------------------------------------------------

def _first_match_path(uid: str) -> Path:
    return RUN_LOG_DIR / f"first_match_{re.sub(r'[^A-Za-z0-9_-]', '', uid)}.json"


def _mark_first_match_emailed(uid: str, emailed: bool) -> None:
    path = _first_match_path(uid)
    try:
        report = json.loads(path.read_text())
        report["emailed"] = bool(emailed)
        report["emailed_at"] = datetime.now(timezone.utc).isoformat() if emailed else None
        path.write_text(json.dumps(report, indent=2, default=str))
    except (OSError, ValueError) as exc:
        print(f"[first-match] couldn't mark {uid} emailed: {exc}")


def _live_summary(lines: list[str]) -> dict:
    """What a run is on right now, from its latest log lines."""
    cur = {"user": None, "round": None, "batch": None, "funnel": None}
    for text in lines:
        if text.startswith("USER: "):
            cur["user"] = text[6:].strip()
        elif "STARTING ROUND" in text or ("ROUND " in text and "DONE" in text):
            cur["round"] = text.strip(" =")
        elif text.startswith("BATCH ") and "·" in text:
            cur["batch"] = text.strip()
        elif "FUNNEL:" in text or text.startswith("Funnel:"):
            cur["funnel"] = text.strip()
    return cur


@app.get("/admin/search/live")
async def admin_search_live(lines: int = 300):
    """Runs this API process started (active first), each with its last lines."""
    runs = []
    for run in _LIVE_RUNS.values():
        tail = list(run["lines"])[-max(1, min(lines, _LIVE_MAX_LINES)):]
        runs.append({
            "label": run["label"], "cmd": run["cmd"], "started_at": run["started_at"],
            "finished_at": run["finished_at"], "exit_code": run["exit_code"],
            "active": run["finished_at"] is None, "line_count": run["line_count"],
            "current": _live_summary(list(run["lines"])), "lines": tail,
        })
    runs.sort(key=lambda r: (not r["active"], r["started_at"]), reverse=False)
    runs = [r for r in runs if r["active"]] + sorted(
        (r for r in runs if not r["active"]), key=lambda r: r["finished_at"], reverse=True)
    return {"full_run_active": _state.full_run_active, "runs": runs}


def _today_search_summary(day: str) -> list[dict]:
    files = sorted(RUN_LOG_DIR.glob(f"job_run_{day.replace('-', '')}_*.json"))
    latest: dict = {}
    # Across all of the day's runs: the most jobs they reached and every job
    # added. (The newest file alone is the run in progress -- it says 0/10
    # for users it hasn't reached yet.)
    best_pending: dict = {}
    day_added: dict = {}
    # A sign-up's first-match search only gathers a pool (nothing is posted
    # until first_match.py curates it): skip it, its jobs never reach them.
    first_match_at = {p.stem[len("first_match_"):]: p.stat().st_mtime for p in RUN_LOG_DIR.glob("first_match_*.json")}

    def is_signup_pool(uid, fname):
        try:
            t = datetime.strptime(fname[len("job_run_"):-len(".json")], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return False
        fm = first_match_at.get(uid)
        return fm is not None and fm - 3 * 3600 <= t <= fm + 60

    for f in files:  # later files win for funnel/sources: each is cumulative for its own run
        try:
            results = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        per_file: dict = {}
        for r in results if isinstance(results, list) else []:
            if r.get("uid") and not is_signup_pool(r["uid"], f.name):
                per_file.setdefault(r["uid"], []).append(r)
                best_pending[r["uid"]] = max(best_pending.get(r["uid"], 0), r.get("pending_today_after_estimate") or 0)
                for j in r.get("jobs_added") or []:
                    day_added.setdefault(r["uid"], {})[j.get("job_url")] = j
        for uid, entries in per_file.items():
            latest[uid] = (f.name, entries)

    users = []
    for uid, (fname, entries) in latest.items():
        m = Counter()
        reasons = Counter()
        added, rejected = [], []
        for r in entries:
            for k, v in (r.get("round_metrics") or {}).items():
                if isinstance(v, (int, float)):
                    m[k] += v

            for j in (r.get("jobs_rejected_by_review") or []) + (r.get("all_rejected_jobs") or []):
                reason = str(j.get("review_reason") or "").strip()
                if reason:
                    reasons[reason[:90]] += 1
                    if len(rejected) < 40:
                        rejected.append({"title": j.get("title"), "company": j.get("company"),
                                         "location": j.get("location"), "job_url": j.get("job_url") or j.get("url"),
                                         "reason": reason[:240]})
        added = [{k: j.get(k) for k in ("title", "company", "location", "job_url", "grade", "match_tier", "source", "review_reason")}
                 for j in (day_added.get(uid) or {}).values()]
        first = entries[0]
        users.append({
            "uid": uid, "email": first.get("email"), "name": first.get("displayName"),
            "plan": first.get("user_plan"), "run_log": fname,
            "rounds": [r.get("round_mode") for r in entries],
            "pending_before": first.get("pending_today_before"),
            "pending_after": best_pending.get(uid, 0),
            "sources": {"jfe": int(m.get("jfe_raw_results", 0)), "hiring_cafe": int(m.get("hc_raw_results", 0)),
                        "jobo": int(m.get("jobo_raw_results", 0)), "openai": int(m.get("openai_jobs_returned", 0))},
            "funnel": {"static_pass": int(m.get("static_pass_count", 0)), "ai_reviewed": int(m.get("ai_reviewed_count", 0)),
                       "ai_rejected": int(m.get("review_rejected_count", 0)), "approved": int(m.get("approved_count", 0))},
            "top_reject_reasons": reasons.most_common(6),
            "jobs_added": added, "rejected_sample": rejected,
            "first_match": _first_match_path(uid).exists(),
        })
    users.sort(key=lambda u: (u["pending_after"] or 0, (u["name"] or u["email"] or "").lower()))
    return users


@app.get("/admin/search/today")
async def admin_search_today(day: Optional[str] = None):
    """Per user, from the latest run log of the day: sources, funnel, why jobs
    were rejected, and the jobs added."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    users = await asyncio.to_thread(_today_search_summary, day)
    return {"day": day, "users": users}


@app.get("/admin/search/first-matches")
async def admin_first_matches(limit: int = 60):
    """New users' curated first batches, newest first."""
    out = []
    for path in sorted(RUN_LOG_DIR.glob("first_match_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            r = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out.append({
            "uid": r.get("uid"), "email": r.get("email"), "name": (r.get("facts") or {}).get("name"),
            "headline": (r.get("facts") or {}).get("headline"),
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "method": r.get("method") or "first version",
            "checked": (r.get("stats") or {}).get("checked"), "candidates": (r.get("stats") or {}).get("candidates"),
            "kept": len(r.get("kept") or []), "dropped": len(r.get("dropped") or []),
            "held": len(r.get("kept") or []) < 3, "emailed": r.get("emailed"),
        })
    return {"first_matches": out}


@app.get("/admin/search/first-matches/{uid}")
async def admin_first_match(uid: str):
    path = _first_match_path(uid)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No first-match report for this user.")
    report = json.loads(path.read_text())
    import first_match
    first = ((report.get("facts") or {}).get("name") or "there").split()[0]
    try:
        report["email_html"] = first_match.render_body_html(report, first)
    except Exception as exc:
        report["email_html"] = f"<p>Couldn't render the email preview: {exc}</p>"
    return report


@app.post("/admin/search/first-matches/{uid}/send")
async def admin_first_match_send(uid: str):
    """Send a held first email (fewer than 3 kept at signup) from the saved report."""
    path = _first_match_path(uid)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No first-match report for this user.")
    report = json.loads(path.read_text())
    if not report.get("kept"):
        raise HTTPException(status_code=409, detail="This first batch has no jobs to send.")
    import first_match
    emailed = await asyncio.to_thread(first_match.send_first_match_email, report, TOP_JOBS_TEST_RECIPIENT or None)
    _mark_first_match_emailed(uid, emailed)
    if not emailed:
        raise HTTPException(status_code=502, detail="The email failed to send.")
    return {"emailed": True}


class SearchSettingsUpdate(BaseModel):
    model: Optional[str] = None
    profile: Optional[str] = None
    first_match_model: Optional[str] = None


class SearchRunRequest(BaseModel):
    scope: str = "user"            # "all" | "user"
    uid: Optional[str] = None
    email: Optional[str] = None
    model: Optional[str] = None    # one of SEARCH_MODELS; default = saved setting
    profile: Optional[str] = None  # quality | balanced | volume; default = saved setting
    count: Optional[int] = None    # one user: find this many more jobs today (default: up to the daily target)


@app.get("/admin/search/settings")
async def admin_search_settings():
    return {"settings": _load_search_settings(), "models": SEARCH_MODELS,
            "first_match_models": FIRST_MATCH_MODELS, "profiles": SEARCH_PROFILES}


@app.put("/admin/search/settings")
async def admin_search_settings_update(update: SearchSettingsUpdate):
    settings = _load_search_settings()
    if update.model is not None:
        if update.model not in {m["id"] for m in SEARCH_MODELS}:
            raise HTTPException(status_code=422, detail="Unknown model.")
        settings["model"] = update.model
    if update.profile is not None:
        if update.profile not in _PROFILE_ENV:
            raise HTTPException(status_code=422, detail="Unknown profile.")
        settings["profile"] = update.profile
    if update.first_match_model is not None:
        if update.first_match_model not in {m["id"] for m in FIRST_MATCH_MODELS}:
            raise HTTPException(status_code=422, detail="Unknown first-batch model.")
        settings["first_match_model"] = update.first_match_model
    settings["updated_at"] = datetime.now(timezone.utc).isoformat()
    SEARCH_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    return {"settings": settings}


@app.post("/admin/search/run", status_code=202)
async def admin_search_run(req: SearchRunRequest, background_tasks: BackgroundTasks):
    """Start a search for everyone or one user, with an optional model/profile
    for this run only (the saved settings stay as they are)."""
    if req.model and req.model not in {m["id"] for m in SEARCH_MODELS}:
        raise HTTPException(status_code=422, detail="Unknown model.")
    if req.profile and req.profile not in _PROFILE_ENV:
        raise HTTPException(status_code=422, detail="Unknown profile.")
    env = _search_env(req.model, req.profile)
    used = f"{req.model or _load_search_settings()['model']} · {req.profile or _load_search_settings()['profile']}"
    if req.count and req.scope != "all":
        env["JOBBYO_FIND_JOBS"] = str(max(1, min(int(req.count), 20)))
        used += f" · {env['JOBBYO_FIND_JOBS']} more job(s)"
    if req.scope == "all":
        if _state.full_run_active:
            raise HTTPException(status_code=409, detail="A full run is already active.")
        background_tasks.add_task(_full_run, "send_jobbyo.py", [], "run:all", env)
        return {"accepted": True, "message": f"Search started for all users ({used})."}
    if not req.uid and not req.email:
        raise HTTPException(status_code=422, detail="Provide uid or email.")
    args, label = (["--uid", req.uid], f"run:user:{req.uid}") if req.uid else (["--email", req.email], f"run:user:{req.email}")
    background_tasks.add_task(_single_run, "send_jobbyo.py", args, label, env)
    return {"accepted": True, "message": f"Search started for {req.uid or req.email} ({used})."}


def _costs_summary(days: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    by_day: dict = {}
    by_user: dict = {}
    by_model: dict = {}
    for path in sorted(RUN_LOG_DIR.glob("run_costs_*.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        day = report.get("run_date") or ""
        if day < since:
            continue
        model = (report.get("models") or {}).get("review") or "gpt-4.1-mini (assumed)"
        d = by_day.setdefault(day, {"day": day, "cost": 0.0, "jobs": 0, "runs": 0, "first_match_cost": 0.0, "first_matches": 0})
        d["runs"] += 1
        for u in report.get("users") or []:
            cost, jobs = float(u.get("estimated_cost_usd") or 0), int(u.get("jobs_added") or 0)
            d["cost"] += cost
            d["jobs"] += jobs
            m = by_model.setdefault(model, {"model": model, "cost": 0.0, "jobs": 0})
            m["cost"] += cost
            m["jobs"] += jobs
            uu = by_user.setdefault(u.get("uid"), {"uid": u.get("uid"), "name": u.get("name"), "email": u.get("email"), "cost": 0.0, "jobs": 0})
            uu["cost"] += cost
            uu["jobs"] += jobs
    for path in RUN_LOG_DIR.glob("first_match_*.json"):
        day = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        if day < since:
            continue
        try:
            cost = float(json.loads(path.read_text()).get("estimated_cost_usd") or 0)
        except (OSError, ValueError):
            continue
        d = by_day.setdefault(day, {"day": day, "cost": 0.0, "jobs": 0, "runs": 0, "first_match_cost": 0.0, "first_matches": 0})
        d["first_match_cost"] += cost
        d["first_matches"] += 1

    per_job = lambda c, j: round(c / j, 4) if j else None
    days_out = []
    for d in sorted(by_day.values(), key=lambda x: x["day"], reverse=True):
        total = d["cost"] + d["first_match_cost"]
        days_out.append({**d, "cost": round(d["cost"], 4), "first_match_cost": round(d["first_match_cost"], 4),
                         "total": round(total, 4), "cost_per_job": per_job(total, d["jobs"])})
    total_cost = sum(d["total"] for d in days_out)
    total_jobs = sum(d["jobs"] for d in days_out)
    return {
        "days": days_out,
        "avg_cost_per_day": round(total_cost / len(days_out), 4) if days_out else 0,
        "avg_cost_per_job": per_job(total_cost, total_jobs),
        "total_cost": round(total_cost, 4), "total_jobs": total_jobs,
        "by_model": [{**m, "cost": round(m["cost"], 4), "cost_per_job": per_job(m["cost"], m["jobs"])} for m in by_model.values()],
        "by_user": sorted(({**u, "cost": round(u["cost"], 4), "cost_per_job": per_job(u["cost"], u["jobs"])} for u in by_user.values()),
                          key=lambda u: u["cost"], reverse=True),
        "note": "Estimates: per-call averages scaled by model price, plus Hiring Cafe/Jobo per-result rates. "
                "Calls retried on the fallback provider aren't counted.",
    }


@app.get("/admin/search/costs")
async def admin_search_costs(days: int = 14):
    return await asyncio.to_thread(_costs_summary, max(1, min(days, 90)))


def _safe_uid(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", uid or "")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@app.get("/admin/search/users")
async def admin_search_users():
    """Everyone the search has documents for, with what exists for each."""
    def build():
        personas = Path(SCRIPT_DIR / "personas")
        contracts = Path(SCRIPT_DIR / "search_contracts")
        reports = Path(SCRIPT_DIR / "strategy_reports")
        uids = {p.name.split(".")[0] for p in personas.glob("*.json")} | {p.stem for p in contracts.glob("*.json")}
        names = {}
        try:
            for u in requests.get(f"{BACKEND_BASE}/users/paid", timeout=30).json() or []:
                names[u.get("uid") or u.get("id")] = (u.get("displayName"), u.get("email"))
        except Exception:
            pass
        out = []
        for uid in uids:
            feedback = _read_json(personas / f"{uid}.feedback.json") or {}
            out.append({
                "uid": uid, "name": (names.get(uid) or (None, None))[0], "email": (names.get(uid) or (None, None))[1],
                "paid": uid in names,
                "persona": (personas / f"{uid}.json").exists(), "contract": (contracts / f"{uid}.json").exists(),
                "feedback_rules": len((feedback.get("notes") or {}).get("rules") or []),
                "feedback_updated": feedback.get("built_at"),
                "adaptations": len(list(reports.glob(f"adaptation_{uid}_*.json"))),
                "first_match": _first_match_path(uid).exists(),
            })
        out.sort(key=lambda u: (not u["paid"], (u["name"] or u["email"] or u["uid"]).lower()))
        return out
    return {"users": await asyncio.to_thread(build)}


@app.get("/admin/search/users/{uid}/docs")
async def admin_search_user_docs(uid: str):
    """Every document the search keeps for one user, plus their notes history."""
    uid = _safe_uid(uid)
    if not uid:
        raise HTTPException(status_code=422, detail="Bad uid.")
    personas = SCRIPT_DIR / "personas"
    reports = SCRIPT_DIR / "strategy_reports"
    adaptations = []
    for path in sorted(reports.glob(f"adaptation_{uid}_*.json"), reverse=True)[:15]:
        adaptations.append({"file": path.name, "content": _read_json(path)})
    fm = _read_json(_first_match_path(uid))
    return {
        "uid": uid,
        "persona": _read_json(personas / f"{uid}.json"),
        "search_contract": _read_json(SCRIPT_DIR / "search_contracts" / f"{uid}.json"),
        "feedback": _read_json(personas / f"{uid}.feedback.json"),
        "adaptations": adaptations,
        "first_match": {k: fm.get(k) for k in ("method", "stats", "copy", "emailed", "estimated_cost_usd")} if fm else None,
    }


# ---------------------------------------------------------------------------
# Admin: the day per user - how many jobs each has (by the automation / added
# by hand), their feedback, what the filter turned down, and the filter itself.
# ---------------------------------------------------------------------------

DAILY_TARGET = int(os.getenv("JOBBYO_TARGET_JOBS", "10"))
# the search plan fields a person may edit from the admin
EDITABLE_FILTER_FIELDS = ("allowed_titles", "forbidden_titles", "location_hard_rule", "salary_hard_rule",
                          "seniority_rules", "hard_reject_rules")


def _url_key(url) -> str:
    return re.sub(r"[?#].*$", "", str(url or "")).rstrip("/").lower()


def _selected_jobs(automation: dict) -> list[dict]:
    return automation.get("selectedJobs") or (automation.get("automation_data") or {}).get("selectedJobs") or []


def _automation_urls(day: str) -> set:
    """Every job the search posted in the run logs around this day (scheduled
    evening runs post tomorrow's jobs, so the day before counts too)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    urls = set()
    for offset in (-1, 0):
        stamp = (d + timedelta(days=offset)).strftime("%Y%m%d")
        for f in RUN_LOG_DIR.glob(f"job_run_{stamp}_*.json"):
            results = _read_json(f)
            for r in results if isinstance(results, list) else []:
                for j in r.get("jobs_added") or []:
                    urls.add(_url_key(j.get("job_url")))
    # a new user's first batch is posted by the search too
    for f in RUN_LOG_DIR.glob("first_match_*.json"):
        for j in (_read_json(f) or {}).get("kept") or []:
            urls.add(_url_key(j.get("job_url")))
    return urls


# everything delivered for the day except what the user turned down - approved
# ones ("needs_review": waiting to be applied) count too
_DAY_STATUSES = _TODAY_STATUSES | {"needs_review", "review", "in_review"}


def _day_jobs(jobs: list[dict], day: str, automated_urls: set) -> list[dict]:
    """The user's jobs for this day (the day they're dated for), each marked
    as found by the search or added by hand."""
    out = []
    for j in jobs:
        added = _parse_job_date(j.get("addedAt") or j.get("createdAt") or "")
        if not added or added.strftime("%Y-%m-%d") != day or str(j.get("status", "")).lower() not in _DAY_STATUSES:
            continue
        out.append({"title": j.get("title"), "company": j.get("company"), "job_url": j.get("job_url"),
                    "status": j.get("status"), "by_search": _url_key(j.get("job_url")) in automated_urls})
    return out


def _day_counts(jobs: list[dict], day: str, automated_urls: set) -> dict:
    day_jobs = _day_jobs(jobs, day, automated_urls)
    total, automated = len(day_jobs), sum(j["by_search"] for j in day_jobs)
    return {"total": total, "automated": automated, "manual": total - automated,
            "target": DAILY_TARGET, "missing": max(0, DAILY_TARGET - total)}


def _feedback(jobs: list[dict], days: int = 14) -> list[dict]:
    """Jobs the user turned down (newest first), with what they said."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for j in jobs:
        if str(j.get("status", "")).lower() != "rejected":
            continue
        at = _parse_job_date(j.get("rejectedAt") or j.get("addedAt") or "")
        if at and at >= cutoff:
            out.append({"title": j.get("title"), "company": j.get("company"), "job_url": j.get("job_url"),
                        "feedback": (j.get("feedback") or "").strip(), "at": at.isoformat()})
    return sorted(out, key=lambda x: x["at"], reverse=True)


@app.get("/admin/search/daily")
async def admin_search_daily(day: Optional[str] = None):
    """Every paying user's jobs for the day: total, by the automation, added by
    hand, how many short of the target, and how much feedback they left."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def build():
        automated_urls = _automation_urls(day)
        out = []
        for user in _fetch_paid_users_sync():
            uid = _uid_of(user)
            if not uid:
                continue
            try:
                jobs = _selected_jobs(_fetch_user_automation_sync(uid))
            except Exception as exc:
                print(f"[admin/daily] automation for {uid}: {exc}")
                continue
            feedback = _feedback(jobs)
            out.append({
                "uid": uid, "name": user.get("displayName") or user.get("name") or "", "email": user.get("email", ""),
                **_day_counts(jobs, day, automated_urls),
                "feedback_count": len(feedback), "feedback_with_text": sum(1 for f in feedback if f["feedback"]),
            })
        out.sort(key=lambda u: (-u["missing"], (u["name"] or u["email"]).lower()))
        return out

    users = await asyncio.to_thread(build)
    return {"day": day, "target": DAILY_TARGET, "users": users}


@app.get("/admin/search/users/{uid}/day")
async def admin_search_user_day(uid: str, day: Optional[str] = None):
    """One user's day in one place: counts, their feedback, the jobs the
    filter turned down (with links), what it learned and the editable filter."""
    uid = _safe_uid(uid)
    if not uid:
        raise HTTPException(status_code=422, detail="Bad uid.")
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def build():
        jobs = _selected_jobs(_fetch_user_automation_sync(uid))
        summary = next((u for u in _today_search_summary(day) if u["uid"] == uid), None) or {}
        contract = _read_json(SCRIPT_DIR / "search_contracts" / f"{uid}.json") or {}
        notes = (_read_json(SCRIPT_DIR / "personas" / f"{uid}.feedback.json") or {}).get("notes") or {}
        automated_urls = _automation_urls(day)
        return {
            "uid": uid, "day": day, **_day_counts(jobs, day, automated_urls),
            "jobs": _day_jobs(jobs, day, automated_urls),
            "feedback": _feedback(jobs),
            "not_passed": summary.get("rejected_sample") or [],
            "added_by_search": summary.get("jobs_added") or [],
            "search": {k: summary.get(k) for k in ("sources", "funnel", "top_reject_reasons")} if summary else None,
            "corrections": list(reversed(team_corrections.load(uid))),
            "learned": {"rules": notes.get("rules") or [], "blocked_companies": notes.get("blocked_companies") or [],
                        "min_salary_usd": notes.get("min_salary_usd")},
            "filter": {k: contract.get(k) for k in EDITABLE_FILTER_FIELDS} if contract else None,
        }

    return await asyncio.to_thread(build)


class FilterUpdate(BaseModel):
    allowed_titles: Optional[list[str]] = None
    forbidden_titles: Optional[list[str]] = None
    location_hard_rule: Optional[str] = None
    salary_hard_rule: Optional[str] = None
    seniority_rules: Optional[str] = None
    hard_reject_rules: Optional[list[str]] = None


@app.put("/admin/search/users/{uid}/filter")
async def admin_search_user_filter(uid: str, update: FilterUpdate):
    """Edits a user's search plan by hand (titles to find or skip, location,
    salary, seniority, hard rules). The next search uses it; the old version is
    kept next to it."""
    uid = _safe_uid(uid)
    path = SCRIPT_DIR / "search_contracts" / f"{uid}.json"
    contract = _read_json(path)
    if not uid or contract is None:
        raise HTTPException(status_code=404, detail="This user has no search plan yet - run a search for them first.")
    changes = {k: v for k, v in (update.model_dump() if hasattr(update, "model_dump") else update.dict()).items() if v is not None}
    for k, v in changes.items():
        if isinstance(v, list):
            changes[k] = [x.strip() for x in v if x and x.strip()]
        elif isinstance(v, str):
            changes[k] = v.strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    history = path.parent / "history"
    history.mkdir(exist_ok=True)
    (history / f"{uid}_{stamp}.json").write_text(json.dumps(contract, indent=2))
    contract.update(changes)
    contract["edited_by_admin_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(contract, indent=2))
    return {"filter": {k: contract.get(k) for k in EDITABLE_FILTER_FIELDS}}


@app.post("/admin/search/fill-short", status_code=202)
async def admin_search_fill_short(req: SearchRunRequest, background_tasks: BackgroundTasks):
    """One search for every paying user short of the daily target today: each
    is topped up to the target (one run, users one after another)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = await admin_search_daily(day)
    emails = [u["email"] for u in daily["users"] if u["missing"] > 0 and u["email"]]
    if not emails:
        return {"accepted": False, "message": "Everyone already has their jobs today."}
    env = _search_env(req.model, req.profile)
    background_tasks.add_task(_single_run, "send_jobbyo.py", ["--emails", ",".join(emails)], f"run:fill:{len(emails)}", env)
    return {"accepted": True, "message": f"Filling {len(emails)} user(s) short of {DAILY_TARGET} today.", "emails": emails}


class AddAnyway(BaseModel):
    title: str = ""
    company: str = ""
    job_url: str
    location: str = ""
    reason: str = ""  # why the filter turned it down


@app.post("/admin/search/users/{uid}/add-job")
async def admin_search_add_anyway(uid: str, job: AddAnyway):
    """A job the filter turned down that a person wants: it goes to the user's
    list, and the search learns not to turn down jobs like it again."""
    uid = _safe_uid(uid)
    if not uid or not job.job_url.strip():
        raise HTTPException(status_code=422, detail="Need a user and the job's link.")

    def post():
        resp = requests.post(f"{BACKEND_BASE}/automations/users/{uid}/selected-jobs", json={
            "job_id": f"admin_{uuid.uuid4().hex[:12]}", "job_url": job.job_url.strip(), "title": job.title,
            "company": job.company, "description": job.location, "grade": 0,
        }, timeout=30)
        resp.raise_for_status()

    try:
        await asyncio.to_thread(post)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't add it to their list: {exc}")
    entry = team_corrections.add(uid, job.title, job.company, job.reason, job.job_url)
    return {"added": True, "correction": entry}


@app.delete("/admin/search/users/{uid}/corrections/{correction_id}")
async def admin_search_remove_correction(uid: str, correction_id: str):
    """Forgets one of the team's lessons for this user."""
    if not team_corrections.remove(_safe_uid(uid), re.sub(r"[^a-f0-9]", "", correction_id)):
        raise HTTPException(status_code=404, detail="Not found.")
    return {"removed": True}


def _paid_uids_sync() -> set:
    return {_uid_of(u) for u in _fetch_paid_users_sync() if _uid_of(u)}


def _doc_files(uid: str) -> list[Path]:
    """Everything the search keeps for one user (persona, notes, lessons,
    search plan and its history, strategy reports)."""
    personas, contracts, reports = SCRIPT_DIR / "personas", SCRIPT_DIR / "search_contracts", SCRIPT_DIR / "strategy_reports"
    return ([p for p in personas.glob(f"{uid}.*") if p.is_file()]
            + [p for p in [contracts / f"{uid}.json"] if p.exists()]
            + list((contracts / "history").glob(f"{uid}_*.json"))
            + list(reports.glob(f"strategy_{uid}_*.json")) + list(reports.glob(f"adaptation_{uid}_*.json")))


@app.post("/admin/search/users/delete-unpaid")
async def admin_search_delete_unpaid(dry_run: bool = False):
    """Deletes the docs of everyone who no longer pays (persona, notes, search
    plan, strategy reports). If they pay again, their first search rebuilds them."""
    def run():
        paid = _paid_uids_sync()
        if len(paid) < 5:  # the paying list didn't load properly - never treat everyone as unpaid
            raise HTTPException(status_code=503, detail="Couldn't load the paying users - nothing was deleted.")
        personas, contracts = SCRIPT_DIR / "personas", SCRIPT_DIR / "search_contracts"
        uids = {p.name.split(".")[0] for p in personas.glob("*.json")} | {p.stem for p in contracts.glob("*.json")}
        unpaid = sorted(u for u in uids if u and u not in paid)
        moved = 0
        for uid in unpaid:
            for path in _doc_files(uid):
                moved += 1
                if not dry_run:
                    path.unlink(missing_ok=True)
        return {"users": len(unpaid), "files": moved, "dry_run": dry_run}
    return await asyncio.to_thread(run)
