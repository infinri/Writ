"""Tests for enriched feedback to Writ server on escalation.

C10: Negative feedback with cycle context.
"""

import json
import os
import subprocess
import tempfile

import pytest

HELPER = os.path.join(os.path.dirname(__file__), "..", "bin", "lib", "writ-session.py")


def run_session(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", HELPER] + cmd,
        capture_output=True, text=True, timeout=5,
    )


@pytest.fixture
def session_id():
    import uuid
    sid = f"test-{uuid.uuid4().hex[:12]}"
    yield sid
    path = os.path.join(tempfile.gettempdir(), f"writ-session-{sid}.json")
    if os.path.exists(path):
        os.remove(path)


def read_cache(session_id: str) -> dict:
    result = run_session(["read", session_id])
    return json.loads(result.stdout)


class TestEnrichedFeedback:

    def test_escalation_sets_feedback_sent_false_initially(self, session_id):
        """Escalation record starts with feedback_sent: false."""
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])
        cache = read_cache(session_id)
        assert cache["escalation"]["needed"] is True
        assert cache["escalation"]["feedback_sent"] is False

    def test_feedback_includes_cycle_context(self, session_id):
        """Escalation record contains gate, diagnosis, and cycle count."""
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "SAME-001", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["gate"] == "phase-a"
        assert data["diagnosis"] == "same-rule"
        assert data["cycles"] == 3

    def test_no_escalation_when_under_threshold(self, session_id):
        """Sessions with fewer than 3 invalidations do not escalate."""
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "R1", "--file", "/f.php", "--evidence", "c1"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["needed"] is False
        assert data["cycles"] == 1

    def test_feedback_sent_can_be_marked(self, session_id):
        """feedback_sent flag can be set in the escalation record."""
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])

        # Manually mark feedback as sent (simulating what writ-rag-inject.sh does)
        cache_path = os.path.join(tempfile.gettempdir(), f"writ-session-{session_id}.json")
        with open(cache_path) as f:
            cache = json.load(f)
        cache["escalation"]["feedback_sent"] = True
        with open(cache_path, "w") as f:
            json.dump(cache, f)

        cache = read_cache(session_id)
        assert cache["escalation"]["feedback_sent"] is True