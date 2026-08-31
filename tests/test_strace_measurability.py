"""A measurement that cannot run must fail, never skip (plan.md, cycle 9).

Pins capabilities.md's "A measurement that cannot run must fail" section.
Nine sites across seven files today do:

    if not trace.exists():
        pytest.skip("strace produced no trace")

which has a hole worse than the skip itself: an EMPTY trace file also passes
`trace.exists()`, and a negative assertion like
`assert "approval_evidence" not in text` (tests/test_approval_evidence.py:616)
then passes VACUOUSLY against an empty string. A vacuous pass is worse than a
skip, because a skip at least shows up in the run summary.

`tests/_strace.py` replaces all nine sites with one function:

    trace_execve(argv, *, input=None, cwd=None, env=None, timeout=180,
                  attempts=2) -> str

returning the trace text, and raising `UnmeasurableTrace` (a RuntimeError
subclass, deliberately not pytest's Skipped, so no pytest.skip machinery is
involved) after the final attempt when the trace is missing, empty, or holds
no `execve(` line. It also owns `unmeasurable_skip_sites(*, tests_dir)`, the
source scan that must return empty once the nine sites convert.

THREE UNMEASURABLE STATES, each pinned separately below, because the plan is
explicit that "empty" and "no execve line" are NOT the same defect as
"missing" and each needs its own test to close:
  1. the trace file was never created (today's only checked case)
  2. the trace file exists and is empty (today's vacuity hole)
  3. the trace file exists, is non-empty, and holds no execve( line

WHAT STAYS A SKIP. `shutil.which("strace") is None` is a fact about the
machine, not a failed measurement, and must keep skipping.

RED TODAY. `tests/_strace.py` does not exist. Every test below calls
`_strace_module()` / `_require_seam()` first, which turn the resulting
ImportError/AttributeError into a `pytest.fail("skeleton: ...")` naming
exactly what is missing, rather than an unrelated traceback or, worse, a
skip that would hide the very defect class this file exists to close.

ASSUMPTION THIS FILE MAKES, stated once rather than nine times: the fake
`subprocess.run` doubles below locate the trace path by finding `"-o"` in the
argv `trace_execve` passes to the real `subprocess.run` and reading the next
element. Every one of the nine existing call sites already builds its strace
invocation this way (`["strace", ..., "-e", "trace=execve", "-o", str(trace),
...]`), so `tests/_strace.py` consolidating them is overwhelmingly likely to
keep that convention; if it does not, the affected tests below fail with a
`ValueError` naming the missing "-o", which is itself a loud, diagnosable
signal rather than a silent pass.

Run: .venv/bin/python -m pytest tests/test_strace_measurability.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_STRACE_PRESENT = pytest.mark.skipif(
    shutil.which("strace") is None, reason="strace unavailable; cannot trace execve"
)


def _strace_module():
    try:
        import tests._strace as strace_mod
    except ImportError as exc:
        pytest.fail(f"skeleton: tests/_strace.py does not exist yet ({exc})")
    return strace_mod


def _require_seam(module, name: str):
    if not hasattr(module, name):
        pytest.fail(f"skeleton: {module.__name__}.{name} is not implemented yet")
    return getattr(module, name)


class _FakeResult:
    """A minimal subprocess.CompletedProcess double for a failed strace run."""

    def __init__(self, returncode: int = 1, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class _RecordingStrace:
    """A `subprocess.run` double that records every invocation's argv and lets
    the test decide what (if anything) lands at the traced `-o` path.

    One home for the argv-parsing assumption stated in the module docstring,
    so every test below shares it rather than re-deriving it nine times.
    """

    def __init__(self, write) -> None:
        self.calls: list[list[str]] = []
        self._write = write

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        idx = argv.index("-o")
        path = Path(argv[idx + 1])
        self._write(path, len(self.calls))
        return _FakeResult(
            returncode=1, stderr=f"synthetic strace failure, attempt {len(self.calls)}"
        )


def _patch_strace(monkeypatch, strace_mod, write) -> _RecordingStrace:
    fake = _RecordingStrace(write)
    monkeypatch.setattr(strace_mod.subprocess, "run", fake)
    return fake


# ---------------------------------------------------------------------------
# The helper returns real trace text when there is real trace text.
# ---------------------------------------------------------------------------


@_STRACE_PRESENT
class TestTraceExecveHappyPath:
    def test_returns_the_trace_text_for_a_real_command(self) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")

        text = trace_execve(["true"])

        assert isinstance(text, str)
        assert "execve(" in text, (
            f"a real command traced by real strace must produce at least one "
            f"execve( line; got: {text[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Unmeasurable state 1 of 3: the trace file is never created at all.
# ---------------------------------------------------------------------------


class TestUnmeasurableOnMissingFile:
    def test_missing_trace_file_raises_after_the_final_attempt(self, monkeypatch) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        fake = _patch_strace(monkeypatch, strace_mod, write=lambda path, n: None)

        with pytest.raises(unmeasurable):
            trace_execve(["true"])

        assert fake.calls, "the helper never invoked strace at all"

    def test_the_raised_type_is_a_runtime_error_not_a_pytest_skip(self) -> None:
        """Skipped/OutcomeException is a BaseException, not an Exception, in
        every supported pytest version, so asserting RuntimeError-ness alone
        already proves this is not pytest's skip machinery, with no need to
        import pytest's internal exception module."""
        strace_mod = _strace_module()
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        assert issubclass(unmeasurable, RuntimeError), (
            "UnmeasurableTrace must be a RuntimeError subclass (plan.md: "
            "'so no pytest.skip machinery is involved')"
        )


# ---------------------------------------------------------------------------
# Unmeasurable state 2 of 3: the trace file exists and is EMPTY. This is the
# vacuity hole named explicitly in the plan: today only Path.exists() is
# checked, so this state currently passes as measured.
# ---------------------------------------------------------------------------


class TestUnmeasurableOnEmptyFile:
    def test_empty_trace_file_raises_rather_than_returning_empty_text(
        self, monkeypatch
    ) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        _patch_strace(monkeypatch, strace_mod, write=lambda path, n: path.write_text(""))

        with pytest.raises(unmeasurable):
            trace_execve(["true"])

    def test_an_empty_trace_can_no_longer_pass_a_negative_assertion_vacuously(
        self, monkeypatch
    ) -> None:
        """Direct pin of the hole named in the plan: `"approval_evidence" not
        in text` is trivially true against an empty string, so the guard
        that used to be `if not trace.exists()` let that assertion pass with
        no measurement having happened at all. Raising instead means the
        caller never reaches that assertion on an empty trace."""
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        _patch_strace(monkeypatch, strace_mod, write=lambda path, n: path.write_text(""))

        with pytest.raises(unmeasurable):
            text = trace_execve(["true"])
            # Unreachable if the fix is correct; documents the exact
            # vacuous-pass shape being closed if it is not.
            assert "approval_evidence" not in text


# ---------------------------------------------------------------------------
# Unmeasurable state 3 of 3: the trace file exists, is non-empty, and holds
# no execve( line (e.g. only strace's own exit banner).
# ---------------------------------------------------------------------------


class TestUnmeasurableOnNoExecveLine:
    def test_a_trace_with_no_execve_line_raises(self, monkeypatch) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        _patch_strace(
            monkeypatch, strace_mod,
            write=lambda path, n: path.write_text("+++ exited with 0 +++\n"),
        )

        with pytest.raises(unmeasurable):
            trace_execve(["true"])


# ---------------------------------------------------------------------------
# The failure message names the cause, not just "unmeasurable".
# ---------------------------------------------------------------------------


class TestFailureMessageNamesTheCause:
    def test_message_names_exit_status_stderr_tail_and_attempt_count(
        self, monkeypatch
    ) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        def _run(argv, **kwargs):
            return _FakeResult(returncode=17, stderr="a distinctive stderr tail marker")

        monkeypatch.setattr(strace_mod.subprocess, "run", _run)

        with pytest.raises(unmeasurable) as excinfo:
            trace_execve(["true"])

        message = str(excinfo.value)
        assert "17" in message, f"exit status missing from message: {message!r}"
        assert "a distinctive stderr tail marker" in message, (
            f"stderr tail missing from message: {message!r}"
        )
        assert "2" in message, f"attempt count missing from message: {message!r}"


# ---------------------------------------------------------------------------
# Retry policy: exactly two attempts, the second independent of the first.
# ---------------------------------------------------------------------------


class TestExactlyTwoAttempts:
    def test_makes_exactly_two_attempts_before_failing(self, monkeypatch) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        fake = _patch_strace(monkeypatch, strace_mod, write=lambda path, n: None)

        with pytest.raises(unmeasurable):
            trace_execve(["true"])

        assert len(fake.calls) == 2, (
            "plan.md pins two attempts (one retry): a second, INDEPENDENT "
            "trial takes an observed ~1.5 percent transient-failure rate to "
            "about 0.02 percent, while a third would only reach 0.0003 "
            f"percent at triple the worst-case cost. Got {len(fake.calls)} "
            "attempts."
        )

    def test_the_second_attempt_uses_a_fresh_trace_path(self, monkeypatch) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")
        unmeasurable = _require_seam(strace_mod, "UnmeasurableTrace")

        fake = _patch_strace(monkeypatch, strace_mod, write=lambda path, n: None)

        with pytest.raises(unmeasurable):
            trace_execve(["true"])

        paths = [call[call.index("-o") + 1] for call in fake.calls]
        assert len(paths) == 2 and paths[0] != paths[1], (
            f"the retry must use a fresh trace path rather than re-reading "
            f"the first attempt's file; got {paths!r}"
        )


# ---------------------------------------------------------------------------
# Unique per-call path across independent top-level calls (not just within
# one call's retries): a leftover file from a crashed earlier run must never
# be read as this run's measurement.
# ---------------------------------------------------------------------------


@_STRACE_PRESENT
class TestUniquePerCallTracePath:
    def test_two_successful_calls_use_two_different_paths(self, monkeypatch) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")

        seen_paths: list[str] = []
        real_run = strace_mod.subprocess.run

        def _spy(argv, **kwargs):
            if "-o" in argv:
                seen_paths.append(argv[argv.index("-o") + 1])
            return real_run(argv, **kwargs)

        monkeypatch.setattr(strace_mod.subprocess, "run", _spy)

        trace_execve(["true"])
        trace_execve(["true"])

        assert len(seen_paths) == 2 and seen_paths[0] != seen_paths[1], (
            "two independent calls must never share a trace path: a fixed "
            "path like the current /tmp/writ-budget-<hook>.trace would let a "
            f"leftover file be read as the next call's measurement; got "
            f"{seen_paths!r}"
        )


# ---------------------------------------------------------------------------
# The one skip that stays: no strace binary on this machine.
# ---------------------------------------------------------------------------


class TestMissingStraceBinaryStillSkips:
    def test_missing_binary_skips_rather_than_raising_unmeasurable(
        self, monkeypatch
    ) -> None:
        strace_mod = _strace_module()
        trace_execve = _require_seam(strace_mod, "trace_execve")

        monkeypatch.setattr(strace_mod.shutil, "which", lambda name: None)

        with pytest.raises(pytest.skip.Exception):
            trace_execve(["true"])


# ---------------------------------------------------------------------------
# The inventory: must find real sites (or it derives nothing and rots into a
# vacuous check), and a planted regression must be caught. Both directions.
# ---------------------------------------------------------------------------


class TestUnmeasurableSkipSitesInventory:
    def test_catches_a_planted_skip_in_a_synthetic_fixture(self, tmp_path) -> None:
        """Direction one: the scanner must actually find a matching site. An
        inventory that always returns [] would make the real-tree ratchet
        below pass for having derived nothing, which is worse than the
        duplication it replaces."""
        strace_mod = _strace_module()
        unmeasurable_skip_sites = _require_seam(strace_mod, "unmeasurable_skip_sites")

        planted = tmp_path / "test_synthetic_regression.py"
        planted.write_text(
            "def test_x():\n"
            "    trace = None\n"
            "    if not trace:\n"
            '        pytest.skip("strace produced no trace")\n'
        )

        sites = unmeasurable_skip_sites(tests_dir=tmp_path)

        assert sites, (
            f"planted a pytest.skip('strace produced no trace') in a "
            f"synthetic fixture at {planted} and the scanner found nothing"
        )
        assert any("test_synthetic_regression.py" in str(site) for site in sites), (
            f"expected the planted file to be named in the results; got "
            f"{sites!r}"
        )

    def test_does_not_flag_an_unrelated_skip_reason(self, tmp_path) -> None:
        """Direction two's counterpart: a genuine skip that has nothing to do
        with strace must not be a false positive."""
        strace_mod = _strace_module()
        unmeasurable_skip_sites = _require_seam(strace_mod, "unmeasurable_skip_sites")

        clean = tmp_path / "test_synthetic_clean.py"
        clean.write_text('def test_x():\n    pytest.skip("Neo4j unreachable")\n')

        sites = unmeasurable_skip_sites(tests_dir=clean.parent)

        assert sites == [], (
            f"an unrelated skip reason must not be flagged as the 'produced "
            f"no trace' shape; got {sites!r}"
        )

    def test_no_matching_skip_remains_under_the_real_tests_directory(self) -> None:
        """Direction two: the ratchet itself. RED until all nine call sites
        (tests/test_approval_evidence.py:613,645;
        tests/test_write_path_process_budget.py:173,195,321;
        tests/test_prompt_path_process_budget.py:135;
        tests/test_blackbox_record_schema.py:349;
        tests/test_hook_out_capture.py:245;
        tests/test_gate_decision_mode_stamp.py:261) convert to trace_execve."""
        strace_mod = _strace_module()
        unmeasurable_skip_sites = _require_seam(strace_mod, "unmeasurable_skip_sites")

        sites = unmeasurable_skip_sites(tests_dir=REPO_ROOT / "tests")

        assert sites == [], (
            f"pytest.skip(...'produced no trace'...) must not remain "
            f"anywhere under tests/ after the strace helper conversion; "
            f"found: {sites!r}"
        )
