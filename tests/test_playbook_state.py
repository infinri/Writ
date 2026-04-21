"""Phase 1 deliverable 7.1: session state transitions for playbook/verification/quality."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SESSION_CLI = Path(__file__).resolve().parents[1] / "bin" / "lib" / "writ-session.py"


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the session cache at an isolated temp dir per test."""
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    # Clear any module-level cache path the imported module may have resolved.
    sys.path.insert(0, str(SESSION_CLI.parent))
    return tmp_path


def _read_session(sid: str, cache_dir: Path) -> dict:
    path = cache_dir / f"writ-session-{sid}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _run_cli(args: list[str], cache_dir: Path) -> subprocess.CompletedProcess:
    env = {"WRIT_CACHE_DIR": str(cache_dir), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(SESSION_CLI), *args],
        capture_output=True, text=True, env=env,
    )


class TestPhase1SessionStateFieldsExist:
    """New Phase 1 fields appear in the cache with the right defaults."""

    def test_new_session_has_playbook_fields(self, isolated_cache: Path) -> None:
        result = _run_cli(["read", "test-session-1"], isolated_cache)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["active_playbook"] is None
        assert data["active_phase"] is None
        assert data["playbook_phase_history"] == []

    def test_new_session_has_verification_fields(self, isolated_cache: Path) -> None:
        result = _run_cli(["read", "test-session-2"], isolated_cache)
        data = json.loads(result.stdout)
        assert data["verification_evidence"] == {}
        assert data["review_ordering_state"] == {}

    def test_new_session_has_quality_fields(self, isolated_cache: Path) -> None:
        result = _run_cli(["read", "test-session-3"], isolated_cache)
        data = json.loads(result.stdout)
        assert data["quality_judgment_state"] == {}
        assert data["quality_override_count"] == 0


class TestForwardCompatWithOldSessions:
    """Old session caches (pre-Phase-1) load cleanly with new fields defaulted."""

    def test_pre_phase_1_cache_loads(self, isolated_cache: Path) -> None:
        # Write a minimal pre-Phase-1 cache file
        sid = "old-session"
        old_cache = {
            "loaded_rule_ids": ["ARCH-ORG-001"],
            "loaded_rules": [],
            "remaining_budget": 5000,
            "context_percent": 10,
            "queries": 1,
            "mode": "work",
            "is_subagent": False,
            "files_written": [],
            "analysis_results": {},
            "feedback_sent": [],
            "pending_violations": [],
            "invalidation_history": {},
            "escalation": {"gate": None, "needed": False, "diagnosis": None, "feedback_sent": False},
            # All Phase 1 fields intentionally absent.
        }
        (isolated_cache / f"writ-session-{sid}.json").write_text(json.dumps(old_cache))
        result = _run_cli(["read", sid], isolated_cache)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        # Existing fields preserved
        assert data["mode"] == "work"
        assert data["loaded_rule_ids"] == ["ARCH-ORG-001"]
        # Phase 1 fields filled in with defaults
        assert data["active_playbook"] is None
        assert data["playbook_phase_history"] == []
        assert data["verification_evidence"] == {}
        assert data["quality_judgment_state"] == {}
        assert data["quality_override_count"] == 0
