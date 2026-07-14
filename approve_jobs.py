"""
approve_jobs.py — Promote pending_review jobs and send email notifications.

Run after send_jobbyo.py has staged jobs as pending_review.

For each paid user:
  - Fetch their selectedJobs
  - Find pending_review jobs
  - If ≥ MIN_JOBS_TO_EMAIL: promote to plan-appropriate status and send email
  - Send a Slack summary of who got emailed vs who is still waiting

Usage:
    python3 approve_jobs.py
    python3 approve_jobs.py --email user@example.com   # single user
"""

import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://fastapi-service-03-160893319817.europe-southwest1.run.app"
DAILY_REPORT_API_BASE = BASE_URL

MIN_JOBS_TO_EMAIL = 5          # minimum pending_review jobs before we send the email
PENDING_REVIEW_STATUS = "pending_review"
PREMIUM_STATUS = "pending"             # for premium plan users
MAX_STATUS = "waiting_approval"        # for max/starter plan users

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
# Slack report
# ---------------------------------------------------------------------------

def send_slack_report(emailed_users, pending_users):
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_results = []
    for u in emailed_users:
        user_results.append({
            "name": u["name"],
            "email": u["email"],
            "jobs_found_today": u["jobs_promoted"],
            "jobs_target": 10,
            "daily_report_sent": True,
            "needs_manual_search": False,
        })
    for u in pending_users:
        user_results.append({
            "name": u["name"],
            "email": u["email"],
            "jobs_found_today": u["pending_count"],
            "jobs_target": 10,
            "daily_report_sent": False,
            "needs_manual_search": True,
        })

    payload = {
        "run_date": run_date,
        "total_jobs_found": sum(u["jobs_promoted"] for u in emailed_users),
        "total_jobs_still_needed": sum(
            max(0, 10 - u.get("pending_count", 0)) for u in pending_users
        ),
        "users_processed": len(emailed_users) + len(pending_users),
        "emails_sent": len(emailed_users),
        "user_results": user_results,
    }

    res = _post(f"{DAILY_REPORT_API_BASE}/api/notifications/slack/daily-run", payload)
    if res is not None:
        print(f"Slack report sent  ({len(emailed_users)} emailed, {len(pending_users)} still pending)")


# ---------------------------------------------------------------------------
# Main
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
        "jobs_promoted": count,
        "new_status": new_status,
        "emailed": emailed,
    }


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
    pending_users = []

    for user in paid_users:
        result = process_user(user)
        if result is None:
            continue
        if result["emailed"]:
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
    print(f"Still pending ({len(pending_users)}):")
    for u in pending_users:
        print(f"  🔴 {u['name']} ({u['email']})  {u['pending_count']}/{MIN_JOBS_TO_EMAIL} jobs")

    if not single_email:
        send_slack_report(emailed_users, pending_users)


if __name__ == "__main__":
    main()
