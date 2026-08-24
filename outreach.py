"""outreach.py — personalized, conversational follow-up to a candidate who
opened their subscribed-search email.

Pulls the candidate's own stated preferences + a few of their actual matched
jobs, has the LLM write a short first-person note in Jobbyo's voice (not
"an AI", no sales language), and sends it via Brevo with thiago@jobbyo.ai
CC'd. This is the message-generation + send piece only — the "who opened
today" check and lead-scoring tiers are a separate scheduled step to be
added once this is confirmed good.

Usage:
    python3 outreach.py --uid <uid>
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
    },
    "required": [
        "strengths", "blind_spots", "market_reality_check",
        "coach_recommendation", "positioning_pitch", "alternative_paths",
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
something specific from their actual background."""


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

    return persona


def _persona_summary(persona):
    lines = []
    if persona.get("career_hybrid"):
        lines.append(f"Career framing: {persona['career_hybrid']}")
    if persona.get("target_titles"):
        lines.append(f"Target titles: {', '.join(persona['target_titles'])}")
    if persona.get("strengths"):
        lines.append("Real strengths:\n" + "\n".join(f"  - {s}" for s in persona["strengths"]))
    if persona.get("blind_spots"):
        lines.append("Honest blind spots:\n" + "\n".join(f"  - {b}" for b in persona["blind_spots"]))
    if persona.get("market_reality_check"):
        lines.append(f"Market reality check: {persona['market_reality_check']}")
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


import html as _html


def _esc(text):
    return _html.escape(str(text or ""))


PDF_CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    color: #14151a;
    font-size: 10.5pt;
    line-height: 1.55;
}
/* One accent color (brand blue), used sparingly: header, headings, the one
   recommendation callout, and target chips. Everything else is plain
   typography -- no rainbow of tinted boxes competing for attention. */
.header {
    background: #3A56E2;
    color: #ffffff;
    padding: 20px 40px 16px 40px;
}
.header .eyebrow {
    font-size: 8.5pt;
    letter-spacing: 2px;
    text-transform: uppercase;
    opacity: 0.75;
    margin: 0 0 6px 0;
}
.header h1 {
    margin: 0;
    font-size: 20pt;
    font-weight: 700;
    letter-spacing: -0.3px;
}
.header .sub {
    margin: 5px 0 0 0;
    font-size: 9.5pt;
    opacity: 0.85;
}
.content { padding: 16px 40px 8px 40px; }
h2 {
    font-size: 11pt;
    color: #14151a;
    margin: 14px 0 5px 0;
    font-weight: 700;
    border-bottom: 1px solid #e6e8f0;
    padding-bottom: 4px;
}
h2:first-child { margin-top: 0; }
.quote {
    font-style: italic;
    color: #3d3f4d;
    font-size: 10.5pt;
}
.story, .focus-text, .reality-text {
    color: #3d3f4d;
    font-size: 9.5pt;
    margin-top: 4px;
}
.match-card {
    border-bottom: 1px solid #eef0f4;
    padding: 7px 0;
    page-break-inside: avoid;
}
.match-card .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.match-card .title { font-weight: 700; font-size: 10.5pt; color: #14151a; }
.match-card .company { color: #6b7280; font-size: 9pt; margin-top: 1px; }
.match-card .reason { color: #4b4d5a; font-size: 9pt; margin-top: 3px; }
.badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 0.3px;
    white-space: nowrap;
    color: #ffffff;
}
.badge-strong { background: #16a34a; }
.badge-good   { background: #d97706; }
.badge-fair   { background: #6b7280; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 8.5pt;
}
.chip-target { background: #EEF1FD; color: #2338A8; }
.chip-avoid  { background: #f3f4f6; color: #6b7280; }
ul.plain { margin: 4px 0 0 0; padding-left: 16px; }
ul.plain li { margin-bottom: 4px; font-size: 9.5pt; color: #3d3f4d; }
.recommendation {
    border-left: 3px solid #3A56E2;
    padding: 2px 0 2px 14px;
    margin-top: 4px;
}
.recommendation .text {
    font-size: 10.5pt;
    color: #14151a;
    font-weight: 600;
}
.pitch-text {
    font-style: italic;
    color: #3d3f4d;
    font-size: 9.5pt;
    margin-top: 4px;
}
.path-item { margin-bottom: 6px; font-size: 9.5pt; color: #3d3f4d; }
.footer {
    margin-top: 14px;
    padding-top: 8px;
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

    if persona.get("target_titles"):
        chips = "".join(f'<span class="chip chip-target">{_esc(t)}</span>' for t in persona["target_titles"])
        sections.append(f'<h2>Targeting</h2><div class="chips">{chips}</div>')

    if persona.get("avoid_roles"):
        chips = "".join(f'<span class="chip chip-avoid">{_esc(t)}</span>' for t in persona["avoid_roles"])
        sections.append(f'<h2>Intentionally skipping</h2><div class="chips">{chips}</div>')

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

    if persona.get("coach_recommendation"):
        sections.append(
            '<h2>If you take one thing from this</h2>'
            f'<div class="recommendation"><div class="text">{_esc(persona["coach_recommendation"])}</div></div>'
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


def send_outreach_email(context, body_text, pdf_bytes=None, override_email=None):
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
        "to": [{"email": email, "name": first_name}, {"email": TEAM_CC_EMAIL, "name": "Thiago"}],
        "subject": f"quick update, {first_name}",
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


def main():
    args = sys.argv[1:]
    uid, email, send_to = None, None, None
    if "--uid" in args:
        uid = args[args.index("--uid") + 1]
    if "--email" in args:
        email = args[args.index("--email") + 1]
    if "--send-to" in args:
        send_to = args[args.index("--send-to") + 1]

    if not uid and email:
        looked_up = send_jobbyo.api_get(f"{send_jobbyo.BASE_URL}/users/email/{email}/") or {}
        uid = looked_up.get("uid") or looked_up.get("id") or looked_up.get("userId")

    if not uid:
        print("Provide --uid or --email")
        sys.exit(1)

    context = build_context(uid)
    print("=== Context ===")
    print(context)

    body_text = generate_message(context)
    print("\n=== Generated message ===")
    print(body_text)

    pdf_bytes = generate_strategy_pdf(context)
    print(f"\n=== PDF generated: {bool(pdf_bytes)} ===")

    # --send-to redirects delivery without changing whose data the content
    # was generated from -- e.g. reviewing what a real user's email would
    # say without actually sending it to that real person.
    send_outreach_email(context, body_text, pdf_bytes=pdf_bytes, override_email=send_to or email)


if __name__ == "__main__":
    main()
