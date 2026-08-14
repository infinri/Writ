"""Session-cache lifecycle and introspection commands.

POL-6g-3 extracts the cache dump (cmd_read) and the compaction-boundary commands
(cmd_clear_rules_for_compaction for PreCompact, cmd_reset_after_compaction for PostCompact)
out of bin/lib/writ-session.py. Imports only lower layers (cache, friction, config) + stdlib;
acyclic. The facade re-exports these; server.py's compaction routes resolve unchanged.
"""

import json
import sys

from writ.session.cache import _read_cache, mutate_cache
from writ.session.friction import _log_friction_event
from writ.session.config import DEFAULT_SESSION_BUDGET, APPROX_TOKENS_PER_RULE_FULL


def cmd_read(session_id: str) -> None:
    cache = _read_cache(session_id)
    json.dump(cache, sys.stdout)
    sys.stdout.write("\n")


def cmd_clear_rules_for_compaction(session_id: str) -> None:
    """Drop the now-stale loaded_rules full objects at the compaction boundary,
    keeping the IDs (for exclusion/coverage). For the PreCompact hook.

    NOTE: the session cache is a separate /tmp file, NOT part of the compacted
    conversation context, so this does not reduce what the summarizer compresses.
    It is boundary hygiene: the conversation those full objects annotated is being
    summarized away. bytes_freed is cache-file bytes, not context tokens.
    """
    with mutate_cache(session_id) as cache:
        rules = cache.get("loaded_rules", [])
        rules_cleared = len(rules)
        bytes_freed = rules_cleared * APPROX_TOKENS_PER_RULE_FULL  # cache-file bytes, not context tokens
        cache["loaded_rules"] = []
    _log_friction_event(
        session_id, cache.get("mode"), "pre_compaction",
        rules_cleared=rules_cleared, bytes_freed=bytes_freed,
    )
    result = {"rules_cleared": rules_cleared, "bytes_freed": bytes_freed}
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def cmd_reset_after_compaction(session_id: str) -> None:
    """Clear current phase's exclusion list and reset budget. For PostCompact hook.

    Also QUEUES the verify-discipline directive (post_compact_pending). The PostCompact hook
    cannot deliver it: CC's hook-output validator rejects a PostCompact hookSpecificOutput
    reply outright ("(root): Invalid input", observed 2026-08-14), so the text is emitted by
    the next writ-rag-inject.sh UserPromptSubmit, which clears the flag. Queueing here rather
    than in the hook costs zero extra spawns (the hook already makes this call) and keeps the
    daemon route POST /session/{id}/reset-after-compaction on the same path as the CLI.
    """
    with mutate_cache(session_id) as cache:
        current_phase = cache.get("current_phase", "unknown")
        by_phase = cache.get("loaded_rule_ids_by_phase", {})
        cleared = list(by_phase.get(current_phase, []))
        by_phase[current_phase] = []
        cache["loaded_rule_ids_by_phase"] = by_phase
        cache["remaining_budget"] = DEFAULT_SESSION_BUDGET
        # Clear sticky rules preference (stale after compaction)
        cache["last_injected_rule_ids"] = []
        cache["post_compact_pending"] = True
    _log_friction_event(
        session_id, cache.get("mode"), "post_compaction",
        rules_cleared=cleared, budget_reset=True, phase=current_phase,
    )
    # mode + phase are returned so the PostCompact hook can re-state the
    # agent's workflow position (the next rag-inject only fires on the next
    # user prompt; an autonomously-resuming agent would otherwise have none).
    result = {
        "rules_cleared": cleared,
        "budget_reset": True,
        "mode": cache.get("mode"),
        "phase": current_phase,
    }
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
