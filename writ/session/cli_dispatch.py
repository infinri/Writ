"""CLI command dispatch for the writ-session facade.

POL-6 keeps the facade (bin/lib/writ-session.py) to a single main() definition: all
command logic lives in the writ.session package. This module holds the routing tables and
per-command arg-parsing handlers; the facade's main() delegates to dispatch(). The cmd_*
implementations are imported from their owning package modules (the same set the facade
re-exports for server.py and other importers).

B6-main: the commands that take a single <session_id> arg collapse to the _SIMPLE_COMMANDS
(handler, usage) table; the non-uniform commands (custom arg parsing, sub-subcommands,
exit-code translation) each get a _cli_* handler routed by _COMPLEX_COMMANDS.
"""

import sys

from writ.session.mode_engine import cmd_mode
from writ.session.gates import cmd_can_write, cmd_can_read_code
from writ.session.approval_workflow import cmd_advance_phase, cmd_current_phase
from writ.session.budget_tracking import cmd_update, cmd_should_skip, cmd_format
from writ.session.investigations import (
    _AUDIT_BUDGET_LOC,
    _AUDIT_BUDGET_FILES,
    cmd_coverage,
    cmd_coverage_map,
    cmd_record_analysis,
    cmd_synthesis_gate,
    cmd_scope_estimate,
    cmd_partition_scope,
    cmd_coverage_rollup,
    cmd_aggregate_findings,
    cmd_triangulation_gate,
    cmd_staleness_check,
    cmd_lens,
)
from writ.session.violations import (
    cmd_add_pending_violation,
    cmd_clear_pending_violations,
    cmd_invalidate_gate,
    cmd_check_escalation,
    cmd_pending_violations,
)
from writ.session.session_lifecycle import (
    cmd_read,
    cmd_clear_rules_for_compaction,
    cmd_reset_after_compaction,
)
from writ.session.feedback import cmd_auto_feedback
from writ.session.metrics import cmd_metrics
from writ.session.cache import resolve_current_session_id
from writ.session.cli_io import _usage_exit


def _opt_value(name: str, default: str, argv: list[str]) -> str:
    """Return the token following --name in argv, or default if --name is absent or has no
    following token (the guarded `--flag value` extraction repeated across CLI commands)."""
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def _cli_update(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py update <session_id> [--add-rules JSON] [--cost N] [--context-percent N]")
    cmd_update(argv[2], argv[3:])


def _cli_format(argv: list[str]) -> None:
    cmd_format()


def _cli_should_skip(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py should-skip <session_id> [--threshold N]")
    threshold = int(_opt_value("--threshold", "75", argv))
    # Translate bool return to shell exit code: True=skip (0), False=proceed (1)
    sys.exit(0 if cmd_should_skip(argv[2], threshold) else 1)


def _cli_record_analysis(argv: list[str]) -> None:
    if len(argv) < 4:
        _usage_exit("Usage: writ-session.py record-analysis <session_id> <file>  (findings JSON on stdin)")
    cmd_record_analysis(argv[2], argv[3])


def _cli_scope_estimate(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py scope-estimate <session_id> [--budget-loc N]")
    budget = _AUDIT_BUDGET_LOC
    if "--budget-loc" in argv:
        budget = int(argv[argv.index("--budget-loc") + 1])
    cmd_scope_estimate(argv[2], budget_loc=budget)


def _cli_partition_scope(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py partition-scope <session_id> [--max-loc N] [--max-files N]")
    max_loc = _AUDIT_BUDGET_LOC
    max_files = _AUDIT_BUDGET_FILES
    if "--max-loc" in argv:
        max_loc = int(argv[argv.index("--max-loc") + 1])
    if "--max-files" in argv:
        max_files = int(argv[argv.index("--max-files") + 1])
    cmd_partition_scope(argv[2], max_loc=max_loc, max_files=max_files)


def _no_session_message(subcmd: str) -> str:
    """The refusal text for `mode set|switch|init` with no sid and nothing to resolve.

    A bare `mode set work` typed by a human is a legitimate path, so the refusal has to
    hand back a recovery rather than a usage line. It names BOTH env vars the resolver
    accepts and the explicit-sid form, and says why the guess is gone: the resolver used
    to fall back to /tmp/writ-current-session (one file per machine, rewritten by every
    session's turn) and then to the newest session cache by mtime, so a bare command
    could apply this mode to whichever session moved last, in any project on the box.
    Failing here costs one retry; guessing cost another project its mode.
    """
    return (
        "writ-session.py: cannot determine which session to apply "
        f"`mode {subcmd}` to, and Writ no longer guesses one.\n"
        "  Supply it explicitly:  writ-session.py mode "
        f"{subcmd} <conversation|debug|review|work> <session_id>\n"
        "  or export an identity:  CLAUDE_SESSION_ID=<session_id>\n"
        "                          CLAUDE_JOB_DIR=<dir whose basename is the session_id>\n"
        "The removed fallbacks (the shared /tmp/writ-current-session pointer, then the "
        "newest session cache by mtime) both named whichever session on this machine "
        "took a turn most recently, which silently applied one session's mode to another."
    )


def _cli_mode(argv: list[str]) -> None:
    if len(argv) < 4:
        _usage_exit("Usage: writ-session.py mode <get|set|switch> <session_id|value> [session_id]")
    subcmd = argv[2]
    if subcmd == "get":
        cmd_mode(argv[3], "get")
    elif subcmd in ("set", "switch", "init"):
        # <session_id> is optional: when omitted, resolve the CURRENT session via the
        # canonical resolver. The sid is the first non-flag positional after the mode
        # value (argv[3]); flags like --orchestrator are skipped. Behavior with an
        # explicit sid is byte-for-byte unchanged.
        sid = next((a for a in argv[4:] if not a.startswith("--")), None)
        if sid is None:
            sid = resolve_current_session_id()
        if not sid:
            _usage_exit(_no_session_message(subcmd))
        orch = "--orchestrator" in argv
        cmd_mode(sid, subcmd, argv[3], is_orchestrator=orch)
    else:
        _usage_exit(f"Unknown mode subcommand: {subcmd}")


def _cli_carry_forward_mode(argv: list[str]) -> None:
    """SessionStart carry: writ-session.py carry-forward-mode <session_id> <cwd> <prev_session_id> <source>."""
    if len(argv) < 6:
        _usage_exit("Usage: writ-session.py carry-forward-mode <session_id> <cwd> <prev_session_id> <source>")
    from writ.session import rotation
    rotation.carry_forward_mode(argv[2], argv[3], argv[4], argv[5])


def _cli_add_pending_violation(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py add-pending-violation <session_id> --rule R --file F [--line N] [--evidence E]")
    cmd_add_pending_violation(argv[2], argv[3:])


def _cli_invalidate_gate(argv: list[str]) -> None:
    if len(argv) < 4:
        _usage_exit("Usage: writ-session.py invalidate-gate <session_id> <gate> --rule R --file F [--evidence E] [--trace T] [--plan-hash H] [--project-root P]")
    cmd_invalidate_gate(argv[2], argv[3:])


def _cli_can_write(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py can-write <session_id> [--skill-dir PATH]")
    cmd_can_write(argv[2], _opt_value("--skill-dir", "", argv))


def _cli_can_read_code(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py can-read-code <session_id> [--skill-dir PATH]  (tool envelope on stdin)")
    cmd_can_read_code(argv[2], _opt_value("--skill-dir", "", argv))


def _cli_advance_phase(argv: list[str]) -> None:
    if len(argv) < 3:
        _usage_exit("Usage: writ-session.py advance-phase <session_id> [--project-root PATH] [--token TOKEN]")
    cmd_advance_phase(argv[2], _opt_value("--project-root", "", argv), _opt_value("--token", "", argv))


def _cli_metrics(argv: list[str]) -> None:
    cmd_metrics(_opt_value("--log", "", argv))


_SIMPLE_COMMANDS = {
    "read": (cmd_read, "Usage: writ-session.py read <session_id>"),
    "coverage": (cmd_coverage, "Usage: writ-session.py coverage <session_id>"),
    "coverage-map": (cmd_coverage_map, "Usage: writ-session.py coverage-map <session_id>"),
    "synthesis-gate": (cmd_synthesis_gate, "Usage: writ-session.py synthesis-gate <session_id>"),
    "coverage-rollup": (cmd_coverage_rollup, "Usage: writ-session.py coverage-rollup <session_id>  (worker coverage-maps JSON on stdin)"),
    "aggregate-findings": (cmd_aggregate_findings, "Usage: writ-session.py aggregate-findings <session_id>  (worker reports JSON on stdin)"),
    "triangulation-gate": (cmd_triangulation_gate, "Usage: writ-session.py triangulation-gate <session_id>"),
    "staleness-check": (cmd_staleness_check, "Usage: writ-session.py staleness-check <session_id>"),
    "lens": (cmd_lens, "Usage: writ-session.py lens <session_id>"),
    "auto-feedback": (cmd_auto_feedback, "Usage: writ-session.py auto-feedback <session_id>"),
    "clear-pending-violations": (cmd_clear_pending_violations, "Usage: writ-session.py clear-pending-violations <session_id>"),
    "check-escalation": (cmd_check_escalation, "Usage: writ-session.py check-escalation <session_id>"),
    "pending-violations": (cmd_pending_violations, "Usage: writ-session.py pending-violations <session_id>"),
    "current-phase": (cmd_current_phase, "Usage: writ-session.py current-phase <session_id>"),
    "clear-rules-for-compaction": (cmd_clear_rules_for_compaction, "Usage: writ-session.py clear-rules-for-compaction <session_id>"),
    "reset-after-compaction": (cmd_reset_after_compaction, "Usage: writ-session.py reset-after-compaction <session_id>"),
}

_COMPLEX_COMMANDS = {
    "update": _cli_update,
    "format": _cli_format,
    "should-skip": _cli_should_skip,
    "record-analysis": _cli_record_analysis,
    "scope-estimate": _cli_scope_estimate,
    "partition-scope": _cli_partition_scope,
    "mode": _cli_mode,
    "carry-forward-mode": _cli_carry_forward_mode,
    "add-pending-violation": _cli_add_pending_violation,
    "invalidate-gate": _cli_invalidate_gate,
    "can-write": _cli_can_write,
    "can-read-code": _cli_can_read_code,
    "advance-phase": _cli_advance_phase,
    "metrics": _cli_metrics,
}


def dispatch(argv: list[str]) -> None:
    """Route argv (sys.argv) to the matching command handler. Single-arg commands resolve
    through _SIMPLE_COMMANDS; the rest through _COMPLEX_COMMANDS; anything else is unknown."""
    if len(argv) < 2:
        print("Usage: writ-session.py <command> [args]", file=sys.stderr)
        _usage_exit("Commands: read, update, format, should-skip, mode, coverage, coverage-map, record-analysis, synthesis-gate, scope-estimate, partition-scope, coverage-rollup, aggregate-findings, triangulation-gate, staleness-check, lens, auto-feedback, can-write, can-read-code, advance-phase, current-phase, metrics")

    cmd = argv[1]

    if cmd in _SIMPLE_COMMANDS:
        handler, usage = _SIMPLE_COMMANDS[cmd]
        if len(argv) < 3:
            _usage_exit(usage)
        handler(argv[2])
        return

    complex_handler = _COMPLEX_COMMANDS.get(cmd)
    if complex_handler is None:
        _usage_exit(f"Unknown command: {cmd}")
    complex_handler(argv)
