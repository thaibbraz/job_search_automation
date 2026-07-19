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
DAILY_REPORT_API_BASE = BASE_URL

MIN_JOBS_TO_EMAIL = 8          # minimum pending_review jobs before we send the email
PENDING_REVIEW_STATUS = "pending_review"
PREMIUM_STATUS = "pending"             # for premium plan users
MAX_STATUS = "waiting_approval"        # for max/starter plan users

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
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
# Plan detection (mirrors send_jobbyo.py logic)
# ---------------------------------------------------------------------------

def get_user_plan(user, automation=None):
    text = " ".join([
        str(user.get("plan") or ""),
        str(user.get("planName") or ""),
        str(user.get("subscription") or ""),
        str((automation or {}).get("plan") or ""),
        str((automation or {}).get("planName") or ""),
    ]).lower()
    if "premium" in text:
        return "premium"
    if "starter" in text or "start" in text:
        return "starter"
    if "max" in text:
        return "max"
    return "max"   # safe default: hold for approval


def target_status(plan):
    if plan == "premium":
        return PREMIUM_STATUS
    return MAX_STATUS   # max / starter / unknown


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

def send_email(user_profile, automation, jobs):
    """Send the daily report email via the existing API endpoint."""
    email = user_profile.get("email") or ""
    name = user_profile.get("displayName") or email.split("@")[0]

    job_list = []
    for j in jobs:
        title = j.get("title", "")
        company = j.get("company", "")
        url = j.get("job_url") or j.get("url") or ""
        reason = (j.get("review_reason") or j.get("reason") or "Matched your profile").strip()
        if url:
            reason = reason + f"\n\n{url}"
        job_list.append({
            "job_title": f"{title} @ {company}" if company else title,
            "company": company,
            "url": url,
            "location": j.get("location", ""),
            "reason": reason,
            "score": j.get("grade") or j.get("review_confidence") or 0,
        })

    payload = {
        "email": email,
        "report": {
            "user_name": name,
            "jobs": job_list,
            "changed_rules": "",
            "next_batch_strategy": "",
            "pending_count": len(jobs),
            "needs_more": False,
        },
    }

    res = _post(f"{DAILY_REPORT_API_BASE}/api/reports/daily", payload)
    if res is not None:
        print(f"  Email sent → {email}  ({len(jobs)} jobs)")
        return True
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

    if emailed_users:
        lines.append(f"✅ *{len(emailed_users)} email(s) sent this run:*")
        for u in emailed_users:
            lines.append(f"  • {u['name']} ({u['email']}) — {u['jobs_promoted']} jobs → {u['new_status']}")
    else:
        lines.append("No new emails sent this run.")

    if skipped_users:
        names = ", ".join(u["name"] for u in skipped_users)
        lines.append(f"⏭️ *{len(skipped_users)} already emailed today* (skipped): {names}")

    if pending_users:
        lines.append(f"⚠️ *{len(pending_users)} below threshold ({MIN_JOBS_TO_EMAIL} jobs):*")
        for u in pending_users:
            lines.append(f"  • {u['name']} ({u['email']}) — {u['pending_count']}/{MIN_JOBS_TO_EMAIL} jobs")

    post_slack("\n".join(lines))


# ---------------------------------------------------------------------------
# Process one user
# ---------------------------------------------------------------------------

def process_user(user):
    uid = user.get("uid") or user.get("id") or ""
    email = user.get("email") or ""
    name = user.get("displayName") or email.split("@")[0]

    if not uid or not email:
        return None

    automation = get_automation(uid)
    user_profile = get_user_profile(uid) or user

    plan = get_user_plan(user, automation)
    new_status = target_status(plan)

    # Extract pending_review jobs from selectedJobs
    selected_jobs = automation.get("selectedJobs") or []
    if not isinstance(selected_jobs, list):
        selected_jobs = []

    pending_review_jobs = [
        j for j in selected_jobs
        if isinstance(j, dict) and str(j.get("status", "")).lower() == PENDING_REVIEW_STATUS
    ]

    count = len(pending_review_jobs)
    print(f"  {name} ({email})  plan={plan}  pending_review={count}")

    if count < MIN_JOBS_TO_EMAIL:
        return {
            "name": name,
            "email": email,
            "uid": uid,
            "pending_count": count,
            "emailed": False,
        }

    # Promote status
    promoted = promote_jobs(email, pending_review_jobs, new_status)
    if promoted is None:
        print(f"  WARNING: status promotion failed for {email}")

    # Send email
    emailed = send_email(user_profile, automation, pending_review_jobs)

    return {
        "name": name,
        "email": email,
        "uid": uid,
        "jobs_promoted": count,
        "new_status": new_status,
        "emailed": emailed,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    single_email = None
    args = sys.argv[1:]
    if args and args[0].startswith("--email"):
        if "=" in args[0]:
            single_email = args[0].split("=", 1)[1].strip()
        elif len(args) > 1:
            single_email = args[1].strip()

    print("=== approve_jobs.py ===")
    print(f"MIN_JOBS_TO_EMAIL={MIN_JOBS_TO_EMAIL}")
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

        result = process_user(user)
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
