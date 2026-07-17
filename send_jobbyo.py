#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — rely on env vars being set externally (GitHub Actions, shell export)


def env_truthy(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except Exception:
        print(f"WARNING: invalid integer for {name}={value!r}; using {default}")
        return default


# --nogpt is intentionally detected before full CLI parsing so the script can
# start without OPENAI_API_KEY when OpenAI is disabled. apply_cli_overrides()
# normalizes the final value and supports env/CLI aliases.
NO_GPT_MODE = (
    env_truthy("JOBBYO_NO_GPT")
    or any(arg in {"--nogpt", "--no-gpt", "--no-openai"} for arg in sys.argv[1:])
)


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://fastapi-service-03-160893319817.europe-southwest1.run.app"


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Fast search model.
SEARCH_MODEL = os.getenv("JOBBYO_SEARCH_MODEL", "gpt-4.1-mini")

# Strict review model. gpt-4.1 handles the classification tiers well at ~15× lower cost than gpt-5.5.
REVIEW_MODEL = os.getenv("JOBBYO_REVIEW_MODEL", "gpt-4.1")

# Resolver can use a stronger model because it replaces manual sourcing:
# company + exact title -> direct ATS/company URL. Keep configurable for GitHub Actions.
RESOLUTION_MODEL = os.getenv("JOBBYO_RESOLUTION_MODEL", "gpt-4.1")
SEARCH_CONTEXT_SIZE = os.getenv("JOBBYO_SEARCH_CONTEXT_SIZE", "high")
RESOLUTION_SEARCH_CONTEXT_SIZE = os.getenv("JOBBYO_RESOLUTION_SEARCH_CONTEXT_SIZE", "high")

# DRY_RUN=False means jobs will actually be posted.
DRY_RUN = False

# None = process all paid users with active automation.
# For testing, set to 1.
MAX_USERS_TO_PROCESS = None

# /users/paid is the source of truth for paid status.
# Keep True unless you want the script to re-check subscription.status and stripeId locally.
TRUST_USERS_PAID_ENDPOINT = True

# Optional targeted-user mode.
# You can run one or many users either by CLI or env:
#   python3 send_jobbyo.py --email user@example.com
#   python3 send_jobbyo.py --email user1@example.com user2@example.com
#   python3 send_jobbyo.py --emails user1@example.com,user2@example.com
#   python3 send_jobbyo.py --uid FirebaseUidHere
#   JOBBYO_SINGLE_USER_EMAIL=user@example.com python3 send_jobbyo.py
#   JOBBYO_SINGLE_USER_EMAILS=user1@example.com,user2@example.com python3 send_jobbyo.py
SINGLE_USER_EMAIL = os.getenv("JOBBYO_SINGLE_USER_EMAIL", "").strip() or None
SINGLE_USER_EMAILS = set()
SINGLE_USER_UID = os.getenv("JOBBYO_SINGLE_USER_UID", "").strip() or None

EXCLUDED_USER_EMAILS = {
    "zakin2time@gmail.com",
    "zachkolp.esl.japan@gmail.com",
    "rosejhickman@gmail.com",
    "merrill.latta@gmail.com",  # removed by request
}

# Users whose automation is forced active regardless of its status flag.
# Trialing users are also force-included automatically (see should_bypass_automation_check).
FORCE_USER_EMAILS = {
    "tayaspencercoo@gmail.com",
    "scxbrian@gmail.com",
    "maldonadomanny0907@gmail.com",
    "selahmosby@gmail.com",
}

# Target new-match jobs per user per day.
TARGET_JOBS_PER_USER = env_int("JOBBYO_TARGET_JOBS", 10)

# The second overnight round only runs for users still below this count after
# the first round AND only when Round 1 had enough live/direct-source signal.
MIN_JOBS_BEFORE_SECOND_ROUND = 10

# Smaller batches reduce junk/hallucinated results and reduce spend.
JOBS_PER_BATCH = 30

# Overnight search plan:
# - Round 1: up to 7 batches for every eligible user.
# - Round 2: up to 4 additional batches only when Round 1 shows enough live/direct-source supply.
FIRST_ROUND_BATCHES = 4
SECOND_ROUND_BATCHES = 2
MAX_ROUNDS_PER_USER = 3
MIN_ACCEPTABLE_JOBS_PER_USER = 5
MINIMUM_VIABLE_ROUND_BATCHES = 2
MIN_VIABLE_SAFE_FALLBACK_GRADE = 58
MIN_VIABLE_CONFIDENCE = 55

# Direct URL resolver: when search finds a promising mirror/job-board lead,
# spend a small extra call trying to find the real ATS/company URL.
MAX_DIRECT_RESOLUTION_ATTEMPTS_PER_BATCH = 2
MAX_REMOTE_FAILURE_RESOLUTION_ATTEMPTS_PER_BATCH = 2
MAX_DIRECT_RESOLUTION_CANDIDATES = 8
MIN_DIRECT_RESOLUTION_GRADE = 65
PENDING_REVIEW_MIN_GRADE = 66

# 15-day quota mode: temporary production bridge until full ATS inventory/scraper is built.
ENABLE_QUOTA_MODE = True
ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH = False
ENABLE_SHARED_DAILY_INVENTORY = False
CLUSTER_PREFETCH_JOBS_PER_CLUSTER = 24
MAX_AUTOMATION_TITLE_CLUSTERS = 14
MAX_PREFETCH_INVENTORY_JOBS = 350
INVENTORY_CANDIDATES_PER_USER = 45
INVENTORY_LOCAL_SCORE_MIN = 28
SAFE_FALLBACK_MIN_GRADE = 66
DISCOVERY_PENDING_MIN_GRADE = 62
DISCOVERY_PENDING_STATUS = "pending_review"

# Companies that consistently return 404s, surveys, or spam across all users.
# These are merged into rejected_companies at the start of each user run so the
# search model is explicitly told to never return them.
PERMANENTLY_BLOCKED_COMPANIES = {
    "towardjobs",
    "toward jobs",
    "skillerszone",
    "usasurveyjob",
    "usa survey job",
    "nogigshere",
    "nogigiddy",
}

# Hiring.cafe Apify integration — primary source before OpenAI fallback.
# Set JOBBYO_APIFY_TOKEN in .env to enable. Costs ~$0.25/user vs ~$0.70 for
# 7 OpenAI search batches, and returns direct ATS URLs (Ashby, Greenhouse, etc.)
APIFY_API_TOKEN = os.getenv("JOBBYO_APIFY_TOKEN", "")
APIFY_HIRING_CAFE_ACTOR_ID = "memo23~apify-hiring-cafe-scraper"
HIRING_CAFE_MAX_ITEMS = 50           # raw results fetched per user from HC
HIRING_CAFE_BATCH_SIZE = 25          # candidates consumed per batch
HIRING_CAFE_LOCAL_SCORE_MIN = 15     # same floor as Jobo/LinkedIn — AI review handles fit
ENABLE_HIRING_CAFE_PREFETCH = bool(APIFY_API_TOKEN)

JOBO_API_BASE = "https://connect.jobo.world"
JOBO_API_KEY = os.getenv("JOBO_API_KEY", "")
JOBO_ATS_MAX_ITEMS = 45           # raw results per call (was 30)
JOBO_LOCAL_SCORE_MIN = 15         # lower than HC — Jobo URLs are ATS-direct; AI review handles fit
ENABLE_JOBO_ATS_PREFETCH = bool(JOBO_API_KEY)

# Jobicy — free public API for remote jobs; used as discovery source only.
# Top-scored candidates are sent through the resolver to obtain ATS-direct URLs.

# LinkedIn via Apify — highest-priority source (direct ATS apply URLs, rich metadata)
# Reuses the same APIFY_API_TOKEN as Hiring Cafe.
APIFY_LINKEDIN_ACTOR_ID = "2rJKkhh7vjpX7pvjg"
LINKEDIN_MAX_ITEMS = 150
LINKEDIN_LOCAL_SCORE_MIN = 15     # same floor as Jobo — AI review handles fit
ENABLE_LINKEDIN_PREFETCH = bool(APIFY_API_TOKEN)

# No-GPT mode: replace OpenAI search/review with more structured inventory from
# Jobo ATS + HiringCafe, then use the same static URL, HTTP, duplicate, location,
# and posting pipeline. Defaults are intentionally higher because Jobbyo has a
# large monthly Jobo allowance. Override these in .env/GitHub Actions as needed.
NOGPT_HIRING_CAFE_MAX_ITEMS = env_int("JOBBYO_NOGPT_HIRING_CAFE_MAX_ITEMS", 120)
NOGPT_HIRING_CAFE_BATCH_SIZE = env_int("JOBBYO_NOGPT_HIRING_CAFE_BATCH_SIZE", 60)
NOGPT_HIRING_CAFE_KEYWORDS = env_int("JOBBYO_NOGPT_HIRING_CAFE_KEYWORDS", 3)
NOGPT_JOBO_PAGE_SIZE = env_int("JOBBYO_NOGPT_JOBO_PAGE_SIZE", 100)
NOGPT_JOBO_KEYWORDS = env_int("JOBBYO_NOGPT_JOBO_KEYWORDS", 8)
NOGPT_JOBO_MAX_CALLS_PER_USER = env_int("JOBBYO_NOGPT_JOBO_MAX_CALLS_PER_USER", 12)
NOGPT_JOBO_US_EXTRA_CALLS = env_int("JOBBYO_NOGPT_JOBO_US_EXTRA_CALLS", 6)
NOGPT_APPROVE_MIN_GRADE = env_int("JOBBYO_NOGPT_APPROVE_MIN_GRADE", 58)
NOGPT_APPROVE_MIN_GRADE_MINIMUM_VIABLE = env_int("JOBBYO_NOGPT_APPROVE_MIN_GRADE_MINIMUM_VIABLE", 52)
# Extra deterministic persona-fit gate for --nogpt mode.
# This is separate from source/link validation: a job must be live AND fit the
# user's automation/persona/search contract before it can be posted.
NOGPT_PERSONA_FIT_MIN_SCORE = env_int("JOBBYO_NOGPT_PERSONA_FIT_MIN_SCORE", 55)
NOGPT_PERSONA_FIT_MIN_SCORE_MINIMUM_VIABLE = env_int("JOBBYO_NOGPT_PERSONA_FIT_MIN_SCORE_MINIMUM_VIABLE", 48)

if NO_GPT_MODE:
    # Use much more inventory in no-GPT mode and avoid all OpenAI-powered
    # resolver/strategy calls. Jobo is the primary volume source; HiringCafe is
    # a helpful structured supplement when the Apify token is present.
    HIRING_CAFE_MAX_ITEMS = max(HIRING_CAFE_MAX_ITEMS, NOGPT_HIRING_CAFE_MAX_ITEMS)
    HIRING_CAFE_BATCH_SIZE = max(HIRING_CAFE_BATCH_SIZE, NOGPT_HIRING_CAFE_BATCH_SIZE)
    HIRING_CAFE_LOCAL_SCORE_MIN = min(HIRING_CAFE_LOCAL_SCORE_MIN, 18)
    JOBO_ATS_MAX_ITEMS = max(JOBO_ATS_MAX_ITEMS, NOGPT_JOBO_PAGE_SIZE)
    JOBO_LOCAL_SCORE_MIN = min(JOBO_LOCAL_SCORE_MIN, 8)
    MAX_DIRECT_RESOLUTION_ATTEMPTS_PER_BATCH = 0
    MAX_REMOTE_FAILURE_RESOLUTION_ATTEMPTS_PER_BATCH = 0
    ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH = False
    ENABLE_SHARED_DAILY_INVENTORY = False

DAILY_REPORT_API_BASE = "https://fastapi-service-03-160893319817.europe-southwest1.run.app"
# Override to a test inbox; set to "" to deliver to the actual user's email.
DAILY_REPORT_OVERRIDE_EMAIL = "hello@jobbyo.ai"

DAILY_PREFETCH_INVENTORY = []
DAILY_PREFETCH_META = {}

# Shared inventory is useful, but stale inventory can poison every user/batch.
# These sets are populated during a run when an inventory lead is proven dead,
# expired, generic, or redirected to a bad source. Candidate-specific rejects
# should not go here because they may still be valid for another user.
DAILY_BAD_INVENTORY_URLS = set()
DAILY_BAD_INVENTORY_COMPANY_TITLES = set()

# Search prompts do not need the full CV every batch once persona + search
# contract exist. Keep full CV for strict review only.
SEARCH_CV_CHAR_LIMIT = 5000

# Backward-compatible label used in a few logs/prompts. The actual per-round
# limit is passed into find_jobs_for_user/run_round.
MAX_BATCHES = FIRST_ROUND_BATCHES

# --- Cost estimation constants (USD) ---
# Hiring.cafe via Apify: $1.25 per 1,000 raw results
COST_PER_HC_RESULT = 0.00125
# Jobo ATS: $49.99/month ÷ 100,000 jobs/month
COST_PER_JOBO_RESULT = 0.0005
# LinkedIn via Apify: $0.60 per 1,000 results
COST_PER_LINKEDIN_RESULT = 0.0006
# OpenAI — gpt-4.1-mini + web_search per search batch (approx input+output tokens)
COST_PER_OPENAI_SEARCH_CALL = 0.05
# OpenAI — gpt-4.1 strict AI review per batch (approx ~1500 input + 200 output tokens)
COST_PER_OPENAI_REVIEW_CALL = 0.008
# OpenAI — gpt-4.1 + web_search per URL resolution attempt
COST_PER_OPENAI_RESOLVER_CALL = 0.02
# OpenAI — strategy pivot (2 calls: analysis + contract rewrite)
COST_PER_OPENAI_PIVOT_CALL = 0.06
# OpenAI — one-time persona creation (gpt-4.1-mini)
COST_PER_PERSONA_CREATE = 0.01
# OpenAI — one-time search contract creation (gpt-4.1-mini)
COST_PER_CONTRACT_CREATE = 0.01


def jobs_to_request(jobs_needed, round_mode="first_round"):
    """Return a smaller search target when only a few jobs are needed.

    Large JSON payloads were causing malformed JSON and lower-quality candidate
    batches, especially when a user only needed 1-2 more jobs. Keep enough
    surplus for validation failures, but do not ask the model for 30 jobs when
    the quota gap is tiny.
    """
    try:
        needed = int(jobs_needed)
    except Exception:
        needed = TARGET_JOBS_PER_USER

    if needed <= 2:
        return 8
    if needed <= 5:
        return 14
    if str(round_mode or "").lower() == "second_round":
        return 18
    return min(JOBS_PER_BATCH, 20)


# Maximum number of internal strategy pivots per user per run. Round 1 can create
# the first pivot after its final batch. Round 2 starts with the second pivot.
MAX_STRATEGY_PIVOTS_PER_USER = 1

REMOTE_TIMEOUT_SECONDS = 8
REMOTE_WORKERS = 14

# Fallback status. Actual posting status is plan-specific:
# - starter/max users: waiting_approval
# - premium users: pending
DEFAULT_STATUS = "waiting_approval"
STARTER_MAX_STATUS = "waiting_approval"
PREMIUM_STATUS = "pending"
LEGACY_PENDING_STATUSES = {"pending_review", "pending", "waiting_approval"}

# Only these AI review decisions will be posted.
REVIEW_APPROVED_DECISIONS = {"exact_match", "strong_adjacent", "safe_fallback", "good_match"}

# Minimum AI review confidence required before posting.
MIN_REVIEW_CONFIDENCE = 65

PERSONA_DIR = Path("./personas")
PERSONA_DIR.mkdir(exist_ok=True)

RUN_LOG_DIR = Path("./run_logs")
RUN_LOG_DIR.mkdir(exist_ok=True)

SEARCH_CONTRACT_DIR = Path("./search_contracts")
SEARCH_CONTRACT_DIR.mkdir(exist_ok=True)

STRATEGY_REPORT_DIR = Path("./strategy_reports")
STRATEGY_REPORT_DIR.mkdir(exist_ok=True)

if not OPENAI_API_KEY and not NO_GPT_MODE and not any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    raise RuntimeError("OPENAI_API_KEY environment variable is required unless --nogpt/JOBBYO_NO_GPT=1 is used.")
if OpenAI is None and not NO_GPT_MODE and not any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    raise RuntimeError("The openai package is required unless --nogpt/JOBBYO_NO_GPT=1 is used.")

client = None
if OPENAI_API_KEY and not NO_GPT_MODE and OpenAI is not None:
    client = OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=120.0,
        max_retries=1,
    )


# ============================================================
# ENDPOINTS
# ============================================================

def paid_users_url():
    return f"{BASE_URL}/users/paid"


def automation_url(uid):
    return f"{BASE_URL}/automations/users/{uid}"


def user_profile_url(uid):
    return f"{BASE_URL}/users/id/{uid}/"


def post_jobs_url(email):
    return f"{BASE_URL}/automations/users/by-email/{urllib.parse.quote(email)}/selected-jobs"


# ============================================================
# API HELPERS
# ============================================================

def api_get(url):
    res = requests.get(
        url,
        headers={"accept": "application/json"},
        timeout=60,
    )

    if res.status_code == 404:
        return None

    res.raise_for_status()
    return res.json()


def api_post_jobs(email, jobs, default_status=DEFAULT_STATUS):
    payload = {
        "jobs": jobs,
        "default_status": default_status,
    }

    print("\nPOSTING PAYLOAD:")
    print(json.dumps(payload, indent=2))

    if DRY_RUN:
        print("\nDRY_RUN=True, not posting.")
        return {
            "dry_run": True,
            "added_count": len(jobs),
            "skipped_count": 0,
            "payload": payload,
        }

    res = requests.post(
        post_jobs_url(email),
        json=payload,
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=90,
    )

    res.raise_for_status()
    return res.json()


# ============================================================
# DOMAIN HELPERS
# ============================================================

def clean_domain(domain):
    return str(domain or "").lower().replace("www.", "").strip()


def url_domain(url):
    try:
        return clean_domain(urllib.parse.urlparse(str(url or "")).netloc)
    except Exception:
        return ""


def domain_matches(domain, domain_set):
    domain = clean_domain(domain)

    for blocked in domain_set:
        blocked = clean_domain(blocked)

        if not blocked:
            continue

        if domain == blocked or domain.endswith("." + blocked):
            return True

    return False


# ============================================================
# JOB LINK FILTERS
# ============================================================

GLOBAL_BLOCKED_COMPANY_NAMES = {
    "mercor",   # talent aggregator / staffing platform
}

AGGREGATOR_DOMAINS = {
    # Major aggregators / search boards
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "careerbuilder.com",
    "simplyhired.com",
    "snagajob.com",
    "dice.com",
    "theladders.com",
    "fairygodboss.com",
    "careerjet.com",
    "grabjobs.co",
    "talent.com",
    "jooble.org",
    "zippia.com",
    "lensa.com",
    "jobgether.com",
    "tealhq.com",

    # Permanently blocked sources requested by Jobbyo
    "bebee.com",
    "usajobs.gov",
    "usajobs.com",
    "builtin.com",
    "builtinsf.com",
    "builtinnyc.com",
    "builtinseattle.com",
    "builtinaustin.com",
    "builtinboston.com",
    "builtinchicago.org",
    "builtindenver.com",
    "builtindc.com",
    "builtinla.com",
    "builtincolorado.com",

    # Remote/job-board mirrors
    "remotejobs.org",
    "dailyremote.com",
    "remotive.com",
    "flexjobs.com",
    "echojobs.io",
    "remote.co",
    "weworkremotely.com",
    "workingnomads.com",
    "wellfound.com",
    "angel.co",
    "otta.com",
    "cord.co",
    "workatastartup.com",
    "startup.jobs",
    "dynamitejobs.com",
    "jobspresso.co",
    "remoteok.com",
    "himalayas.app",
    "jobicy.com",
    "virtualvocations.com",

    # HiringCafe is allowed only as discovery, never as the final job URL.
    "hiring.cafe",
    "hiringcafe.com",

    # Search-result aggregators confirmed to post category/listing pages as jobs.
    "jobright.ai",
    "www.jobright.ai",

    # Survey/gig spam sources that consistently produce 404s or non-jobs.
    "towardjobs.com",
    "www.towardjobs.com",
    "skillerszone.com",
    "www.skillerszone.com",
}
KNOWN_DIRECT_ATS_DOMAINS = {
    "jobs.lever.co",
    "lever.co",
    "jobs.ashbyhq.com",
    "ashbyhq.com",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "greenhouse.io",
    "apply.workable.com",
    "jobs.workable.com",
    "workable.com",
    "jobs.smartrecruiters.com",
    "careers.smartrecruiters.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "wd1.myworkdaysite.com",
    "wd3.myworkdaysite.com",
    "wd5.myworkdaysite.com",
    "workdayjobs.com",
    "workforcenow.adp.com",
    "adp.com",
    "jobs.dayforcehcm.com",
    "dayforcehcm.com",
    "jobs.jobvite.com",
    "jobvite.com",
    "ats.rippling.com",
    "rippling.com",
    "jobs.bamboohr.com",
    "bamboohr.com",
    "careers.icims.com",
    "icims.com",
    "recruiting.paylocity.com",
    "paylocity.com",
    "paycomonline.net",
    "paycom.com",
    "applytojob.com",
    "recruiting.ultipro.com",
    "recruiting2.ultipro.com",
    "ukg.net",
    "taleo.net",
    "oraclecloud.com",
    "successfactors.com",
    "avature.net",
    "eightfold.ai",
    "pinpointhq.com",
    "personio.com",
    "recruitee.com",
    "teamtailor.com",
    "breezy.hr",
    "jazz.co",
    "trakstar.com",
    "hire.trakstar.com",
    "fountain.com",
    "comeet.com",
    "recruiterbox.com",
    "jobadder.com",
    "hrmdirect.com",
    "applicantpro.com",
    "isolvedhire.com",
    "polymer.co",
    "join.com",
    "jobsoid.com",
    "jobs.jobsoid.com",
    "hireology.com",
    "workstream.us",
    "clearcompany.com",
    "gohire.io",
    "jobs.gohire.io",
    "careers-page.com",
    "peoplehr.net",
    "eploy.net",
    "ciphr-irecruit.com",
    "hirebridge.com",
    "catsone.com",
    "loxo.co",
    "app.loxo.co",
    "cvviz.com",
    "jobs.cvviz.com",
    "silkroad.com",
    "jobs.silkroad.com",
    "ourcareerpages.com",
    "jobs.ourcareerpages.com",
    "careers.hireology.com",
    "jobs.crelate.com",
    "apply.catsone.com",
    "recruitingbypaycor.com",
    "paycor.com",
    "talentreef.com",
    "my.peoplematter.com",
    "harri.com",
    "workpop.com",
    "careers.adp.com",
    "jobs.localjobnetwork.com",
    "jobtarget.com",
    "hirevouch.com",
    "freshteam.com",
    "jobs.freshteam.com",
}

DIRECT_COMPANY_CAREER_DOMAINS = {
    "amazon.jobs",
    "jobs.amazon.com",
    "careers.homedepot.com",
    "jobs.target.com",
    "jobs.walmart.com",
    "careers.walmart.com",
    "jobs.lowes.com",
    "careers.fedex.com",
    "jobs.ups.com",
    "careers.dhl.com",
    "careers.grainger.com",
    "jobs.bestbuy.com",
    "bestbuy-jobs.com",
    "careers.wayfair.com",
    "www.wayfair.com",
    "careers.hdsupply.com",
    "careers.costco.com",
    "jobs.kroger.com",
    "careers.aldi.us",
    "jobs.cvshealth.com",
    "jobs.walgreens.com",
    "careers.chewy.com",
    "careers.autozone.com",
    "careers.oreillyauto.com",
    "careers.advanceautoparts.com",
    "jobs.cintas.com",
    "careers.usfoods.com",
    "careers.sysco.com",
    "careers.xpo.com",
    "jobs.ryder.com",
    "jobs.gopuff.com",
    "careers.dollargeneral.com",
    "jobs.dollartree.com",
    "careers.petsmart.com",
    "careers.petco.com",
    "careers.nordstrom.com",
    "jobs.sephora.com",
    "careers.lululemon.com",
    "careers.nike.com",
    "careers.adidas-group.com",
}

# Extra mirror/job-board domains that are useful only for discovery. They should
# never be posted as final URLs. If they produce a promising lead, the direct URL
# resolver tries to find the company's ATS/career-page job instead.
ADDITIONAL_BLOCKED_FINAL_DOMAINS = {
    "remoteo.es",
    "reactremotejobs.com",
    "peopleinai.com",
    "pyjobs.com",
    "hubmub.com",
    "recruit.net",
    "ycombinator.com",
    "ycwork.com",
    "remoterocketship.com",
    "spainjobs.io",
    "trabajas.es",
    "yardcorporate.com",
    "dailyremote.com",
    "remotejobs.org",
    "otta.com",
    "jobgether.com",
    "jobs.earlybird.com",
    "earlybird.com",
    "hiring.cafe",
    "hiringcafe.com",
    "comunidad.es.python.org",
    "python.org",
    "discuss.python.org",
    "forum.djangoproject.com",
    "reddit.com",
    "news.ycombinator.com",
    "lobste.rs",
    "medium.com",
    "substack.com",
    "dev.to",
}

DISCOVERY_ONLY_DOMAINS = set(AGGREGATOR_DOMAINS) | ADDITIONAL_BLOCKED_FINAL_DOMAINS

AGGREGATOR_DOMAINS.update(ADDITIONAL_BLOCKED_FINAL_DOMAINS)

# Never blacklist broad ATS providers as domains because one expired Lever/Ashby/
# Greenhouse URL should not poison the whole source. Track exact failed URLs
# instead.
NON_BLOCKABLE_DIRECT_SOURCE_DOMAINS = KNOWN_DIRECT_ATS_DOMAINS | DIRECT_COMPANY_CAREER_DOMAINS


def is_non_blockable_direct_source_domain(domain):
    return domain_matches(domain, NON_BLOCKABLE_DIRECT_SOURCE_DOMAINS)


def is_blocked_final_domain(domain):
    return domain_matches(domain, AGGREGATOR_DOMAINS)


def is_mirror_or_low_quality_source(job):
    return is_blocked_final_domain(url_domain(canonical_job_url(job)))


def direct_resolution_lead_score(job):
    """Cheap score for deciding whether a rejected discovery result is worth
    spending a resolver call on. The search model sometimes returns bad grade
    scales (0/3/5), so combine normalized grade with title/location/company
    signals instead of trusting grade alone.
    """
    title = str(job.get("title", "")).lower()
    company = str(job.get("company", "")).lower()
    location = str(job.get("location", "")).lower()
    description = str(job.get("description", "")).lower()
    blob = f"{title} {company} {location} {description}"

    score = normalize_grade(job.get("grade", 0))

    positive_markers = [
        "founding", "co-founder", "cofounder", "backend", "python",
        "full-stack", "full stack", "ai engineer", "llm", "rag",
        "product engineer", "software engineer", "startup", "equity",
        "remote", "remote europe", "remote spain", "barcelona", "portugal",
        "ireland", "worldwide", "international contractor",
    ]
    negative_markers = [
        "sales", "go-to-market", "commercial", "marketing", "support",
        "frontend only", "react native", "ios", "android",
    ]

    score += 8 * sum(1 for marker in positive_markers if marker in blob)
    score -= 12 * sum(1 for marker in negative_markers if marker in blob)

    return max(0, min(100, score))


def should_attempt_direct_resolution(job, reason):
    reason = str(reason or "")
    if not str(job.get("company", "")).strip() or not str(job.get("title", "")).strip():
        return False

    resolvable_reason = reason in {
        "aggregator_page",
        "generic_or_homepage_url",
        "search_page",
        "career_homepage",
        "wrong_or_generic_job_page",
        "404_not_found",
        "expired",
        "soft_404_or_not_found",
        "dead_job_url_pattern",
        "wrong_job",
        "redirected_to_aggregator",
    } or is_mirror_or_low_quality_source(job)

    if not resolvable_reason:
        return False

    return direct_resolution_lead_score(job) >= MIN_DIRECT_RESOLUTION_GRADE

ATS_SEARCH_PATTERNS = [
    'site:jobs.lever.co "{target_title}" "{location_or_remote}"',
    'site:lever.co "{target_title}" "{location_or_remote}"',
    'site:jobs.ashbyhq.com "{target_title}" "{location_or_remote}"',
    'site:ashbyhq.com "{target_title}" "{location_or_remote}"',
    'site:job-boards.greenhouse.io "{target_title}" "{location_or_remote}"',
    'site:boards.greenhouse.io "{target_title}" "{location_or_remote}"',
    'site:job-boards.eu.greenhouse.io "{target_title}" "{location_or_remote}"',
    'site:greenhouse.io "{target_title}" "{location_or_remote}"',
    'site:apply.workable.com "{target_title}" "{location_or_remote}"',
    'site:jobs.workable.com "{target_title}" "{location_or_remote}"',
    'site:workable.com "{target_title}" "{location_or_remote}"',
    'site:jobs.smartrecruiters.com "{target_title}" "{location_or_remote}"',
    'site:careers.smartrecruiters.com "{target_title}" "{location_or_remote}"',
    'site:smartrecruiters.com "{target_title}" "{location_or_remote}"',
    'site:myworkdayjobs.com "{target_title}" "{location_or_remote}"',
    'site:wd1.myworkdaysite.com "{target_title}" "{location_or_remote}"',
    'site:wd3.myworkdaysite.com "{target_title}" "{location_or_remote}"',
    'site:wd5.myworkdaysite.com "{target_title}" "{location_or_remote}"',
    'site:workdayjobs.com "{target_title}" "{location_or_remote}"',
    'site:workforcenow.adp.com "{target_title}" "{location_or_remote}"',
    'site:adp.com "{target_title}" "{location_or_remote}"',
    'site:jobs.dayforcehcm.com "{target_title}" "{location_or_remote}"',
    'site:dayforcehcm.com "{target_title}" "{location_or_remote}"',
    'site:jobs.jobvite.com "{target_title}" "{location_or_remote}"',
    'site:jobvite.com "{target_title}" "{location_or_remote}"',
    'site:ats.rippling.com "{target_title}" "{location_or_remote}"',
    'site:rippling.com "{target_title}" "{location_or_remote}"',
    'site:jobs.bamboohr.com "{target_title}" "{location_or_remote}"',
    'site:bamboohr.com "{target_title}" "{location_or_remote}"',
    'site:careers.icims.com "{target_title}" "{location_or_remote}"',
    'site:icims.com "{target_title}" "{location_or_remote}"',
    'site:recruiting.paylocity.com "{target_title}" "{location_or_remote}"',
    'site:paylocity.com "{target_title}" "{location_or_remote}"',
    'site:paycomonline.net "{target_title}" "{location_or_remote}"',
    'site:paycom.com "{target_title}" "{location_or_remote}"',
    'site:applytojob.com "{target_title}" "{location_or_remote}"',
    'site:recruiting.ultipro.com "{target_title}" "{location_or_remote}"',
    'site:recruiting2.ultipro.com "{target_title}" "{location_or_remote}"',
    'site:ukg.net "{target_title}" "{location_or_remote}"',
    'site:taleo.net "{target_title}" "{location_or_remote}"',
    'site:oraclecloud.com "{target_title}" "{location_or_remote}"',
    'site:successfactors.com "{target_title}" "{location_or_remote}"',
    'site:avature.net "{target_title}" "{location_or_remote}"',
    'site:eightfold.ai "{target_title}" "{location_or_remote}"',
    'site:pinpointhq.com "{target_title}" "{location_or_remote}"',
    'site:personio.com "{target_title}" "{location_or_remote}"',
    'site:recruitee.com "{target_title}" "{location_or_remote}"',
    'site:teamtailor.com "{target_title}" "{location_or_remote}"',
    'site:breezy.hr "{target_title}" "{location_or_remote}"',
    'site:jazz.co "{target_title}" "{location_or_remote}"',
    'site:trakstar.com "{target_title}" "{location_or_remote}"',
    'site:hire.trakstar.com "{target_title}" "{location_or_remote}"',
    'site:fountain.com "{target_title}" "{location_or_remote}"',
    'site:comeet.com "{target_title}" "{location_or_remote}"',
    'site:recruiterbox.com "{target_title}" "{location_or_remote}"',
    'site:jobadder.com "{target_title}" "{location_or_remote}"',
    'site:hrmdirect.com "{target_title}" "{location_or_remote}"',
    'site:applicantpro.com "{target_title}" "{location_or_remote}"',
    'site:isolvedhire.com "{target_title}" "{location_or_remote}"',
    'site:polymer.co "{target_title}" "{location_or_remote}"',
    'site:join.com "{target_title}" "{location_or_remote}"',
    'site:jobsoid.com "{target_title}" "{location_or_remote}"',
    'site:jobs.jobsoid.com "{target_title}" "{location_or_remote}"',
    'site:hireology.com "{target_title}" "{location_or_remote}"',
    'site:workstream.us "{target_title}" "{location_or_remote}"',
    'site:clearcompany.com "{target_title}" "{location_or_remote}"',
    'site:gohire.io "{target_title}" "{location_or_remote}"',
    'site:jobs.gohire.io "{target_title}" "{location_or_remote}"',
    'site:careers-page.com "{target_title}" "{location_or_remote}"',
    'site:peoplehr.net "{target_title}" "{location_or_remote}"',
    'site:eploy.net "{target_title}" "{location_or_remote}"',
    'site:ciphr-irecruit.com "{target_title}" "{location_or_remote}"',
    'site:hirebridge.com "{target_title}" "{location_or_remote}"',
    'site:catsone.com "{target_title}" "{location_or_remote}"',
    'site:loxo.co "{target_title}" "{location_or_remote}"',
    'site:app.loxo.co "{target_title}" "{location_or_remote}"',
    'site:cvviz.com "{target_title}" "{location_or_remote}"',
    'site:jobs.cvviz.com "{target_title}" "{location_or_remote}"',
    'site:silkroad.com "{target_title}" "{location_or_remote}"',
    'site:jobs.silkroad.com "{target_title}" "{location_or_remote}"',
    'site:ourcareerpages.com "{target_title}" "{location_or_remote}"',
    'site:jobs.ourcareerpages.com "{target_title}" "{location_or_remote}"',
    'site:careers.hireology.com "{target_title}" "{location_or_remote}"',
    'site:jobs.crelate.com "{target_title}" "{location_or_remote}"',
    'site:apply.catsone.com "{target_title}" "{location_or_remote}"',
    'site:recruitingbypaycor.com "{target_title}" "{location_or_remote}"',
    'site:paycor.com "{target_title}" "{location_or_remote}"',
    'site:talentreef.com "{target_title}" "{location_or_remote}"',
    'site:my.peoplematter.com "{target_title}" "{location_or_remote}"',
    'site:harri.com "{target_title}" "{location_or_remote}"',
    'site:workpop.com "{target_title}" "{location_or_remote}"',
    'site:careers.adp.com "{target_title}" "{location_or_remote}"',
    'site:jobs.localjobnetwork.com "{target_title}" "{location_or_remote}"',
    'site:jobtarget.com "{target_title}" "{location_or_remote}"',
    'site:hirevouch.com "{target_title}" "{location_or_remote}"',
    'site:freshteam.com "{target_title}" "{location_or_remote}"',
    'site:jobs.freshteam.com "{target_title}" "{location_or_remote}"',
    'site:amazon.jobs "{target_title}" "{location_or_remote}"',
    'site:careers.homedepot.com "{target_title}" "{location_or_remote}"',
    'site:jobs.target.com "{target_title}" "{location_or_remote}"',
    'site:jobs.walmart.com "{target_title}" "{location_or_remote}"',
    'site:jobs.lowes.com "{target_title}" "{location_or_remote}"',
    'site:careers.fedex.com "{target_title}" "{location_or_remote}"',
    'site:jobs.ups.com "{target_title}" "{location_or_remote}"',
    'site:careers.dhl.com "{target_title}" "{location_or_remote}"',
    'site:careers.grainger.com "{target_title}" "{location_or_remote}"',
    'site:jobs.bestbuy.com "{target_title}" "{location_or_remote}"',
    'site:careers.wayfair.com "{target_title}" "{location_or_remote}"',
    'site:careers.hdsupply.com "{target_title}" "{location_or_remote}"',
    'site:jobs.cvshealth.com "{target_title}" "{location_or_remote}"',
    'site:jobs.walgreens.com "{target_title}" "{location_or_remote}"',
    'site:jobs.cintas.com "{target_title}" "{location_or_remote}"',
    'site:careers.xpo.com "{target_title}" "{location_or_remote}"',
    'site:jobs.ryder.com "{target_title}" "{location_or_remote}"',
]
EXPIRED_MARKERS = [
    "job not found",
    "posting no longer available",
    "no longer accepting applications",
    "position has been filled",
    "this job is no longer available",
    "this posting has expired",
    "job has expired",
    "we couldn't find this job",
    "the job you are looking for is no longer available",
    "not currently accepting applications",
    "this position is no longer open",
    "this role is no longer open",
    "no longer open for applications",
    "this job is no longer active",
]

CAREER_HOME_MARKERS = [
    "current openings",
    "all jobs",
    "search jobs",
    "create a job alert",
    "view all jobs",
    "open positions",
]

# Soft-404 and closed-job indicators. These catch ATS pages that return HTTP 200
# while the underlying job is gone, expired, or redirected to a not-found page.
SOFT_404_MARKERS = [
    "404 not found",
    "page not found",
    "job not found",
    "job posting not found",
    "posting not found",
    "opening not found",
    "position not found",
    "requisition not found",
    "this page could not be found",
    "the page you requested could not be found",
    "the page you are looking for could not be found",
    "we can't find the page",
    "we could not find the page",
    "sorry, this job could not be found",
    "sorry, this posting could not be found",
    "no job found",
    "no longer exists",
]

DEAD_JOB_URL_PATH_MARKERS = [
    "/404",
    "/not-found",
    "/notfound",
    "/job-not-found",
    "/jobs-not-found",
    "/job-expired",
    "/expired",
    "/closed",
]

LINK_FAILURE_REASONS = {
    "404_not_found",
    "expired",
    "dead_job_url_pattern",
    "soft_404_or_not_found",
    "wrong_or_generic_job_page",
    "wrong_job",
    "career_homepage",
    "search_page",
    "generic_or_homepage_url",
}


def looks_like_dead_job_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        path = urllib.parse.unquote(parsed.path or "").lower()
    except Exception:
        return False

    return any(marker in path for marker in DEAD_JOB_URL_PATH_MARKERS)


def response_looks_like_dead_job(body, final_url=""):
    body = (body or "").lower()

    if looks_like_dead_job_url(final_url):
        return True

    return any(marker in body for marker in SOFT_404_MARKERS)


GENERIC_JOB_PAGE_SLUGS = {
    "", "careers", "career", "jobs", "job", "openings", "opening",
    "positions", "position", "roles", "role", "apply", "application",
    "opportunities", "work-with-us", "join-us", "join", "hiring",
    "search", "all-jobs", "open-positions", "job-search", "jobsearch",
}

SPECIFIC_COMPANY_JOB_PATH_PREFIXES = {
    "careers", "career", "jobs", "job", "openings", "opening",
    "positions", "position", "roles", "role", "apply", "application",
    "opportunities", "o", "vacancies", "vacancy", "work-with-us",
}


def path_segments(url_or_path):
    try:
        parsed = urllib.parse.urlparse(str(url_or_path or ""))
        path = parsed.path if parsed.scheme or parsed.netloc else str(url_or_path or "")
    except Exception:
        path = str(url_or_path or "")

    return [
        re.sub(r"[^a-z0-9-]+", "", part.lower())
        for part in urllib.parse.unquote(path).split("/")
        if part.strip()
    ]


def looks_like_specific_company_job_url(url):
    """Allow real company career pages that do not use classic ATS job-id
    patterns. Examples from latest run: parser.bamboohr.com/careers/34,
    careers.traceair.net/o/senior-python-backendfull-stack-developer,
    matcha.fm/apply/be, marsbased.com/es/jobs/python-backend-engineer.

    This is still conservative: it rejects generic careers/search/list pages and
    final URLs on blocked discovery-only domains. Remote validation later confirms
    the page mentions the title/company and is not expired.
    """
    if not url:
        return False

    domain = url_domain(url)
    if not domain or is_blocked_final_domain(domain):
        return False

    try:
        parsed = urllib.parse.urlparse(str(url))
        query = parsed.query.lower()
    except Exception:
        query = ""

    segments = path_segments(url)
    if not segments:
        return False

    # BambooHR often uses /careers/34?source=... for specific jobs.
    if domain.endswith("bamboohr.com") and len(segments) >= 2 and segments[-1].isdigit():
        return True

    # Common job-id query parameters, even on company-hosted career pages.
    if any(key in query for key in [
        "jobid=", "job_id=", "reqid=", "req_id=", "postingid=",
        "jobpostingid=", "requisitionid=", "job=", "id="
    ]):
        return True

    # Strip locale prefixes like /en/jobs/foo or /es/careers/foo.
    core = segments[:]
    if core and len(core[0]) in {2, 3, 5} and core[0] not in SPECIFIC_COMPANY_JOB_PATH_PREFIXES:
        core = core[1:]

    if not core:
        return False

    first = core[0]
    last = core[-1]

    if first in SPECIFIC_COMPANY_JOB_PATH_PREFIXES and len(core) >= 2:
        if last not in GENERIC_JOB_PAGE_SLUGS and (len(last) >= 2 or last.isdigit()):
            return True

    # Some companies use /department/title or /team/title with no jobs prefix.
    if len(core) >= 2:
        joined = "-".join(core[-2:])
        jobish_markers = [
            "engineer", "developer", "software", "backend", "frontend",
            "full-stack", "fullstack", "machine-learning", "ml", "ai",
            "data", "product", "founding", "technical", "lead", "senior",
        ]
        if any(marker in joined for marker in jobish_markers) and last not in GENERIC_JOB_PAGE_SLUGS:
            return True

    return False


def normalize_url(url):
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url.strip())
    netloc = clean_domain(parsed.netloc)
    path = re.sub(r"/+$", "", urllib.parse.unquote(parsed.path))

    keep_query = []
    for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in {
            "gh_jid",
            "jobid",
            "job_id",
            "reqid",
            "req_id",
            "token",
            "ashby_jid",
        }:
            keep_query.append((k, v))

    query_text = urllib.parse.urlencode(keep_query)

    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            path,
            "",
            query_text,
            "",
        )
    )


def company_title_key(job):
    company = str(job.get("company", "")).strip().lower()
    title = str(job.get("title", "")).strip().lower()

    company = re.sub(r"\s+", " ", company)
    title = re.sub(r"\s+", " ", title)

    return company, title


def has_job_identifier(url):
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    query = parsed.query.lower()

    patterns = [
        r"/jobs/\d{5,}",
        r"/jobs/[0-9a-fA-F-]{32,}",
        r"/j/[A-Z0-9]{8,}/?",
        r"/view/[A-Za-z0-9]{10,}/?",
        r"/jobs/listing/[^/]+/\d+",
        r"/[0-9]{12,}-[a-z0-9-]+",
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        r"R-\d{5,}",
        r"R0\d{5,}",
        r"_R\d{5,}",
        r"_R0\d{5,}",
        r"JR\d{5,}",
        r"_JR\d{5,}",
        r"/jobs/\d+\.html",
        r"/job/[A-Za-z0-9-]+",
        r"/jobs/[A-Za-z0-9][A-Za-z0-9-]{7,}",
        r"/careers/[A-Za-z0-9][A-Za-z0-9-]{7,}",
        r"/companies/[A-Za-z0-9-]+/\d{5,}-[A-Za-z0-9-]+",
        r"/careers/job/[A-Za-z0-9-]+",
        r"/jobs/fk[a-z0-9]+",
        r"/careers/[A-Za-z0-9-]+/[A-Za-z0-9-]+",
        r"/position/[A-Za-z0-9-]+",
        r"/openings/[A-Za-z0-9-]+",
        r"/job-detail/[A-Za-z0-9-]+",
        r"/jobdetails/[A-Za-z0-9-]+",
        r"/requisition/[A-Za-z0-9-]+",
        r"/posting/[A-Za-z0-9-]+",
    ]

    if any(re.search(p, path, re.IGNORECASE) for p in patterns):
        return True

    if any(
        x in query
        for x in [
            "gh_jid=",
            "jobid=",
            "job_id=",
            "reqid=",
            "req_id=",
            "token=",
            "ashby_jid=",
            "jobpostingid=",
            "postingid=",
            "requisitionid=",
        ]
    ):
        return True

    return False


def normalize_grade(value):
    try:
        grade = int(float(value))
    except Exception:
        return 75

    # Safety: sometimes the review model returns a 1-5 or 1-10 score.
    if 1 <= grade <= 5:
        return grade * 20

    if 6 <= grade <= 10:
        return grade * 10

    if grade < 1:
        return 1

    if grade > 100:
        return 100

    return grade


def static_check(
    job,
    existing_urls,
    existing_company_titles,
    seen_urls,
    seen_company_titles,
):
    url = str(job.get("job_url", "")).strip()
    title = str(job.get("title", "")).strip()
    company = str(job.get("company", "")).strip()

    if not title or not company or not url:
        return False, "missing_title_company_or_url"

    parsed = urllib.parse.urlparse(url)

    if parsed.scheme != "https" or not parsed.netloc:
        return False, "invalid_url"

    domain = clean_domain(parsed.netloc)

    if looks_like_dead_job_url(url):
        return False, "dead_job_url_pattern"

    if company.lower().strip() in GLOBAL_BLOCKED_COMPANY_NAMES:
        return False, "blocked_company"

    if domain_matches(domain, AGGREGATOR_DOMAINS):
        return False, "aggregator_page"

    path_lower = urllib.parse.unquote(parsed.path.lower())
    query_lower = parsed.query.lower()

    if any(
        x in path_lower
        for x in [
            "/search",
            "/jobs/search",
            "/careers/search",
            "/job-search",
            "/jobsearch",
            "/all-jobs",
            "/open-positions",
        ]
    ):
        return False, "search_page"

    if any(x in query_lower for x in ["keyword=", "search=", "q="]) and "gh_jid=" not in query_lower:
        return False, "search_page"

    if domain.endswith("greenhouse.io") and "/jobs/" not in path_lower and "gh_jid=" not in query_lower:
        return False, "career_homepage"

    if domain == "jobs.lever.co" and len([p for p in parsed.path.split("/") if p]) < 2:
        return False, "career_homepage"

    if domain == "jobs.ashbyhq.com" and not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path_lower,
    ):
        return False, "career_homepage"

    if domain == "apply.workable.com" and "/j/" not in path_lower:
        return False, "career_homepage"

    if domain == "jobs.workable.com" and "/view/" not in path_lower:
        return False, "career_homepage"

    is_prefetch_job = str(job.get("source", "")).startswith(("hiring_cafe", "jobo_ats", "linkedin_apify"))
    if not is_prefetch_job and not has_job_identifier(url) and not looks_like_specific_company_job_url(url):
        return False, "generic_or_homepage_url"

    norm = normalize_url(url)

    if norm in existing_urls:
        return False, "duplicate_existing_url"

    if norm in seen_urls:
        return False, "duplicate_batch_url"

    ct = company_title_key(job)

    if ct in existing_company_titles:
        return False, "duplicate_existing_company_title"

    if ct in seen_company_titles:
        return False, "duplicate_batch_company_title"

    seen_urls.add(norm)
    seen_company_titles.add(ct)

    return True, "static_ok"


def remote_check(job):
    url = job["job_url"]

    if looks_like_dead_job_url(url):
        return False, "dead_job_url_pattern"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    last_error = None

    for attempt in range(1, 3):
        try:
            res = requests.get(
                url,
                headers=headers,
                timeout=REMOTE_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            break
        except Exception as e:
            last_error = e
            if attempt == 1:
                time.sleep(0.5)
                continue
            return False, f"broken_or_timeout: {str(last_error)[:80]}"

    if res.status_code == 404:
        return False, "404_not_found"

    if res.status_code == 410:
        return False, "expired"

    # Keep rejecting 403 as requested.
    if res.status_code in {401, 403}:
        return False, f"blocked_or_forbidden_{res.status_code}"

    if res.status_code >= 400:
        return False, f"http_{res.status_code}"

    final_url = res.url
    final_domain = clean_domain(urllib.parse.urlparse(final_url).netloc)

    if looks_like_dead_job_url(final_url):
        return False, "dead_job_url_pattern"

    if domain_matches(final_domain, AGGREGATOR_DOMAINS):
        return False, "redirected_to_aggregator"

    body = res.text.lower()[:650000]

    if response_looks_like_dead_job(body, final_url=final_url):
        return False, "soft_404_or_not_found"

    if any(marker in body for marker in EXPIRED_MARKERS):
        return False, "expired"

    if not has_job_identifier(final_url) and not looks_like_specific_company_job_url(final_url):
        if any(marker in body for marker in CAREER_HOME_MARKERS):
            return False, "career_homepage"
        return False, "wrong_or_generic_job_page"

    title_tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", job.get("title", "").lower())
        if len(t) >= 4
    ]

    company_tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", job.get("company", "").lower())
        if len(t) >= 4
    ]

    title_hits = sum(1 for t in title_tokens[:8] if t in body)
    company_hits = sum(1 for t in company_tokens[:5] if t in body)

    # Direct ATS URLs are still rejected if the page is a soft-404, expired,
    # wrong job, or does not mention the title/company at all. This is the main
    # 404-avoidance guard before any job can reach AI review or posting.
    if domain_matches(final_domain, KNOWN_DIRECT_ATS_DOMAINS) and res.status_code < 400:
        if title_hits >= 1 or company_hits >= 1:
            return True, "live"

    if title_hits >= 1 or company_hits >= 1:
        return True, "live"

    if any(marker in body for marker in CAREER_HOME_MARKERS):
        return False, "career_homepage"

    return False, "wrong_job"


# ============================================================
# USER / AUTOMATION HELPERS
# ============================================================

def get_paid_users():
    return api_get(paid_users_url()) or []


def get_user_automation(uid):
    data = api_get(automation_url(uid))

    if isinstance(data, list):
        if not data:
            return None

        # Prefer an active, non-draft automation if the API returns multiple.
        # Some users may have old/draft automations first in the list.
        for automation in data:
            if has_active_automation(automation):
                return automation

        # Fall back to the first record so debug output can explain why it failed.
        return data[0]

    return data


def get_user_profile(uid):
    return api_get(user_profile_url(uid))


def has_active_automation(automation):
    if not automation:
        return False

    if automation.get("isDraft") is True:
        return False

    return automation.get("isActive") is True

def should_bypass_automation_check(user, email):
    """True for trialing users and anyone in FORCE_USER_EMAILS."""
    subscription = (user or {}).get("subscription") or {}
    if subscription.get("status") == "trialing":
        return True
    return normalize_selected_email(email) in {normalize_selected_email(e) for e in FORCE_USER_EMAILS}


def automation_debug_summary(automation):
    if not automation:
        return {"exists": False}

    return {
        "exists": True,
        "id": automation.get("id") or automation.get("automationId") or automation.get("uid"),
        "isActive": automation.get("isActive"),
        "isDraft": automation.get("isDraft"),
        "hasJobPreferences": bool(extract_job_preferences(automation)),
        "selectedJobsCount": len(extract_existing_jobs(automation)),
    }


def user_paid_debug_summary(user):
    return {
        "email": user.get("email"),
        "uid": user.get("uid"),
        "subscription": user.get("subscription"),
        "stripeId_present": bool(user.get("stripeId")),
        "TRUST_USERS_PAID_ENDPOINT": TRUST_USERS_PAID_ENDPOINT,
    }


def extract_job_preferences(automation):
    prefs = automation.get("jobPreferences") or {}

    if not prefs:
        prefs = (
            automation.get("settings", {})
            .get("jobPreferences", {})
        )

    return prefs or {}


SELECTED_JOBS_KEY = "selectedJobs"
REJECTED_STATUS = "rejected"


def looks_like_job_record(item):
    if not isinstance(item, dict):
        return False

    return bool(
        item.get("job_url")
        or item.get("url")
        or item.get("apply_url")
        or item.get("jobUrl")
        or (item.get("title") and item.get("company"))
    )


def canonical_job_url(job):
    return (
        job.get("job_url")
        or job.get("jobUrl")
        or job.get("url")
        or job.get("apply_url")
        or job.get("applyUrl")
        or ""
    )


def normalize_job_record(job):
    if not isinstance(job, dict):
        return {}

    normalized = dict(job)

    if not normalized.get("job_url"):
        url = canonical_job_url(job)
        if url:
            normalized["job_url"] = url

    if not normalized.get("company"):
        normalized["company"] = job.get("companyName") or job.get("employer") or ""

    if not normalized.get("title"):
        normalized["title"] = job.get("jobTitle") or job.get("position") or ""

    return normalized


def extract_selected_jobs(automation):
    """Return normalized jobs from automation.selectedJobs only.

    Source of truth correction: all active/pending/approved/rejected user jobs are
    stored in the single camelCase selectedJobs array. Do not scan selected_jobs,
    jobs, addedJobs, rejectedJobs, or nested lookalike arrays because those can
    accidentally pull unrelated payloads into duplicate checks and prompt memory.
    """
    selected_jobs = (automation or {}).get(SELECTED_JOBS_KEY)

    if not isinstance(selected_jobs, list):
        return []

    output = []
    for item in selected_jobs:
        if looks_like_job_record(item):
            output.append(normalize_job_record(item))

    return output


def extract_existing_jobs(automation):
    jobs = []
    seen = set()

    for job in extract_selected_jobs(automation):
        norm_url = normalize_url(canonical_job_url(job))
        company, title = company_title_key(job)
        status = str(job.get("status", "")).strip().lower()
        dedupe_key = norm_url or f"{company}|{title}|{status}"

        if not dedupe_key or dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        jobs.append(job)

    return jobs


def extract_rejected_jobs_from_automation(automation, limit=80):
    """Return user-rejected jobs from selectedJobs using status + feedback.

    Rejected jobs look like:
      {"status": "rejected", "feedback": "[Rating: 4/5] ...", ...}

    We intentionally filter only by status == "rejected" and keep feedback as
    the main learning signal. The job reason is kept as secondary context.
    """
    rejected = []
    seen = set()

    for job in extract_selected_jobs(automation):
        status_text = str(job.get("status", "")).strip().lower()

        if status_text != REJECTED_STATUS:
            continue

        norm_url = normalize_url(canonical_job_url(job))
        company, title = company_title_key(job)
        dedupe_key = norm_url or f"{company}|{title}|{status_text}"

        if not dedupe_key or dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        rejected.append(job)

    return rejected[-limit:]


def parse_feedback_rating(feedback):
    text = str(feedback or "")
    match = re.search(r"rating\s*:\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)

    if not match:
        return None

    try:
        score = float(match.group(1))
        maximum = float(match.group(2))
    except Exception:
        return None

    if maximum <= 0:
        return None

    return round((score / maximum) * 100)


def compact_rejected_job_learning(rejected_jobs, limit=40):
    compact = []

    for job in rejected_jobs[-limit:]:
        feedback = str(job.get("feedback") or "").strip()
        reason = str(job.get("reason") or job.get("review_reason") or "").strip()

        compact.append({
            "title": str(job.get("title", ""))[:90],
            "company": str(job.get("company", ""))[:80],
            "status": str(job.get("status", ""))[:30],
            "feedback": feedback[:160],
            "feedback_rating_100": parse_feedback_rating(feedback),
            "reason": reason[:180],
            "grade": job.get("grade"),
            "location": str(job.get("location", ""))[:80],
            "domain": url_domain(canonical_job_url(job)),
        })

    return compact


def build_link_failure_notes(rejected_jobs, limit=80):
    notes = []

    for item in rejected_jobs[-300:]:
        reason = str(
            item.get("reason")
            or item.get("review_decision")
            or item.get("review_reason")
            or ""
        )

        if reason not in LINK_FAILURE_REASONS and not any(x in reason for x in ["404", "expired", "not_found"]):
            continue

        notes.append({
            "url": normalize_url(canonical_job_url(item)),
            "domain": url_domain(canonical_job_url(item)),
            "reason": reason[:100],
            "title": str(item.get("title", ""))[:90],
            "company": str(item.get("company", ""))[:80],
        })

    return notes[-limit:]


def existing_duplicate_sets(existing_jobs):
    urls = set()
    company_titles = set()

    for job in existing_jobs:
        url = normalize_url(canonical_job_url(job))
        if url:
            urls.add(url)

        ct = company_title_key(job)
        if ct[0] and ct[1]:
            company_titles.add(ct)

    return urls, company_titles


def is_paid_user(user):
    # The script starts from /users/paid, so that endpoint should be the source
    # of truth. Do not reject users just because subscription/stripe fields are
    # missing or shaped differently in this response.
    if TRUST_USERS_PAID_ENDPOINT:
        return True

    subscription = user.get("subscription") or {}

    if subscription.get("status") != "active":
        return False

    if not user.get("stripeId"):
        return False

    return True


def parse_added_at_date(value):
    if not value:
        return None

    text = str(value).strip()

    try:
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")
        return datetime.fromisoformat(text).date()
    except Exception:
        pass

    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


PLAN_HINT_KEYS = {
    "plan",
    "planName",
    "plan_name",
    "tier",
    "product",
    "productName",
    "product_name",
    "price",
    "priceName",
    "price_name",
    "subscription",
    "membership",
}


def collect_plan_strings(obj, max_depth=3):
    strings = []

    def walk(value, key="", depth=0):
        if depth > max_depth:
            return

        key_text = str(key or "")
        key_l = key_text.lower()
        key_is_relevant = any(hint.lower() in key_l for hint in PLAN_HINT_KEYS)

        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k, depth + 1)
        elif isinstance(value, list):
            for item in value[:8]:
                walk(item, key, depth + 1)
        elif key_is_relevant and value is not None:
            strings.append(str(value))

    walk(obj or {})
    return strings


def get_user_plan(user, user_profile=None, automation=None):
    text = " ".join(
        collect_plan_strings(user)
        + collect_plan_strings(user_profile)
        + collect_plan_strings(automation)
    ).lower()

    if "premium" in text:
        return "premium"

    if "starter" in text or "start" in text:
        return "starter"

    # Match the product tier "max" without accidentally matching words like
    # maximum. Also accept common plural/possessive wording used internally.
    if re.search(r"\bmax\b|\bmaxs\b|max's", text):
        return "max"

    return "unknown"


def status_for_user_plan(plan):
    plan = str(plan or "").lower().strip()

    if plan == "premium":
        return PREMIUM_STATUS

    if plan in {"starter", "max"}:
        return STARTER_MAX_STATUS

    # Unknown paid users held for approval — safer than silently posting as premium.
    return STARTER_MAX_STATUS


def statuses_counted_for_today(job_status):
    # Include legacy pending_review so existing old automation data does not
    # cause the nightly run to over-fill users who already have today's jobs.
    return {str(job_status or "").lower(), "pending_review"}


def count_jobs_today(existing_jobs, job_status=None):
    today = datetime.now(timezone.utc).date()
    count = 0
    accepted_statuses = statuses_counted_for_today(job_status or DEFAULT_STATUS)

    for job in existing_jobs:
        status = str(job.get("status", "")).strip().lower()
        added_date = parse_added_at_date(job.get("addedAt") or job.get("createdAt") or job.get("added_at") or job.get("created_at"))

        if status in accepted_statuses and added_date == today:
            count += 1

    return count


def count_pending_review_today(existing_jobs):
    # Backward-compatible wrapper for older call sites.
    return count_jobs_today(existing_jobs, DEFAULT_STATUS)



# ============================================================
# CV / RESUME TEXT
# ============================================================

def flatten_skills(skills):
    output = []

    if isinstance(skills, list):
        for item in skills:
            if isinstance(item, str):
                output.append(item)
            elif isinstance(item, dict):
                value = item.get("list")
                if isinstance(value, list):
                    output.extend([str(x) for x in value])
                else:
                    for v in item.values():
                        if isinstance(v, str):
                            output.append(v)
                        elif isinstance(v, list):
                            output.extend([str(x) for x in v])

    return output


def cv_to_text(user_profile):
    cv = user_profile.get("cv") or {}

    parts = []

    name = f"{cv.get('first_name', '')} {cv.get('last_name', '')}".strip()
    if name:
        parts.append(f"Name: {name}")

    if cv.get("title"):
        parts.append(f"Current title: {cv.get('title')}")

    location = ", ".join(
        [
            str(cv.get("city", "")).strip(),
            str(cv.get("state", "")).strip(),
            str(cv.get("country", "")).strip(),
        ]
    ).strip(", ")

    if location:
        parts.append(f"Location: {location}")

    if cv.get("summary"):
        parts.append(f"Summary: {cv.get('summary')}")

    skills = flatten_skills(cv.get("skills", []))
    if skills:
        parts.append("Skills: " + ", ".join(skills[:80]))

    experiences = cv.get("experiences", [])
    if isinstance(experiences, list):
        for exp in experiences[:8]:
            parts.append(
                f"""
Experience:
Title: {exp.get('title', '')}
Company: {exp.get('company', '')}
Date: {exp.get('date', '')}
Location: {exp.get('location', '')}
Description:
{exp.get('description', '')}
"""
            )

    education = cv.get("education", [])
    if isinstance(education, list):
        for edu in education[:4]:
            parts.append(
                f"Education: {edu.get('degree', '')} — {edu.get('institution', '')} {edu.get('date', '')}"
            )

    if user_profile.get("cv_url"):
        parts.append(f"CV URL: {user_profile.get('cv_url')}")

    text = "\n".join(parts).strip()

    if not text:
        text = json.dumps(cv, indent=2)

    return text[:30000]


def compact_for_prompt(text, limit=SEARCH_CV_CHAR_LIMIT):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[CV truncated for cheaper search prompt; full CV is still used in strict review.]"


def build_candidate_search_brief(user_profile, automation, cv_text):
    cv = user_profile.get("cv") or {}
    prefs = extract_job_preferences(automation)
    name = (user_profile.get("displayName") or f"{cv.get('first_name', '')} {cv.get('last_name', '')}").strip()
    current_title = cv.get("title") or ""
    location = ", ".join([
        str(cv.get("city", "")).strip(),
        str(cv.get("state", "")).strip(),
        str(cv.get("country", "")).strip(),
    ]).strip(", ")

    return f"""
Candidate search brief:
- Name: {name}
- Current title: {current_title}
- Location: {location}
- Automation preference summary: {json.dumps(prefs, ensure_ascii=False)[:1800]}

Compact CV excerpt for search only:
{compact_for_prompt(cv_text)}
""".strip()


# ============================================================
# LOCAL PERSONA
# ============================================================

PERSONA_SCHEMA = {
    "type": "object",
    "properties": {
        "career_hybrid": {"type": "string"},
        "transformation_story": {"type": "string"},
        "target_titles": {"type": "array", "items": {"type": "string"}},
        "best_fit_roles": {"type": "array", "items": {"type": "string"}},
        "avoid_roles": {"type": "array", "items": {"type": "string"}},
        "must_have_filters": {"type": "array", "items": {"type": "string"}},
        "nice_to_have_filters": {"type": "array", "items": {"type": "string"}},
        "location_rules": {"type": "string"},
        "salary_rules": {"type": "string"},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
        "scoring_rules": {"type": "string"},
    },
    "required": [
        "career_hybrid",
        "transformation_story",
        "target_titles",
        "best_fit_roles",
        "avoid_roles",
        "must_have_filters",
        "nice_to_have_filters",
        "location_rules",
        "salary_rules",
        "search_keywords",
        "scoring_rules",
    ],
    "additionalProperties": False,
}


def persona_path(uid):
    return PERSONA_DIR / f"{uid}.json"


def build_local_persona(user_profile, automation, cv_text):
    """Create a lightweight persona without OpenAI for --nogpt mode.

    It is intentionally conservative and uses only automation preferences + CV
    text. This keeps the pipeline candidate-agnostic while avoiding OpenAI calls.
    """
    prefs = extract_job_preferences(automation or {})
    cv = (user_profile or {}).get("cv") or {}
    titles = extract_automation_job_titles(automation or {}, limit=20)
    if not titles and cv.get("title"):
        titles = [str(cv.get("title"))]
    locations = extract_automation_locations(automation or {})
    skills = flatten_skills(cv.get("skills", []))[:40]
    summary = str(cv.get("summary") or "")[:600]

    persona = {
        "career_hybrid": (
            "No-GPT local persona built from automation targets"
            + (f": {', '.join(titles[:5])}" if titles else "")
        ),
        "transformation_story": summary or "Candidate profile derived locally from automation preferences and CV.",
        "target_titles": titles,
        "best_fit_roles": titles,
        "avoid_roles": [],
        "must_have_filters": ["Use the automation config as source of truth for job title, location, job type, and salary."],
        "nice_to_have_filters": skills[:20],
        "location_rules": (
            "Automation locations: " + ", ".join(locations)
            if locations else
            "Use automation/CV location policy; do not apply hardcoded country bans."
        ),
        "salary_rules": json.dumps(prefs.get("salaryRange") or prefs.get("minimumAcceptableSalary") or {}, default=str),
        "search_keywords": list(dict.fromkeys(titles + skills[:20]))[:60],
        "scoring_rules": "Local scoring: prioritize title/function overlap, direct ATS URLs, compatible location, and non-duplicate companies.",
    }
    return persona


def get_or_create_persona(user_profile, automation, cv_text):
    uid = user_profile["uid"]
    path = persona_path(uid)

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            persona = json.load(f)
        print(f"Loaded local persona: {path}")
        return persona

    if NO_GPT_MODE:
        persona = build_local_persona(user_profile, automation, cv_text)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(persona, f, indent=2)
        print(f"Created local no-GPT persona: {path}")
        return persona

    prefs = extract_job_preferences(automation)

    prompt = f"""
Create a job search persona for this user.

Use the resume/CV and automation config.

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION CONFIG:
{json.dumps(prefs, indent=2)}

CV:
{cv_text}

Return a strict JSON persona.

Make it practical for job matching:
- career_hybrid: the unique professional mix
- transformation_story: what happens when this person joins a company
- target_titles: exact target titles
- best_fit_roles: role categories that should score high
- avoid_roles: roles that should score low or be rejected
- must_have_filters: hard rules
- nice_to_have_filters: softer preferences
- location_rules: location and remote/hybrid/on-site rules
- salary_rules: salary rules
- search_keywords: useful job search keywords
- scoring_rules: how to grade jobs from 1-100
"""

    response = client.responses.create(
        model=SEARCH_MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "user_persona",
                "schema": PERSONA_SCHEMA,
                "strict": True,
            }
        },
    )

    persona = json.loads(response.output_text)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(persona, f, indent=2)

    print(f"Created local persona: {path}")

    return persona



# ============================================================
# SEARCH CONTRACT
# ============================================================

SEARCH_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_snapshot": {"type": "string"},
        "allowed_titles": {"type": "array", "items": {"type": "string"}},
        "forbidden_titles": {"type": "array", "items": {"type": "string"}},
        "allowed_functions": {"type": "array", "items": {"type": "string"}},
        "forbidden_functions": {"type": "array", "items": {"type": "string"}},
        "location_hard_rule": {"type": "string"},
        "salary_hard_rule": {"type": "string"},
        "seniority_rules": {"type": "string"},
        "source_strategy": {"type": "array", "items": {"type": "string"}},
        "hard_reject_rules": {"type": "array", "items": {"type": "string"}},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "broadening_plan_if_zero_results": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "candidate_snapshot",
        "allowed_titles",
        "forbidden_titles",
        "allowed_functions",
        "forbidden_functions",
        "location_hard_rule",
        "salary_hard_rule",
        "seniority_rules",
        "source_strategy",
        "hard_reject_rules",
        "search_queries",
        "broadening_plan_if_zero_results",
    ],
    "additionalProperties": False,
}


def search_contract_path(uid):
    return SEARCH_CONTRACT_DIR / f"{uid}.json"


def save_search_contract(user_profile, search_contract):
    uid = user_profile.get("uid") or "unknown_uid"
    path = search_contract_path(uid)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(search_contract, f, indent=2)

    print(f"Saved updated search contract: {path}")
    return path


DIRECT_ATS_CONTRACT_DOMAINS = sorted(
    {
        "lever.co",
        "jobs.lever.co",
        "ashbyhq.com",
        "jobs.ashbyhq.com",
        "greenhouse.io",
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
        "workable.com",
        "apply.workable.com",
        "jobs.workable.com",
        "workdayjobs.com",
        "myworkdayjobs.com",
        "myworkdaysite.com",
        "wd1.myworkdaysite.com",
        "wd3.myworkdaysite.com",
        "wd5.myworkdaysite.com",
        "smartrecruiters.com",
        "jobs.smartrecruiters.com",
        "careers.smartrecruiters.com",
        "bamboohr.com",
        "jobs.bamboohr.com",
        "icims.com",
        "careers.icims.com",
        "jobvite.com",
        "jobs.jobvite.com",
        "teamtailor.com",
        "recruitee.com",
        "personio.com",
        "rippling.com",
        "ats.rippling.com",
        "successfactors.com",
        "oraclecloud.com",
        "taleo.net",
        "eightfold.ai",
    },
    key=len,
    reverse=True,
)

DIRECT_ATS_CONTRACT_OVERRIDE = (
    "Direct ATS provider override: Lever, Ashby, Greenhouse, Workable, Workday, "
    "SmartRecruiters, BambooHR, iCIMS, Jobvite, Teamtailor, Recruitee, Personio, "
    "Rippling, SuccessFactors, Oracle/Taleo, and Eightfold are allowed final sources "
    "when the URL is a specific job post. Do not block a whole ATS provider domain; "
    "only reject exact failed URLs, generic/search pages, expired posts, and mirror/aggregator pages."
)

DIRECT_ATS_HARD_REJECT_OVERRIDE = (
    "Never reject a job only because it is hosted on a known direct ATS provider "
    "such as Lever, Ashby, Greenhouse, Workable, Workday, SmartRecruiters, BambooHR, "
    "iCIMS, Jobvite, Teamtailor, Recruitee, Personio, Rippling, SuccessFactors, "
    "Oracle/Taleo, or Eightfold. Reject only if the URL itself is expired, generic, "
    "duplicated, a search page, or fails the candidate-specific match rules."
)

CONTRACT_BLOCKING_WORDS = {
    "block", "blocked", "blacklist", "exclude", "excluded", "excluding",
    "reject", "rejected", "avoid", "avoidance", "do not", "don't", "never",
    "low-quality", "mirror", "aggregator", "failed", "failing",
}

LOCATION_EXTRACTION_KEYWORDS = {
    "place", "places", "location", "locations", "city", "cities", "country",
    "countries", "state", "states", "region", "regions", "geo", "geography",
}

NON_LOCATION_VALUES = {
    "remote", "hybrid", "on-site", "onsite", "office", "distributed",
    "full_time", "full-time", "part_time", "part-time", "contract", "internship",
    "true", "false", "yes", "no", "any", "anywhere",
}


def _contract_text_has_any(text, terms):
    blob = str(text or "").lower()
    return any(str(term).lower() in blob for term in terms)


def _normalize_contract_rule_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _remove_direct_ats_domains_from_blocking_text(text):
    """Keep generated/adapted contracts from treating ATS providers as bad domains.

    The search contract is AI-generated and sometimes says things like
    "blocked domains: apply.workable.com, job-boards.greenhouse.io". That is
    harmful because those domains are valid direct ATS sources when the URL is a
    specific job post. This normalizer removes ATS provider names only from
    negative/blocking rules while leaving true aggregator blocks intact.
    """
    original = str(text or "")
    lowered = original.lower()

    if not any(domain in lowered for domain in DIRECT_ATS_CONTRACT_DOMAINS):
        return original, False

    if not any(word in lowered for word in CONTRACT_BLOCKING_WORDS):
        return original, False

    updated = original
    changed = False

    for domain in DIRECT_ATS_CONTRACT_DOMAINS:
        pattern = re.compile(r"\b" + re.escape(domain) + r"\b", re.IGNORECASE)
        if pattern.search(updated):
            updated = pattern.sub("", updated)
            changed = True

    if changed:
        # Clean dangling punctuation created by removing domains from comma lists.
        updated = re.sub(r"\(\s*,\s*", "(", updated)
        updated = re.sub(r",\s*,+", ",", updated)
        updated = re.sub(r"\s+,", ",", updated)
        updated = re.sub(r",\s*\)", ")", updated)
        updated = re.sub(r"\(\s*\)", "", updated)
        updated = re.sub(r"\s{2,}", " ", updated).strip(" ,;.-")
        updated = (
            updated
            + ". Direct ATS provider domains were removed from this blocking rule; "
              "specific job-post URLs on those ATS platforms remain allowed."
        )

    return updated, changed


def _split_location_values(value):
    if value is None:
        return []

    if isinstance(value, (int, float, bool)):
        return []

    text = str(value).strip()
    if not text:
        return []

    pieces = re.split(r"\n|;|\||,(?=\s*[A-Za-z])", text)
    if not pieces:
        pieces = [text]

    output = []
    for piece in pieces:
        item = re.sub(r"\s+", " ", piece).strip(" -•\t")
        lowered = item.lower()
        if not item or lowered in NON_LOCATION_VALUES:
            continue
        if len(item) < 2 or len(item) > 90:
            continue
        # Avoid accidentally preserving job titles as locations when schemas are noisy.
        if looks_like_role_title(item):
            continue
        output.append(item)
    return output


def extract_automation_locations(automation, limit=40):
    """Return explicit location/place values from automation preferences.

    This is intentionally schema-flexible so the runner stays candidate-agnostic.
    It looks for location-like keys anywhere in jobPreferences/settings and keeps
    only concrete places/regions/countries, not job type values such as remote or
    hybrid.
    """
    prefs = extract_job_preferences(automation or {})
    locations = []

    def walk(value, key_path=""):
        key_l = key_path.lower().replace("_", "")
        key_relevant = any(k in key_l for k in LOCATION_EXTRACTION_KEYWORDS)

        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{key_path}.{k}" if key_path else str(k))
            return

        if isinstance(value, list):
            if key_relevant:
                for item in value:
                    locations.extend(_split_location_values(item))
            else:
                for item in value:
                    walk(item, key_path)
            return

        if key_relevant:
            locations.extend(_split_location_values(value))

    walk(prefs)

    seen = set()
    output = []
    for loc in locations:
        norm = re.sub(r"\s+", " ", loc.strip().lower())
        if not norm or norm in seen:
            continue
        seen.add(norm)
        output.append(loc.strip())
        if len(output) >= limit:
            break
    return output


def normalize_search_contract_for_automation(search_contract, automation, user_profile=None):
    """Repair AI-generated/adapted contracts using candidate automation truth.

    Candidate-agnostic behavior:
    1. Preserve every explicit automation location in location_hard_rule.
    2. Prevent the contract from blocking known direct ATS provider domains.
    3. Add source/hard-reject overrides that apply to all candidates.
    """
    if not isinstance(search_contract, dict):
        return search_contract, False

    changed = False

    for field in ["source_strategy", "hard_reject_rules", "broadening_plan_if_zero_results"]:
        values = search_contract.get(field)
        if not isinstance(values, list):
            continue

        normalized_values = []
        for value in values:
            if not isinstance(value, str):
                normalized_values.append(value)
                continue
            updated, did_change = _remove_direct_ats_domains_from_blocking_text(value)
            normalized_values.append(_normalize_contract_rule_text(updated))
            changed = changed or did_change

        if normalized_values != values:
            search_contract[field] = normalized_values
            changed = True

    source_strategy = search_contract.setdefault("source_strategy", [])
    if not any("direct ats provider override" in str(item).lower() for item in source_strategy):
        source_strategy.insert(0, DIRECT_ATS_CONTRACT_OVERRIDE)
        changed = True

    hard_reject_rules = search_contract.setdefault("hard_reject_rules", [])
    if not any("never reject a job only because it is hosted on a known direct ats provider" in str(item).lower() for item in hard_reject_rules):
        hard_reject_rules.append(DIRECT_ATS_HARD_REJECT_OVERRIDE)
        changed = True

    automation_locations = extract_automation_locations(automation)
    if automation_locations:
        location_rule = str(search_contract.get("location_hard_rule") or "")
        location_rule_l = location_rule.lower()
        missing_locations = [
            loc for loc in automation_locations
            if loc.lower() not in location_rule_l
        ]
        if missing_locations:
            addition = (
                " Automation location override: the automation config explicitly allows "
                + ", ".join(missing_locations)
                + ". These locations must remain allowed unless a job itself has a conflicting work-authorization or relocation requirement."
            )
            search_contract["location_hard_rule"] = (location_rule + addition).strip()
            changed = True

    return search_contract, changed


def build_local_search_contract(user_profile, automation, cv_text, persona):
    """Create a strict but usable search contract without OpenAI."""
    prefs = extract_job_preferences(automation or {})
    titles = extract_automation_job_titles(automation or {}, limit=30)
    if not titles and isinstance(persona, dict):
        titles = list(persona.get("target_titles") or [])[:30]
    locations = extract_automation_locations(automation or {})
    salary_min = (
        prefs.get("minimumAcceptableSalary")
        or (prefs.get("salaryRange") or {}).get("min")
        or ""
    )
    salary_currency = prefs.get("salaryCurrency") or (prefs.get("salaryRange") or {}).get("currency") or ""
    job_types = prefs.get("preferredJobTypes") or {}
    location_rule = (
        "Allowed automation locations: " + ", ".join(locations) + ". "
        if locations else
        "Use automation/CV location policy; no global country ban. "
    )
    location_rule += "Remote/hybrid/on-site eligibility must match the automation config and the job page."

    search_queries = []
    location_text = " OR ".join(locations[:5]) if locations else "remote OR hybrid OR onsite"
    for title in titles[:12]:
        search_queries.append(f'"{title}" ({location_text}) direct apply ATS')

    return {
        "candidate_snapshot": f"No-GPT local contract for {user_profile.get('displayName') or user_profile.get('email')}",
        "allowed_titles": titles,
        "forbidden_titles": [],
        "allowed_functions": titles + list((persona or {}).get("best_fit_roles") or [])[:20],
        "forbidden_functions": [],
        "location_hard_rule": location_rule,
        "salary_hard_rule": (
            f"Minimum salary from automation: {salary_min} {salary_currency}. Allow missing salary for manual review."
            if salary_min else
            "No reliable hard salary floor extracted locally; allow missing salary for manual review."
        ),
        "seniority_rules": "Use automation/CV seniority; reject obvious internships, graduate trainee roles, or impossible executive mismatches unless target titles allow them.",
        "source_strategy": [
            DIRECT_ATS_CONTRACT_OVERRIDE,
            "No-GPT mode: source from Jobo ATS and HiringCafe first; use direct ATS/company URLs only.",
            "Prefer direct ATS providers and company job pages with specific job IDs.",
        ],
        "hard_reject_rules": [
            DIRECT_ATS_HARD_REJECT_OVERRIDE,
            "Reject aggregators, mirrors, generic search pages, expired/404 pages, duplicates, and clear location conflicts.",
            "Reject roles with obvious forbidden job type conflicts: " + json.dumps(job_types, default=str),
        ],
        "search_queries": search_queries or ["direct ATS jobs matching automation titles and locations"],
        "broadening_plan_if_zero_results": [
            "Use more Jobo ATS calls across target title variants.",
            "Use HiringCafe structured results as a supplement.",
            "Allow close title variants only when static/remote validation passes and local score remains strong.",
        ],
    }


def get_or_create_search_contract(user_profile, automation, cv_text, persona):
    uid = user_profile["uid"]
    path = search_contract_path(uid)

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            search_contract = json.load(f)
        search_contract, normalized = normalize_search_contract_for_automation(
            search_contract,
            automation,
            user_profile=user_profile,
        )
        if normalized:
            save_search_contract(user_profile, search_contract)
            print(f"Normalized local search contract: {path}")
        print(f"Loaded local search contract: {path}")
        return search_contract

    if NO_GPT_MODE:
        search_contract = build_local_search_contract(user_profile, automation, cv_text, persona)
        search_contract, _ = normalize_search_contract_for_automation(
            search_contract,
            automation,
            user_profile=user_profile,
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(search_contract, f, indent=2)
        print(f"Created local no-GPT search contract: {path}")
        return search_contract

    prefs = extract_job_preferences(automation)

    prompt = f"""
Create a strict candidate-specific SEARCH CONTRACT for job sourcing.

This contract will be used BEFORE web search to prevent wasted searches and bad matches.
It must be stricter and more operational than a persona.

Use the automation config as the source of truth for location, salary, target roles, and avoid rules.
Use the persona and CV to define the exact role cluster.

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION CONFIG:
{json.dumps(prefs, indent=2)}

PERSONA:
{json.dumps(persona, indent=2)}

CV:
{cv_text}

Build the contract with these goals:
- allowed_titles: exact titles and very close variants only
- forbidden_titles: titles that often create false positives for this candidate
- allowed_functions: the daily work/function that truly fits the candidate
- forbidden_functions: adjacent-but-wrong functions to reject before review
- location_hard_rule: exact allowed geography/remote rule, not vague
- salary_hard_rule: minimum salary rule and how to treat missing salary
- seniority_rules: acceptable seniority range and what is too junior/senior
- source_strategy: where to search first, especially direct ATS/company pages
- hard_reject_rules: clear rules that mean “do not return this job at all”
- search_queries: 10 to 20 precise web search queries/sites to try first
- broadening_plan_if_zero_results: how to relax carefully only after zero approved jobs

Important:
- Do not over-broaden.
- If the automation config says remote only, location_hard_rule must say remote only.
- Preserve every explicit location/place/country listed in the automation config. Do not silently drop any allowed automation location when creating location_hard_rule or search_queries.
- If automation allows multiple locations, location_hard_rule must include all of them.
- Never classify direct ATS provider domains as blocked/aggregator domains. Lever, Ashby, Greenhouse, Workable, Workday, SmartRecruiters, BambooHR, iCIMS, Jobvite, Teamtailor, Recruitee, Personio, Rippling, SuccessFactors, Oracle/Taleo, and Eightfold are allowed when the URL is a specific job post.
- Only block exact failed URLs, expired posts, generic career/search pages, mirrors, and true aggregators.
- If a title/function is only keyword-adjacent but not actually a fit, put it in forbidden_titles or forbidden_functions.
- The contract should reduce wasted web-search and review calls.

Return strict JSON only.
"""

    response = client.responses.create(
        model=SEARCH_MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "candidate_search_contract",
                "schema": SEARCH_CONTRACT_SCHEMA,
                "strict": True,
            }
        },
    )

    search_contract = json.loads(response.output_text)
    search_contract, _ = normalize_search_contract_for_automation(
        search_contract,
        automation,
        user_profile=user_profile,
    )

    with open(path, "w", encoding="utf-8") as f:
        json.dump(search_contract, f, indent=2)

    print(f"Created local search contract: {path}")

    return search_contract


def print_search_contract(search_contract):
    print("\nSEARCH CONTRACT:")
    print(json.dumps(search_contract, indent=2))


# ============================================================
# OPENAI JOB SEARCH
# ============================================================

JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "job_url": {"type": "string"},
                    "source": {"type": "string"},
                    "grade": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": [
                    "title",
                    "company",
                    "location",
                    "job_url",
                    "source",
                    "grade",
                    "description",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


ROLE_EXPANSION_TEXT = """
Search a bit broader than exact title only.

Use:
- exact target titles from the automation
- close title variants
- senior/lead/director variants when allowed by experience level
- adjacent titles from the persona
- remote-friendly title variants

For design users, include:
Brand Designer, Senior Brand Designer, Graphic Designer, Senior Graphic Designer,
Visual Designer, Marketing Designer, Creative Designer, Web Designer,
Brand & Web Designer, Campaign Designer, Growth Creative Designer,
Art Director, Senior Art Director, Associate Creative Director,
Creative Director, Design Director, Head of Design.

For sales users, include:
Account Executive, Senior Account Executive, Enterprise Account Executive,
Account Manager, Strategic Account Manager, Partnerships Manager,
Business Development Manager, Revenue roles.

For product users, include:
Product Manager, Senior Product Manager, Technical Product Manager,
Platform Product Manager, AI Product Manager, Data Product Manager,
Group Product Manager, Principal Product Manager.

For operations/project users, include:
Operations Manager, Program Manager, Project Manager, Business Operations,
Transformation Manager, PMO, Reporting Manager, Analytics Manager,
Project Controls, Cost Control, Change Manager.

Do not over-broaden into unrelated jobs.
The job still must match the resume, persona, location, salary, and automation config.
Return different companies from previous batches.
"""


ENTRY_LEVEL_ADMIN_EXPANSION_TEXT = """
For entry-level admin/data-entry/customer-support users, do not only search "remote data entry".
That phrase returns too many spam, duplicate, and aggregator jobs.

Also search real company/ATS roles such as:
- Administrative Assistant
- Office Assistant
- Records Clerk
- Document Control Clerk
- Data Coordinator
- Operations Assistant
- Customer Support Assistant
- Customer Service Representative
- Patient Access Representative
- Referral Coordinator
- Claims Support Processor
- Enrollment Specialist
- Intake Coordinator
- Scheduling Coordinator
- Medical Records Clerk
- Library Assistant
- IT Help Desk Assistant
- Technical Support Assistant
- Support Specialist
- Operations Coordinator

Avoid suspicious "easy remote data entry" jobs unless the URL is a real company career page
or a direct ATS job post with a job ID.
"""


def get_role_specific_source_mode_text(user_profile, automation, persona):
    prefs = extract_job_preferences(automation)
    blob = json.dumps(
        {
            "profile": user_profile,
            "prefs": prefs,
            "persona": persona,
        },
        default=str,
    ).lower()

    if any(term in blob for term in ["warehouse", "inventory", "logistics", "material handler", "forklift", "retail"]):
        return """
ROLE-SPECIFIC SOURCE MODE: LOCAL WAREHOUSE / RETAIL / LOGISTICS
- Prefer direct company career pages and ATS pages for local employers.
- Search local direct sources like Amazon Jobs, Home Depot Careers, Target Jobs, Walmart Careers, Lowe's Jobs, FedEx Careers, UPS Jobs, DHL Careers, Grainger Careers, Best Buy Jobs, Wayfair Careers, HD Supply Careers, Ryder Jobs, XPO Careers, Cintas Jobs, Sysco Careers, and US Foods Careers.
- For on-site warehouse/logistics roles, only return the exact requested city/metro unless the automation explicitly allows remote or other places.
- Do not return remote-office jobs unless the role itself is actually suitable for remote work.
"""

    if any(term in blob for term in ["product manager", "technical product", "platform product", "ai product", "senior product"]):
        return """
ROLE-SPECIFIC SOURCE MODE: PRODUCT
- Prefer direct ATS jobs from Ashby, Greenhouse, Lever, Workday, SmartRecruiters, Workable, iCIMS, Jobvite, Rippling, BambooHR, and Eightfold.
- Search product titles plus domain variants: Technical Product Manager, Platform Product Manager, AI Product Manager, Data Product Manager, SaaS Product Manager, Growth Product Manager, and Healthcare Product Manager.
- Avoid project manager, product marketing, program manager, owner-only, and business analyst roles unless the persona clearly supports them.
"""

    if any(term in blob for term in ["software engineer", "devops", "sre", "site reliability", "cloud", "platform engineer", "kubernetes", "aws", "security engineer"]):
        return """
ROLE-SPECIFIC SOURCE MODE: ENGINEERING / CLOUD / SRE
- Prefer direct ATS jobs from Ashby, Lever, Greenhouse, Workday, SmartRecruiters, Jobvite, Rippling, and iCIMS.
- Search exact infrastructure/cloud/SRE/platform titles first.
- Avoid backend-only, data-platform-only, or application engineering roles unless the persona says they are acceptable.
"""

    if any(term in blob for term in ["brand designer", "graphic designer", "art director", "creative director", "visual designer", "marketing designer"]):
        return """
ROLE-SPECIFIC SOURCE MODE: DESIGN / CREATIVE
- Prefer company ATS pages, agency/company career pages, and direct apply pages with real job IDs.
- Search title variants: Brand Designer, Visual Designer, Marketing Designer, Graphic Designer, Art Director, Creative Director, Design Director, Web Designer, Campaign Designer.
- Avoid generic portfolio communities, design job boards, Built In, and third-party mirrors as final URLs.
"""

    if any(term in blob for term in ["account executive", "sales", "business development", "partnerships", "customer success", "solutions engineer", "sales engineer"]):
        return """
ROLE-SPECIFIC SOURCE MODE: SALES / CUSTOMER / SOLUTIONS
- Prefer direct ATS/company postings from SaaS, fintech, healthcare tech, infrastructure, cloud, and B2B companies.
- Search exact title plus adjacent titles only when aligned: Account Executive, Enterprise Account Executive, Partnerships Manager, Business Development Manager, Customer Success Manager, Solutions Engineer, Sales Engineer.
- Avoid generic sales-only ads, commission-only roles, and staffing agency posts unless the automation explicitly allows them.
"""

    if any(term in blob for term in ["healthcare", "clinical", "patient", "medical", "nurse", "therapy", "care coordinator"]):
        return """
ROLE-SPECIFIC SOURCE MODE: HEALTHCARE / CLINICAL / PATIENT OPS
- Prefer direct hospital, healthcare company, health-tech ATS, and provider career pages with a real job ID.
- Search exact role family plus adjacent healthcare ops titles where appropriate.
- Verify location/licensure/salary carefully before review.
"""

    if any(term in blob for term in ["data entry", "administrative", "admin", "records", "clerk", "customer service", "customer support", "intake", "scheduling"]):
        return """
ROLE-SPECIFIC SOURCE MODE: ADMIN / DATA ENTRY / SUPPORT
- Avoid the phrase-only search 'remote data entry' because it creates junk.
- Prefer real employer pages and ATS postings for Admin Assistant, Records Clerk, Document Control Clerk, Intake Coordinator, Scheduling Coordinator, Claims Processor, Customer Support Assistant, and Patient Access roles.
- Never return sketchy easy-apply aggregators or generic remote job mirrors.
"""

    return """
ROLE-SPECIFIC SOURCE MODE: GENERAL
- Prefer direct ATS/company job URLs over any job board.
- Search exact target titles first, then close title variants from the persona.
- Return fewer jobs rather than weak or generic URLs.
"""


def get_candidate_specific_expansion_text(user_profile, automation, persona):
    prefs = extract_job_preferences(automation)

    blob = json.dumps(
        {
            "profile": user_profile,
            "prefs": prefs,
            "persona": persona,
        },
        default=str,
    ).lower()

    entry_level_terms = [
        "data entry",
        "administrative",
        "admin",
        "office assistant",
        "records",
        "clerk",
        "customer service",
        "customer support",
        "librarian",
        "library",
        "it help",
        "help desk",
        "entry level",
        "entry-level",
    ]

    if any(term in blob for term in entry_level_terms):
        return ENTRY_LEVEL_ADMIN_EXPANSION_TEXT

    return ""


def build_ats_patterns_text():
    return "\n".join(f"- {pattern}" for pattern in ATS_SEARCH_PATTERNS)


def parse_json_output(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
            return json.loads(candidate)

        raise


def openai_web_search_call(prompt):
    if NO_GPT_MODE or client is None:
        raise RuntimeError("OpenAI web search is disabled in --nogpt mode.")
    try:
        return client.responses.create(
            model=SEARCH_MODEL,
            tools=[
                {
                    "type": "web_search",
                    "search_context_size": SEARCH_CONTEXT_SIZE,
                }
            ],
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "job_results",
                    "schema": JOB_SCHEMA,
                    "strict": True,
                }
            },
        )
    except Exception as e:
        if "web_search" not in str(e):
            raise

        return client.responses.create(
            model=SEARCH_MODEL,
            tools=[
                {
                    "type": "web_search_preview",
                    "search_context_size": SEARCH_CONTEXT_SIZE,
                }
            ],
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "job_results",
                    "schema": JOB_SCHEMA,
                    "strict": True,
                }
            },
        )


def repair_job_json_output(raw_text):
    if NO_GPT_MODE or client is None:
        return None
    """Repair malformed JSON returned by the search+web call.

    The web-search model can still occasionally emit malformed JSON despite the
    schema. Instead of losing the whole batch, run a cheap no-web repair pass
    against the raw output. This does not invent new jobs; it only extracts or
    fixes job objects already present in the failed response.
    """
    raw_text = str(raw_text or "").strip()
    if not raw_text:
        return None

    repair_prompt = f"""
The text below was supposed to be JSON matching this schema:
{{"jobs": [{{"title": string, "company": string, "location": string, "job_url": string, "source": string, "grade": integer, "description": string}}]}}

Repair it into valid strict JSON only.
Rules:
- Do not invent jobs.
- Keep only jobs that already appear in the text.
- Drop incomplete job objects.
- Escape quotes/newlines inside strings.
- Description must be one short plain-text sentence.
- Return {{"jobs": []}} if no complete jobs can be recovered.

RAW TEXT:
{raw_text[:18000]}
"""

    try:
        response = client.responses.create(
            model=SEARCH_MODEL,
            input=repair_prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "job_results_repaired",
                    "schema": JOB_SCHEMA,
                    "strict": True,
                }
            },
        )
        return parse_json_output(response.output_text)
    except Exception as e:
        print(f"JSON repair failed: {e}")
        return None


DIRECT_JOB_RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_url": {"type": "string"},
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "source": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "why_this_is_direct": {"type": "string"},
                },
                "required": [
                    "job_url",
                    "title",
                    "company",
                    "location",
                    "source",
                    "confidence",
                    "why_this_is_direct",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


RESOLUTION_TIMEOUT_SECONDS = 30.0


def openai_direct_resolution_call(prompt):
    if NO_GPT_MODE or client is None:
        raise RuntimeError("OpenAI direct resolution is disabled in --nogpt mode.")
    try:
        return client.responses.create(
            model=RESOLUTION_MODEL,
            tools=[{"type": "web_search", "search_context_size": RESOLUTION_SEARCH_CONTEXT_SIZE}],
            input=prompt,
            timeout=RESOLUTION_TIMEOUT_SECONDS,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "direct_job_resolution_results",
                    "schema": DIRECT_JOB_RESOLUTION_SCHEMA,
                    "strict": True,
                }
            },
        )
    except Exception as e:
        if "web_search" not in str(e):
            raise

        return client.responses.create(
            model=RESOLUTION_MODEL,
            tools=[{"type": "web_search_preview", "search_context_size": RESOLUTION_SEARCH_CONTEXT_SIZE}],
            input=prompt,
            timeout=RESOLUTION_TIMEOUT_SECONDS,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "direct_job_resolution_results",
                    "schema": DIRECT_JOB_RESOLUTION_SCHEMA,
                    "strict": True,
                }
            },
        )


def search_safe_company_token(company):
    text = re.sub(r"[^a-zA-Z0-9\s.-]", " ", str(company or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80]


def search_safe_title_token(title):
    text = re.sub(r"[^a-zA-Z0-9\s/()+&.,:-]", " ", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def resolve_direct_job_urls(job, avoid_urls, rejected_domains, max_candidates=MAX_DIRECT_RESOLUTION_CANDIDATES):
    """Try to turn a discovery/job-board lead into a real ATS/company job URL.

    This mirrors the manual sourcing workflow:
    - use the mirror/HiringCafe/job-board result only as a lead;
    - search company name + exact job title;
    - scan past job-board copies;
    - return only the direct ATS/company apply URL.
    """
    company = search_safe_company_token(job.get("company"))
    title = search_safe_title_token(job.get("title"))
    location = str(job.get("location", ""))[:120]
    original_url = canonical_job_url(job)
    original_domain = url_domain(original_url)

    if not company or not title:
        return []

    avoid_urls_clean = sorted({normalize_url(u) for u in avoid_urls if u})[-160:]
    rejected_domains_clean = sorted(
        d for d in {clean_domain(x) for x in rejected_domains if x}
        if d and not is_non_blockable_direct_source_domain(d)
    )[-80:]

    prompt = f"""
Find the real direct apply URL for this job lead.

The current URL may be HiringCafe, Wellfound, YC, Startup.jobs, a VC job board,
a mirror, a search page, or a generic company career page. Use those sources ONLY
for discovery. The final returned URL must be a real direct company/ATS job post.

Emulate this manual workflow exactly:
1. Search the exact company + exact job title:
   - "{company}" "{title}"
   - "{company}" "{title}" apply
   - "{company}" "{title}" careers
   - "{company}" "{title}" jobs
   - "{company}" hiring "{title}"
2. Search ATS sources with company + title:
   - site:jobs.lever.co "{company}" "{title}"
   - site:jobs.ashbyhq.com "{company}" "{title}"
   - site:job-boards.greenhouse.io "{company}" "{title}"
   - site:boards.greenhouse.io "{company}" "{title}"
   - site:apply.workable.com "{company}" "{title}"
   - site:jobs.workable.com "{company}" "{title}"
   - site:myworkdayjobs.com "{company}" "{title}"
   - site:jobs.smartrecruiters.com "{company}" "{title}"
   - site:teamtailor.com "{company}" "{title}"
   - site:recruitee.com "{company}" "{title}"
3. Search broad web if ATS search fails:
   - "{company}" "{title}" "Greenhouse"
   - "{company}" "{title}" "Lever"
   - "{company}" "{title}" "Ashby"
   - "{company}" "{title}" "Workable"
   - "{company}" "{title}" "application"
4. You may use HiringCafe, Wellfound, YC, Startup.jobs, Otta, LinkedIn snippets,
   VC job boards, and other job-board results as discovery clues. Do not return
   those URLs. Scan for the ATS/company result that often appears between them.
5. If the original company appears to be a recruiter, VC, marketplace, or job board,
   infer the true hiring company from the title/description and search both names.

Original job lead:
{json.dumps({
    "title": job.get("title"),
    "company": job.get("company"),
    "location": location,
    "job_url": original_url,
    "job_url_domain": original_domain,
    "source": job.get("source"),
    "description": job.get("description"),
}, indent=2)}

Return up to {max_candidates} candidates.

Hard rules:
- Return only direct ATS/company job post URLs.
- A company career page slug is allowed if it is a specific job post, such as /careers/<id>, /jobs/<role-slug>, /o/<role-slug>, or /apply/<role-slug>.
- Company-hosted BambooHR, Teamtailor, Recruitee, Workable, Greenhouse, Lever, Ashby, Personio, SmartRecruiters and similar ATS pages are strongly preferred.
- Do not return HiringCafe, LinkedIn, Indeed, Glassdoor, Built In, Wellfound,
  Startup.jobs, YC, Hubmub, PeopleInAI, Remoteo, ReactRemoteJobs, PyJobs,
  Recruit.net, RemoteRocketship, SpainJobs, Trabajas, DailyRemote, or mirrors.
- Do not return generic career homepages, search pages, or pages without the specific job title.
- Do not return URLs already avoided.
- If you cannot find a real direct job URL, return an empty candidates array.

Avoid URLs:
{json.dumps(avoid_urls_clean, indent=2)}

Rejected non-ATS discovery/mirror domains:
{json.dumps(rejected_domains_clean, indent=2)}

Return JSON only.
"""

    try:
        response = openai_direct_resolution_call(prompt)
        data = parse_json_output(response.output_text)
    except Exception as e:
        print(f"Direct URL resolver failed for {title} — {company}: {e}")
        return []

    candidates = []
    seen = set()

    for item in data.get("candidates", [])[:max_candidates]:
        url = str(item.get("job_url", "")).strip()
        norm = normalize_url(url)
        domain = url_domain(url)

        if not url or not norm or norm in seen or norm in avoid_urls:
            continue
        if is_blocked_final_domain(domain):
            continue
        if domain in rejected_domains and not is_non_blockable_direct_source_domain(domain):
            # Non-ATS rejected domains are usually discovery mirrors. A true ATS/company
            # direct URL should not be on this list, but keep this guard for noisy sources.
            continue

        seen.add(norm)
        candidates.append({
            "title": item.get("title") or job.get("title"),
            "company": item.get("company") or job.get("company"),
            "location": item.get("location") or job.get("location", ""),
            "job_url": url,
            "source": f"direct_url_resolver:{item.get('source') or url_domain(url)}",
            "grade": normalize_grade(job.get("grade", 75)),
            "description": job.get("description", ""),
            "resolved_from_url": original_url,
            "direct_resolution_confidence": normalize_grade(item.get("confidence", 75)),
            "direct_resolution_reason": item.get("why_this_is_direct", ""),
        })

    return candidates


HARD_PRE_REVIEW_RISK_MARKERS = [
    "location conflict",
    "us-only",
    "usa only",
    "united states only",
    "onsite",
    "on-site",
    "no remote",
    "wrong function",
    "commercial",
    "go-to-market",
    "sales",
    "business development",
    "salary below",
    "below minimum",
    "not a strong match",
    "not closely aligned",
    "weak alignment",
    "too specialized",
    "not enough evidence",
]


def extract_salary_amounts_annual_usdish(text):
    """Very cheap salary detector for obvious under-minimum roles.

    It intentionally does not try to convert currencies perfectly. It only catches
    explicit k-style annual ranges that are clearly below the usual $80k target.
    """
    blob = str(text or "").lower().replace(",", "")
    amounts = []

    # €40k, £70k, $120k, 40-50k, 80,000 etc.
    patterns = [
        r"(?:[$€£]|usd|eur|gbp)?\s*(\d{2,3})(?:\.\d+)?\s*k\b",
        r"(?:[$€£]|usd|eur|gbp)\s*(\d{5,6})\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, blob, re.IGNORECASE):
            try:
                raw = float(match.group(1))
            except Exception:
                continue
            if raw < 1000:
                raw *= 1000
            if 10000 <= raw <= 500000:
                amounts.append(int(raw))

    return amounts


def has_strong_equity_or_founder_signal(text):
    blob = str(text or "").lower()
    return any(
        marker in blob
        for marker in [
            "significant equity",
            "meaningful equity",
            "founder-level equity",
            "co-founder",
            "cofounder",
            "technical co-founder",
            "cto",
            "founding team",
            "founding engineer",
            "equity upside",
        ]
    )



def _norm_words(text):
    return re.sub(r"[^a-z0-9+.#-]+", " ", str(text or "").lower())


REGION_MARKERS = {
    "us": [
        r"\bus\b", r"\bu\.s\.\b", r"\busa\b", r"\bunited states\b", r"\bamerica\b",
        r"\bnorth america\b", r"\bcalifornia\b", r"\bsan francisco\b", r"\bmenlo park\b",
        r"\bnew york\b", r"\bseattle\b", r"\baustin\b", r"\bboston\b", r"\bchicago\b",
        r"\bremote[- ]?us\b", r"\bus remote\b", r"\bremote usa\b", r"\bremote, usa\b",
    ],
    "canada": [r"\bcanada\b", r"\btoronto\b", r"\bvancouver\b", r"\bremote canada\b"],
    "europe": [r"\beurope\b", r"\beu\b", r"\bemea\b", r"\bcet\b", r"\beuropean\b"],
    "uk": [r"\buk\b", r"\bunited kingdom\b", r"\blondon\b", r"\bengland\b", r"\bscotland\b", r"\bwales\b"],
    "spain": [r"\bspain\b", r"\bbarcelona\b", r"\bmadrid\b", r"\bvalencia\b"],
    "portugal": [r"\bportugal\b", r"\blisbon\b", r"\bporto\b"],
    "ireland": [r"\bireland\b", r"\bdublin\b"],
    "india": [r"\bindia\b", r"\bdelhi\b", r"\bbengaluru\b", r"\bbangalore\b", r"\bmumbai\b", r"\bhyderabad\b"],
    "latam": [r"\blatam\b", r"\blatin america\b", r"\bmexico\b", r"\bbrazil\b", r"\bargentina\b", r"\bcolombia\b", r"\bchile\b"],
    "africa": [r"\bafrica\b", r"\bsouth africa\b", r"\bnigeria\b", r"\bkenya\b", r"\bghana\b"],
    "middle_east": [
        r"\bunited arab emirates\b", r"\buae\b", r"\bdubai\b", r"\babu dhabi\b",
        r"\bsharjah\b", r"\bgcc\b", r"\bmiddle east\b", r"\bsaudi\b", r"\bsaudi arabia\b",
        r"\bqatar\b", r"\bdoha\b", r"\boman\b", r"\bmuscat\b", r"\bbahrain\b",
        r"\bkuwait\b", r"\bisrael\b",
    ],
    "australia": [r"\baustralia\b", r"\bnew zealand\b", r"\banz\b", r"\bsydney\b", r"\bmelbourne\b"],
}


COUNTRY_TO_REGION_HINTS = {
    "united states": "us", "usa": "us", "us": "us", "america": "us",
    "canada": "canada",
    "spain": "spain", "portugal": "portugal", "ireland": "ireland",
    "united kingdom": "uk", "uk": "uk", "england": "uk",
    "india": "india",
    "south africa": "africa", "nigeria": "africa", "kenya": "africa", "ghana": "africa",
    "united arab emirates": "middle_east", "uae": "middle_east", "saudi arabia": "middle_east",
    "australia": "australia", "new zealand": "australia",
}


def text_matches_any_regex(text, patterns):
    blob = _norm_words(text)
    return any(re.search(pattern, blob) for pattern in patterns)


def detect_regions_from_text(text):
    blob = _norm_words(text)
    regions = set()
    for region, patterns in REGION_MARKERS.items():
        if any(re.search(pattern, blob) for pattern in patterns):
            regions.add(region)
    return regions


def candidate_location_policy(user_profile=None, automation=None, search_contract=None):
    prefs = extract_job_preferences(automation or {})
    cv = (user_profile or {}).get("cv") or {}
    pieces = [
        json.dumps(prefs, default=str),
        json.dumps(search_contract or {}, default=str),
        str((user_profile or {}).get("location") or ""),
        str(cv.get("city") or ""),
        str(cv.get("state") or ""),
        str(cv.get("country") or ""),
    ]
    blob = _norm_words(" ".join(pieces))

    allowed_regions = detect_regions_from_text(blob)

    # Add the user's CV country as a location hint when present.
    country = _norm_words(cv.get("country") or "")
    for key, region in COUNTRY_TO_REGION_HINTS.items():
        if re.search(r"\b" + re.escape(key) + r"\b", country):
            allowed_regions.add(region)

    worldwide = any(
        marker in blob
        for marker in [
            "worldwide", "global remote", "remote global", "anywhere", "any location",
            "work from anywhere", "international remote", "international contractor",
            "any country", "globally distributed",
        ]
    )

    remote_allowed = any(marker in blob for marker in ["remote", "hybrid", "work from home", "distributed"])
    us_requires_international_support = (
        "us" in allowed_regions
        and any(marker in blob for marker in ["international contractor", "international remote", "contractor support", "open to international", "worldwide remote including verified us"])
        and not re.search(r"\b(us|usa|united states|u\.s\.)\s+(only|citizens|residents|required)\b", blob)
    )

    # If the config is vague (e.g. only says "remote") do not invent geography.
    # Let AI review decide rather than hard-rejecting a country for all users.
    restrictive = bool(allowed_regions or worldwide)

    return {
        "allowed_regions": sorted(allowed_regions),
        "worldwide": worldwide,
        "remote_allowed": remote_allowed,
        "restrictive": restrictive,
        "us_requires_international_support": us_requires_international_support,
    }


def location_policy_summary(user_profile=None, automation=None, search_contract=None):
    policy = candidate_location_policy(user_profile, automation, search_contract)
    if policy["worldwide"]:
        scope = "worldwide/international remote is allowed when the job itself confirms it."
    elif policy["allowed_regions"]:
        scope = "allowed regions inferred from this user's automation/CV/search contract: " + ", ".join(policy["allowed_regions"])
        if policy.get("us_requires_international_support"):
            scope += "; US-only jobs require explicit international contractor/remote support for this user"
    else:
        scope = "no specific geography was safely inferred; do not apply any global country ban and rely on the automation text."

    return (
        f"Candidate-specific location policy: {scope} "
        "Never apply a hardcoded global rejection like 'US remote is bad' or 'India is bad'. "
        "Reject a location only when it clearly conflicts with this user's own automation/search-contract rules."
    )


def job_location_conflicts_candidate_policy(job, user_profile=None, automation=None, search_contract=None):
    policy = candidate_location_policy(user_profile, automation, search_contract)
    if policy["worldwide"] or not policy["restrictive"]:
        return False

    location = _norm_words(job.get("location", ""))
    title = _norm_words(job.get("title", ""))
    description = _norm_words(job.get("description", ""))
    source = _norm_words(job.get("source", ""))
    combined = f"{title} {location} {description} {source}"

    # Do not reject if the page explicitly says international/worldwide support.
    if any(marker in combined for marker in [
        "worldwide", "global remote", "international contractor", "international remote",
        "work from anywhere", "anywhere", "globally distributed", "any country",
    ]):
        return False

    job_regions = detect_regions_from_text(location)
    if not job_regions:
        return False

    allowed_regions = set(policy["allowed_regions"])

    if "us" in job_regions and "us" in allowed_regions and policy.get("us_requires_international_support"):
        # Candidate-specific rule for profiles that can consider US roles only
        # when international contractor/remote support is explicit. This does
        # not affect US-based users whose automation simply allows US roles.
        return True

    if job_regions & allowed_regions:
        return False

    # If the job says remote + a specific region and that region does not
    # overlap with this user's allowed regions, it is a candidate-specific
    # conflict. This is not global: Remote-US is allowed for US users, Remote
    # Europe is allowed for Europe users, India is allowed for India users, etc.
    remote_region_limited = "remote" in location and bool(job_regions)
    explicit_nonremote_location = "remote" not in location and bool(job_regions)

    if remote_region_limited or explicit_nonremote_location:
        return True

    return False


ROLE_FAMILY_RULES = [
    {
        "name": "sales_business_development",
        "trigger_terms": [
            "sales", "account executive", "business development", "partnerships",
            "revenue", "commercial", "account manager", "client partner",
            "customer account", "key account",
        ],
        "allow_terms": [
            "sales", "business development", "partnerships", "revenue growth",
            "commercial", "account management", "account executive", "client relations",
            "client relationship", "customer account", "key account", "customer success",
            "business growth", "business development manager", "sales operations",
        ],
    },
    {
        "name": "growth_marketing_gtm",
        "trigger_terms": [
            "go-to-market", "gtm", "head of growth", "growth manager",
            "growth marketing", "marketing", "demand generation",
        ],
        "allow_terms": [
            "go-to-market", "gtm", "growth", "growth marketing", "marketing",
            "demand generation", "brand marketing", "performance marketing",
            "campaign", "acquisition", "lifecycle marketing",
        ],
    },
    {
        "name": "technical_field_leadership",
        "trigger_terms": [
            "regional field cto", "field cto", "technical evangelist",
            "developer advocate", "solutions architect", "solution architect",
        ],
        "allow_terms": [
            "field cto", "solutions architect", "solution architect", "sales engineer",
            "solutions engineer", "technical evangelist", "developer advocate",
            "pre-sales", "presales", "cloud architect", "customer engineer",
        ],
    },
    {
        "name": "general_management",
        "trigger_terms": ["general manager", "gm", "country manager", "operations director"],
        "allow_terms": [
            "general manager", "country manager", "operations director", "operations management",
            "business operations", "business growth", "commercial operations", "p&l",
            "profit and loss", "site leadership", "branch operations",
        ],
    },
]

GLOBAL_PRE_REVIEW_WRONG_FUNCTION_MARKERS = [
    # Keep this list intentionally tiny. Most role-family decisions must be
    # candidate-aware through ROLE_FAMILY_RULES + the automation/search contract.
    "commercial co-founder",
]


def candidate_match_context_blob(search_contract=None, automation=None, user_profile=None):
    prefs = extract_job_preferences(automation or {})
    context = {
        "automation_preferences": prefs,
        "automation_job_titles": extract_automation_job_titles(automation or {}, limit=80),
        "search_contract_allowed_titles": (search_contract or {}).get("allowed_titles", []),
        "search_contract_allowed_functions": (search_contract or {}).get("allowed_functions", []),
        "search_contract_seniority_rules": (search_contract or {}).get("seniority_rules", ""),
        "search_contract_source_strategy": (search_contract or {}).get("source_strategy", []),
        "candidate_cv_title": ((user_profile or {}).get("cv") or {}).get("title", ""),
        "candidate_cv_summary": ((user_profile or {}).get("cv") or {}).get("summary", ""),
    }
    return _norm_words(json.dumps(context, default=str))


def candidate_allows_terms(search_contract=None, automation=None, user_profile=None, terms=None):
    terms = [str(term).lower() for term in (terms or []) if str(term).strip()]
    if not terms:
        return False
    blob = candidate_match_context_blob(search_contract, automation, user_profile)
    return any(term in blob for term in terms)


def candidate_forbids_title_or_function(job, search_contract=None):
    """Reject only when the candidate-specific contract forbids the role.

    This replaces broad global title bans. If the contract says Software Engineer,
    Sales, Business Development, etc. is forbidden for this specific candidate,
    then the cheap filter can reject. Otherwise borderline roles should reach AI
    review instead of being killed globally.
    """
    if not isinstance(search_contract, dict):
        return False

    title = _norm_words(job.get("title", ""))
    description = _norm_words(job.get("description", ""))
    combined = f"{title} {description}"

    allowed_terms = [
        _norm_words(x)
        for x in (search_contract.get("allowed_titles", []) + search_contract.get("allowed_functions", []))
        if str(x).strip()
    ]
    forbidden_terms = [
        _norm_words(x)
        for x in (search_contract.get("forbidden_titles", []) + search_contract.get("forbidden_functions", []))
        if str(x).strip()
    ]

    for forbidden in forbidden_terms:
        if not forbidden or len(forbidden) < 3:
            continue
        if forbidden in combined:
            # If a term is both forbidden and explicitly allowed due to a noisy
            # contract, do not cheap-reject. Let AI review resolve the conflict.
            if any(allowed and allowed in combined for allowed in allowed_terms):
                return False
            return True

    return False


def role_family_conflicts_candidate(job, search_contract=None, automation=None, user_profile=None):
    title = _norm_words(job.get("title", ""))

    # Check trigger terms on title only. Descriptions legitimately mention words
    # like "commercial", "revenue", or "account" in technical contexts (government
    # contracting, healthcare, UC engineering), which would cause false positives
    # if we scanned the description. Role family is a title-level concept; the AI
    # review step handles description-level nuance.
    for rule in ROLE_FAMILY_RULES:
        if not any(term in title for term in rule["trigger_terms"]):
            continue
        if candidate_allows_terms(
            search_contract=search_contract,
            automation=automation,
            user_profile=user_profile,
            terms=rule["allow_terms"],
        ):
            continue
        return True, f"wrong_function_pre_review:{rule['name']}"

    return False, "role_family_ok"


def cheap_pre_review_reject(job, search_contract, user_profile=None, automation=None):
    """Cheap local filter to avoid expensive strict review on obvious misses.

    This runs before remote validation and AI review. It should only reject cases
    that are clearly wrong from title/location/description; borderline judgment
    still belongs to the AI review step.
    """
    title = str(job.get("title", "")).lower()
    location = str(job.get("location", "")).lower()
    description = str(job.get("description", "")).lower()
    source = str(job.get("source", "")).lower()
    contract_blob = json.dumps(search_contract or {}, default=str).lower()
    combined = f"{title} {location} {description} {source}"

    if candidate_forbids_title_or_function(job, search_contract=search_contract):
        return True, "forbidden_by_candidate_contract_pre_review"

    if any(marker in title for marker in GLOBAL_PRE_REVIEW_WRONG_FUNCTION_MARKERS):
        return True, "wrong_function_pre_review"

    role_family_conflict, role_family_reason = role_family_conflicts_candidate(
        job,
        search_contract=search_contract,
        automation=automation,
        user_profile=user_profile,
    )
    if role_family_conflict:
        return True, role_family_reason

    if job_location_conflicts_candidate_policy(
        job,
        user_profile=user_profile,
        automation=automation,
        search_contract=search_contract,
    ):
        return True, "location_conflict_pre_review"

    if "research ml" in title and not any(marker in description for marker in ["backend", "llm", "rag", "api", "product engineer", "application"]):
        return True, "too_ml_research_heavy_pre_review"

    if ("machine learning engineer" in title or "ml engineer" in title) and "platform" in title:
        if not any(marker in description for marker in ["llm", "rag", "api", "backend", "product", "application", "python"]):
            return True, "too_ml_platform_specialized_pre_review"

    if any(marker in title for marker in ["react native", "mobile engineer", "ios engineer", "android engineer"]):
        if not any(marker in description for marker in ["backend", "llm", "rag", "api", "full-stack", "full stack"]):
            return True, "too_mobile_frontend_heavy_pre_review"

    if any(marker in title for marker in ["speech", "audio", "voice", "asr", "tts"]):
        if not any(marker in contract_blob for marker in ["speech", "audio", "voice", "asr", "tts"]):
            return True, "too_speech_audio_specialized_pre_review"

    amounts = extract_salary_amounts_annual_usdish(combined)
    if amounts:
        max_amount = max(amounts)
        if max_amount < 60000:
            return True, "salary_clearly_below_minimum_pre_review"
        if max_amount < 75000 and not has_strong_equity_or_founder_signal(combined):
            return True, "salary_below_minimum_without_equity_pre_review"

    return False, "pre_review_ok"


def safe_borderline_for_waiting_approval(
    reason,
    risk_flags,
    recommended_grade=None,
    search_contract=None,
    automation=None,
    user_profile=None,
):
    blob = (str(reason or "") + " " + " ".join(str(x) for x in (risk_flags or []))).lower()

    if recommended_grade is not None and normalize_grade(recommended_grade) < DISCOVERY_PENDING_MIN_GRADE:
        return False

    markers = set(HARD_PRE_REVIEW_RISK_MARKERS)

    # Candidate-agnostic, candidate-aware: sales/BD/commercial risk language is
    # only a hard blocker for users whose automation/contract does not allow it.
    if candidate_allows_terms(
        search_contract=search_contract,
        automation=automation,
        user_profile=user_profile,
        terms=[
            "sales", "business development", "commercial", "account management",
            "account executive", "partnerships", "revenue growth", "business growth",
        ],
    ):
        markers.discard("sales")
        markers.discard("business development")
        markers.discard("commercial")

    return not any(marker in blob for marker in markers)



ATS_ONLY_MODE_TEXT = """
ATS-ONLY EMERGENCY MODE IS ACTIVE.

Return ONLY direct ATS/company job URLs from approved direct sources.
Do not return generic company career pages.
Do not return aggregators, search pages, mirrors, or job-board URLs.
Do not return HiringCafe, FlexJobs, Built In, USAJOBS, beBee, LinkedIn, Indeed, Glassdoor, ZipRecruiter, EchoJobs, or any blocked final URL.
Avoid domains and URL patterns that failed earlier in this run.
Improve source quality aggressively; fewer strong direct URLs are better than many weak URLs.
Every returned URL must be a direct job post with a real job ID.
"""


FIRST_STRATEGY_PIVOT_GUIDANCE = """
FIRST INTERNAL STRATEGY PIVOT — AFTER ROUND 1 FINAL BATCH
Use this internal pivot to improve the next search attempt.
- Expand title variants.
- Add adjacent but still relevant role families.
- Allow missing salary if otherwise strong.
- Adjust search queries.
- Remove source patterns that keep failing.
- Keep hard location, salary, blocked-domain, and direct-link rules.
"""


SECOND_STRATEGY_PIVOT_GUIDANCE = """
SECOND INTERNAL STRATEGY PIVOT — ROUND 2, 5 EXTRA BATCHES MAX
Use this pivot only for users still below the minimum viable daily count.
- Broaden geography only if not hard.
- Consider one seniority level lower or higher.
- Add industry-adjacent companies.
- Still keep AI review strict.
- Never weaken blocked-domain, direct-apply, or real-job-ID validation rules.
"""


def get_batch_phase_text(round_mode, batch_number):
    if round_mode == "first_round" and 1 <= batch_number <= 5:
        return """
ROUND 1, BATCHES 1-5: EXACT CONTRACT MODE
- Use the exact search contract first.
- Use strict ATS/direct company job links only.
- Do not broaden titles or functions beyond exact/very close contract variants.
- Reject vague keyword matches, generic career pages, search pages, and weak URL evidence.
"""

    if round_mode == "first_round" and 6 <= batch_number <= 10:
        return """
ROUND 1, BATCHES 6-10: ATS-ONLY SOURCE QUALITY MODE
- ATS-only emergency mode is active.
- Avoid rejected domains and failed source patterns from earlier batches.
- Improve source quality before trying broader matching.
- Return fewer jobs rather than weak or generic links.
"""

    if round_mode == "first_round" and 11 <= batch_number <= 12:
        return """
ROUND 1, BATCHES 11-12: FINAL STRICT DIRECT-SOURCE SWEEP
- Keep the current search contract strict.
- Use failed-batch feedback to avoid bad sources and bad title families.
- Try alternate approved ATS/company source patterns.
- Do not broaden into second-pivot rules yet; the first internal strategy pivot happens after Round 1 finishes.
"""

    if round_mode == "second_round":
        return """
ROUND 2, 5-BATCH SECOND STRATEGY MODE
- The second strategy pivot is active.
- Broaden geography only if the automation/search contract does not make geography hard.
- Consider one seniority level lower or higher when sensible.
- Add industry-adjacent companies and adjacent-but-relevant role families.
- Keep AI review strict and keep all direct-link/domain validation rules.
"""

    if round_mode == "minimum_viable":
        return """
MINIMUM VIABLE FALLBACK MODE — up to 2 batches.
This user has fewer than 5 jobs today despite earlier rounds.
- Broaden titles by 2 levels: include adjacent and near-adjacent roles.
- Accept same-domain industry variants (e.g. if target is Account Director, try Marketing Director, Client Partner, Senior Account Manager).
- Return the best available direct ATS URLs even if fit is 60-70% rather than 80%+.
- All direct-URL, domain-blocking, and remote-validation rules still apply.
- Return fewer high-confidence jobs rather than many weak ones.
"""

    return """
DEFAULT STRICT MODE
- Use the search contract and direct job-post links only.
- Avoid weak sources and do not over-broaden.
"""


def should_use_ats_only_mode(round_mode, batch_number):
    return round_mode == "first_round" and 6 <= batch_number <= 10


def ask_openai_for_jobs(
    user_profile,
    automation,
    cv_text,
    persona,
    search_contract,
    batch_number,
    avoid_urls,
    rejected_notes,
    rejected_domains,
    failed_batch_feedback,
    jobs_needed,
    ats_only_mode=False,
    strategy_prompt_patch="",
    max_batches_for_round=FIRST_ROUND_BATCHES,
    round_mode="first_round",
    batch_phase_text="",
    user_plan="unknown",
    job_status=DEFAULT_STATUS,
    plan_rejected_learning=None,
    link_failure_notes=None,
    rejected_companies=None,
    seen_companies_round=None,
):
    prefs = extract_job_preferences(automation)
    candidate_specific_expansion = get_candidate_specific_expansion_text(
        user_profile=user_profile,
        automation=automation,
        persona=persona,
    )
    role_specific_source_mode = get_role_specific_source_mode_text(
        user_profile=user_profile,
        automation=automation,
        persona=persona,
    )

    ats_patterns_text = build_ats_patterns_text()
    automation_titles = extract_automation_job_titles(automation, limit=40)
    cluster_key_hint = title_cluster_key(" ".join(automation_titles)) if automation_titles else "unknown"

    rejected_domains_clean = sorted(
        {
            clean_domain(domain)
            for domain in rejected_domains
            if clean_domain(domain)
        }
    )

    plan_rejected_learning = plan_rejected_learning or []
    link_failure_notes = link_failure_notes or []
    search_cv_brief = build_candidate_search_brief(user_profile, automation, cv_text)
    candidate_location_guidance = location_policy_summary(user_profile, automation, search_contract)
    candidate_count = jobs_to_request(jobs_needed, round_mode)

    prompt = f"""
Find up to {candidate_count} real, currently open jobs for this user.

We need {jobs_needed} more jobs for this user today. Return up to {candidate_count}
candidate jobs because some links will fail validation or AI review. Return fewer
jobs if you cannot find strong direct URLs. Do not pad the response.

Use ALL of this:
1. Automation config
2. Resume/CV
3. Stored persona

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION JOB PREFERENCES:
{json.dumps(prefs, indent=2)}

JOB TITLES EXTRACTED DIRECTLY FROM THIS USER'S AUTOMATION:
{json.dumps(automation_titles, indent=2)}

AUTOMATION TITLE CLUSTER HINT:
{cluster_key_hint}

PERSONA:
{json.dumps(persona, indent=2)}

SEARCH CONTRACT — MUST OBEY BEFORE RETURNING ANY JOB:
{json.dumps(search_contract, indent=2)}

COMPACT CV / SEARCH BRIEF:
{search_cv_brief}

BATCH:
This is {round_mode} batch #{batch_number} of {max_batches_for_round}.

PLAN / OUTPUT STATUS:
- Detected user plan: {user_plan}
- If jobs pass all validation/review, they will be posted with status: {job_status}
- This status is handled by the script, but use the plan context to respect user expectations.

COMPACT USER-REJECTION LEARNING:
{json.dumps(plan_rejected_learning[-40:], indent=2)}

For starter/max users, the compact user-rejection list is a strong search signal.
Avoid repeating rejected companies, title families, domains, or role patterns unless
the new job clearly fixes the rejection reason. Keep this efficient: infer patterns,
do not overfit to one rejected job.

RECENT LINK FAILURES TO AVOID, ESPECIALLY 404/EXPIRED:
{json.dumps(link_failure_notes[-80:], indent=2)}

Never return a URL that looks like these failed links. Prefer freshly verified direct
ATS/company URLs with stable job IDs and visible apply pages.

LOCATION POLICY FOR THIS USER:
{candidate_location_guidance}

BATCH PHASE STRATEGY — THIS TAKES PRIORITY OVER GENERAL BROADER SEARCH RULES:
{batch_phase_text or get_batch_phase_text(round_mode, batch_number)}

BROADER SEARCH RULES:
{ROLE_EXPANSION_TEXT}

CANDIDATE-SPECIFIC EXPANSION:
{candidate_specific_expansion}

ROLE-SPECIFIC SOURCE MODE:
{role_specific_source_mode}

ATS-ONLY EMERGENCY MODE:
{ATS_ONLY_MODE_TEXT if ats_only_mode else "Not active for this batch."}

ADAPTIVE STRATEGY PATCH:
{strategy_prompt_patch or "No adaptive strategy patch yet. Use the current search contract exactly."}

If the adaptive patch conflicts with automation hard rules, blocked-domain rules,
direct-link rules, or the current search contract hard_reject_rules, the hard
rules win.

DIRECT ATS SEARCH PATTERNS:
Use these patterns by replacing target_title and location_or_remote with the candidate's titles and location rules.
Prefer these sources before any other source.

{ats_patterns_text}

SEARCH CONTRACT RULES:
- You are a strict sourcing engine, not a general job search assistant.
- Only return jobs that satisfy the SEARCH CONTRACT after applying the direct ATS provider override.
- If the SEARCH CONTRACT appears to block a known direct ATS provider, ignore that part and only reject exact failed URLs, expired posts, generic/search pages, mirrors, and true aggregators.
- If a job violates any hard_reject_rules, do not return it.
- If title/function/location/salary evidence is unclear, reject silently and return fewer jobs.
- Do not use keyword overlap as evidence. The daily function must match allowed_functions.
- Do not return forbidden_titles or forbidden_functions.
- Use search_queries and source_strategy from the SEARCH CONTRACT first.

VERY IMPORTANT LINK RULES:
- Return only direct job post URLs.
- Prefer ATS/direct apply links. Company-hosted specific job pages like /careers/<id>, /jobs/<role-slug>, /o/<role-slug>, or /apply/<role-slug> are allowed when they are not job-list homepages.
- HiringCafe is allowed for discovery only: use it to identify company/title, then find and return the company/ATS direct apply URL.
- If you find a promising lead on HiringCafe, Wellfound, YC, Startup.jobs, a VC board, or another job board, search company name + exact job title and return only the direct ATS/company URL.
- Do not return a generic company careers homepage.
- Do not return a search results page.
- Do not return aggregator/mirror job pages.
- Do not return URLs from cached snippets where the job may already be closed.
- Do not return URLs that contain /404, /not-found, /job-not-found, /expired, or /closed.
- Do not return jobs whose page says job not found, posting not found, no longer accepting applications, expired, filled, or closed.
- If a direct URL cannot be found, return fewer jobs instead of guessing.
- If a source/domain appeared in rejected domains, do not use it again in this run unless it is a broad ATS domain and the previous failure was an individual expired job.
- Return max 1 job per company per batch.
- Use HiringCafe only for discovery; never return a HiringCafe URL as the final job_url.
- Do the same for Wellfound, YC, Startup.jobs, VC boards, and other mirrors: use as discovery only, then return the direct company/ATS URL.

DO NOT RETURN:
- LinkedIn
- Indeed
- Glassdoor
- ZipRecruiter
- Built In or BuiltIn city sites
- USAJOBS
- beBee
- HiringCafe final URLs
- FlexJobs
- EchoJobs
- Lensa
- Talent
- Jooble
- Wellfound
- HiringCafe final URLs
- YC / Y Combinator final URLs
- Startup.jobs final URLs
- Hubmub final URLs
- RemoteRocketship final URLs
- SpainJobs / Trabajas final URLs
- Remote.co
- We Work Remotely
- generic job boards
- search pages
- generic career homepages
- invented URLs
- repeated avoided URLs
- repeated rejected domains
- more than one job from the same company in the same batch

MATCHING RULES:
- The job must match the automation config.
- The job must match the CV.
- Grade must be 1-100 using the resume, automation config, and persona. Never use a 1-5 or 1-10 scale.
- Description must explain why it fits in one short paragraph.
- If salary is available and below minimum acceptable salary, reject it.
- If location does not match THIS USER'S automation/search-contract location rules, reject it. Do not use hardcoded global country bans; US/Canada/India/Europe/etc. can be valid for users whose profile or automation allows them.
- Return fresh different jobs from previous batches.

COMPANIES CONFIRMED DEAD THIS RUN (all URLs 404/expired/hallucinated) — NEVER RETURN ANY JOB FROM THESE COMPANIES UNDER ANY URL:
{json.dumps(sorted(rejected_companies or [])[-120:], indent=2)}

COMPANIES ALREADY TRIED THIS ROUND — prefer entirely different companies; only return if you have a verified new direct ATS URL that has NOT been tried:
{json.dumps(sorted(seen_companies_round or [])[-150:], indent=2)}

AVOID THESE URLS:
{json.dumps(list(avoid_urls)[-300:], indent=2)}

REJECTED DOMAINS:
{json.dumps(rejected_domains_clean[-120:], indent=2)}

PREVIOUS REJECTIONS:
{json.dumps(rejected_notes[-200:], indent=2)}

FAILED BATCH FEEDBACK:
{json.dumps(failed_batch_feedback[-20:], indent=2)}

JSON OUTPUT SAFETY RULES:
- Return one JSON object only: {{"jobs": [...]}}.
- Use double quotes for every string.
- Do not include markdown or explanations outside JSON.
- Keep each description under 260 characters.
- Do not paste long job-page text, bullets, HTML, or multiline descriptions.
- Escape internal quotes or rewrite the sentence without quotes.
- If no strong direct jobs are found, return {{"jobs": []}}.

Return JSON only.
"""

    for attempt in range(1, 3):
        try:
            response = openai_web_search_call(prompt)
            raw_output = getattr(response, "output_text", "")
            try:
                return parse_json_output(raw_output)
            except json.JSONDecodeError as e:
                print(f"JSON parse error from OpenAI search attempt {attempt}: {e}")
                repaired = repair_job_json_output(raw_output)
                if repaired is not None:
                    print(f"JSON repair recovered {len(repaired.get('jobs', []))} jobs from failed search output.")
                    return repaired
                raise
        except json.JSONDecodeError:
            prompt += f"""

Your previous response was invalid JSON. Retry with a smaller result set: return up to {max(4, min(6, candidate_count))} jobs only.
Return valid JSON only that matches the schema.
Do not include markdown, comments, trailing commas, multiline strings, or incomplete job objects.
"""
        except Exception:
            raise

    return {"jobs": []}


# ============================================================
# AI REVIEW BEFORE POSTING
# ============================================================

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "job_url": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["exact_match", "strong_adjacent", "safe_fallback", "discovery_pending", "bad_match"],
                    },
                    "confidence": {"type": "integer"},
                    "recommended_grade": {"type": "integer"},
                    "reason": {"type": "string"},
                    "risk_flags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "title",
                    "company",
                    "job_url",
                    "decision",
                    "confidence",
                    "recommended_grade",
                    "reason",
                    "risk_flags",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
    "additionalProperties": False,
}


def review_jobs_with_ai(user_profile, automation, cv_text, persona, jobs, minimum_viable_mode=False):
    prefs = extract_job_preferences(automation)

    # Compress prefs to only the fields that drive the review decision.
    # Full prefs can be 3-5KB; this trims to ~400 chars without losing signal.
    review_prefs = {k: prefs[k] for k in (
        "jobTitles", "location", "remote", "salary", "salaryMin", "salaryMax",
        "forbiddenTitles", "forbidden", "employmentType", "seniority",
    ) if k in prefs and prefs[k] not in (None, [], {}, "")}

    # Trim job descriptions in the review payload — title/company/location already
    # carry 80% of the signal; 800 chars of description is enough for the rest.
    jobs_for_review = [
        {**j, "description": (j.get("description") or "")[:800]}
        for j in jobs
    ]

    # CV summary for review: first 1500 chars cover current role + recent experience.
    cv_for_review = (cv_text or "")[:1500]

    minimum_viable_note = ""
    if minimum_viable_mode:
        minimum_viable_note = f"""
MINIMUM VIABLE FALLBACK: This user has fewer than {MIN_ACCEPTABLE_JOBS_PER_USER} jobs today.
Accept safe_fallback at grade {MIN_VIABLE_SAFE_FALLBACK_GRADE}+ when the URL is direct and verified.
Accept discovery_pending at grade {MIN_VIABLE_CONFIDENCE}+ when the title/function broadly fits.
Only use bad_match for clear-cut mismatches (wrong function, obvious location conflict, 404-style URL).

"""

    prompt = f"""
Review these jobs BEFORE they are posted to the user.

{minimum_viable_note}This script is running 15-day quota mode. The target is 10 useful, real, direct-apply jobs per user.
Do not pretend every job is perfect. Classify into honest tiers.

Use:
- Resume/CV
- Automation config
- Persona
- Job title
- Company
- Job location if available
- Job URL/source
- Job description/reason already generated

Decision tiers:
- exact_match: Tier A. Best roles. Direct URL. Strong fit. Post normally.
- strong_adjacent: Tier B. Not perfect, but clearly useful. Direct URL. Post normally.
- safe_fallback: Tier C. Still relevant, location-safe, direct URL, not a trash match. Only used to reach quota. Post normally.
- discovery_pending: Tier D. High-potential role, valid/direct link, but needs human check. Post as pending_review for manual-review users.
- bad_match: Reject.

Strict rejection rules:
- Reject if the function is clearly wrong.
- Reject if seniority is clearly impossible.
- Reject if location clearly conflicts with THIS USER'S automation config/search rules.
- Reject if salary is known and clearly below the minimum acceptable salary with no equity/ownership exception.
- Reject if the role seems invented, generic, closed, or not a specific direct job page.
- Do not reject just because it is not perfect; use strong_adjacent or safe_fallback when it is useful and safe.
- recommended_grade must be an integer from 1 to 100. Never use a 1-5 or 1-10 scale. Example: a strong match should be 80-95, not 4 or 5.

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION CONFIG:
{json.dumps(review_prefs, indent=2)}

PERSONA:
{json.dumps(persona, indent=2)}

CV:
{cv_for_review}

JOBS TO REVIEW:
{json.dumps(jobs_for_review, indent=2)}

Return strict JSON only.
"""

    response = client.responses.create(
        model=REVIEW_MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "job_review_results",
                "schema": REVIEW_SCHEMA,
                "strict": True,
            }
        },
    )

    return parse_json_output(response.output_text)


def review_and_filter_jobs_without_gpt(user_profile, automation, persona, jobs, job_status=DEFAULT_STATUS, search_contract=None, minimum_viable_mode=False):
    """Deterministic persona-fit review used only in --nogpt mode.

    No-GPT mode still has a real filter before posting. A job must already have
    passed static URL validation, remote/live validation, and cheap pre-review.
    This final gate then checks candidate persona/search-contract fit using
    automation titles, local persona, allowed/forbidden functions, role-family
    rules, location policy, and a local match score.
    """
    approved = []
    rejected = []
    grade_floor = NOGPT_APPROVE_MIN_GRADE_MINIMUM_VIABLE if minimum_viable_mode else NOGPT_APPROVE_MIN_GRADE
    persona_floor = NOGPT_PERSONA_FIT_MIN_SCORE_MINIMUM_VIABLE if minimum_viable_mode else NOGPT_PERSONA_FIT_MIN_SCORE
    print("\nNO-GPT PERSONA REVIEW BEFORE POSTING:")
    print(f"Reviewing {len(jobs)} live jobs locally with grade floor {grade_floor} and persona-fit floor {persona_floor}...")

    for job in jobs:
        persona_ok, persona_score, persona_reason, persona_risk_flags = nogpt_persona_fit_review(
            job,
            automation=automation,
            persona=persona,
            search_contract=search_contract,
            user_profile=user_profile,
            minimum_viable_mode=minimum_viable_mode,
        )

        local_score = local_inventory_match_score(
            job,
            automation=automation,
            persona=persona,
            search_contract=search_contract,
            user_profile=user_profile,
        )
        source_grade = normalize_grade(job.get("grade", 0))
        recommended_grade = max(source_grade, local_score, persona_score)

        if recommended_grade < grade_floor:
            rejected.append({
                **job,
                "grade": recommended_grade,
                "persona_fit_score": persona_score,
                "review_decision": "nogpt_persona_reject",
                "review_confidence": 0,
                "review_reason": f"No-GPT local grade {recommended_grade} below floor {grade_floor}. {persona_reason}",
                "risk_flags": persona_risk_flags or ["low_local_grade"],
            })
            print(f"LOCAL PERSONA REVIEW SKIP: {job.get('title')} — {job.get('company')} — grade {recommended_grade} < {grade_floor}; persona {persona_score}")
            continue

        if not persona_ok:
            rejected.append({
                **job,
                "grade": recommended_grade,
                "persona_fit_score": persona_score,
                "review_decision": "nogpt_persona_reject",
                "review_confidence": 0,
                "review_reason": persona_reason,
                "risk_flags": persona_risk_flags or ["persona_fit_rejected"],
            })
            print(f"LOCAL PERSONA REVIEW SKIP: {job.get('title')} — {job.get('company')} — persona {persona_score}: {persona_reason}")
            continue

        approved_job = {
            "job_url": job["job_url"],
            "title": job["title"],
            "description": job["description"],
            "company": job["company"],
            "grade": recommended_grade,
            "persona_fit_score": persona_score,
            "status": DISCOVERY_PENDING_STATUS if minimum_viable_mode else job_status,
            "review_decision": "nogpt_persona_approved",
            "match_tier": "nogpt_persona_approved",
            "review_confidence": min(88, max(60, persona_score, recommended_grade)),
            "review_reason": (
                "No-GPT persona approval: passed static URL validation, remote live check, "
                "candidate-aware pre-review, local grade floor, and deterministic persona-fit gate. "
                + persona_reason
            ),
        }
        for optional_key in ["location", "source", "inventory_local_score", "cluster_key"]:
            if job.get(optional_key) not in {None, ""}:
                approved_job[optional_key] = job.get(optional_key)
        approved.append(approved_job)
        print("\nLOCAL PERSONA REVIEW APPROVED:")
        print(f"• {approved_job['title']} — {approved_job['company']}")
        print(f"  Grade: {approved_job['grade']}")
        print(f"  Persona fit: {approved_job['persona_fit_score']}")
        print(f"  Status stored: {approved_job.get('status')}")
        print(f"  URL: {approved_job['job_url']}")

    return approved, rejected


def review_and_filter_jobs(user_profile, automation, cv_text, persona, jobs, job_status=DEFAULT_STATUS, search_contract=None, minimum_viable_mode=False):
    if not jobs:
        return [], []

    if NO_GPT_MODE:
        return review_and_filter_jobs_without_gpt(
            user_profile=user_profile,
            automation=automation,
            persona=persona,
            jobs=jobs,
            job_status=job_status,
            search_contract=search_contract,
            minimum_viable_mode=minimum_viable_mode,
        )

    print("\nAI REVIEW BEFORE POSTING:")
    print(f"Reviewing {len(jobs)} live jobs...")
    if minimum_viable_mode:
        print("  [minimum viable mode — loosened thresholds]")

    try:
        review = review_jobs_with_ai(
            user_profile=user_profile,
            automation=automation,
            cv_text=cv_text,
            persona=persona,
            jobs=jobs,
            minimum_viable_mode=minimum_viable_mode,
        )
    except Exception as e:
        print(f"AI review failed. No jobs from this batch will be posted. Error: {e}")
        rejected = []
        for job in jobs:
            rejected.append({
                **job,
                "review_decision": "review_failed",
                "review_confidence": 0,
                "review_reason": str(e)[:200],
                "risk_flags": ["ai_review_failed"],
            })
        return [], rejected

    reviews = review.get("reviews", [])
    review_by_url = {}

    for item in reviews:
        review_by_url[normalize_url(item.get("job_url", ""))] = item

    approved = []
    rejected = []

    for job in jobs:
        norm = normalize_url(job.get("job_url", ""))
        item = review_by_url.get(norm)

        if not item:
            rejected.append({
                **job,
                "review_decision": "missing_review",
                "review_confidence": 0,
                "review_reason": "AI did not return review for this job.",
                "risk_flags": ["missing_review"],
            })
            print(f"REVIEW SKIP: {job.get('title')} — {job.get('company')} — missing_review")
            continue

        decision = item.get("decision")
        confidence = int(item.get("confidence", 0))
        recommended_grade = normalize_grade(item.get("recommended_grade", job.get("grade", 75)))
        reason = item.get("reason", "")
        risk_flags = item.get("risk_flags") or []

        reviewed_job = {
            **job,
            "grade": recommended_grade,
            "review_decision": decision,
            "review_confidence": confidence,
            "review_reason": reason,
            "risk_flags": risk_flags,
        }

        # 15-day quota mode tiers:
        # Tier A exact_match, Tier B strong_adjacent, Tier C safe_fallback post normally.
        # Tier D discovery_pending posts only for manual-review style users as status=pending_review.
        normal_post_decisions = {"exact_match", "strong_adjacent", "safe_fallback", "good_match"}
        manual_review_statuses = {STARTER_MAX_STATUS, DEFAULT_STATUS, DISCOVERY_PENDING_STATUS, "waiting_approval"}

        # ATS-direct sources (Jobo, HC, LinkedIn Apify, resolver) are already
        # verified real open positions — apply a slightly lower floor than OpenAI.
        _is_ats_direct = str(job.get("source", "")).startswith(
            ("jobo_ats", "hiring_cafe", "linkedin_apify", "direct_url_resolver")
        )
        if minimum_viable_mode:
            grade_floor = MIN_VIABLE_SAFE_FALLBACK_GRADE
            conf_floor  = MIN_VIABLE_CONFIDENCE
        elif _is_ats_direct:
            grade_floor = 62
            conf_floor  = 60
        else:
            grade_floor = SAFE_FALLBACK_MIN_GRADE
            conf_floor  = MIN_REVIEW_CONFIDENCE

        allow_normal_post = (
            decision in normal_post_decisions
            and confidence >= conf_floor
            and recommended_grade >= grade_floor
        )
        allow_discovery_pending = (
            decision == "discovery_pending"
            and (job_status in manual_review_statuses or minimum_viable_mode)
            and confidence >= conf_floor
            and recommended_grade >= (MIN_VIABLE_CONFIDENCE if minimum_viable_mode else DISCOVERY_PENDING_MIN_GRADE)
            and (
                minimum_viable_mode
                # Non-starter users (premium/default) go to pending_review for human check anyway —
                # trust the AI reviewer's discovery_pending classification directly.
                or job_status != STARTER_MAX_STATUS
                # Starter users: apply the safety gate before posting to pending_review.
                or safe_borderline_for_waiting_approval(
                    reason,
                    risk_flags,
                    recommended_grade,
                    search_contract=search_contract,
                    automation=automation,
                    user_profile=user_profile,
                )
            )
        )

        if allow_normal_post or allow_discovery_pending:
            stored_status = DISCOVERY_PENDING_STATUS if (allow_discovery_pending or minimum_viable_mode) else job_status
            stored_decision = decision
            approved_job = {
                "job_url": job["job_url"],
                "title": job["title"],
                "description": job["description"],
                "company": job["company"],
                "grade": recommended_grade,
                "status": stored_status,
                "review_decision": stored_decision,
                "match_tier": stored_decision,
                "review_confidence": confidence,
                "review_reason": reason[:500],
            }

            # Preserve useful metadata for audit/debug in selectedJobs.
            for optional_key in ["location", "source", "inventory_local_score", "cluster_key"]:
                if job.get(optional_key) not in {None, ""}:
                    approved_job[optional_key] = job.get(optional_key)

            approved.append(approved_job)

            print("\nREVIEW APPROVED:")
            print(f"• {approved_job['title']} — {approved_job['company']}")
            print(f"  Grade: {approved_job['grade']}")
            print(f"  Tier: {approved_job.get('match_tier')}")
            print(f"  Status stored: {approved_job.get('status')}")
            print(f"  Confidence: {confidence}")
            print(f"  Reason: {reason}")
            print(f"  URL: {approved_job['job_url']}")
        else:
            rejected.append(reviewed_job)

            print("\nREVIEW SKIP:")
            print(f"• {job.get('title')} — {job.get('company')}")
            print(f"  Decision: {decision}")
            print(f"  Confidence: {confidence}")
            print(f"  Reason: {reason}")
            if risk_flags:
                print("  Risk flags:")
                for flag in risk_flags:
                    print(f"    - {flag}")

    return approved, rejected


# ============================================================
# PRINTING
# ============================================================

def print_user_header(user_profile):
    print("\n\n============================================================")
    print(f"USER: {user_profile.get('displayName')} — {user_profile.get('email')}")
    print(f"UID: {user_profile.get('uid')}")
    print("============================================================")


def print_persona(persona):
    print("\nPERSONA:")
    print(json.dumps(persona, indent=2))


def print_jobs_for_user(user_profile, jobs, title="JOBS READY"):
    print("\n------------------------------------------------------------")
    print(f"{title}: {user_profile.get('displayName')} — {user_profile.get('email')}")
    print("------------------------------------------------------------")

    if not jobs:
        print("No jobs.")
        return

    for i, job in enumerate(jobs, start=1):
        print(f"\n{i}. {job['title']} — {job['company']}")
        print(f"Grade: {job['grade']}")
        print(f"Status: {job['status']}")
        if job.get("match_tier"):
            print(f"Tier: {job.get('match_tier')}")
        print(f"URL: {job['job_url']}")
        print(f"Why: {job['description']}")


def print_skip(job, stage, reason):
    print(
        f"SKIP {stage}: {job.get('title', 'Untitled')} — "
        f"{job.get('company', 'Unknown')} — {reason}"
    )



# ============================================================
# REJECTION DOMAIN TRACKING
# ============================================================

BAD_SOURCE_REASONS = {
    "aggregator_page",
    "generic_or_homepage_url",
    "search_page",
    "career_homepage",
    "redirected_to_aggregator",
    "wrong_or_generic_job_page",
}


def maybe_track_rejected_domain(job, reason, rejected_domains):
    domain = url_domain(job.get("job_url", ""))

    if not domain:
        return

    # Never poison broad ATS/company-direct sources. A dead Lever URL should be
    # remembered through avoid_urls/link_failure_notes, not by blocking all Lever.
    if is_non_blockable_direct_source_domain(domain):
        return

    reason = str(reason or "")

    # Only track discovery/mirror/job-board domains at domain level. Do not
    # globally blacklist normal company domains just because one page was a
    # homepage/generic slug; exact failed URLs are already carried in avoid_urls
    # and link_failure_notes. This keeps direct company career pages usable.
    if domain_matches(domain, AGGREGATOR_DOMAINS):
        rejected_domains.add(domain)
        return

    if reason in {"redirected_to_aggregator"}:
        rejected_domains.add(domain)



# ============================================================
# INTERNAL STRATEGY REPORT
# ============================================================

STRATEGY_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "report_title": {"type": "string"},
        "candidate_email": {"type": "string"},
        "candidate_name": {"type": "string"},
        "summary": {"type": "string"},
        "why_under_target": {"type": "array", "items": {"type": "string"}},
        "recommended_relaxations": {"type": "array", "items": {"type": "string"}},
        "new_search_strategy": {"type": "array", "items": {"type": "string"}},
        "operator_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "report_title",
        "candidate_email",
        "candidate_name",
        "summary",
        "why_under_target",
        "recommended_relaxations",
        "new_search_strategy",
        "operator_notes",
    ],
    "additionalProperties": False,
}


def create_zero_approval_strategy_report(
    user_profile,
    automation,
    persona,
    search_contract,
    batch_number,
    failed_batch_feedback,
    rejected_jobs,
    rejected_domains,
    pivot_guidance="",
):
    prefs = extract_job_preferences(automation)

    rejected_summary = []
    for item in rejected_jobs[-80:]:
        rejected_summary.append({
            "title": item.get("title"),
            "company": item.get("company"),
            "location": item.get("location"),
            "url_domain": url_domain(canonical_job_url(item)),
            "status": item.get("status"),
            "feedback": str(item.get("feedback") or "")[:160],
            "reason": item.get("reason")
                or item.get("review_decision")
                or item.get("review_reason"),
            "risk_flags": item.get("risk_flags", []),
        })

    prompt = f"""
Create an internal strategy report for an adaptive job-search engine.

Situation:
- This is an overnight, non-interactive job search run.
- Do NOT ask the operator anything.
- Do NOT create or suggest a user-facing message.
- Do NOT stop the user because of this report.
- Diagnose why the search is underperforming and produce a better internal strategy for the next batches.
- The report is internal feedback used to improve the search contract and prompt.

PIVOT GUIDANCE:
{pivot_guidance or "Use the failed-batch feedback to improve the next search batches without weakening hard rules."}

The new strategy should relax constraints carefully and only where sensible:
- Broaden location only in a controlled way.
- Allow slightly more junior roles when sensible.
- Loosen salary a bit or allow missing salary when the role is otherwise strong.
- Keep the role function/persona aligned. Do not suggest random jobs.
- Make tradeoffs explicit and honest.

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION CONFIG:
{json.dumps(prefs, indent=2)}

PERSONA:
{json.dumps(persona, indent=2)}

CURRENT SEARCH CONTRACT:
{json.dumps(search_contract, indent=2)}

FAILED BATCH FEEDBACK:
{json.dumps(failed_batch_feedback[-20:], indent=2)}

RECENT REJECTIONS:
{json.dumps(rejected_summary, indent=2)}

REJECTED DOMAINS:
{json.dumps(sorted(rejected_domains)[-80:], indent=2)}

Return strict JSON only.
"""

    response = client.responses.create(
        model=SEARCH_MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "zero_approval_strategy_report",
                "schema": STRATEGY_REPORT_SCHEMA,
                "strict": True,
            }
        },
    )

    return json.loads(response.output_text)


def save_strategy_report(user_profile, report):
    uid = user_profile.get("uid") or "unknown_uid"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = STRATEGY_REPORT_DIR / f"strategy_{uid}_{timestamp}.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return path


def print_strategy_report(report, path=None):
    label = path or "strategy report"
    print(f"Strategy report saved: {label}")



ADAPTIVE_SEARCH_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "updated_search_contract": SEARCH_CONTRACT_SCHEMA,
        "prompt_patch": {"type": "string"},
        "changed_rules": {"type": "array", "items": {"type": "string"}},
        "preserved_hard_rules": {"type": "array", "items": {"type": "string"}},
        "next_batch_strategy": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "updated_search_contract",
        "prompt_patch",
        "changed_rules",
        "preserved_hard_rules",
        "next_batch_strategy",
    ],
    "additionalProperties": False,
}


def adapt_search_contract_from_strategy_report(
    user_profile,
    automation,
    cv_text,
    persona,
    search_contract,
    strategy_report,
    failed_batch_feedback,
    rejected_jobs,
    rejected_domains,
    pivot_guidance="",
):
    prefs = extract_job_preferences(automation)

    rejected_summary = []
    for item in rejected_jobs[-100:]:
        rejected_summary.append({
            "title": item.get("title"),
            "company": item.get("company"),
            "location": item.get("location"),
            "url_domain": url_domain(canonical_job_url(item)),
            "status": item.get("status"),
            "feedback": str(item.get("feedback") or "")[:160],
            "reason": item.get("reason")
                or item.get("review_decision")
                or item.get("review_reason"),
            "risk_flags": item.get("risk_flags", []),
        })

    prompt = f"""
You are improving a job search engine after several zero-approval batches.

Use the strategy report as INTERNAL FEEDBACK. Do not write a user-facing report.
Your job is to produce an improved search contract and a compact prompt patch
for the next batches.

PIVOT GUIDANCE:
{pivot_guidance or "Use the report to improve recall while preserving hard constraints."}

Main objective:
- Find more real suitable jobs for the candidate.
- Improve search recall without lowering quality too much.
- Keep all true hard constraints intact.
- Relax only soft constraints or search strategy assumptions.
- Avoid repeating rejected domains, bad URL patterns, wrong title families, and wrong locations.

Rules you must preserve:
- Never allow aggregators, mirrors, search pages, or generic career homepages as final job_url.
- Never relax blocked domains.
- Never ignore direct-apply URL rules.
- Never broaden into unrelated roles.
- If automation says remote only, keep remote only.
- If automation has a hard salary minimum, do not approve known-below-minimum salaries.
- Missing salary may be allowed through sourcing only when role fit is strong; AI review still decides.
- Location, salary, and seniority relaxations must be specific and controlled.

Improve these areas where useful:
- allowed_titles and close title variants
- forbidden_titles that caused false positives
- allowed_functions and forbidden_functions
- search_queries and source_strategy
- hard_reject_rules clarity
- broadening_plan_if_zero_results
- next batch wording to OpenAI web search

USER:
{json.dumps({
    "uid": user_profile.get("uid"),
    "displayName": user_profile.get("displayName"),
    "email": user_profile.get("email"),
}, indent=2)}

AUTOMATION CONFIG:
{json.dumps(prefs, indent=2)}

PERSONA:
{json.dumps(persona, indent=2)}

CV:
{cv_text}

CURRENT SEARCH CONTRACT:
{json.dumps(search_contract, indent=2)}

INTERNAL STRATEGY REPORT:
{json.dumps(strategy_report, indent=2)}

FAILED BATCH FEEDBACK:
{json.dumps(failed_batch_feedback[-30:], indent=2)}

RECENT REJECTIONS:
{json.dumps(rejected_summary, indent=2)}

REJECTED DOMAINS:
{json.dumps(sorted(rejected_domains)[-120:], indent=2)}

Return strict JSON only.
"""

    response = client.responses.create(
        model=SEARCH_MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "adaptive_search_contract",
                "schema": ADAPTIVE_SEARCH_CONTRACT_SCHEMA,
                "strict": True,
            }
        },
    )

    return json.loads(response.output_text)


def save_strategy_adaptation(user_profile, adaptation):
    uid = user_profile.get("uid") or "unknown_uid"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = STRATEGY_REPORT_DIR / f"adaptation_{uid}_{timestamp}.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(adaptation, f, indent=2)

    return path


def maybe_create_strategy_pivot(
    user_profile,
    automation,
    cv_text,
    persona,
    search_contract,
    batch_number,
    posted_jobs_this_round,
    failed_batch_feedback,
    rejected_jobs,
    rejected_domains,
    strategy_pivot_count,
    pivot_stage,
    pivot_guidance,
    force=False,
):
    """Create an internal, non-interactive strategy pivot.

    This never asks the operator anything and never creates a user-facing message.
    It saves the report/adaptation locally, updates the search contract, and returns
    a prompt patch for later batches/rounds.
    """

    if NO_GPT_MODE:
        print(f"\nSTRATEGY PIVOT SKIPPED: --nogpt mode is active ({pivot_stage}).")
        return None

    if not force:
        return None

    if strategy_pivot_count >= MAX_STRATEGY_PIVOTS_PER_USER:
        print(
            f"\nSTRATEGY PIVOT SKIPPED: reached MAX_STRATEGY_PIVOTS_PER_USER="
            f"{MAX_STRATEGY_PIVOTS_PER_USER} for this user."
        )
        return None

    print(
        f"\nSTRATEGY PIVOT: {pivot_stage}. Creating an internal report, "
        "improving the search contract/prompt, then continuing automatically."
    )

    try:
        pivot_feedback = list(failed_batch_feedback or [])
        pivot_feedback.append({
            "batch": batch_number,
            "reason": "scheduled_internal_strategy_pivot",
            "message": pivot_guidance,
            "pivot_stage": pivot_stage,
        })

        report = create_zero_approval_strategy_report(
            user_profile=user_profile,
            automation=automation,
            persona=persona,
            search_contract=search_contract,
            batch_number=batch_number,
            failed_batch_feedback=pivot_feedback,
            rejected_jobs=rejected_jobs,
            rejected_domains=rejected_domains,
            pivot_guidance=pivot_guidance,
        )
        report_path = save_strategy_report(user_profile, report)
        print_strategy_report(report, path=report_path)

        adaptation = adapt_search_contract_from_strategy_report(
            user_profile=user_profile,
            automation=automation,
            cv_text=cv_text,
            persona=persona,
            search_contract=search_contract,
            strategy_report=report,
            failed_batch_feedback=pivot_feedback,
            rejected_jobs=rejected_jobs,
            rejected_domains=rejected_domains,
            pivot_guidance=pivot_guidance,
        )
        adaptation_path = save_strategy_adaptation(user_profile, adaptation)

        updated_search_contract = adaptation.get("updated_search_contract") or search_contract
        updated_search_contract, _ = normalize_search_contract_for_automation(
            updated_search_contract,
            automation,
            user_profile=user_profile,
        )
        search_contract_path_saved = save_search_contract(user_profile, updated_search_contract)

        print(f"Strategy pivot [{pivot_stage}]: adaptation saved → {adaptation_path}")

        return {
            "pivot_stage": pivot_stage,
            "report": report,
            "report_path": str(report_path),
            "adaptation": adaptation,
            "adaptation_path": str(adaptation_path),
            "search_contract": updated_search_contract,
            "prompt_patch": adaptation.get("prompt_patch", ""),
            "posted_jobs_so_far": len(posted_jobs_this_round),
        }

    except Exception as e:
        print(f"Failed to create strategy pivot: {e}")
        return {
            "pivot_stage": pivot_stage,
            "report": None,
            "report_path": None,
            "adaptation": None,
            "adaptation_path": None,
            "search_contract": search_contract,
            "prompt_patch": "",
            "posted_jobs_so_far": len(posted_jobs_this_round),
            "error": str(e)[:300],
        }



# ============================================================
# 15-DAY QUOTA MODE: AUTOMATION-TITLE CLUSTER PREFETCH
# ============================================================

ROLE_TITLE_HINT_WORDS = {
    "engineer", "developer", "devops", "sre", "cloud", "platform", "security",
    "architect", "product", "manager", "director", "lead", "analyst", "data",
    "scientist", "designer", "creative", "marketing", "growth", "sales", "account",
    "executive", "customer", "support", "success", "operations", "admin", "assistant",
    "coordinator", "specialist", "consultant", "compliance", "risk", "finance",
    "warehouse", "logistics", "clerk", "technician", "nurse", "clinical", "recruiter",
}

TITLE_CLUSTER_PATTERNS = [
    ("engineering_ai_backend", ["software", "engineer", "developer", "backend", "full stack", "full-stack", "python", "ai", "machine learning", "ml", "llm", "rag", "founding", "technical co-founder", "devops", "sre", "cloud", "platform"]),
    ("product_management", ["product manager", "technical product", "platform product", "product owner", "group product", "principal product"]),
    ("sales_revenue", ["account executive", "sales", "business development", "partnership", "revenue", "account manager", "enterprise account"]),
    ("marketing_growth", ["marketing", "growth", "brand", "content", "email", "seo", "performance", "paid media", "campaign"]),
    ("design_creative", ["designer", "graphic", "visual", "creative", "art director", "ux", "ui", "brand designer"]),
    ("admin_support", ["administrative", "admin", "assistant", "data entry", "records", "clerk", "customer service", "customer support", "help desk", "library"]),
    ("data_analytics", ["data analyst", "data analytics", "bi", "business intelligence", "data engineer", "analytics", "reporting"]),
    ("operations_project", ["operations", "program manager", "project manager", "pmo", "business operations", "coordinator", "transformation"]),
    ("compliance_risk", ["compliance", "kyc", "aml", "risk", "audit", "third-party", "global mobility"]),
    ("healthcare_ops", ["healthcare", "clinical", "patient", "clinic", "medical", "pharma", "trial", "care"]),
    ("warehouse_logistics", ["warehouse", "inventory", "logistics", "forklift", "material handler", "shipping", "receiving"]),
    ("finance_accounting", ["finance", "accounting", "bookkeeper", "controller", "fp&a", "financial analyst"]),
]

TITLE_EXTRACTION_KEYWORDS = [
    "title", "titles", "target", "targets", "role", "roles", "position", "positions",
    "jobtitle", "jobtitles", "desired", "preferred",
]


def split_possible_titles(value):
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(split_possible_titles(item))
        return out
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out.extend(split_possible_titles(v))
        return out

    text = str(value or "").strip()
    if not text:
        return []

    # Preserve slash titles like "DevOps/SRE" but split long preference text.
    parts = re.split(r"\n|;|\||,(?=\s*[A-Z0-9])", text)
    if len(parts) == 1 and len(text) > 90:
        parts = re.split(r",|/", text)

    output = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip(" -•\t")
        if 3 <= len(part) <= 90:
            output.append(part)
    return output


def looks_like_role_title(text):
    t = str(text or "").strip().lower()
    if not t or len(t) < 3 or len(t) > 90:
        return False
    if "@" in t or "http" in t or "/" in t and len(t.split()) > 8:
        return False
    if re.fullmatch(r"[a-z]{2,}(,\s*[a-z]{2,})*", t) and not any(w in t for w in ROLE_TITLE_HINT_WORDS):
        return False
    return any(word in t for word in ROLE_TITLE_HINT_WORDS)


def extract_automation_job_titles(automation, limit=40):
    prefs = extract_job_preferences(automation)
    titles = []

    def walk(value, key_path=""):
        key_l = key_path.lower().replace("_", "")
        key_relevant = any(k in key_l for k in TITLE_EXTRACTION_KEYWORDS)

        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{key_path}.{k}" if key_path else str(k))
            return

        if isinstance(value, list):
            if key_relevant:
                for item in value:
                    for title in split_possible_titles(item):
                        if looks_like_role_title(title):
                            titles.append(title)
            else:
                for item in value:
                    walk(item, key_path)
            return

        if key_relevant:
            for title in split_possible_titles(value):
                if looks_like_role_title(title):
                    titles.append(title)

    walk(prefs)

    # Fallback: if the automation uses unusual keys, scan short strings in the
    # preferences that look like role titles.
    if not titles:
        for title in split_possible_titles(prefs):
            if looks_like_role_title(title):
                titles.append(title)

    seen = set()
    output = []
    for title in titles:
        norm = re.sub(r"\s+", " ", str(title).strip().lower())
        if not norm or norm in seen:
            continue
        seen.add(norm)
        output.append(str(title).strip())
        if len(output) >= limit:
            break
    return output


def title_cluster_key(title):
    t = str(title or "").lower()
    for key, markers in TITLE_CLUSTER_PATTERNS:
        if any(marker in t for marker in markers):
            return key
    words = [w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2]
    return "role_" + "_".join(words[:3]) if words else "uncategorized"


def build_automation_title_clusters(users):
    clusters = {}
    for user in users:
        uid = user.get("uid")
        email = user.get("email")
        automation = user.get("_automation_cache")
        if automation is None and uid:
            try:
                automation = get_user_automation(uid)
                user["_automation_cache"] = automation
            except Exception:
                automation = None
        titles = extract_automation_job_titles(automation or {})
        for title in titles:
            key = title_cluster_key(title)
            bucket = clusters.setdefault(key, {"titles": [], "users": set(), "emails": set()})
            if title not in bucket["titles"]:
                bucket["titles"].append(title)
            if uid:
                bucket["users"].add(uid)
            if email:
                bucket["emails"].add(email)

    # Highest-user clusters first; then most title coverage.
    ordered = sorted(
        clusters.items(),
        key=lambda kv: (len(kv[1]["users"]), len(kv[1]["titles"])),
        reverse=True,
    )[:MAX_AUTOMATION_TITLE_CLUSTERS]

    return {
        key: {
            "titles": value["titles"][:35],
            "user_count": len(value["users"]),
            "sample_emails": sorted(value["emails"])[:8],
        }
        for key, value in ordered
    }


def prefetch_jobs_for_title_cluster(cluster_key, cluster_data):
    titles = cluster_data.get("titles", [])[:35]
    if not titles:
        return []

    prompt = f"""
Daily cluster prefetch for Jobbyo job inventory.

Goal: find a broad reusable pool of real, currently open jobs for multiple users.
This is NOT for one user. These jobs will later be matched per user using each
user's own automation/CV/location/salary rules.

Cluster key: {cluster_key}
Active users in this cluster: {cluster_data.get('user_count')}
Job titles extracted directly from active automations:
{json.dumps(titles, indent=2)}

Return up to {CLUSTER_PREFETCH_JOBS_PER_CLUSTER} jobs.

Source rules:
- Prefer direct ATS/company URLs: Lever, Ashby, Greenhouse, Workable, Workday,
  SmartRecruiters, BambooHR, Teamtailor, Recruitee, Personio, iCIMS, Jobvite,
  Rippling, ADP, Dayforce, ApplyToJob, company-hosted /careers/<specific-job> pages.
- You may use HiringCafe, Wellfound, YC, Built In, LinkedIn, VC job boards, or
  newsletters only as discovery clues, then return the direct company/ATS URL.
- Do not return aggregator, forum, community, newsletter, or job-board URLs as final job_url.
- Do not invent URLs. Return fewer jobs if direct URLs are not findable.
- Include diverse companies and regions. Do not overfit to one user.
- Grade should estimate broad relevance to the cluster, 1-100.

Return JSON only.
"""
    try:
        response = openai_web_search_call(prompt)
        data = parse_json_output(response.output_text)
        jobs = data.get("jobs", [])
    except Exception as e:
        print(f"Cluster prefetch failed for {cluster_key}: {e}")
        return []

    cleaned = []
    seen = set()
    for job in jobs:
        job = normalize_job_record(job)
        norm = normalize_url(canonical_job_url(job))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        job["source"] = job.get("source") or f"cluster_prefetch:{cluster_key}"
        job["cluster_key"] = cluster_key
        job["prefetch_cluster_titles"] = titles[:12]
        cleaned.append(job)
    return cleaned


def initialize_daily_cluster_prefetch(users):
    global DAILY_PREFETCH_INVENTORY, DAILY_PREFETCH_META, DAILY_BAD_INVENTORY_URLS, DAILY_BAD_INVENTORY_COMPANY_TITLES

    DAILY_BAD_INVENTORY_URLS = set()
    DAILY_BAD_INVENTORY_COMPANY_TITLES = set()

    if not ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH:
        print("Automation-title cluster prefetch disabled.")
        return []

    clusters = build_automation_title_clusters(users)
    DAILY_PREFETCH_META = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cluster_count": len(clusters),
        "clusters": clusters,
    }

    print("\n\n============================================================")
    print("AUTOMATION-TITLE CLUSTER PREFETCH")
    print("============================================================")
    print(f"Clusters built from active /automations job titles: {len(clusters)}")
    for key, data in clusters.items():
        print(f"• {key}: {data.get('user_count')} users; titles={data.get('titles')[:8]}")

    inventory = []
    seen = set()
    for key, data in clusters.items():
        jobs = prefetch_jobs_for_title_cluster(key, data)
        print(f"Cluster {key}: prefetched {len(jobs)} raw jobs")
        for job in jobs:
            norm = normalize_url(canonical_job_url(job))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            inventory.append(job)
            if len(inventory) >= MAX_PREFETCH_INVENTORY_JOBS:
                break
        if len(inventory) >= MAX_PREFETCH_INVENTORY_JOBS:
            break

    DAILY_PREFETCH_INVENTORY = inventory

    if ENABLE_SHARED_DAILY_INVENTORY:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = RUN_LOG_DIR / f"daily_prefetch_inventory_{timestamp}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"meta": DAILY_PREFETCH_META, "jobs": DAILY_PREFETCH_INVENTORY}, f, indent=2)
            print(f"Saved daily prefetch inventory: {path}")
        except Exception as e:
            print(f"Could not save daily prefetch inventory: {e}")

    print(f"Total shared inventory jobs prefetched: {len(DAILY_PREFETCH_INVENTORY)}")
    return DAILY_PREFETCH_INVENTORY


def candidate_title_signal_text(automation=None, persona=None, search_contract=None):
    titles = []
    for title in extract_automation_job_titles(automation or {}, limit=60):
        titles.append(title)
    if isinstance(persona, dict):
        titles.extend(persona.get("target_titles") or [])
        titles.extend(persona.get("best_fit_roles") or [])
    if isinstance(search_contract, dict):
        titles.extend(search_contract.get("allowed_titles") or [])
        titles.extend(search_contract.get("allowed_functions") or [])
    seen = set()
    output = []
    for item in titles:
        item = str(item or "").strip()
        norm = item.lower()
        if item and norm not in seen:
            seen.add(norm)
            output.append(item)
    return " ".join(output[:80])


def local_inventory_match_score(job, automation=None, persona=None, search_contract=None, user_profile=None):
    title = _norm_words(job.get("title", ""))
    desc = _norm_words(job.get("description", ""))
    company = _norm_words(job.get("company", ""))
    blob = f"{title} {desc} {company}"
    signal = _norm_words(candidate_title_signal_text(automation, persona, search_contract))

    score = 0
    title_words = {w for w in signal.split() if len(w) >= 4}
    job_words = {w for w in blob.split() if len(w) >= 4}
    score += min(45, len(title_words & job_words) * 5)

    if any(marker in title for marker in ["senior", "lead", "manager", "director", "engineer", "analyst", "specialist", "assistant", "coordinator"]):
        score += 8
    if any(marker in blob for marker in ["remote", "hybrid", "equity", "salary", "full-time", "contract"]):
        score += 5
    if job.get("cluster_key") and str(job.get("cluster_key")) in title_cluster_key(signal):
        score += 10

    forbidden_blob = _norm_words(" ".join((search_contract or {}).get("forbidden_titles", []) + (search_contract or {}).get("forbidden_functions", [])))
    forbidden_words = {w for w in forbidden_blob.split() if len(w) >= 5}
    score -= min(30, len(forbidden_words & job_words) * 6)

    if job_location_conflicts_candidate_policy(job, user_profile=user_profile, automation=automation, search_contract=search_contract):
        score -= 80

    return max(0, min(100, score))


def _important_role_words(text):
    """Tokenize role text for deterministic no-GPT persona-fit scoring."""
    stop_words = {
        "and", "or", "the", "for", "with", "from", "into", "role", "jobs",
        "job", "work", "remote", "hybrid", "onsite", "on", "site", "full",
        "time", "part", "contract", "senior", "junior", "lead", "manager",
        "director", "specialist", "associate", "assistant", "coordinator",
        "executive", "head", "principal", "staff", "global", "regional",
    }
    return {
        w for w in re.findall(r"[a-z0-9+#.-]+", _norm_words(text))
        if len(w) >= 3 and w not in stop_words
    }


def extract_candidate_role_signals(automation=None, persona=None, search_contract=None):
    """Return candidate-specific allowed role signals from automation + persona + contract.

    This keeps --nogpt candidate-agnostic in code while still candidate-aware in
    behavior: different users get different local filters because their
    automation/persona/search-contract values are different.
    """
    signals = []
    for title in extract_automation_job_titles(automation or {}, limit=80):
        signals.append(str(title).strip())
    if isinstance(persona, dict):
        signals.extend(str(x).strip() for x in (persona.get("target_titles") or []))
        signals.extend(str(x).strip() for x in (persona.get("best_fit_roles") or []))
        signals.extend(str(x).strip() for x in (persona.get("search_keywords") or []))
    if isinstance(search_contract, dict):
        signals.extend(str(x).strip() for x in (search_contract.get("allowed_titles") or []))
        signals.extend(str(x).strip() for x in (search_contract.get("allowed_functions") or []))

    seen = set()
    output = []
    for signal in signals:
        norm = re.sub(r"\s+", " ", signal.lower()).strip()
        if not norm or norm in seen or len(norm) < 3:
            continue
        seen.add(norm)
        output.append(signal)
    return output[:120]


def nogpt_persona_fit_score(job, automation=None, persona=None, search_contract=None, user_profile=None):
    """Score how closely a live job matches the user's persona without GPT.

    This is intentionally conservative. A job that merely has a valid URL is not
    enough; it needs title/function overlap with this user's target signals and
    must not conflict with forbidden functions or location rules.
    """
    title = _norm_words(job.get("title", ""))
    description = _norm_words(job.get("description", ""))
    company = _norm_words(job.get("company", ""))
    blob = f"{title} {description} {company}"
    title_words = _important_role_words(title)
    blob_words = _important_role_words(blob)
    signals = extract_candidate_role_signals(automation, persona, search_contract)

    if not signals:
        # No persona/search-contract to compare against. Keep this conservative
        # but not impossible, because some imported automations are sparse.
        return 45, ["no_candidate_role_signals"]

    score = 0
    reasons = []
    best_title_overlap = 0
    best_blob_overlap = 0
    exact_phrase_hit = False
    cluster_hit = False

    job_cluster = title_cluster_key(title)
    for signal in signals:
        s_norm = _norm_words(signal)
        if not s_norm:
            continue
        signal_words = _important_role_words(s_norm)
        if not signal_words:
            continue

        if s_norm and (s_norm in title or title in s_norm):
            exact_phrase_hit = True

        signal_cluster = title_cluster_key(s_norm)
        if signal_cluster and signal_cluster == job_cluster and signal_cluster != "uncategorized":
            cluster_hit = True

        title_overlap = len(title_words & signal_words) / max(1, len(signal_words))
        blob_overlap = len(blob_words & signal_words) / max(1, len(signal_words))
        best_title_overlap = max(best_title_overlap, title_overlap)
        best_blob_overlap = max(best_blob_overlap, blob_overlap)

    if exact_phrase_hit:
        score += 35
        reasons.append("exact_or_near_title_phrase_match")
    if cluster_hit:
        score += 25
        reasons.append("same_role_cluster")

    score += int(best_title_overlap * 35)
    score += int(best_blob_overlap * 15)

    local_score = local_inventory_match_score(
        job,
        automation=automation,
        persona=persona,
        search_contract=search_contract,
        user_profile=user_profile,
    )
    score += min(15, int(local_score * 0.15))

    if any(marker in title for marker in ["senior", "lead", "manager", "director", "engineer", "analyst", "specialist", "assistant", "coordinator", "executive"]):
        score += 5
        reasons.append("recognizable_seniority_title")

    if job.get("source"):
        source = str(job.get("source", "")).lower()
        if source.startswith(("jobo_ats", "hiring_cafe", "linkedin_apify")):
            score += 5
            reasons.append("structured_source")

    if job_location_conflicts_candidate_policy(job, user_profile=user_profile, automation=automation, search_contract=search_contract):
        score -= 80
        reasons.append("location_conflict")

    if candidate_forbids_title_or_function(job, search_contract=search_contract):
        score -= 70
        reasons.append("forbidden_title_or_function")

    role_conflict, role_reason = role_family_conflicts_candidate(
        job,
        search_contract=search_contract,
        automation=automation,
        user_profile=user_profile,
    )
    if role_conflict:
        score -= 55
        reasons.append(role_reason)

    forbidden_blob = _norm_words(" ".join((search_contract or {}).get("forbidden_titles", []) + (search_contract or {}).get("forbidden_functions", [])))
    forbidden_words = {w for w in forbidden_blob.split() if len(w) >= 5}
    forbidden_hits = forbidden_words & blob_words
    if forbidden_hits:
        score -= min(30, len(forbidden_hits) * 8)
        reasons.append("forbidden_keyword_overlap:" + ",".join(sorted(list(forbidden_hits))[:5]))

    if best_title_overlap < 0.20 and not exact_phrase_hit and not cluster_hit:
        reasons.append("weak_title_overlap")

    if best_blob_overlap < 0.15 and not exact_phrase_hit and not cluster_hit:
        reasons.append("weak_description_overlap")

    return max(0, min(100, score)), reasons


def nogpt_persona_fit_review(job, automation=None, persona=None, search_contract=None, user_profile=None, minimum_viable_mode=False):
    """Final no-GPT persona gate before posting.

    Returns (ok, score, reason, risk_flags). This is the deterministic equivalent
    of the AI reviewer for --nogpt runs. It is intentionally stricter than the
    inventory scorer and runs only after static/remote checks have already
    proven that the job link is a live direct job post.
    """
    min_score = NOGPT_PERSONA_FIT_MIN_SCORE_MINIMUM_VIABLE if minimum_viable_mode else NOGPT_PERSONA_FIT_MIN_SCORE
    score, reasons = nogpt_persona_fit_score(
        job,
        automation=automation,
        persona=persona,
        search_contract=search_contract,
        user_profile=user_profile,
    )
    risk_flags = []

    if "location_conflict" in reasons:
        risk_flags.append("location_conflict")
    if any(r.startswith("wrong_function_pre_review") for r in reasons):
        risk_flags.append("wrong_function")
    if "forbidden_title_or_function" in reasons:
        risk_flags.append("forbidden_title_or_function")
    if "weak_title_overlap" in reasons and "weak_description_overlap" in reasons:
        risk_flags.append("weak_persona_alignment")

    if score < min_score:
        return False, score, f"No-GPT persona-fit score {score} below floor {min_score}. Signals: {', '.join(reasons[:6])}", risk_flags or ["low_persona_fit_score"]

    if any(flag in risk_flags for flag in ["location_conflict", "wrong_function", "forbidden_title_or_function"]):
        return False, score, f"No-GPT persona-fit rejected due to hard risk flags: {', '.join(risk_flags)}. Signals: {', '.join(reasons[:6])}", risk_flags

    return True, score, f"No-GPT persona-fit approved with score {score}. Signals: {', '.join(reasons[:6])}", risk_flags


INVENTORY_GLOBAL_BAD_REASONS = set(LINK_FAILURE_REASONS) | BAD_SOURCE_REASONS | {
    "aggregator_page",
    "redirected_to_aggregator",
    "dead_job_url_pattern",
    "soft_404_or_not_found",
    "expired",
    "404_not_found",
    "wrong_or_generic_job_page",
    "career_homepage",
    "search_page",
    "generic_or_homepage_url",
}


def is_shared_inventory_job(job):
    return "shared_daily_inventory" in str((job or {}).get("source", "")).lower()


def mark_shared_inventory_job_bad(job, reason):
    """Suppress stale shared-inventory leads after they fail objective link/source checks.

    Do not globally suppress candidate-specific match rejects because a job that is
    wrong for one candidate may be right for another. Link/source failures are
    objective and should not be retried repeatedly during the same run.
    """
    if not is_shared_inventory_job(job):
        return

    reason_text = str(reason or "")
    base_reason = reason_text.split(":", 1)[0].replace("remote_resolution_", "").replace("direct_resolution_", "")

    if base_reason not in INVENTORY_GLOBAL_BAD_REASONS:
        return

    norm = normalize_url(canonical_job_url(job))
    if norm:
        DAILY_BAD_INVENTORY_URLS.add(norm)

    ct = company_title_key(job)
    if ct[0] and ct[1]:
        DAILY_BAD_INVENTORY_COMPANY_TITLES.add(ct)


# ============================================================
# HIRING.CAFE APIFY INTEGRATION
# ============================================================

def _strip_html(html):
    """Strip HTML tags and entities to plain text, capped for prompt size."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(html))
    text = re.sub(r"&[a-z]{2,6};", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:3000]


def _parse_hiring_cafe_job(raw_item):
    """Map one raw Hiring.cafe API item to the internal job dict format.
    Returns None for expired or malformed records."""
    if not isinstance(raw_item, dict):
        return None
    if raw_item.get("is_expired"):
        return None

    apply_url = raw_item.get("apply_url", "").strip()
    if not apply_url:
        return None
    # JazzHR and some other ATS providers return http:// URLs; upgrade to https://
    # since HC only returns direct ATS apply pages, which universally support HTTPS.
    if apply_url.startswith("http://"):
        apply_url = "https://" + apply_url[7:]

    v5 = raw_item.get("v5_processed_job_data") or {}
    job_info = raw_item.get("job_information") or {}
    enriched = raw_item.get("enriched_company_data") or {}

    title = (v5.get("core_job_title") or job_info.get("title") or "").strip()
    # enriched_company_data.name is more reliable than v5.company_name (v5 sometimes
    # misidentifies the company, e.g. "Overview" instead of "Central Health").
    company = (enriched.get("name") or v5.get("company_name") or "").strip()
    if not title or not company:
        return None

    # Build description from clean structured summary first, then raw HTML.
    # Many ATS pages (hrmdirect, icims) bury the job content after heavy UI
    # boilerplate — the raw stripped text would exhaust the 3000-char limit on
    # nav/share widgets before reaching the actual role description.
    requirements_summary = (v5.get("requirements_summary") or "").strip()
    description_html = job_info.get("description") or ""
    description_raw = _strip_html(description_html)
    if requirements_summary:
        description = (requirements_summary + "\n\n" + description_raw[:800]).strip()
    else:
        description = description_raw
    description = description[:3000]
    location = (v5.get("formatted_workplace_location") or "").strip()
    workplace_type = (v5.get("workplace_type") or "").strip()
    source_tag = raw_item.get("source", "")
    board = raw_item.get("board_token", "")

    # Salary range for the AI review prompt context.
    sal_min = v5.get("yearly_min_compensation")
    sal_max = v5.get("yearly_max_compensation")
    salary_note = ""
    if sal_min and sal_max:
        salary_note = f"${int(sal_min):,}–${int(sal_max):,}/yr"
    elif sal_min:
        salary_note = f"from ${int(sal_min):,}/yr"

    return {
        "job_url": apply_url,
        "title": title,
        "company": company,
        "description": description + (f"\n\nSalary: {salary_note}" if salary_note else ""),
        "location": location,
        "source": f"hiring_cafe/{source_tag}/{board}"[:120],
        "grade": 70,                         # placeholder; scored locally below
        "_hc_workplace_type": workplace_type,
        "_hc_object_id": raw_item.get("objectID", ""),
    }


def _build_hc_query(automation, search_contract, user_profile, persona=None):
    """Extract keyword(s), location, and workplace_type for the Apify HC actor.

    Returns (keywords_list, location, workplace_type). Uses the same three-tier
    priority as fetch_linkedin_for_user so HC gets the same quality signal.
    """
    # Priority 1: persona target_titles (AI-generated, specific)
    keywords = []
    if persona and isinstance(persona, dict):
        _target = persona.get("target_titles") or []
        keywords = [str(t).strip() for t in _target if str(t).strip()][:3]

    # Priority 2: extract from automation prefs
    if not keywords:
        keywords = extract_automation_job_titles(automation, limit=3)

    # Priority 3: raw jobTitles from prefs (e.g. "Retail")
    if not keywords:
        _prefs = extract_job_preferences(automation)
        _raw = _prefs.get("jobTitles") or []
        keywords = [str(t).strip() for t in _raw if str(t).strip()][:3]

    # Fallback: search contract queries
    if not keywords:
        _q = str((search_contract or {}).get("search_queries", [""])[0])[:80]
        if _q:
            keywords = [_q]

    # Derive workplace_type from the location/remote policy.
    policy = candidate_location_policy(user_profile, automation, search_contract)
    if policy.get("worldwide") or policy.get("remote_allowed"):
        workplace_type = "Remote"
    else:
        workplace_type = "Any"

    # Build a location string: if no strong region, leave blank so HC searches globally.
    allowed_regions = policy.get("allowed_regions", [])
    location = ""
    if "us" in allowed_regions:
        location = "United States"
    elif "uk" in allowed_regions:
        location = "United Kingdom"
    elif "eu" in allowed_regions:
        location = "Europe"
    # Otherwise blank = no geo filter on HC side (let our own policy filter do it).

    return keywords, location, workplace_type


def fetch_hiring_cafe_for_user(automation, search_contract, user_profile, avoid_urls, rejected_companies, persona=None, limit=None):
    """Fetch and locally-score Hiring.cafe candidates for one user via Apify.

    Normal mode: up to 2 keywords (persona titles first), splitting maxItems
    across keywords so total raw cost stays the same.
    No-GPT mode: more keywords with higher maxItems budget.
    """
    if not ENABLE_HIRING_CAFE_PREFETCH:
        return [], 0

    base_limit = limit or HIRING_CAFE_MAX_ITEMS
    keywords, location, workplace_type = _build_hc_query(automation, search_contract, user_profile, persona=persona)
    if NO_GPT_MODE:
        extra_keywords = extract_automation_job_titles(automation, limit=NOGPT_HIRING_CAFE_KEYWORDS)
        keywords = list(dict.fromkeys([k for k in extra_keywords + keywords if k]))[:NOGPT_HIRING_CAFE_KEYWORDS]
    else:
        # Normal mode: use up to 2 keywords so HC searches are more targeted
        keywords = keywords[:2]
    if not keywords:
        print("Hiring.cafe: no keyword extracted — skipping prefetch")
        return [], 0

    per_keyword_limit = max(8, base_limit // max(1, len(keywords))) if not NO_GPT_MODE else max(25, base_limit // max(1, len(keywords)))
    print(f"\nHiring.cafe prefetch  keywords={keywords!r}  location={location!r}  type={workplace_type}  maxItems/keyword={per_keyword_limit}")

    raw_items_all = []
    for kw in keywords:
        try:
            actor_input = {"keyword": kw, "workplaceType": workplace_type, "maxItems": per_keyword_limit}
            if location:
                actor_input["location"] = location

            resp = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_HIRING_CAFE_ACTOR_ID}/run-sync-get-dataset-items",
                params={"token": APIFY_API_TOKEN},
                json=actor_input,
                timeout=180,
            )
            resp.raise_for_status()
            raw_items = resp.json()
            if isinstance(raw_items, list):
                raw_items_all.extend(raw_items)
                print(f"Hiring.cafe: keyword={kw!r} → {len(raw_items)} raw results")
            else:
                print(f"Hiring.cafe: unexpected response type {type(raw_items).__name__} for keyword={kw!r}")
        except Exception as e:
            print(f"Hiring.cafe API error for keyword={kw!r}: {e}")

    raw_items = raw_items_all
    print(f"Hiring.cafe: {len(raw_items)} total raw results received")

    # Parse → dedup by (company, title) → filter seen/blocked → score → sort.
    avoid_norm = {normalize_url(u) for u in (avoid_urls or set())}
    blocked_cos = {str(c).strip().lower() for c in (rejected_companies or set())}

    seen_co_title = set()
    deduped = []
    for raw in raw_items:
        job = _parse_hiring_cafe_job(raw)
        if not job:
            continue
        co = re.sub(r"\s+", " ", job["company"].strip().lower())
        ti = re.sub(r"\s+", " ", job["title"].strip().lower())
        if (co, ti) in seen_co_title:
            continue
        seen_co_title.add((co, ti))
        deduped.append(job)

    parsed = []
    for job in deduped:
        norm = normalize_url(job["job_url"])
        if norm and norm in avoid_norm:
            continue
        co_key = re.sub(r"\s+", " ", job["company"].strip().lower())
        if co_key and co_key in blocked_cos:
            continue
        score = local_inventory_match_score(
            job,
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
        )
        if score < HIRING_CAFE_LOCAL_SCORE_MIN:
            continue
        job["grade"] = max(job["grade"], score)
        job["inventory_local_score"] = score
        parsed.append((score, job))

    parsed.sort(key=lambda x: x[0], reverse=True)
    results = [j for _, j in parsed]

    print(f"Hiring.cafe: {len(results)} jobs after local scoring (min score {HIRING_CAFE_LOCAL_SCORE_MIN})")
    return results, len(raw_items_all)

def _parse_jobo_ats_job(raw_item):
    """Map one raw jobo.world ATS Jobs API item to the internal job dict format."""
    if not isinstance(raw_item, dict):
        return None
    apply_url = (raw_item.get("apply_url") or "").strip()
    if not apply_url:
        return None
    title = (raw_item.get("title") or "").strip()
    company = ((raw_item.get("company") or {}).get("name") or "").strip()
    if not title or not company:
        return None
    summary = (raw_item.get("summary") or "").strip()
    description_raw = (raw_item.get("description") or "").strip()
    if summary:
        description = (summary + "\n\n" + description_raw[:800]).strip()
    else:
        description = description_raw
    description = description[:3000]
    locations = raw_item.get("locations") or []
    location = ""
    if locations:
        loc = locations[0]
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        location = ", ".join(p for p in parts if p)
    workplace_type = (raw_item.get("workplace_type") or "").strip()
    comp = raw_item.get("compensation") or {}
    sal_min = comp.get("min")
    sal_max = comp.get("max")
    period = (comp.get("period") or "").lower()
    multiplier = {"yearly": 1, "monthly": 12, "weekly": 52, "hourly": 2080}.get(period, 1)
    if sal_min:
        sal_min = int(sal_min * multiplier)
    if sal_max:
        sal_max = int(sal_max * multiplier)
    salary_note = ""
    if sal_min and sal_max:
        salary_note = f"${sal_min:,}–${sal_max:,}/yr"
    elif sal_min:
        salary_note = f"from ${sal_min:,}/yr"
    elif sal_max:
        salary_note = f"up to ${sal_max:,}/yr"
    source_ats = raw_item.get("source", "")
    return {
        "job_url": apply_url,
        "title": title,
        "company": company,
        "description": description + (f"\n\nSalary: {salary_note}" if salary_note else ""),
        "location": location,
        "source": f"jobo_ats/{source_ats}"[:120],
        "grade": 70,
        "_hc_workplace_type": workplace_type,
        "_hc_object_id": raw_item.get("id", ""),
    }


def _extract_salary_floor_for_jobo(search_contract):
    """Parse the user's annual USD salary minimum from their search contract text.

    Returns an int (e.g. 100000) or None if nothing reliable was found.
    Conservative: only returns a value when we're clearly confident, so we
    never accidentally filter out valid jobs at the API level.
    """
    sc = search_contract or {}
    text = " ".join([
        str(sc.get("salary_hard_rule") or ""),
        str(sc.get("salary_rules") or ""),
    ]).replace(",", "")
    amounts = []
    for m in re.finditer(r'(?:[$€£]|usd|eur|gbp)?\s*(\d{2,3})k\b|\b(\d{5,6})\b', text, re.IGNORECASE):
        raw = m.group(1) or m.group(2)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if m.group(1):
            val *= 1000
        if 40000 <= val <= 500000:
            amounts.append(val)
    if not amounts:
        return None
    floor = min(amounts)
    return floor if floor >= 40000 else None


def is_us_allowed_candidate(user_profile=None, automation=None, search_contract=None):
    policy = candidate_location_policy(user_profile, automation, search_contract)
    if "us" in set(policy.get("allowed_regions") or []):
        return True
    text = _norm_words(json.dumps({
        "automation": extract_job_preferences(automation or {}),
        "profile": user_profile or {},
        "contract": search_contract or {},
    }, default=str))
    return any(marker in text for marker in ["united states", "usa", "u s", " us "])


def build_jobo_search_bodies(keywords, location, workplace_type, salary_floor, limit, user_profile, automation, search_contract):
    posted_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work_models = ["remote"] if workplace_type == "Remote" else ["remote", "hybrid", "onsite"]

    if not NO_GPT_MODE:
        body = {
            "queries": keywords,
            "work_models": work_models,
            "page_size": limit,
            "posted_after": posted_after,
            "include_fields": ["summary", "description"],
        }
        if location:
            body["locations"] = [location]
        if salary_floor:
            body["salary_usd"] = {"min": salary_floor}
        return [body]

    # No-GPT: spend more Jobo calls. One query per title gives better recall and
    # avoids a single broad mixed query crowding out useful roles. For US-allowed
    # users, add a remote/no-location pass because the US inventory is large.
    bodies = []
    max_calls = NOGPT_JOBO_MAX_CALLS_PER_USER + (NOGPT_JOBO_US_EXTRA_CALLS if is_us_allowed_candidate(user_profile, automation, search_contract) else 0)
    for kw in keywords:
        if len(bodies) >= max_calls:
            break
        base = {
            "queries": [kw],
            "work_models": work_models,
            "page_size": limit,
            "posted_after": posted_after,
            "include_fields": ["summary", "description"],
        }
        if location:
            body = dict(base)
            body["locations"] = [location]
            bodies.append(body)
        else:
            bodies.append(base)

        if len(bodies) >= max_calls:
            break

        if is_us_allowed_candidate(user_profile, automation, search_contract):
            # US users usually benefit from both location-filtered and remote/open
            # passes. This uses the extra quota the user mentioned.
            remote_body = dict(base)
            remote_body["work_models"] = ["remote", "hybrid", "onsite"]
            remote_body["locations"] = ["United States"]
            bodies.append(remote_body)

    if salary_floor:
        for body in bodies:
            body["salary_usd"] = {"min": salary_floor}
    return bodies[:max_calls]


def fetch_jobo_ats_for_user(automation, search_contract, user_profile, avoid_urls, rejected_companies, limit=None):
    """Fetch and locally-score jobo.world ATS Jobs API candidates via direct API.

    In --nogpt mode this intentionally makes more Jobo calls across more target
    titles, because Jobo becomes the primary source replacing GPT search.
    """
    if not ENABLE_JOBO_ATS_PREFETCH:
        return [], 0
    limit = limit or (NOGPT_JOBO_PAGE_SIZE if NO_GPT_MODE else JOBO_ATS_MAX_ITEMS)
    keyword_limit = NOGPT_JOBO_KEYWORDS if NO_GPT_MODE else 3
    keywords = extract_automation_job_titles(automation, limit=keyword_limit)
    _, location, workplace_type = _build_hc_query(automation, search_contract, user_profile)
    if not keywords:
        print("Jobo ATS: no keywords extracted — skipping prefetch")
        return [], 0
    salary_floor = _extract_salary_floor_for_jobo(search_contract)
    posted_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bodies = build_jobo_search_bodies(
        keywords=keywords,
        location=location,
        workplace_type=workplace_type,
        salary_floor=salary_floor,
        limit=limit,
        user_profile=user_profile,
        automation=automation,
        search_contract=search_contract,
    )
    print(
        f"\nJobo ATS prefetch  queries={keywords}  location={location!r}  type={workplace_type}"
        f"  salary_min={salary_floor}  posted_after={posted_after[:10]}  calls={len(bodies)}  page_size={limit}"
    )

    raw_items_all = []
    for idx, body in enumerate(bodies, start=1):
        try:
            resp = requests.post(
                f"{JOBO_API_BASE}/api/jobs/search",
                headers={"X-Api-Key": JOBO_API_KEY, "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            resp.raise_for_status()
            raw_items = resp.json().get("jobs", [])
            raw_items_all.extend(raw_items)
            print(f"Jobo ATS call {idx}/{len(bodies)}: query={body.get('queries')} locations={body.get('locations')} → {len(raw_items)} raw results")
        except Exception as e:
            print(f"Jobo ATS API error on call {idx}/{len(bodies)}: {e}")
            continue

    raw_items = raw_items_all
    print(f"Jobo ATS: {len(raw_items)} total raw results received")
    avoid_norm = {normalize_url(u) for u in (avoid_urls or set())}
    blocked_cos = {str(c).strip().lower() for c in (rejected_companies or set())}
    seen_co_title = set()
    parsed = []
    for raw in raw_items:
        job = _parse_jobo_ats_job(raw)
        if not job:
            continue
        co = re.sub(r"\s+", " ", job["company"].strip().lower())
        ti = re.sub(r"\s+", " ", job["title"].strip().lower())
        if (co, ti) in seen_co_title:
            continue
        seen_co_title.add((co, ti))
        norm = normalize_url(job["job_url"])
        if norm and norm in avoid_norm:
            continue
        if co and co in blocked_cos:
            continue
        score = local_inventory_match_score(
            job,
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
        )
        if score < JOBO_LOCAL_SCORE_MIN:
            continue
        job["grade"] = max(job["grade"], score)
        job["inventory_local_score"] = score
        parsed.append((score, job))
    parsed.sort(key=lambda x: x[0], reverse=True)
    results = [j for _, j in parsed]
    print(f"Jobo ATS: {len(results)} jobs after local scoring (min score {JOBO_LOCAL_SCORE_MIN})")
    return results, len(raw_items_all)

def _parse_linkedin_apify_job(raw_item):
    """Map one raw LinkedIn Apify actor item to the internal job dict format."""
    if not isinstance(raw_item, dict):
        return None

    # Prefer the direct ATS apply URL. Easy Apply jobs have no applyUrl (or a
    # linkedin.com URL) — skip them: they can't be applied to outside LinkedIn.
    apply_url = str(raw_item.get("applyUrl") or "").strip()
    job_url_fallback = str(raw_item.get("jobUrl") or "").strip()
    if apply_url and not urllib.parse.urlparse(apply_url).netloc.endswith("linkedin.com"):
        job_url = apply_url
    else:
        return None  # Easy Apply — no external ATS URL, skip

    if not job_url.startswith("http"):
        return None

    title = str(raw_item.get("jobTitle") or "").strip()
    company = str(raw_item.get("companyName") or "").strip()
    if not title or not company:
        return None

    location = str(raw_item.get("location") or "").strip()
    description = str(raw_item.get("jobDescription") or "")[:3000]

    # Prepend salary info if available
    salary_info = raw_item.get("salaryInfo") or []
    if salary_info and isinstance(salary_info, list):
        salary_str = " – ".join(str(s) for s in salary_info if s)
        if salary_str:
            description = f"Salary: {salary_str}\n\n" + description

    job_id = str(raw_item.get("jobId") or "")
    source = f"linkedin_apify/{job_id}"[:120]

    return {
        "job_url": job_url,
        "title": title,
        "company": company,
        "description": description,
        "location": location,
        "source": source,
        "grade": 70,
        "_hc_workplace_type": "on-site",   # conservative default; not exposed by this actor
        "_hc_object_id": job_id,
    }



def fetch_linkedin_for_user(automation, search_contract, user_profile, avoid_urls, rejected_companies, persona=None, limit=None):
    """Fetch and locally-score LinkedIn jobs via Apify actor (2rJKkhh7vjpX7pvjg).

    This is the highest-priority source — LinkedIn has direct ATS apply URLs and
    rich structured metadata. Results are merged into the pre-fetch pool before
    Hiring Cafe and Jobo, so they are consumed first in the batch loop.
    """
    if not ENABLE_LINKEDIN_PREFETCH:
        return [], 0

    limit = limit or LINKEDIN_MAX_ITEMS

    # Priority 1: persona target_titles — AI-generated, specific, searchable titles
    keywords = []
    if persona and isinstance(persona, dict):
        _target = persona.get("target_titles") or []
        keywords = [str(t).strip() for t in _target if str(t).strip()][:3]

    # Priority 2: extract from automation (works for most users with standard titles)
    if not keywords:
        keywords = extract_automation_job_titles(automation, limit=3)

    # Priority 3: raw jobTitles from preferences (catches simple values like "Retail")
    if not keywords:
        _prefs = extract_job_preferences(automation)
        _raw_titles = _prefs.get("jobTitles") or []
        keywords = [str(t).strip() for t in _raw_titles if str(t).strip()][:3]

    _, location, _ = _build_hc_query(automation, search_contract, user_profile)
    if not keywords:
        print("LinkedIn: no keywords extracted — skipping prefetch")
        return [], 0

    actor_input = {
        "keyword": keywords,
        "publishedAt": "r604800",   # last 7 days
        "maxItems": limit,
        "enrichCompanyData": False,
        "excludeRecruitingAgencies": False,
        "filterEasyApply": False,
        "saveOnlyUniqueItems": False,
    }
    if location:
        actor_input["locations"] = [location]

    print(f"\nLinkedIn prefetch  keywords={keywords!r}  location={location!r}  maxResults={limit}")

    try:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{APIFY_LINKEDIN_ACTOR_ID}/run-sync-get-dataset-items",
            params={"token": APIFY_API_TOKEN},
            json=actor_input,
            timeout=180,
        )
        resp.raise_for_status()
        raw_items_all = resp.json()
        if not isinstance(raw_items_all, list):
            print(f"LinkedIn: unexpected response type {type(raw_items_all).__name__}")
            raw_items_all = []
    except Exception as e:
        print(f"LinkedIn Apify error: {e}")
        return [], 0

    # Client-side limit guard in case the actor ignores maxResults
    raw_items_all = raw_items_all[:limit]
    print(f"LinkedIn: {len(raw_items_all)} raw results received")

    avoid_norm = {normalize_url(u) for u in (avoid_urls or set())}
    blocked_cos = {str(c).strip().lower() for c in (rejected_companies or set())}
    seen_co_title = set()
    parsed = []
    for raw in raw_items_all:
        job = _parse_linkedin_apify_job(raw)
        if not job:
            continue
        co = re.sub(r"\s+", " ", job["company"].strip().lower())
        ti = re.sub(r"\s+", " ", job["title"].strip().lower())
        if (co, ti) in seen_co_title:
            continue
        seen_co_title.add((co, ti))
        norm = normalize_url(job["job_url"])
        if norm and norm in avoid_norm:
            continue
        if co and co in blocked_cos:
            continue
        score = local_inventory_match_score(
            job,
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
        )
        if score < LINKEDIN_LOCAL_SCORE_MIN:
            continue
        job["grade"] = max(job["grade"], score)
        job["inventory_local_score"] = score
        parsed.append((score, job))

    parsed.sort(key=lambda x: x[0], reverse=True)
    results = [j for _, j in parsed]
    print(f"LinkedIn: {len(results)} jobs after local scoring (min score {LINKEDIN_LOCAL_SCORE_MIN})")
    return results, len(raw_items_all)


def get_inventory_candidates_for_user(user_profile, automation, persona, search_contract, avoid_urls, limit=INVENTORY_CANDIDATES_PER_USER):
    if not DAILY_PREFETCH_INVENTORY:
        return []

    scored = []
    for job in DAILY_PREFETCH_INVENTORY:
        norm = normalize_url(canonical_job_url(job))
        ct = company_title_key(job)
        if not norm or norm in avoid_urls or norm in DAILY_BAD_INVENTORY_URLS:
            continue
        if ct[0] and ct[1] and ct in DAILY_BAD_INVENTORY_COMPANY_TITLES:
            continue
        score = local_inventory_match_score(
            job,
            automation=automation,
            persona=persona,
            search_contract=search_contract,
            user_profile=user_profile,
        )
        if score < INVENTORY_LOCAL_SCORE_MIN:
            continue
        enriched = dict(job)
        enriched["grade"] = max(normalize_grade(enriched.get("grade", 70)), score)
        enriched["source"] = f"shared_daily_inventory:{enriched.get('source', '')}"[:180]
        enriched["inventory_local_score"] = score
        scored.append((score, enriched))

    scored.sort(key=lambda item: item[0], reverse=True)
    out = []
    seen_companies = set()
    for score, job in scored:
        company_key = company_title_key(job)[0]
        if company_key and company_key in seen_companies:
            continue
        seen_companies.add(company_key)
        out.append(job)
        if len(out) >= limit:
            break
    return out


def merge_jobs_dedup(primary_jobs, secondary_jobs):
    output = []
    seen_urls = set()
    seen_ct = set()
    for job in list(primary_jobs or []) + list(secondary_jobs or []):
        job = normalize_job_record(job)
        norm = normalize_url(canonical_job_url(job))
        ct = company_title_key(job)
        if norm and norm in seen_urls:
            continue
        if ct[0] and ct[1] and ct in seen_ct:
            continue
        if norm:
            seen_urls.add(norm)
        if ct[0] and ct[1]:
            seen_ct.add(ct)
        output.append(job)
    return output

# ============================================================
# COST + SOURCE HELPERS
# ============================================================

def classify_job_source(source_str):
    s = str(source_str or "").lower()
    if s.startswith("linkedin_apify"):  return "linkedin"
    if s.startswith("hiring_cafe"):     return "hc"
    if s.startswith("jobo_ats"):        return "jobo"
    if "direct_url_resolver" in s:      return "resolver"
    return "openai"


def compute_source_success_rates(round_results):
    totals = {src: {"sourced": 0, "added": 0} for src in ("linkedin", "hc", "jobo", "openai", "resolver")}
    for r in (round_results or []):
        funnel = (r.get("round_metrics") or {}).get("source_funnel", {})
        for src, counts in funnel.items():
            if src not in totals:
                continue
            totals[src]["sourced"] += counts.get("sourced", 0)
            totals[src]["added"] += counts.get("added", 0)
    return {src: d["added"] / max(1, d["sourced"]) for src, d in totals.items()}


# ============================================================
# PROCESS ONE USER
# ============================================================

def find_jobs_for_user(
    user,
    round_number,
    max_batches_for_round=FIRST_ROUND_BATCHES,
    round_mode="first_round",
    previous_result=None,
    minimum_viable_mode=False,
    source_limit_overrides=None,
):
    uid = user.get("uid")
    email = user.get("email")
    previous_result = previous_result or {}

    empty_result = {
        "user": user,
        "user_profile": None,
        "automation": None,
        "persona": None,
        "search_contract": None,
        "cv_text": "",
        "jobs_added": [],
        "jobs_rejected_by_review": [],
        "all_rejected_jobs": [],
        "rejected_domains": [],
        "rejected_companies": [],
        "seen_companies_round": [],
        "pending_today_before": 0,
        "pending_today_after_estimate": 0,
        "needs_more": False,
        "round_number": round_number,
        "round_mode": round_mode,
        "user_plan": None,
        "job_status": None,
    }

    if not uid or not email:
        print("SKIP: missing uid or email")
        return empty_result

    if not is_paid_user(user):
        print(f"SKIP: not active paid user — {email}")
        print("  paid debug:", json.dumps(user_paid_debug_summary(user), indent=2, default=str))
        return empty_result

    automation = user.get("_automation_cache") or get_user_automation(uid)
    if automation is not None:
        user["_automation_cache"] = automation

    if not should_bypass_automation_check(user, email) and not has_active_automation(automation):
        print(f"SKIP: no active automation — {email}")
        print("  automation debug:", json.dumps(automation_debug_summary(automation), indent=2, default=str))
        empty_result["automation"] = automation
        return empty_result

    user_profile = get_user_profile(uid)

    if not user_profile:
        print(f"SKIP: could not load user profile — {email}")
        empty_result["automation"] = automation
        return empty_result

    print_user_header(user_profile)

    prefs = extract_job_preferences(automation)
    user_plan = get_user_plan(user=user, user_profile=user_profile, automation=automation)
    job_status = status_for_user_plan(user_plan)
    existing_jobs = extract_existing_jobs(automation)
    rejected_jobs_from_automation = extract_rejected_jobs_from_automation(automation)
    plan_rejected_learning = (
        compact_rejected_job_learning(rejected_jobs_from_automation, limit=40)
        if user_plan in {"starter", "max"}
        else []
    )
    pending_today_before = count_jobs_today(existing_jobs, job_status)

    jobs_needed = max(0, TARGET_JOBS_PER_USER - pending_today_before)

    print(f"Round: {round_number}")
    print(f"Round mode: {round_mode}")
    print(f"Max batches this round: {max_batches_for_round}")
    print("Automation active: yes")
    print(f"Detected plan: {user_plan}")
    print(f"Post status for new jobs: {job_status}")
    print(f"Existing selected jobs: {len(existing_jobs)}")
    print(f"Rejected jobs available for compact learning: {len(rejected_jobs_from_automation)}")
    print(f"Counted {job_status}/legacy jobs today: {pending_today_before}")
    print(f"Jobs needed to reach {TARGET_JOBS_PER_USER}: {jobs_needed}")
    print("Job preferences:")
    print(json.dumps(prefs, indent=2))

    if jobs_needed <= 0:
        print(f"SKIP: already has {TARGET_JOBS_PER_USER}+ {job_status}/legacy jobs today.")
        return {
            **empty_result,
            "user_profile": user_profile,
            "automation": automation,
            "pending_today_before": pending_today_before,
            "pending_today_after_estimate": pending_today_before,
            "needs_more": False,
            "user_plan": user_plan,
            "job_status": job_status,
        }

    cv_text = cv_to_text(user_profile)
    _persona_existed = persona_path(uid).exists()
    persona = get_or_create_persona(user_profile, automation, cv_text)
    _persona_new = not _persona_existed

    _contract_existed = search_contract_path(uid).exists()
    search_contract = get_or_create_search_contract(user_profile, automation, cv_text, persona)
    _contract_new = not _contract_existed

    existing_urls, existing_company_titles = existing_duplicate_sets(existing_jobs)

    print(f"Existing URL duplicate set: {len(existing_urls)}")
    print(f"Existing company/title duplicate set: {len(existing_company_titles)}")

    posted_jobs_this_round = []
    review_rejected_jobs_this_round = []
    rejected_jobs = list(previous_result.get("all_rejected_jobs", [])[-250:])
    if rejected_jobs_from_automation:
        rejected_jobs.extend(
            {
                **job,
                "reason": job.get("reason") or "historical_user_rejection",
                "feedback": job.get("feedback") or "",
                "status": job.get("status") or REJECTED_STATUS,
            }
            for job in rejected_jobs_from_automation[-80:]
        )
    rejected_domains = set(previous_result.get("rejected_domains", []))
    failed_batch_feedback = list(previous_result.get("failed_batch_feedback", [])[-60:])
    relaxation_strategy_report = None
    strategy_pivots = list(previous_result.get("strategy_pivots", []))
    strategy_prompt_patch = previous_result.get("strategy_prompt_patch", "") or ""
    strategy_pivot_count = len(strategy_pivots)
    consecutive_zero_approval_batches = 0
    round_metrics = {
        "openai_jobs_returned": 0,
        "static_candidates": 0,
        "static_pass_count": 0,
        "live_jobs_count": 0,
        "ai_reviewed_count": 0,
        "review_rejected_count": 0,
        "approved_count": 0,
        "direct_resolution_attempts": 0,
        "direct_resolution_successes": 0,
        "cheap_pre_review_rejections": 0,
        "openai_search_calls": 0,
        "openai_review_calls": 0,
        "openai_pivot_calls": 0,
        "linkedin_raw_results": 0,
        "hc_raw_results": 0,
        "jobo_raw_results": 0,
        "persona_created_this_run": False,
        "contract_created_this_run": False,
        "source_funnel": {
            "linkedin": {"sourced": 0, "static_pass": 0, "remote_pass": 0, "added": 0},
            "hc":       {"sourced": 0, "static_pass": 0, "remote_pass": 0, "added": 0},
            "jobo":     {"sourced": 0, "static_pass": 0, "remote_pass": 0, "added": 0},
            "openai":   {"sourced": 0, "static_pass": 0, "remote_pass": 0, "added": 0},
            "resolver": {"sourced": 0, "static_pass": 0, "remote_pass": 0, "added": 0},
        },
        "estimated_cost_usd": 0.0,
        "cost_breakdown": {},
    }
    round_metrics["persona_created_this_run"] = _persona_new
    round_metrics["contract_created_this_run"] = _contract_new

    # Company names whose every URL attempt 404'd/expired/hallucinated this run.
    # Carried forward from Round 1 so Round 2 doesn't retry the same dead companies.
    rejected_companies = set(previous_result.get("rejected_companies", []))
    rejected_companies.update(PERMANENTLY_BLOCKED_COMPANIES)
    # All company names seen across all batches this round (for prompt diversity hint).
    seen_companies_round = set(previous_result.get("seen_companies_round", []))

    seen_urls = set()
    seen_company_titles = set()
    avoid_urls = set(existing_urls)

    def apply_pivot(pivot):
        nonlocal search_contract
        nonlocal strategy_prompt_patch
        nonlocal strategy_pivot_count
        nonlocal relaxation_strategy_report

        if not pivot:
            return False

        relaxation_strategy_report = pivot
        strategy_pivots.append(pivot)
        search_contract = pivot.get("search_contract") or search_contract

        if pivot.get("prompt_patch"):
            strategy_prompt_patch = pivot["prompt_patch"]

        strategy_pivot_count += 1
        round_metrics["openai_pivot_calls"] += 1
        print("\nContinuing automatically with adapted search contract/prompt patch.")
        return True

    if round_mode == "second_round":
        second_pivot = maybe_create_strategy_pivot(
            user_profile=user_profile,
            automation=automation,
            cv_text=cv_text,
            persona=persona,
            search_contract=search_contract,
            batch_number=0,
            posted_jobs_this_round=posted_jobs_this_round,
            failed_batch_feedback=failed_batch_feedback,
            rejected_jobs=rejected_jobs,
            rejected_domains=rejected_domains,
            strategy_pivot_count=strategy_pivot_count,
            pivot_stage="second_round_start",
            pivot_guidance=SECOND_STRATEGY_PIVOT_GUIDANCE,
            force=True,
        )
        apply_pivot(second_pivot)

    # Pre-fetch candidates once per user before the batch loop.
    # Pool order: HC → Jobo → LinkedIn (last resort).  OpenAI fires only when pool empties.
    hc_inventory = []   # variable name kept so the batch loop needs no changes

    # 1. Hiring Cafe
    _hc_limit = (source_limit_overrides or {}).get("hc")
    if ENABLE_HIRING_CAFE_PREFETCH and round_mode in ("first_round", "second_round"):
        _hc_jobs, _hc_raw = fetch_hiring_cafe_for_user(
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
            avoid_urls=avoid_urls,
            rejected_companies=rejected_companies,
            persona=persona,
            limit=_hc_limit,
        )
        round_metrics["hc_raw_results"] += _hc_raw
        hc_inventory.extend(_hc_jobs)

    # 2. Jobo ATS
    _jobo_limit = (source_limit_overrides or {}).get("jobo")
    if ENABLE_JOBO_ATS_PREFETCH and round_mode in ("first_round", "second_round", "minimum_viable"):
        _jobo_jobs, _jobo_raw = fetch_jobo_ats_for_user(
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
            avoid_urls=avoid_urls,
            rejected_companies=rejected_companies,
            limit=_jobo_limit,
        )
        round_metrics["jobo_raw_results"] += _jobo_raw
        hc_inventory.extend(_jobo_jobs)

    # 3. LinkedIn — only when HC+Jobo pool is too thin to fill the user's quota.
    # Skipping saves ~$0.09/user when cheaper sources already have enough candidates.
    _li_limit = (source_limit_overrides or {}).get("linkedin")
    _hc_jobo_pool_size = len(hc_inventory)
    _linkedin_needed = ENABLE_LINKEDIN_PREFETCH and round_mode in ("first_round", "second_round") and _hc_jobo_pool_size < TARGET_JOBS_PER_USER
    if _linkedin_needed:
        print(f"HC+Jobo pool only {_hc_jobo_pool_size} jobs — running LinkedIn to supplement")
        _li_jobs, _li_raw = fetch_linkedin_for_user(
            automation=automation,
            search_contract=search_contract,
            user_profile=user_profile,
            avoid_urls=avoid_urls,
            rejected_companies=rejected_companies,
            persona=persona,
            limit=_li_limit,
        )
        round_metrics["linkedin_raw_results"] += _li_raw
        hc_inventory.extend(_li_jobs)
    elif ENABLE_LINKEDIN_PREFETCH and not _linkedin_needed:
        print(f"HC+Jobo pool has {_hc_jobo_pool_size} jobs — skipping LinkedIn to save cost")

    if hc_inventory:
        _pool_sources = "HC + Jobo ATS + Jobicy" + (" + LinkedIn" if _linkedin_needed else "")
        print(f"Pre-fetch pool: {len(hc_inventory)} total jobs ({_pool_sources})")

    for batch_number in range(1, max_batches_for_round + 1):
        current_total_estimate = pending_today_before + len(posted_jobs_this_round)

        if current_total_estimate >= TARGET_JOBS_PER_USER:
            break

        still_needed = TARGET_JOBS_PER_USER - current_total_estimate
        ats_only_mode = should_use_ats_only_mode(round_mode, batch_number)
        batch_phase_text = get_batch_phase_text(round_mode, batch_number)

        # Per-batch funnel counters — reset each iteration.
        batch_returned = 0
        batch_static_pass = 0
        batch_remote_pass = 0
        batch_approved = 0
        batch_remote_fail_types = []

        filled = min(current_total_estimate, TARGET_JOBS_PER_USER)
        bar = "█" * filled + "░" * (TARGET_JOBS_PER_USER - filled)
        last_fb = failed_batch_feedback[-1] if failed_batch_feedback else None

        print("\n============================================================")
        print(f"BATCH {batch_number}/{max_batches_for_round}  ·  {round_mode}  ·  {email}")
        print(f"Progress: [{bar}] {current_total_estimate}/{TARGET_JOBS_PER_USER}  (need {still_needed} more)")
        if last_fb:
            fb_stats = last_fb.get("stats", {})
            if fb_stats:
                print(
                    f"Prev:  {fb_stats.get('returned','?')} returned"
                    f" → {fb_stats.get('static_pass','?')} static"
                    f" → {fb_stats.get('remote_pass','?')} remote"
                    f" → {fb_stats.get('approved','?')} approved"
                    f"  [{last_fb.get('reason','')}]"
                )
            else:
                print(f"Prev:  [{last_fb.get('reason','')}]  {(last_fb.get('message') or '')[:90]}")
        if ats_only_mode:
            print("Mode:  ATS-ONLY EMERGENCY")
        if minimum_viable_mode:
            print("Mode:  MINIMUM VIABLE FALLBACK (grade≥58 conf≥55)")
        print("============================================================")

        rejected_notes = [
            {
                "url": canonical_job_url(item),
                "domain": url_domain(canonical_job_url(item)),
                "reason": item.get("reason")
                    or item.get("review_decision")
                    or item.get("review_reason"),
                "status": item.get("status"),
                "feedback": str(item.get("feedback") or "")[:160],
                "title": item.get("title"),
                "company": item.get("company"),
            }
            for item in rejected_jobs[-200:]
        ]

        # --- SOURCE JOBS: structured inventory first, OpenAI only when allowed ---
        if hc_inventory:
            # Consume next slice of pre-fetched structured candidates.
            jobs = hc_inventory[:HIRING_CAFE_BATCH_SIZE]
            hc_inventory = hc_inventory[HIRING_CAFE_BATCH_SIZE:]
            batch_source = "structured_inventory"
            print(f"Structured inventory: {len(jobs)} jobs  ({len(hc_inventory)} remaining in pool)")
        elif _hc_jobo_pool_size >= TARGET_JOBS_PER_USER and round_mode == "first_round" and current_total_estimate >= TARGET_JOBS_PER_USER:
            # Pool was sufficient AND the user actually hit their quota — stop.
            print("Pre-fetch pool exhausted (was sufficient, quota met) — skipping OpenAI search for this batch.")
            break
        elif NO_GPT_MODE:
            # In no-GPT mode, never fall back to OpenAI. We simply mark the
            # structured pool as exhausted and continue to the next batch/round.
            batch_source = "nogpt_inventory_exhausted"
            jobs = []
        else:
            # HC/Jobo pool exhausted (or disabled) — fall back to OpenAI web search.
            batch_source = "openai_search"
            try:
                result = ask_openai_for_jobs(
                    user_profile=user_profile,
                    automation=automation,
                    cv_text=cv_text,
                    persona=persona,
                    search_contract=search_contract,
                    batch_number=batch_number,
                    avoid_urls=avoid_urls,
                    rejected_notes=rejected_notes,
                    rejected_domains=rejected_domains,
                    failed_batch_feedback=failed_batch_feedback,
                    jobs_needed=still_needed,
                    ats_only_mode=ats_only_mode,
                    strategy_prompt_patch=strategy_prompt_patch,
                    max_batches_for_round=max_batches_for_round,
                    round_mode=round_mode,
                    batch_phase_text=batch_phase_text,
                    user_plan=user_plan,
                    job_status=job_status,
                    plan_rejected_learning=plan_rejected_learning,
                    link_failure_notes=build_link_failure_notes(rejected_jobs, limit=80),
                    rejected_companies=rejected_companies,
                    seen_companies_round=seen_companies_round,
                )
            except Exception as e:
                print(f"OpenAI search error for {email}: {e}")
                failed_batch_feedback.append({
                    "batch": batch_number,
                    "reason": "openai_search_error",
                    "message": str(e)[:300],
                    "round_mode": round_mode,
                    "stats": {"returned": 0, "static_pass": 0, "remote_pass": 0, "approved": 0},
                })
                consecutive_zero_approval_batches += 1
                continue

            jobs = result.get("jobs", [])

            if ENABLE_QUOTA_MODE and ENABLE_SHARED_DAILY_INVENTORY:
                inventory_candidates = get_inventory_candidates_for_user(
                    user_profile=user_profile,
                    automation=automation,
                    persona=persona,
                    search_contract=search_contract,
                    avoid_urls=avoid_urls,
                    limit=INVENTORY_CANDIDATES_PER_USER if batch_number == 1 else max(8, INVENTORY_CANDIDATES_PER_USER // 3),
                )
                if inventory_candidates:
                    print(f"Shared daily inventory candidates injected: {len(inventory_candidates)}")
                    jobs = merge_jobs_dedup(inventory_candidates, jobs)

        batch_returned = len(jobs)
        print(f"Source [{batch_source}]  jobs in batch: {batch_returned}")
        round_metrics["openai_jobs_returned"] += batch_returned
        round_metrics["static_candidates"] += batch_returned

        if batch_source == "openai_search":
            round_metrics["openai_search_calls"] += 1

        # Track sourced count per source
        for _job in jobs:
            _src = classify_job_source(_job.get("source", ""))
            round_metrics["source_funnel"][_src]["sourced"] += 1

        if not jobs:
            zero_reason = "nogpt_structured_inventory_exhausted" if batch_source == "nogpt_inventory_exhausted" else ("structured_inventory_returned_zero_candidates" if batch_source == "structured_inventory" else "openai_returned_zero_jobs")
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": zero_reason,
                "message": "No candidates from this source for this batch.",
                "round_mode": round_mode,
                "source": batch_source,
                "stats": {"returned": 0, "static_pass": 0, "remote_pass": 0, "approved": 0},
            })
            consecutive_zero_approval_batches += 1
            continue

        static_pass_jobs = []
        batch_companies_seen = set()
        direct_resolution_attempts_this_batch = 0

        for job in jobs:
            company_key = str(job.get("company", "")).strip().lower()
            company_key = re.sub(r"\s+", " ", company_key)

            if company_key and company_key in batch_companies_seen:
                reason = "duplicate_company_in_batch"
                rejected_item = {**job, "reason": reason, "round_mode": round_mode, "batch": batch_number}
                rejected_jobs.append(rejected_item)
                print_skip(job, "STATIC", reason)
                continue

            if company_key:
                batch_companies_seen.add(company_key)
                seen_companies_round.add(company_key)

            reject_pre_static, pre_static_reason = cheap_pre_review_reject(job, search_contract, user_profile=user_profile, automation=automation)
            if reject_pre_static:
                rejected_item = {**job, "reason": pre_static_reason, "round_mode": round_mode, "batch": batch_number}
                rejected_jobs.append(rejected_item)
                mark_shared_inventory_job_bad(job, pre_static_reason)
                round_metrics["cheap_pre_review_rejections"] += 1
                print_skip(job, "CHEAP_PRE_STATIC", pre_static_reason)
                continue

            if job.get("job_url"):
                avoid_urls.add(normalize_url(job["job_url"]))

            ok, reason = static_check(
                job=job,
                existing_urls=existing_urls,
                existing_company_titles=existing_company_titles,
                seen_urls=seen_urls,
                seen_company_titles=seen_company_titles,
            )

            if ok:
                static_pass_jobs.append(job)
            else:
                resolved_any = False

                if (
                    not NO_GPT_MODE
                    and should_attempt_direct_resolution(job, reason)
                    and direct_resolution_attempts_this_batch < MAX_DIRECT_RESOLUTION_ATTEMPTS_PER_BATCH
                ):
                    direct_resolution_attempts_this_batch += 1
                    round_metrics["direct_resolution_attempts"] += 1
                    print(
                        f"DIRECT RESOLUTION: trying to resolve mirror/generic URL for "
                        f"{job.get('title')} — {job.get('company')} ({reason})"
                    )

                    resolved_candidates = resolve_direct_job_urls(
                        job=job,
                        avoid_urls=avoid_urls,
                        rejected_domains=rejected_domains,
                    )

                    for resolved_job in resolved_candidates:
                        resolved_norm = normalize_url(resolved_job.get("job_url", ""))
                        if resolved_norm:
                            avoid_urls.add(resolved_norm)

                        ok_resolved, resolved_reason = static_check(
                            job=resolved_job,
                            existing_urls=existing_urls,
                            existing_company_titles=existing_company_titles,
                            seen_urls=seen_urls,
                            seen_company_titles=seen_company_titles,
                        )

                        if ok_resolved:
                            static_pass_jobs.append(resolved_job)
                            round_metrics["direct_resolution_successes"] += 1
                            resolved_any = True
                            print(
                                f"DIRECT RESOLUTION OK: {resolved_job.get('title')} — "
                                f"{resolved_job.get('company')} — {resolved_job.get('job_url')}"
                            )
                            break

                        rejected_resolved = {
                            **resolved_job,
                            "reason": f"direct_resolution_{resolved_reason}",
                            "round_mode": round_mode,
                            "batch": batch_number,
                        }
                        rejected_jobs.append(rejected_resolved)
                        maybe_track_rejected_domain(resolved_job, resolved_reason, rejected_domains)
                        print_skip(resolved_job, "DIRECT_RESOLUTION_STATIC", resolved_reason)

                if not resolved_any:
                    rejected_item = {**job, "reason": reason, "round_mode": round_mode, "batch": batch_number}
                    rejected_jobs.append(rejected_item)
                    mark_shared_inventory_job_bad(job, reason)
                    maybe_track_rejected_domain(job, reason, rejected_domains)
                    print_skip(job, "STATIC", reason)

        batch_static_pass = len(static_pass_jobs)
        print(f"Static passed: {batch_static_pass}")
        round_metrics["static_pass_count"] += batch_static_pass
        for _job in static_pass_jobs:
            _src = classify_job_source(_job.get("source", ""))
            round_metrics["source_funnel"][_src]["static_pass"] += 1

        if not static_pass_jobs:
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": "all_jobs_failed_static_validation",
                "message": (
                    f"All {batch_returned} returned jobs failed static URL validation — "
                    "wrong domain pattern, aggregator, generic page, or dupe. "
                    "Next batch must use only direct ATS/company job URLs with real job IDs. "
                    "Avoid rejected domains and aggregator/mirror sources."
                ),
                "rejected_domains": sorted(rejected_domains)[-50:],
                "round_mode": round_mode,
                "stats": {"returned": batch_returned, "static_pass": 0, "remote_pass": 0, "approved": 0},
            })
            print("No jobs passed static check. Trying next batch.")
            consecutive_zero_approval_batches += 1
            continue

        pre_review_pass_jobs = []
        for job in static_pass_jobs:
            reject_pre, pre_reason = cheap_pre_review_reject(job, search_contract, user_profile=user_profile, automation=automation)
            if reject_pre:
                rejected_item = {**job, "reason": pre_reason, "round_mode": round_mode, "batch": batch_number}
                rejected_jobs.append(rejected_item)
                mark_shared_inventory_job_bad(job, pre_reason)
                round_metrics["cheap_pre_review_rejections"] += 1
                print_skip(job, "CHEAP_PRE_REVIEW", pre_reason)
                continue
            pre_review_pass_jobs.append(job)

        print(f"Cheap pre-review passed: {len(pre_review_pass_jobs)}")

        if not pre_review_pass_jobs:
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": "all_jobs_failed_cheap_pre_review",
                "message": (
                    f"{batch_returned} returned, {batch_static_pass} passed static URL check, "
                    "but all were obvious function/location/seniority misses before remote validation. "
                    "Next batch should search closer title/function/location matches."
                ),
                "round_mode": round_mode,
                "stats": {"returned": batch_returned, "static_pass": batch_static_pass, "remote_pass": 0, "approved": 0},
            })
            print("No jobs survived cheap pre-review. Trying next batch.")
            consecutive_zero_approval_batches += 1
            continue

        live_jobs_for_review = []
        remote_resolution_attempts_this_batch = 0

        with ThreadPoolExecutor(max_workers=REMOTE_WORKERS) as executor:
            future_map = {
                executor.submit(remote_check, job): job
                for job in pre_review_pass_jobs
            }

            for future in as_completed(future_map):
                job = future_map[future]

                try:
                    ok, reason = future.result()
                except Exception as e:
                    ok = False
                    reason = f"remote_exception: {str(e)[:80]}"

                if not ok:
                    resolved_live_job = None

                    if (
                        not NO_GPT_MODE
                        and should_attempt_direct_resolution(job, reason)
                        and remote_resolution_attempts_this_batch < MAX_REMOTE_FAILURE_RESOLUTION_ATTEMPTS_PER_BATCH
                    ):
                        remote_resolution_attempts_this_batch += 1
                        round_metrics["direct_resolution_attempts"] += 1
                        print(
                            f"REMOTE RESOLUTION: trying alternate direct URL for "
                            f"{job.get('title')} — {job.get('company')} ({reason})"
                        )

                        resolved_candidates = resolve_direct_job_urls(
                            job=job,
                            avoid_urls=avoid_urls,
                            rejected_domains=rejected_domains,
                        )

                        for resolved_job in resolved_candidates:
                            resolved_norm = normalize_url(resolved_job.get("job_url", ""))
                            if resolved_norm:
                                avoid_urls.add(resolved_norm)

                            ok_resolved_static, resolved_static_reason = static_check(
                                job=resolved_job,
                                existing_urls=existing_urls,
                                existing_company_titles=existing_company_titles,
                                seen_urls=seen_urls,
                                seen_company_titles=seen_company_titles,
                            )

                            if not ok_resolved_static:
                                rejected_resolved = {
                                    **resolved_job,
                                    "reason": f"remote_resolution_{resolved_static_reason}",
                                    "round_mode": round_mode,
                                    "batch": batch_number,
                                }
                                rejected_jobs.append(rejected_resolved)
                                maybe_track_rejected_domain(resolved_job, resolved_static_reason, rejected_domains)
                                print_skip(resolved_job, "REMOTE_RESOLUTION_STATIC", resolved_static_reason)
                                continue

                            reject_resolved_pre, resolved_pre_reason = cheap_pre_review_reject(
                                resolved_job,
                                search_contract,
                                user_profile=user_profile,
                                automation=automation,
                            )
                            if reject_resolved_pre:
                                rejected_resolved = {
                                    **resolved_job,
                                    "reason": f"remote_resolution_{resolved_pre_reason}",
                                    "round_mode": round_mode,
                                    "batch": batch_number,
                                }
                                rejected_jobs.append(rejected_resolved)
                                round_metrics["cheap_pre_review_rejections"] += 1
                                print_skip(resolved_job, "REMOTE_RESOLUTION_PRE_REVIEW", resolved_pre_reason)
                                continue

                            ok_resolved_live, resolved_live_reason = remote_check(resolved_job)
                            if not ok_resolved_live:
                                rejected_resolved = {
                                    **resolved_job,
                                    "reason": f"remote_resolution_{resolved_live_reason}",
                                    "round_mode": round_mode,
                                    "batch": batch_number,
                                }
                                rejected_jobs.append(rejected_resolved)
                                maybe_track_rejected_domain(resolved_job, resolved_live_reason, rejected_domains)
                                print_skip(resolved_job, "REMOTE_RESOLUTION_REMOTE", resolved_live_reason)
                                continue

                            round_metrics["direct_resolution_successes"] += 1
                            resolved_live_job = resolved_job
                            print(
                                f"REMOTE RESOLUTION OK: {resolved_job.get('title')} — "
                                f"{resolved_job.get('company')} — {resolved_job.get('job_url')}"
                            )
                            break

                    if not resolved_live_job:
                        rejected_item = {**job, "reason": reason, "round_mode": round_mode, "batch": batch_number}
                        rejected_jobs.append(rejected_item)
                        mark_shared_inventory_job_bad(job, reason)
                        maybe_track_rejected_domain(job, reason, rejected_domains)
                        # Mark the company as dead so the model never retries it.
                        company_norm = re.sub(r"\s+", " ", str(job.get("company", "")).strip().lower())
                        fail_type = str(reason).split(":")[0]
                        batch_remote_fail_types.append(fail_type)
                        if company_norm and fail_type in {
                            "404_not_found", "soft_404_or_not_found", "expired",
                            "dead_job_url_pattern", "wrong_job",
                        }:
                            rejected_companies.add(company_norm)
                        print_skip(job, "REMOTE", reason)
                        continue

                    job = resolved_live_job

                candidate_job = {
                    "job_url": job["job_url"],
                    "title": job["title"],
                    "description": job["description"],
                    "company": job["company"],
                    "grade": normalize_grade(job.get("grade", 75)),
                    "status": job_status,
                    "location": job.get("location", ""),
                    "source": job.get("source", ""),
                }

                live_jobs_for_review.append(candidate_job)

                print("\nLIVE JOB READY FOR AI REVIEW:")
                print(f"• {candidate_job['title']} — {candidate_job['company']}")
                print(f"  Grade: {candidate_job['grade']}")
                print(f"  URL: {candidate_job['job_url']}")

        batch_remote_pass = len(live_jobs_for_review)
        print(f"\nLive jobs ready for review after batch {batch_number}: {batch_remote_pass}")
        round_metrics["live_jobs_count"] += batch_remote_pass
        round_metrics["ai_reviewed_count"] += batch_remote_pass
        for _job in live_jobs_for_review:
            _src = classify_job_source(_job.get("source", ""))
            round_metrics["source_funnel"][_src]["remote_pass"] += 1

        if not live_jobs_for_review:
            remote_fail_summary = dict(Counter(batch_remote_fail_types).most_common(5))
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": "all_jobs_failed_remote_validation",
                "message": (
                    f"{batch_returned} returned, {batch_static_pass} passed static, "
                    f"0 survived HTTP check. Failure types: {remote_fail_summary}. "
                    "These URLs are expired, 404'd, or blocked. "
                    "Next batch must use fresh ATS job IDs — avoid boards, aggregators, and career pages without specific job IDs."
                ),
                "remote_fail_types": remote_fail_summary,
                "rejected_domains": sorted(rejected_domains)[-50:],
                "round_mode": round_mode,
                "stats": {"returned": batch_returned, "static_pass": batch_static_pass, "remote_pass": 0, "approved": 0},
            })
            print("No live jobs to review this batch. Trying next batch.")
            consecutive_zero_approval_batches += 1
            continue

        approved_jobs, review_rejected_jobs = review_and_filter_jobs(
            user_profile=user_profile,
            automation=automation,
            cv_text=cv_text,
            persona=persona,
            jobs=live_jobs_for_review,
            job_status=job_status,
            search_contract=search_contract,
            minimum_viable_mode=minimum_viable_mode,
        )
        round_metrics["openai_review_calls"] += 1

        review_rejected_jobs_this_round.extend(review_rejected_jobs)
        rejected_jobs.extend({**job, "round_mode": round_mode, "batch": batch_number} for job in review_rejected_jobs)
        round_metrics["review_rejected_count"] += len(review_rejected_jobs)

        for rejected_job in review_rejected_jobs:
            maybe_track_rejected_domain(
                rejected_job,
                rejected_job.get("review_decision") or rejected_job.get("review_reason"),
                rejected_domains,
            )

        approved_this_batch_count = 0

        if approved_jobs:
            approved_jobs = approved_jobs[:still_needed]

            print_jobs_for_user(
                user_profile,
                approved_jobs,
                title=f"APPROVED JOBS TO POST FROM BATCH {batch_number}",
            )

            try:
                post_result = api_post_jobs(email, approved_jobs, default_status=job_status)
                print("\nPOST RESULT:")
                print(json.dumps(post_result, indent=2))

                posted_jobs_this_round.extend(approved_jobs)
                approved_this_batch_count = len(approved_jobs)
                round_metrics["approved_count"] += approved_this_batch_count

                for posted_job in approved_jobs:
                    existing_urls.add(normalize_url(posted_job["job_url"]))
                    existing_company_titles.add(company_title_key(posted_job))
                    avoid_urls.add(normalize_url(posted_job["job_url"]))
                    _src = classify_job_source(posted_job.get("source", ""))
                    round_metrics["source_funnel"][_src]["added"] += 1

            except Exception as e:
                print(f"POST FAILED for {email}. Jobs not counted as posted. Error: {e}")
                failed_batch_feedback.append({
                    "batch": batch_number,
                    "reason": "post_failed",
                    "message": str(e)[:300],
                    "round_mode": round_mode,
                    "stats": {"returned": batch_returned, "static_pass": batch_static_pass, "remote_pass": batch_remote_pass, "approved": 0},
                })
        else:
            decision_counts = dict(Counter(j.get("review_decision", "unknown") for j in review_rejected_jobs).most_common(5))
            grades = [j.get("grade", 0) for j in review_rejected_jobs if j.get("grade")]
            avg_grade = round(sum(grades) / len(grades)) if grades else 0
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": "all_jobs_failed_ai_review",
                "message": (
                    f"{batch_returned} returned, {batch_static_pass} static, {batch_remote_pass} remote, "
                    f"all {len(review_rejected_jobs)} failed AI review (avg grade {avg_grade}). "
                    f"Decision breakdown: {decision_counts}. "
                    "Improve title/function/location fit and role alignment in next batch."
                ),
                "review_decision_counts": decision_counts,
                "avg_review_grade": avg_grade,
                "round_mode": round_mode,
                "stats": {"returned": batch_returned, "static_pass": batch_static_pass, "remote_pass": batch_remote_pass, "approved": 0},
            })
            print("\nNo jobs passed AI review in this batch. Nothing posted.")

        batch_approved = approved_this_batch_count
        if batch_approved > 0:
            consecutive_zero_approval_batches = 0
            failed_batch_feedback.append({
                "batch": batch_number,
                "reason": "batch_succeeded",
                "message": f"Approved and posted {batch_approved} jobs.",
                "round_mode": round_mode,
                "stats": {"returned": batch_returned, "static_pass": batch_static_pass, "remote_pass": batch_remote_pass, "approved": batch_approved},
            })
        else:
            consecutive_zero_approval_batches += 1

        total_after_batch = pending_today_before + len(posted_jobs_this_round)
        batches_left = max_batches_for_round - batch_number
        print(
            f"\nBATCH {batch_number} FUNNEL:  "
            f"{batch_returned} returned"
            f" → {batch_static_pass} static"
            f" → {batch_remote_pass} remote"
            f" → {len(review_rejected_jobs)} AI rejected"
            f" → {batch_approved} posted"
        )
        outcome = f"✓{batch_approved}" if batch_approved > 0 else "✗0"
        print(
            f"BATCH {batch_number} DONE  [{outcome} posted]  "
            f"{total_after_batch}/{TARGET_JOBS_PER_USER} jobs  ·  "
            f"{batches_left} batch(es) left  ·  "
            f"zeros streak: {consecutive_zero_approval_batches}  ·  "
            f"dead domains: {len(rejected_domains)}"
        )

        if pending_today_before + len(posted_jobs_this_round) < TARGET_JOBS_PER_USER:
            time.sleep(1)

    pending_today_after_estimate = pending_today_before + len(posted_jobs_this_round)
    needs_more = pending_today_after_estimate < TARGET_JOBS_PER_USER

    # Only pivot when the round genuinely struggled (≥2 zero-approval batches).
    # Avoids 2 wasted AI calls when Round 1 simply ran out of time/batches.
    round_had_enough_failures = consecutive_zero_approval_batches >= 2 or not posted_jobs_this_round
    if round_mode == "first_round" and needs_more and round_had_enough_failures:
        first_pivot = maybe_create_strategy_pivot(
            user_profile=user_profile,
            automation=automation,
            cv_text=cv_text,
            persona=persona,
            search_contract=search_contract,
            batch_number=max_batches_for_round,
            posted_jobs_this_round=posted_jobs_this_round,
            failed_batch_feedback=failed_batch_feedback,
            rejected_jobs=rejected_jobs,
            rejected_domains=rejected_domains,
            strategy_pivot_count=strategy_pivot_count,
            pivot_stage="first_round_after_final_batch",
            pivot_guidance=FIRST_STRATEGY_PIVOT_GUIDANCE,
            force=True,
        )
        apply_pivot(first_pivot)

    print_jobs_for_user(
        user_profile,
        posted_jobs_this_round,
        title="FINAL JOBS POSTED FOR THIS USER THIS ROUND",
    )

    print("\n============================================================")
    print(f"ROUND {round_number} COMPLETE  ·  {email}")
    print(
        f"Before: {pending_today_before}  +added: {len(posted_jobs_this_round)}"
        f"  =after: {pending_today_after_estimate}/{TARGET_JOBS_PER_USER}"
        f"  |  needs_more: {needs_more}"
    )
    print(
        f"Funnel:  {round_metrics['openai_jobs_returned']} returned"
        f" → {round_metrics['static_pass_count']} static"
        f" → {round_metrics['live_jobs_count']} remote"
        f" → {round_metrics['review_rejected_count']} AI rejected"
        f" → {round_metrics['approved_count']} posted"
    )
    print(
        f"Plan: {user_plan}  |  status: {job_status}  |  pivots: {strategy_pivot_count}"
        f"  |  dead companies: {len(rejected_companies)}  |  dead domains: {len(rejected_domains)}"
    )
    print("============================================================")

    _cost_breakdown = {
        "openai_search":   round(round_metrics["openai_search_calls"]       * COST_PER_OPENAI_SEARCH_CALL,   4),
        "openai_review":   round(round_metrics["openai_review_calls"]        * COST_PER_OPENAI_REVIEW_CALL,   4),
        "openai_resolver": round(round_metrics["direct_resolution_attempts"] * COST_PER_OPENAI_RESOLVER_CALL, 4),
        "openai_pivot":    round(round_metrics["openai_pivot_calls"]         * COST_PER_OPENAI_PIVOT_CALL,    4),
        "openai_persona":  round(COST_PER_PERSONA_CREATE  if round_metrics["persona_created_this_run"]  else 0, 4),
        "openai_contract": round(COST_PER_CONTRACT_CREATE if round_metrics["contract_created_this_run"] else 0, 4),
        "apify_linkedin":  round(round_metrics["linkedin_raw_results"] * COST_PER_LINKEDIN_RESULT, 4),
        "apify_hc":        round(round_metrics["hc_raw_results"]      * COST_PER_HC_RESULT,        4),
        "jobo":            round(round_metrics["jobo_raw_results"]     * COST_PER_JOBO_RESULT,      4),
    }
    round_metrics["cost_breakdown"] = _cost_breakdown
    round_metrics["estimated_cost_usd"] = round(sum(_cost_breakdown.values()), 4)

    return {
        "user": user,
        "user_profile": user_profile,
        "automation": automation,
        "persona": persona,
        "search_contract": search_contract,
        "cv_text": cv_text,
        "jobs_added": posted_jobs_this_round,
        "jobs_rejected_by_review": review_rejected_jobs_this_round,
        "all_rejected_jobs": rejected_jobs[-400:],
        "rejected_domains": sorted(rejected_domains),
        "rejected_companies": sorted(rejected_companies),
        "seen_companies_round": sorted(seen_companies_round),
        "pending_today_before": pending_today_before,
        "pending_today_after_estimate": pending_today_after_estimate,
        "needs_more": needs_more,
        "relaxation_strategy_report": relaxation_strategy_report,
        "strategy_pivots": strategy_pivots,
        "strategy_prompt_patch": strategy_prompt_patch,
        "failed_batch_feedback": failed_batch_feedback,
        "round_number": round_number,
        "round_mode": round_mode,
        "max_batches_for_round": max_batches_for_round,
        "user_plan": user_plan,
        "job_status": job_status,
        "round_metrics": round_metrics,
    }



def normalize_selected_email(value):
    return str(value or "").strip().lower()


def split_multi_cli_values(values):
    """Split one or many CLI/env values into clean tokens.

    Supports all of these:
      --email a@example.com b@example.com
      --emails a@example.com,b@example.com
      JOBBYO_SINGLE_USER_EMAILS="a@example.com b@example.com"
    """
    if values is None:
        return []

    if isinstance(values, str):
        values = [values]

    output = []
    for value in values:
        for item in re.split(r"[,\s]+", str(value or "")):
            item = normalize_selected_email(item)
            if item:
                output.append(item)

    return output


def selected_email_filter_active():
    return bool(SINGLE_USER_EMAILS or SINGLE_USER_EMAIL)


def selected_email_matches(email):
    if not selected_email_filter_active():
        return True

    selected = set(SINGLE_USER_EMAILS)
    if SINGLE_USER_EMAIL:
        selected.add(normalize_selected_email(SINGLE_USER_EMAIL))

    return normalize_selected_email(email) in selected


# ============================================================
# RUN ROUNDS
# ============================================================

def get_eligible_paid_users():
    paid_users = get_paid_users()

    print(f"\nPaid users returned: {len(paid_users)}")

    eligible = []

    for user in paid_users:
        uid = user.get("uid")
        email = user.get("email")
        display_name = user.get("displayName")

        if MAX_USERS_TO_PROCESS is not None and len(eligible) >= MAX_USERS_TO_PROCESS:
            break

        print("\n############################################################")
        print(f"CHECKING ELIGIBILITY: {display_name} — {email} — {uid}")
        print("############################################################")

        if not uid or not email:
            print("SKIP: missing uid/email")
            continue

        if normalize_selected_email(email) in {normalize_selected_email(e) for e in EXCLUDED_USER_EMAILS}:
            print(f"SKIP: excluded user — {email}")
            continue

        if not selected_email_matches(email):
            continue

        if SINGLE_USER_UID and uid != SINGLE_USER_UID:
            continue

        if not is_paid_user(user):
            print("SKIP: not active paid user")
            print("  paid debug:", json.dumps(user_paid_debug_summary(user), indent=2, default=str))
            continue

        try:
            automation = get_user_automation(uid)
            user["_automation_cache"] = automation
        except Exception as e:
            print(f"SKIP: could not fetch automation — {e}")
            continue

        if not should_bypass_automation_check(user, email) and not has_active_automation(automation):
            print("SKIP: no active automation")
            print("  automation debug:", json.dumps(automation_debug_summary(automation), indent=2, default=str))
            continue

        # Skip users with no job titles — we have nothing to search with.
        # Send them a one-time nudge email to complete their profile.
        _titles = extract_automation_job_titles(automation, limit=3)
        if not _titles:
            print(f"SKIP: no job titles configured — sending profile-incomplete email to {email}")
            send_incomplete_profile_email(email, display_name or email)
            continue

        plan = get_user_plan(user=user, automation=automation)
        job_status = status_for_user_plan(plan)
        existing_jobs = extract_existing_jobs(automation)
        pending_today = count_jobs_today(existing_jobs, job_status)

        print("Eligible: yes")
        print(f"Detected plan: {plan}")
        print(f"Counted {job_status}/legacy jobs today: {pending_today}")

        eligible.append(user)

    return eligible


def run_round(
    users,
    round_number,
    max_batches_for_round,
    round_mode,
    previous_results_by_uid=None,
    minimum_viable_mode=False,
    accumulated_results=None,
    source_limit_overrides=None,
):
    previous_results_by_uid = previous_results_by_uid or {}

    print("\n\n============================================================")
    print(f"STARTING ROUND {round_number} — {round_mode}")
    print(f"Max batches this round: {max_batches_for_round}")
    print("============================================================")

    round_results = []

    for user in users:
        uid = user.get("uid")
        email = user.get("email")
        display_name = user.get("displayName")

        print("\n\n############################################################")
        print(f"ROUND {round_number} USER: {display_name} — {email} — {uid}")
        print("############################################################")

        try:
            automation = user.get("_automation_cache") or get_user_automation(uid)
            if automation is not None:
                user["_automation_cache"] = automation
            if not should_bypass_automation_check(user, email) and not has_active_automation(automation):
                print("SKIP: no active automation")
                print("  automation debug:", json.dumps(automation_debug_summary(automation), indent=2, default=str))
                continue

            plan = get_user_plan(user=user, automation=automation)
            job_status = status_for_user_plan(plan)
            existing_jobs = extract_existing_jobs(automation or {})
            pending_today = count_jobs_today(existing_jobs, job_status)

            if pending_today >= TARGET_JOBS_PER_USER:
                print(f"SKIP: already has {pending_today} {job_status}/legacy jobs today.")
                continue

            result = find_jobs_for_user(
                user=user,
                round_number=round_number,
                max_batches_for_round=max_batches_for_round,
                round_mode=round_mode,
                previous_result=previous_results_by_uid.get(uid),
                minimum_viable_mode=minimum_viable_mode,
                source_limit_overrides=(source_limit_overrides or {}).get(user.get("uid")),
            )
            round_results.append(result)
            if accumulated_results is not None:
                accumulated_results.append(result)
                save_run_log(accumulated_results)

        except Exception as e:
            print(f"FAILED USER {email}: {e}")

    print("\n\n============================================================")
    print(f"ROUND {round_number} DONE — {round_mode}")
    print("============================================================")

    total_added = sum(len(r.get("jobs_added", [])) for r in round_results)
    total_review_rejected = sum(len(r.get("jobs_rejected_by_review", [])) for r in round_results)
    incomplete = [r for r in round_results if r.get("needs_more")]

    print(f"Users processed this round: {len(round_results)}")
    print(f"Jobs approved and posted this round: {total_added}")
    print(f"Jobs rejected by AI review this round: {total_review_rejected}")
    print(f"Users still below {TARGET_JOBS_PER_USER}: {len(incomplete)}")

    if incomplete:
        print("\nUSERS STILL BELOW TARGET:")
        for r in incomplete:
            user_profile = r.get("user_profile") or {}
            user = r.get("user") or {}
            email = user_profile.get("email") or user.get("email")
            name = user_profile.get("displayName") or user.get("displayName")
            count = r.get("pending_today_after_estimate", 0)
            print(f"• {name} — {email} — estimated pending today: {count}/{TARGET_JOBS_PER_USER}")

    return round_results


def get_users_below_pending_threshold(users, threshold):
    below_threshold = []

    print("\n\n============================================================")
    print(f"CHECKING WHO IS BELOW {threshold} JOBS TODAY")
    print("============================================================")

    for user in users:
        uid = user.get("uid")
        email = user.get("email")
        name = user.get("displayName")

        try:
            automation = user.get("_automation_cache") or get_user_automation(uid)
            if automation is not None:
                user["_automation_cache"] = automation
            plan = get_user_plan(user=user, automation=automation)
            job_status = status_for_user_plan(plan)
            existing_jobs = extract_existing_jobs(automation or {})
            pending_today = count_jobs_today(existing_jobs, job_status)

            print(f"• {name} — {email}: {pending_today}/{TARGET_JOBS_PER_USER} {job_status}/legacy today ({plan})")

            if pending_today < threshold:
                below_threshold.append(user)

        except Exception as e:
            print(f"Could not check {email}: {e}")

    return below_threshold


def should_run_second_round_for_result(result, threshold=MIN_JOBS_BEFORE_SECOND_ROUND):
    """Run Round 2 only when Round 1 shows enough useful supply signal.

    Being below the daily count is not enough. If Round 1 mostly produced
    aggregators, generic pages, 404s, or obvious mismatches, another search round
    usually just burns calls.
    """
    if not result:
        return False, "missing_first_round_result"

    pending_after = int(result.get("pending_today_after_estimate") or 0)
    if pending_after >= threshold:
        return False, f"already_at_or_above_threshold_{threshold}"

    if ENABLE_QUOTA_MODE:
        return True, f"quota_mode_user_below_target_{pending_after}_of_{threshold}"

    metrics = result.get("round_metrics") or {}
    added = len(result.get("jobs_added") or [])
    live_jobs = int(metrics.get("live_jobs_count") or 0)
    static_pass = int(metrics.get("static_pass_count") or 0)
    openai_returned = int(metrics.get("openai_jobs_returned") or 0)
    direct_successes = int(metrics.get("direct_resolution_successes") or 0)
    review_rejected = len(result.get("jobs_rejected_by_review") or [])

    static_pass_rate = static_pass / max(1, openai_returned)

    if added >= 3:
        return True, "three_or_more_jobs_added"

    if direct_successes >= 1 and static_pass >= 5:
        return True, "direct_url_resolver_found_some_supply"

    if live_jobs >= 2 and static_pass >= 6:
        return True, "reviewable_supply_needs_better_second_pivot"

    if static_pass_rate >= 0.20 and static_pass >= 8:
        return True, "enough_static_supply_for_smarter_second_round"

    if added >= 1 and live_jobs >= 3 and static_pass_rate >= 0.12:
        return True, "some_added_and_more_live_supply"

    return False, (
        f"low_supply_signal_added={added}_live={live_jobs}_"
        f"review_rejected={review_rejected}_static_rate={static_pass_rate:.2f}_"
        f"direct_successes={direct_successes}"
    )


def get_second_round_users_from_first_round(users, first_results_by_uid, threshold):
    selected = []

    print("\n\n============================================================")
    print(f"CHECKING WHO QUALIFIES FOR SECOND ROUND BELOW {threshold} JOBS")
    print("============================================================")

    for user in users:
        uid = user.get("uid")
        email = user.get("email")
        name = user.get("displayName")
        result = first_results_by_uid.get(uid)
        should_run, reason = should_run_second_round_for_result(result, threshold=threshold)

        pending_after = 0
        if result:
            pending_after = result.get("pending_today_after_estimate", 0)

        print(f"• {name} — {email}: pending={pending_after}/{TARGET_JOBS_PER_USER}; second_round={should_run}; reason={reason}")

        if should_run:
            selected.append(user)

    return selected


# ============================================================
# LOGGING
# ============================================================

def build_daily_report_payload(result):
    user_profile = result.get("user_profile") or {}
    user = result.get("user") or {}
    email = user_profile.get("email") or user.get("email") or ""
    name = (
        user_profile.get("displayName")
        or user.get("displayName")
        or email.split("@")[0]
    )

    jobs_added = result.get("jobs_added") or []
    metrics = result.get("round_metrics") or {}
    strategy_pivots = result.get("strategy_pivots") or []
    strategy_prompt_patch = (result.get("strategy_prompt_patch") or "").strip()
    failed_batch_feedback = result.get("failed_batch_feedback") or []
    pending_after = result.get("pending_today_after_estimate", 0)
    pending_before = result.get("pending_today_before", 0)
    needs_more = result.get("needs_more", False)

    # --- Jobs list ---
    jobs = []
    for job in jobs_added:
        title_line = job.get("title", "")
        company = job.get("company", "")
        full_title = f"{title_line} @ {company}" if company else title_line
        job_url = job.get("job_url", "")
        reason = (job.get("review_reason") or "Matched your profile").strip()
        if job_url:
            reason = reason + f"\n\n{job_url}"
        score = job.get("grade") or job.get("review_confidence") or 0
        jobs.append({
            "job_title": full_title,
            "company": company,
            "url": job_url,
            "location": job.get("location", ""),
            "reason": reason,
            "score": score,
        })

    # --- Changed rules ---
    _field_labels = {
        "allowed_titles": "Job titles",
        "forbidden_titles": "Excluded titles",
        "allowed_functions": "Role types",
        "forbidden_functions": "Excluded roles",
        "location_hard_rule": "Location",
        "salary_hard_rule": "Salary",
        "source_strategy": "Job sources",
        "hard_reject_rules": "Filters",
        "seniority_rules": "Seniority",
        "search_queries": "Search terms",
        "broadening_plan_if_zero_results": "Broadening plan",
    }
    if strategy_pivots:
        latest = strategy_pivots[-1]
        adaptation = latest.get("adaptation") or {}
        changed_list = adaptation.get("changed_rules") or []
        readable = []
        for item in changed_list[:5]:
            if "unchanged" in item.lower():
                continue
            humanized = item
            for key, label in _field_labels.items():
                if item.lower().startswith(key):
                    humanized = label + item[len(key):]
                    break
            readable.append(f"• {humanized}")
        changed_rules = "\n".join(readable) if readable else "Search strategy adjusted based on this run's results."
    else:
        changed_rules = "No strategy changes this run."

    # --- Next batch strategy ---
    if strategy_prompt_patch:
        next_batch_strategy = strategy_prompt_patch[:400]
    elif failed_batch_feedback:
        last_good = next(
            (f for f in reversed(failed_batch_feedback) if f.get("reason") == "batch_succeeded"),
            None,
        )
        if last_good:
            next_batch_strategy = f"Continuing the approach from the last successful batch: {last_good.get('message', '')}"
        else:
            next_batch_strategy = "Broadening search criteria to find more matches."
    else:
        next_batch_strategy = "Continuing current search strategy."

    # --- Comments (human-readable run summary) ---
    approved = metrics.get("approved_count", len(jobs_added))
    ai_reviewed = metrics.get("ai_reviewed_count", 0)
    returned = metrics.get("openai_jobs_returned", 0)
    static_pass = metrics.get("static_pass_count", 0)
    review_rejected = metrics.get("review_rejected_count", 0)

    new_jobs = pending_after - pending_before
    if new_jobs > 0:
        comments = (
            f"Great news — {new_jobs} new job{'s' if new_jobs != 1 else ''} added to your queue today! "
            f"We reviewed {ai_reviewed} job{'s' if ai_reviewed != 1 else ''} (from {returned} sourced), "
            f"with {review_rejected} rejected by AI before reaching your inbox. "
        )
    else:
        comments = (
            f"No new jobs were added this run. "
            f"We sourced {returned} jobs, {static_pass} passed initial filters, "
            f"and {ai_reviewed} were reviewed by AI — none met the bar this time. "
            f"We're adjusting the strategy for the next run."
        )

    if needs_more:
        comments += f" You still need {TARGET_JOBS_PER_USER - pending_after} more job{'s' if TARGET_JOBS_PER_USER - pending_after != 1 else ''} to hit today's target — we'll keep looking."

    uid = user_profile.get("uid") or user.get("uid") or ""
    app_url = f"https://app.jobbyo.ai/auto-apply/{uid}" if uid else ""
    to_email = DAILY_REPORT_OVERRIDE_EMAIL or email
    return {
        "email": to_email,
        "name": name,
        "report": {
            "jobs": jobs,
            "changed_rules": changed_rules,
            "next_batch_strategy": next_batch_strategy,
            "comments": comments,
            "app_url": app_url,
        },
    }


def send_incomplete_profile_email(email, name):
    """Notify a user that their profile has no job titles and we couldn't search for them."""
    payload = {
        "email": email,
        "name": name,
        "type": "incomplete_profile",
        "message": (
            "We tried to find jobs for you today but your profile doesn't have any job titles set. "
            "Please log in and add your target job titles so we can start finding matches for you."
        ),
    }
    try:
        resp = requests.post(
            f"{DAILY_REPORT_API_BASE}/api/notifications/incomplete-profile",
            json=payload,
            timeout=15,
        )
        print(f"Incomplete-profile email → {email}  [{resp.status_code}]")
    except Exception as e:
        print(f"Incomplete-profile email failed ({email}): {e}")


def send_daily_report(result):
    payload = build_daily_report_payload(result)
    jobs_count = len(payload["report"]["jobs"])
    to = payload["email"]
    try:
        resp = requests.post(
            f"{DAILY_REPORT_API_BASE}/api/reports/daily",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"Daily report → {to}  ({jobs_count} job{'s' if jobs_count != 1 else ''})  [{resp.status_code}]")
    except Exception as e:
        print(f"Daily report send failed ({to}): {e}")


def send_slack_run_report(results_by_uid):
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    users = []
    total_found = 0
    total_needed = 0
    manual_search = []

    for uid, r in results_by_uid.items():
        user_profile = r.get("user_profile") or {}
        automation = r.get("automation") or {}
        prefs = automation.get("jobPreferences") or {}
        name = user_profile.get("displayName") or (r.get("user") or {}).get("displayName") or ""
        email = user_profile.get("email") or (r.get("user") or {}).get("email") or ""
        jobs_found = len(r.get("jobs_added") or [])
        pending = r.get("pending_today_after_estimate", 0)
        needs_manual = pending < MIN_ACCEPTABLE_JOBS_PER_USER
        keywords = prefs.get("jobTitles") or []
        loc = prefs.get("location") or {}
        location_places = ", ".join(loc.get("places") or [])

        total_found += jobs_found
        total_needed += max(0, TARGET_JOBS_PER_USER - pending)

        entry = {
            "uid": uid,
            "name": name,
            "email": email,
            "jobs_found_today": jobs_found,
            "jobs_target": TARGET_JOBS_PER_USER,
            "location_preference": location_places,
            "search_keywords": keywords[:3],
            "daily_report_sent": True,
            "needs_manual_search": needs_manual,
        }
        users.append(entry)
        if needs_manual:
            manual_search.append({"name": name, "email": email, "keywords": keywords[:3], "location": location_places})

    payload = {
        "run_date": run_date,
        "total_jobs_found": total_found,
        "total_jobs_still_needed": total_needed,
        "users_processed": len(users),
        "emails_sent": len(users),
        "run_duration_minutes": 0,
        "user_results": users,
    }

    try:
        resp = requests.post(
            f"{DAILY_REPORT_API_BASE}/api/notifications/slack/daily-run",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"Slack run report sent  ({len(users)} users)  [{resp.status_code}]")
    except Exception as e:
        print(f"Slack run report failed: {e}")


def save_run_log(all_results):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = RUN_LOG_DIR / f"job_run_{timestamp}.json"

    clean_results = []

    for r in all_results:
        user = r.get("user") or {}
        user_profile = r.get("user_profile") or {}

        clean_results.append(
            {
                "uid": user_profile.get("uid") or user.get("uid"),
                "email": user_profile.get("email") or user.get("email"),
                "displayName": user_profile.get("displayName") or user.get("displayName"),
                "user_plan": r.get("user_plan"),
                "job_status": r.get("job_status"),
                "round_metrics": r.get("round_metrics", {}),
                "jobs_added": r.get("jobs_added", []),
                "jobs_rejected_by_review": r.get("jobs_rejected_by_review", []),
                "all_rejected_jobs": r.get("all_rejected_jobs", []),
                "rejected_domains": r.get("rejected_domains", []),
                "rejected_companies": r.get("rejected_companies", []),
                "seen_companies_round": r.get("seen_companies_round", []),
                "relaxation_strategy_report": r.get("relaxation_strategy_report"),
                "strategy_pivots": r.get("strategy_pivots", []),
                "strategy_prompt_patch": r.get("strategy_prompt_patch", ""),
                "failed_batch_feedback": r.get("failed_batch_feedback", []),
                "pending_today_before": r.get("pending_today_before", 0),
                "pending_today_after_estimate": r.get("pending_today_after_estimate", 0),
                "needs_more": r.get("needs_more", False),
                "round_number": r.get("round_number"),
                "round_mode": r.get("round_mode"),
                "max_batches_for_round": r.get("max_batches_for_round"),
            }
        )

    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_results, f, indent=2)

    print(f"\nSaved run log: {path}")


def load_previous_run_log():
    """Find and load the most recent run log to seed cross-run learning."""
    if not RUN_LOG_DIR.exists():
        return []
    log_files = sorted(RUN_LOG_DIR.glob("job_run_*.json"), reverse=True)
    if not log_files:
        return []
    latest = log_files[0]
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
        print(f"\nPrevious run log: {latest.name}  ({len(data)} result(s))")
        return data
    except Exception as e:
        print(f"\nWarning: could not load previous run log {latest.name}: {e}")
        return []


def build_previous_run_results_by_uid(previous_run_data):
    """Return a per-uid dict keeping the latest round entry per user."""
    by_uid = {}
    for entry in (previous_run_data or []):
        uid = entry.get("uid")
        if not uid:
            continue
        existing = by_uid.get(uid)
        if existing is None or (entry.get("round_number") or 0) >= (existing.get("round_number") or 0):
            by_uid[uid] = entry
    return by_uid


# ============================================================
# MAIN
# ============================================================

def apply_cli_overrides():
    global SINGLE_USER_EMAIL, SINGLE_USER_EMAILS, SINGLE_USER_UID, MAX_USERS_TO_PROCESS, NO_GPT_MODE

    # Load env-based multi-email filter first. CLI values are added to this set.
    env_email_values = []
    if SINGLE_USER_EMAIL:
        env_email_values.append(SINGLE_USER_EMAIL)
    env_email_values.append(os.getenv("JOBBYO_SINGLE_USER_EMAILS", ""))
    SINGLE_USER_EMAILS.update(split_multi_cli_values(env_email_values))

    args = sys.argv[1:]
    i = 0

    while i < len(args):
        arg = args[i]

        if arg in {"-h", "--help"}:
            print("""Usage:
  python3 send_jobbyo.py
  python3 send_jobbyo.py --email user@example.com
  python3 send_jobbyo.py --email user1@example.com user2@example.com user3@example.com
  python3 send_jobbyo.py --emails user1@example.com,user2@example.com,user3@example.com
  python3 send_jobbyo.py --email=user@example.com
  python3 send_jobbyo.py --emails=user1@example.com,user2@example.com
  python3 send_jobbyo.py --uid FirebaseUidHere
  python3 send_jobbyo.py --uid=FirebaseUidHere
  python3 send_jobbyo.py --max-users 3
  python3 send_jobbyo.py --max-users=3
  python3 send_jobbyo.py --nogpt

Environment alternatives:
  JOBBYO_SINGLE_USER_EMAIL=user@example.com python3 send_jobbyo.py
  JOBBYO_SINGLE_USER_EMAILS=user1@example.com,user2@example.com python3 send_jobbyo.py
  JOBBYO_SINGLE_USER_UID=FirebaseUidHere python3 send_jobbyo.py
  OPENAI_API_KEY=your_key_here python3 send_jobbyo.py
  JOBBYO_NO_GPT=1 JOBO_API_KEY=your_key python3 send_jobbyo.py
""")
            sys.exit(0)

        if arg in {"--nogpt", "--no-gpt", "--no-openai"}:
            NO_GPT_MODE = True
            i += 1
            continue

        if arg.startswith("--nogpt=") or arg.startswith("--no-gpt=") or arg.startswith("--no-openai="):
            NO_GPT_MODE = str(arg.split("=", 1)[1]).strip().lower() not in {"0", "false", "no", "off"}
            i += 1
            continue

        if arg in {"--email", "--single-email", "--emails"}:
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                raise SystemExit(f"Missing value after {arg}")

            values = []
            j = i + 1
            while j < len(args) and not args[j].startswith("--"):
                values.append(args[j])
                j += 1

            parsed_emails = split_multi_cli_values(values)
            if not parsed_emails:
                raise SystemExit(f"No valid email values after {arg}")

            SINGLE_USER_EMAILS.update(parsed_emails)
            i = j
            continue

        if arg.startswith("--email=") or arg.startswith("--emails="):
            value = arg.split("=", 1)[1].strip()
            parsed_emails = split_multi_cli_values(value)
            if not parsed_emails:
                raise SystemExit(f"No valid email values in {arg.split('=', 1)[0]}")

            SINGLE_USER_EMAILS.update(parsed_emails)
            i += 1
            continue

        if arg in {"--uid", "--single-uid"}:
            if i + 1 >= len(args):
                raise SystemExit("Missing value after --uid")
            SINGLE_USER_UID = args[i + 1].strip() or None
            i += 2
            continue

        if arg.startswith("--uid="):
            SINGLE_USER_UID = arg.split("=", 1)[1].strip() or None
            i += 1
            continue

        if arg == "--max-users":
            if i + 1 >= len(args):
                raise SystemExit("Missing value after --max-users")
            value = args[i + 1].strip()
            MAX_USERS_TO_PROCESS = int(value) if value else None
            i += 2
            continue

        if arg.startswith("--max-users="):
            value = arg.split("=", 1)[1].strip()
            MAX_USERS_TO_PROCESS = int(value) if value else None
            i += 1
            continue

        print(f"WARNING: unknown argument ignored: {arg}")
        i += 1

    SINGLE_USER_EMAILS = {normalize_selected_email(email) for email in SINGLE_USER_EMAILS if normalize_selected_email(email)}

    # Keep the old SINGLE_USER_EMAIL variable useful for logs/backward compatibility.
    if len(SINGLE_USER_EMAILS) == 1:
        SINGLE_USER_EMAIL = next(iter(SINGLE_USER_EMAILS))
    elif len(SINGLE_USER_EMAILS) > 1:
        SINGLE_USER_EMAIL = None
    elif SINGLE_USER_EMAIL:
        SINGLE_USER_EMAIL = normalize_selected_email(SINGLE_USER_EMAIL)
        SINGLE_USER_EMAILS.add(SINGLE_USER_EMAIL)

    if SINGLE_USER_UID:
        SINGLE_USER_UID = SINGLE_USER_UID.strip()

    if selected_email_filter_active() or SINGLE_USER_UID:
        MAX_USERS_TO_PROCESS = None


def main():
    apply_cli_overrides()

    print("Starting paid-user job automation script...")
    print(f"DRY_RUN={DRY_RUN}")
    print(f"NO_GPT_MODE={NO_GPT_MODE}")
    print(f"SEARCH_MODEL={SEARCH_MODEL}")
    print(f"REVIEW_MODEL={REVIEW_MODEL}")
    print(f"MAX_USERS_TO_PROCESS={MAX_USERS_TO_PROCESS}")
    print(f"TRUST_USERS_PAID_ENDPOINT={TRUST_USERS_PAID_ENDPOINT}")
    print(f"SINGLE_USER_EMAIL={SINGLE_USER_EMAIL}")
    print(f"SINGLE_USER_EMAILS={sorted(SINGLE_USER_EMAILS)}")
    print(f"SINGLE_USER_UID={SINGLE_USER_UID}")
    print(f"TARGET_JOBS_PER_USER={TARGET_JOBS_PER_USER}")
    print(f"STARTER_MAX_STATUS={STARTER_MAX_STATUS}")
    print(f"PREMIUM_STATUS={PREMIUM_STATUS}")
    print(f"MIN_JOBS_BEFORE_SECOND_ROUND={MIN_JOBS_BEFORE_SECOND_ROUND}")
    print(f"JOBS_PER_BATCH={JOBS_PER_BATCH}")
    print(f"FIRST_ROUND_BATCHES={FIRST_ROUND_BATCHES}")
    print(f"SECOND_ROUND_BATCHES={SECOND_ROUND_BATCHES}")
    print(f"MAX_ROUNDS_PER_USER={MAX_ROUNDS_PER_USER}")
    print(f"MAX_STRATEGY_PIVOTS_PER_USER={MAX_STRATEGY_PIVOTS_PER_USER}")
    print(f"REVIEW_APPROVED_DECISIONS={REVIEW_APPROVED_DECISIONS}")
    print(f"MIN_REVIEW_CONFIDENCE={MIN_REVIEW_CONFIDENCE}")
    print(f"ENABLE_QUOTA_MODE={ENABLE_QUOTA_MODE}")
    print(f"ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH={ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH}")
    print(f"INVENTORY_CANDIDATES_PER_USER={INVENTORY_CANDIDATES_PER_USER}")
    print(f"SAFE_FALLBACK_MIN_GRADE={SAFE_FALLBACK_MIN_GRADE}")
    print(f"DISCOVERY_PENDING_MIN_GRADE={DISCOVERY_PENDING_MIN_GRADE}")
    print(f"ENABLE_LINKEDIN_PREFETCH={ENABLE_LINKEDIN_PREFETCH} LINKEDIN_MAX_ITEMS={LINKEDIN_MAX_ITEMS}")
    print(f"ENABLE_HIRING_CAFE_PREFETCH={ENABLE_HIRING_CAFE_PREFETCH} HIRING_CAFE_MAX_ITEMS={HIRING_CAFE_MAX_ITEMS} HIRING_CAFE_BATCH_SIZE={HIRING_CAFE_BATCH_SIZE}")
    print(f"ENABLE_JOBO_ATS_PREFETCH={ENABLE_JOBO_ATS_PREFETCH} JOBO_ATS_MAX_ITEMS={JOBO_ATS_MAX_ITEMS} JOBO_LOCAL_SCORE_MIN={JOBO_LOCAL_SCORE_MIN}")
    if NO_GPT_MODE:
        print(f"NO-GPT limits: JOBO_KEYWORDS={NOGPT_JOBO_KEYWORDS}, JOBO_MAX_CALLS_PER_USER={NOGPT_JOBO_MAX_CALLS_PER_USER}, US_EXTRA_CALLS={NOGPT_JOBO_US_EXTRA_CALLS}, HC_KEYWORDS={NOGPT_HIRING_CAFE_KEYWORDS}")

    users = get_eligible_paid_users()

    print("\n\n============================================================")
    print("ELIGIBLE USERS SUMMARY")
    print("============================================================")
    print(f"Eligible paid users with active automation: {len(users)}")

    if (selected_email_filter_active() or SINGLE_USER_UID) and not users:
        print("\nNo eligible user matched the selected-user filter.")
        print(f"Requested emails: {sorted(SINGLE_USER_EMAILS)}")
        print(f"Requested uid: {SINGLE_USER_UID}")
        print("Check spelling and active automation. Paid status is trusted from /users/paid when TRUST_USERS_PAID_ENDPOINT=True.")
        return

    # Skip the expensive cluster prefetch when targeting a single user —
    # the shared inventory only pays off across many users.
    single_user_mode = bool(selected_email_filter_active() or SINGLE_USER_UID)
    if ENABLE_AUTOMATION_TITLE_CLUSTER_PREFETCH and users and not single_user_mode:
        initialize_daily_cluster_prefetch(users)

    all_results = []

    # Load previous run to carry dead companies/domains/feedback into today's Round 1.
    previous_run_data = load_previous_run_log()
    previous_run_by_uid = build_previous_run_results_by_uid(previous_run_data)

    if previous_run_by_uid:
        print(f"Cross-run learning: {len(previous_run_by_uid)} user(s) with prior data")
        for _uid, r in previous_run_by_uid.items():
            n_companies = len(r.get("rejected_companies") or [])
            n_domains = len(r.get("rejected_domains") or [])
            n_feedback = len(r.get("failed_batch_feedback") or [])
            print(f"  {r.get('email', '?')}: {n_companies} dead companies, {n_domains} dead domains, {n_feedback} feedback entries")
    else:
        print("No previous run log found — starting fresh.")

    # Round 1: every eligible user gets quota-mode batches.
    first_round_results = run_round(
        users=users,
        round_number=1,
        max_batches_for_round=FIRST_ROUND_BATCHES,
        round_mode="first_round",
        previous_results_by_uid=previous_run_by_uid,
        accumulated_results=all_results,
    )
    save_run_log(all_results)

    first_results_by_uid = {}
    for result in first_round_results:
        user_profile = result.get("user_profile") or {}
        user = result.get("user") or {}
        uid = user_profile.get("uid") or user.get("uid")
        if uid:
            first_results_by_uid[uid] = result

    # Round 2: no input prompt. Only users below threshold AND with enough
    # Round-1 supply signal get another cheaper adaptive pass.
    second_round_users = get_second_round_users_from_first_round(
        users=users,
        first_results_by_uid=first_results_by_uid,
        threshold=MIN_JOBS_BEFORE_SECOND_ROUND,
    )

    # Compute per-source success rates from Round 1 to adapt Round 2 fetch budgets.
    _r1_source_rates = compute_source_success_rates(first_round_results)
    print(f"\nRound 1 source rates:  " + "  ·  ".join(f"{s} {v:.0%}" for s, v in _r1_source_rates.items()))

    _best_source = max(_r1_source_rates, key=_r1_source_rates.get)
    _r2_source_limit_overrides = {}
    for r in first_round_results:
        _uid = (r.get("user_profile") or {}).get("uid") or (r.get("user") or {}).get("uid")
        if not _uid:
            continue
        _overrides = {}
        _max_rate = max(_r1_source_rates.values()) if _r1_source_rates else 0
        if _r1_source_rates.get("linkedin", 0) == _max_rate and _r1_source_rates["linkedin"] > 0:
            _overrides["linkedin"] = max(10, int(LINKEDIN_MAX_ITEMS * 1.5))
        elif _r1_source_rates.get("linkedin", 0) == 0:
            _overrides["linkedin"] = max(10, int(LINKEDIN_MAX_ITEMS * 0.5))
        if _r1_source_rates.get("hc", 0) == _max_rate and _r1_source_rates["hc"] > 0:
            _overrides["hc"] = max(10, int(HIRING_CAFE_MAX_ITEMS * 1.5))
        elif _r1_source_rates.get("hc", 0) == 0:
            _overrides["hc"] = max(10, int(HIRING_CAFE_MAX_ITEMS * 0.5))
        if _r1_source_rates.get("jobo", 0) == _max_rate and _r1_source_rates["jobo"] > 0:
            _overrides["jobo"] = max(10, int(JOBO_ATS_MAX_ITEMS * 1.5))
        elif _r1_source_rates.get("jobo", 0) == 0:
            _overrides["jobo"] = max(10, int(JOBO_ATS_MAX_ITEMS * 0.5))
        if _overrides:
            _r2_source_limit_overrides[_uid] = _overrides

    if _best_source in ("linkedin", "hc", "jobo") and _r1_source_rates[_best_source] > 0:
        print(f"Round 2 budget adjustment:  {_best_source.upper()} +50%  (best performer at {_r1_source_rates[_best_source]:.0%})")

    if second_round_users:
        print(
            f"\nStarting automatic second round for {len(second_round_users)} users "
            f"below {MIN_JOBS_BEFORE_SECOND_ROUND} jobs."
        )
        second_round_results = run_round(
            users=second_round_users,
            round_number=2,
            max_batches_for_round=SECOND_ROUND_BATCHES,
            round_mode="second_round",
            previous_results_by_uid=first_results_by_uid,
            accumulated_results=all_results,
            source_limit_overrides=_r2_source_limit_overrides,
        )
        save_run_log(all_results)
    else:
        print(
            f"\nNo second round needed. No eligible user is below "
            f"{MIN_JOBS_BEFORE_SECOND_ROUND} plan-status/legacy jobs today."
        )

    # Round 3 (minimum viable): only users still below MIN_ACCEPTABLE_JOBS_PER_USER.
    latest_results_by_uid = {}
    for r in all_results:
        uid = (r.get("user_profile") or {}).get("uid") or (r.get("user") or {}).get("uid")
        if uid:
            latest_results_by_uid[uid] = r

    minimum_viable_users = [
        user for user in users
        if latest_results_by_uid.get(user.get("uid"), {}).get("pending_today_after_estimate", 0) < MIN_ACCEPTABLE_JOBS_PER_USER
    ]

    if minimum_viable_users:
        print(f"\nStarting minimum viable round for {len(minimum_viable_users)} users below {MIN_ACCEPTABLE_JOBS_PER_USER} jobs.")
        minimum_viable_results = run_round(
            users=minimum_viable_users,
            round_number=3,
            max_batches_for_round=MINIMUM_VIABLE_ROUND_BATCHES,
            round_mode="minimum_viable",
            previous_results_by_uid=latest_results_by_uid,
            minimum_viable_mode=True,
            accumulated_results=all_results,
        )
        save_run_log(all_results)
    else:
        print(f"\nNo minimum viable round needed — all users at or above {MIN_ACCEPTABLE_JOBS_PER_USER} jobs.")

    # Send one daily report per user after ALL rounds are complete.
    # Merge jobs_added across rounds; use the last round's result for metadata.
    results_by_uid = {}
    for r in all_results:
        uid = (r.get("user_profile") or {}).get("uid") or (r.get("user") or {}).get("uid")
        if not uid:
            continue
        if uid not in results_by_uid:
            results_by_uid[uid] = r.copy()
            results_by_uid[uid]["jobs_added"] = list(r.get("jobs_added") or [])
        else:
            # Merge jobs from later rounds (deduplicate by job_url).
            existing_urls = {j.get("job_url") for j in results_by_uid[uid]["jobs_added"]}
            for j in (r.get("jobs_added") or []):
                if j.get("job_url") not in existing_urls:
                    results_by_uid[uid]["jobs_added"].append(j)
                    existing_urls.add(j.get("job_url"))
            # Keep latest round's metadata (strategy pivots, estimates).
            for key in ("strategy_pivots", "strategy_prompt_patch", "pending_today_after_estimate",
                        "needs_more", "failed_batch_feedback"):
                if r.get(key) is not None:
                    results_by_uid[uid][key] = r[key]
            # Accumulate round_metrics across rounds rather than replacing —
            # cost/source counters must sum across all rounds to be accurate.
            if r.get("round_metrics"):
                _existing = results_by_uid[uid].get("round_metrics") or {}
                _new = r["round_metrics"]
                _merged_rm = dict(_existing)
                for _k, _v in _new.items():
                    if isinstance(_v, (int, float)) and isinstance(_existing.get(_k), (int, float)):
                        _merged_rm[_k] = _existing[_k] + _v
                    elif isinstance(_v, dict) and isinstance(_existing.get(_k), dict):
                        # Nested dict (source_funnel, cost_breakdown) — sum numeric leaves
                        _merged_sub = dict(_existing[_k])
                        for _sk, _sv in _v.items():
                            if isinstance(_sv, (int, float)) and isinstance(_merged_sub.get(_sk), (int, float)):
                                _merged_sub[_sk] = _merged_sub[_sk] + _sv
                            elif isinstance(_sv, dict) and isinstance(_merged_sub.get(_sk), dict):
                                _merged_sub[_sk] = {
                                    _ik: _merged_sub[_sk].get(_ik, 0) + _iv
                                    if isinstance(_iv, (int, float)) else _iv
                                    for _ik, _iv in _sv.items()
                                }
                            else:
                                _merged_sub[_sk] = _sv
                        _merged_rm[_k] = _merged_sub
                    else:
                        _merged_rm[_k] = _v
                # Recompute total estimated cost from merged breakdown
                _bd = _merged_rm.get("cost_breakdown", {})
                if _bd:
                    _merged_rm["estimated_cost_usd"] = round(sum(_bd.values()), 4)
                results_by_uid[uid]["round_metrics"] = _merged_rm

    # Slack team summary — only for full runs, not single-user tests.
    if not (SINGLE_USER_EMAIL or SINGLE_USER_EMAILS or SINGLE_USER_UID):
        send_slack_run_report(results_by_uid)

    total_new = sum(len(v.get("jobs_added") or []) for v in results_by_uid.values())
    total_rejected = sum(len(r.get("jobs_rejected_by_review") or []) for r in all_results)

    # Write run_costs JSON
    import datetime as _dt
    _run_ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_date = _dt.datetime.now().strftime("%Y-%m-%d")
    _cost_report_users = []
    _total_cost = 0.0
    for _uid, _merged in results_by_uid.items():
        _up = _merged.get("user_profile") or {}
        _u = _merged.get("user") or {}
        _rm = _merged.get("round_metrics") or {}
        _funnel = _rm.get("source_funnel", {})
        _jobs_by_source = {src: counts.get("added", 0) for src, counts in _funnel.items()}
        _cost = _rm.get("estimated_cost_usd", 0.0)
        _total_cost += _cost
        _cost_report_users.append({
            "uid": _uid,
            "email": _up.get("email") or _u.get("email") or "",
            "name": _up.get("displayName") or _u.get("displayName") or _uid,
            "jobs_added": len(_merged.get("jobs_added") or []),
            "jobs_by_source": _jobs_by_source,
            "estimated_cost_usd": _cost,
            "cost_breakdown": _rm.get("cost_breakdown", {}),
            "source_funnel": _funnel,
        })
    _cost_payload = {
        "run_date": _run_date,
        "run_timestamp": _run_ts,
        "total_estimated_cost_usd": round(_total_cost, 4),
        "source_rates": _r1_source_rates,
        "users": _cost_report_users,
    }
    try:
        import os as _os
        _os.makedirs("run_logs", exist_ok=True)
        _cost_file = f"run_logs/run_costs_{_run_ts}.json"
        with open(_cost_file, "w") as _f:
            json.dump(_cost_payload, _f, indent=2)
        print(f"\nCost report saved: {_cost_file}")
    except Exception as _e:
        print(f"WARNING: could not write cost report: {_e}")

    print("\n\n============================================================")
    print("RUN COMPLETE")
    print("============================================================")

    for uid, merged in results_by_uid.items():
        user_profile = merged.get("user_profile") or {}
        user = merged.get("user") or {}
        name = user_profile.get("displayName") or user.get("displayName") or uid
        email = user_profile.get("email") or user.get("email") or ""
        new_jobs = len(merged.get("jobs_added") or [])
        pending = merged.get("pending_today_after_estimate", 0)
        rm = merged.get("round_metrics") or {}
        funnel = rm.get("source_funnel", {})

        if pending >= TARGET_JOBS_PER_USER:
            icon = "✅"
        elif pending >= MIN_ACCEPTABLE_JOBS_PER_USER:
            icon = "⚠️ "
        else:
            icon = "🔴"

        src_parts = "  ·  ".join(
            f"{s.upper()} {counts.get('added', 0)}"
            for s, counts in funnel.items()
            if counts.get("added", 0) > 0
        )
        src_str = f"  [{src_parts}]" if src_parts else ""
        print(f"{icon} {name} ({email})  +{new_jobs} jobs  ({pending}/{TARGET_JOBS_PER_USER} in queue){src_str}")

    print()
    print(f"Users: {len(results_by_uid)}  ·  Jobs added: {total_new}  ·  Rejected: {total_rejected}")
    if DRY_RUN:
        print("DRY RUN — nothing was actually posted")


if __name__ == "__main__":
    main()

