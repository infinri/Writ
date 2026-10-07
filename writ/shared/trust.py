"""The one staleness rule and the one tag spelling for a rule's trust state.

Lowest layer on purpose: the ranked header, the /always-on route and both always-on
renderers share it, and writ.retrieval, writ.session and writ.server may not import each other.
"""
from datetime import date, timedelta

VERIFY_INTERVAL_DAYS_DEFAULT = 180


def is_verify_stale(last_verified, verify_interval_days, today: date) -> bool:
    try:
        verified = date.fromisoformat(last_verified)
    except (TypeError, ValueError):
        return False
    interval = VERIFY_INTERVAL_DAYS_DEFAULT if verify_interval_days is None else verify_interval_days
    return today > verified + timedelta(days=interval)


def trust_tags(entry: dict) -> list[str]:
    tags = []
    if entry.get("stale"):
        tags.append("STALE")
    if entry.get("deliberate"):
        tags.append("DELIBERATE")
    return tags
