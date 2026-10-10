"""Setup-time suggestions and strategy brief for a candidate, used by the webapp's new-automation
flow (via jobbyo-fastapi-server, which proxies with the admin key).

Two calls:
- suggest_titles(uid): right after the resume is uploaded. Reads the parsed CV and returns
  12-20 job title variations to search for (current role, close variants, natural next steps and
  realistic pivots), plus a normalised seniority and location. Fast, no web search.
- build_brief(uid, prefs): at the end of setup. The same coaching analysis and web-search-grounded
  market snapshot that go into the paid welcome email's strategy PDF (outreach.py), plus
  location and title advice specific to the choices they just made in setup.

Nothing here writes to personas/ or search_contracts/ -- those drive the live nightly search, and
this is generated before the candidate has even launched. Results are cached on disk per uid and
inputs so re-opening the last setup screen doesn't re-bill the LLM calls.
"""

import hashlib
import json
import time
from pathlib import Path

import outreach
import send_jobbyo

CACHE_DIR = Path("./setup_briefs")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600

SENIORITY_LEVELS = ["intern", "entry", "mid", "senior", "lead", "executive"]


def _cache_path(kind, uid, inputs):
    digest = hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return CACHE_DIR / f"{kind}_{uid}_{digest}.json"


def _cached(path):
    try:
        if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _store(path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"setup_brief cache write failed (non-fatal): {e}")


def _profile_or_raise(uid, cv=None):
    """The user's profile and CV text. `cv` is the resume the webapp is showing right now (it may
    not be saved to the account yet); when given it wins over the saved one."""
    profile = send_jobbyo.get_user_profile(uid) or {}
    if isinstance(cv, dict) and (cv.get("first_name") or cv.get("experiences") or cv.get("title")):
        profile = {**profile, "cv": cv}
    cv_text = send_jobbyo.cv_to_text(profile) if profile else ""
    if not cv_text.strip():
        raise ValueError("No parsed resume on file for this user yet.")
    return profile, cv_text


# ---------------------------------------------------------------------------
# Title suggestions
# ---------------------------------------------------------------------------

TITLES_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["current", "variant", "step_up", "pivot"]},
                },
                "required": ["title", "kind"],
                "additionalProperties": False,
            },
        },
        "seniority": {"type": "string", "enum": SENIORITY_LEVELS},
        "location": {"type": "string"},
    },
    "required": ["titles", "seniority", "location"],
    "additionalProperties": False,
}

TITLES_PROMPT = """You are a senior technical recruiter. From this candidate's resume, list the job \
titles a job search for them should cover. These are used as literal search terms on company \
career pages and job boards, so use titles that real companies actually post -- no invented or \
inflated titles.

CV:
{cv_text}

Return:
- titles: 12 to 20 distinct job titles, most relevant first. Mix of:
  - current: what they are today (1-2)
  - variant: the same job under the different names companies use for it (most of the list)
  - step_up: a realistic next step from their actual experience (2-4)
  - pivot: an adjacent role their background genuinely supports (2-4)
  Keep each title short (2-5 words), in English, without company names, levels like "II", or
  locations. Do not repeat near-duplicates.
- seniority: their current level, one of {levels}.
- location: where they're based, as "City, Country" if the CV says, otherwise "".
"""


def suggest_titles(uid, cv=None):
    profile, cv_text = _profile_or_raise(uid, cv)
    path = _cache_path("titles", uid, {"cv": cv_text})
    hit = _cached(path)
    if hit:
        return hit

    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=TITLES_PROMPT.format(cv_text=cv_text, levels=", ".join(SENIORITY_LEVELS)),
        text={"format": {"type": "json_schema", "name": "setup_titles", "schema": TITLES_SCHEMA, "strict": True}},
    )
    data = json.loads(response.output_text)

    seen, titles = set(), []
    for t in data.get("titles") or []:
        title = " ".join(str(t.get("title") or "").split())
        if title and title.lower() not in seen:
            seen.add(title.lower())
            titles.append({"title": title, "kind": t.get("kind") or "variant"})
    result = {"titles": titles[:20], "seniority": data.get("seniority") or "", "location": data.get("location") or ""}
    _store(path, result)
    return result


# ---------------------------------------------------------------------------
# Strategy brief
# ---------------------------------------------------------------------------

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "location_advice": {"type": "string"},
        "title_advice": {"type": "string"},
        "titles_to_add": {"type": "array", "items": {"type": "string"}},
        "titles_to_drop": {"type": "array", "items": {"type": "string"}},
        "salary_advice": {"type": "string"},
        "numbers": {
            "type": "object",
            "properties": {
                "open_roles": {"type": "integer"},
                "remote_share_percent": {"type": "integer"},
                "pay_currency": {"type": "string"},
                "pay_low": {"type": "integer"},
                "pay_mid": {"type": "integer"},
                "pay_high": {"type": "integer"},
                "top_hirers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["open_roles", "remote_share_percent", "pay_currency", "pay_low", "pay_mid", "pay_high", "top_hirers"],
            "additionalProperties": False,
        },
        "market_is_small": {"type": "boolean"},
        "expansions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["add_title", "add_location", "add_remote", "lower_salary"]},
                    "value": {"type": "string"},
                    "label": {"type": "string"},
                    "reason": {"type": "string"},
                    "estimated_roles_added": {"type": "integer"},
                },
                "required": ["kind", "value", "label", "reason", "estimated_roles_added"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "location_advice", "title_advice", "titles_to_add", "titles_to_drop", "salary_advice", "numbers",
                 "market_is_small", "expansions"],
    "additionalProperties": False,
}

ADVICE_PROMPT = """You are this candidate's career coach. They just set up their job search with the \
choices below. Using the coaching notes and the market snapshot (already researched from current \
sources), give short, specific advice on THEIR choices. Write plainly, like a note to them, in \
second person. Never use an em dash.

THEIR SETUP CHOICES:
{choices}

COACHING NOTES:
{coach}

MARKET SNAPSHOT:
{market}

CV (excerpt):
{cv_excerpt}

Return:
- headline: one sentence, under 16 words, that sums up their best angle in this market.
- location_advice: 1-2 sentences on their location choices (for example, if remote-only shrinks
  the pool a lot, or a nearby hub or a specific country is worth adding). If their choices are
  already right, say so briefly.
- title_advice: 1-2 sentences on the titles they picked: which pivot or title change would open
  the most doors, grounded in their background.
- titles_to_add: 0-4 titles worth adding to their search that they did not pick.
- titles_to_drop: 0-3 of the titles they picked that are likely to waste applications, if any.
- salary_advice: one sentence comparing their minimum salary to the market range; "" if no
  minimum was set.
- numbers: the figures from the MARKET SNAPSHOT only, for charts. Never estimate or invent:
  - open_roles: the approximate number of open roles it mentions, or 0 if it gives none.
  - remote_share_percent: the remote-friendly share it mentions (0-100), or 0 if none.
  - pay_currency: the 3-letter currency of the pay range (e.g. USD, EUR, GBP), or "" if none.
  - pay_low / pay_high: the yearly pay range it gives, in whole units (e.g. 75000), or 0 if none.
  - pay_mid: the typical/median yearly pay if it gives one, otherwise 0.
  - top_hirers: up to 5 companies or kinds of company it names as hiring, or [] if none.
- market_is_small: true if their current choices leave a small pool (roughly under 1,500 open roles,
  or the snapshot says roles are scarce for this profile/location), else false.
- expansions: 2-4 concrete one-tap changes that would grow their pool the most, realistic for
  their background. Each has:
  - kind: add_title (a close title they didn't pick), add_location (a nearby city/metro or
    country they could work in), add_remote (only if they excluded remote), or lower_salary.
  - value: exactly what to apply: the title text, the location text, "remote", or the new yearly
    minimum as digits in their currency.
  - label: 2-6 words for a button, e.g. "Add Data Analyst", "Add Madrid", "Include remote roles",
    "Lower minimum to 55,000".
  - reason: one short sentence on why it helps.
  - estimated_roles_added: your best estimate of extra open roles it adds, consistent with
    numbers.open_roles; 0 if you cannot estimate.
  Don't suggest titles or locations they already chose.
Do not invent numbers that are not in the market snapshot.
"""


def _clean_numbers(n):
    """Charts only get figures that make sense; anything zero/negative/inverted becomes null."""
    def pos(v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    low, mid, high = pos(n.get("pay_low")), pos(n.get("pay_mid")), pos(n.get("pay_high"))
    if low and high and low > high:
        low, high = high, low
    if mid and not (low and high and low <= mid <= high):
        mid = None
    share = pos(n.get("remote_share_percent"))
    return {
        "open_roles": pos(n.get("open_roles")),
        "remote_share_percent": share if share and share <= 100 else None,
        "pay": {"currency": (n.get("pay_currency") or "").upper()[:3] or None, "low": low, "mid": mid, "high": high} if (low and high) else None,
        "top_hirers": [str(h).strip() for h in (n.get("top_hirers") or []) if str(h).strip()][:5],
    }


def _clean_expansions(items, prefs):
    """Drop suggestions that repeat what's already chosen or can't be applied."""
    titles = {str(t).strip().lower() for t in prefs.get("jobTitles") or []}
    places = {str(p).strip().lower() for p in (prefs.get("location") or {}).get("places") or []}
    types = (prefs.get("location") or {}).get("type") or []
    types = [types] if isinstance(types, str) else types
    out, seen = [], set()
    for e in items:
        kind, value = e.get("kind"), " ".join(str(e.get("value") or "").split())
        if not value or (kind, value.lower()) in seen:
            continue
        if kind == "add_title" and value.lower() in titles:
            continue
        if kind == "add_location" and value.lower() in places:
            continue
        if kind == "add_remote" and "remote" in types:
            continue
        if kind == "lower_salary":
            digits = "".join(ch for ch in value if ch.isdigit())
            if not digits:
                continue
            value = digits
        seen.add((kind, value.lower()))
        out.append({
            "kind": kind,
            "value": value,
            "label": str(e.get("label") or value)[:60].replace("\u2014", ","),
            "reason": str(e.get("reason") or "")[:200].replace("\u2014", ","),
            "estimated_roles_added": max(0, int(e.get("estimated_roles_added") or 0)),
        })
    return out[:4]


def build_brief(uid, prefs, cv=None):
    prefs = prefs or {}
    profile, cv_text = _profile_or_raise(uid, cv)
    inputs = {
        "cv": cv_text,
        "jobTitles": prefs.get("jobTitles") or [],
        "location": prefs.get("location") or {},
        "minimumAcceptableSalary": prefs.get("minimumAcceptableSalary") or "",
        "salaryCurrency": prefs.get("salaryCurrency") or "",
        "v": 3,  # bump when the brief's shape changes, so old cached briefs aren't served
    }
    path = _cache_path("brief", uid, inputs)
    hit = _cached(path)
    if hit:
        return hit

    coach = outreach.generate_coach_analysis(profile, prefs)
    try:
        market = outreach.generate_market_overview(profile, prefs, coach)
    except Exception as e:
        print(f"setup_brief market overview failed (non-fatal): {e}")
        market = {}

    choices = json.dumps({
        "job_titles": inputs["jobTitles"],
        "workplace": (inputs["location"] or {}).get("type"),
        "locations": (inputs["location"] or {}).get("places"),
        "minimum_salary": inputs["minimumAcceptableSalary"],
        "currency": inputs["salaryCurrency"],
    }, indent=2)
    try:
        response = send_jobbyo.responses_create(
            model=send_jobbyo.SEARCH_MODEL,
            input=ADVICE_PROMPT.format(
                choices=choices,
                coach=json.dumps(coach, indent=2),
                market=json.dumps(market, indent=2),
                cv_excerpt=cv_text[:1500],
            ),
            text={"format": {"type": "json_schema", "name": "setup_advice", "schema": ADVICE_SCHEMA, "strict": True}},
        )
        advice = json.loads(response.output_text)
    except Exception as e:
        print(f"setup_brief advice failed (non-fatal): {e}")
        advice = {}

    strip = outreach._strip_citations
    result = {
        "headline": (advice.get("headline") or "").replace("—", ","),
        "positioning_pitch": coach.get("positioning_pitch", ""),
        "strengths": coach.get("strengths", []),
        "blind_spots": coach.get("blind_spots", []),
        "coach_recommendation": coach.get("coach_recommendation", ""),
        "alternative_paths": coach.get("alternative_paths", []),
        "recommended_titles": coach.get("recommended_titles", []),
        "roles_to_avoid": coach.get("roles_to_avoid", []),
        "market": {k: strip(v) for k, v in (market or {}).items()},
        "advice": {
            "location": advice.get("location_advice", ""),
            "titles": advice.get("title_advice", ""),
            "salary": advice.get("salary_advice", ""),
            "titles_to_add": advice.get("titles_to_add", []),
            "titles_to_drop": advice.get("titles_to_drop", []),
        },
        "numbers": _clean_numbers(advice.get("numbers") or {}),
        "market_is_small": bool(advice.get("market_is_small")),
        "expansions": _clean_expansions(advice.get("expansions") or [], prefs),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _store(path, result)
    return result


# ---------------------------------------------------------------------------
# Calibration roles
# ---------------------------------------------------------------------------
# Shown while the first search runs, for a quick "would you apply?" rating. These are EXAMPLE
# roles written by the model (never real postings, never real company names, no links): each one
# deliberately varies company size, industry, culture, workplace, seniority and pay so every
# rating says something about the candidate's preferences. The webapp saves the ratings (with
# these tags) on the automation as `calibration`, plus a short `calibrationProfile` summary; the
# search learns from both (see send_jobbyo.extract_rejected_jobs_from_automation).

CALIBRATION_COUNT = 7
COMPANY_SIZES = ["startup", "scaleup", "midsize", "enterprise"]
SENIORITY_SHIFTS = ["same_level", "step_up", "step_down"]
WORKPLACES = ["remote", "hybrid", "onsite"]

CALIBRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "company_size": {"type": "string", "enum": COMPANY_SIZES},
                    "industry": {"type": "string"},
                    "culture": {"type": "string"},
                    "workplace": {"type": "string", "enum": WORKPLACES},
                    "location": {"type": "string"},
                    "seniority": {"type": "string", "enum": SENIORITY_SHIFTS},
                    "salary_min": {"type": "integer"},
                    "salary_max": {"type": "integer"},
                    "currency": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "company", "company_size", "industry", "culture", "workplace", "location",
                             "seniority", "salary_min", "salary_max", "currency", "skills"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["roles"],
    "additionalProperties": False,
}

CALIBRATION_PROMPT = """You are calibrating a job-search agent for this candidate. Write {count} EXAMPLE job \
roles for them to rate ("would you apply?"). These are not real postings: they exist only to learn the \
candidate's preferences, so design them as a set where every role tests something.

CANDIDATE'S SEARCH:
{choices}

CV (excerpt):
{cv_excerpt}

Rules for the set:
- Every role must be one this candidate could realistically apply to: their target titles, close
  variants, or a role similar to their most recent jobs. Realistic for their seniority and skills.
- Vary these across the set so each rating is informative: company_size (startup, scaleup, midsize,
  enterprise), industry (mostly ones close to their background, plus one or two adjacent ones),
  culture (e.g. fast-paced and scrappy, structured and process-driven, mission-driven, client-facing
  agency), workplace (remote, hybrid, onsite), seniority (mostly same_level, one step_up, at most one
  step_down) and pay (around their minimum, and above it).
- Locations: their target cities and nearby cities or metro areas; for remote roles say which
  country or region it's open to.
- company: a short DESCRIPTION, never a real company name. E.g. "Series B fintech, ~80 people",
  "Global logistics group, 20,000 employees", "Regional hospital network".
- title: 2-6 words, in English. skills: 3-4 short skills the role asks for.
- salary_min / salary_max: yearly, whole units, in {currency}. Plausible for the role and location.
- No two roles alike. Never use an em dash.
"""


def calibration_jobs(uid, prefs=None, cv=None):
    """Example roles for calibration. `prefs` is the jobPreferences the user just set (the
    automation may not be saved yet); falls back to the saved automation."""
    profile, cv_text = _profile_or_raise(uid, cv)
    prefs = prefs or {}
    if not prefs.get("jobTitles"):
        automation = send_jobbyo.get_user_automation(uid) or {}
        prefs = (automation.get("settings") or {}).get("jobPreferences") or automation.get("jobPreferences") or prefs
    currency = (prefs.get("salaryCurrency") or "USD").upper()
    choices = {
        "job_titles": (prefs.get("jobTitles") or [])[:8],
        "workplace": (prefs.get("location") or {}).get("type"),
        "locations": (prefs.get("location") or {}).get("places"),
        "minimum_salary": prefs.get("minimumAcceptableSalary") or "",
        "currency": currency,
    }

    path = _cache_path("calibration", uid, {"cv": cv_text, "choices": choices, "v": 1})
    hit = _cached(path)
    if hit:
        return hit

    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=CALIBRATION_PROMPT.format(
            count=CALIBRATION_COUNT,
            choices=json.dumps(choices, indent=2),
            cv_excerpt=cv_text[:2500],
            currency=currency,
        ),
        text={"format": {"type": "json_schema", "name": "calibration_roles", "schema": CALIBRATION_SCHEMA, "strict": True}},
    )
    roles = json.loads(response.output_text).get("roles") or []

    jobs = []
    for i, r in enumerate(roles[:CALIBRATION_COUNT]):
        title = " ".join(str(r.get("title") or "").split())
        if not title:
            continue
        lo, hi = int(r.get("salary_min") or 0), int(r.get("salary_max") or 0)
        jobs.append({
            "id": f"cal-{i}-{hashlib.sha1(title.encode()).hexdigest()[:6]}",
            "example": True,
            "title": title,
            "company": str(r.get("company") or "").replace("\u2014", ","),
            "location": str(r.get("location") or ""),
            "workplace": r.get("workplace") or "",
            "salary": {"min": lo, "max": hi, "currency": (r.get("currency") or currency).upper()} if hi > 0 else None,
            "tags": {
                "size": r.get("company_size") or "",
                "industry": str(r.get("industry") or ""),
                "culture": str(r.get("culture") or ""),
                "workplace": r.get("workplace") or "",
                "seniority": r.get("seniority") or "",
            },
            "skills": [str(x) for x in (r.get("skills") or [])][:4],
        })
    result = {"jobs": jobs}
    _store(path, result)
    return result
