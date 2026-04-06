"""Tests for bin/lib/writ-session.py tier commands.

The session helper is a standalone stdlib-only script. We import its
functions directly to test tier get/set logic, up-only enforcement,
and cache field integration.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import importlib.util
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


@pytest.fixture()
def session_id(tmp_path, monkeypatch):
    """Provide a unique session ID and redirect cache to tmp_path."""
    monkeypatch.setattr(writ_session, "CACHE_DIR", str(tmp_path))
    return "test-tier-session"


def _read_raw_cache(tmp_path, session_id: str) -> dict:
    path = os.path.join(str(tmp_path), f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tier get: reading tier from session cache
# ---------------------------------------------------------------------------

class TestTierGet:

    def test_get_returns_empty_when_no_tier_set(self, session_id, capsys):
        """Fresh session has no tier -- get should output empty string."""
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_get_returns_tier_after_set(self, session_id, capsys):
        """After setting tier=2, get should output '2'."""
        writ_session.cmd_tier(session_id, "set", "2")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out
        assert out.strip() == "2"

    def test_get_returns_integer_string_not_json(self, session_id, capsys):
        """Output is plain '2', not '"2"' or '{"tier": 2}'."""
        writ_session.cmd_tier(session_id, "set", "2")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "get")
        out = capsys.readouterr().out.strip()
        assert out == "2"
        assert '"' not in out
        assert "{" not in out


# ---------------------------------------------------------------------------
# Tier set: writing tier to session cache
# ---------------------------------------------------------------------------

class TestTierSet:

    def test_set_valid_tier_0(self, session_id, capsys):
        """Tier 0 (Research) is a valid classification."""
        writ_session.cmd_tier(session_id, "set", "0")
        out = capsys.readouterr().out
        assert "set: 0" in out

    def test_set_valid_tier_1(self, session_id, capsys):
        """Tier 1 (Patch) is a valid classification."""
        writ_session.cmd_tier(session_id, "set", "1")
        out = capsys.readouterr().out
        assert "set: 1" in out

    def test_set_valid_tier_2(self, session_id, capsys):
        """Tier 2 (Standard) is a valid classification."""
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert "set: 2" in out

    def test_set_valid_tier_3(self, session_id, capsys):
        """Tier 3 (Complex) is a valid classification."""
        writ_session.cmd_tier(session_id, "set", "3")
        out = capsys.readouterr().out
        assert "set: 3" in out

    def test_set_rejects_negative_tier(self, session_id):
        """Tier -1 is invalid -- must reject."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "-1")

    def test_set_rejects_tier_above_3(self, session_id):
        """Tier 4 is invalid -- must reject."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "4")

    def test_set_rejects_non_integer(self, session_id):
        """Tier 'high' is invalid -- must reject."""
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "high")

    def test_set_outputs_confirmation(self, session_id, capsys):
        """Set should output 'set: N' on success."""
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert out.strip() == "set: 2"

    def test_set_persists_to_cache_file(self, session_id, tmp_path):
        """Tier value must be readable from the JSON cache file on disk."""
        writ_session.cmd_tier(session_id, "set", "2")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["tier"] == 2


# ---------------------------------------------------------------------------
# Up-only escalation enforcement
# ---------------------------------------------------------------------------

class TestTierEscalation:

    def test_escalate_1_to_2_succeeds(self, session_id, capsys):
        """Escalating from Tier 1 to Tier 2 is allowed."""
        writ_session.cmd_tier(session_id, "set", "1")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert "escalated" in out

    def test_escalate_1_to_3_succeeds(self, session_id, capsys):
        """Escalating from Tier 1 to Tier 3 is allowed (skip tiers)."""
        writ_session.cmd_tier(session_id, "set", "1")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "set", "3")
        out = capsys.readouterr().out
        assert "escalated: 1 -> 3" in out

    def test_escalate_outputs_message(self, session_id, capsys):
        """Escalation should output 'escalated: 1 -> 2'."""
        writ_session.cmd_tier(session_id, "set", "1")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert out.strip() == "escalated: 1 -> 2"

    def test_downgrade_2_to_1_rejected(self, session_id):
        """Downgrading from Tier 2 to Tier 1 must fail with exit code 1."""
        writ_session.cmd_tier(session_id, "set", "2")
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "1")

    def test_downgrade_3_to_0_rejected(self, session_id):
        """Downgrading from Tier 3 to Tier 0 must fail with exit code 1."""
        writ_session.cmd_tier(session_id, "set", "3")
        with pytest.raises(SystemExit, match="1"):
            writ_session.cmd_tier(session_id, "set", "0")

    def test_set_same_tier_is_noop(self, session_id, capsys):
        """Setting the same tier again should succeed silently (no error)."""
        writ_session.cmd_tier(session_id, "set", "2")
        capsys.readouterr()
        writ_session.cmd_tier(session_id, "set", "2")
        out = capsys.readouterr().out
        assert "set: 2" in out

    def test_set_same_tier_preserves_cache(self, session_id, tmp_path):
        """Re-setting same tier must not corrupt other cache fields."""
        writ_session.cmd_tier(session_id, "set", "2")
        # Add some other state to the cache
        writ_session.cmd_update(session_id, ["--add-rules", '["ARCH-ORG-001"]'])
        writ_session.cmd_tier(session_id, "set", "2")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["tier"] == 2
        assert "ARCH-ORG-001" in data["loaded_rule_ids"]


# ---------------------------------------------------------------------------
# Cache integration: tier field coexists with existing fields
# ---------------------------------------------------------------------------

class TestTierCacheIntegration:

    def test_tier_field_present_in_cache_after_set(self, session_id, tmp_path):
        """Cache JSON must contain 'tier' key after tier set."""
        writ_session.cmd_tier(session_id, "set", "1")
        data = _read_raw_cache(tmp_path, session_id)
        assert "tier" in data
        assert data["tier"] == 1

    def test_existing_cache_fields_preserved_after_tier_set(self, session_id, tmp_path):
        """Setting tier must not clobber loaded_rule_ids, remaining_budget, etc."""
        writ_session.cmd_update(session_id, [
            "--add-rules", '["PERF-IO-001", "SEC-UNI-002"]',
            "--cost", "240",
            "--context-percent", "30",
        ])
        writ_session.cmd_tier(session_id, "set", "2")
        data = _read_raw_cache(tmp_path, session_id)
        assert data["tier"] == 2
        assert "PERF-IO-001" in data["loaded_rule_ids"]
        assert "SEC-UNI-002" in data["loaded_rule_ids"]
        assert data["remaining_budget"] == 8000 - 240
        assert data["context_percent"] == 30

    def test_read_cache_defaults_tier_to_none(self, session_id):
        """_read_cache on a fresh session should have tier=None in the dict."""
        cache = writ_session._read_cache(session_id)
        assert cache["tier"] is None

    def test_update_command_preserves_tier(self, session_id, tmp_path):
        """Running 'update --add-rules' after 'tier set' must not erase tier."""
        writ_session.cmd_tier(session_id, "set", "3")
        writ_session.cmd_update(session_id, ["--add-rules", '["ARCH-DRY-001"]'])
        data = _read_raw_cache(tmp_path, session_id)
        assert data["tier"] == 3
        assert "ARCH-DRY-001" in data["loaded_rule_ids"]

    def test_tier_survives_multiple_updates(self, session_id, tmp_path):
        """Tier must persist through multiple update calls with other flags."""
        writ_session.cmd_tier(session_id, "set", "2")
        writ_session.cmd_update(session_id, ["--add-rules", '["R-001"]'])
        writ_session.cmd_update(session_id, ["--cost", "120"])
        writ_session.cmd_update(session_id, ["--context-percent", "50"])
        writ_session.cmd_update(session_id, ["--inc-queries"])
        data = _read_raw_cache(tmp_path, session_id)
        assert data["tier"] == 2
