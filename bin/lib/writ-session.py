#!/usr/bin/env python3
"""Session cache helper for Writ RAG bridge hooks.

Manages per-session state (loaded rule IDs, remaining budget, context pressure)
in a temp file so hooks can deduplicate rules across turns.

Stdlib only -- no external dependencies.
"""

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# POL-6a: ensure the skill root is importable so `import writ.session.*` resolves whether
# this file is run as a script by absolute path (hooks) or loaded via spec_from_file_location
# (server/tests). The skill root is three levels above this file (bin/lib/writ-session.py).
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SKILL_ROOT not in sys.path:
    sys.path.insert(0, _SKILL_ROOT)

# POL-6c: friction logging moved to writ/session/friction.py; re-exported so the 22 callers
# and external `mod._log_friction_event` access still resolve.
from writ.session.friction import _log_friction_event  # noqa: E402


# POL-6a: budget/cost constants moved to writ/session/config.py (single source of truth).
# POL-6b: citation-bound constants (_CITATION_LOG_MAX/_CITATION_EXCERPT_MAX) moved there too.
# Re-imported here so inline functions and external `mod.<name>` access still resolve.
from writ.session.config import (  # noqa: E402
    _BUDGET_JSON,
    _budget_data,
    DEFAULT_SESSION_BUDGET,
    APPROX_TOKENS_PER_RULE_FULL,
    APPROX_TOKENS_PER_RULE_STANDARD,
    APPROX_TOKENS_PER_RULE_SUMMARY,
    DEFAULT_ALWAYS_ON_CAP,
    _CITATION_LOG_MAX,
    _CITATION_EXCERPT_MAX,
    EXT_TO_DOMAIN,
)
# POL-6b/6b-2: cache I/O lives in writ/session/cache.py and resolves WRIT_CACHE_DIR at call
# time, so these are pure re-exports (no wrapper, no facade global). CACHE_DIR is the cache
# module's import-time snapshot, re-exported for server.py's health / desync display.
from writ.session.cache import (  # noqa: E402
    CACHE_DIR,
    _cache_path,
    _read_cache,
    _write_cache,
    _migrate_command_log,
    mutate_cache,
)
# POL-6d: file-locators and the mode engine moved to their own modules; re-imported so the
# still-resident gate / approval / investigation callers and main() resolve them unchanged.
from writ.session.locators import (  # noqa: E402
    _find_debug_md,
    _find_plan_md,
)
from writ.session.mode_engine import (  # noqa: E402
    MODE_CONFIG,
    VALID_MODES,
    GATE_SEQUENCE_WORK,
    _PHASE_AFTER_GATE_WORK,
    _initial_phase_for_mode,
    _gate_sequence_for_mode,
    _source_type_for_mode,
    _gate_strictness_for_mode,
    _next_pending_gate,
    _extract_root_cause,
    _promote_root_cause_to_plan,
    _mode_set,
    _mode_switch,
    cmd_mode,
    _effective_source_type,
    _VALID_SOURCE_TYPES,
)
# POL-6e: the gate cluster moved to writ/session/gates.py; re-imported so server.py
# (/pre-write-check) and main()'s can-write / can-read-code dispatch resolve unchanged.
from writ.session.gates import (  # noqa: E402
    _CODE_EXTENSIONS,
    _parse_file_path_from_envelope,
    _load_categories,
    _glob_match,
    _matches_any,
    _log_gate_denial,
    _validate_root_cause,
    _section_body,
    _has_real_content,
    _validate_evidence_narrowing,
    _can_write_check,
    _can_read_code_check,
    cmd_can_write,
    cmd_can_read_code,
)
# POL-6f: the phase-advance / gate-validation cluster moved to writ/session/approval_workflow.py;
# re-imported so main()'s advance-phase / current-phase dispatch resolves unchanged.
from writ.session.approval_workflow import (  # noqa: E402
    _validate_citations,
    _validate_phase_a,
    _validate_test_skeletons,
    _GATE_VALIDATORS,
    cmd_advance_phase,
    cmd_current_phase,
    cmd_reopen_planning,
)
# POL-6g-1: typed citations + the token-budget command set moved to their own modules;
# re-imported so cmd_update / cmd_should_skip / cmd_format callers (main(), server.py
# /budget routes) resolve unchanged. _VALID_SOURCE_TYPES relocated to mode_engine (above).
from writ.session.citations import _append_citation  # noqa: E402
from writ.session.budget_tracking import (  # noqa: E402
    cmd_update,
    cmd_should_skip,
    _estimate_cost,
    cmd_format,
)
# POL-6g-2: the investigation engine (coverage / audit fan-out / research / lens) moved to
# writ/session/investigations.py; re-imported so main()'s INV subcommand dispatch and the
# _AUDIT_BUDGET_* / _LENS_TABLE references resolve unchanged. EXT_TO_DOMAIN relocated to
# config (shared with feedback); it arrives via the config re-import above.
from writ.session.investigations import (  # noqa: E402
    _COVERAGE_SAMPLE_MAX,
    _AUDIT_BUDGET_LOC,
    _AUDIT_BUDGET_FILES,
    _ATTENTION_ERROR_WEIGHT,
    _TRIANGULATION_MIN_DOMAINS,
    _LENS_TABLE,
    cmd_coverage,
    _examined_files,
    cmd_coverage_map,
    _parse_findings,
    cmd_record_analysis,
    cmd_synthesis_gate,
    _file_loc,
    cmd_scope_estimate,
    cmd_partition_scope,
    cmd_coverage_rollup,
    cmd_aggregate_findings,
    _independent_domain,
    cmd_triangulation_gate,
    cmd_staleness_check,
    cmd_lens,
)
# POL-6g-3: the final command clusters moved to their own modules; re-imported so main()'s
# subcommand dispatch and server.py's compaction routes resolve unchanged.
from writ.session.violations import (  # noqa: E402
    MAX_CYCLES_BEFORE_ESCALATION,
    cmd_add_pending_violation,
    cmd_clear_pending_violations,
    cmd_invalidate_gate,
    cmd_check_escalation,
    cmd_pending_violations,
)
from writ.session.session_lifecycle import (  # noqa: E402
    cmd_read,
    cmd_clear_rules_for_compaction,
    cmd_reset_after_compaction,
)
from writ.session.feedback import (  # noqa: E402
    WRIT_FEEDBACK_URL,
    cmd_auto_feedback,
)
# POL-6h: the friction-log metrics report moved to writ/session/metrics.py (a pure stdlib
# leaf); re-imported so main()'s `metrics` subcommand resolves unchanged.
from writ.session.metrics import cmd_metrics  # noqa: E402


def main() -> None:
    # POL-6: the facade defines only main(); command routing + per-command handlers live
    # in writ/session/cli_dispatch.py, keeping this file to a single def (B6-main).
    from writ.session.cli_dispatch import dispatch
    dispatch(sys.argv)


if __name__ == "__main__":
    main()
