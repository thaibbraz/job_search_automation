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
from pydantic import BaseModel

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable
RUN_LOG_DIR = SCRIPT_DIR / "run_logs"
SYNC_RUN_TIMEOUT_SECONDS = 600

BACKEND_BASE = os.getenv("JOBBYO_BACKEND_URL", "https://fastapi-service-03-160893319817.europe-southwest1.run.app")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

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


class TopJobsResponse(BaseModel):
    accepted:        bool
    message:         str
    duration_seconds: float
    jobs:            list[TopJob]

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
    return TopJobsResponse(
        accepted=True,
        message=f"Found {len(jobs)} job(s) for {target.uid or target.email}.",
        duration_seconds=duration_seconds,
        jobs=jobs,
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

_TODAY_STATUSES = {"applied", "pending", "waiting_approval", "pending_review", "legacy"}
_APPLIED_STATUSES = {"applied", "approved"}

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%a, %d %b %Y %H:%M:%S %Z",
]


def _parse_job_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 or RFC 2822 date strings into UTC-aware datetime."""
    if not date_str:
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


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


@app.get("/coverage/today")
async def coverage_today(send_slack: bool = False):
    """Count jobs in active statuses added TODAY per paid user.

    Statuses counted: applied, pending, waiting_approval, pending_review, legacy.
    Query ?send_slack=true posts a summary to Slack.
    """
    today_utc = datetime.now(timezone.utc).date()
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
            print(f"[coverage/today] Could not fetch automation for {uid}: {exc}")
            continue

        jobs = automation.get("selectedJobs") or []
        total = applied_c = pending_c = waiting_c = 0

        for job in jobs:
            status = job.get("status", "")
            if status not in _TODAY_STATUSES:
                continue
            added = _parse_job_date(job.get("addedAt", ""))
            if added is None or added.date() != today_utc:
                continue
            total += 1
            if status == "applied":
                applied_c += 1
            elif status == "pending":
                pending_c += 1
            elif status == "waiting_approval":
                waiting_c += 1

        if total >= 10:
            covered += 1
        elif total >= 1:
            partial += 1
        else:
            missing += 1

        result_users.append({
            "name": user.get("displayName") or user.get("name") or "",
            "email": user.get("email", ""),
            "total": total,
            "applied": applied_c,
            "pending": pending_c,
            "waiting_approval": waiting_c,
        })

    if send_slack:
        ok_lines = [
            f"  • {u['name']} ({u['email']}): {u['total']} jobs"
            for u in result_users if u["total"] >= 10
        ]
        warn_lines = [
            f"  • {u['name']} ({u['email']}): {u['total']} jobs"
            for u in result_users if u["total"] < 10
        ]
        slack_text = (
            f"*Coverage Report — {today_utc.isoformat()}*\n"
            f"Total: {len(result_users)} | ✅ Covered (≥10): {covered} | "
            f"⚠️ Partial (1-9): {partial} | ❌ Missing (0): {missing}\n"
        )
        if ok_lines:
            slack_text += "\n✅ *At 10+ jobs:*\n" + "\n".join(ok_lines)
        if warn_lines:
            slack_text += "\n\n⚠️ *Below 10 jobs:*\n" + "\n".join(warn_lines)
        await asyncio.to_thread(_send_slack_sync, slack_text)

    return {
        "date": today_utc.isoformat(),
        "total_users": len(result_users),
        "covered": covered,
        "partial": partial,
        "missing": missing,
        "users": result_users,
    }


@app.get("/coverage/applied")
async def coverage_applied(send_slack: bool = False):
    """Count jobs with status applied or approved added TODAY per paid user.

    Query ?send_slack=true posts counts to Slack.
    """
    today_utc = datetime.now(timezone.utc).date()
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
            if added is not None and added.date() == today_utc:
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
    today_utc = datetime.now(timezone.utc).date()
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
            if job_date == today_utc and status in _TODAY_STATUSES:
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
