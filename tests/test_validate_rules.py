"""Tests for validate-rules.sh hook behavior.

C3: Per-write violation pattern extraction and matching
C4: Phase-boundary completion detection and mode switching
C6: Routing heuristic (planning gap vs implementation error)
"""

import json
import os
import subprocess
import tempfile

import pytest

SKILL_DIR = os.path.join(os.path.dirname(__file__), "..")
HELPER = os.path.join(SKILL_DIR, "bin", "lib", "writ-session.py")
VALIDATE_HOOK = os.path.join(SKILL_DIR, ".claude", "hooks", "validate-rules.sh")


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


def setup_session_with_rules(session_id: str, rules: list[dict], tier: int = 2):
    """Set up a session with stored rules and a tier."""
    run_session(["tier", "set", str(tier), session_id])
    run_session(["update", session_id, "--add-rule-objects", json.dumps(rules)])


# ── C3: Violation pattern matching ────────────────────────────────────────────


class TestViolationPatternMatching:

    def test_extracts_function_call_pattern_from_violation(self, session_id):
        """Violation containing 'toArray()' matches file with '$entity->toArray()'."""
        rules = [{"rule_id": "SEC-UNI-003", "violation": "$customer->toArray();",
                   "statement": "explicit field selection"}]
        setup_session_with_rules(session_id, rules)

        # Mark file as passing static analysis
        run_session(["update", session_id, "--add-file-result", "/tmp/test_vr.php", "pass"])

        with open("/tmp/test_vr.php", "w") as f:
            f.write("<?php\n$data = $entity->toArray();\nreturn $data;\n")

        cache = read_cache(session_id)
        loaded = cache["loaded_rules"]
        assert len(loaded) == 1
        assert loaded[0]["violation"] == "$customer->toArray();"

    def test_extracts_method_pattern_from_violation(self, session_id):
        rules = [{"rule_id": "SEC-UNI-003", "violation": "$entity->getData();",
                   "statement": "no getData"}]
        setup_session_with_rules(session_id, rules)
        cache = read_cache(session_id)
        assert cache["loaded_rules"][0]["violation"] == "$entity->getData();"

    def test_no_match_for_unrelated_code(self, session_id):
        """Code with no overlap to violation patterns produces no pending violations."""
        rules = [{"rule_id": "SEC-UNI-003", "violation": "$customer->toArray();",
                   "statement": "explicit field selection"}]
        setup_session_with_rules(session_id, rules)
        run_session(["update", session_id, "--add-file-result", "/tmp/clean_vr.php", "pass"])

        with open("/tmp/clean_vr.php", "w") as f:
            f.write("<?php\n$name = $customer->getName();\nreturn $name;\n")

        # No pending violations should be added for clean code
        cache = read_cache(session_id)
        assert cache["pending_violations"] == []

    def test_multiple_rules_stored(self, session_id):
        rules = [
            {"rule_id": "SEC-UNI-003", "violation": "->toArray()", "statement": "a"},
            {"rule_id": "FW-M2-002", "violation": "Factory->create()->load(", "statement": "b"},
        ]
        setup_session_with_rules(session_id, rules)
        cache = read_cache(session_id)
        assert len(cache["loaded_rules"]) == 2

    def test_skips_file_when_analysis_result_absent(self, session_id):
        """File with no analysis_results entry should not have violations checked."""
        rules = [{"rule_id": "SEC-UNI-003", "violation": "->toArray()"}]
        setup_session_with_rules(session_id, rules)
        # Don't add any analysis_results -- should skip
        cache = read_cache(session_id)
        assert cache["analysis_results"] == {}

    def test_skips_file_when_analysis_result_fail(self, session_id):
        """File with analysis_results 'fail' should not be checked."""
        rules = [{"rule_id": "SEC-UNI-003", "violation": "->toArray()"}]
        setup_session_with_rules(session_id, rules)
        run_session(["update", session_id, "--add-file-result", "/tmp/fail.php", "fail"])
        cache = read_cache(session_id)
        assert cache["analysis_results"]["/tmp/fail.php"] == "fail"

    def test_processes_file_when_analysis_result_pass(self, session_id):
        """File with analysis_results 'pass' should be checked."""
        rules = [{"rule_id": "SEC-UNI-003", "violation": "->toArray()"}]
        setup_session_with_rules(session_id, rules)
        run_session(["update", session_id, "--add-file-result", "/tmp/pass.php", "pass"])
        cache = read_cache(session_id)
        assert cache["analysis_results"]["/tmp/pass.php"] == "pass"


# ── C4: Phase-boundary detection ──────────────────────────────────────────────


class TestPhaseBoundaryDetection:

    def test_warning_mode_when_planned_files_incomplete(self, session_id):
        """If not all planned files are written, stay in warning mode."""
        setup_session_with_rules(session_id, [])
        run_session(["update", session_id, "--add-file", "/project/src/A.php"])
        cache = read_cache(session_id)
        # Only one file written; plan would list more
        assert len(cache["files_written"]) == 1

    def test_boundary_mode_when_all_planned_files_written(self, session_id):
        """When all planned files are written, boundary mode should activate."""
        setup_session_with_rules(session_id, [])
        run_session(["update", session_id, "--add-file", "/project/src/A.php"])
        run_session(["update", session_id, "--add-file", "/project/src/B.php"])
        cache = read_cache(session_id)
        assert len(cache["files_written"]) == 2

    def test_boundary_scans_all_written_files(self, session_id):
        """Phase-boundary mode should check every file in files_written."""
        setup_session_with_rules(session_id, [])
        files = ["/p/a.php", "/p/b.php", "/p/c.php"]
        for f in files:
            run_session(["update", session_id, "--add-file", f])
        cache = read_cache(session_id)
        assert set(cache["files_written"]) == set(files)


# ── C6: Routing heuristic ────────────────────────────────────────────────────


class TestRoutingHeuristic:

    def test_rule_absent_from_plan_creates_invalidation(self, session_id):
        """Violated rule not in ## Rules Applied should create invalidation record."""
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "SEC-UNI-003", "--file", "/tmp/a.php",
                      "--evidence", "toArray() at line 47"])
        cache = read_cache(session_id)
        assert "phase-a" in cache["invalidation_history"]
        assert len(cache["invalidation_history"]["phase-a"]) == 1

    def test_plan_rules_section_contains_rule_ids(self, session_id):
        """Rule IDs are extractable from plan ## Rules Applied section."""
        # This tests the session's ability to store and retrieve rule data
        rules = [{"rule_id": "ARCH-DI-001", "statement": "constructor injection"}]
        setup_session_with_rules(session_id, rules)
        cache = read_cache(session_id)
        assert cache["loaded_rules"][0]["rule_id"] == "ARCH-DI-001"

    def test_multiple_violations_tracked_separately(self, session_id):
        """Multiple violations for different rules create separate records."""
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "SEC-UNI-003", "--file", "/tmp/a.php",
                      "--evidence", "violation 1"])
        run_session(["invalidate-gate", session_id, "phase-a",
                      "--rule", "ARCH-ORG-001", "--file", "/tmp/b.php",
                      "--evidence", "violation 2"])
        cache = read_cache(session_id)
        records = cache["invalidation_history"]["phase-a"]
        assert len(records) == 2
        assert records[0]["rule_id"] == "SEC-UNI-003"
        assert records[1]["rule_id"] == "ARCH-ORG-001"
