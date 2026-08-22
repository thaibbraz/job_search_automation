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
"""

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


def ingest_new_companies(jobs):
    """Best-effort: pull board links out of this run's jobs and add any the
    crawler doesn't already have to its bucket, one .txt file per ATS. Never
    raises — a failure here should never take down a search run."""
    try:
        by_ats = {}
        for job in jobs or []:
            found = extract_board_link(job.get("job_url") or job.get("url"))
            if found:
                ats_name, board_url = found
                by_ats.setdefault(ats_name, set()).add(board_url)

        if not by_ats:
            return

        client = _get_storage_client()
        if client is None:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        for ats_name, links in by_ats.items():
            prefix = f"{PREFIX_TO_BOARDLINKS}/{ats_name}/"
            existing = _get_existing_links(client, BUCKET_NAME, prefix)
            new_links = sorted(links - existing)
            if not new_links:
                continue
            blob_name = f"{prefix}{timestamp}.txt"
            client.bucket(BUCKET_NAME).blob(blob_name).upload_from_string("\n".join(new_links))
            print(f"  company_ingestion: added {len(new_links)} new {ats_name} board link(s) -> {blob_name}")
    except Exception as e:
        print(f"  company_ingestion: skipped due to error: {e}")
