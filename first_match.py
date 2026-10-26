"""
first_match.py — curate a new user's first matches before anything reaches them.

The signup run (api.py run_user_subscribed) runs send_jobbyo.py in dry-run +
first-match mode, so it gathers a wider pool and posts nothing. This module
then does what used to be done by hand for each new user:

  1. Pull the full posting from the ATS (the pipeline only sees ~1,100 chars,
     which is where "must live in Florida" or "80% on-site" get missed) and
     drop anything already closed.
  2. A second, strict review against the full text: location/residency rules,
     pay against what they earn now (stricter for $80k+ earners), seniority
     against their CV. Fallback picks don't survive this.
  3. Keep only the best (up to their daily limit), each with a one-line reason.
  4. Write the email copy from what actually happened: what was left out, why
     there are only X, and 1-2 settings changes that would find more.

Usage:
    python3 first_match.py --uid UID                 # test: curate latest run log, print the email, post/send nothing
    python3 first_match.py --uid UID --run-log PATH  # same, for a specific run log
    python3 first_match.py --uid UID --send [--send-to EMAIL]  # send a held first email from the saved report
"""

import html
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import send_jobbyo as sj

# The first batch decides whether a new user trusts us, so it gets the
# strongest model. If it fails or keeps nothing, first_match_v1 (the previous
# version, unchanged) runs the batch instead.
FIRST_MATCH_CLAUDE_MODEL = os.getenv("JOBBYO_FIRST_MATCH_CLAUDE_MODEL", "claude-opus-5-5")
MIN_JOBS_TO_EMAIL = 3
MIN_SCORE = 75
REVIEW_BATCH_SIZE = 5
FULL_TEXT_CHARS = 8000
# Above this, a role paying well under what they earn now does more harm in
# a first email than leaving it out.
HIGH_EARNER_SALARY = 80000
RUN_LOG_DIR = Path(__file__).resolve().parent / "run_logs"

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_CLOSED_MARKERS = (
    "no longer accepting", "job not found", "position has been filled",
    "this job is no longer", "job is no longer available", "posting has expired",
)


# ---------------------------------------------------------------------------
# Full posting text
# ---------------------------------------------------------------------------

def _clean(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", str(s or ""), flags=re.S | re.I)
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s))).split())


def _get(url, **kw):
    return requests.get(url, headers=_UA, timeout=20, **kw)


def _fetch_ats(url):
    """Returns (text, status) from the ATS's public API when we know it.
    status: "open", "closed", or None when this isn't a known ATS URL."""
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if m:
        board = _get(f"https://api.ashbyhq.com/posting-api/job-board/{m[1]}?includeCompensation=true").json()
        for j in board.get("jobs", []):
            if j.get("id") == m[2]:
                comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                head = (f"Location: {j.get('location')} | Workplace: {j.get('workplaceType')} | "
                        f"Remote: {j.get('isRemote')} | Pay: {comp}")
                return f"{head}\n{_clean(j.get('descriptionHtml'))}", "open"
        return "", "closed"

    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)", url)
    if m:
        r = _get(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}")
        if r.status_code == 404:
            return "", "closed"
        j = r.json()
        return f"Location: {(j.get('location') or {}).get('name')}\n{_clean(j.get('content'))}", "open"

    m = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url)
    if m:
        r = _get(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}")
        if r.status_code == 404:
            return "", "closed"
        j = r.json()
        cats = j.get("categories") or {}
        body = (j.get("descriptionPlain") or "") + " " + " ".join(
            f"{x.get('text')}: {_clean(x.get('content'))}" for x in j.get("lists") or [])
        return (f"Location: {cats.get('location')} | Workplace: {j.get('workplaceType')} | "
                f"Pay: {j.get('salaryRange') or ''}\n{_clean(body)} {_clean(j.get('additionalPlain'))}"), "open"

    m = re.search(r"https://([^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+?)(?:[?#]|$)", url)
    if m:
        tenant = m[1].split(".")[0]
        r = _get(f"https://{m[1]}/wday/cxs/{tenant}/{m[2]}/job/{m[3]}")
        if r.status_code in (404, 410):
            return "", "closed"
        j = (r.json() or {}).get("jobPostingInfo") or {}
        return f"Location: {j.get('location')}\n{_clean(j.get('jobDescription'))}", "open"

    m = re.search(r"https://([^.]+)\.recruitee\.com/o/([^/?#]+)", url)
    if m:
        r = _get(f"https://{m[1]}.recruitee.com/api/offers/{m[2]}")
        if r.status_code == 404:
            return "", "closed"
        j = r.json().get("offer") or {}
        return (f"Location: {j.get('location')} | Remote: {j.get('remote')}\n"
                f"{_clean(j.get('description'))} {_clean(j.get('requirements'))}"), "open"

    m = re.search(r"https://([^.]+)\.bamboohr\.com/careers/(\d+)", url)
    if m:
        r = _get(f"https://{m[1]}.bamboohr.com/careers/{m[2]}/detail")
        if r.status_code == 404:
            return "", "closed"
        j = (r.json().get("result") or {}).get("jobOpening") or {}
        if str(j.get("jobOpeningStatus") or "").lower() not in ("", "open"):
            return "", "closed"
        return (f"Location: {j.get('location')} | Location type: {j.get('locationType')} | "
                f"Pay: {j.get('compensation') or ''}\n{_clean(j.get('description'))}"), "open"

    return "", None


def _left_the_posting(asked, landed):
    def jobbish(u):
        return bool(re.search(r"job|career|position|opening|vacanc|posting|apply|\d{4,}", u, re.I))
    a, b = asked.split("#")[0].rstrip("/"), landed.split("#")[0].rstrip("/")
    return a != b and jobbish(a) and not jobbish(b)


def fetch_full_posting(job):
    """(text, status) for one job. status is "open", "closed" or "unknown";
    unknown keeps the pipeline's own description so a slow or odd ATS page
    never silently costs the user a good match."""
    url = job.get("job_url") or ""
    fallback = str(job.get("description") or "")
    try:
        text, status = _fetch_ats(url)
        if status == "closed":
            return "", "closed"
        if status == "open" and len(text) > 200:
            return text[:FULL_TEXT_CHARS], "open"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code in (404, 410):
            return "", "closed"
        # Redirected off the posting (e.g. a made-up careers URL landing on a
        # product page): no job there.
        if _left_the_posting(url, r.url):
            return "", "closed"
        page = _clean(r.text)
        if any(mk in page.lower() for mk in _CLOSED_MARKERS):
            return "", "closed"
        if len(page) > len(fallback) + 300:
            return page[:FULL_TEXT_CHARS], "open"
    except Exception:
        pass
    return fallback[:FULL_TEXT_CHARS], "unknown"


# ---------------------------------------------------------------------------
# Candidate facts
# ---------------------------------------------------------------------------

def _to_int(v):
    try:
        return int(float(re.sub(r"[^\d.]", "", str(v)))) if str(v or "").strip() else None
    except ValueError:
        return None


def candidate_facts(user_profile, automation):
    prefs = sj.extract_job_preferences(automation)
    settings = (automation or {}).get("settings") or {}
    cv = (user_profile or {}).get("cv") or {}
    loc = prefs.get("location") or {}
    min_salary = _to_int(prefs.get("minimumAcceptableSalary"))
    current = _to_int(prefs.get("currentSalary"))
    high_earner = max(min_salary or 0, current or 0) >= HIGH_EARNER_SALARY
    # For a high earner a first batch below what they make now reads as "this
    # product doesn't get me" -- hold pay to ~85% of current, not just the min.
    pay_floor = min_salary
    if high_earner and current:
        pay_floor = max(min_salary or 0, round(current * 0.85))
    experiences = [
        f"{e.get('title') or e.get('position')} at {e.get('company')} ({e.get('startYear')}-{e.get('endYear') or 'present'})"
        for e in (cv.get("experiences") or [])[:6]
    ]
    return {
        "name": ((user_profile or {}).get("displayName") or cv.get("first name") or cv.get("first_name")
                 or str((user_profile or {}).get("email") or "").split("@")[0].split(".")[0].title() or ""),
        "headline": cv.get("professional_title") or "",
        "summary": (cv.get("summary") or "")[:700],
        "experience": experiences,
        "target_titles": prefs.get("jobTitles") or [],
        "workplace_types": loc.get("type") or [],
        "places": loc.get("places") or [],
        "home": {k: (settings.get("address") or {}).get(k) for k in ("city", "state", "country")},
        "work_authorization": (settings.get("visaAndWork") or {}).get("citizenshipStatus"),
        "experience_level": prefs.get("experienceLevel") or [],
        "minimum_salary": min_salary,
        "current_salary": current,
        "salary_currency": ((prefs.get("salaryRange") or {}).get("currency")) or "USD",
        "pay_floor": pay_floor,
        "high_earner": high_earner,
        "daily_limit": _to_int(prefs.get("dailyLimit")) or 10,
        "excluded_companies": ((prefs.get("companyPreferences") or {}).get("excludedCompanies")) or [],
    }


# ---------------------------------------------------------------------------
# Strict second review
# ---------------------------------------------------------------------------

DROP_CATEGORIES = [
    "none", "location_or_residency", "onsite_or_hybrid_mismatch", "pay_below_level",
    "too_senior_or_people_manager", "too_junior", "wrong_function", "wrong_industry", "closed", "other",
]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "score": {"type": "integer"},
                    "drop_category": {"type": "string", "enum": DROP_CATEGORIES},
                    "drop_reason": {"type": "string"},
                    "company": {"type": "string"},
                    "workplace": {"type": "string"},
                    "pay": {"type": "string"},
                    "fit_line": {"type": "string"},
                    "angle": {"type": "string"},
                    "stretch": {"type": "boolean"},
                },
                "required": ["id", "keep", "score", "drop_category", "drop_reason",
                             "company", "workplace", "pay", "fit_line", "angle", "stretch"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


def _review_prompt(facts, batch):
    floor = facts["pay_floor"]
    pay_rule = (
        f"- Pay: if the posting lists pay and its TOP is below {floor:,} {facts['salary_currency']}/year, drop it "
        f"(pay_below_level). They earn {facts['current_salary'] or 'n/a'} now; a first match below their level "
        "costs more trust than it gains. No listed pay is fine."
        if floor else "- Pay: no salary floor set; don't drop on pay."
    )
    jobs_txt = "\n\n".join(
        f"### JOB id={j['_id']}\nTitle: {j.get('title')}\nCompany (as scraped, may be an aggregator): {j.get('company')}\n"
        f"Listed location: {j.get('location')}\nURL: {j.get('job_url')}\nFULL POSTING:\n{j['_full_text']}"
        for j in batch
    )
    return f"""You are a senior recruiter hand-picking a candidate's FIRST job matches. This first email
decides whether they trust the product, so be strict: keep only roles you would personally send them.
Read the FULL posting, not just the title.

CANDIDATE
{json.dumps(facts, indent=1)}

RULES (drop on any of these, pick the closest drop_category)
- Location/residency: drop if the posting requires living in a state/region/country the candidate isn't
  in (e.g. "must live in Florida", "East Coast only", "home-based in AR/LA/NM/OK/TX"), or needs work
  authorization they don't have. Their home: {facts['home']}.
- Workplace: they accept {facts['workplace_types'] or 'any'}. Drop on-site roles if they didn't accept
  on-site, and hybrid/on-site roles outside their places ({facts['places']}) unless the posting also
  offers remote for where they live. Heavy travel (50%+) or "on-site up to 80%" counts as on-site.
  If you can't tell whether they're eligible to work from where they live, drop it.
{pay_rule}
- Seniority: drop roles mainly about managing a team (Head of / VP / Director) unless their CV shows they
  have managed people at that level; drop roles clearly below their experience. For entry-level or
  junior candidates (students, recent graduates, under ~2 years), ALWAYS drop Senior / Staff / Lead /
  Principal titles and anything asking for 3+ years.
- Legitimacy: drop anything that doesn't look like a real job at a real employer (vague posting, no
  real company named, staffing-agency bait, "too good to be true" pay, or a page that isn't a job
  posting). Use drop_category "other" with drop_reason "doesn't look like a real job".
- Function/industry: drop roles whose day-to-day work isn't what they do or target.
- Shortlist test: would a recruiter at this company realistically shortlist this person from their CV?
  Shared title words are not enough (a childcare center Director is not a Director of Product
  Management; a CSM is not a Head of CS). If not, drop it (wrong_function or too_senior_or_people_manager).
- If the posting looks closed/expired, drop it (closed).

FOR EACH JOB return:
- keep, score 0-100 (how strongly you'd recommend it; 85+ = obvious fit), drop_category ("none" if kept),
  drop_reason (short, specific, e.g. "must live in Florida").
- company: the REAL employer (if scraped company is a job board/aggregator, use the employer named in the posting).
- workplace: 1-3 words, e.g. "Remote", "SF hybrid", "Denver on-site". No explanations.
- pay: as listed, compact, e.g. "$105k–150k"; "" if not listed.
- fit_line: for kept jobs, ONE short line (max 14 words) on why it fits THIS person, naming something
  concrete from their background, e.g. "Network security, your home turf from Lumen and AT&T."
  Plain, warm, no hype, no numbers that aren't in the posting or CV. About why the WORK fits them --
  never restate location, pay or eligibility (those are shown separately). "" for dropped jobs.
- angle: for kept jobs, what to LEAD WITH when applying, max 14 words, one concrete thing from their CV
  this posting cares about, e.g. "your $250M+ hyperscaler deal and Fortune 50 account ownership".
  "" for dropped jobs.
- stretch: true only for a kept job that is a real step up (clearly higher pay or scope) but still a
  realistic shortlist. Most kept jobs are false.

{jobs_txt}
"""


def _llm_json(prompt, schema, name):
    """Claude (FIRST_MATCH_CLAUDE_MODEL). Errors propagate: curate() then hands
    the batch to first_match_v1 so the user gets exactly the previous result."""
    resp = sj._anthropic_create(prompt, schema, False, model=FIRST_MATCH_CLAUDE_MODEL)
    return sj.parse_json_output(resp.output_text) or {}


def review_batch(facts, batch):
    out = _llm_json(_review_prompt(facts, batch), REVIEW_SCHEMA, "first_match_review")
    return {str(r.get("id")): r for r in out.get("jobs") or []}


# ---------------------------------------------------------------------------
# Pipeline rejections (for "what I left out" / "how to get more")
# ---------------------------------------------------------------------------

_PIPELINE_BUCKETS = [
    ("location", r"location|onsite|on-site|city|cities|based in|remote only|country|relocat|region"),
    ("salary", r"salary|pay\b|compensation"),
    ("seniority", r"yrs|years|senior|junior|seniority|entry|level|director|manager role"),
    ("function", r"function|title|not align|outside your|unrelated|diverges|different"),
]


def _pipeline_bucket(reason):
    reason = str(reason or "").lower()
    for name, pat in _PIPELINE_BUCKETS:
        if re.search(pat, reason):
            return name
    return None


def compact_pay(pay):
    """"$110,000–$150,000" / "$160k–$190k" -> "$110k–150k". Anything it can't
    parse (hourly, other currencies) is returned unchanged."""
    s = str(pay or "").strip()
    nums = re.findall(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?", s)
    if not nums or re.search(r"hour|/hr|month|€|£", s, re.I):
        return s
    vals = []
    for n, k in nums[:2]:
        v = float(n.replace(",", "")) * (1000 if k else 1)
        if v < 1000:
            return s
        vals.append(round(v / 1000))
    rng = f"${vals[0]}k" + (f"–{vals[1]}k" if len(vals) > 1 and vals[1] != vals[0] else "")
    return rng + (" + commission" if "commission" in s.lower() else "")


def more_matches_levers(facts, stats, dropped):
    """Concrete settings changes that would find more, from what actually
    cut the most. The copy model may only phrase these -- never invent its own."""
    levers = []
    types = {str(t).lower() for t in facts["workplace_types"]}
    home = facts["home"].get("city") or (facts["places"][0] if facts["places"] else "")
    drops = Counter(d.get("drop_category") for d in dropped)
    drops.update({"location_or_residency": stats["pipeline_drops"].get("location", 0)})
    if types == {"remote"}:
        levers.append(f"Add hybrid (or on-site) near {home}: remote-only is the narrowest setting, "
                      f"and location rules were the most common reason roles were dropped."
                      if home else "Add hybrid or on-site: remote-only is the narrowest setting.")
    elif len(facts["places"]) <= 2 and drops["location_or_residency"] + drops["onsite_or_hybrid_mismatch"] >= 3:
        levers.append(f"Add more cities near {home} to their places (they have {len(facts['places'])}).")
    if len(facts["target_titles"]) < 5:
        levers.append(f"Add a few more job titles: they have only {len(facts['target_titles'])}, "
                      "suggest 2 adjacent titles that fit their CV.")
    elif drops["wrong_function"] >= 3:
        levers.append("Add 2 adjacent titles that fit their CV and are more common in their market.")
    pay_cut = drops["pay_below_level"] + stats["pipeline_drops"].get("salary", 0)
    if facts["minimum_salary"] and pay_cut >= 4 and not facts["high_earner"]:
        levers.append(f"Their minimum salary ({facts['minimum_salary']:,}) cut {pay_cut} roles; "
                      "consider whether it reflects what they'd really accept.")
    if drops["too_senior_or_people_manager"] >= 3:
        levers.append("Add one level up or down in seniority if they'd consider it.")
    return levers


# ---------------------------------------------------------------------------
# Email copy
# ---------------------------------------------------------------------------

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "left_out": {"type": "string"},
        "why_only": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intro", "left_out", "why_only", "tips"],
    "additionalProperties": False,
}


def write_copy(facts, stats, kept, dropped):
    examples = [
        {"category": d["drop_category"], "reason": d["drop_reason"], "title": d.get("title")}
        for d in dropped[:14]
    ]
    show_more = len(kept) < facts["daily_limit"]
    levers = more_matches_levers(facts, stats, dropped)
    top_drop = Counter(d.get("drop_category") for d in dropped).most_common(1)
    prompt = f"""Write parts of a short first-matches email to a job seeker, in Jobbyo's voice: warm, plain,
direct, like a sharp recruiter friend. No hype, no exclamation marks, never say "auto-apply".

FACTS (use ONLY numbers that appear here; never invent statistics or market data)
- Open roles checked today: {stats['checked']}
- Kept: {len(kept)} (their daily limit is {facts['daily_limit']})
- Kept roles: {json.dumps([{'title': j['title'], 'company': j['company'], 'workplace': j['workplace']} for j in kept])}
- Dropped in the final hand-check, with reasons: {json.dumps(examples)}
- Earlier automatic filters dropped, by reason: {json.dumps(stats['pipeline_drops'])}
- Their settings: target titles ({len(facts['target_titles'])}): {facts['target_titles'][:20]}; workplace types: {facts['workplace_types']};
  places: {facts['places']}; home: {facts['home']}; minimum salary: {facts['minimum_salary']}; current salary: {facts['current_salary']}
- CV headline: {facts['headline']}
- Most common reason in the final hand-check: {top_drop[0][0] + f" ({top_drop[0][1]})" if top_drop else "none"}
- Settings changes that would find more (the ONLY tips you may give): {json.dumps(levers)}

WRITE
- intro: 1-2 sentences, like "I checked {stats['checked']} open roles today and kept {len(kept)}, all <what they have in
  common, e.g. 'enterprise customer success with remote or Bay Area hybrid setups'>. I opened every posting to
  confirm location rules and pay." Adapt the middle; keep it under 40 words.
- left_out: ONE sentence starting "What I left out:" naming the 2-3 most common concrete reasons from the
  hand-check, e.g. "a role that required living in Florida, one open to East Coast only, and two paying well
  below your level." Empty string if nothing was dropped.
- why_only: {"ONE sentence starting 'Why only " + str(len(kept)) + ":' naming the most common real drop reason above in plain words, tied to their settings where it fits (e.g. 'most roles that matched your experience were on-site, and you chose remote only.'). No market claims or numbers that aren't in the facts." if show_more else "empty string."}
- tips: {"Phrase 1-2 of the settings changes listed above (pick the strongest; none of your own). When a change is about titles, name 2 real titles that fit their CV (e.g. Technical Assistance Specialist, Training Manager). Start each with the change in bold markdown, then one short reason, e.g. '**Add hybrid near Dumfries.** It opens up center and program roles across Northern Virginia and DC.' Max 25 words each. Empty list if none are listed." if show_more and levers else "empty list."}
"""
    try:
        copy = _llm_json(prompt, COPY_SCHEMA, "first_match_copy")
    except Exception as e:
        print(f"first_match: copy generation failed ({e}) — using plain fallback")
        copy = {}
    copy.setdefault("intro", f"I checked {stats['checked']} open roles today and kept {len(kept)}. "
                             "I opened every posting to confirm location rules and pay.")
    copy.setdefault("left_out", "")
    copy.setdefault("why_only", "")
    copy.setdefault("tips", [])
    if not show_more:
        copy["why_only"], copy["tips"] = "", []
    if not levers:
        copy["tips"] = []
    copy["tips"] = [t for t in copy["tips"] if str(t).strip()][:2]
    return copy


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------

def _review_and_split(facts, open_jobs):
    """One Claude review pass over the open jobs -> (kept, dropped). Raises if
    any batch fails, so curate() can fall back to first_match_v1 as a whole."""
    reviews = {}
    for i in range(0, len(open_jobs), REVIEW_BATCH_SIZE):
        reviews.update(review_batch(facts, open_jobs[i:i + REVIEW_BATCH_SIZE]))
    kept, dropped = [], []
    for j in open_jobs:
        rv = reviews.get(j["_id"]) or {}
        merged = {**j, **{k: rv.get(k) for k in ("score", "drop_category", "drop_reason", "workplace", "pay", "fit_line", "angle", "stretch")}}
        if rv.get("company"):
            merged["company"] = rv["company"]
        if rv.get("keep") and (rv.get("score") or 0) >= MIN_SCORE and rv.get("fit_line"):
            kept.append(merged)
        else:
            dropped.append(merged)
    return kept, dropped


def estimate_cost(model, n_jobs):
    """Rough AI cost of one curation: review batches (~12.5k tokens in / 1.2k out
    each, mostly full postings) plus the email-copy call."""
    price = sj.LLM_PRICES.get(model) or sj.LLM_PRICES["gpt-4.1"]
    batches = -(-n_jobs // REVIEW_BATCH_SIZE) if n_jobs else 0
    tokens_in, tokens_out = batches * 12500 + 2000, batches * 1200 + 300
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1e6, 4)


def _previous_version(uid, run_entries, user_profile, automation, why, claude_jobs=0):
    """Exactly the previous first match (first_match_v1), untouched."""
    import first_match_v1
    print(f"first_match: {why} — using the previous version (first_match_v1)")
    report = first_match_v1.curate(uid, run_entries, user_profile, automation)
    report["method"] = f"previous version ({why})"
    report["estimated_cost_usd"] = round(
        estimate_cost(FIRST_MATCH_CLAUDE_MODEL, claude_jobs)
        + estimate_cost(first_match_v1.FIRST_MATCH_MODEL, report.get("stats", {}).get("candidates") or 0), 4)
    (RUN_LOG_DIR / f"first_match_{uid}.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def curate(uid, run_entries, user_profile=None, automation=None):
    """run_entries: this user's entries from the signup run's job_run_*.json
    (one per round). Returns a report dict; posts and sends nothing."""
    user_profile = user_profile or sj.get_user_profile(uid) or {}
    automation = automation or sj.get_user_automation(uid) or {}
    facts = candidate_facts(user_profile, automation)

    seen, candidates, metrics, pipeline_drops = set(), [], Counter(), Counter()
    for r in run_entries:
        for k, v in (r.get("round_metrics") or {}).items():
            if isinstance(v, (int, float)):
                metrics[k] += v
        for j in r.get("all_rejected_jobs") or []:
            b = _pipeline_bucket(j.get("review_reason"))
            if b:
                pipeline_drops[b] += 1
        for j in r.get("jobs_added") or []:
            key = sj.normalize_url(j.get("job_url", ""))
            if key and key not in seen:
                seen.add(key)
                candidates.append(dict(j))
    checked = int(sum(metrics.get(k, 0) for k in
                      ("hc_raw_results", "jobo_raw_results", "jfe_raw_results", "openai_jobs_returned")))
    stats = {"checked": max(checked, len(candidates)), "candidates": len(candidates),
             "pipeline_drops": dict(pipeline_drops)}
    print(f"first_match: {len(candidates)} candidates from {stats['checked']} checked")

    for i, j in enumerate(candidates):
        j["_id"] = str(i)
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(fetch_full_posting, candidates))
    open_jobs, dropped = [], []
    for j, (text, status) in zip(candidates, fetched):
        if status == "closed":
            dropped.append({**j, "drop_category": "closed", "drop_reason": "posting is closed"})
        else:
            j["_full_text"] = text
            open_jobs.append(j)

    # Claude first. If it fails or keeps nothing, the previous version runs
    # the whole batch instead, so the user gets exactly what they used to.
    method = f"claude ({FIRST_MATCH_CLAUDE_MODEL})"
    try:
        kept, reviewed_out = _review_and_split(facts, open_jobs)
    except Exception as e:
        return _previous_version(uid, run_entries, user_profile, automation, f"Claude failed: {str(e)[:80]}")
    if not kept:
        return _previous_version(uid, run_entries, user_profile, automation, "Claude kept 0", len(open_jobs))
    dropped += reviewed_out

    kept.sort(key=lambda j: j.get("score") or 0, reverse=True)
    # Safe bets first, then at most one clearly labelled stretch.
    stretches = [j for j in kept if j.get("stretch")]
    kept = [j for j in kept if not j.get("stretch")]
    kept = kept[: facts["daily_limit"] - (1 if stretches else 0)] + stretches[:1]
    for j in kept:
        j["pay"] = compact_pay(j.get("pay"))
        j["workplace"] = re.sub(r"\s*\(.*?\)", "", str(j.get("workplace") or "")).strip()
        j["grade"] = j.get("score") or j.get("grade")
        j["review_reason"] = j.get("fit_line") or j.get("review_reason")
    copy = write_copy(facts, stats, kept, dropped)

    clean = lambda j: {k: v for k, v in j.items() if not k.startswith("_")}
    report = {
        "uid": uid,
        "email": user_profile.get("email"),
        "facts": facts,
        "stats": stats,
        "method": method,
        "estimated_cost_usd": estimate_cost(FIRST_MATCH_CLAUDE_MODEL, len(open_jobs)),
        "kept": [clean(j) for j in kept],
        "dropped": [{k: j.get(k) for k in ("title", "company", "job_url", "score", "drop_category", "drop_reason")} for j in dropped],
        "copy": copy,
    }
    RUN_LOG_DIR.mkdir(exist_ok=True)
    (RUN_LOG_DIR / f"first_match_{uid}.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"first_match: [{method}] kept {len(kept)}, dropped {len(dropped)} "
          f"({dict(Counter(d.get('drop_category') for d in dropped))})")
    return report


# ---------------------------------------------------------------------------
# Email body
# ---------------------------------------------------------------------------

def _md_bold(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(str(s)))


def render_body_html(report, first_name):
    p = 'style="margin:0 0 14px 0;"'
    copy = report["copy"]
    parts = [f"<p {p}>Hi {html.escape(first_name)},</p>", f"<p {p}>{html.escape(copy['intro'])}</p>"]
    items = ""
    for j in report["kept"]:
        meta = " · ".join(x for x in (j.get("workplace"), j.get("pay")) if x)
        title = html.escape(j.get("title") or "")
        url = j.get("job_url") or ""
        title_html = f'<a href="{html.escape(url)}" style="color:#3A56E2;">{title}</a>' if url else title
        stretch = ' <em style="color:#6b7280;">(a stretch)</em>' if j.get("stretch") else ""
        angle = (f'<br><span style="color:#4b5563;">Lead with {html.escape(j["angle"])}.</span>'
                 if j.get("angle") else "")
        items += (f'<li style="margin-bottom:10px;"><strong>{html.escape(j.get("company") or "")}</strong>, '
                  f'{title_html}{" · " + html.escape(meta) if meta else ""}{stretch}. '
                  f'{html.escape(j.get("fit_line") or "")}{angle}</li>')
    parts.append(f'<ul style="margin:0 0 14px 0;padding-left:20px;">{items}</ul>')
    if copy.get("left_out"):
        parts.append(f"<p {p}>{_md_bold(copy['left_out'])}</p>")
    if copy.get("why_only"):
        parts.append(f"<p {p}>{_md_bold(copy['why_only'])}</p>")
    if copy.get("tips"):
        tips = "".join(f'<li style="margin-bottom:6px;">{_md_bold(t)}</li>' for t in copy["tips"])
        parts.append(f'<p style="margin:0 0 6px 0;"><strong>To get more matches:</strong></p>'
                     f'<ul style="margin:0 0 14px 0;padding-left:20px;">{tips}</ul>')
    return "\n".join(parts)


def render_body_text(report, first_name):
    """Plain-text preview of the same body, for testing and Slack."""
    c = report["copy"]
    lines = [f"Hi {first_name},", "", c["intro"], ""]
    for j in report["kept"]:
        meta = " · ".join(x for x in (j.get("workplace"), j.get("pay")) if x)
        stretch = " (a stretch)" if j.get("stretch") else ""
        lines.append(f"- {j.get('company')}, {j.get('title')}{' · ' + meta if meta else ''}{stretch}. {j.get('fit_line')}")
        if j.get("angle"):
            lines.append(f"    Lead with {j['angle']}.")
    for key in ("left_out", "why_only"):
        if c.get(key):
            lines += ["", c[key]]
    if c.get("tips"):
        lines += ["", "To get more matches:"] + [f"- {t}" for t in c["tips"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Posting + sending
# ---------------------------------------------------------------------------

_QUEUE_FIELDS = ("job_url", "title", "description", "company", "grade", "status", "review_decision",
                 "match_tier", "review_confidence", "review_reason", "location", "source")


def post_kept(report, job_status):
    """Write the kept jobs to the user's queue (the signup run posted nothing)."""
    jobs = [{**{k: j.get(k) for k in _QUEUE_FIELDS}, "status": job_status} for j in report["kept"]]
    if not jobs:
        return None
    return sj.api_post_jobs(report["email"], jobs, default_status=job_status, uid=report["uid"])


def send_first_match_email(report, recipient=None):
    """The welcome email with the curated body, plus the strategy PDF."""
    if str(report.get("method", "")).startswith("previous version"):
        import first_match_v1
        return first_match_v1.send_first_match_email(report, recipient)
    import outreach
    context = outreach.build_context(report["uid"])
    context["sample_jobs"] = report["kept"]
    try:
        pdf_bytes = outreach.generate_strategy_pdf(context)
    except Exception as e:
        print(f"first_match: PDF generation failed (sends without it): {e}")
        pdf_bytes = None
    first = (report["facts"].get("name") or context["first_name"] or "there").split()[0]
    context["first_name"] = first
    n = len(report["kept"])
    return bool(outreach.send_paid_welcome_email(
        context, "", pdf_bytes, recipient or report["email"],
        subject=f"{first}, your first {n} {'match' if n == 1 else 'matches'}",
        body_html=render_body_html(report, first),
    ))


def main():
    args = sys.argv[1:]
    if "--send" in args and "--uid" in args:
        # Send a held first email (fewer than MIN_JOBS_TO_EMAIL at signup) once
        # the team has topped it up -- re-curates nothing, uses the saved report.
        uid = args[args.index("--uid") + 1]
        report = json.loads((RUN_LOG_DIR / f"first_match_{uid}.json").read_text())
        to = args[args.index("--send-to") + 1] if "--send-to" in args else None
        print("sent" if send_first_match_email(report, to) else "send failed")
        return
    if "--uid" not in args:
        print(__doc__)
        sys.exit(1)
    uid = args[args.index("--uid") + 1]
    if "--run-log" in args:
        path = Path(args[args.index("--run-log") + 1])
    else:
        path = None
        for f in sorted(RUN_LOG_DIR.glob("job_run_*.json"), reverse=True):
            if uid in f.read_text(encoding="utf-8", errors="ignore"):
                path = f
                break
    if not path:
        print(f"No run log found for {uid}")
        sys.exit(1)
    entries = [r for r in json.loads(path.read_text()) if r.get("uid") == uid]
    report = curate(uid, entries)
    first = (report["facts"]["name"] or "there").split()[0]
    print("\n" + "=" * 60 + f"\nEMAIL PREVIEW ({path.name})\n" + "=" * 60)
    print(render_body_text(report, first))


if __name__ == "__main__":
    main()
