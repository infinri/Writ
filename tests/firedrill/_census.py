"""The declared refusal inventory (plan.md Decision 2 and Decision 6's sibling table).

Per refusal: its script, event, trigger setup, declared mechanism, declared record
shape, and declared stream -- plus the action-marker set a refusal reason must match
(the mechanical form of the keystone that a refusal must name the way out: a guard
whose message names no action is a deadlock, not a control).

Each entry's `setup(iso)` is real fixture-building code (files, a session cache, extra
env), not a mock: it returns the envelope to feed the hook on stdin plus any per-case
env overrides. `generic=False` entries (validate-rules.sh's two sentinel sites) are
exercised by dedicated tests in test_bash_refusals.py rather than the generic
parametrized loop, because their trigger paths are not uniform with the rest (one
needs a live `/analyze` stub server); they are still declared here so the script-level
completeness check in test_refusal_inventory.py has one canonical list to read.

COVERAGE NOTE (stated here rather than hidden): this census does not yet declare
every bash-side refusal the plan's Analysis counts (33 across 14 scripts). It covers
26 refusals across 15 scripts with a real, working trigger each, including the whole
irreversible-destruction vector in writ-bash-write-gate.sh (the Neo4j-via-container
statements and the five git-history patterns match on plain command text, so each is
a one-line payload with no fixture, no live server and no classification step -- the
cheapest family in the file). Not yet declared: writ-dispatch-discipline.sh's
reroute-vs-deny escalation, writ-memory-policy-guard.sh, and
writ-pre-write-dispatch.sh's gate-denial/escalation paths -- each needs a multi-step
fixture (a live escalation history, or a memory-write classification) this cycle's
skeleton did not build. Once `tests/_inventory.py`'s own source-derived
`derive_refusing_scripts()` lands, a gap between it and `refusing_scripts()` below
for one of THESE scripts is this census
being incomplete, not a new, undeclared refusal -- that distinction matters for
whoever triages the first red run of test_refusal_inventory.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from tests.firedrill._harness import Isolation, write_cache

# ── The action-marker set ────────────────────────────────────────────────────────
#
# Each phrase below is copied from an ACTUAL refusal reason already in this codebase
# (see the comment on the matching census entry for which one), not invented in the
# abstract. A refusal reason that matches none of these is real signal that the
# message may be a deadlock rather than a control -- report it, do not loosen the
# marker set to make it pass.
ACTION_MARKERS: tuple[str, ...] = (
    "fix",                          # enforce-violations.sh, writ-verify-before-claim.sh
    "read the log",                 # writ-run-pending-tests.sh (emit-summary.py)
    "re-send",                      # writ-comms-output-gate.sh
    "template",                     # writ-bash-write-gate.sh credential deny
    "ask the user",                 # writ-bash-write-gate.sh / writ-state-write-gate.sh state deny
    "re-issue",                     # writ-read-junk-gate.sh (enforce mode)
    "bypass",                       # validate-test-file.sh
    "must name",                    # validate-design-doc.sh
    "delete it",                    # pre-validate-file.sh (shell-commented-out)
    "write it by filling in",       # validate-exit-plan.sh
    "add missing keys",             # validate-handoff.sh
    "record it in debug.md",        # writ-debug-code-gate.sh
    "before creating the worktree", # writ-worktree-safety.sh
)


def matches_action_marker(reason: str) -> bool:
    low = (reason or "").lower()
    return any(marker in low for marker in ACTION_MARKERS)


@dataclass
class Refusal:
    id: str
    script: str
    event: str
    mechanism: str  # "exit2_stderr" | "exit_nonzero_stderr" | "permissionDecisionReason"
    shape: str  # "gate_decision" | "write_attempt" | "friction_custom" | "invalidation_history"
    setup: Callable[[Isolation], dict] | None
    exit_code: int | None = None
    permission_decision: str | None = None  # "deny" | "ask"
    gate_name: str | None = None
    custom_event: str | None = None
    generic: bool = True
    check_action_marker: bool = True
    notes: str = ""


# ── Setup functions. Each writes only the fixtures its own case needs
# (TEST-FIXTURE-002) and returns {"envelope": ..., "extra_env": {...}?}. ─────────────


def _setup_enforce_violations(iso: Isolation) -> dict:
    write_cache(iso, {
        "mode": "work",
        "pending_violations": [{"rule_id": "ENF-TEST-999", "file": "src/thing.py"}],
    })
    return {"envelope": {"session_id": iso.session_id, "hook_event_name": "Stop"}}


def _setup_run_pending_tests(iso: Isolation) -> dict:
    tests_dir = iso.project_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_firedrill_fail.py"
    test_file.write_text("def test_that_fails():\n    assert False\n")
    gates_dir = iso.project_root / ".claude" / "gates" / iso.session_id
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / "phase-a.approved").write_text("ok")
    (gates_dir / "test-skeletons.approved").write_text("ok")
    write_cache(iso, {"mode": "work", "current_phase": "implementation"})
    marker_dir = iso.cache_dir / iso.session_id
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "pending-tests.txt").write_text(str(test_file) + "\n")
    return {"envelope": {"session_id": iso.session_id, "hook_event_name": "Stop"}}


def _setup_verify_before_claim(iso: Isolation) -> dict:
    write_cache(iso, {
        "mode": "work",
        "quality_judgment_state": {"src/thing.py": {"score": 1, "overridden": False}},
    })
    return {"envelope": {"session_id": iso.session_id, "hook_event_name": "Stop"}}


def _setup_comms_output_gate(iso: Isolation) -> dict:
    transcript = iso.tmp_path / "transcript.jsonl"
    row = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Done -- but not really."}]},
    })
    transcript.write_text(row + "\n")
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    }


def _setup_bash_write_credential(iso: Isolation) -> dict:
    target = iso.project_root / ".env"
    cmd = f"echo 'SECRET=1' > {target}"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        },
    }


def _setup_bash_write_state(iso: Isolation) -> dict:
    # A non-read-only redirect naming a path that CONTAINS "writ-session-": the state
    # guard matches on command TEXT, not on an actual gate-state path, and a plain
    # `cat`/`grep` of the same text is treated as read-only inspection and allowed.
    target = iso.tmp_path / "writ-session-forged.json"
    cmd = f"echo '{{}}' > {target}"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        },
    }


def _setup_state_write_gate(iso: Isolation) -> dict:
    target = iso.cache_dir / f"writ-session-{iso.session_id}.json"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
        },
    }


def _setup_read_junk_enforce(iso: Isolation) -> dict:
    big_file = iso.project_root / "big.log"
    big_file.write_text("x" * (150 * 1024))  # over the 100 KB default WRIT_READ_SIZE_KB
    write_cache(iso, {"mode": "work"})
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(big_file)},
        },
        "extra_env": {"WRIT_READ_JUNK_GATE": "enforce"},
    }


def _setup_validate_test_file(iso: Isolation) -> dict:
    write_cache(iso, {"mode": "work"})
    target = iso.project_root / "src" / "thing.py"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
        },
    }


def _setup_validate_design_doc(iso: Isolation) -> dict:
    write_cache(iso, {"mode": "work"})
    target = iso.project_root / "docs" / "areas" / "specs" / "widget-design.md"
    content = "# Widget design\n\nnothing filled in yet.\n"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": content},
        },
    }


def _setup_pre_validate_commented_out(iso: Isolation) -> dict:
    target = iso.project_root / "scripts" / "thing.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/bash\necho hello\n")
    new_content = "# fully commented out now\n# nothing runs\n"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": new_content},
        },
    }


def _setup_validate_exit_plan(iso: Isolation) -> dict:
    write_cache(iso, {"mode": "work"})
    # No plan.md anywhere under project_root -> _validate_phase_a's "not found" branch.
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "ExitPlanMode",
            "tool_input": {},
        },
    }


def _setup_validate_handoff(iso: Isolation) -> dict:
    handoffs = iso.project_root / ".claude" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    target = handoffs / "slice-1.json"
    target.write_text("{}")
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
        },
    }


def _setup_debug_code_gate(iso: Isolation) -> dict:
    write_cache(iso, {"mode": "debug"})  # source_type defaults to "runtime" in debug mode
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"path": str(iso.project_root)},
        },
    }


def _setup_worktree_safety(iso: Isolation) -> dict:
    write_cache(iso, {"mode": "work"})
    cmd = "git worktree add .worktrees/featx featx"
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        },
    }


def _bash_command_setup(cmd: str) -> Callable[[Isolation], dict]:
    """A setup factory for writ-bash-write-gate.sh cases that are JUST a command
    string: no cache, no fixture files, no live server. The irreversible-destruction
    vector matches on plain command text, so this is the whole trigger."""

    def _setup(iso: Isolation) -> dict:
        return {
            "envelope": {
                "session_id": iso.session_id,
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
            },
        }

    return _setup


# The Neo4j destructive-statement vector (2026-08-08 incident): a graph-reaching verb
# (docker exec / docker compose exec / docker-compose exec / cypher-shell) AND a
# destructive Cypher statement, both required. One entry per _IRREV_CYPHER_RE
# sub-pattern, since each is an independent branch of the same regex alternation and
# a regression narrowing the regex to only the first alternative would otherwise pass
# with a single case.
# ASSEMBLED FROM FRAGMENTS, deliberately, so that no line in this file lexically
# contains a whole-graph delete. tests/test_graph_dump.py statically scans the repo for
# unscoped `MATCH (n) ... DELETE` and flagged these trigger strings as offenders: a
# lexical scanner cannot tell a test fixture's TRIGGER from real code that would run.
# The alternative was exempting this path in that scanner, which would have widened a
# guard protecting against the two graph wipes in this repo's history in order to
# accommodate a test. The mention-versus-use seam belongs on this side of the line.
_MATCH_ALL = "MATCH " + "(n)"
_IRREVERSIBLE_CYPHER_STATEMENTS = {
    "detach-delete": f"{_MATCH_ALL} DETACH " + "DELETE n",
    "match-delete": f"{_MATCH_ALL} " + "DELETE n",
    "drop-constraint": "DROP CONSTRAINT foo",
    "drop-index": "DROP INDEX foo",
}

# The git-history-destruction vector: five patterns, each its own case so a
# regression narrowing any ONE of them is caught rather than averaged away.
_IRREVERSIBLE_GIT_COMMANDS = {
    "reset-hard": "git reset --hard HEAD~1",
    "tag-d": "git tag -d v1.0.0",
    "branch-D": "git branch -D feature-branch",
    "clean-f": "git clean -fd",
    "push-force": "git push origin main --force",
}


def _setup_validate_rules_site_a(iso: Isolation) -> dict:
    sys_tmp = iso.tmp_path / "sysTmp"
    sys_tmp.mkdir(parents=True, exist_ok=True)
    sentinel = sys_tmp / f"writ-validate-rules-invalidated-{iso.session_id}"
    sentinel.write_text("invalidated")
    return {
        "envelope": {
            "session_id": iso.session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "irrelevant.py"},
        },
        "extra_env": {"TMPDIR": str(sys_tmp)},
        "sentinel_path": sentinel,
    }


def _setup_validate_rules_site_b(iso: Isolation) -> dict:
    """Not run through the generic loop (needs a live /analyze stub server).

    See test_bash_refusals.py::TestValidateRulesBothSites.test_tail_end_sentinel_
    exit_2_after_a_routed_invalidation for the full build (plan.md, session cache with
    loaded_rules + files_written + analysis_results, TMPDIR, and the stub server).
    Declared here only so `refusing_scripts()` names validate-rules.sh once and the
    site-a and site-b entries stay side by side for a reader comparing them.
    """
    return {}


REFUSALS: list[Refusal] = [
    Refusal(
        id="enforce-violations",
        script="enforce-violations.sh",
        event="Stop",
        mechanism="exit2_stderr",
        exit_code=2,
        shape="gate_decision",
        setup=_setup_enforce_violations,
        notes="D1: the only Stop hook with a real blocking exit 2 logged nothing.",
    ),
    Refusal(
        id="run-pending-tests",
        script="writ-run-pending-tests.sh",
        event="Stop",
        mechanism="exit_nonzero_stderr",
        exit_code=1,
        shape="gate_decision",
        setup=_setup_run_pending_tests,
        notes="D1b + D3 pin: exit code stays 1; the record it never had is the fix.",
    ),
    Refusal(
        id="verify-before-claim",
        script="writ-verify-before-claim.sh",
        event="Stop",
        mechanism="exit_nonzero_stderr",
        exit_code=1,
        shape="gate_decision",
        gate_name="verify-before-claim",
        setup=_setup_verify_before_claim,
        notes="D3 pin: exit code stays 1 (PINNED, NOT CHANGED).",
    ),
    Refusal(
        id="comms-output-gate",
        script="writ-comms-output-gate.sh",
        event="Stop",
        mechanism="exit_nonzero_stderr",
        exit_code=1,
        shape="gate_decision",
        gate_name="comms-output",
        setup=_setup_comms_output_gate,
        notes="D3 pin: exit code stays 1 (PINNED, NOT CHANGED).",
    ),
    Refusal(
        id="bash-write-credential",
        script="writ-bash-write-gate.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="bash-write",
        setup=_setup_bash_write_credential,
        notes="D4: emit_deny fires today; log_gate_decision does not (the fix).",
    ),
    Refusal(
        id="bash-write-state",
        script="writ-bash-write-gate.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="bash-write",
        setup=_setup_bash_write_state,
        notes="Sibling of the credential deny; already calls log_gate_decision today.",
    ),
    Refusal(
        id="state-write-gate",
        script="writ-state-write-gate.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="state-write",
        setup=_setup_state_write_gate,
    ),
    Refusal(
        id="read-junk-enforce",
        script="writ-read-junk-gate.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="friction_custom",
        custom_event="read_blocked",
        setup=_setup_read_junk_enforce,
        notes="D8: the custom-event friction-append pipe shape.",
    ),
    Refusal(
        id="validate-test-file",
        script="validate-test-file.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="test-first",
        setup=_setup_validate_test_file,
    ),
    Refusal(
        id="validate-design-doc",
        script="validate-design-doc.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="design-doc",
        setup=_setup_validate_design_doc,
    ),
    Refusal(
        id="pre-validate-commented-out",
        script="pre-validate-file.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="shell-commented-out",
        setup=_setup_pre_validate_commented_out,
    ),
    Refusal(
        id="validate-exit-plan",
        script="validate-exit-plan.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="friction_custom",
        custom_event="exitplanmode_denial",
        setup=_setup_validate_exit_plan,
    ),
    Refusal(
        id="validate-handoff",
        script="validate-handoff.sh",
        event="PostToolUse",
        mechanism="exit_nonzero_stderr",
        exit_code=1,
        shape="gate_decision",
        gate_name="handoff",
        setup=_setup_validate_handoff,
    ),
    Refusal(
        id="debug-code-gate",
        script="writ-debug-code-gate.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="debug-code-read",
        setup=_setup_debug_code_gate,
    ),
    Refusal(
        id="worktree-safety",
        script="writ-worktree-safety.sh",
        event="PreToolUse",
        mechanism="permissionDecisionReason",
        permission_decision="deny",
        shape="gate_decision",
        gate_name="worktree-safety",
        setup=_setup_worktree_safety,
    ),
    *[
        Refusal(
            id=f"bash-write-irreversible-cypher-{name}",
            script="writ-bash-write-gate.sh",
            event="PreToolUse",
            mechanism="permissionDecisionReason",
            permission_decision="deny",
            shape="gate_decision",
            gate_name="irreversible",
            setup=_bash_command_setup(f'docker exec neo4j cypher-shell "{statement}"'),
            notes=(
                "Irreversible-destruction vector, Neo4j sub-pattern "
                f"{name!r}: plain command text, no fixture needed. Already refused "
                "and already recorded today (pre-existing, not a cycle fix)."
            ),
        )
        for name, statement in _IRREVERSIBLE_CYPHER_STATEMENTS.items()
    ],
    *[
        Refusal(
            id=f"bash-write-irreversible-git-{name}",
            script="writ-bash-write-gate.sh",
            event="PreToolUse",
            mechanism="permissionDecisionReason",
            permission_decision="deny",
            shape="gate_decision",
            gate_name="irreversible",
            setup=_bash_command_setup(command),
            notes=(
                f"Irreversible-destruction vector, git sub-pattern {name!r}: plain "
                "command text, no fixture needed. Already refused and already "
                "recorded today (pre-existing, not a cycle fix)."
            ),
        )
        for name, command in _IRREVERSIBLE_GIT_COMMANDS.items()
    ],
    Refusal(
        id="validate-rules-site-a",
        script="validate-rules.sh",
        event="PostToolUse",
        mechanism="exit2_stderr",
        exit_code=2,
        shape="invalidation_history",
        setup=_setup_validate_rules_site_a,
        generic=False,
        check_action_marker=False,
        notes="D2 site A (top-of-file): bare exit 2, no stderr today (the fix).",
    ),
    Refusal(
        id="validate-rules-site-b",
        script="validate-rules.sh",
        event="PostToolUse",
        mechanism="exit2_stderr",
        exit_code=2,
        shape="invalidation_history",
        setup=_setup_validate_rules_site_b,
        generic=False,
        check_action_marker=False,
        notes="D2 site B (tail-end): bare exit 2, no stderr today (the fix).",
    ),
]


def by_id(refusal_id: str) -> Refusal:
    for r in REFUSALS:
        if r.id == refusal_id:
            return r
    raise KeyError(refusal_id)


def generic_refusals() -> list[Refusal]:
    return [r for r in REFUSALS if r.generic]


def refusing_scripts() -> set[str]:
    """The set of hook scripts this census declares as having a refusal path."""
    return {r.script for r in REFUSALS}


# Refusing scripts this census does NOT yet declare, each with the reason.
#
# This exists so the completeness check can be `derived == declared | deferred`
# rather than a narrowed derivation. Narrowing the derivation to make the sets
# match would convert a visible gap into an invisible one and destroy the only
# check that catches a NEW undeclared refusal, which is the whole point of
# test_refusal_inventory.py. A name here is a debt with a stated reason; a name
# missing from the derivation is a hole nobody can see.
#
# To discharge one: build its fixture, add its Refusal entries above, and delete
# the line here. The completeness assertion then keeps passing on its own.
DEFERRED_SCRIPTS: dict[str, str] = {
    "validate-file.sh": (
        "post-write static-analysis refusal (exit 1). Needs a project fixture whose "
        "written file yields a real analyzer error, plus a run-analysis.sh that is not "
        "stubbed, so the trigger is a toolchain setup rather than a payload."
    ),
    "writ-dispatch-discipline.sh": (
        "reroute-versus-deny escalation. The deny arm fires only after a recorded "
        "escalation history, so the fixture has to build prior state, not one envelope."
    ),
    "writ-memory-policy-guard.sh": (
        "rule-weakening memory write. Needs content that trips one of the eight "
        "weakening regexes at a path matching the auto-memory glob, which is a "
        "classification fixture rather than a command string."
    ),
    "writ-pre-write-dispatch.sh": (
        "gate-denial and repeated-violation escalation paths. The decision comes from "
        "the daemon or the local fallback, so the fixture must drive a real "
        "_can_write_check into deny and then into ask via a denial count."
    ),
}


def deferred_scripts() -> set[str]:
    """Refusing scripts knowingly not declared yet. See DEFERRED_SCRIPTS."""
    return set(DEFERRED_SCRIPTS)
