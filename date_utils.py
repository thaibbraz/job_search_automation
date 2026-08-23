"""Shared UTC timestamp parsing for job addedAt/createdAt fields.

Some upstream writers (the auto-apply bot, in particular) save these
timestamps without a timezone marker. There's no way to know after the fact
what timezone they meant, so every reader in this codebase makes the same
explicit assumption: a naive timestamp is UTC. That matches how every writer
we control actually behaves -- these are all Cloud Run services, which
default to a UTC system clock, so a naive datetime.now() on those hosts
already IS UTC wall-clock time, just without the marker.

Before this module, send_jobbyo.py, approve_jobs.py, and api.py each had
their own slightly different parsing logic, so a naive timestamp could be
read differently depending on which file happened to touch it. Centralizing
the assumption here means every caller reads a given timestamp the same way.
"""

from datetime import datetime, timezone

_FALLBACK_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%a, %d %b %Y %H:%M:%S %Z",
]


def parse_utc_timestamp(value):
    """Parse a job's addedAt/createdAt into an aware UTC datetime, or None.

    Naive input (no offset, no "Z") is assumed to already be UTC -- see the
    module docstring for why.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso_text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    for fmt in _FALLBACK_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def is_same_utc_date(value, reference_date):
    """True if value's UTC calendar date matches reference_date (a date)."""
    dt = parse_utc_timestamp(value)
    return dt is not None and dt.date() == reference_date
