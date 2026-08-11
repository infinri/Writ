"""Bash <-> Python parity for the session-scoped gate-dir path (Part 2, isolation cycle,
plan_hash 033bb1595c2c, capabilities 21 and 22).

The path is built in two languages on purpose (plan.md ## Analysis, Part 2): five readers
are bash, and one of them, writ-rag-inject.sh, is on the per-prompt hot path where a
python spawn costs about 19.5ms. So the shape must be constructible in bash without a
spawn, which a concatenation plus a `case` glob for the charset check is. That is a
duplicated seam, and this repo has been bitten by one before (the two `## Files` parsers;
`gate_token_path` matching the bash writer byte for byte) -- the mitigation is this parity
test, which runs both sides over the same inputs, including a rejected id, and compares
bytes.

`writ.session.locators.gate_dir` is the single python source plan.md names; `writ_gate_dir`
in bin/lib/common.sh is its bash mirror. Neither exists yet, so every test below fails at
the first call (ImportError on the python side, "command not found" on the bash side) --
the correct RED for a capability whose implementation does not exist.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

GOOD_IDS = ["abc123", "a.b-c_d", "A" * 128, "sid.2026-08-10"]
BAD_IDS = ["", "a/b", "../escape", "a" * 129, "with space", "semi;colon", ".."]


def _python_gate_dir(project_root: str, session_id: str) -> str:
    from writ.session.locators import gate_dir

    return gate_dir(project_root, session_id)


def _bash_gate_dir(project_root: str, session_id: str):
    import subprocess

    return subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail\nsource {COMMON_SH} >/dev/null 2>&1\nwrit_gate_dir "$1" "$2"',
         "_", project_root, session_id],
        capture_output=True, text=True, timeout=30,
    )


def _assert_parity(project_root: str, session_id: str, label: str) -> str:
    want = _python_gate_dir(project_root, session_id)
    proc = _bash_gate_dir(project_root, session_id)
    assert proc.returncode == 0, (
        f"{label}: writ_gate_dir aborted its caller (rc={proc.returncode}): "
        f"{proc.stderr[:200]}"
    )
    got = proc.stdout.strip()
    assert got == want, f"{label}: python said {want!r}, bash said {got!r}"
    return got


class TestByteIdenticalPaths:
    """Capability 21: `writ_gate_dir` in bash and `gate_dir` in python produce
    byte-identical paths for the same project root and session id, including for a
    rejected session id."""

    @pytest.mark.parametrize("session_id", GOOD_IDS)
    def test_a_wellformed_id_matches(self, session_id, tmp_path) -> None:
        got = _assert_parity(str(tmp_path), session_id, f"good id {session_id!r}")
        assert got != "", "a well-formed id must resolve to a real path on both sides"

    @pytest.mark.parametrize("session_id", BAD_IDS)
    def test_a_rejected_id_matches(self, session_id, tmp_path) -> None:
        """Capability 21's explicit case: "including for a rejected session id" -- both
        sides must agree on the REFUSAL too (empty string), not just on the happy path."""
        got = _assert_parity(str(tmp_path), session_id, f"bad id {session_id!r}")
        assert got == "", f"a rejected id must resolve to no path on both sides, got {got!r}"

    def test_the_python_reference_actually_distinguishes_good_from_bad(
        self, tmp_path
    ) -> None:
        """Guards the guard: if `gate_dir` always returned the same shape of answer
        regardless of validity, every parity assertion above would compare two equally
        wrong answers and call it agreement."""
        good = _python_gate_dir(str(tmp_path), "abc123")
        bad = _python_gate_dir(str(tmp_path), "../escape")
        assert good != "" and bad == "", (
            f"the python reference does not distinguish a good id from a bad one "
            f"(good={good!r}, bad={bad!r}), so the parity tests above are vacuous"
        )


class TestTheGateDirBuildIsPureShell:
    """Capability 22: the per-prompt hook path gains no python interpreter spawn. The
    bash gate-dir build and its charset check must be pure shell.

    Structural, not a timing measurement: tests/test_prompt_path_process_budget.py
    already owns the measured per-hook python-start ratchet (currently 10, per its own
    PYTHON_BUDGET constant) -- this test does not touch or raise that ratchet, it pins
    the ONE new function's own body, the same pattern
    test_project_root_bash_parity.py's TestTheSpawnIsGone uses for detect_project_root.
    """

    def _function_body(self) -> str:
        src = COMMON_SH.read_text()
        assert "writ_gate_dir() {" in src, (
            "common.sh has no writ_gate_dir function yet -- Part 2 has not landed"
        )
        start = src.index("writ_gate_dir() {")
        end = src.index("\n}\n", start)
        return src[start:end]

    def test_writ_gate_dir_does_not_spawn_python(self) -> None:
        body = self._function_body()
        assert "python3" not in body, (
            "writ_gate_dir spawns python, so the per-prompt path pays the interpreter "
            "cost this capability says it must not"
        )

    def test_the_charset_check_is_a_bash_case_glob_not_a_spawn(self) -> None:
        """The session-id validity check inside writ_gate_dir must be a `case` glob
        (bash-native) rather than `grep -E` or a python regex, which is itself a process
        spawn on every gate-dir build on the hot path."""
        body = self._function_body()
        assert "case " in body, (
            "writ_gate_dir's charset check does not look like a bash `case` glob"
        )
        assert "grep" not in body, (
            "writ_gate_dir's charset check spawns grep instead of using a bash case glob"
        )

    def test_the_reference_ratchet_is_not_raised_by_this_file(self) -> None:
        """This file's own promise, checked against the number rather than assumed: it
        must never edit tests/test_prompt_path_process_budget.py's PYTHON_BUDGET
        constant. A parity/structural test that quietly raised the shared ratchet would
        defeat the whole point of a ratchet."""
        budget_file = REPO / "tests" / "test_prompt_path_process_budget.py"
        text = budget_file.read_text()
        assert "PYTHON_BUDGET = 10" in text, (
            "the per-prompt python-start ratchet in test_prompt_path_process_budget.py "
            "has moved since this file recorded its value; re-check before adding the "
            "bash gate-dir build to writ-rag-inject.sh that this ratchet must still cover"
        )
