"""What the team corrected for a user from the admin's Job Search page.

When a person adds a job the filter turned down ("Add anyway"), that's a
lesson: the review was wrong about this kind of job. The lessons are kept per
user in personas/<uid>.team_corrections.json and go into every AI review of
that user's jobs (send_jobbyo.py), so the same mistake isn't repeated.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path("./personas")
MAX_IN_REVIEW = 20  # newest lessons that go into the review prompt


def _path(uid: str) -> Path:
    return DIR / f"{uid}.team_corrections.json"


def load(uid: str) -> list[dict]:
    try:
        return json.loads(_path(uid).read_text()).get("corrections") or []
    except (OSError, ValueError, AttributeError):
        return []


def _save(uid: str, corrections: list[dict]) -> None:
    DIR.mkdir(exist_ok=True)
    _path(uid).write_text(json.dumps({"corrections": corrections}, indent=2))


def add(uid: str, title: str, company: str, reason: str, job_url: str = "") -> dict:
    """A job the filter rejected that a person approved."""
    entry = {
        "id": uuid.uuid4().hex[:10], "kind": "approved_anyway",
        "title": (title or "").strip()[:160], "company": (company or "").strip()[:120],
        "filter_reason": (reason or "").strip()[:300], "job_url": job_url,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _save(uid, load(uid) + [entry])
    return entry


def remove(uid: str, correction_id: str) -> bool:
    corrections = load(uid)
    kept = [c for c in corrections if c.get("id") != correction_id]
    if len(kept) == len(corrections):
        return False
    _save(uid, kept)
    return True


def review_context(uid: str) -> str:
    """Text block for the AI review prompt; empty when nothing was corrected."""
    lessons = load(uid)[-MAX_IN_REVIEW:]
    if not lessons:
        return ""
    lines = ["CORRECTED BY THE JOBBYO TEAM (a person reviewed these - do not repeat the mistake):"]
    for c in lessons:
        why = f' because "{c["filter_reason"]}"' if c.get("filter_reason") else ""
        lines.append(f"- The filter rejected \"{c['title']}\" at {c['company']}{why}, and the team approved it. "
                     "Don't reject similar jobs for that reason.")
    return "\n".join(lines)
