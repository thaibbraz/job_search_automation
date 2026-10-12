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
        "location_options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["add_remote", "add_hybrid", "add_location"]},
                    "value": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["kind", "value", "label"],
                "additionalProperties": False,
            },
        },
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
    "required": ["headline", "location_advice", "location_options", "title_advice", "titles_to_add", "titles_to_drop", "salary_advice", "numbers",
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
- location_options: the one-tap changes your location_advice recommends, 0-3, so the candidate can
  apply them right there. Each: kind add_remote, add_hybrid or add_location; value "remote",
  "hybrid", or the city/metro/country text; label 2-5 words for a button, e.g. "Include remote",
  "Include hybrid", "Add Porto". Only what the advice actually suggests, never something they
  already chose; [] if the advice says their choices are already right.
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


def _clean_location_options(items, prefs):
    """Location advice as buttons: skip anything already chosen or empty."""
    loc = prefs.get("location") or {}
    places = {str(p).strip().lower() for p in loc.get("places") or []}
    types = loc.get("type") or []
    types = [types] if isinstance(types, str) else types
    out, seen = [], set()
    for o in items:
        kind = o.get("kind")
        value = " ".join(str(o.get("value") or "").split())
        if kind == "add_remote":
            value = "remote"
        elif kind == "add_hybrid":
            value = "hybrid"
        if not value or (kind, value.lower()) in seen or value in types or value.lower() in places:
            continue
        seen.add((kind, value.lower()))
        out.append({"kind": kind, "value": value, "label": str(o.get("label") or value)[:40].replace("\u2014", ",")})
    return out[:3]


def build_brief(uid, prefs, cv=None):
    prefs = prefs or {}
    profile, cv_text = _profile_or_raise(uid, cv)
    inputs = {
        "cv": cv_text,
        "jobTitles": prefs.get("jobTitles") or [],
        "location": prefs.get("location") or {},
        "minimumAcceptableSalary": prefs.get("minimumAcceptableSalary") or "",
        "salaryCurrency": prefs.get("salaryCurrency") or "",
        "v": 4,  # bump when the brief's shape changes, so old cached briefs aren't served
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
            "location_options": _clean_location_options(advice.get("location_options") or [], prefs),
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
# Pool re-estimate
# ---------------------------------------------------------------------------
# When the candidate taps a "grow your pool" change on the brief, the webapp asks for a fresh total
# for the whole updated search instead of adding a per-change guess to the original number (those
# guesses were made relative to the first estimate and drifted far from what a fresh brief for the
# same choices reports). Same market snapshot, no web search, so it is quick and stays consistent.

RECOUNT_SCHEMA = {
    "type": "object",
    "properties": {"open_roles": {"type": "integer"}},
    "required": ["open_roles"],
    "additionalProperties": False,
}

RECOUNT_PROMPT = """You estimated how many open roles fit a job seeker's search. They changed their \
search. Estimate the open roles for the UPDATED search, on the same basis as the original estimate.

ORIGINAL SEARCH:
{before}

ORIGINAL ESTIMATE: about {open_roles} open roles.

UPDATED SEARCH:
{after}

MARKET SNAPSHOT used for the original estimate:
{market}

Rules:
- Count roles that fit the whole updated search: any of its titles, in any of its locations or
  remote if included, at or above its minimum salary.
- Be consistent with the original estimate: if the search only grew (more titles, more places,
  remote added, lower minimum) the number must not go down; if it only shrank it must not go up;
  if nothing that matters changed, return the original number.
- Size the change realistically for what was added: a big metro or "remote" for a common role can
  add a lot; a niche title or a small town adds little.
- open_roles: one whole number."""


def _search_summary(prefs):
    loc = (prefs or {}).get("location") or {}
    return json.dumps({
        "job_titles": (prefs or {}).get("jobTitles") or [],
        "workplace": loc.get("type"),
        "locations": loc.get("places"),
        "minimum_salary": (prefs or {}).get("minimumAcceptableSalary") or "",
        "currency": (prefs or {}).get("salaryCurrency") or "",
    }, indent=2)


def recount_open_roles(uid, base_prefs, prefs, market=None, open_roles=0):
    """Estimated open roles for `prefs`, anchored on the brief's estimate (`open_roles`) for
    `base_prefs`. Returns {"open_roles": int}; falls back to the original number on failure."""
    base = max(0, int(open_roles or 0))
    before, after = _search_summary(base_prefs), _search_summary(prefs)
    if before == after or not base:
        return {"open_roles": base}
    path = _cache_path("recount", uid, {"before": before, "after": after, "base": base, "v": 2})
    hit = _cached(path)
    if hit:
        return hit
    try:
        response = send_jobbyo.responses_create(
            model=send_jobbyo.SEARCH_MODEL,
            input=RECOUNT_PROMPT.format(before=before, after=after, open_roles=base,
                                        market=json.dumps(market or {}, indent=2)[:4000]),
            text={"format": {"type": "json_schema", "name": "pool_recount", "schema": RECOUNT_SCHEMA, "strict": True}},
        )
        n = max(0, int(json.loads(response.output_text).get("open_roles") or 0))
    except Exception as e:
        print(f"setup_brief recount failed (non-fatal): {e}")
        return {"open_roles": base}
    # Hold the model to the direction of the change: a broader search never shows fewer roles.
    direction = _search_direction(base_prefs, prefs)
    if direction > 0:
        n = max(n, base)
    elif direction < 0:
        n = min(n, base) if n else base
    result = {"open_roles": n or base}
    _store(path, result)
    return result


def _search_direction(before, after):
    """1 if `after` only widens `before` (more titles/places/workplaces, lower minimum), -1 if it
    only narrows it, 0 otherwise."""
    def parts(p):
        p = p or {}
        loc = p.get("location") or {}
        t = loc.get("type")
        types = t if isinstance(t, list) else [t] if t else []
        try:
            sal = float(str(p.get("minimumAcceptableSalary") or 0).replace(",", "") or 0)
        except ValueError:
            sal = 0.0
        return set(p.get("jobTitles") or []), set(loc.get("places") or []), set(types), sal
    t0, p0, w0, s0 = parts(before)
    t1, p1, w1, s1 = parts(after)
    if t1 >= t0 and p1 >= p0 and w1 >= w0 and (s1 <= s0 or not s0):
        return 1
    if t1 <= t0 and p1 <= p0 and w1 <= w0 and (s1 >= s0 or not s1):
        return -1
    return 0


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


# ---------------------------------------------------------------------------
# Job profile from calibration
# ---------------------------------------------------------------------------
# After the candidate rates the example roles, turn the ratings plus their resume into a short read
# on what suits them and the structured preferences the app keeps in settings. Option lists match
# the webapp's automation settings (jobPreferences.companyPreferences.industries / .sizes and
# jobPreferences.experienceLevel), so whatever is filled here shows up there and can be edited.
# Industries are free text (AI-picked, as specific as the ratings allow); sizes and levels use the
# settings' fixed options.

PROFILE_SIZES = ["Startup", "Small", "Mid-size", "Large"]

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "industries": {"type": "array", "items": {"type": "string"}},
        "company_sizes": {"type": "array", "items": {"type": "string", "enum": PROFILE_SIZES}},
        "culture": {"type": "array", "items": {"type": "string"}},
        "levels": {"type": "array", "items": {"type": "string", "enum": SENIORITY_LEVELS}},
    },
    "required": ["summary", "industries", "company_sizes", "culture", "levels"],
    "additionalProperties": False,
}

PROFILE_PROMPT = """You are a sharp, warm career coach. A job seeker just rated example roles ("would apply" or \
"not for me"). From their ratings and resume, tell them what suits them and fill in their job
preferences.

THEIR SEARCH:
{choices}

RATINGS (each role's company size, industry, culture, workplace and seniority, and their answer):
{ratings}

CV (excerpt):
{cv_excerpt}

Return:
- summary: 2 short sentences to them ("you"), under 35 words total, everyday words. First: what
  kind of place suits them. Second: what they avoid, or why it fits their background. No jargon,
  no flattery, no em dashes. Example: "You do best in calm, well-run hospitals. You skip fast
  startups, which fits your hands-on ICU work."
- industries: 1-3 specific industries or sectors that best match what they would apply to and their
  background, 1-3 words each, as precise as the ratings allow (e.g. "Public healthcare", "Fintech",
  "Renewable energy", "B2B SaaS"), not broad buckets like "Other".
- company_sizes: 1-3 from the allowed list (Startup <50, Small 50-200, Mid-size 201-1000, Large 1000+).
- culture: 1-3 short traits they lean towards, 1-3 words each, e.g. "Calm", "Mission-driven",
  "Fast-paced", "Structured".
- levels: 1-2 seniority levels to target, from: {levels}.
Base it on the ratings first; use the resume to break ties. If they said "not for me" to everything,
infer from what they rejected and their resume."""


def calibration_profile(uid, ratings, prefs=None, cv=None):
    """{"summary", "industries", "company_sizes", "culture", "levels"} for the candidate, from their
    calibration ratings (title, company, tags, liked) and resume. Cached per uid + inputs."""
    _, cv_text = _profile_or_raise(uid, cv)
    prefs = prefs or {}
    rated = []
    for r in ratings or []:
        if not isinstance(r, dict) or not isinstance(r.get("liked"), bool):
            continue
        rated.append({
            "role": f"{str(r.get('title') or '')[:80]} at {str(r.get('company') or '')[:80]}",
            "tags": r.get("tags") if isinstance(r.get("tags"), dict) else {},
            "answer": "would apply" if r["liked"] else "not for me",
        })
    if not rated:
        raise ValueError("No calibration ratings to analyse.")
    choices = {
        "job_titles": prefs.get("jobTitles") or [],
        "locations": (prefs.get("location") or {}).get("places") or [],
    }
    path = _cache_path("profile", uid, {"cv": cv_text, "rated": rated, "choices": choices, "v": 3})
    hit = _cached(path)
    if hit:
        return hit

    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=PROFILE_PROMPT.format(
            choices=json.dumps(choices, indent=2),
            ratings=json.dumps(rated, indent=2),
            cv_excerpt=cv_text[:2000],
            levels=", ".join(SENIORITY_LEVELS),
        ),
        text={"format": {"type": "json_schema", "name": "job_profile", "schema": PROFILE_SCHEMA, "strict": True}},
    )
    data = json.loads(response.output_text)

    def uniq(items, allowed=None, limit=3):
        out = []
        for x in items or []:
            x = " ".join(str(x).split())[:40]
            if x and x not in out and (allowed is None or x in allowed):
                out.append(x)
        return out[:limit]

    result = {
        "summary": str(data.get("summary") or "").replace("—", ",").strip()[:600],
        "industries": uniq(data.get("industries")),
        "company_sizes": uniq(data.get("company_sizes"), PROFILE_SIZES),
        "culture": uniq(data.get("culture")),
        "levels": uniq(data.get("levels"), SENIORITY_LEVELS, 2),
    }
    _store(path, result)
    return result
