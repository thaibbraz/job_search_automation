"""Scores a just-created automation's salary/location signal into a hot-lead
tier, and tracks who's already been recorded so the same person doesn't get
re-added on every automation edit.

Called from api.py's POST /leads/hot, which jobbyo-fastapi-server's
create_automation_job hits (best-effort, fire-and-forget) for anyone who
isn't already a paying/trialing user at automation-creation time. This is
the qualification half only -- the daily outreach agent that actually
nudges these people (24h after they qualify, bounded follow-ups, Slack per
send) is a separate, not-yet-built piece; today this module only decides
"is this person worth chasing" and records them for that later job to pick
up.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HOT_LEADS_DIR = Path("./hot_leads")

# Static FX table (-> USD). Unknown currency is treated as unparseable and
# excluded rather than guessed -- better to miss a lead than misqualify one
# on a bad conversion.
_FX_TO_USD = {
    "usd": 1.0,
    "eur": 1.08,
    "gbp": 1.27,
    "cad": 0.73,
    "aud": 0.66,
}

# Fire-tier salary bands, USD-equivalent. Named constants so they're easy to
# retune without hunting through logic.
TIER_1_MIN_USD = 60_000   # good lead
TIER_2_MIN_USD = 90_000   # super good lead
TIER_3_MIN_USD = 130_000  # super-duper good lead

_US_MARKERS = {
    "united states", "usa", "u.s.", "u.s.a.", "us",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
    "new york city", "nyc", "san francisco", "los angeles", "chicago",
    "seattle", "austin", "boston", "denver", "miami", "atlanta",
}

_EU_MARKERS = {
    "europe", "european union", "eu",
    "united kingdom", "uk", "england", "scotland", "wales",
    "ireland", "france", "germany", "spain", "italy", "portugal",
    "netherlands", "belgium", "luxembourg", "switzerland", "austria",
    "sweden", "norway", "denmark", "finland", "poland", "czech republic",
    "czechia", "hungary", "romania", "greece", "croatia", "slovenia",
    "slovakia", "estonia", "latvia", "lithuania", "bulgaria",
    "london", "city of london", "paris", "berlin", "munich", "madrid",
    "barcelona", "milan", "rome", "amsterdam", "dublin", "lisbon",
    "zurich", "geneva", "stockholm", "copenhagen", "vienna", "warsaw",
}


@dataclass
class ScoredLead:
    uid: str
    email: Optional[str]
    name: Optional[str]
    tier: int
    salary_usd: int
    region: str  # "US" | "EU"


def normalize_salary_usd(amount, currency: Optional[str]) -> Optional[int]:
    """amount: numeric or numeric-string, no symbol (e.g. "80000" or 80000).
    currency: 3-letter code, any case, e.g. "USD"/"GBP". Returns None if
    either can't be parsed -- excluded rather than guessed."""
    if amount is None:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(amount))
        value = float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        value = None
    if not value:
        return None

    rate = _FX_TO_USD.get((currency or "usd").strip().lower())
    if rate is None:
        return None
    return int(round(value * rate))


def classify_region(places) -> Optional[str]:
    """places: list of free-text location strings. Best-effort keyword
    match -- ambiguous/unrecognized locations are excluded, not force-fit
    into a region."""
    if not places:
        return None
    for place in places:
        text = str(place).strip().lower()
        if not text:
            continue
        if text in _US_MARKERS or any(marker in text for marker in _US_MARKERS):
            return "US"
        if text in _EU_MARKERS or any(marker in text for marker in _EU_MARKERS):
            return "EU"
    return None


def fire_tier(salary_usd: int) -> int:
    if salary_usd >= TIER_3_MIN_USD:
        return 3
    if salary_usd >= TIER_2_MIN_USD:
        return 2
    return 1


def qualifies(
    uid: str,
    email: Optional[str],
    name: Optional[str],
    salary,
    salary_currency: Optional[str],
    locations,
) -> Optional[ScoredLead]:
    """None if this signal doesn't clear the salary+region bar. A qualifying
    signal below TIER_1_MIN_USD never reaches here (fire_tier only grades
    what's already >= the tier-1 floor)."""
    salary_usd = normalize_salary_usd(salary, salary_currency)
    if salary_usd is None or salary_usd < TIER_1_MIN_USD:
        return None

    region = classify_region(locations)
    if region is None:
        return None

    return ScoredLead(
        uid=uid,
        email=email,
        name=name,
        tier=fire_tier(salary_usd),
        salary_usd=salary_usd,
        region=region,
    )


def _lead_path(uid: str) -> Path:
    return HOT_LEADS_DIR / f"{uid}.json"


def already_recorded(uid: str) -> bool:
    return _lead_path(uid).exists()


def record_lead(lead: ScoredLead) -> None:
    """Idempotent-ish: overwrites if called again (e.g. a later automation
    edit re-scores higher), but callers should check already_recorded()
    first to avoid re-Slacking the same person on every edit."""
    HOT_LEADS_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(lead)
    data["added_at"] = datetime.now(timezone.utc).isoformat()
    data["touches"] = []
    with open(_lead_path(lead.uid), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
