"""
api.py — FastAPI app to trigger job-search and email workflows.

Endpoints
---------
GET  /health                  Health check
GET  /run/status              Current run state + last run info
GET  /schedule                Show configured auto-run schedule

POST /run/all                 Trigger send_jobbyo.py for all users
POST /run/user                Trigger send_jobbyo.py for one user  (body: {uid?, email?})

POST /email/all               Trigger approve_jobs.py for all users (promote + email)
POST /email/user              Trigger approve_jobs.py for one user  (body: {email})

POST /approve/all             Alias for /email/all
POST /approve/user            Alias for /email/user

Scheduling
----------
Set JOBBYO_RUN_TIMES in .env to a comma-separated list of UTC times (HH:MM).
The API will automatically trigger /run/all at each of those times.
Default: "07:00,11:00,16:00,21:00"  (4 runs per day)

Set JOBBYO_TARGET_JOBS in .env to control the per-user daily target (default 9).

Usage
-----
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Schedule config
# ---------------------------------------------------------------------------

_DEFAULT_RUN_TIMES = "07:00,11:00,16:00,21:00"
_RUN_TIMES_RAW = os.getenv("JOBBYO_RUN_TIMES", _DEFAULT_RUN_TIMES)

def _parse_run_times(raw: str) -> list[tuple[int, int]]:
    """Parse 'HH:MM,HH:MM,...' into [(hour, minute), ...]."""
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            result.append((int(h), int(m)))
        except ValueError:
            print(f"[scheduler] Ignoring invalid time entry: {part!r}")
    return result

SCHEDULED_TIMES = _parse_run_times(_RUN_TIMES_RAW)

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
    last_scheduled_trigger_at: Optional[datetime] = None

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

class StatusResponse(BaseModel):
    full_run_active:            bool
    full_run_script:            Optional[str]
    full_run_started_at:        Optional[str]
    last_full_run_at:           Optional[str]
    last_full_run_result:       Optional[str]
    last_full_run_exit_code:    Optional[int]
    last_scheduled_trigger_at:  Optional[str]

class ScheduleResponse(BaseModel):
    timezone:        str
    run_times_utc:   list[str]
    target_jobs:     int
    next_runs:       list[str]

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


async def _scheduled_run():
    """Called by APScheduler. Skips gracefully if a run is already active."""
    now = datetime.now(timezone.utc)
    _state.last_scheduled_trigger_at = now
    if _state.full_run_active:
        print(f"[scheduler] {now.isoformat()} — skipping scheduled run, one is already active")
        return
    print(f"[scheduler] {now.isoformat()} — triggering scheduled top-up run")
    await _full_run("send_jobbyo.py", [], f"scheduled:{now.strftime('%H:%M')}")

# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone="UTC")

for _hour, _minute in SCHEDULED_TIMES:
    scheduler.add_job(
        _scheduled_run,
        CronTrigger(hour=_hour, minute=_minute, timezone="UTC"),
        id=f"topup_{_hour:02d}{_minute:02d}",
        replace_existing=True,
    )

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

app = FastAPI(title="Jobbyo Runner API", version="1.0.0")

@app.on_event("startup")
async def _startup():
    scheduler.start()
    times_str = ", ".join(f"{h:02d}:{m:02d} UTC" for h, m in SCHEDULED_TIMES)
    print(f"[scheduler] Started — daily top-up runs at: {times_str}")
    print(f"[scheduler] Target jobs per user: {os.getenv('JOBBYO_TARGET_JOBS', '9')}")

@app.on_event("shutdown")
async def _shutdown():
    scheduler.shutdown(wait=False)

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
        last_scheduled_trigger_at=_state.last_scheduled_trigger_at.isoformat() if _state.last_scheduled_trigger_at else None,
    )


@app.get("/schedule", response_model=ScheduleResponse)
async def get_schedule():
    """Show the configured auto-run schedule and next fire times."""
    next_runs = []
    for job in scheduler.get_jobs():
        next_fire = job.next_run_time
        if next_fire:
            next_runs.append(next_fire.isoformat())
    next_runs.sort()
    return ScheduleResponse(
        timezone="UTC",
        run_times_utc=[f"{h:02d}:{m:02d}" for h, m in SCHEDULED_TIMES],
        target_jobs=int(os.getenv("JOBBYO_TARGET_JOBS", "9")),
        next_runs=next_runs[:8],
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
