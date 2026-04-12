"""Tests for sub-agent session isolation in Writ v3.

Covers: agent_id-based session routing, isolated cache creation,
fresh RAG budget per worker, parse-hook-stdin agent_id extraction.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import the session helper as a module (it's not in a package)
# ---------------------------------------------------------------------------

HELPER_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py"
)

spec = importlib.util.spec_from_file_location("writ_session", HELPER_PATH)
writ_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writ_session)

SKILL_DIR = os.path.join(os.path.dirname(__file__), os.pardir)

PARSE_HOOK_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "bin", "lib", "parse-hook-stdin.py"
)


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    """Provide a unique session ID and redirect cache to tmp_path."""
    monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
    return "test-parent-session"


@pytest.fixture()
def agent_id():
    return "agent-worker-abc123"


# ===========================================================================
# Part 1: parse-hook-stdin.py extracts agent_id
# ===========================================================================


class TestParseHookStdinAgentId:
    """Verify parse-hook-stdin.py extracts agent_id and agent_type."""

    def test_agent_id_extracted_from_envelope(self, tmp_path):
        """Envelope with agent_id should produce agent_id in output."""
        import subprocess

        envelope = json.dumps({
            "session_id": "parent-session",
            "agent_id": "agent-xyz",
            "agent_type": "general-purpose",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.txt", "content": "test"},
        })
        result = subprocess.run(
            [sys.executable, PARSE_HOOK_PATH],
            input=envelope, capture_output=True, text=True,
        )
        parsed = json.loads(result.stdout)
        assert parsed["agent_id"] == "agent-xyz"
        assert parsed["agent_type"] == "general-purpose"

    def test_no_agent_id_produces_empty_string(self, tmp_path):
        """Envelope without agent_id should produce empty strings."""
        import subprocess

        envelope = json.dumps({
            "session_id": "parent-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.txt", "content": "test"},
        })
        result = subprocess.run(
            [sys.executable, PARSE_HOOK_PATH],
            input=envelope, capture_output=True, text=True,
        )
        parsed = json.loads(result.stdout)
        assert parsed["agent_id"] == ""
        assert parsed["agent_type"] == ""


# ===========================================================================
# Part 2: Session ID routing prefers agent_id
# ===========================================================================


class TestSessionIdRouting:
    """Verify cmd_can_write resolves to agent_id-based session when present."""

    def _call_can_write(
        self, session_id, file_path, monkeypatch, capsys,
        agent_id="", skill_dir=""
    ):
        """Call cmd_can_write with a synthetic envelope including agent_id."""
        capsys.readouterr()
        envelope = {
            "tool_input": {"file_path": file_path},
            "session_id": session_id,
        }
        if agent_id:
            envelope["agent_id"] = agent_id
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(envelope)))
        writ_session.cmd_can_write(session_id if not agent_id else agent_id, skill_dir or SKILL_DIR)
        out = capsys.readouterr().out.strip()
        return json.loads(out)

    def test_parent_session_used_when_no_agent_id(
        self, session_id, monkeypatch, capsys
    ):
        """Without agent_id, session resolves to parent session_id."""
        writ_session.cmd_mode(session_id, "set", "conversation")
        result = self._call_can_write(
            session_id, "/tmp/test.py", monkeypatch, capsys
        )
        assert result["decision"] == "allow"

    def test_agent_session_isolated_from_parent(
        self, session_id, agent_id, monkeypatch, capsys, tmp_path
    ):
        """Agent_id-based session has independent state from parent."""
        # Parent is in conversation mode
        writ_session.cmd_mode(session_id, "set", "conversation")
        # Agent session is in work mode with no gates
        writ_session.cmd_mode(agent_id, "set", "work")

        parent_cache = writ_session._read_cache(session_id)
        agent_cache = writ_session._read_cache(agent_id)

        assert parent_cache["mode"] == "conversation"
        assert agent_cache["mode"] == "work"


# ===========================================================================
# Part 3: Isolated session cache creation
# ===========================================================================


class TestIsolatedCacheCreation:
    """Verify SubagentStart creates fresh session caches for workers."""

    def test_fresh_cache_has_full_budget(self, session_id, agent_id, tmp_path, monkeypatch):
        """New agent session should have full RAG budget (8000)."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        writ_session.cmd_mode(agent_id, "set", "work")
        cache = writ_session._read_cache(agent_id)
        assert cache["remaining_budget"] == 8000

    def test_fresh_cache_has_empty_loaded_rules(self, session_id, agent_id, tmp_path, monkeypatch):
        """New agent session should have no loaded rules."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        writ_session.cmd_mode(agent_id, "set", "work")
        cache = writ_session._read_cache(agent_id)
        assert cache["loaded_rule_ids"] == []

    def test_fresh_cache_has_empty_denial_counts(self, session_id, agent_id, tmp_path, monkeypatch):
        """New agent session should have no denial history."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        writ_session.cmd_mode(agent_id, "set", "work")
        cache = writ_session._read_cache(agent_id)
        assert cache.get("denial_counts", {}) == {}

    def test_parent_and_agent_caches_are_separate_files(
        self, session_id, agent_id, tmp_path, monkeypatch
    ):
        """Parent and agent should have different cache file paths."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        writ_session.cmd_mode(session_id, "set", "conversation")
        writ_session.cmd_mode(agent_id, "set", "work")

        parent_path = os.path.join(str(tmp_path), f"writ-session-{session_id}.json")
        agent_path = os.path.join(str(tmp_path), f"writ-session-{agent_id}.json")

        assert os.path.exists(parent_path)
        assert os.path.exists(agent_path)
        assert parent_path != agent_path


# ===========================================================================
# Part 4: Friction log agent_id attribution
# ===========================================================================


class TestFrictionLogAgentId:
    """Verify friction events include agent_id when available."""

    def test_friction_event_includes_agent_id(
        self, agent_id, tmp_path, monkeypatch
    ):
        """Friction events logged with agent_id should include it."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)

        writ_session._log_friction_event(
            agent_id, "work", "write_attempt",
            file_path="/tmp/test.py", result="allow",
            agent_id=agent_id,
        )

        log_path = tmp_path / "workflow-friction.log"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        assert len(events) >= 1
        assert events[-1].get("agent_id") == agent_id

    def test_friction_event_without_agent_id(
        self, session_id, tmp_path, monkeypatch
    ):
        """Friction events from parent should not have agent_id."""
        monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir(exist_ok=True)

        writ_session._log_friction_event(
            session_id, "work", "write_attempt",
            file_path="/tmp/test.py", result="allow",
        )

        log_path = tmp_path / "workflow-friction.log"
        events = [json.loads(line) for line in log_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        assert events[-1].get("agent_id") is None
