"""Learn from what each user rejects, every night.

Users reject jobs in the app, often with a reason ("too much AI", "low
salary", "already applied to 4 Etsy positions"). Until now that only fed the
OpenAI web-search prompt, which barely runs anymore. This turns it into:

  1. preference notes per user -- a short LLM summary of their rejections into
     rules, cached in personas/<uid>.feedback.json and rebuilt only when their
     rejections change
  2. hard rules (via fit_ranker): companies they've turned against, too many
     jobs from one company, salary floor
  3. soft rules: the notes + recent rejections go into the AI review prompt
  4. a weekly rejection rate per user for the Slack report, to see it drop
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTES_DIR = Path("./personas")
NOTES_MODEL = "gpt-4.1-mini"
COMPANY_CAP = 3          # max jobs from one company in a user's last 30 days
COMPANY_WINDOW_DAYS = 30
MAX_REJECTIONS_FOR_NOTES = 60

_NOTES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rules", "blocked_companies", "min_salary_usd"],
    "properties": {
        "rules": {"type": "array", "items": {"type": "string"}},
        "blocked_companies": {"type": "array", "items": {"type": "string"}},
        "min_salary_usd": {"type": ["integer", "null"]},
    },
}


def _norm_company(name) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# --- 1. preference notes -------------------------------------------------


def preference_notes(client, uid: str, rejected_jobs: list[dict]) -> dict:
    """Short, reusable rules learned from this user's rejections."""
    empty = {"rules": [], "blocked_companies": [], "min_salary_usd": None}
    rejected = [j for j in rejected_jobs if j.get("title") or j.get("company")][-MAX_REJECTIONS_FOR_NOTES:]
    if not rejected or not uid:
        return empty

    compact = [
        {"title": str(j.get("title", ""))[:90], "company": str(j.get("company", ""))[:60],
         "feedback": str(j.get("feedback") or "")[:200]}
        for j in rejected
    ]
    key = hashlib.sha1(json.dumps(compact, sort_keys=True).encode()).hexdigest()
    path = NOTES_DIR / f"{uid}.feedback.json"
    try:
        cached = json.loads(path.read_text())
        if cached.get("key") == key:
            return cached["notes"]
    except (OSError, ValueError, KeyError):
        pass

    if client is None:
        return empty

    prompt = f"""A job seeker rejected these jobs we suggested. Some include their reason.
Turn them into a few short, specific rules for picking their NEXT jobs.

- rules: 0-6 rules, each under 15 words, only patterns supported by the data
  (a reason given, or the same kind of job rejected repeatedly). e.g.
  "Avoid AI-first startups", "No contracts shorter than 6 months",
  "Avoid marketing-tech products". No generic advice.
- blocked_companies: only companies they clearly don't want (a reason about
  the company itself, or rejected 3+ times). Not every rejected company.
- min_salary_usd: only if they said salary was too low AND a number is implied; else null.

REJECTED JOBS (oldest first):
{json.dumps(compact, indent=1)}"""
    try:
        resp = client.responses.create(
            model=NOTES_MODEL,
            input=prompt,
            text={"format": {"type": "json_schema", "name": "notes", "schema": _NOTES_SCHEMA, "strict": True}},
        )
        notes = json.loads(resp.output_text)
    except Exception as e:  # learning is an improvement, never a reason to fail a user's run
        print(f"Feedback notes failed (non-fatal): {e}")
        return empty

    try:
        NOTES_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps({"key": key, "built_at": datetime.now(timezone.utc).isoformat(), "notes": notes}))
    except OSError:
        pass
    return notes


# --- 2. inputs for fit_ranker's hard rules --------------------------------


def blocked_companies(prefs: dict, notes: dict) -> set[str]:
    """User's own excluded companies + companies their feedback rules out."""
    excluded = (prefs.get("companyPreferences") or {}).get("excludedCompanies") or []
    return {_norm_company(c) for c in list(excluded) + list(notes.get("blocked_companies") or []) if _norm_company(c)}


def recent_company_counts(selected_jobs: list[dict]) -> Counter:
    """Jobs per company added to the user's queue in the last 30 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=COMPANY_WINDOW_DAYS)
    counts = Counter()
    for j in selected_jobs:
        added = _parse_ts(j.get("addedAt"))
        if added and added >= cutoff and j.get("company"):
            counts[_norm_company(j["company"])] += 1
    return counts


def min_salary_usd(prefs: dict, notes: dict):
    currency = str(prefs.get("salaryCurrency") or (prefs.get("salaryRange") or {}).get("currency") or "USD").upper()
    floor = None
    if currency == "USD":
        try:
            floor = int(float(str(prefs.get("minimumAcceptableSalary") or "").replace(",", "")))
        except ValueError:
            floor = None
    learned = notes.get("min_salary_usd")
    if learned and (floor is None or learned > floor):
        floor = learned
    return floor if floor and floor >= 10_000 else None


# --- 3. soft rules for the AI review ---------------------------------------


def review_context(notes: dict, rejected_jobs: list[dict]) -> str:
    """Text block for the AI review prompt; empty when there's nothing learned."""
    recent = [
        f"- {j.get('title', '')} at {j.get('company', '')}" + (f": \"{str(j['feedback'])[:120]}\"" if j.get("feedback") else "")
        for j in rejected_jobs[-15:]
    ]
    if not notes.get("rules") and not recent:
        return ""
    parts = ["LEARNED FROM THIS USER'S OWN REJECTIONS (weigh heavily; downgrade or reject jobs that repeat these patterns):"]
    parts += [f"- {r}" for r in notes.get("rules") or []]
    if recent:
        parts.append("Recently rejected by the user:")
        parts += recent
    return "\n".join(parts)


# --- 4. weekly rejection rate --------------------------------------------


_UNREVIEWED = {"waiting_approval", "pending_review", "needs_review", "pending", "review", "in_review"}


def rejection_counts(selected_jobs: list[dict]) -> dict:
    """Jobs the user acted on, and how many of those they rejected, this week
    and last week (by the day the job was added to their queue)."""
    now = datetime.now(timezone.utc)
    windows = {"week": (now - timedelta(days=7), now), "prev_week": (now - timedelta(days=14), now - timedelta(days=7))}
    out = {}
    for name, (start, end) in windows.items():
        delivered = rejected = 0
        for j in selected_jobs:
            added = _parse_ts(j.get("addedAt"))
            status = str(j.get("status", "")).lower()
            # Jobs still waiting for the user's review would make this week
            # look artificially good -- count only jobs they've acted on.
            if added and start <= added < end and status not in _UNREVIEWED:
                delivered += 1
                rejected += status == "rejected"
        out[name] = (delivered, rejected)
    return out


def pct(delivered: int, rejected: int):
    return round(100 * rejected / delivered) if delivered else None
