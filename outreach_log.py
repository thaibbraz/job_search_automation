"""Per-user touch history for the hot-lead outreach cadence -- one file per
user, checked before each send so the same person is never re-nudged more
often than the cadence allows (touch 1 immediate, touch 2 at +3 days, touch
3 at +7 days, stop after 3 or on conversion -- see the daily outreach job).

Separate from lead_scoring.py's hot_leads/ (the pending-first-touch queue,
which a lead is removed from the moment touch 1 sends) -- this is the
durable record of everything sent, kept regardless of queue state.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

OUTREACH_LOG_DIR = Path("./outreach_log")


def _log_path(uid: str) -> Path:
    return OUTREACH_LOG_DIR / f"{uid}.json"


def get_touch_history(uid: str) -> list:
    path = _log_path(uid)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("touches", [])
    except Exception:
        return []


def record_touch(uid: str, campaign: str, tier: Optional[int] = None, summary: Optional[str] = None) -> None:
    """campaign: e.g. "hot_lead_nudge". summary: a short plain-text
    description of what the email said, for the retention Slack post -- not
    the full body, just enough for a human skimming the channel."""
    OUTREACH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    touches = get_touch_history(uid)
    touches.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign,
        "tier": tier,
        "summary": summary,
    })
    with open(_log_path(uid), "w", encoding="utf-8") as f:
        json.dump({"uid": uid, "touches": touches}, f, indent=2)
