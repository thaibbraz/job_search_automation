"""
approve_jobs.py — Promote pending_review jobs and send email notifications.

Run after send_jobbyo.py has staged jobs as pending_review.

For each paid user:
  - Skip if already emailed today (dedup via run_logs/emailed_YYYY-MM-DD.json)
  - Fetch their selectedJobs
  - Find pending_review jobs
  - If >= MIN_JOBS_TO_EMAIL: promote to plan-appropriate status and send email
  - Post a Slack summary of this run's results

On startup, deletes emailed_*.json logs older than yesterday to avoid accumulation.

Usage:
    python3 approve_jobs.py
    python3 approve_jobs.py --email user@example.com   # single user
    python3 approve_jobs.py --force                    # bypass MIN_JOBS_TO_EMAIL for everyone
    python3 approve_jobs.py --email user@example.com --force  # bypass for one user
"""

import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://fastapi-service-03-160893319817.europe-southwest1.run.app"

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_API_URL = "https://api.brevo.com/v3"
SENDER_EMAIL = "hello@jobbyo.ai"
SENDER_NAME = "Jobbyo"

MIN_JOBS_TO_EMAIL = 8
AUTO_STATUS = "approved"
MAX_STATUS = "waiting_approval"

# The just-subscribed welcome email (send_subscribed_jobs_email) is gated on
# a lower bar than the daily digest above — a thin first batch is still worth
# sending right after signup, since the alternative is silence.
MIN_JOBS_TO_NOTIFY_SUBSCRIBED = 3

# Only jobs added TODAY count toward the threshold and email.
# "pending_review" is send_jobbyo.py's staging status for freshly-found jobs
# (see promote_jobs() below) -- included here so a normal day's search
# actually counts toward the threshold instead of silently never reaching it.
# "approved"/"applied" is what Auto Mode users' jobs get promoted straight to
# (see target_status() below) -- Auto Mode users still get the daily email,
# just as a "here's what I found/applied to for you" summary rather than a
# review prompt, since silence isn't an acceptable substitute for an email.
STATUSES_TO_COUNT = {"applied", "approved", "pending", "waiting_approval", "pending_review"}
STATUSES_TO_EMAIL = {"pending", "waiting_approval", "pending_review", "approved", "applied"}

# Per-user emailed/missing detail — as opposed to run health/coverage %,
# which api.py posts to SLACK_WEBHOOK_URL_DAILY_RUN instead.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL_USER_DETAILS", "")
RUN_LOGS_DIR = Path("run_logs")

# ---------------------------------------------------------------------------
# Log cleanup
# ---------------------------------------------------------------------------

def cleanup_old_emailed_logs():
    """Delete emailed_*.json files older than yesterday."""
    if not RUN_LOGS_DIR.exists():
        return
    cutoff = date.today() - timedelta(days=1)
    for f in RUN_LOGS_DIR.glob("emailed_*.json"):
        try:
            file_date = date.fromisoformat(f.stem.replace("emailed_", ""))
            if file_date < cutoff:
                f.unlink()
                print(f"Deleted old log: {f.name}")
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _is_today(job):
    """Return True if the job's addedAt calendar date is today (UTC).

    Calendar-date equality is safe again because the evening search passes
    (send_jobbyo.py's api_post_jobs) now pre-date scheduled-run jobs with
    tomorrow's date at the moment they're added, instead of the real add
    time -- so by the time this next-morning email pass runs, "today" is
    already baked into addedAt. (A rolling 24h window doesn't work here: it
    would still count yesterday's already-emailed batch as "recent" for
    several hours into today, since scheduled jobs cluster at a fixed
    08:00 UTC timestamp rather than spreading across the evening.)
    """
    for field in ("addedAt", "createdAt", "added_at", "created_at"):
        val = job.get(field)
        if val:
            try:
                dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                return dt.date() == datetime.now(timezone.utc).date()
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# Emailed-today deduplication
# ---------------------------------------------------------------------------

def _emailed_today_path():
    return RUN_LOGS_DIR / f"emailed_{date.today().isoformat()}.json"


def load_emailed_today():
    path = _emailed_today_path()
    if path.exists():
        try:
            return set(json.loads(path.read_text()).get("uids", []))
        except Exception:
            return set()
    return set()


def save_emailed_today(uid_set):
    RUN_LOGS_DIR.mkdir(exist_ok=True)
    _emailed_today_path().write_text(json.dumps({"uids": sorted(uid_set)}, indent=2))


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(url):
    try:
        res = requests.get(url, headers={"accept": "application/json"}, timeout=30)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"GET {url} failed: {e}")
        return None


def _post(url, payload):
    try:
        res = requests.post(
            url,
            json=payload,
            headers={"accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"POST {url} failed: {e}")
        return None


def get_paid_users():
    return _get(f"{BASE_URL}/users/paid") or []


def get_automation(uid):
    return _get(f"{BASE_URL}/automations/users/{uid}") or {}


def get_user_profile(uid):
    return _get(f"{BASE_URL}/users/id/{uid}/") or {}


# ---------------------------------------------------------------------------
# Automation mode detection (mirrors jobbyo-admin-platform-2's
# getAutomationMode in PaidUsers.js)
# ---------------------------------------------------------------------------

def get_automation_mode(automation):
    cfg = (automation or {}).get("applyConfig")
    if not cfg:
        return "approve"   # no applyConfig on the record => backend default
    if cfg.get("reviewBeforeSubmit") is not False:
        return "control"
    if cfg.get("requireApproval") is not False:
        return "approve"
    return "auto"


def target_status(mode):
    if mode == "auto":
        return AUTO_STATUS
    return MAX_STATUS   # control and approve both hold for the user to review


# ---------------------------------------------------------------------------
# Job promotion
# ---------------------------------------------------------------------------

def promote_jobs(email, jobs_to_promote, new_status):
    """
    Re-post the jobs with the new status. The backend upserts by job_url,
    so sending the same jobs with a new default_status updates them in place.
    """
    payload = {
        "jobs": jobs_to_promote,
        "default_status": new_status,
    }
    url = f"{BASE_URL}/automations/users/by-email/{urllib.parse.quote(email)}/selected-jobs"
    return _post(url, payload)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _job_reason(job):
    """Reviewer's reason, with a grade-based fallback when none was stored."""
    reason = (job.get("review_reason") or job.get("reason") or "").strip()
    if reason:
        return reason
    grade = int(job.get("grade") or 0)
    if grade >= 80:
        return f"Strong match ({grade}/100) — selected based on your background and search preferences."
    if grade >= 60:
        return f"Good fit ({grade}/100) — aligns with your target roles and location."
    return "Selected based on your search preferences and profile."


def send_first_matches_email(user_profile, jobs, override_email=None, is_paid=True):
    """Welcome email sent right after a candidate finishes their automation.

    Distinct from the daily digest: this is the first thing they receive, so it
    frames what the product just did rather than reporting a routine batch. For
    a candidate without a plan the matches are shown but not saved, so the
    closing copy points at signing up instead of at their queue.
    """
    email = override_email or user_profile.get("email") or ""
    name = user_profile.get("displayName") or (user_profile.get("email") or "").split("@")[0]
    first_name = name.split()[0] if name else "there"

    cards = ""
    for j in jobs:
        title = j.get("title") or ""
        company = j.get("company") or ""
        url = j.get("job_url") or j.get("url") or ""
        location = j.get("location") or ""
        grade = int(j.get("grade") or 0)
        reason = _job_reason(j)

        heading = (
            f'<a href="{url}" style="color:#3A56E2;text-decoration:none;font-weight:700;">{title}</a>'
            if url else f'<strong style="color:#3A56E2;">{title}</strong>'
        )
        meta = " · ".join([p for p in (company, location) if p])

        cards += f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin:0 0 16px 0;">
      <p style="margin:0 0 4px 0;font-size:16px;line-height:1.5;">{heading}</p>
      <p style="margin:0 0 12px 0;font-size:13px;color:#6b7280;font-family:Arial,sans-serif;">{meta}</p>
      <p style="margin:0;font-size:14px;color:#374151;line-height:1.7;">{reason}</p>
      <p style="margin:12px 0 0 0;font-size:12px;color:#9ca3af;font-family:Arial,sans-serif;">Match score {grade}/100</p>
    </div>"""

    if is_paid:
        closing = "These are saved to your queue — review and approve them whenever you're ready."
        cta_text, cta_url = "Review &amp; Approve Jobs", "https://app.jobbyo.ai/auto-apply"
    else:
        closing = "This is a preview. Start your plan and Jobbyo will keep finding roles like these every day, and apply for you."
        cta_text, cta_url = "Start Applying", "https://app.jobbyo.ai/pricing"

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Georgia,serif;background:#ffffff;margin:0;padding:0;color:#111827;">
  <div style="max-width:580px;margin:0 auto;padding:40px 24px;">

    <p style="margin:0 0 6px 0;font-size:12px;font-weight:700;color:#3A56E2;text-transform:uppercase;letter-spacing:0.08em;font-family:Arial,sans-serif;">Jobbyo</p>
    <p style="margin:0 0 24px 0;font-size:15px;line-height:1.7;">Hi {first_name},</p>
    <p style="margin:0 0 28px 0;font-size:15px;line-height:1.7;">Your profile is set up, and I've already been looking. Here {'is' if len(jobs) == 1 else 'are'} the {'role' if len(jobs) == 1 else f'{len(jobs)} roles'} that fit you best right now.</p>

    {cards if cards else '<p style="font-size:15px;color:#6b7280;font-style:italic;">No strong matches yet — I&#39;ll keep looking and email you as soon as I find one.</p>'}

    <p style="margin:28px 0 0 0;font-size:15px;line-height:1.7;">{closing}</p>
    <p style="margin:20px 0 0 0;font-size:15px;line-height:1.7;">Best,<br>Jobbyo</p>

    <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0;">

    <div style="text-align:center;margin-bottom:16px;">
      <a href="{cta_url}" style="display:inline-block;background:#3A56E2;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-size:14px;font-family:Arial,sans-serif;font-weight:600;">
        {cta_text}
      </a>
    </div>
    <p style="text-align:center;font-size:12px;color:#9ca3af;font-family:Arial,sans-serif;">
      <a href="https://app.jobbyo.ai/settings" style="color:#9ca3af;">Manage preferences</a>
    </p>

  </div>
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": name}],
        "subject": f"{first_name}, here {'is your first match' if len(jobs) == 1 else f'are your first {len(jobs)} matches'}",
        "htmlContent": html_content,
    }

    if not BREVO_API_KEY:
        print("  BREVO_API_KEY not set — cannot send first-matches email.")
        return False

    try:
        res = requests.post(
            f"{BREVO_API_URL}/smtp/email",
            headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
            json=payload,
            timeout=30,
        )
        if res.status_code >= 400:
            print(f"  Brevo error {res.status_code}: {res.text[:200]}")
            return False
        print(f"  First-matches email sent → {email}  ({len(jobs)} jobs)")
        return True
    except Exception as e:
        print(f"  First-matches email failed for {email}: {e}")
        return False


def send_subscribed_jobs_email(user_profile, jobs, target_count, override_email=None):
    """Sent right after a candidate subscribes, once their first search finishes.

    Gated on MIN_JOBS_TO_NOTIFY_SUBSCRIBED — a batch thinner than that isn't
    worth interrupting the candidate for; the caller should just let the
    regular pipeline pick them up later instead. When the batch falls short
    of target_count the closing says more are still coming, so a candidate
    with 3 jobs isn't left thinking the search is already done.
    """
    if len(jobs) < MIN_JOBS_TO_NOTIFY_SUBSCRIBED:
        return False

    email = override_email or user_profile.get("email") or ""
    name = user_profile.get("displayName") or (user_profile.get("email") or "").split("@")[0]
    first_name = name.split()[0] if name else "there"

    cards = ""
    for j in jobs:
        title = j.get("title") or ""
        company = j.get("company") or ""
        url = j.get("job_url") or j.get("url") or ""
        location = j.get("location") or ""
        grade = int(j.get("grade") or 0)
        reason = _job_reason(j)

        heading = (
            f'<a href="{url}" style="color:#3A56E2;text-decoration:none;font-weight:700;">{title}</a>'
            if url else f'<strong style="color:#3A56E2;">{title}</strong>'
        )
        meta = " · ".join([p for p in (company, location) if p])

        cards += f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin:0 0 16px 0;">
      <p style="margin:0 0 4px 0;font-size:16px;line-height:1.5;">{heading}</p>
      <p style="margin:0 0 12px 0;font-size:13px;color:#6b7280;font-family:Arial,sans-serif;">{meta}</p>
      <p style="margin:0;font-size:14px;color:#374151;line-height:1.7;">{reason}</p>
      <p style="margin:12px 0 0 0;font-size:12px;color:#9ca3af;font-family:Arial,sans-serif;">Match score {grade}/100</p>
    </div>"""

    if len(jobs) >= target_count:
        closing = "These are saved to your queue — review and approve them whenever you're ready."
    else:
        closing = (
            f"That's the first {len(jobs)} I could find right now — I'm still searching and will "
            "keep adding roles to your queue today."
        )

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Georgia,serif;background:#ffffff;margin:0;padding:0;color:#111827;">
  <div style="max-width:580px;margin:0 auto;padding:40px 24px;">

    <p style="margin:0 0 6px 0;font-size:12px;font-weight:700;color:#3A56E2;text-transform:uppercase;letter-spacing:0.08em;font-family:Arial,sans-serif;">Jobbyo</p>
    <p style="margin:0 0 24px 0;font-size:15px;line-height:1.7;">Hi {first_name},</p>
    <p style="margin:0 0 28px 0;font-size:15px;line-height:1.7;">Welcome! I've already been looking. Here {'is' if len(jobs) == 1 else 'are'} the {'role' if len(jobs) == 1 else f'{len(jobs)} roles'} that fit you best right now, and why.</p>

    {cards}

    <p style="margin:28px 0 0 0;font-size:15px;line-height:1.7;">{closing}</p>
    <p style="margin:20px 0 0 0;font-size:15px;line-height:1.7;">Best,<br>Jobbyo</p>

    <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0;">

    <div style="text-align:center;margin-bottom:16px;">
      <a href="https://app.jobbyo.ai/auto-apply" style="display:inline-block;background:#3A56E2;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-size:14px;font-family:Arial,sans-serif;font-weight:600;">
        Review &amp; Approve Jobs
      </a>
    </div>
    <p style="text-align:center;font-size:12px;color:#9ca3af;font-family:Arial,sans-serif;">
      <a href="https://app.jobbyo.ai/settings" style="color:#9ca3af;">Manage preferences</a>
    </p>

  </div>
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": name}],
        "subject": f"{first_name}, here {'is your first match' if len(jobs) == 1 else f'are your first {len(jobs)} matches'}",
        "htmlContent": html_content,
    }

    if not BREVO_API_KEY:
        print("  BREVO_API_KEY not set — cannot send subscribed-jobs email.")
        return False

    try:
        res = requests.post(
            f"{BREVO_API_URL}/smtp/email",
            headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
            json=payload,
            timeout=30,
        )
        if res.status_code >= 400:
            print(f"  Brevo error {res.status_code}: {res.text[:200]}")
            return False
        print(f"  Subscribed-jobs email sent → {email}  ({len(jobs)} jobs)")
        return True
    except Exception as e:
        print(f"  Subscribed-jobs email failed for {email}: {e}")
        return False


def send_email(user_profile, automation, jobs, override_email=None):
    """Send the daily report email directly via Brevo."""
    email = override_email or user_profile.get("email") or ""
    name = user_profile.get("displayName") or (user_profile.get("email") or "").split("@")[0]
    first_name = name.split()[0] if name else "there"

    jobs_html = ""
    for j in jobs:
        title = j.get("title", "")
        company = j.get("company", "")
        url = j.get("job_url") or j.get("url") or ""
        reason = (j.get("review_reason") or j.get("reason") or "").strip()
        grade = int(j.get("grade") or 0)

        # Fallback reason when none stored
        if not reason:
            if grade >= 80:
                reason = f"Strong match ({grade}/100) — selected based on your background and search preferences."
            elif grade >= 60:
                reason = f"Good fit ({grade}/100) — aligns with your target roles and location."
            else:
                reason = "Selected based on your search preferences and profile."

        if url:
            title_link = f'<a href="{url}" style="color:#3A56E2;text-decoration:underline;font-weight:700;">{title}</a>'
        else:
            title_link = f'<strong style="color:#3A56E2;">{title}</strong>'

        company_part = f' at {company}' if company else ""
        jobs_html += f'<p style="margin:0 0 20px 0;font-size:15px;color:#111827;line-height:1.7;">{title_link}{company_part}: <span style="color:#374151;">{reason}</span></p>'

    html_content = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Georgia,serif;background:#ffffff;margin:0;padding:0;color:#111827;">
  <div style="max-width:580px;margin:0 auto;padding:40px 24px;">

    <p style="margin:0 0 6px 0;font-size:12px;font-weight:700;color:#3A56E2;text-transform:uppercase;letter-spacing:0.08em;font-family:Arial,sans-serif;">Jobbyo</p>
    <p style="margin:0 0 24px 0;font-size:15px;line-height:1.7;">Hi {first_name},</p>
    <p style="margin:0 0 28px 0;font-size:15px;line-height:1.7;">Here are the roles I found for you today.</p>

    {jobs_html if jobs_html else '<p style="font-size:15px;color:#6b7280;font-style:italic;">No new roles in this batch.</p>'}

    <p style="margin:28px 0 0 0;font-size:15px;line-height:1.7;">Best,<br>Jobbyo</p>

    <hr style="border:none;border-top:1px solid #e5e7eb;margin:32px 0;">

    <div style="text-align:center;margin-bottom:16px;">
      <a href="https://app.jobbyo.ai/auto-apply" style="display:inline-block;background:#3A56E2;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-size:14px;font-family:Arial,sans-serif;font-weight:600;">
        Review &amp; Approve Jobs
      </a>
    </div>
    <p style="text-align:center;font-size:12px;color:#9ca3af;font-family:Arial,sans-serif;">
      <a href="https://app.jobbyo.ai/settings" style="color:#9ca3af;">Manage preferences</a>
    </p>

  </div>
</body>
</html>"""

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": name}, {"email": "hello@jobbyo.ai", "name": "Jobbyo"}],
        "subject": f"Your job matches for today, {first_name}",
        "htmlContent": html_content,
    }

    try:
        res = requests.post(
            f"{BREVO_API_URL}/smtp/email",
            headers={"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"},
            json=payload,
            timeout=30,
        )
        if res.status_code >= 400:
            print(f"  Brevo error {res.status_code}: {res.text[:200]}")
            return False
        print(f"  Email sent → {email}  ({len(jobs)} jobs)")
        return True
    except Exception as e:
        print(f"  Email failed for {email}: {e}")
        return False


# ---------------------------------------------------------------------------
# Slack summary
# ---------------------------------------------------------------------------

def post_slack(text):
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Slack post failed: {e}")


def send_slack_summary(emailed_users, skipped_users, pending_users):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📧 *approve_jobs* — {now}"]

    lines.append(f"✅ {len(emailed_users)} email(s) sent this run." if emailed_users else "No new emails sent this run.")
    if skipped_users:
        lines.append(f"⏭️ {len(skipped_users)} skipped (already sent today).")

    if pending_users:
        lines.append(f"⚠️ *{len(pending_users)} below threshold ({MIN_JOBS_TO_EMAIL} jobs):*")
        for u in pending_users:
            lines.append(f"  • {u['name']} ({u['email']}) — {u['pending_count']}/{MIN_JOBS_TO_EMAIL} jobs")

    post_slack("\n".join(lines))


# ---------------------------------------------------------------------------
# Process one user
# ---------------------------------------------------------------------------

def process_user(user, force=False):
    """force=True bypasses MIN_JOBS_TO_EMAIL — used for a manual/admin-triggered
    send to a user who didn't naturally reach the threshold today, so they
    still get whatever was found instead of nothing.
    """
    uid = user.get("uid") or user.get("id") or ""
    email = user.get("email") or ""
    name = user.get("displayName") or email.split("@")[0]

    if not uid or not email:
        return None

    automation = get_automation(uid)
    user_profile = get_user_profile(uid) or user

    mode = get_automation_mode(automation)
    new_status = target_status(mode)

    selected_jobs = automation.get("selectedJobs") or []
    if not isinstance(selected_jobs, list):
        selected_jobs = []

    def _status(j):
        return str(j.get("status", "")).lower()

    today_jobs = [j for j in selected_jobs if isinstance(j, dict) and _is_today(j)]
    jobs_for_count = [j for j in today_jobs if _status(j) in STATUSES_TO_COUNT]
    jobs_for_email = [
        j for j in today_jobs
        if _status(j) in STATUSES_TO_EMAIL
    ]

    count = len(jobs_for_count)
    print(f"  {name} ({email})  mode={mode}  today={count}  emailable={len(jobs_for_email)}  force={force}")

    if (count < MIN_JOBS_TO_EMAIL and not force) or not jobs_for_email:
        return {
            "name": name,
            "email": email,
            "uid": uid,
            "pending_count": count,
            "emailed": False,
        }

    # Only jobs still staged as "pending_review" (send_jobbyo.py's status for
    # freshly-found jobs) need promoting to the plan's real status --
    # promote_jobs() upserts by job_url with a flat default_status, so
    # including anything already past that stage (e.g. an Auto Mode job the
    # apply bot has since moved to "applied") would incorrectly demote it
    # back down instead of leaving it alone.
    to_promote = [j for j in jobs_for_email if _status(j) == "pending_review" and _status(j) != new_status]
    if to_promote:
        promote_jobs(email, to_promote, new_status)

    emailed = send_email(user_profile, automation, jobs_for_email)

    return {
        "name": name,
        "email": email,
        "uid": uid,
        "jobs_promoted": len(jobs_for_email),
        "new_status": new_status,
        "emailed": emailed,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    single_email = None
    args = sys.argv[1:]
    force = "--force" in args
    if args and args[0].startswith("--email"):
        if "=" in args[0]:
            single_email = args[0].split("=", 1)[1].strip()
        elif len(args) > 1 and not args[1].startswith("--"):
            single_email = args[1].strip()

    print("=== approve_jobs.py ===")
    print(f"MIN_JOBS_TO_EMAIL={MIN_JOBS_TO_EMAIL}  force={force}")
    print()

    # Cleanup logs older than yesterday
    cleanup_old_emailed_logs()

    # Load UIDs already emailed today (dedup guard)
    emailed_today_uids = load_emailed_today() if not single_email else set()

    paid_users = get_paid_users()
    if not paid_users:
        print("No paid users found.")
        return

    if single_email:
        paid_users = [u for u in paid_users if u.get("email", "").lower() == single_email.lower()]
        if not paid_users:
            print(f"User {single_email} not found in paid users.")
            return

    emailed_users = []
    skipped_users = []
    pending_users = []

    for user in paid_users:
        uid = user.get("uid") or user.get("id") or ""
        email = user.get("email") or ""
        name = user.get("displayName") or email.split("@")[0]

        if uid and uid in emailed_today_uids:
            print(f"  {name} ({email}) — already emailed today, skipping")
            skipped_users.append({"name": name, "email": email})
            continue

        result = process_user(user, force=force)
        if result is None:
            continue

        if result["emailed"]:
            emailed_today_uids.add(uid)
            save_emailed_today(emailed_today_uids)
            emailed_users.append(result)
        else:
            pending_users.append(result)

    print()
    print("============================================================")
    print("APPROVE COMPLETE")
    print("============================================================")
    print(f"Emailed ({len(emailed_users)}):")
    for u in emailed_users:
        print(f"  ✅ {u['name']} ({u['email']})  {u['jobs_promoted']} jobs → {u['new_status']}")
    if skipped_users:
        print(f"Already emailed today ({len(skipped_users)}):")
        for u in skipped_users:
            print(f"  ⏭️  {u['name']} ({u['email']})")
    print(f"Still pending ({len(pending_users)}):")
    for u in pending_users:
        print(f"  🔴 {u['name']} ({u['email']})  {u['pending_count']}/{MIN_JOBS_TO_EMAIL} jobs")

    if not single_email:
        send_slack_summary(emailed_users, skipped_users, pending_users)


if __name__ == "__main__":
    main()
