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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable
RUN_LOG_DIR = SCRIPT_DIR / "run_logs"
SYNC_RUN_TIMEOUT_SECONDS = 600

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


class TopJobsResponse(BaseModel):
    accepted: bool
    message:  str
    jobs:     list[TopJob]

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
        return TopJobsResponse(accepted=True, message=f"Run finished for {target.uid or target.email}, but no run log entry was found.", jobs=[])

    jobs_added = sorted(match.get("jobs_added") or [], key=lambda j: j.get("grade") or 0, reverse=True)[:limit]
    jobs = [
        TopJob(title=j.get("title"), company=j.get("company"), url=j.get("job_url"), location=j.get("location"), grade=j.get("grade"))
        for j in jobs_added
    ]
    return TopJobsResponse(accepted=True, message=f"Found {len(jobs)} job(s) for {target.uid or target.email}.", jobs=jobs)


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
