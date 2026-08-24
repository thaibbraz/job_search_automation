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
on their own search than a plain job list -- reflect back how you're reading \
their profile (using the persona/strategy notes below), and surface one \
alternative angle or adjacent direction worth considering that they may not \
have thought of themselves. This should feel like real, specific insight \
into their search, not generic career advice.

STRICT RULES:
- Never say "AI", "artificial intelligence", "bot", "automated", or anything \
that frames you as software. Write like a person who has been working on \
their search.
- Never use an em dash (—) anywhere. Use a period, comma, or "and" instead.
- Do not invent specific statistics, percentages, or numbers about the job \
market. You may make ONE brief, general, qualitative comment about the \
market for their field if it feels natural (e.g. "it's been a decent time \
to be looking in that space") -- nothing that sounds like a fabricated stat.
- Reference their actual stated preferences and 1-2 of the actual jobs found \
for them (given below) so it's obviously personal, not generic.
- Use the persona/strategy notes below to name ONE alternative angle,
adjacent title, or strategy shift worth considering, and briefly say why.
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


def _load_persona(uid):
    """Pull the already-generated persona file (career framing, target
    titles, roles being intentionally avoided, alternative strategy angles)
    -- used for both the email prompt and the attached PDF."""
    try:
        path = send_jobbyo.persona_path(uid)
        if not path.exists():
            return {}
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _persona_summary(persona):
    lines = []
    if persona.get("career_hybrid"):
        lines.append(f"Career framing: {persona['career_hybrid']}")
    if persona.get("target_titles"):
        lines.append(f"Target titles: {', '.join(persona['target_titles'])}")
    if persona.get("best_fit_roles"):
        lines.append(f"Best-fit role categories: {', '.join(persona['best_fit_roles'])}")
    if persona.get("avoid_roles"):
        lines.append(f"Intentionally avoiding: {', '.join(persona['avoid_roles'])}")
    return "\n".join(lines) or "(no persona notes on file yet)"


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

    persona = _load_persona(uid)

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


def _grade_badge_class(grade):
    grade = int(grade or 0)
    if grade >= 80:
        return "badge-strong", f"{grade} MATCH"
    if grade >= 60:
        return "badge-good", f"{grade} MATCH"
    return "badge-fair", f"{grade} MATCH"


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
.header {
    background: linear-gradient(135deg, #3A56E2 0%, #2338A8 100%);
    color: #ffffff;
    padding: 22px 40px 18px 40px;
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
    font-size: 21pt;
    font-weight: 700;
    letter-spacing: -0.3px;
}
.header .sub {
    margin: 6px 0 0 0;
    font-size: 10pt;
    opacity: 0.85;
}
.content { padding: 18px 40px 10px 40px; }
h2 {
    font-size: 11.5pt;
    color: #2338A8;
    margin: 13px 0 6px 0;
    font-weight: 700;
}
h2:first-child { margin-top: 0; }
.quote-box {
    background: #EEF1FD;
    border-left: 3px solid #3A56E2;
    border-radius: 4px;
    padding: 10px 16px;
    font-style: italic;
    color: #1f2338;
    font-size: 10.5pt;
}
.story {
    color: #3d3f4d;
    margin-top: 6px;
    font-size: 10pt;
}
.match-card {
    border: 1px solid #e6e8f0;
    border-radius: 8px;
    padding: 8px 14px;
    margin-bottom: 6px;
    background: #fdfdff;
    page-break-inside: avoid;
}
.match-card .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.match-card .title { font-weight: 700; font-size: 10.5pt; color: #14151a; }
.match-card .company { color: #6b7280; font-size: 9pt; margin-top: 1px; }
.match-card .reason { color: #4b4d5a; font-size: 9pt; margin-top: 4px; }
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
    padding: 4px 11px;
    border-radius: 20px;
    font-size: 8.5pt;
}
.chip-target { background: #3A56E2; color: #ffffff; }
.chip-avoid  { background: #eef0f4; color: #4b4d5a; }
.focus-text { color: #3d3f4d; font-size: 9.5pt; }
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
        sections.append(f'<h2>How I see your background</h2><div class="quote-box">{_esc(persona["career_hybrid"])}</div>')

    if persona.get("transformation_story"):
        sections.append(f'<div class="story">{_esc(persona["transformation_story"])}</div>')

    real_jobs = [j for j in (context.get("automation_jobs") or []) if _looks_like_real_job(j)]
    real_jobs.sort(key=lambda j: j.get("grade") or 0, reverse=True)
    top_matches = real_jobs[:3]
    if top_matches:
        cards = ""
        for j in top_matches:
            badge_class, badge_text = _grade_badge_class(j.get("grade"))
            reason = approve_jobs._job_reason(j)
            cards += f"""<div class="match-card">
  <div class="row">
    <div>
      <div class="title">{_esc(j.get('title'))}</div>
      <div class="company">{_esc(j.get('company'))}</div>
    </div>
    <span class="badge {badge_class}">{_esc(badge_text)}</span>
  </div>
  <div class="reason">{_esc(reason)}</div>
</div>"""
        sections.append(f'<h2>Your top matches right now</h2>{cards}')

    if persona.get("target_titles"):
        chips = "".join(f'<span class="chip chip-target">{_esc(t)}</span>' for t in persona["target_titles"])
        sections.append(f'<h2>Targeting</h2><div class="chips">{chips}</div>')

    if persona.get("avoid_roles"):
        chips = "".join(f'<span class="chip chip-avoid">{_esc(t)}</span>' for t in persona["avoid_roles"])
        sections.append(f'<h2>Intentionally skipping</h2><div class="chips">{chips}</div>')

    if persona.get("location_rules"):
        sections.append(f'<h2>Search focus</h2><div class="focus-text">{_esc(persona["location_rules"])}</div>')

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
    uid, email = None, None
    if "--uid" in args:
        uid = args[args.index("--uid") + 1]
    if "--email" in args:
        email = args[args.index("--email") + 1]

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

    send_outreach_email(context, body_text, pdf_bytes=pdf_bytes, override_email=email)


if __name__ == "__main__":
    main()
