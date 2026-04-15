"""Tests for orchestrator workflow hardening from the Back-in-Stock audit.

Covers three fixes:
1. Orchestrator mode-set instructions specify --orchestrator flag
2. Sub-agent post-write verification in agent definitions
3. Agent-type fallback + pre_write_decision logging
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = SKILL_DIR / ".claude" / "agents"
HOOKS_DIR = SKILL_DIR / ".claude" / "hooks"
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
# Fix 3a: Pre-write dispatcher decision logging
# ---------------------------------------------------------------------------


class TestPreWriteDecisionLogging:
    """writ-pre-write-dispatch.sh must log pre_write_decision events."""

    def test_dispatch_hook_logs_pre_write_decision(self) -> None:
        hook = HOOKS_DIR / "writ-pre-write-dispatch.sh"
        assert hook.exists()
        content = hook.read_text()
        assert "pre_write_decision" in content, (
            "writ-pre-write-dispatch.sh must emit pre_write_decision friction event"
        )

    def test_dispatch_hook_logs_decision_field(self) -> None:
        """The event payload must include a decision field."""
        content = (HOOKS_DIR / "writ-pre-write-dispatch.sh").read_text()
        # Accept any plausible decision field syntax: JSON key (single or double
        # quoted in Python heredoc), bash var, or shell variable reference.
        assert (
            '"decision"' in content
            or "'decision':" in content
            or "decision=" in content
            or "${DECISION" in content
            or "$DECISION" in content
        ), "pre_write_decision event must carry a decision field"


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
