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


def _profile_or_raise(uid):
    profile = send_jobbyo.get_user_profile(uid) or {}
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


def suggest_titles(uid):
    profile, cv_text = _profile_or_raise(uid)
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
    },
    "required": ["headline", "location_advice", "title_advice", "titles_to_add", "titles_to_drop", "salary_advice"],
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
Do not invent numbers that are not in the market snapshot.
"""


def build_brief(uid, prefs):
    prefs = prefs or {}
    profile, cv_text = _profile_or_raise(uid)
    inputs = {
        "cv": cv_text,
        "jobTitles": prefs.get("jobTitles") or [],
        "location": prefs.get("location") or {},
        "minimumAcceptableSalary": prefs.get("minimumAcceptableSalary") or "",
        "salaryCurrency": prefs.get("salaryCurrency") or "",
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
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _store(path, result)
    return result
