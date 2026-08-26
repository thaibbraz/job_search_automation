"""
api.py — FastAPI app to trigger job-search and email workflows.

Endpoints
---------
GET  /health                  Health check
GET  /run/status              Current run state + last run info

POST /run/all                 Trigger send_jobbyo.py for all users
POST /run/user                Trigger send_jobbyo.py for one user  (body: {uid?, email?})
POST /run/user/subscribed     Run for one user, block until done, email their first matches
                               (body: {uid?, email?}) — called from the Stripe successful-
                               checkout webhook, once payment is confirmed. On a successful
                               send, also fires outreach.send_paid_welcome_email
                               (PAID_WELCOME_DELAY_SECONDS later, in the background) — a
                               personal follow-up note with a strategy PDF, no trial pitch.

POST /email/all               Trigger approve_jobs.py for all users (promote + email)
POST /email/user              Trigger approve_jobs.py for one user  (body: {email})

POST /approve/all             Alias for /email/all
POST /approve/user            Alias for /email/user

GET  /coverage/today          Per-user job counts today + coverage %  (query: ?send_slack=true)
GET  /coverage/missing        Paid users not yet emailed today (per approve_jobs.py's send log)
POST /email/missing           Force-send the daily report to everyone not yet emailed today

POST /leads/hot                Score a just-created automation's salary/location; if it clears
                               the hot-lead bar (see lead_scoring.py), record it and Slack-post.
                               Called from jobbyo-fastapi-server's create_automation_job for
                               anyone not already paid/trialing — no-ops otherwise.

Usage
-----
    uvicorn api:app --host 0.0.0.0 --port 8000

Set JOBBYO_TARGET_JOBS in .env to control the per-user daily target (default 9).
"""

import asyncio
import json
import os
import sys
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

# How long after the transactional welcome email (send_subscribed_jobs_email)
# the personal follow-up note (outreach.send_paid_welcome_email, with the
# strategy PDF) fires. Not zero -- landing at the same instant as the first
# email reads as two automated messages, not one person following up.
PAID_WELCOME_DELAY_SECONDS = int(os.getenv("JOBBYO_PAID_WELCOME_DELAY_SECONDS", "900"))

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
# #marketing-team only, posted under a separate persona ("Jobbyo — the
# retention specialist") from Laras -- hot-lead / retention-outreach events
# specifically, not general ops reporting.
SLACK_WEBHOOK_URL_RETENTION = os.getenv("SLACK_WEBHOOK_URL_RETENTION", "")
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

async def _run_subprocess(cmd: list[str], label: str) -> int:
    print(f"[{label}] Starting: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(SCRIPT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for line in proc.stdout:
        print(f"[{label}] {line.decode(errors='replace').rstrip()}")
    await proc.wait()
    code = proc.returncode or 0
    print(f"[{label}] Finished — exit code {code}")
    return code


async def _full_run(script: str, args: list[str], label: str):
    """Background task for full (all-user) runs. Sets _state flags."""
    _state.full_run_active = True
    _state.full_run_started_at = datetime.now(timezone.utc)
    _state.full_run_script = script
    try:
        code = await _run_subprocess([PYTHON, script, *args], label)
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


async def _single_run(script: str, args: list[str], label: str):
    """Background task for single-user runs. Does NOT lock full-run state."""
    try:
        await _run_subprocess([PYTHON, script, *args], label)
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
    allow_methods=["GET", "POST", "OPTIONS"],
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
    background_tasks.add_task(_full_run, "send_jobbyo.py", [], "run:all")
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
    background_tasks.add_task(_single_run, "send_jobbyo.py", args, label)
    return RunResponse(accepted=True, message=f"Top-up run started for {target.uid or target.email}.")


def _fire_paid_welcome(uid: str, label: str) -> None:
    """Fire-and-forget: waits PAID_WELCOME_DELAY_SECONDS, then sends the
    personal follow-up note (outreach.send_paid_welcome_email) -- real
    matches from build_context, persona coaching, strategy PDF, no trial
    pitch. Best-effort; a failure here doesn't touch the transactional
    welcome email that already went out."""

    async def _run():
        await asyncio.sleep(PAID_WELCOME_DELAY_SECONDS)
        try:
            import outreach

            context = await asyncio.to_thread(outreach.build_context, uid)
            body_text = await asyncio.to_thread(outreach.generate_paid_welcome_message, context)
            try:
                pdf_bytes = await asyncio.to_thread(outreach.generate_strategy_pdf, context)
            except Exception as e:
                print(f"[{label}] paid-welcome PDF generation failed (non-fatal, sends without it): {e}")
                pdf_bytes = None
            sent = await asyncio.to_thread(
                outreach.send_paid_welcome_email, context, body_text, pdf_bytes,
                TOP_JOBS_TEST_RECIPIENT or None,
            )
            if sent:
                await asyncio.to_thread(
                    _send_ops_only_slack_sync,
                    f"Sent {context.get('first_name') or uid} a personal follow-up note with their strategy PDF.",
                )
        except Exception as e:
            print(f"[{label}] paid-welcome send failed (non-fatal): {e}")

    task = asyncio.ensure_future(_run())
    _background_keepalive.add(task)
    task.add_done_callback(_background_keepalive.discard)


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

    emailed = False
    profile = {}
    try:
        import send_jobbyo, approve_jobs

        profile = send_jobbyo.get_user_profile(resolved_uid) or {}
        recipient = TOP_JOBS_TEST_RECIPIENT or resolved_email or target.email
        emailed = bool(
            await asyncio.to_thread(
                approve_jobs.send_subscribed_jobs_email,
                profile,
                jobs_added,
                send_jobbyo.TARGET_JOBS_PER_USER,
                recipient,
            )
        )
    except Exception as exc:
        print(f"[{label}] email send failed: {exc}")

    if emailed and resolved_uid:
        _fire_paid_welcome(resolved_uid, label)

    # send_subscribed_jobs_email now always sends (a thin/empty batch just
    # gets a "still searching" variant instead of job cards -- see that
    # function's docstring), so emailed=False here means an actual send
    # failure (Brevo error, missing key), not a thin-batch skip.
    below_minimum = len(jobs_added) < approve_jobs.MIN_JOBS_TO_NOTIFY_SUBSCRIBED
    suffix = " Emailed (no matches yet)." if emailed and below_minimum else (" Emailed." if emailed else " Email failed.")

    recipient_for_slack = TOP_JOBS_TEST_RECIPIENT or resolved_email or target.email
    who = profile.get("displayName") or recipient_for_slack
    if emailed:
        job_word = "job" if len(jobs_added) == 1 else "jobs"
        slack_text = f"Found {len(jobs_added)} {job_word} for {who} and just sent them a message with the details."
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
        message=f"Found {len(jobs_added)} job(s) for {target.uid or target.email}.{suffix}",
        duration_seconds=duration_seconds,
        jobs_found=len(jobs_added),
        jobs=response_jobs,
        emailed=emailed,
    )


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

    task = asyncio.ensure_future(_run_subprocess([PYTHON, "send_jobbyo.py", *args], label))
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

    await asyncio.to_thread(lead_scoring.record_lead, lead)

    fire_label = {1: "🔥", 2: "🔥🔥", 3: "🔥🔥🔥"}.get(lead.tier, "🔥")
    tier_name = {1: "good", 2: "super good", 3: "super-duper good"}.get(lead.tier, "good")
    where = lead.location_text or lead.region
    await asyncio.to_thread(
        _send_retention_slack_sync,
        f"{fire_label} Just spotted a {tier_name} lead — {lead.name or lead.email or lead.uid} "
        f"(${lead.salary_usd:,}, {where}). They've set up their search but haven't started "
        "their trial yet. I'll give them 24h, then reach out if they still haven't signed up.",
    )

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
    """Send the prospect nudge email to a recorded hot lead. Meant to be
    called by the daily outreach job (not built yet) -- exposed as an
    endpoint too so it can be triggered/tested manually in the meantime.
    Once it sends: removes the lead from hot_leads/ (a lead only sits there
    while its first touch is still pending -- ongoing cadence tracking
    lives in outreach_log/, which this also writes to) and posts a
    retention Slack summary with who they are, their salary/location, and
    a snippet of what the email said."""
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
        where = lead.get("location_text") or lead.get("region")
        await asyncio.to_thread(
            _send_retention_slack_sync,
            f"📨 Sent a nudge to {lead.get('name') or lead.get('email') or uid} "
            f"(${lead.get('salary_usd', 0):,}, {where}). What I said: "
            f"“{summary}…”",
        )

    return NudgeResponse(
        accepted=True, sent=sent,
        message=f"Nudge {'sent' if sent else 'failed'} for {uid}.",
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


def _send_retention_slack_sync(text: str) -> bool:
    """Hot-lead / retention-outreach events, posted to #marketing-team under
    a distinct persona from Laras -- this is a different audience (growth/
    marketing, not ops) watching for a different kind of signal (leads worth
    chasing, not pipeline health)."""
    if not SLACK_WEBHOOK_URL_RETENTION:
        print("[slack] No retention Slack webhook configured — skipping notification.")
        return False
    resp = requests.post(
        SLACK_WEBHOOK_URL_RETENTION,
        json={"text": text, "username": "Jobbyo — the retention specialist", "icon_emoji": ":handshake:"},
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
