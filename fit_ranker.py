"""Experience-first job fit: hard rules + CV-embedding ranking.

Runs on the merged structured pool (JFE + Hiring.cafe + Jobo) after sourcing
and before the AI review, so the reviewer only sees jobs that:

  1. pass hard rules taken from what the USER chose -- never from the
     AI-generated persona/search contract, which was observed inventing
     countries and titles the user never picked (e.g. a Luxembourg/NL/BE
     user's persona adding "United States" + "Claims Manager"):
       - location: the job's country must be one the user listed; when the
         user only listed cities for a country (no bare country), an
         on-site/hybrid job must be in one of those cities/states
       - remote-only users never get on-site/hybrid jobs
       - experience: job's minimum years can't exceed the user's by > 2,
         and entry-level/intern roles don't go to experienced users
       - sponsorship: users who need a visa skip jobs that say they won't sponsor
  2. rank by semantic similarity between the user's CV and the job
     (OpenAI embeddings), so experience -- not title overlap -- decides
     which jobs reach the reviewer first. Only the top FIT_POOL_KEEP survive.

Hiring.cafe jobs carry structured facts (_hc_fit, set in _parse_hiring_cafe_job);
other sources fall back to parsing their location/description text.
"""
from __future__ import annotations

import math
import os
import re
from datetime import date

FIT_EMBED_MODEL = os.getenv("JOBBYO_FIT_EMBED_MODEL", "text-embedding-3-small")
FIT_POOL_KEEP = int(os.getenv("JOBBYO_FIT_POOL_KEEP", "60") or 60)
MAX_YEARS_GAP = 2

# Country name/alias -> ISO2. Covers every country seen in user prefs so far
# plus the common ones; unknown places just don't constrain by country.
_COUNTRIES = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "america": "US",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR", "chile": "CL",
    "colombia": "CO", "peru": "PE", "united kingdom": "GB", "uk": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "ireland": "IE", "france": "FR", "germany": "DE",
    "netherlands": "NL", "the netherlands": "NL", "belgium": "BE", "luxembourg": "LU",
    "spain": "ES", "portugal": "PT", "italy": "IT", "switzerland": "CH", "austria": "AT",
    "poland": "PL", "czech republic": "CZ", "czechia": "CZ", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "greece": "GR", "romania": "RO", "hungary": "HU",
    "india": "IN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP",
    "singapore": "SG", "malaysia": "MY", "thailand": "TH", "vietnam": "VN", "philippines": "PH",
    "indonesia": "ID", "japan": "JP", "republic of korea": "KR", "south korea": "KR",
    "korea": "KR", "china": "CN", "hong kong": "HK", "taiwan": "TW", "australia": "AU",
    "new zealand": "NZ", "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA",
    "qatar": "QA", "israel": "IL", "turkey": "TR", "egypt": "EG", "nigeria": "NG",
    "ghana": "GH", "kenya": "KE", "south africa": "ZA", "morocco": "MA",
}
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
}
_NO_SPONSOR = re.compile(
    r"(unable|not able|cannot|can't|will not|won't|do not|does not|don't|not)\s+(to\s+)?"
    r"(provide\s+|offer\s+)?(visa\s+)?sponsor"
    r"|no\s+(visa\s+)?sponsorship"
    r"|without\s+(the\s+need\s+for\s+)?(current\s+or\s+future\s+)?(visa\s+)?sponsorship",
    re.I,
)
_YEARS_REQ = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?(?:\s+of)?\s+(?:\w+\s+){0,3}experience", re.I)
# A user picking 1-MAX_FOCUSED_INDUSTRIES industries is a real choice; picking
# many reads as "open to anything" and isn't enforced.
MAX_FOCUSED_INDUSTRIES = 3
_INDUSTRY_WORDS = {
    "healthcare": ["health", "medical", "hospital", "clinic", "pharma", "biotech", "life science", "care"],
    "technology": ["software", "saas", "tech", "internet", "ai", "data", "cloud", "cyber", "it "],
    "finance": ["financ", "bank", "fintech", "insurance", "invest", "payment", "lending", "capital"],
    "education": ["educat", "edtech", "school", "university", "learning", "academ"],
    "consulting": ["consult", "advisory", "professional services"],
    "media": ["media", "entertainment", "publishing", "advertis", "news", "gaming"],
    "manufacturing": ["manufactur", "industrial", "automotive", "aerospace", "hardware"],
    "retail": ["retail", "e-commerce", "ecommerce", "consumer", "apparel", "grocery"],
}
_ENTRY_LEVEL = {"entry level", "internship", "intern", "new grad", "junior"}


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _country_of(text: str) -> str | None:
    t = _norm(text)
    if t in _COUNTRIES:
        return _COUNTRIES[t]
    if t in _US_STATES:
        return "US"
    return None


# --- candidate side --------------------------------------------------------


def candidate_profile(user_profile: dict, prefs: dict) -> dict:
    """What the user actually chose + facts from their CV."""
    loc = prefs.get("location") or {}
    types = {_norm(t).replace("onsite", "on-site") for t in (loc.get("type") or []) if _norm(t)}

    # country -> None (anywhere in that country) or a set of city/state tokens
    places: dict[str, set[str] | None] = {}
    for raw in loc.get("places") or []:
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        if not parts:
            continue
        country = None
        for p in reversed(parts):
            country = _country_of(p)
            if country:
                break
        if not country:
            continue
        local = [_norm(p) for p in parts if not _country_of(p) or _norm(p) in _US_STATES]
        if not local:
            places[country] = None  # bare country: anywhere there
        elif places.get(country, set()) is not None:
            places.setdefault(country, set()).update(local)

    cv = (user_profile or {}).get("cv") or {}
    starts = []
    for exp in cv.get("experiences") or []:
        m = re.search(r"(19|20)\d{2}", str((exp or {}).get("startYear") or (exp or {}).get("date") or ""))
        if m:
            starts.append(int(m.group()))
    years = max(0, date.today().year - min(starts)) if starts else None
    if years is not None:
        years = min(years, 40)

    sponsorship = _norm(cv.get("sponsorship"))
    needs_sponsorship = sponsorship in {"yes", "true", "required", "need", "needed"} or cv.get("sponsorship") is True

    return {
        "types": types,
        "places": places,
        "years": years,
        "needs_sponsorship": needs_sponsorship,
        "titles": [str(t) for t in (prefs.get("jobTitles") or []) if str(t).strip()],
        "industries": [_norm(i) for i in ((prefs.get("companyPreferences") or {}).get("industries") or []) if _norm(i)],
    }


# --- job side --------------------------------------------------------------


def job_facts(job: dict) -> dict:
    hc = job.get("_hc_fit") or {}
    location = job.get("location") or ""
    wtype = _norm(hc.get("workplace_type") or job.get("_hc_workplace_type") or "")
    if not wtype and "remote" in _norm(location):
        wtype = "remote"

    countries = set(hc.get("countries") or [])
    if not countries:
        for part in re.split(r"[,;/|]| or ", location):
            c = _country_of(part)
            if c:
                countries.add(c)

    min_years = hc.get("min_yoe")
    if min_years is None:
        found = [int(n) for n in _YEARS_REQ.findall(job.get("description") or "") if 0 < int(n) <= 20]
        min_years = min(found) if found else None

    return {
        "type": wtype,
        "countries": countries,
        "remote_countries": set(hc.get("remote_countries") or []),
        "worldwide": bool(hc.get("worldwide_ok")),
        "local": _norm(" ".join([location, *(hc.get("cities") or []), *(hc.get("states") or [])])),
        "seniority": _norm(hc.get("seniority")),
        "min_years": min_years,
        "industries": _norm(" ".join(hc.get("industries") or [])),
    }


def hard_reject_reason(job: dict, cand: dict) -> str | None:
    f = job_facts(job)
    is_remote = f["type"] == "remote"
    places = cand["places"]

    if cand["types"] == {"remote"} and f["type"] in {"onsite", "on-site", "hybrid"}:
        return f"user wants remote only, job is {f['type']}"

    if places:
        if is_remote:
            reach = f["remote_countries"] or f["countries"]
            if not f["worldwide"] and reach and not reach & places.keys():
                return f"remote only in {','.join(sorted(reach))}, user wants {','.join(sorted(places))}"
        elif f["countries"]:
            shared = f["countries"] & places.keys()
            if not shared:
                return f"job in {','.join(sorted(f['countries']))}, user wants {','.join(sorted(places))}"
            if all(places[c] is not None for c in shared):
                wanted = set().union(*(places[c] for c in shared))
                if f["local"] and not any(w in f["local"] for w in wanted):
                    return f"job not in user's cities ({', '.join(sorted(wanted))})"

    years = cand["years"]
    if years is not None and f["min_years"] is not None and f["min_years"] > years + MAX_YEARS_GAP:
        return f"needs {f['min_years']}+ yrs, user has ~{years}"
    if years is not None and years >= 6 and f["seniority"] in _ENTRY_LEVEL:
        return f"{f['seniority']} role, user has ~{years} yrs"

    picked = cand["industries"]
    if 0 < len(picked) <= MAX_FOCUSED_INDUSTRIES and f["industries"]:
        words = [w for i in picked for w in _INDUSTRY_WORDS.get(i, [i])]
        if not any(w in f["industries"] for w in words):
            return f"company not in {', '.join(picked)}"

    if cand["needs_sponsorship"] and _NO_SPONSOR.search(job.get("description") or ""):
        return "no visa sponsorship"
    return None


def describe_for_review(job: dict) -> str:
    """One line of structured facts prepended to the description the AI reviewer sees."""
    f = job_facts(job)
    bits = []
    if f["seniority"]:
        bits.append(f"Seniority: {f['seniority']}")
    if f["min_years"] is not None:
        bits.append(f"Min experience: {f['min_years']} yrs")
    if f["type"]:
        bits.append(f"Workplace: {f['type']}")
    return " · ".join(bits)


# --- embedding ranking -----------------------------------------------------


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(client, texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 100):
        resp = client.embeddings.create(model=FIT_EMBED_MODEL, input=texts[i:i + 100])
        out.extend(d.embedding for d in resp.data)
    return out


def _job_text(job: dict) -> str:
    return f"{job.get('title') or ''} at {job.get('company') or ''}\n{(job.get('description') or '')[:2000]}"


def rank_by_cv_fit(client, jobs: list[dict], cv_text: str, titles: list[str]) -> list[dict]:
    """Sets job['fit_score'] (0-100) and returns jobs sorted best first."""
    profile = cv_text[:8000] + ("\n\nTarget roles: " + ", ".join(titles) if titles else "")
    vectors = _embed(client, [profile] + [_job_text(j) for j in jobs])
    cv_vec = vectors[0]
    for job, vec in zip(jobs, vectors[1:]):
        job["fit_score"] = round(100 * max(0.0, _cosine(cv_vec, vec)))
    return sorted(jobs, key=lambda j: j["fit_score"], reverse=True)


def apply_fit_stage(jobs, user_profile, prefs, cv_text, openai_client=None, keep=FIT_POOL_KEEP):
    """Hard rules, then CV-fit ranking. Returns (kept_jobs, rejected_with_reasons)."""
    cand = candidate_profile(user_profile, prefs)
    kept, rejected = [], []
    for job in jobs:
        reason = hard_reject_reason(job, cand)
        if reason:
            rejected.append((job, reason))
        else:
            facts = describe_for_review(job)
            if facts and not str(job.get("description") or "").startswith(facts):
                job["description"] = f"{facts}\n\n{job.get('description') or ''}"
            kept.append(job)

    print(
        f"Fit stage: {len(jobs)} jobs → {len(kept)} pass hard rules "
        f"(user ~{cand['years']} yrs, places={ {k: sorted(v) if v else 'any' for k, v in cand['places'].items()} }, "
        f"types={sorted(cand['types'])}, needs_sponsorship={cand['needs_sponsorship']})"
    )
    for reason, n in _top_reasons(rejected):
        print(f"  rejected {n}× {reason}")

    if kept and openai_client and cv_text:
        try:
            kept = rank_by_cv_fit(openai_client, kept, cv_text, cand["titles"])
            dropped = kept[keep:]
            kept = kept[:keep]
            rejected.extend((j, f"low CV fit ({j['fit_score']})") for j in dropped)
            if kept:
                print(f"Fit stage: CV-fit ranked; keeping top {len(kept)} "
                      f"(fit {kept[0]['fit_score']}–{kept[-1]['fit_score']}), dropped {len(dropped)}")
        except Exception as e:  # ranking is an improvement, never a reason to lose the pool
            print(f"Fit stage: CV-fit ranking failed, keeping source order: {e}")
    return kept, rejected


def _top_reasons(rejected, n=6):
    counts: dict[str, int] = {}
    for _, reason in rejected:
        key = re.sub(r"\d+", "N", reason) if reason.startswith("needs") else reason
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:n]
