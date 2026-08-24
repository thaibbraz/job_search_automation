"""
api.py — FastAPI app to trigger job-search and email workflows.

Endpoints
---------
GET  /health                  Health check
GET  /run/status              Current run state + last run info

POST /run/all                 Trigger send_jobbyo.py for all users
POST /run/user                Trigger send_jobbyo.py for one user  (body: {uid?, email?})
POST /run/user/top-jobs        Run for one user and block until done, returning top N jobs found
                               (body: {uid?, email?}, query: ?limit=3)

POST /email/all               Trigger approve_jobs.py for all users (promote + email)
POST /email/user              Trigger approve_jobs.py for one user  (body: {email})

POST /approve/all             Alias for /email/all
POST /approve/user            Alias for /email/user

GET  /coverage/today          Per-user job counts today + coverage %  (query: ?send_slack=true)
GET  /coverage/missing        Paid users not yet emailed today (per approve_jobs.py's send log)
POST /email/missing           Force-send the daily report to everyone not yet emailed today

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

# /run/user/subscribed's callers (Stripe webhook, automation-creation flow)
# fire it as a detached background task with no read timeout of their own —
# they're happy to wait however long the search takes. So give it a much
# longer budget than the other synchronous endpoints, which a frontend is
# waiting on for a loading state.
SUBSCRIBED_RUN_TIMEOUT_SECONDS = 1800

# Tasks kept alive here so they aren't garbage-collected mid-run: used when a
# subscribed run outlives SUBSCRIBED_RUN_TIMEOUT_SECONDS — asyncio.shield
# keeps the underlying subprocess going instead of abandoning it, but nothing
# else holds a reference to that task once the request handler returns.
_background_keepalive: set = set()

# /run/user/top-jobs runs the whole pipeline, so each call takes minutes and
# costs real money. Allow one per candidate per window.
TOP_JOBS_COOLDOWN_SECONDS = int(os.getenv("JOBBYO_TOP_JOBS_COOLDOWN_SECONDS", "86400"))
# While testing, redirect every first-matches email here instead of to the
# candidate. Unset in production so real candidates receive their own.
TOP_JOBS_TEST_RECIPIENT = os.getenv("JOBBYO_TOP_JOBS_TEST_RECIPIENT", "").strip()
_top_jobs_last_run = {}   # normalized uid/email -> datetime of last accepted call
_subscribed_last_run = {} # normalized uid/email -> datetime of last accepted /run/user/subscribed call

BACKEND_BASE = os.getenv("JOBBYO_BACKEND_URL", "https://fastapi-service-03-160893319817.europe-southwest1.run.app")
# Run health / coverage % — as opposed to per-user emailed/missing detail,
# which approve_jobs.py posts to SLACK_WEBHOOK_URL_USER_DETAILS instead.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL_DAILY_RUN", "")
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
    # When true, /run/user/top-jobs emails the results straight to the
    # candidate via Brevo, so a webhook caller needs only this one request.
    send_email: bool = False

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


class TopJobsResponse(BaseModel):
    accepted:        bool
    message:         str
    duration_seconds: float
    jobs:            list[TopJob]
    emailed:         bool = False


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
    # Called directly by jobbyo-fastapi-server's Stripe webhook the moment a
    # user pays, before it has any shared secret to send -- keeping this key
    # in sync between the two services has repeatedly drifted, so it's
    # intentionally left open rather than gated.
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


@app.post("/run/user/top-jobs", response_model=TopJobsResponse)
async def run_user_top_jobs(target: UserTarget, limit: int = 3):
    """Run a top-up search for one user and block until it's done, returning
    the top-graded jobs found this run. Meant for a frontend to call directly
    and show a loading state for — this can take a few minutes."""
    if not target.uid and not target.email:
        raise HTTPException(status_code=422, detail="Provide uid or email.")

    # One run per candidate per window: each call is a full pipeline run.
    key = (target.uid or target.email or "").strip().lower()
    last = _top_jobs_last_run.get(key)
    now = datetime.now(timezone.utc)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < TOP_JOBS_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=429,
                detail=f"Already ran for {key} recently. Try again in {int(TOP_JOBS_COOLDOWN_SECONDS - elapsed)}s.",
            )
    _top_jobs_last_run[key] = now

    if target.uid:
        args, label = ["--uid", target.uid], f"run:user:{target.uid}"
    else:
        args, label = ["--email", target.email], f"run:user:{target.email}"

    existing_logs = set(RUN_LOG_DIR.glob("job_run_*.json"))
    started_at = asyncio.get_event_loop().time()

    try:
        code = await asyncio.wait_for(
            _run_subprocess([PYTHON, "send_jobbyo.py", *args], label),
            timeout=SYNC_RUN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Run for {target.uid or target.email} did not finish within {SYNC_RUN_TIMEOUT_SECONDS}s.",
        )

    duration_seconds = round(asyncio.get_event_loop().time() - started_at, 1)

    if code != 0:
        raise HTTPException(status_code=500, detail=f"Run failed with exit code {code}. Check server logs for '{label}'.")

    new_logs = sorted(set(RUN_LOG_DIR.glob("job_run_*.json")) - existing_logs)
    match = None
    for log_path in new_logs:
        try:
            with open(log_path, encoding="utf-8") as f:
                results = json.load(f)
        except Exception:
            continue
        match = next(
            (r for r in results if (target.uid and r.get("uid") == target.uid) or (target.email and r.get("email") == target.email)),
            None,
        )
        if match is not None:
            break

    if match is None:
        return TopJobsResponse(
            accepted=True,
            message=f"Run finished for {target.uid or target.email}, but no run log entry was found.",
            duration_seconds=duration_seconds,
            jobs=[],
        )

    jobs_added = sorted(match.get("jobs_added") or [], key=lambda j: j.get("grade") or 0, reverse=True)[:limit]
    jobs = [
        TopJob(
            title=j.get("title"),
            company=j.get("company"),
            url=j.get("job_url"),
            location=j.get("location"),
            grade=j.get("grade"),
            reason=j.get("review_reason"),
        )
        for j in jobs_added
    ]
    emailed = False
    if target.send_email and jobs_added:
        # Send straight from here so a webhook caller needs one request, not a
        # round trip back with the results.
        try:
            import send_jobbyo, approve_jobs

            resolved_uid = match.get("uid")
            profile = send_jobbyo.get_user_profile(resolved_uid) or {}
            # A candidate whose jobs were not stored has no plan, so the email
            # closes on signing up rather than on their queue.
            is_paid = bool(match.get("jobs_added"))
            recipient = TOP_JOBS_TEST_RECIPIENT or match.get("email") or target.email
            emailed = bool(
                await asyncio.to_thread(
                    approve_jobs.send_first_matches_email,
                    profile,
                    jobs_added,
                    recipient,
                    is_paid,
                )
            )
        except Exception as exc:
            print(f"[{label}] email send failed: {exc}")

    suffix = " Emailed." if emailed else (" Email failed." if target.send_email and jobs_added else "")
    return TopJobsResponse(
        accepted=True,
        message=f"Found {len(jobs)} job(s) for {target.uid or target.email}.{suffix}",
        duration_seconds=duration_seconds,
        jobs=jobs,
        emailed=emailed,
    )


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
        await asyncio.to_thread(
            _send_slack_sync,
            f":wave: Subscribed search — {target.uid or target.email}: 0 jobs found, not emailed.",
        )
        return SubscribedJobsResponse(
            accepted=True,
            message=f"Run finished for {target.uid or target.email}, but no jobs were found this run.",
            duration_seconds=duration_seconds,
            jobs_found=0,
            emailed=False,
        )

    emailed = False
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

    below_minimum = len(jobs_added) < approve_jobs.MIN_JOBS_TO_NOTIFY_SUBSCRIBED
    suffix = " Emailed." if emailed else (" Below minimum — not emailed." if below_minimum else " Email failed.")

    recipient_for_slack = TOP_JOBS_TEST_RECIPIENT or resolved_email or target.email
    status_icon = ":white_check_mark:" if emailed else (":hourglass:" if below_minimum else ":x:")
    await asyncio.to_thread(
        _send_slack_sync,
        f"{status_icon} Subscribed search — {recipient_for_slack}: {len(jobs_added)} job(s) found.{suffix}",
    )

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
    """Call this the moment a user subscribes (e.g. from the signup/billing
    webhook). Runs the full pipeline for that one user, blocking until done
    (same as /run/user/top-jobs — can take a few minutes), then emails them
    their first batch with a reason per job. The email is only sent once at
    least MIN_JOBS_TO_NOTIFY_SUBSCRIBED (3) jobs were found; below the daily
    target it tells the candidate more are on the way instead of implying
    the search is finished, and above/at target it doesn't.

    Both callers (Stripe webhook, automation-creation flow) fire this as a
    detached background task with no read timeout of their own, so a slow
    run is never abandoned here either: if it outlives
    SUBSCRIBED_RUN_TIMEOUT_SECONDS, asyncio.shield keeps the subprocess
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
        if elapsed < TOP_JOBS_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=429,
                detail=f"Already ran for {key} recently. Try again in {int(TOP_JOBS_COOLDOWN_SECONDS - elapsed)}s.",
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
    if not SLACK_WEBHOOK_URL:
        print("[slack] SLACK_WEBHOOK_URL not set — skipping notification.")
        return False
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    ok = resp.status_code == 200
    if not ok:
        print(f"[slack] Webhook returned {resp.status_code}: {resp.text[:200]}")
    return ok


def _uid_of(user: dict) -> Optional[str]:
    return user.get("uid") or user.get("id") or user.get("_id")


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
            f"*Coverage Report — {report['date']}*\n"
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
        slack_text = f"*Applied Jobs Report — {today_utc.isoformat()}*\n"
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
