"""company_ingestion.py — feed newly-discovered companies back into
jobbyo-job-crawler's board-links collection.

Ported from jobbyo-job-crawler/main_vm.py's save_board_links_to_s3 /
get_existing_links_from_s3, writing to the exact same GCS bucket + prefix
("boardsLinks/{ats_name}/...") so the crawler picks these up on its next
run same as any board link it found itself. This only adds — it never
reads job data back into the search pipeline.

Requires GOOGLE_APPLICATION_CREDENTIALS pointing at a service account key
with Storage write access to the bucket; if that's not configured, ingestion
is skipped (logged, not fatal — this must never break a search run).

No longer posts to Slack directly (this runs many times a day -- every
scheduled pass, plus once per new paid signup). New links are recorded
into meta/company_ingestion_today.json in the same bucket instead;
jobbyo-job-crawler's daily crawler-numbers report reads that once a day
and folds it into its one post.
"""

import json
import os
import re
from datetime import datetime

BUCKET_NAME = os.getenv("JOBBYO_DATA_BUCKET", "jobbyo-jobs-dev")
PREFIX_TO_BOARDLINKS = "boardsLinks"

# (ats_name, url regex, board-URL template) — ats_name matches
# jobbyo-job-crawler's src/URLs.py ATS_TO_URL keys so the crawler recognizes
# these the same way it would its own finds. Not exhaustive — only the ATS
# types that show up often in Apify/Hiring.cafe results; anything else is
# skipped rather than guessed at.
_ATS_PATTERNS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/]+)/", re.I), "https://jobs.ashbyhq.com/{0}"),
    ("eu_lever", re.compile(r"jobs\.eu\.lever\.co/([^/]+)/", re.I), "https://jobs.eu.lever.co/{0}"),
    ("lever", re.compile(r"jobs\.lever\.co/([^/]+)/", re.I), "https://jobs.lever.co/{0}"),
    ("grnhse", re.compile(r"job-boards\.greenhouse\.io/([^/]+)/", re.I), "https://job-boards.greenhouse.io/{0}"),
    ("grnhse", re.compile(r"boards\.greenhouse\.io/([^/]+)/", re.I), "https://boards.greenhouse.io/{0}"),
    ("workable", re.compile(r"apply\.workable\.com/([^/]+)/", re.I), "https://apply.workable.com/{0}"),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([^/]+)/", re.I), "https://careers.smartrecruiters.com/{0}"),
    ("bamboohr", re.compile(r"([a-z0-9\-]+)\.bamboohr\.com/careers", re.I), "https://{0}.bamboohr.com/careers"),
    ("personio", re.compile(r"([a-z0-9\-]+)\.jobs\.personio\.com", re.I), "https://{0}.jobs.personio.com"),
    ("rippling", re.compile(r"ats\.rippling\.com/([^/]+)/jobs", re.I), "https://ats.rippling.com/{0}/jobs"),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([^/]+)/", re.I), "https://jobs.jobvite.com/{0}"),
    ("breezy", re.compile(r"([a-z0-9\-]+)\.breezy\.hr/", re.I), "https://{0}.breezy.hr/"),
    ("workday", re.compile(r"([a-z0-9\-]+\.wd\d+\.myworkdayjobs\.com)/([^/]+)/", re.I), "https://{0}/{1}"),
    # Added to close the "29 ATS with a crawler but zero seeded companies"
    # gap surfaced by /ats-coverage. First attempt at these (2026-08-23) was
    # guessed from jobbyo-job-crawler's ATS_TO_URL board-*listing*-page
    # patterns without checking a single real Jobo apply_url -- several
    # turned out wrong, since an individual apply link often has a very
    # different URL shape than the board root (query-param IDs instead of
    # path segments, a fixed non-tenant subdomain, etc). Every pattern below
    # is verified against real apply_url samples pulled from Jobo
    # (sources=<ats>) on 2026-08-23/24, not guessed.
    ("gohire", re.compile(r"jobs\.gohire\.io/([^/]+)/", re.I), "https://jobs.gohire.io/{0}/"),
    # allhires, amris, gr8people, peoplebank, peoplehr: Jobo returned zero
    # samples for all five as of 2026-08-23, so these patterns are still
    # unverified guesses -- left in (harmless if never exercised) rather
    # than dropped, but treat any future match from these with suspicion
    # until it can be checked against a real URL.
    ("allhires", re.compile(r"([a-z0-9\-]+)\.allhires\.com/app/", re.I), "https://{0}.allhires.com/app/"),
    ("amris", re.compile(r"([a-z0-9\-]+)\.amris-wizard-proxy\.com/vacancyList", re.I), "https://{0}.amris-wizard-proxy.com/vacancyList.php"),
    ("applicantpro", re.compile(r"([a-z0-9\-]+)\.applicantpro\.com/jobs/", re.I), "https://{0}.applicantpro.com/jobs/"),
    # Real apply_url: app.careerpuck.com/job-board/{company}/job/{id} --
    # different subdomain (app, not api) and path (job-board singular, no
    # /v1/public/ prefix) than guessed.
    ("careerpuck", re.compile(r"app\.careerpuck\.com/job-board/([^/]+)/", re.I), "https://app.careerpuck.com/job-board/{0}/"),
    # Real apply_url path is /applying.cfm, not /main.cfm as guessed. The
    # segment before it isn't reliably a company slug (seen "rpc" as well as
    # real company names), so this will occasionally write a bogus board --
    # better than the previous pattern matching nothing at all.
    ("cvmail", re.compile(r"([a-z0-9\-]+)\.cvmailuk\.com/([^/]+)/applying\.cfm", re.I), "https://{0}.cvmailuk.com/{1}/applying.cfm"),
    ("cvmail", re.compile(r"([a-z0-9\-]+)\.cvmail\.net/([^/]+)/applying\.cfm", re.I), "https://{0}.cvmail.net/{1}/applying.cfm"),
    ("gem", re.compile(r"jobs\.gem\.com/([^/]+)", re.I), "https://jobs.gem.com/{0}"),
    ("gr8people", re.compile(r"([a-z0-9\-]+)\.gr8people\.com/jobs", re.I), "https://{0}.gr8people.com/jobs"),
    # Real apply_url: recruit.hirebridge.com/v3/application/AppLink.aspx?cid=X&jid=Y
    # -- company id is a query param (cid), not a path segment as guessed;
    # written back into the board-listing URL shape the crawler expects.
    ("hirebridge", re.compile(r"hirebridge\.com/v3/application/AppLink\.aspx\?(?:[^&]*&)*cid=(\d+)", re.I), "https://recruit.hirebridge.com/v3/careercenter/v2/{0}"),
    # hiringthing dropped entirely: real apply_urls are white-labeled across
    # many different reseller domains (rippling-ats.com, oasisrecruit.com,
    # prismhr-hire.com, verahr-hiring.com, ...), not *.hiringthing.com --
    # there is no single hostname suffix to match on, same category as
    # jobbyo-job-crawler's harbour/reachats/hireful/kronos (no safe generic
    # pattern, needs an explicit ats_name per company instead).
    # Real apply_url: {company}.homerun.co/{job-slug} directly, no /jobs/
    # path segment as guessed.
    ("homerun", re.compile(r"feed\.homerun\.co/([^/]+)", re.I), "https://feed.homerun.co/{0}"),
    ("homerun", re.compile(r"([a-z0-9\-]+)\.homerun\.co/", re.I), "https://{0}.homerun.co/"),
    # hrmdirect dropped entirely: real apply_urls all go through a single
    # shared apply.hrmdirect.com host with an opaque req_id, no company
    # slug anywhere in the URL -- unlike the board-listing page (which does
    # use a per-company subdomain), so there is nothing here to extract.
    # Real apply_url: {company}.hua.hrsmart.com/hr/ats/Posting/view/{id} (or
    # the .mua.hrdepartment.com variant) -- different path than guessed
    # (/hr/ats/Posting/view/, not /{dept}/ats/JobSearch/viewAll).
    ("hrsmart", re.compile(r"([a-z0-9\-]+)\.hua\.hrsmart\.com/hr/ats/Posting", re.I), "https://{0}.hua.hrsmart.com/hr/ats/Posting/view/1"),
    ("hrsmart", re.compile(r"([a-z0-9\-]+)\.mua\.hrdepartment\.com/hr/ats/Posting", re.I), "https://{0}.mua.hrdepartment.com/hr/ats/Posting/view/1"),
    # Real apply_url: www.joblinkapply.com/Joblink/{tenant_id}/Job/Index/{id}
    # -- fixed "www" subdomain (not per-company as guessed), tenant id is a
    # path segment, not readable as a company name but still a valid,
    # distinct identifier.
    ("joblinkapply", re.compile(r"joblinkapply\.com/Joblink/(\d+)/", re.I), "https://www.joblinkapply.com/Joblink/{0}/"),
    ("joincom", re.compile(r"join\.com/companies/([^/]+)", re.I), "https://join.com/companies/{0}"),
    ("kula", re.compile(r"careers\.kula\.ai/([^/]+)", re.I), "https://careers.kula.ai/{0}"),
    # Real apply_url: www.careers-page.com/{company}/job/... -- fixed "www"
    # subdomain (not per-company as guessed), company is a path segment.
    ("manatal", re.compile(r"careers-page\.com/([^/]+)/job/", re.I), "https://www.careers-page.com/{0}/"),
    ("onecruiter", re.compile(r"([a-z0-9\-]+)\.onecruiter\.com/", re.I), "https://{0}.onecruiter.com/"),
    ("peopleadmin", re.compile(r"([a-z0-9\-]+)\.peopleadmin\.com/postings", re.I), "https://{0}.peopleadmin.com/postings"),
    ("peoplebank", re.compile(r"([a-z0-9\-]+)\.peoplebank\.com/pb3/corporate/([^/]+)", re.I), "https://{0}.peoplebank.com/pb3/corporate/{1}"),
    ("peoplehr", re.compile(r"([a-z0-9\-]+)\.accessacloud\.com/([^/]+)/Recruitment", re.I), "https://{0}.accessacloud.com/{1}/Recruitment/Vacancies.aspx"),
    ("recooty", re.compile(r"careerspage\.io/([^/]+)", re.I), "https://careerspage.io/{0}"),
    # recooty also runs white-labeled under jobs.recooty.com directly, seen
    # in real samples alongside careerspage.io.
    ("recooty", re.compile(r"jobs\.recooty\.com/([^/]+)/", re.I), "https://jobs.recooty.com/{0}/"),
    # recruitee: real samples show most companies on fully custom/white-label
    # domains this can't classify -- same documented, accepted limitation as
    # jobbyo-job-crawler's own ATS_TO_URL entry for recruitee ("public-facing
    # careers_url can also be on a custom domain, not matched by this
    # pattern"). This pattern is correct as far as it goes; low yield here
    # is expected, not a bug.
    ("recruitee", re.compile(r"([a-z0-9\-]+)\.recruitee\.com/(?:o|career)/", re.I), "https://{0}.recruitee.com/career/"),
    ("salesforcesites", re.compile(r"([a-z0-9\-]+)\.my\.salesforce-sites\.com/recruit", re.I), "https://{0}.my.salesforce-sites.com/recruit"),
    # zohorecruit real samples include .in and .eu tenants, not just .com as
    # guessed -- TLD is captured and preserved in the template, since a
    # hardcoded .com would write a URL that does not exist for those
    # tenants (e.g. barcodeindia.zohorecruit.in, not .com).
    ("zohorecruit", re.compile(r"([a-z0-9\-]+)\.zohorecruit\.(com|in|eu)/jobs/careers", re.I), "https://{0}.zohorecruit.{1}/jobs/careers"),
]


def extract_board_link(job_url):
    """job posting URL -> (ats_name, board_root_url), or None if it doesn't
    match a known ATS pattern."""
    if not job_url:
        return None
    for ats_name, pattern, template in _ATS_PATTERNS:
        m = pattern.search(job_url)
        if m:
            return ats_name, template.format(*m.groups())
    return None


def _get_storage_client():
    try:
        from google.cloud import storage
    except ImportError:
        print("  company_ingestion: google-cloud-storage not installed — skipping.")
        return None
    try:
        return storage.Client()
    except Exception as e:
        print(f"  company_ingestion: no usable GCS credentials — skipping ({e}).")
        return None


def _get_existing_links(client, bucket_name, prefix):
    existing = set()
    bucket = client.bucket(bucket_name)
    for blob in bucket.list_blobs(prefix=prefix):
        if not blob.name.endswith(".txt"):
            continue
        try:
            content = blob.download_as_text()
        except Exception as e:
            print(f"  company_ingestion: could not read {blob.name}: {e}")
            continue
        for line in content.strip().splitlines():
            line = line.strip()
            if line:
                existing.add(line)
    return existing


ACCUMULATOR_BLOB_NAME = "meta/company_ingestion_today.json"


def _record_for_daily_report(client, added_by_ats):
    """No longer posts to Slack directly -- send_jobbyo.py runs many times a
    day (every scheduled pass, plus once per new paid signup now that
    /run/user/subscribed actually works), so a Slack post per run here was
    firing constantly. Instead, accumulate today's counts into a small JSON
    file in the same GCS bucket jobbyo-job-crawler already reads for its own
    board-link data -- that repo's daily crawler-numbers report (once a day,
    as "Erik") reads this and folds it into that one post, then clears it.
    Best-effort; never raises, never blocks a run.
    """
    if not added_by_ats or client is None:
        return
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        blob = client.bucket(BUCKET_NAME).blob(ACCUMULATOR_BLOB_NAME)
        try:
            existing = json.loads(blob.download_as_text())
        except Exception:
            existing = {}
        if existing.get("date") != today:
            existing = {"date": today, "added_by_ats": {}}
        totals = existing.setdefault("added_by_ats", {})
        for ats, count in added_by_ats.items():
            totals[ats] = totals.get(ats, 0) + count
        blob.upload_from_string(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"  company_ingestion: daily-report accumulator update failed (non-fatal): {e}")


INGESTION_BLOB_NAME = "company_ingestion.txt"


def ingest_new_companies(jobs):
    """Best-effort: pull board links out of this run's jobs and add any the
    crawler doesn't already have to its bucket. Never raises — a failure
    here should never take down a search run.

    Writes into one stable file per ATS (INGESTION_BLOB_NAME), merging new
    links in rather than creating a new timestamped file every run -- this
    used to write a fresh {prefix}{timestamp}.txt on every call, which
    could run many times a day (every scheduled pass, plus every new paid
    signup), silently filling each ATS folder with dozens of near-empty
    files over time.

    Returns {ats_name: count_added} for whatever the caller wants to do with
    it (e.g. logging); empty dict if nothing new was found or ingestion was
    skipped.
    """
    added_by_ats = {}
    client = None
    try:
        by_ats = {}
        for job in jobs or []:
            found = extract_board_link(job.get("job_url") or job.get("url"))
            if found:
                ats_name, board_url = found
                by_ats.setdefault(ats_name, set()).add(board_url)

        if not by_ats:
            return added_by_ats

        client = _get_storage_client()
        if client is None:
            return added_by_ats

        for ats_name, links in by_ats.items():
            prefix = f"{PREFIX_TO_BOARDLINKS}/{ats_name}/"
            existing = _get_existing_links(client, BUCKET_NAME, prefix)
            new_links = sorted(links - existing)
            if not new_links:
                continue
            blob = client.bucket(BUCKET_NAME).blob(f"{prefix}{INGESTION_BLOB_NAME}")
            try:
                current = blob.download_as_text()
                current_links = {l.strip() for l in current.splitlines() if l.strip()}
            except Exception:
                current_links = set()
            merged = sorted(current_links | set(new_links))
            blob.upload_from_string("\n".join(merged))
            print(
                f"  company_ingestion: added {len(new_links)} new {ats_name} board link(s) "
                f"-> {prefix}{INGESTION_BLOB_NAME} ({len(merged)} total)"
            )
            added_by_ats[ats_name] = len(new_links)
    except Exception as e:
        print(f"  company_ingestion: skipped due to error: {e}")

    _record_for_daily_report(client, added_by_ats)
    return added_by_ats
