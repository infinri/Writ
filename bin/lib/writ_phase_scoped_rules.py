#!/usr/bin/env python3
"""Phase-scoped rule-id selection shared by the RAG hooks (Wave-3 DRY).

writ-read-rag.sh, writ-rag-inject.sh and writ-posttool-rag.sh each need the
rule-id list scoped to the session's current phase: the current phase's bucket
from ``loaded_rule_ids_by_phase`` when both it and ``current_phase`` are
populated, otherwise ``rule_ids_since_compaction`` once the session has
compacted, else the flat ``loaded_rule_ids``. This module is the single source
of that selection.

Two entry points:
  * ``phase_scoped_ids(cache)`` -- the pure selection, imported by
    writ-posttool-rag.sh (which fuses it into one python3 spawn that also emits
    budget and mode).
  * ``__main__`` -- reads a cache dict as JSON on stdin, prints the selected
    list as JSON on stdout. There is deliberately NO try/except: malformed
    input raises and the process exits non-zero, which is what lets the
    callers' shell guard (``2>/dev/null || echo '[]'``) degrade to ``[]``.
    writ-read-rag.sh and writ-rag-inject.sh use this path.

stdlib-only; no writ-package import (it runs as a bare ``python3 file.py`` from
inside a hook).
"""
import json
import sys


def phase_scoped_ids(cache: dict) -> list:
    by_phase = cache.get('loaded_rule_ids_by_phase', {})
    current_phase = cache.get('current_phase', '')
    if by_phase and current_phase:
        return by_phase.get(current_phase, [])
    # Program item 1c: outside a work phase, the rules shown since the last compaction;
    # None until the session first compacts, and then the flat list as before. Mirrors
    # writ.session.injection_state.retrieval_exclude_ids (parity test in
    # tests/test_compaction_clears_shown_exclusion.py).
    since = cache.get('rule_ids_since_compaction')
    if isinstance(since, list):
        return since
    return cache.get('loaded_rule_ids', [])


if __name__ == "__main__":
    print(json.dumps(phase_scoped_ids(json.load(sys.stdin))))
