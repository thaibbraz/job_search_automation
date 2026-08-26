"""outreach.py — personalized, conversational follow-up to a candidate who
opened their subscribed-search email, plus two related sends:
  - send_prospect_outreach_email: a hot lead who hasn't subscribed yet.
  - send_paid_welcome_email: someone who just subscribed, alongside their
    real matches -- same personal-read format as the hot-lead nudge, minus
    any trial/pricing pitch since they're already a customer.

Pulls the candidate's own stated preferences + a few of their actual matched
jobs, has the LLM write a short first-person note in Jobbyo's voice (not
"an AI", no sales language), and sends it via Brevo with thiago@jobbyo.ai
CC'd. This is the message-generation + send piece only — the "who opened
today" check and lead-scoring tiers are a separate scheduled step to be
added once this is confirmed good.

Usage:
    python3 outreach.py --uid <uid>
    python3 outreach.py --uid <uid> --paid-welcome
    python3 outreach.py --email <email>
"""

import json
import sys

import requests

import send_jobbyo
import approve_jobs

SENDER_NAME = approve_jobs.SENDER_NAME
SENDER_EMAIL = approve_jobs.SENDER_EMAIL
BREVO_API_KEY = approve_jobs.BREVO_API_KEY
BREVO_API_URL = approve_jobs.BREVO_API_URL

TEAM_CC_EMAIL = "thiago@jobbyo.ai"
# Prospect nudges (send_prospect_outreach_email) CC both, but a reply goes
# straight to Adnan -- he owns retention conversations, not Thiago.
RETENTION_CC_EMAILS = ["thiago@jobbyo.ai", "adnan@jobbyo.ai"]
RETENTION_REPLY_TO_EMAIL = "adnan@jobbyo.ai"

# Junk/placeholder entries seen in test data (a URL instead of a real job
# title) -- filtered out so they never leak into a real message.
def _looks_like_real_job(job):
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    return bool(title) and bool(company) and not title.startswith("http")


PROMPT_TEMPLATE = """You are writing a short, warm, first-person follow-up email as "Jobbyo" -- \
a job search service, not a company sending a marketing blast. The candidate \
already opened an earlier email from you listing job matches; this is a \
personal, casual check-in, not a sales pitch.

The goal of THIS email specifically is to give them a different perspective \
on their own search than a plain job list -- you have a real coaching \
analysis of this candidate below (their strengths, honest blind spots, a \
market reality check, one recommendation, and alternative paths). Draw from \
THAT actual analysis rather than inventing your own take -- pick the one or \
two pieces from it that would land hardest for this specific person, and \
put them in your own conversational words. This should feel like real, \
specific insight into their search, not generic career advice.

STRICT RULES:
- Never say "AI", "artificial intelligence", "bot", "automated", or anything \
that frames you as software. Write like a person who has been working on \
their search.
- Never use an em dash (—) anywhere. Use a period, comma, or "and" instead.
- Do not invent your own statistics or market claims -- the market reality \
check below is already grounded; paraphrase it, don't add invented numbers \
on top of it.
- Reference their actual stated preferences and 1-2 of the actual jobs found \
for them (given below) so it's obviously personal, not generic.
- If there's a real blind spot below (something honestly working against \
them), it is more useful to raise it gently than to avoid it -- a real coach \
would rather they hear it from you now than get silence from employers \
later. Don't be harsh, but don't soften it into nothing either.
- Pull ONE alternative path from the notes below (not invented fresh) and \
say briefly why it fits them specifically.
- Do NOT offer to hop on a call, schedule time, or anything implying you \
personally can meet with them -- your job is finding jobs and strategy, \
not scheduling. Do not mention calls, meetings, or availability at all.
- End with ONE short, specific question that invites a reply (not "let me \
know if you have questions" -- something concrete about their search).
- Do NOT write a sign-off, closing line, or signature ("Jobbyo", "Best,", \
etc.) -- the email ends right after your question. That part is added \
separately, after your text.
- Plain, short paragraphs. Under 160 words total. No bullet points, no bold, \
no emoji, no subject line -- just the body text.

CANDIDATE:
First name: {first_name}
Target job title(s): {job_titles}
Minimum acceptable salary: {min_salary}
Location preference: {location}

A FEW JOBS ALREADY FOUND FOR THEM:
{job_list}

HOW WE'RE READING THEIR PROFILE (persona notes):
{persona_summary}

Write the email body now."""


COACH_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "strengths": {"type": "array", "items": {"type": "string"}},
        "blind_spots": {"type": "array", "items": {"type": "string"}},
        "market_reality_check": {"type": "string"},
        "coach_recommendation": {"type": "string"},
        "positioning_pitch": {"type": "string"},
        "alternative_paths": {"type": "array", "items": {"type": "string"}},
        "recommended_titles": {"type": "array", "items": {"type": "string"}},
        "roles_to_avoid": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "strengths", "blind_spots", "market_reality_check",
        "coach_recommendation", "positioning_pitch", "alternative_paths",
        "recommended_titles", "roles_to_avoid",
    ],
    "additionalProperties": False,
}

COACH_ANALYSIS_PROMPT = """You are an experienced career coach reviewing this candidate's resume and \
stated job search preferences. You have seen thousands of resumes and know \
what actually gets someone hired versus what just sounds good on paper. Form \
a real, specific opinion about THIS person, not generic career advice that \
could apply to anyone.

CANDIDATE PROFILE:
{profile_summary}

CV:
{cv_text}

Write:
- strengths: 3-5 specific, evidence-based strengths -- reference what's \
actually in the CV (a project, a result, a progression), not generic traits.
- blind_spots: 2-4 honest gaps or risks in how they're approaching their \
search or how they'll read to employers. Be direct. If a stated preference \
(salary, location, title) looks unrealistic, say so plainly.
- market_reality_check: one honest paragraph on how competitive their \
actual target is right now, given their real experience level and stated \
constraints, not encouragement for its own sake.
- coach_recommendation: the single highest-leverage piece of advice you'd \
give this person right now if you only got one sentence. Specific to them.
- positioning_pitch: a sharper one-to-two sentence way for them to describe \
themselves than however they currently frame it.
- alternative_paths: 1-3 adjacent roles or directions worth considering \
that they likely haven't thought of, each with a short reason grounded in \
something specific from their actual background.
- recommended_titles: 2-4 job title variants worth adding to their search \
beyond what they already listed -- titles that describe the same real work \
but that they may not be searching for, so real openings don't get missed. \
Grounded in their actual level and background, not aspirational titles \
above their reach.
- roles_to_avoid: 1-3 title or role types NOT worth their time right now, \
each as one short phrase with a brief reason (e.g. "Director-level roles, \
not yet backed by people-management experience")."""


def generate_coach_analysis(profile, prefs):
    """Standalone coaching analysis, independent of the job-matching
    persona. Never written to personas/{uid}.json -- that file also drives
    the live search pipeline's filtering, and this is being generated for a
    one-off content-quality check, not to change what jobs someone's actual
    account searches for."""
    cv_text = send_jobbyo.cv_to_text(profile)
    profile_summary = json.dumps({
        "displayName": profile.get("displayName"),
        "target_titles": prefs.get("jobTitles"),
        "minimum_salary": prefs.get("minimumAcceptableSalary"),
        "location": prefs.get("location"),
    }, indent=2)
    prompt = COACH_ANALYSIS_PROMPT.format(profile_summary=profile_summary, cv_text=cv_text)
    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=prompt,
        text={"format": {"type": "json_schema", "name": "coach_analysis", "schema": COACH_ANALYSIS_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


MARKET_OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "market_size": {"type": "string"},
        "competitive_landscape": {"type": "string"},
        "compensation_range": {"type": "string"},
        "sourcing_takeaway": {"type": "string"},
    },
    "required": ["market_size", "competitive_landscape", "compensation_range", "sourcing_takeaway"],
    "additionalProperties": False,
}

# A condensed, candidate-facing cut of a market-mapping report -- same
# underlying idea (how big is this pool, who else is competing for it, what
# does it pay, what does that imply for the search) but four short labeled
# lines instead of a multi-section recruiter deliverable, and framed as
# demand-for-this-candidate rather than supply-of-candidates-for-a-role.
MARKET_OVERVIEW_PROMPT = """You are a research analyst producing a short, honest market snapshot for a job \
seeker, not a recruiter. Use web search to ground this in real, current \
information -- current job listing counts, real companies actively hiring, \
real compensation data. Where you can't find solid current data, say so and \
give a broad, directional range instead of a falsely precise number.

CANDIDATE:
Target title(s): {job_titles}
Recommended title variants to also search: {recommended_titles}
Location preference: {location}
Minimum acceptable salary: {min_salary}
Current level, from their background: {level_summary}

Write exactly four short items, each 1-2 sentences, plain language, no \
jargon, no bullet sub-points:
- market_size: roughly how many current openings/postings exist for this \
role and level in this location right now. Cite the rough count and where \
it's from (e.g. "~740 listed on Glassdoor, Aug 2026"). Label it clearly as \
Observed, Estimated, or Inferred.
- competitive_landscape: name 3-5 real companies actively hiring for this \
kind of role in this market right now, and one honest line on how tight or \
loose the talent pool looks (e.g. a couple of employers known for strong \
retention that make outside moves harder to land).
- compensation_range: a realistic total-comp range for this level and \
location, from real current sources. If your sources are thin or generic, \
say the range is broad and directional rather than precise.
- sourcing_takeaway: one concrete sentence on what this means for THEIR \
search specifically -- where to focus first, given the market size and \
competition above.

Write the four items now."""


def generate_market_overview(profile, prefs, coach_analysis):
    """Candidate-facing market snapshot, grounded via live web search (same
    tools=[{"type": "web_search"}] pattern send_jobbyo.py already uses for
    job-URL resolution, with the same web_search -> web_search_preview
    fallback). A separate call from generate_coach_analysis so a slow/failed
    search doesn't take the rest of the persona content down with it."""
    prefs = prefs or {}
    cv_text = send_jobbyo.cv_to_text(profile)
    level_summary = (cv_text[:400] + "...") if len(cv_text) > 400 else cv_text

    prompt = MARKET_OVERVIEW_PROMPT.format(
        job_titles=", ".join(prefs.get("jobTitles") or []) or "not specified",
        recommended_titles=", ".join(coach_analysis.get("recommended_titles") or []) or "none",
        location=", ".join((prefs.get("location") or {}).get("places") or []) or "not specified",
        min_salary=prefs.get("minimumAcceptableSalary") or "not specified",
        level_summary=level_summary,
    )

    kwargs = dict(
        model=send_jobbyo.SEARCH_MODEL,
        input=prompt,
        text={"format": {"type": "json_schema", "name": "market_overview", "schema": MARKET_OVERVIEW_SCHEMA, "strict": True}},
    )
    try:
        response = send_jobbyo.responses_create(
            tools=[{"type": "web_search", "search_context_size": "medium"}], **kwargs
        )
    except Exception as e:
        if "web_search" not in str(e):
            raise
        response = send_jobbyo.responses_create(
            tools=[{"type": "web_search_preview", "search_context_size": "medium"}], **kwargs
        )
    return json.loads(response.output_text)


def _load_persona(uid, profile=None, prefs=None):
    """Pull the already-generated persona file (career framing, target
    titles, roles being intentionally avoided) -- used for both the email
    prompt and the attached PDF. If it predates the coaching-analysis
    fields (or doesn't exist), those are generated fresh in memory via
    generate_coach_analysis and merged in WITHOUT touching the file on
    disk, so a real customer's live search config is never altered by
    running this."""
    persona = {}
    try:
        path = send_jobbyo.persona_path(uid)
        if path.exists():
            persona = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        persona = {}

    if not persona.get("coach_recommendation") and profile is not None:
        try:
            analysis = generate_coach_analysis(profile, prefs or {})
            persona = {**persona, **analysis}
            if not persona.get("career_hybrid"):
                persona["career_hybrid"] = persona.get("positioning_pitch", "")
        except Exception as e:
            print(f"Coach analysis generation failed (non-fatal): {e}")

    if not persona.get("market_overview") and profile is not None and persona.get("recommended_titles") is not None:
        try:
            persona["market_overview"] = generate_market_overview(profile, prefs or {}, persona)
        except Exception as e:
            print(f"Market overview generation failed (non-fatal): {e}")

    return persona


def _persona_summary(persona):
    lines = []
    if persona.get("career_hybrid"):
        lines.append(f"Career framing: {persona['career_hybrid']}")
    target_titles = list(persona.get("target_titles") or []) + list(persona.get("recommended_titles") or [])
    if target_titles:
        lines.append(f"Target titles (incl. ones worth adding): {', '.join(target_titles)}")
    avoid = list(persona.get("avoid_roles") or []) + list(persona.get("roles_to_avoid") or [])
    if avoid:
        lines.append(f"Roles to avoid right now: {', '.join(avoid)}")
    if persona.get("strengths"):
        lines.append("Real strengths:\n" + "\n".join(f"  - {s}" for s in persona["strengths"]))
    if persona.get("blind_spots"):
        lines.append("Honest blind spots:\n" + "\n".join(f"  - {b}" for b in persona["blind_spots"]))
    if persona.get("market_reality_check"):
        lines.append(f"Market reality check: {persona['market_reality_check']}")
    market = persona.get("market_overview") or {}
    if market:
        lines.append(
            "Market snapshot (web-search grounded):\n"
            f"  - Market size: {market.get('market_size', '')}\n"
            f"  - Who else is hiring: {market.get('competitive_landscape', '')}\n"
            f"  - Compensation: {market.get('compensation_range', '')}\n"
            f"  - What this means for them: {market.get('sourcing_takeaway', '')}"
        )
    if persona.get("coach_recommendation"):
        lines.append(f"Coach's one recommendation: {persona['coach_recommendation']}")
    if persona.get("alternative_paths"):
        lines.append("Alternative paths worth considering:\n" + "\n".join(f"  - {a}" for a in persona["alternative_paths"]))
    return "\n\n".join(lines) or "(no persona notes on file yet)"


def build_context(uid):
    profile = send_jobbyo.get_user_profile(uid) or {}
    automation = send_jobbyo.get_user_automation(uid) or {}
    prefs = (automation.get("settings") or {}).get("jobPreferences") or {}

    name = profile.get("displayName") or (profile.get("email") or "").split("@")[0]
    first_name = name.split()[0] if name else "there"

    real_jobs = [j for j in (automation.get("selectedJobs") or []) if _looks_like_real_job(j)]
    real_jobs.sort(key=lambda j: j.get("addedAt") or "", reverse=True)
    sample_jobs = real_jobs[:3]
    job_list = "\n".join(f"- {j.get('title')} at {j.get('company')}" for j in sample_jobs) or "- (none found yet)"

    persona = _load_persona(uid, profile=profile, prefs=prefs)

    return {
        "profile": profile,
        "first_name": first_name,
        "job_titles": ", ".join(prefs.get("jobTitles") or []) or "not specified",
        "min_salary": prefs.get("minimumAcceptableSalary") or "not specified",
        "location": ", ".join((prefs.get("location") or {}).get("places") or []) or "not specified",
        "job_list": job_list,
        "automation_jobs": automation.get("selectedJobs") or [],
        "persona": persona,
        "persona_summary": _persona_summary(persona),
    }


PROSPECT_PROMPT_TEMPLATE = """You are writing a short, casual, first-person email as "Jobbyo" -- a job \
search service, not a company sending a marketing blast. This person \
signed up and set up their search preferences, but hasn't started their \
free trial yet. This is your first real message to them, not a follow-up.

The goal of THIS email is to show them you've already done real work on \
their behalf, purely from the profile they gave you: a real read on their \
background, one alternative angle worth considering, and an honest sense of \
what the market looks like for them right now. You have real research below \
(strengths, honest blind spots, a market snapshot grounded in current data, \
one recommendation, and alternative paths). Draw from THAT actual research \
rather than inventing your own take -- pick the pieces that would land \
hardest for this specific person, and put them in your own words, casually. \
This should feel like a genuinely sharp, personal read, not a report.

After your text, the email continues with a short preview of real job \
openings and then a link to start the trial -- you don't need to write \
that part, it's added separately. Just end your text with ONE short, \
casual line that naturally leads into "here's a taste of what's out there" \
(you don't have the actual job list, so don't name specific companies or \
roles in this transition line, just gesture at it).

STRICT RULES:
- Write like you're dashing off a quick, genuine note to someone, not \
writing a professional memo. Contractions, everyday words, short \
sentences. Skip corporate/consultant phrasing entirely -- no "leadership \
scope", "P&L", "positioning", "market reality" as phrases; say the same \
thing the way you'd actually say it out loud to a friend.
- Never say "AI", "artificial intelligence", "bot", "automated", or anything \
that frames you as software. Write like a person who has been working on \
their search.
- Never use an em dash (—) anywhere. Use a period, comma, or "and" instead.
- Do not invent your own statistics or market claims -- the market snapshot \
below is already grounded in real research; paraphrase it loosely and \
casually (a rough number or company name is great, don't invent new ones \
on top of it).
- Reference their actual stated preferences (given below) so it's obviously \
personal, not generic.
- If there's a real blind spot below (something honestly working against \
them), it is more useful to raise it gently than to avoid it -- don't be \
harsh, but don't soften it into nothing either. Say it plainly, not \
diplomatically.
- Pull ONE alternative path from the notes below (not invented fresh) and \
say briefly why it fits them specifically.
- Work in ONE concrete detail from the market snapshot below (a rough \
number of openings, a company actively hiring, or the comp range) so it \
reads as real research, not a vague gesture at "the market."
- Do NOT offer to hop on a call, schedule time, or anything implying you \
personally can meet with them.
- Do NOT write a sign-off, closing line, or signature ("Jobbyo", "Best,", \
etc.) -- the email ends right after your transition line. That part is \
added separately.
- Plain, short paragraphs. Under 170 words total. No bullet points, no bold, \
no emoji, no subject line -- just the body text.

CANDIDATE:
First name: {first_name}
Target job title(s): {job_titles}
Minimum acceptable salary: {min_salary}
Location preference: {location}

HOW WE'RE READING THEIR PROFILE (persona notes):
{persona_summary}

Write the email body now."""


def build_prospect_context(uid):
    """Same shape as build_context, for someone who hasn't started their
    trial yet. sample_jobs is usually empty here -- their search hasn't run
    yet unless the daily nudge job that calls this also runs a one-time
    preview search first (not built yet; see the 24h-nudge job). Reads
    automation.selectedJobs the same way build_context does so this picks
    up real matches for free the moment that piece exists, no code change
    needed here."""
    profile = send_jobbyo.get_user_profile(uid) or {}
    automation = send_jobbyo.get_user_automation(uid) or {}
    prefs = (automation.get("settings") or {}).get("jobPreferences") or {}

    name = profile.get("displayName") or (profile.get("email") or "").split("@")[0]
    first_name = name.split()[0] if name else "there"

    real_jobs = [j for j in (automation.get("selectedJobs") or []) if _looks_like_real_job(j)]
    real_jobs.sort(key=lambda j: j.get("addedAt") or "", reverse=True)
    sample_jobs = real_jobs[:3]

    persona = _load_persona(uid, profile=profile, prefs=prefs)

    return {
        "profile": profile,
        "first_name": first_name,
        "job_titles": ", ".join(prefs.get("jobTitles") or []) or "not specified",
        "min_salary": prefs.get("minimumAcceptableSalary") or "not specified",
        "location": ", ".join((prefs.get("location") or {}).get("places") or []) or "not specified",
        "sample_jobs": sample_jobs,
        "persona": persona,
        "persona_summary": _persona_summary(persona),
    }


def generate_prospect_message(context):
    """Returns just the personal-read + transition text -- no sign-off, no
    CTA. send_prospect_outreach_email appends the job-preview cards, the
    "full breakdown is in the PDF" line, the trial CTA, and the sign-off as
    fixed HTML, so those stay consistent and are never left to the model."""
    prompt = PROSPECT_PROMPT_TEMPLATE.format(
        first_name=context["first_name"],
        job_titles=context["job_titles"],
        min_salary=context["min_salary"],
        location=context["location"],
        persona_summary=context["persona_summary"],
    )
    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=prompt,
    )
    text = (response.output_text or "").strip()
    text = text.replace("—", ",")
    return text


def generate_message(context):
    prompt = PROMPT_TEMPLATE.format(
        first_name=context["first_name"],
        job_titles=context["job_titles"],
        min_salary=context["min_salary"],
        location=context["location"],
        job_list=context["job_list"],
        persona_summary=context["persona_summary"],
    )
    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=prompt,
    )
    text = (response.output_text or "").strip()
    # Safety net on top of the prompt instruction -- em dashes read as AI-
    # generated, so strip them regardless of whether the model complied.
    text = text.replace("—", ",")

    # Fixed, not LLM-generated -- this is a factual product detail (MAX plan
    # includes periodic human 1:1s), so it's worded the same way every time
    # rather than left to the model's phrasing.
    text += (
        "\n\nIf you'd rather talk this through with a real person, that's "
        "available with periodic 1:1s on the MAX plan.\n\nJobbyo"
    )
    return text


PAID_WELCOME_PROMPT_TEMPLATE = """You are writing a short, casual, first-person email as "Jobbyo" -- a job \
search service, not a company sending a marketing blast. This person just \
subscribed and their first search already ran. This is a personal note \
that goes alongside their real matches, not a sales message -- they're \
already a customer, so there is nothing to sell them here.

The goal of THIS email is to show them you already understand their \
background: a real read on their profile, one alternative angle worth \
considering, and an honest sense of what the market looks like for them \
right now. You have real research below (strengths, honest blind spots, a \
market snapshot grounded in current data, one recommendation, and \
alternative paths). Draw from THAT actual research rather than inventing \
your own take -- pick the pieces that would land hardest for this specific \
person, and put them in your own words, casually. This should feel like a \
genuinely sharp, personal read, not a report.

After your text, the email continues with their actual job matches and \
then a pointer to a more detailed PDF -- you don't need to write that \
part, it's added separately. Just end your text with ONE short, casual \
line that naturally leads into "here's what I found for you" (you don't \
have the actual job list, so don't name specific companies or roles in \
this transition line, just gesture at it).

STRICT RULES:
- Write like you're dashing off a quick, genuine note to someone, not \
writing a professional memo. Contractions, everyday words, short \
sentences. Skip corporate/consultant phrasing entirely -- no "leadership \
scope", "P&L", "positioning", "market reality" as phrases; say the same \
thing the way you'd actually say it out loud to a friend.
- Never say "AI", "artificial intelligence", "bot", "automated", or anything \
that frames you as software. Write like a person who has been working on \
their search.
- Never use an em dash (—) anywhere. Use a period, comma, or "and" instead.
- Do not invent your own statistics or market claims -- the market snapshot \
below is already grounded in real research; paraphrase it loosely and \
casually (a rough number or company name is great, don't invent new ones \
on top of it).
- Reference their actual stated preferences (given below) so it's obviously \
personal, not generic.
- If there's a real blind spot below (something honestly working against \
them), it is more useful to raise it gently than to avoid it -- don't be \
harsh, but don't soften it into nothing either. Say it plainly, not \
diplomatically.
- Pull ONE alternative path from the notes below (not invented fresh) and \
say briefly why it fits them specifically.
- Work in ONE concrete detail from the market snapshot below (a rough \
number of openings, a company actively hiring, or the comp range) so it \
reads as real research, not a vague gesture at "the market."
- Do NOT mention starting a trial, subscribing, upgrading, or pricing in \
any way -- they are already a paying customer, so anything sales-shaped \
here reads as tone-deaf.
- Do NOT offer to hop on a call, schedule time, or anything implying you \
personally can meet with them.
- Do NOT write a sign-off, closing line, or signature ("Jobbyo", "Best,", \
etc.) -- the email ends right after your transition line. That part is \
added separately.
- Plain, short paragraphs. Under 170 words total. No bullet points, no bold, \
no emoji, no subject line -- just the body text.

CANDIDATE:
First name: {first_name}
Target job title(s): {job_titles}
Minimum acceptable salary: {min_salary}
Location preference: {location}

HOW WE'RE READING THEIR PROFILE (persona notes):
{persona_summary}

Write the email body now."""


def generate_paid_welcome_message(context):
    """Same idea as generate_prospect_message, for someone who already
    subscribed -- build_context(uid) already has their real job matches, so
    there's no "taste of what's out there" framing and no trial pitch, just
    the personal read plus a transition into the real matches appended by
    send_paid_welcome_email."""
    prompt = PAID_WELCOME_PROMPT_TEMPLATE.format(
        first_name=context["first_name"],
        job_titles=context["job_titles"],
        min_salary=context["min_salary"],
        location=context["location"],
        persona_summary=context["persona_summary"],
    )
    response = send_jobbyo.responses_create(
        model=send_jobbyo.SEARCH_MODEL,
        input=prompt,
    )
    text = (response.output_text or "").strip()
    text = text.replace("—", ",")
    return text


import html as _html
import re as _re


def _esc(text):
    return _html.escape(str(text or ""))


_CITATION_RE = _re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)")


def _strip_citations(text):
    """market_overview is grounded via web_search, and the model sometimes
    inlines markdown-style source links (e.g. "([glassdoor.com](url))") --
    fine in a chat reply, but this isn't a markdown renderer, so left alone
    it would show up as literal bracket/paren text in the PDF. Strip it;
    the grounding still shows up as the specific numbers/names in the text
    itself, just without the raw link syntax."""
    return _CITATION_RE.sub("", str(text or ""))


PDF_CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    color: #1a1b20;
    font-size: 11pt;
    line-height: 1.7;
}
/* Deliberately plain -- one accent color (brand blue), used only for the
   header band and section labels. No rules, no pills, no boxes. Reads like
   a short personal letter, not a report. */
.header {
    background: #3A56E2;
    color: #ffffff;
    padding: 30px 46px 24px 46px;
}
.header .eyebrow {
    font-size: 8.5pt;
    letter-spacing: 2px;
    text-transform: uppercase;
    opacity: 0.75;
    margin: 0 0 8px 0;
}
.header h1 {
    margin: 0;
    font-size: 22pt;
    font-weight: 700;
    letter-spacing: -0.3px;
}
.header .sub {
    margin: 6px 0 0 0;
    font-size: 9.5pt;
    opacity: 0.85;
}
.content { padding: 32px 46px 16px 46px; }
h2 {
    font-size: 9.5pt;
    color: #3A56E2;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin: 28px 0 8px 0;
    font-weight: 700;
}
h2:first-child { margin-top: 0; }
.quote, .pitch-text {
    font-style: italic;
    color: #2a2c34;
}
.story, .focus-text, .reality-text {
    color: #2a2c34;
}
ul.plain { margin: 0; padding-left: 18px; }
ul.plain li { margin-bottom: 8px; color: #2a2c34; }
.plain-list { margin: 0; color: #2a2c34; }
.recommendation {
    font-size: 12pt;
    font-weight: 600;
    color: #14151a;
}
.path-item { margin-bottom: 10px; color: #2a2c34; }
.market-row { margin-bottom: 9px; color: #2a2c34; }
.market-label { display: block; font-weight: 700; color: #14151a; font-size: 9pt; margin-bottom: 1px; }
.footer {
    margin-top: 30px;
    padding-top: 12px;
    border-top: 1px solid #e6e8f0;
    text-align: center;
    color: #9aa0ae;
    font-size: 8pt;
    font-style: italic;
}
"""


def generate_strategy_pdf(context):
    """A short, visual strategy report from the candidate's own persona and
    top matches -- rendered from real HTML/CSS via WeasyPrint (gradients,
    shadows, proper typography) rather than hand-positioned shapes, so it
    actually looks like a designed document instead of a plain text dump."""
    try:
        from weasyprint import HTML
    except ImportError:
        print("weasyprint not installed — skipping PDF.")
        return None

    persona = context["persona"]
    if not persona:
        return None

    sections = []

    if persona.get("career_hybrid"):
        sections.append(f'<h2>How I see your background</h2><div class="quote">{_esc(persona["career_hybrid"])}</div>')

    if persona.get("transformation_story"):
        sections.append(f'<div class="story">{_esc(persona["transformation_story"])}</div>')

    if persona.get("positioning_pitch"):
        sections.append(f'<h2>A sharper way to pitch yourself</h2><div class="pitch-text">"{_esc(persona["positioning_pitch"])}"</div>')

    if persona.get("strengths"):
        items = "".join(f"<li>{_esc(s)}</li>" for s in persona["strengths"])
        sections.append(f'<h2>Your real strengths</h2><ul class="plain">{items}</ul>')

    target_titles = list(persona.get("target_titles") or []) + list(persona.get("recommended_titles") or [])
    if target_titles:
        sections.append(f'<h2>Titles worth adding to your search</h2><div class="plain-list">{_esc(", ".join(target_titles))}</div>')

    avoid = list(persona.get("avoid_roles") or []) + list(persona.get("roles_to_avoid") or [])
    if avoid:
        sections.append(f'<h2>What to avoid right now</h2><div class="plain-list">{_esc(", ".join(avoid))}</div>')

    if persona.get("location_rules"):
        sections.append(f'<h2>Search focus</h2><div class="focus-text">{_esc(persona["location_rules"])}</div>')

    if persona.get("alternative_paths"):
        items = "".join(f'<div class="path-item">{_esc(p)}</div>' for p in persona["alternative_paths"])
        sections.append(f'<h2>Alternative paths worth considering</h2>{items}')

    if persona.get("blind_spots"):
        items = "".join(f"<li>{_esc(b)}</li>" for b in persona["blind_spots"])
        sections.append(f'<h2>Honest read</h2><ul class="plain">{items}</ul>')

    if persona.get("market_reality_check"):
        sections.append(f'<h2>Market reality check</h2><div class="reality-text">{_esc(persona["market_reality_check"])}</div>')

    market = persona.get("market_overview") or {}
    if market:
        rows = "".join(
            f'<div class="market-row"><span class="market-label">{_esc(label)}</span>{_esc(_strip_citations(market[key]))}</div>'
            for key, label in [
                ("market_size", "Market size"),
                ("competitive_landscape", "Who else is hiring"),
                ("compensation_range", "Compensation"),
                ("sourcing_takeaway", "What this means for you"),
            ]
            if market.get(key)
        )
        sections.append(f'<h2>Job market overview</h2>{rows}')

    if persona.get("coach_recommendation"):
        sections.append(
            '<h2>If you take one thing from this</h2>'
            f'<div class="recommendation">{_esc(persona["coach_recommendation"])}</div>'
        )

    body_html = "\n".join(sections)
    first_name = _esc(context["first_name"])

    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{PDF_CSS}</style></head>
<body>
  <div class="header">
    <p class="eyebrow">Job Search Strategy</p>
    <h1>{first_name}'s plan</h1>
    <p class="sub">Personally put together by Jobbyo</p>
  </div>
  <div class="content">
    {body_html}
    <div class="footer">This is a living strategy, it updates as I learn more about what you want.</div>
  </div>
</body>
</html>"""

    return HTML(string=full_html).write_pdf()


def send_outreach_email(context, body_text, pdf_bytes=None, override_email=None, subject=None):
    email = override_email or context["profile"].get("email") or ""
    first_name = context["first_name"]

    # Deliberately not a centered/boxed template -- a normal person's email
    # is just left-aligned text filling the width, not a marketing card.
    paragraphs = "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in body_text.split("\n\n") if p.strip())

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#111111;margin:0;padding:16px;">
{paragraphs}
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": first_name}],
        "cc": [{"email": TEAM_CC_EMAIL, "name": "Thiago"}],
        "subject": subject or f"quick update, {first_name}",
        "htmlContent": html_content,
    }

    if pdf_bytes:
        import base64
        payload["attachment"] = [{
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "name": f"{first_name.lower()}-job-search-strategy.pdf",
        }]

    if not BREVO_API_KEY:
        print("BREVO_API_KEY not set — cannot send.")
        return False

    res = requests.post(
        f"{BREVO_API_URL}/smtp/email",
        headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
        json=payload,
        timeout=30,
    )
    if res.status_code >= 400:
        print(f"Brevo error {res.status_code}: {res.text[:200]}")
        return False
    print(f"Outreach email sent → {email}{' (with PDF)' if pdf_bytes else ''}")
    return True


PDF_CONTENTS_BLURB = [
    "Your real strengths, and a few honest blind spots",
    "Titles worth adding to your search, and a couple worth skipping",
    "A grounded look at the market: how many roles are out there, who's hiring, what it pays",
]


def send_prospect_outreach_email(context, body_text, pdf_bytes=None, override_email=None, subject=None):
    """Prospect version of send_outreach_email: same personal-note framing,
    a plain list of real matches (title, match %, link) if
    context["sample_jobs"] has any, a short list of what's in the attached
    PDF, and one soft inline mention of starting the trial -- no button, no
    hard sell. All appended as HTML so it's never left to the model."""
    email = override_email or context["profile"].get("email") or ""
    first_name = context["first_name"]
    sample_jobs = context.get("sample_jobs") or []

    paragraphs = "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in body_text.split("\n\n") if p.strip())

    if sample_jobs:
        lines = ""
        for j in sample_jobs:
            title = j.get("title") or ""
            company = j.get("company") or ""
            url = j.get("job_url") or j.get("url") or ""
            grade = j.get("grade")
            match_str = f" — {int(grade)}% match" if grade is not None else ""
            label = f"{title} at {company}{match_str}"
            row = f'<a href="{url}" style="color:#3A56E2;text-decoration:none;">{label}</a>' if url else label
            lines += f'<li style="margin-bottom:6px;">{row}</li>'
        preview_block = f'<ul style="margin:0 0 14px 0;padding-left:20px;">{lines}</ul>'
    else:
        preview_block = (
            '<p style="margin:0 0 14px 0;color:#4b5563;font-style:italic;">'
            "Still digging up your first matches, they'll be waiting in your queue the moment you start your trial."
            "</p>"
        )

    pdf_block = ""
    if pdf_bytes:
        pdf_items = "".join(f'<li style="margin-bottom:4px;">{item}</li>' for item in PDF_CONTENTS_BLURB)
        pdf_block = (
            '<p style="margin:14px 0 6px 0;">There\'s more where that came from, plus the full picture on '
            'where you stand, in the PDF I put together:</p>'
            f'<ul style="margin:0 0 14px 0;padding-left:20px;color:#374151;">{pdf_items}</ul>'
        )

    closing = (
        '<p style="margin:14px 0 0 0;">Whenever you\'re ready, I\'m happy to start actually applying for you, '
        'not just finding these: <a href="https://app.jobbyo.ai/auto-apply" style="color:#3A56E2;">start your trial</a>.</p>'
        '<p style="margin:16px 0 0 0;">Jobbyo</p>'
    )

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#111111;margin:0;padding:16px;">
{paragraphs}
{preview_block}
{pdf_block}
{closing}
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": first_name}],
        "cc": [{"email": addr, "name": addr.split("@")[0].title()} for addr in RETENTION_CC_EMAILS],
        # A reply goes to Adnan, not the sender address or Thiago -- he owns
        # retention conversations.
        "replyTo": {"email": RETENTION_REPLY_TO_EMAIL, "name": "Adnan"},
        "subject": subject or f"{first_name}, a few things I found",
        "htmlContent": html_content,
    }

    if pdf_bytes:
        import base64
        payload["attachment"] = [{
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "name": f"{first_name.lower()}-job-search-strategy.pdf",
        }]

    if not BREVO_API_KEY:
        print("BREVO_API_KEY not set — cannot send.")
        return False

    res = requests.post(
        f"{BREVO_API_URL}/smtp/email",
        headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
        json=payload,
        timeout=30,
    )
    if res.status_code >= 400:
        print(f"Brevo error {res.status_code}: {res.text[:200]}")
        return False
    print(f"Prospect outreach email sent → {email}{' (with PDF)' if pdf_bytes else ''}")
    return True


def send_paid_welcome_email(context, body_text, pdf_bytes=None, override_email=None, subject=None):
    """Paid-user counterpart to send_prospect_outreach_email: same personal-
    note framing and PDF-contents teaser, but the job list is their real
    matches (context["sample_jobs"] from build_context, already saved to
    their queue) and there's no trial CTA -- they're already subscribed, so
    the closing just points at reviewing the queue instead of selling
    anything."""
    email = override_email or context["profile"].get("email") or ""
    first_name = context["first_name"]
    sample_jobs = context.get("sample_jobs") or []

    paragraphs = "".join(f'<p style="margin:0 0 14px 0;">{p}</p>' for p in body_text.split("\n\n") if p.strip())

    if sample_jobs:
        lines = ""
        for j in sample_jobs:
            title = j.get("title") or ""
            company = j.get("company") or ""
            url = j.get("job_url") or j.get("url") or ""
            grade = j.get("grade")
            match_str = f" — {int(grade)}% match" if grade is not None else ""
            label = f"{title} at {company}{match_str}"
            row = f'<a href="{url}" style="color:#3A56E2;text-decoration:none;">{label}</a>' if url else label
            lines += f'<li style="margin-bottom:6px;">{row}</li>'
        preview_block = f'<ul style="margin:0 0 14px 0;padding-left:20px;">{lines}</ul>'
    else:
        preview_block = (
            '<p style="margin:0 0 14px 0;color:#4b5563;font-style:italic;">'
            "Still digging up more for you, they'll land in your queue as soon as I find them."
            "</p>"
        )

    pdf_block = ""
    if pdf_bytes:
        pdf_items = "".join(f'<li style="margin-bottom:4px;">{item}</li>' for item in PDF_CONTENTS_BLURB)
        pdf_block = (
            '<p style="margin:14px 0 6px 0;">There\'s more where that came from, plus the full picture on '
            'where you stand, in the PDF I put together:</p>'
            f'<ul style="margin:0 0 14px 0;padding-left:20px;color:#374151;">{pdf_items}</ul>'
        )

    closing = (
        '<p style="margin:14px 0 0 0;">These are already sitting in your '
        '<a href="https://app.jobbyo.ai/auto-apply" style="color:#3A56E2;">queue</a> to review.</p>'
        '<p style="margin:16px 0 0 0;">Jobbyo</p>'
    )

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#111111;margin:0;padding:16px;">
{paragraphs}
{preview_block}
{pdf_block}
{closing}
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": first_name}],
        "cc": [{"email": TEAM_CC_EMAIL, "name": "Thiago"}],
        "subject": subject or f"{first_name}, here's what I found for you",
        "htmlContent": html_content,
    }

    if pdf_bytes:
        import base64
        payload["attachment"] = [{
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "name": f"{first_name.lower()}-job-search-strategy.pdf",
        }]

    if not BREVO_API_KEY:
        print("BREVO_API_KEY not set — cannot send.")
        return False

    res = requests.post(
        f"{BREVO_API_URL}/smtp/email",
        headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
        json=payload,
        timeout=30,
    )
    if res.status_code >= 400:
        print(f"Brevo error {res.status_code}: {res.text[:200]}")
        return False
    print(f"Paid welcome email sent → {email}{' (with PDF)' if pdf_bytes else ''}")
    return True


def main():
    args = sys.argv[1:]
    uid, email, send_to = None, None, None
    if "--uid" in args:
        uid = args[args.index("--uid") + 1]
    if "--email" in args:
        email = args[args.index("--email") + 1]
    if "--send-to" in args:
        send_to = args[args.index("--send-to") + 1]
    # --paid-welcome: the new-subscriber note (personal read + real matches
    # + PDF, no trial pitch). Default (no flag) is the original follow-up
    # to someone who already opened their welcome email.
    paid_welcome = "--paid-welcome" in args

    if not uid and email:
        looked_up = send_jobbyo.api_get(f"{send_jobbyo.BASE_URL}/users/email/{email}/") or {}
        uid = looked_up.get("uid") or looked_up.get("id") or looked_up.get("userId")

    if not uid:
        print("Provide --uid or --email")
        sys.exit(1)

    context = build_context(uid)
    print("=== Context ===")
    print(context)

    pdf_bytes = generate_strategy_pdf(context)
    print(f"\n=== PDF generated: {bool(pdf_bytes)} ===")

    # --send-to redirects delivery without changing whose data the content
    # was generated from -- e.g. reviewing what a real user's email would
    # say without actually sending it to that real person.
    if paid_welcome:
        body_text = generate_paid_welcome_message(context)
        print("\n=== Generated message ===")
        print(body_text)
        send_paid_welcome_email(context, body_text, pdf_bytes=pdf_bytes, override_email=send_to or email)
    else:
        body_text = generate_message(context)
        print("\n=== Generated message ===")
        print(body_text)
        send_outreach_email(context, body_text, pdf_bytes=pdf_bytes, override_email=send_to or email)


if __name__ == "__main__":
    main()
