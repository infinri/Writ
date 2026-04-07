"""Tests for writ-session.py feedback loop features.

C1: Full rule objects stored in session cache
C3: Pending violations tracking
C5: Gate invalidation with structured failure context
C7: Cycle counter with escalation at 3
C8: Differential diagnosis at escalation
"""

import json
import os
import subprocess
import tempfile

import pytest

HELPER = os.path.join(os.path.dirname(__file__), "..", "bin", "lib", "writ-session.py")


def run_session(cmd: list[str], stdin_data: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", HELPER] + cmd,
        capture_output=True,
        text=True,
        timeout=5,
        input=stdin_data,
    )


@pytest.fixture
def session_id():
    """Unique session ID per test."""
    import uuid
    sid = f"test-{uuid.uuid4().hex[:12]}"
    yield sid
    # Cleanup
    path = os.path.join(tempfile.gettempdir(), f"writ-session-{sid}.json")
    if os.path.exists(path):
        os.remove(path)


def read_cache(session_id: str) -> dict:
    result = run_session(["read", session_id])
    return json.loads(result.stdout)


# ── C1: Rule object storage ──────────────────────────────────────────────────


class TestRuleObjectStorage:

    def test_add_rule_objects_stores_full_content(self, session_id):
        rules = [{"rule_id": "SEC-UNI-003", "trigger": "when returning entity data",
                   "statement": "explicit field selection", "violation": "toArray()",
                   "pass_example": "responseFactory->create()", "enforcement": "grep",
                   "domain": "security", "severity": "high"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules)])
        cache = read_cache(session_id)
        assert len(cache["loaded_rules"]) == 1
        stored = cache["loaded_rules"][0]
        assert stored["rule_id"] == "SEC-UNI-003"
        assert stored["trigger"] == "when returning entity data"
        assert stored["violation"] == "toArray()"
        assert stored["domain"] == "security"

    def test_add_rule_objects_deduplicates_by_rule_id(self, session_id):
        rules = [{"rule_id": "ARCH-DI-001", "statement": "v1"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules)])
        rules2 = [{"rule_id": "ARCH-DI-001", "statement": "v2"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules2)])
        cache = read_cache(session_id)
        assert len(cache["loaded_rules"]) == 1

    def test_add_rule_objects_merges_with_existing(self, session_id):
        rules1 = [{"rule_id": "RULE-A", "statement": "a"}]
        rules2 = [{"rule_id": "RULE-B", "statement": "b"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules1)])
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules2)])
        cache = read_cache(session_id)
        ids = {r["rule_id"] for r in cache["loaded_rules"]}
        assert ids == {"RULE-A", "RULE-B"}

    def test_loaded_rules_survives_other_updates(self, session_id):
        rules = [{"rule_id": "TEST-001", "statement": "test"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules)])
        run_session(["update", session_id, "--cost", "100"])
        run_session(["update", session_id, "--add-file", "/tmp/test.php"])
        cache = read_cache(session_id)
        assert len(cache["loaded_rules"]) == 1
        assert cache["loaded_rules"][0]["rule_id"] == "TEST-001"


# ── C3: Pending violations ────────────────────────────────────────────────────


class TestPendingViolations:

    def test_add_pending_violation_appends(self, session_id):
        run_session(["add-pending-violation", session_id,
                      "--rule", "SEC-UNI-003", "--file", "/tmp/a.php",
                      "--line", "47", "--evidence", "toArray() call"])
        cache = read_cache(session_id)
        assert len(cache["pending_violations"]) == 1
        assert cache["pending_violations"][0]["rule_id"] == "SEC-UNI-003"

    def test_add_pending_violation_deduplicates_exact_triple(self, session_id):
        for _ in range(3):
            run_session(["add-pending-violation", session_id,
                          "--rule", "R1", "--file", "/f.php", "--line", "10",
                          "--evidence", "x"])
        cache = read_cache(session_id)
        assert len(cache["pending_violations"]) == 1

    def test_add_pending_violation_allows_same_rule_different_lines(self, session_id):
        run_session(["add-pending-violation", session_id,
                      "--rule", "R1", "--file", "/f.php", "--line", "10",
                      "--evidence", "x"])
        run_session(["add-pending-violation", session_id,
                      "--rule", "R1", "--file", "/f.php", "--line", "20",
                      "--evidence", "y"])
        cache = read_cache(session_id)
        assert len(cache["pending_violations"]) == 2

    def test_clear_pending_violations_empties_list(self, session_id):
        run_session(["add-pending-violation", session_id,
                      "--rule", "R1", "--file", "/f.php", "--evidence", "x"])
        run_session(["clear-pending-violations", session_id])
        cache = read_cache(session_id)
        assert cache["pending_violations"] == []

    def test_clear_pending_violations_preserves_other_fields(self, session_id):
        rules = [{"rule_id": "KEEP-001", "statement": "keep"}]
        run_session(["update", session_id, "--add-rule-objects", json.dumps(rules)])
        run_session(["add-pending-violation", session_id,
                      "--rule", "R1", "--file", "/f.php", "--evidence", "x"])
        run_session(["clear-pending-violations", session_id])
        cache = read_cache(session_id)
        assert cache["pending_violations"] == []
        assert len(cache["loaded_rules"]) == 1


# ── C5, C7: Gate invalidation ────────────────────────────────────────────────


class TestGateInvalidation:

    def test_invalidate_gate_creates_record(self, session_id):
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "SEC-UNI-003", "--file", "/tmp/a.php",
                      "--evidence", "toArray() at line 47"])
        cache = read_cache(session_id)
        records = cache["invalidation_history"]["phase-a"]
        assert len(records) == 1
        assert records[0]["rule_id"] == "SEC-UNI-003"
        assert records[0]["cycle"] == 1
        assert "timestamp" in records[0]

    def test_invalidate_gate_deletes_approved_file(self, session_id):
        with tempfile.TemporaryDirectory() as td:
            gate_dir = os.path.join(td, ".claude", "gates")
            os.makedirs(gate_dir)
            gate_file = os.path.join(gate_dir, "phase-a.approved")
            with open(gate_file, "w") as f:
                f.write("")
            assert os.path.exists(gate_file)

            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", "test",
                          "--project-root", td])
            assert not os.path.exists(gate_file)

    def test_invalidate_gate_record_written_when_file_not_deletable(self, session_id):
        # No project root / no gate file -- record should still be written
        result = run_session(["invalidate-gate", session_id, "phase-a",
                               "--rule", "R1", "--file", "/f.php",
                               "--evidence", "test",
                               "--project-root", "/nonexistent/path"])
        assert result.returncode == 0
        cache = read_cache(session_id)
        assert len(cache["invalidation_history"]["phase-a"]) == 1

    def test_invalidate_gate_increments_cycle_count(self, session_id):
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", f"R{i}", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])
        cache = read_cache(session_id)
        records = cache["invalidation_history"]["phase-a"]
        assert len(records) == 3
        assert [r["cycle"] for r in records] == [1, 2, 3]

    def test_invalidate_gate_stores_plan_hash(self, session_id):
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "R1", "--file", "/f.php",
                      "--evidence", "test", "--plan-hash", "abc123"])
        cache = read_cache(session_id)
        assert cache["invalidation_history"]["phase-a"][0]["prior_plan_hash"] == "abc123"

    def test_invalidate_gate_sets_escalation_at_three(self, session_id):
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "SAME-001", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])
        cache = read_cache(session_id)
        assert cache["escalation"]["needed"] is True
        assert cache["escalation"]["gate"] == "phase-a"

    def test_invalidate_gate_no_escalation_at_two(self, session_id):
        for i in range(2):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", f"cycle {i+1}"])
        cache = read_cache(session_id)
        assert cache["escalation"]["needed"] is False

    def test_invalidate_gate_exit_zero_on_success(self, session_id):
        # Even when escalation triggers (3 cycles), exit is 0
        for i in range(3):
            result = run_session(["invalidate-gate", session_id, "phase-a",
                                   "--rule", "R1", "--file", "/f.php",
                                   "--evidence", "test"])
        assert result.returncode == 0

    def test_invalidate_gate_exit_nonzero_on_bad_args(self, session_id):
        result = run_session(["invalidate-gate", session_id])
        assert result.returncode != 0


# ── C7, C8: Escalation and diagnosis ─────────────────────────────────────────


class TestEscalation:

    def test_check_escalation_returns_false_when_no_history(self, session_id):
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["needed"] is False

    def test_check_escalation_returns_true_after_three_cycles(self, session_id):
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", "test"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["needed"] is True
        assert data["gate"] == "phase-a"
        assert data["cycles"] == 3

    def test_diagnosis_same_rule(self, session_id):
        for _ in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "SAME-001", "--file", "/f.php",
                          "--evidence", "test"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["diagnosis"] == "same-rule"

    def test_diagnosis_different_rules(self, session_id):
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", f"DIFF-{i:03d}", "--file", "/f.php",
                          "--evidence", "test"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["diagnosis"] == "different-rules"

    def test_diagnosis_mixed(self, session_id):
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "R1", "--file", "/f.php", "--evidence", "test"])
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "R1", "--file", "/f.php", "--evidence", "test"])
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "R2", "--file", "/f.php", "--evidence", "test"])
        result = run_session(["check-escalation", session_id])
        data = json.loads(result.stdout)
        assert data["diagnosis"] == "mixed"

    def test_escalation_is_one_way(self, session_id):
        for i in range(3):
            run_session(["invalidate-gate", session_id, "phase-a",
                          "--rule", "R1", "--file", "/f.php",
                          "--evidence", "test"])
        # Read twice -- should still be needed
        r1 = json.loads(run_session(["check-escalation", session_id]).stdout)
        r2 = json.loads(run_session(["check-escalation", session_id]).stdout)
        assert r1["needed"] is True
        assert r2["needed"] is True
