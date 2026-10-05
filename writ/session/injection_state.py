"""Per-session record of which collapsible rules were already shown in full.

The always-on block and a mode's floor methodology repeat every turn. They render in full
once per EPOCH and as one pointer line per rule after that. An epoch is the pair
(compaction_epoch, current_phase): cmd_reset_after_compaction bumps the counter and every
phase write changes the phase, so both boundaries reset the record without any of the nine
phase writers having to know this module exists. Mode-independent by construction: outside
work mode current_phase stays None and only the counter moves, the same boundary
retrieval_exclude_ids below uses for the ranked exclusion (item 1c).
"""
from __future__ import annotations

COLLAPSIBLE_SECTIONS = ("always_on", "floor")


def injection_epoch(cache: dict) -> str:
    try:
        counter = int(cache.get("compaction_epoch") or 0)
    except (TypeError, ValueError):
        counter = 0
    return f"{counter}|{cache.get('current_phase') or ''}"


def shown_ids(cache: dict, section: str) -> set[str]:
    """Ids shown in full in the CURRENT epoch; empty for any other epoch."""
    record = cache.get("injection_shown") or {}
    if not isinstance(record, dict) or record.get("epoch") != injection_epoch(cache):
        return set()
    return {str(i) for i in (record.get(section) or [])}


def apply_mark_shown(cache: dict, section: str, epoch: str, ids: list) -> None:
    """Union `ids` into the record. A write computed from a snapshot of a DIFFERENT epoch
    is dropped: the next turn renders those rules in full under the new epoch."""
    if section not in COLLAPSIBLE_SECTIONS:
        return
    current = injection_epoch(cache)
    if epoch != current:
        return
    record = cache.get("injection_shown")
    if not isinstance(record, dict) or record.get("epoch") != current:
        record = {"epoch": current}
    merged = set(record.get(section) or [])
    merged.update(str(i) for i in ids if i)
    record[section] = sorted(merged)
    cache["injection_shown"] = record


def retrieval_exclude_ids(cache: dict) -> list[str]:
    """The rule ids ranked and pre-write retrieval must not re-inject.

    In a work phase, that phase's bucket of loaded_rule_ids_by_phase. Outside one, the rules
    shown since the last compaction; before a session's first compaction that field is None
    and the flat loaded_rule_ids is the answer, as it always was. cmd_reset_after_compaction
    empties whichever applies (program item 1c). bin/lib/writ_phase_scoped_rules.py mirrors
    this for the hooks (stdlib-only, so it cannot import it), and
    tests/test_compaction_clears_shown_exclusion.py holds the two equal.
    """
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    current_phase = cache.get("current_phase", "")
    if by_phase and current_phase:
        return list(by_phase.get(current_phase, []))
    since = cache.get("rule_ids_since_compaction")
    if isinstance(since, list):
        return list(since)
    return list(cache.get("loaded_rule_ids", []))
