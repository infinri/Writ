"""Tests for the confidence metrics command in writ-session.py.

Tests cmd_metrics which reads workflow-friction.log and produces
enforcement quality reports.
"""

import json
import os
import subprocess
import tempfile
import pytest

SESSION_HELPER = "bin/lib/writ-session.py"


def _write_friction_log(events: list[dict]) -> str:
    """Write events to a temporary friction log and return the path."""
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


def _run_metrics(log_path: str) -> dict:
    result = subprocess.run(
        ["python3", SESSION_HELPER, "metrics", "--log", log_path],
        capture_output=True, text=True, timeout=5,
    )
    return json.loads(result.stdout)


# -- Clean run rate -----------------------------------------------------------

class TestCleanRunRate:
    def test_all_clean_sessions(self):
        """100% clean rate when no gate_denied_then_approved events exist."""
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition", "from_phase": "planning", "to_phase": "testing"},
            {"session": "s2", "tier": 1, "event": "phase_transition", "from_phase": None, "to_phase": "implementation"},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            assert report["clean_run_rate"] == 100.0
        finally:
            os.unlink(path)

    def test_mixed_sessions(self):
        """Correct percentage when some sessions have invalidations."""
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition"},
            {"session": "s2", "tier": 2, "event": "gate_denied_then_approved", "gate": "phase-a", "denials": 1},
            {"session": "s3", "tier": 1, "event": "phase_transition"},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            # 2 clean out of 3 = 66.7%
            assert report["clean_run_rate"] == 66.7
        finally:
            os.unlink(path)

    def test_no_data(self):
        """Returns error when friction log is empty."""
        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        try:
            result = subprocess.run(
                ["python3", SESSION_HELPER, "metrics", "--log", path],
                capture_output=True, text=True, timeout=5,
            )
            report = json.loads(result.stdout)
            assert "error" in report
        finally:
            os.unlink(path)


# -- Phase transition time ----------------------------------------------------

class TestPhaseTransitionTime:
    def test_average_transition_time(self):
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition_time", "from_gate": "phase-a", "to_gate": "test-skeletons", "elapsed_seconds": 100},
            {"session": "s2", "tier": 2, "event": "phase_transition_time", "from_gate": "phase-a", "to_gate": "test-skeletons", "elapsed_seconds": 200},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            assert report["transition_times"]["avg"] == 150.0
        finally:
            os.unlink(path)

    def test_p50_and_p90(self):
        events = [
            {"session": f"s{i}", "tier": 2, "event": "phase_transition_time", "elapsed_seconds": i * 10}
            for i in range(1, 21)  # 10, 20, ..., 200
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            stats = report["transition_times"]
            assert stats["count"] == 20
            assert stats["p50"] == 110  # index 10 of sorted [10..200]
            assert stats["p90"] == 190  # index 18 of sorted [10..200]
        finally:
            os.unlink(path)

    def test_single_transition(self):
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition_time", "elapsed_seconds": 42},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            stats = report["transition_times"]
            assert stats["p50"] == 42
            assert stats["p90"] == 42
        finally:
            os.unlink(path)


# -- Friction event frequency -------------------------------------------------

class TestEventFrequency:
    def test_counts_by_event_type(self):
        events = [
            {"session": "s1", "tier": 2, "event": "approval_pattern_miss", "prompt": "ok go"},
            {"session": "s1", "tier": 2, "event": "approval_pattern_miss", "prompt": "sure"},
            {"session": "s1", "tier": 2, "event": "hallucinated_rule_ids"},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            assert report["event_frequency"]["approval_pattern_miss"] == 2
            assert report["event_frequency"]["hallucinated_rule_ids"] == 1
        finally:
            os.unlink(path)

    def test_includes_all_known_event_types(self):
        events = [{"session": "s1", "tier": 1, "event": "phase_transition"}]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            freq = report["event_frequency"]
            for key in ["approval_pattern_miss", "gate_denied_then_approved",
                        "tier_escalated", "phase_transition_time",
                        "phase_transition", "hallucinated_rule_ids",
                        "agent_self_approval_blocked"]:
                assert key in freq
        finally:
            os.unlink(path)


# -- Tier distribution --------------------------------------------------------

class TestTierDistribution:
    def test_counts_unique_sessions_per_tier(self):
        events = [
            {"session": "s1", "tier": 1, "event": "phase_transition"},
            {"session": "s2", "tier": 2, "event": "phase_transition"},
            {"session": "s3", "tier": 2, "event": "phase_transition"},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            assert report["tier_distribution"]["1"] == 1
            assert report["tier_distribution"]["2"] == 2
        finally:
            os.unlink(path)

    def test_escalated_session_counted_at_final_tier(self):
        events = [
            {"session": "s1", "tier": 1, "event": "phase_transition"},
            {"session": "s1", "tier": 2, "event": "tier_escalated", "old_tier": 1, "new_tier": 2},
        ]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            assert report["tier_distribution"]["1"] == 0
            assert report["tier_distribution"]["2"] == 1
        finally:
            os.unlink(path)


# -- Output format ------------------------------------------------------------

class TestOutputFormat:
    def test_json_output(self):
        events = [{"session": "s1", "tier": 1, "event": "phase_transition"}]
        path = _write_friction_log(events)
        try:
            result = subprocess.run(
                ["python3", SESSION_HELPER, "metrics", "--log", path],
                capture_output=True, text=True, timeout=5,
            )
            parsed = json.loads(result.stdout)
            assert isinstance(parsed, dict)
        finally:
            os.unlink(path)

    def test_required_keys_present(self):
        events = [{"session": "s1", "tier": 1, "event": "phase_transition"}]
        path = _write_friction_log(events)
        try:
            report = _run_metrics(path)
            for key in ["clean_run_rate", "transition_times", "event_frequency", "tier_distribution"]:
                assert key in report
        finally:
            os.unlink(path)
