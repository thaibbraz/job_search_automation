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
from datetime import date, datetime, timezone

FIT_EMBED_MODEL = os.getenv("JOBBYO_FIT_EMBED_MODEL", "text-embedding-3-small")
FIT_POOL_KEEP = int(os.getenv("JOBBYO_FIT_POOL_KEEP", "60") or 60)
# Taste: what the user approved / rejected themselves moves a job up or down
# next to the CV match. Needs a few signals before it switches on.
TASTE_MIN_SIGNALS = int(os.getenv("JOBBYO_TASTE_MIN_SIGNALS", "3") or 3)
TASTE_WEIGHT = float(os.getenv("JOBBYO_TASTE_WEIGHT", "1.5") or 1.5)
# A job this close to one they rejected (and not closer to one they liked) is a near-copy: dropped.
TASTE_NEAR_COPY = float(os.getenv("JOBBYO_TASTE_NEAR_COPY", "0.92") or 0.92)
TASTE_MAX_SIGNALS = 40
# Fresher postings get answered: jobs older than FRESH_DAYS lose a few fit points.
FRESH_DAYS = int(os.getenv("JOBBYO_FRESH_DAYS", "14") or 14)
STALE_PENALTY = int(os.getenv("JOBBYO_STALE_PENALTY", "5") or 5)
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
    "kuwait": "KW", "ghana": "GH", "kenya": "KE", "south africa": "ZA", "morocco": "MA",
    # Names users type in their own language.
    "brasil": "BR", "méxico": "MX", "españa": "ES", "deutschland": "DE", "italia": "IT",
    "nederland": "NL", "schweiz": "CH", "suisse": "CH", "österreich": "AT", "polska": "PL",
    "türkiye": "TR", "perú": "PE", "belgië": "BE", "belgique": "BE",
    # Eastern Europe and more of the Americas (Georgia is left out: also a US state)
    "ukraine": "UA", "russia": "RU", "estonia": "EE", "latvia": "LV", "lithuania": "LT", "serbia": "RS",
    "croatia": "HR", "bulgaria": "BG", "slovakia": "SK", "slovenia": "SI", "iceland": "IS", "cyprus": "CY",
    "malta": "MT", "armenia": "AM", "belarus": "BY", "kazakhstan": "KZ", "costa rica": "CR", "uruguay": "UY",
    "ecuador": "EC", "venezuela": "VE", "guatemala": "GT", "dominican republic": "DO", "puerto rico": "PR",
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


_VAGUE_PLACE = re.compile(
    r"^(any|anywhere|anyplace|everywhere|worldwide|world ?wide|global(ly)?|international|the world|world|earth|all|"
    r"all locations|any location|no preference|flexible|open|n/?a|remote|remote anywhere|fully remote|100% remote|"
    r"remote worldwide|remote global)$", re.I)


def candidate_profile(user_profile: dict, prefs: dict, home_country: str | None = None) -> dict:
    """What the user actually chose + facts from their CV."""
    loc = prefs.get("location") or {}
    types = {_norm(t).replace("onsite", "on-site") for t in (loc.get("type") or []) if _norm(t)}

    # "Anywhere", "Worldwide", "Remote"... aren't places (the app turns them away
    # now, utils/places.js); one saved before that is ignored, not read as the world.
    given = [p for p in loc.get("places") or [] if str(p).strip() and not _VAGUE_PLACE.match(str(p).strip().rstrip(".!"))]

    # country -> None (anywhere in that country) or a set of city/state tokens
    places: dict[str, set[str] | None] = {}
    for raw in given:
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
    # Zero or one place we couldn't read ("Hartford, CT"): their home country.
    # Several unreadable places can span countries (Shanghai + Bangkok): left open.
    if not places and len(given) <= 1:
        home = _country_of(home_country or cv.get("country") or "")
        if home:
            places[home] = None

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

    countries = {_country_of(c) or c for c in hc.get("countries") or []}
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
        "salary_max": hc.get("salary_max"),
        "h1b_sponsor": hc.get("h1b_sponsor"),
        "posted_at": hc.get("posted_at") or "",
    }


# ISO2 -> a country's name, for notes the user reads (first alias = the usual name)
_COUNTRY_NAMES = {}
for _alias, _code in _COUNTRIES.items():
    _COUNTRY_NAMES.setdefault(_code, _alias.title() if len(_alias) > 3 else _alias.upper())


def outside_area(job: dict, cand: dict) -> str | None:
    """A short plain note when the job is outside the places they picked (e.g.
    "In United States, not Spain"), else None. The same location checks as
    hard_reject_reason: jobs from a loosened round can still land here."""
    f = job_facts(job)
    places = cand["places"]
    name = lambda codes: ", ".join(sorted(_COUNTRY_NAMES.get(c, c) for c in codes))
    if cand["types"] == {"remote"} and f["type"] in {"onsite", "on-site", "hybrid"}:
        return f"{'Hybrid' if f['type'] == 'hybrid' else 'On-site'}, and you picked remote"
    if not places:
        return None
    if f["type"] == "remote":
        reach = f["remote_countries"] or f["countries"]
        if not f["worldwide"] and reach and not reach & places.keys():
            return f"Remote only in {name(reach)}"
        return None
    if f["countries"]:
        shared = f["countries"] & places.keys()
        if not shared:
            return f"In {name(f['countries'])}, not {name(places.keys())}"
        if all(places[c] is not None for c in shared):
            wanted = set().union(*(places[c] for c in shared))
            if f["local"] and not any(w in f["local"] for w in wanted):
                return f"Outside {', '.join(sorted(w.title() for w in wanted))}"
    return None


def _company_key(name) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def hard_reject_reason(job: dict, cand: dict) -> str | None:
    f = job_facts(job)

    # Learned from the user's own rejections + excluded companies (feedback_learning.py).
    company = _company_key(job.get("company"))
    if company and company in cand.get("blocked_companies", ()):
        return "company blocked by user"
    seen = (cand.get("company_counts") or {}).get(company, 0)
    if company and seen >= cand.get("company_cap", 99):
        return f"already {seen} jobs from this company in 30 days"
    floor = cand.get("salary_floor")
    if floor and f["salary_max"] and f["salary_max"] < floor:
        return f"salary below user's minimum"

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
    if cand["needs_sponsorship"] and f["h1b_sponsor"] is False and "US" in f["countries"]:
        return "no visa sponsorship"
    return None


def _age_days(posted_at) -> float | None:
    try:
        ts = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400


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


def taste_signals(selected_jobs: list[dict], automation: dict | None = None) -> dict:
    """The user's own decisions: jobs they rejected, and jobs they approved
    themselves. Auto-mode users' jobs are approved automatically, so only
    their rejections count."""
    cfg = (automation or {}).get("applyConfig") or ((automation or {}).get("automation_data") or {}).get("applyConfig") or {}
    auto_mode = cfg.get("reviewBeforeSubmit") is False and cfg.get("requireApproval") is False
    liked, rejected = [], []
    for j in reversed(selected_jobs or []):  # newest first
        status = str(j.get("status") or "").lower()
        if status == "rejected":
            rejected.append(j)
        elif status in ("approved", "applied") and not auto_mode:
            liked.append(j)
    # The example roles they rated at setup count too, after their own decisions:
    # a new user's first search is already shaped by them.
    for item in (automation or {}).get("calibration") or []:
        if not isinstance(item, dict) or not isinstance(item.get("liked"), bool):
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
        desc = ", ".join(str(v) for v in tags.values() if v)
        if item.get("skills"):
            desc += "\nSkills: " + ", ".join(str(s) for s in item["skills"])
        example = {"title": item.get("title"), "company": item.get("company"), "description": desc}
        (liked if item["liked"] else rejected).append(example)
    return {"liked": liked[:TASTE_MAX_SIGNALS], "rejected": rejected[:TASTE_MAX_SIGNALS]}


def _knn(vec, others, k=3) -> float:
    sims = sorted((_cosine(vec, o) for o in others), reverse=True)[:k]
    return sum(sims) / len(sims) if sims else 0.0


def rank_by_cv_fit(client, jobs: list[dict], cv_text: str, titles: list[str], taste: dict | None = None) -> list[dict]:
    """Sets job['fit_score'] (0-100) and returns jobs sorted best first.
    With enough taste signals, the score also moves toward jobs like the ones
    they approved and away from ones they rejected (job['taste_score'])."""
    profile = cv_text[:8000] + ("\n\nTarget roles: " + ", ".join(titles) if titles else "")
    liked = (taste or {}).get("liked") or []
    rejected = (taste or {}).get("rejected") or []
    use_taste = len(liked) + len(rejected) >= TASTE_MIN_SIGNALS
    texts = [profile] + [_job_text(j) for j in jobs]
    if use_taste:
        texts += [_job_text(j) for j in liked] + [_job_text(j) for j in rejected]
    vectors = _embed(client, texts)
    cv_vec = vectors[0]
    job_vecs = vectors[1:1 + len(jobs)]
    liked_vecs = vectors[1 + len(jobs):1 + len(jobs) + len(liked)] if use_taste else []
    rejected_vecs = vectors[1 + len(jobs) + len(liked):] if use_taste else []
    for job, vec in zip(jobs, job_vecs):
        cv_fit = max(0.0, _cosine(cv_vec, vec))
        job["fit_score"] = round(100 * cv_fit)
        if use_taste:
            like = _knn(vec, liked_vecs) if liked_vecs else 0.0
            dislike = _knn(vec, rejected_vecs) if rejected_vecs else 0.0
            closest_rejected = max((_cosine(vec, r) for r in rejected_vecs), default=0.0)
            closest_liked = max((_cosine(vec, l) for l in liked_vecs), default=0.0)
            job["taste_score"] = round(100 * (like - dislike), 1)
            job["_near_rejected"] = closest_rejected >= TASTE_NEAR_COPY and closest_rejected > closest_liked
            job["fit_score"] = round(100 * max(0.0, cv_fit + TASTE_WEIGHT * (like - dislike)))
        posted = job_facts(job)["posted_at"]
        age = _age_days(posted) if posted else None
        if age is not None and age > FRESH_DAYS:
            job["fit_score"] = max(0, job["fit_score"] - STALE_PENALTY)
    return sorted(jobs, key=lambda j: j["fit_score"], reverse=True)


def apply_fit_stage(jobs, user_profile, prefs, cv_text, openai_client=None, keep=FIT_POOL_KEEP,
                    blocked_companies=None, company_counts=None, company_cap=None, salary_floor=None, taste=None,
                    home_country=None):
    """Hard rules, then CV-fit ranking. Returns (kept_jobs, rejected_with_reasons)."""
    cand = candidate_profile(user_profile, prefs, home_country)
    cand.update({
        "blocked_companies": blocked_companies or set(),
        "company_counts": company_counts or {},
        "company_cap": company_cap or 99,
        "salary_floor": salary_floor,
    })
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
            kept = rank_by_cv_fit(openai_client, kept, cv_text, cand["titles"], taste=taste)
            near = [j for j in kept if j.pop("_near_rejected", False)]
            if near:
                kept = [j for j in kept if j not in near]
                rejected.extend((j, "too similar to a job they rejected") for j in near)
            if taste and any("taste_score" in j for j in kept + near):
                print(f"Fit stage: taste from {len(taste.get('liked') or [])} approved / {len(taste.get('rejected') or [])} rejected "
                      f"jobs; dropped {len(near)} near-copies of rejected jobs")
            dropped = kept[keep:]
            kept = kept[:keep]
            rejected.extend((j, f"low CV fit ({j['fit_score']})") for j in dropped)
            if kept:
                print(f"Fit stage: CV-fit ranked; keeping top {len(kept)} "
                      f"(fit {kept[0]['fit_score']}–{kept[-1]['fit_score']}), dropped {len(dropped)}")
                # the webapp's live card lists these while the review reads them
                for j in kept:
                    print(f"  shortlist: {j.get('title') or ''} — {j.get('company') or ''} | {j.get('location') or ''} | {j.get('job_url') or ''}")
        except Exception as e:  # ranking is an improvement, never a reason to lose the pool
            print(f"Fit stage: CV-fit ranking failed, keeping source order: {e}")
    return kept, rejected


def _top_reasons(rejected, n=6):
    counts: dict[str, int] = {}
    for _, reason in rejected:
        key = re.sub(r"\d+", "N", reason) if reason.startswith(("needs", "already")) else reason
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:n]
