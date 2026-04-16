"""Tests for the cleanup cycle: friction reader, de-dupe, tier removal,
hygiene, ID-collision check, sub-agent unlimited budget.

Each test class targets one issue from the plan so failures point at the
right change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import util
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parent.parent
BUDGET_JSON = SKILL_DIR / "writ" / "shared" / "budget.json"
BIN_SESSION = SKILL_DIR / "bin" / "lib" / "writ-session.py"
PY_SESSION = SKILL_DIR / "writ" / "retrieval" / "session.py"
SUBAGENT_START_HOOK = SKILL_DIR / ".claude" / "hooks" / "writ-subagent-start.sh"
CODEBASE_MD = SKILL_DIR / "CODEBASE.md"
CONTRIBUTING_MD = SKILL_DIR / "CONTRIBUTING.md"
GITIGNORE = SKILL_DIR / ".gitignore"


def _load_bin_session_module():
    spec = util.spec_from_file_location("_bin_writ_session", BIN_SESSION)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Budget de-dupe
# ---------------------------------------------------------------------------


class TestBudgetJson:
    def test_budget_json_exists(self) -> None:
        assert BUDGET_JSON.exists(), "writ/shared/budget.json must exist"

    def test_budget_json_has_required_keys(self) -> None:
        data = json.loads(BUDGET_JSON.read_text())
        for key in ("default_budget", "rule_cost_full", "rule_cost_standard", "rule_cost_summary"):
            assert key in data, f"budget.json missing key: {key}"
        assert data["default_budget"] == 8000
        assert data["rule_cost_full"] == 200
        assert data["rule_cost_standard"] == 120
        assert data["rule_cost_summary"] == 40

    def test_budget_json_declares_subagent_unlimited(self) -> None:
        data = json.loads(BUDGET_JSON.read_text())
        assert "subagent_budget" in data, (
            "budget.json must declare subagent_budget (null = unlimited)"
        )


class TestNoDuplicatedBudgetLiterals:
    def test_bin_session_has_no_hardcoded_8000(self) -> None:
        content = BIN_SESSION.read_text()
        # Must not have `DEFAULT_SESSION_BUDGET = 8000` as a literal assignment
        assert "DEFAULT_SESSION_BUDGET = 8000" not in content, (
            "bin/lib/writ-session.py must load DEFAULT_SESSION_BUDGET from budget.json, not hardcode 8000"
        )

    def test_py_session_has_no_hardcoded_8000(self) -> None:
        content = PY_SESSION.read_text()
        assert "DEFAULT_SESSION_BUDGET = 8000" not in content, (
            "writ/retrieval/session.py must load DEFAULT_SESSION_BUDGET from budget.json"
        )

    def test_both_sources_load_identical_budget(self) -> None:
        """Both the Python package and the stdlib helper must return the same constants."""
        bin_mod = _load_bin_session_module()
        # writ/retrieval/session.py is importable via the installed package
        sys.path.insert(0, str(SKILL_DIR))
        try:
            from writ.retrieval import session as py_session  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)
        assert bin_mod.DEFAULT_SESSION_BUDGET == py_session.DEFAULT_SESSION_BUDGET, (
            "Both session modules must report identical DEFAULT_SESSION_BUDGET"
        )


# ---------------------------------------------------------------------------
# Sub-agent unlimited budget
# ---------------------------------------------------------------------------


class TestSubagentUnlimitedBudget:
    def test_subagent_start_marks_is_subagent(self) -> None:
        content = SUBAGENT_START_HOOK.read_text()
        assert "is_subagent" in content, (
            "writ-subagent-start.sh must set is_subagent: true on the fresh cache"
        )

    def test_should_skip_returns_false_for_subagent(self, tmp_path: Path) -> None:
        """An is_subagent session must not be skipped even with remaining_budget exhausted."""
        mod = _load_bin_session_module()
        mod.CACHE_DIR = str(tmp_path)
        session_id = "sub-test"
        cache = mod._read_cache(session_id)
        cache["is_subagent"] = True
        cache["remaining_budget"] = 0  # Exhausted
        mod._write_cache(session_id, cache)
        # should-skip must return False (don't skip) for sub-agents regardless of budget
        result = mod.cmd_should_skip(session_id)
        assert result is False or result == "false", (
            f"cmd_should_skip must return False for is_subagent sessions; got {result!r}"
        )

    def test_should_skip_returns_true_for_non_subagent_exhausted(self, tmp_path: Path) -> None:
        """Baseline: non-subagent with exhausted budget IS skipped."""
        mod = _load_bin_session_module()
        mod.CACHE_DIR = str(tmp_path)
        session_id = "master-test"
        cache = mod._read_cache(session_id)
        cache["is_subagent"] = False
        cache["remaining_budget"] = 0
        mod._write_cache(session_id, cache)
        result = mod.cmd_should_skip(session_id)
        # should skip when budget exhausted AND not a sub-agent
        assert result is True or result == "true", (
            f"cmd_should_skip must return True for non-subagent sessions with budget=0; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Tier removal
# ---------------------------------------------------------------------------


class TestTierRemoval:
    def test_no_cmd_tier(self) -> None:
        content = BIN_SESSION.read_text()
        assert "def cmd_tier" not in content, (
            "cmd_tier function must be removed (legacy tier facade)"
        )

    def test_no_mode_to_tier_mapping(self) -> None:
        content = BIN_SESSION.read_text()
        assert "_MODE_TO_TIER" not in content, (
            "_MODE_TO_TIER mapping must be removed"
        )

    def test_no_tier_field_in_defaults(self) -> None:
        content = BIN_SESSION.read_text()
        # "tier" appears in other tokens ("writ-test-writer", "tier_num" vars inside cmd_tier which we're removing).
        # Narrow assertion: the cache default field "tier": None must not exist.
        assert '"tier": None' not in content, (
            '"tier": None default field must be removed'
        )

    def test_no_tier_dispatch_case(self) -> None:
        content = BIN_SESSION.read_text()
        # Dispatch typically has `elif subcommand == "tier":` or similar
        assert '"tier"' not in content or 'cmd_tier' not in content, (
            "tier subcommand dispatch must be removed from main()"
        )


# ---------------------------------------------------------------------------
# Friction reader
# ---------------------------------------------------------------------------


class TestFrictionReader:
    def test_friction_module_exists(self) -> None:
        module = SKILL_DIR / "writ" / "analysis" / "friction.py"
        assert module.exists(), "writ/analysis/friction.py must exist"

    def test_cli_has_analyze_friction_subcommand(self) -> None:
        content = (SKILL_DIR / "writ" / "cli.py").read_text()
        assert "analyze-friction" in content or "analyze_friction" in content, (
            "writ CLI must register an analyze-friction subcommand"
        )

    def test_friction_reader_parses_well_formed_log(self, tmp_path: Path) -> None:
        """Feed a small synthetic log and verify aggregation."""
        sys.path.insert(0, str(SKILL_DIR))
        try:
            from writ.analysis.friction import load_events, summarize  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        log = tmp_path / "workflow-friction.log"
        log.write_text(
            '{"ts": "2026-04-15T10:00:00Z", "session": "s1", "event": "hook_execution", "hook_name": "writ-rag-inject", "duration_ms": 120}\n'
            '{"ts": "2026-04-15T10:01:00Z", "session": "s1", "event": "rag_query", "rule_ids": ["RULE-A-001", "RULE-B-002"]}\n'
            '{"ts": "2026-04-15T10:02:00Z", "session": "s1", "event": "gate_denial"}\n'
            'malformed-line-should-be-skipped\n'
            '{"ts": "2026-04-15T10:03:00Z", "session": "s2", "event": "write_failure"}\n'
        )
        events = load_events(log)
        assert len(events) == 4, "parser must skip malformed lines and return 4 events"
        summary = summarize(events)
        assert summary["event_counts"]["hook_execution"] == 1
        assert summary["event_counts"]["rag_query"] == 1
        assert summary["event_counts"]["gate_denial"] == 1
        assert summary["event_counts"]["write_failure"] == 1

    def test_friction_reader_rotation_renames_when_too_large(self, tmp_path: Path) -> None:
        sys.path.insert(0, str(SKILL_DIR))
        try:
            from writ.analysis.friction import rotate_if_needed  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        log = tmp_path / "workflow-friction.log"
        # Write > 5MB of content (5_500_000 bytes of dummy JSONL)
        line = '{"ts": "2026-04-15T10:00:00Z", "event": "hook_execution", "pad": "' + ("x" * 200) + '"}\n'
        with log.open("w") as f:
            while log.stat().st_size < 5_500_000:
                f.write(line)
        rotated = rotate_if_needed(log, threshold_bytes=5 * 1024 * 1024)
        assert rotated is True, "rotate_if_needed must return True when threshold exceeded"
        assert (tmp_path / "workflow-friction.log.1").exists(), "log.1 must be created"
        assert log.stat().st_size < 1024, "original log must be reset to near-empty"


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


class TestHygiene:
    def test_stray_tilde_dir_removed(self) -> None:
        tilde_dir = SKILL_DIR / "~"
        assert not tilde_dir.exists(), (
            "stray '~/' directory at repo root must be removed"
        )

    def test_gitignore_has_pycache(self) -> None:
        content = GITIGNORE.read_text()
        assert "__pycache__/" in content, (
            ".gitignore must include __pycache__/ defensively"
        )


# ---------------------------------------------------------------------------
# ID collision check
# ---------------------------------------------------------------------------


class TestIdCollisionCheck:
    def test_authoring_has_collision_check(self) -> None:
        """Authoring pipeline must have an explicit ID-collision check."""
        # The check may live in writ/authoring.py or writ/graph/authoring.py
        candidates = [
            SKILL_DIR / "writ" / "authoring.py",
            SKILL_DIR / "writ" / "graph" / "authoring.py",
        ]
        found = False
        for c in candidates:
            if c.exists() and "check_id_collision" in c.read_text():
                found = True
                break
        assert found, (
            "An explicit check_id_collision function must exist in the authoring pipeline"
        )

    def test_collision_raises_dedicated_error(self) -> None:
        """Collision path must raise a named exception, not a generic one."""
        candidates = [
            SKILL_DIR / "writ" / "authoring.py",
            SKILL_DIR / "writ" / "graph" / "authoring.py",
        ]
        for c in candidates:
            if c.exists():
                content = c.read_text()
                if "check_id_collision" in content:
                    assert "RuleIdCollisionError" in content or "IdCollisionError" in content, (
                        "collision check must raise a dedicated error class, not a generic Exception"
                    )
                    return
        pytest.fail("no authoring module with check_id_collision found")

    def test_contributing_lists_id_collision_gate_explicitly(self) -> None:
        content = CONTRIBUTING_MD.read_text().lower()
        # Must mention id collision as a gate check
        assert "id collision" in content or "id_collision" in content or "rule id collision" in content, (
            "CONTRIBUTING.md must explicitly list ID collision as a gate check"
        )
