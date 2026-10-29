"""
email_pauses.py — people whose daily report email is paused.

They keep getting jobs in the app; approve_jobs.py just doesn't email them
until they're resumed. Set from the admin Job Search page (Emails tab).
Keyed by lowercased email. Lives in run_logs/, a docker volume on the
server, so it survives redeploys.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

PATH = Path("run_logs") / "email_pauses.json"


def load() -> dict:
    """email -> {"paused_at", "reason"}"""
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def is_paused(email: str) -> bool:
    return (email or "").strip().lower() in load()


def set_paused(email: str, paused: bool, reason: str = "") -> dict:
    key = (email or "").strip().lower()
    pauses = load()
    if paused:
        pauses[key] = {"paused_at": datetime.now(timezone.utc).isoformat(), "reason": reason.strip()}
    else:
        pauses.pop(key, None)
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(pauses, indent=2))
    return pauses
