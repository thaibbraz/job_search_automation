"""Backfill for jobbyo-job-crawler's boardsLinks/ bucket, run once per
script_job_search deploy.

Feeds every job/discovery URL this project has ever collected into
company_ingestion.py's board-link writer, so its ATS pattern coverage
applies retroactively, not just to runs from today on. Safe to re-run
(dedup-checked against what's already in GCS, only ever adds).

Also reads jobbyo-job-crawler's published list of ATS it actually has a
crawler for (boardsLinks/_registered_ats.json, see
write_registered_ats_manifest in that repo's main_vm.py) and writes
boardsLinks/_onboarding_status.md as a checklist (patched in place) marking
which of those already have companies seeded -- so it is visible at a
glance which ATS still have nothing, without needing a live external
lookup to close that gap.

Why this lives here and not in jobbyo-job-crawler: that repo's own
utils/backfill_board_links.py assumed it could read this project's
run_logs/ and jobo_discovery_log/ directly off the local filesystem, but it
runs on a different production host (jobbyo-crawler-prod) than this
project does (jobbyo-search-server) -- so it always found zero files there.
This runs the same idea from the correct side, where the files already are.

Usage: python3 backfill_boardlinks_from_history.py [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import company_ingestion

ROOT = Path(__file__).parent

REGISTERED_ATS_MANIFEST_PATH = f"{company_ingestion.PREFIX_TO_BOARDLINKS}/_registered_ats.json"
ONBOARDING_STATUS_PATH = f"{company_ingestion.PREFIX_TO_BOARDLINKS}/_onboarding_status.md"
_STATUS_ROW_RE = re.compile(r"^\|\s*([a-zA-Z_0-9]+)\s*\|\s*(✅|⬜)\s*\|\s*$")


def known_ats_with_companies(client):
    """ATS names that already have at least one boardsLinks/ file in GCS."""
    bucket = client.bucket(company_ingestion.BUCKET_NAME)
    top_level = bucket.list_blobs(prefix=f"{company_ingestion.PREFIX_TO_BOARDLINKS}/", delimiter="/")
    list(top_level)  # force iteration so .prefixes is populated
    return {p.rstrip("/").split("/")[-1] for p in top_level.prefixes}


def read_registered_ats(client):
    """The list of ATS jobbyo-job-crawler actually has a crawler for, per
    the manifest it publishes on every run (see write_registered_ats_manifest
    in main_vm.py) -- the real "master list" for the checklist, since a
    platform with no crawler yet would never get scanned even if seeded.
    Falls back to company_ingestion's own pattern list (a smaller,
    hand-maintained subset) if the manifest hasn't been published yet or
    can't be read, so this never hard-fails on a fresh setup.
    """
    try:
        bucket = client.bucket(company_ingestion.BUCKET_NAME)
        blob = bucket.blob(REGISTERED_ATS_MANIFEST_PATH)
        if blob.exists():
            manifest = json.loads(blob.download_as_text())
            ats = manifest.get("ats") or []
            if ats:
                return sorted(set(ats))
    except Exception as e:
        print(f"  could not read registered ATS manifest, falling back to pattern list: {e}")
    return sorted({name for name, _pattern, _template in company_ingestion._ATS_PATTERNS})


def update_onboarding_status(client, all_ats, seeded_ats):
    """Patch the boardsLinks/_onboarding_status.md checklist in place: mark
    only the ATS(s) just seeded this run as done, add any ATS the checklist
    has never seen before as unmarked, leave every already-checked row
    untouched. Skips the write entirely if nothing changed.
    """
    bucket = client.bucket(company_ingestion.BUCKET_NAME)
    blob = bucket.blob(ONBOARDING_STATUS_PATH)
    rows = {}
    try:
        if blob.exists():
            for line in blob.download_as_text().splitlines():
                m = _STATUS_ROW_RE.match(line.strip())
                if m:
                    rows[m.group(1)] = m.group(2)
    except Exception as e:
        print(f"  could not read existing onboarding status: {e}")

    changed = False
    for ats in sorted(all_ats):
        if ats not in rows:
            rows[ats] = "⬜"
            changed = True
    for ats in seeded_ats:
        if rows.get(ats) != "✅":
            rows[ats] = "✅"
            changed = True

    if not changed:
        return

    lines = ["# boardsLinks onboarding status", "", "| ATS | Seeded |", "|---|---|"]
    lines += [f"| {ats} | {rows[ats]} |" for ats in sorted(rows)]
    try:
        blob.upload_from_string("\n".join(lines))
    except Exception as e:
        print(f"  could not write onboarding status: {e}")


def iter_run_log_urls():
    run_files = sorted(ROOT.glob("run_logs/job_run_*.json"))
    print(f"Scanning {len(run_files)} run_logs files...")
    for i, path in enumerate(run_files, 1):
        if i % 500 == 0:
            print(f"  ...{i}/{len(run_files)}")
        try:
            with open(path, encoding="utf-8") as f:
                runs = json.load(f)
        except Exception as e:
            print(f"  could not parse {path.name}: {e}")
            continue
        for user_run in runs:
            for job in user_run.get("jobs_added", []) or []:
                url = (job.get("job_url") or "").strip()
                if url:
                    yield url


def iter_jobo_discovery_urls():
    log_files = sorted(ROOT.glob("jobo_discovery_log/*.jsonl"))
    print(f"Scanning {len(log_files)} jobo_discovery_log files...")
    for path in log_files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    url = (entry.get("listing_url") or "").strip()
                    if url:
                        yield url
        except Exception as e:
            print(f"  could not read {path.name}: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Classify and report counts only, do not write to GCS")
    args = parser.parse_args()

    urls = set(iter_run_log_urls()) | set(iter_jobo_discovery_urls())
    print(f"{len(urls)} distinct historical URLs collected")

    if args.dry_run:
        by_ats = {}
        unmatched = 0
        for url in urls:
            found = company_ingestion.extract_board_link(url)
            if found:
                by_ats.setdefault(found[0], set()).add(found[1])
            else:
                unmatched += 1
        for ats, links in sorted(by_ats.items()):
            print(f"  {ats}: {len(links)} distinct board(s)")
        print(f"{unmatched} URLs did not match any known ATS pattern")
    else:
        jobs = [{"job_url": u} for u in urls]
        added = company_ingestion.ingest_new_companies(jobs)
        total = sum(added.values())
        print(f"Added {total} new board link(s) across {len(added)} ATS(s): {added}")

    # Checklist -- shows which registered ATS still have zero companies,
    # even though this pass can only fix the ones that happened to appear in
    # past search history. Anything still unmarked after this needs a
    # separate source (a manual one-off, or a future organic company_
    # ingestion.py capture from a live search).
    client = company_ingestion._get_storage_client()
    if client is None:
        print("  no usable GCS credentials -- skipping onboarding checklist")
        return

    all_ats = read_registered_ats(client)
    existing_ats = known_ats_with_companies(client)
    still_empty = [a for a in all_ats if a not in existing_ats]
    print(f"{len(all_ats)} registered ATS total, {len(still_empty)} still have zero companies: {still_empty}")

    if not args.dry_run:
        update_onboarding_status(client, all_ats, existing_ats)


if __name__ == "__main__":
    sys.exit(main())
