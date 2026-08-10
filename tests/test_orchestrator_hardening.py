"""Tests for orchestrator workflow hardening from the Back-in-Stock audit.

Covers three fixes:
1. Orchestrator mode-set instructions specify --orchestrator flag
2. Sub-agent post-write verification in agent definitions
3. Agent-type fallback + sub-agent start logging; write_attempt is the canonical
   write-decision event (the bare pre_write_decision event is retired -- Phase 1.3)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401


SKILL_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = SKILL_DIR / "agents"
HOOKS_DIR = SKILL_DIR / "hooks" / "scripts"
RULES_DIR = SKILL_DIR / "rules"


# ---------------------------------------------------------------------------
# Fix 1: Orchestrator flag propagation
# ---------------------------------------------------------------------------


class TestOrchestratorFlagInRules:
    """rules/writ-orchestrator.md must instruct using --orchestrator on mode-set."""

    def test_orchestrator_rules_file_exists(self) -> None:
        assert (RULES_DIR / "writ-orchestrator.md").exists()

    def test_orchestrator_rules_mention_flag(self) -> None:
        content = (RULES_DIR / "writ-orchestrator.md").read_text()
        assert "--orchestrator" in content, (
            "writ-orchestrator.md must instruct orchestrator to use --orchestrator flag"
        )

    def test_orchestrator_rules_explain_flag(self) -> None:
        """The file should explain WHY the flag matters, not just show it."""
        content = (RULES_DIR / "writ-orchestrator.md").read_text()
        content_lower = content.lower()
        assert (
            "suppress" in content_lower
            or "injection" in content_lower
            or "token" in content_lower
        ), "writ-orchestrator.md must explain why --orchestrator matters"


# ---------------------------------------------------------------------------
# Fix 2: Agent-definition post-write verification
# ---------------------------------------------------------------------------


class TestAgentPostWriteVerification:
    """Each worker agent definition must include a verification step."""

    @pytest.mark.parametrize(
        "agent_name,expected_files",
        [
            ("writ-planner", ["plan.md", "capabilities.md"]),
            ("writ-test-writer", ["test"]),  # any mention of test file verification
            ("writ-implementer", ["plan.md"]),  # verify files listed in plan.md
        ],
    )
    def test_agent_has_verification_instruction(
        self, agent_name: str, expected_files: list[str]
    ) -> None:
        agent_path = AGENTS_DIR / f"{agent_name}.md"
        assert agent_path.exists(), f"{agent_name}.md must exist"
        content = agent_path.read_text().lower()
        # Must have a verification/check instruction
        assert any(
            token in content for token in ("verify", "check that", "confirm", "exists on disk")
        ), f"{agent_name}.md must include a post-write verification instruction"
        # Must mention retrying once on failure
        assert "re-attempt" in content or "retry" in content, (
            f"{agent_name}.md must specify retry-once behavior on verification failure"
        )


# ---------------------------------------------------------------------------
# Fix 3a: write_attempt is the canonical write-decision event
# (the bare pre_write_decision event is retired -- Phase 1.3)
# ---------------------------------------------------------------------------


class TestPreWriteDecisionRetired:
    """pre_write_decision was emitted bare (DECISION_PAYLOAD defaulted to '{}'),
    so analyze-friction reported every decision as 'unknown'. write_attempt
    (emitted by the gate) already carries the rich file_path/result/gate_status,
    so the dead event is retired and the analyzer reads write_attempt instead."""

    def test_dispatch_hook_no_longer_logs_pre_write_decision(self) -> None:
        content = (HOOKS_DIR / "writ-pre-write-dispatch.sh").read_text()
        # The emit passed the event name quoted: log_friction_event ... "pre_write_decision".
        # Explanatory comments may still mention the (unquoted) name.
        assert '"pre_write_decision"' not in content, (
            "pre_write_decision is retired; write_attempt is the canonical "
            "write-decision event"
        )

    def test_write_attempt_is_canonical_decision_event(self) -> None:
        """The gate emits write_attempt with the rich decision fields."""
        gates = (SKILL_DIR / "writ" / "session" / "gates.py").read_text()
        assert "write_attempt" in gates, "the gate must emit write_attempt"
        assert "gate_status" in gates and "file_path" in gates, (
            "write_attempt must carry gate_status and file_path"
        )

    def test_analyzer_reads_write_attempt_for_decisions(self) -> None:
        """analyze-friction's decision summary must come from write_attempt,
        not the retired pre_write_decision (which produced 'unknown' for all)."""
        friction = (SKILL_DIR / "writ" / "analysis" / "friction.py").read_text()
        assert 'evt == "pre_write_decision"' not in friction, (
            "analyzer must not key its decision summary on the retired event"
        )
        assert "write_decisions" in friction and 'evt == "write_attempt"' in friction


# ---------------------------------------------------------------------------
# Fix 3b: Sub-agent type fallback + telemetry
# ---------------------------------------------------------------------------


class TestSubagentTypeFallback:
    """writ-subagent-start.sh and writ-subagent-stop.sh must fall back to
    'general-purpose' and emit subagent_type_fallback when stdin lacks agent_type.
    """

    def test_start_hook_has_fallback(self) -> None:
        content = (HOOKS_DIR / "writ-subagent-start.sh").read_text()
        assert "general-purpose" in content, (
            "writ-subagent-start.sh must fall back to 'general-purpose' when agent_type is empty"
        )

    def test_start_hook_logs_fallback_event(self) -> None:
        content = (HOOKS_DIR / "writ-subagent-start.sh").read_text()
        assert "subagent_type_fallback" in content, (
            "writ-subagent-start.sh must log subagent_type_fallback when fallback fires"
        )

    def test_stop_hook_has_fallback(self) -> None:
        content = (HOOKS_DIR / "writ-subagent-stop.sh").read_text()
        assert "general-purpose" in content, (
            "writ-subagent-stop.sh must fall back to 'general-purpose' when agent_type is empty"
        )

    def test_stop_hook_logs_fallback_event(self) -> None:
        content = (HOOKS_DIR / "writ-subagent-stop.sh").read_text()
        assert "subagent_type_fallback" in content, (
            "writ-subagent-stop.sh must log subagent_type_fallback when fallback fires"
        )


# ---------------------------------------------------------------------------
# Fix 3c (keystone): writ-subagent-start.sh must source common.sh BEFORE the
# empty-agent_type fallback branch, or `set -e` kills the hook at the undefined
# log_friction_event -- the root cause of subagent_start being under-logged ~5x
# and un-typed sub-agents getting no session/rules (Phase 1.1).
# ---------------------------------------------------------------------------


class TestSubagentStartLogsReliably:
    """Regression: start hook logs subagent_start even when agent_type is empty."""

    def test_common_sourced_before_first_use(self) -> None:
        content = (HOOKS_DIR / "writ-subagent-start.sh").read_text()
        src_idx = content.find("bin/lib/common.sh")
        # Match the CALL form `log_friction_event "...` (a space+quote follows),
        # not prose comments that merely name the function.
        first_use = content.find('log_friction_event "')
        assert src_idx != -1, "start hook must source common.sh"
        assert first_use != -1, "start hook must call log_friction_event"
        assert src_idx < first_use, (
            "common.sh must be sourced BEFORE the first log_friction_event call "
            "(else set -e kills the hook on the empty-agent_type fallback branch)"
        )

    def test_start_hook_logs_subagent_start_with_empty_agent_type(self, tmp_path: Path) -> None:
        import json
        import subprocess

        hook = HOOKS_DIR / "writ-subagent-start.sh"
        friction = tmp_path / "workflow-friction.log"
        env = {
            **os.environ,
            "WRIT_FRICTION_LOG": str(friction),
            "WRIT_CACHE_DIR": str(tmp_path),
            "WRIT_PORT": "0",  # force the /health probe to fail fast -> skip RAG
        }
        envelope = {"agent_id": "kt-agent-1", "agent_type": "", "session_id": "kt-parent"}
        proc = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps(envelope),
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        events = (
            [json.loads(ln) for ln in friction.read_text().splitlines() if ln.strip()]
            if friction.exists() else []
        )
        kinds = [e.get("event") for e in events]
        assert "subagent_start" in kinds, (
            f"start hook must log subagent_start even with empty agent_type; "
            f"got {kinds}, stderr={proc.stderr!r}"
        )
        # The start-hook fallback telemetry now actually fires (was 0 before the fix).
        assert "subagent_type_fallback" in kinds


# ---------------------------------------------------------------------------
# End-to-end: session cache propagates is_orchestrator through mode set
# ---------------------------------------------------------------------------


class TestOrchestratorSessionCache:
    """Setting mode with --orchestrator must persist is_orchestrator=True in cache."""

    def test_mode_set_with_orchestrator_flag_persists(self, tmp_path: Path) -> None:
        """Invoking writ-session.py mode set work --orchestrator sets is_orchestrator=True."""
        import subprocess
        import json

        session_id = "test-orch-hardening"
        env = {**os.environ, "WRIT_CACHE_DIR": str(tmp_path)}
        helper = SKILL_DIR / "bin" / "lib" / "writ-session.py"

        subprocess.run(
            ["python3", str(helper), "mode", "set", "work", session_id, "--orchestrator"],
            env=env,
            check=True,
            capture_output=True,
        )

        cache_file = tmp_path / f"writ-session-{session_id}.json"
        assert cache_file.exists(), "session cache must be written"
        cache = json.loads(cache_file.read_text())
        assert cache.get("is_orchestrator") is True, (
            "is_orchestrator must be True after --orchestrator flag"
        )

    def test_mode_set_without_orchestrator_flag_stays_false(self, tmp_path: Path) -> None:
        """Without --orchestrator, is_orchestrator defaults to False."""
        import subprocess
        import json

        session_id = "test-no-orch"
        env = {**os.environ, "WRIT_CACHE_DIR": str(tmp_path)}
        helper = SKILL_DIR / "bin" / "lib" / "writ-session.py"

        subprocess.run(
            ["python3", str(helper), "mode", "set", "work", session_id],
            env=env,
            check=True,
            capture_output=True,
        )

        cache_file = tmp_path / f"writ-session-{session_id}.json"
        cache = json.loads(cache_file.read_text())
        assert cache.get("is_orchestrator") is False, (
            "is_orchestrator must stay False when flag not passed"
        )
