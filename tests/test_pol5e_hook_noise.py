"""POL-5e: silence benign workflow hook-noise in two unrelated hooks.

Issue 1 (validate-rules.sh, PostToolUse): emits "[Writ rule compliance] <summary>"
  + exit 1 even when 0 findings are status=="violated" (only "uncertain" findings).
  Fix: gate the banner + warning-exit on a confirmed-violation count; 0 -> silent exit 0.
Issue 2 (writ-run-pending-tests.sh, Stop): emits "[ENF-TEST-001] N test failure(s)"
  + exit 1 during the testing phase, where RED skeletons are expected. Fix: suppress
  the nag when current_phase == "testing"; keep it elsewhere.

Source-shape guards prove the fix exists in the source; behavioral guards run each
hook via bash against production state, never a shared address resolved at import
time.

TRIAGE CLUSTER 2: the three behavioral tests below used to be gated by an
import-time `skipif`, backed by `rc == 0` plus a substring check that a broken
hook satisfies as easily as a correct one. `test_benign_edit_no_banner`
(renamed test_analyze_reached_for_seeded_pass_verdict below) could not even
reach the code it was named for: with no `analysis_results` seed the helper's
own `should_proceed` gate exits three steps before `/analyze` is ever called,
so its "no banner" assertion held for a reason that had nothing to do with the
violation-count gate it was written to exercise. The run-pending pair asserted
only `rc` and a substring, which is silent for the same reason a hook that
died early would be.

The synthetic failing test file the run-pending property drives moves OUTSIDE
this checkout (under tmp_path), never `<repo>/tests/`: the hook's own `pytest
<file>` subprocess would otherwise load `tests/conftest.py`, whose
`pytest_sessionstart` wipes and replays the isolated graph and whose
`pytest_sessionfinish` stops whatever daemon answers on the suite port. That
file has never executed under the old skip, so the damage described above has
never happened; un-skipping it without moving the file first would introduce
it.

RED until the two hooks are refactored (the shape classes) and RED until
tests/_hook_runner.py exists (the two behavioral functions below).
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests._hook_runner import (
    count_requests,
    hook_env,
    isolated_hook_daemon,  # noqa: F401  (imported for pytest fixture discovery)
    run_hook,
    seed_session_cache,
    verify_seeded_mode,
)

# Belt-and-suspenders with hook_env's own WRIT_FRICTION_LOG pop (see
# tests/_hook_runner.py): the phase-polarity property reads the `metrics` and
# `audit` streams back via tests/conftest.py's per-stream file layout, which a
# collapsed WRIT_FRICTION_LOG would hide behind. Same discipline
# tests/_prompt_turn.py's two consumers already carry.
pytestmark = pytest.mark.no_friction_isolation

SKILL_DIR = Path(__file__).resolve().parent.parent
VALIDATE_RULES = SKILL_DIR / "hooks" / "scripts" / "validate-rules.sh"
RUN_PENDING = SKILL_DIR / "hooks" / "scripts" / "writ-run-pending-tests.sh"
VENV_BIN = SKILL_DIR / ".venv" / "bin"

VALIDATE_SRC = VALIDATE_RULES.read_text()
RUNPENDING_SRC = RUN_PENDING.read_text()


# --------------------------------------------------------------------------- #
# helpers (test-local; not shared harness state)
# --------------------------------------------------------------------------- #
def _no_py_crash(r) -> None:
    assert "Traceback" not in r.stderr, f"python traceback:\n{r.stderr[:500]}"
    assert "SyntaxError" not in r.stderr, f"python SyntaxError:\n{r.stderr[:500]}"


def _req(method: str, path: str) -> str:
    """A precise uvicorn-access-log substring: the quoted request line up to (not
    including) the HTTP version, so `/session/{sid}` cannot accidentally match a
    longer sibling path like `/session/{sid}/mode`."""
    return f'"{method} {path} HTTP'


def _new_project(tmp_path: Path) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    return str(root)


def _analyze_control(daemon: dict, code: str, file_path: str) -> dict:
    """A direct /analyze call to the SAME daemon with the SAME content, bypassing
    the hook entirely. This is the control that makes a silent hook run
    attributable to the verdict rather than to an outage: if the hook's own
    /analyze call and this one could disagree, silence would prove nothing."""
    body = json.dumps({
        "code": code, "file_path": file_path, "phase": "code_generation", "context": "",
    }).encode()
    request = urllib.request.Request(
        f"{daemon['base_url']}/analyze", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read())


# --------------------------------------------------------------------------- #
# Issue 1 -- validate-rules.sh: source-shape (the violation-count gate)
# --------------------------------------------------------------------------- #
class TestValidateRulesGate:
    def test_violated_count_gate_exists(self) -> None:
        assert "VIOLATED_COUNT" in VALIDATE_SRC, (
            "validate-rules.sh must count confirmed (status=='violated') findings"
        )
        assert "-gt 0" in VALIDATE_SRC, "the gate must compare the violation count"

    def test_banner_computed_after_gate(self) -> None:
        gate = VALIDATE_SRC.find("VIOLATED_COUNT")
        banner = VALIDATE_SRC.find("[Writ rule compliance]")
        assert gate != -1 and banner != -1, "both the gate and the banner must exist"
        assert gate < banner, "the violation-count gate must precede the compliance banner"

    def test_warning_exit_conditioned(self) -> None:
        assert "WARN_EXIT" in VALIDATE_SRC, (
            "the warning-mode exits must use a violation-count-conditioned code, "
            "not a bare `exit 1`"
        )
        assert ('exit "$WARN_EXIT"' in VALIDATE_SRC) or ("exit $WARN_EXIT" in VALIDATE_SRC)


# --------------------------------------------------------------------------- #
# Issue 1 -- behavioral: /analyze is actually reached, and a clean verdict
# produces no banner
# --------------------------------------------------------------------------- #
def test_analyze_reached_for_seeded_pass_verdict(isolated_hook_daemon, tmp_path) -> None:
    """WAS test_benign_edit_no_banner. Renamed to say what it proves: today it
    cannot reach the code it is named for, for two independent reasons, both
    early exits that guarantee the banner's absence without exercising the
    violation-count gate at all.
    `validate-rules-helper.py::cmd_pre_analyze` sets `should_proceed` False
    whenever the cache is non-empty and `analysis_results[file] != "pass"`, so
    a seeded work-mode session with no `analysis_results` entry exits three
    gates before /analyze; and with no daemon the /analyze curl returns empty
    and the hook exits before the violation-count gate either way.

    Seeded `analysis_results = {str(file): "pass"}` beside `mode=work`, which
    is the production shape (validate-file.sh records that verdict before the
    write): `POST /analyze` delta is 1, which is what PROVES the seed worked
    and is the in-run positive for the absence that follows; no
    `[Writ rule compliance]` banner in stderr; no `[Writ] /analyze errored`
    line (a server-side error must not masquerade as a clean pass); and a
    direct control call to the SAME daemon with the SAME content shows zero
    findings at status=="violated", which is what makes the silence
    attributable to the verdict rather than to an outage.

    COVERAGE LIMIT, stated rather than resolved: the VIOLATED_COUNT gate
    (hook 225-247) is only REACHED on a warn or fail verdict; a `pass` verdict
    exits earlier. An isolated daemon starts in calibration mode (a fresh log
    root has no calibration.jsonl), where escalation always happens and an
    unusable LLM client returns UNCERTAIN findings for every rule, which
    computes to `warn` with zero confirmed violations -- precisely the POL-5e
    scenario -- but this is a fact to MEASURE against the running daemon, not
    to assume. If the measured verdict is warn, the mutation `-gt 0` to
    `-ge 0` in validate-rules.sh:225,228 reddens this test; if it measures
    `pass` instead, that mutation does NOT reach this test, which then stands
    on the four assertions above alone. Record which of the two it was in the
    commit message rather than glossing over the limit.
    """
    daemon = isolated_hook_daemon
    cache_dir = daemon["health"]["cache_dir"]
    sid = f"test-5e-vr-{uuid.uuid4().hex[:8]}"
    f = tmp_path / "benign.py"
    code = "x = 1\n"
    f.write_text(code)

    seed_session_cache(cache_dir, sid, "work", analysis_results={str(f): "pass"})
    verify_seeded_mode(daemon, sid, "work")

    env = hook_env(daemon)
    cwd = _new_project(tmp_path)
    envelope = json.dumps({
        "session_id": sid, "hook_event_name": "PostToolUse",
        "tool_name": "Edit", "tool_input": {"file_path": str(f)},
    })

    before_analyze = count_requests(daemon, _req("POST", "/analyze"))
    r = run_hook(VALIDATE_RULES, envelope, env=env, cwd=cwd, timeout=30)
    after_analyze = count_requests(daemon, _req("POST", "/analyze"))

    assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
    _no_py_crash(r)
    assert after_analyze - before_analyze == 1, (
        f"the seeded analysis_results pass must let the hook reach /analyze "
        f"exactly once; delta={after_analyze - before_analyze}"
    )
    assert "[Writ rule compliance]" not in r.stderr, (
        f"a clean verdict must not emit the compliance banner; stderr={r.stderr[:300]!r}"
    )
    assert "[Writ] /analyze errored" not in r.stderr, (
        f"a server-side error must not masquerade as a clean pass; stderr={r.stderr[:300]!r}"
    )

    control = _analyze_control(daemon, code, str(f))
    violated = [x for x in control.get("findings", []) if x.get("status") == "violated"]
    assert violated == [], (
        f"control /analyze call (same daemon, same content) found confirmed "
        f"violations, so this test's silence would not be attributable to the "
        f"verdict: {violated!r}"
    )


# --------------------------------------------------------------------------- #
# Issue 2 -- writ-run-pending-tests.sh: phase gate (source-shape)
# --------------------------------------------------------------------------- #
class TestRunPendingTestsPhaseGateShape:
    def test_reads_phase_and_gates_on_testing(self) -> None:
        assert "current-phase" in RUNPENDING_SRC, (
            "the Stop hook must read the session phase"
        )
        assert "testing" in RUNPENDING_SRC, (
            "the Stop hook must gate the failure nag on the testing phase"
        )


# --------------------------------------------------------------------------- #
# Issue 2 -- behavioral: phase polarity. No daemon at all: is_work_mode reads
# the cache FILE, current-phase is a local writ-session.py CLI call, and both
# loggers (log_friction_event, log_gate_decision) write through local python.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "phase, expect_rc, expect_nag",
    [
        pytest.param("testing", 0, False, id="testing-suppresses"),
        pytest.param("implementation", 1, True, id="implementation-nags"),
    ],
)
def test_run_pending_tests_phase_polarity(tmp_path, phase, expect_rc, expect_nag) -> None:
    """Collapses test_testing_phase_suppresses_nag and
    test_implementation_phase_still_nags into one property. Both rows assert
    the same three POSITIVES, which is what removes the vacuity from the
    suppressed row: the run log names the failing test and a pytest failure
    line (the runner RAN and FAILED, so silence is a decision, not an early
    exit); a `hook_execution` friction row for writ-run-pending-tests carries a
    non-zero `result_code` and `resolved_count` 1; and the marker file is
    truncated (consumed). Only the polarity then differs: `testing` exits 0
    with no ENF-TEST-001 and no "test failure" in the combined output and ZERO
    new `gate_decision` rows for gate `pending-tests`; `implementation` exits 1
    with ENF-TEST-001 present and EXACTLY ONE new `pending-tests` deny row.

    The synthetic failing test file lives under tmp_path, OUTSIDE this
    checkout, so the hook's nested `pytest <file>` subprocess loads no
    conftest.py and cannot wipe the isolated graph or stop a daemon; `*` in
    the resolve-test glob (`*/tests/test_*.py`) spans `/`, so a `tests/`
    directory under tmp_path still matches. The marker and the run log live
    under this test's own tmp cache dir, never `<repo>/cache/`.

    Mutation: adding `testing` to the phase case at hook line ~152 reddens the
    `testing-suppresses` row (it would nag instead of suppressing). Replacing
    the synthetic failing test with a passing one reddens BOTH rows on their
    shared positives (no run log naming a failure, no non-zero result_code),
    which is the proof those positives are not decoration.
    """
    from writ.shared.logging import read_streams, resolve_project

    sid = f"test-5e-rpt-{uuid.uuid4().hex[:8]}"
    cache_dir = tmp_path / "cache"
    marker_dir = cache_dir / sid
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "pending-tests.txt"

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_file = test_dir / f"test_pol5e_tmpfail_{uuid.uuid4().hex[:8]}.py"
    test_file.write_text("def test_pol5e_intentional_fail():\n    assert False\n")
    marker.write_text(str(test_file) + "\n")

    seed_session_cache(str(cache_dir), sid, "work", current_phase=phase)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    cwd = str(project_root)

    env = hook_env(None, cache_dir=str(cache_dir))
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"

    project = resolve_project(cwd)
    before_metrics = read_streams(project, ["metrics"])
    before_audit = read_streams(project, ["audit"])

    r = run_hook(
        RUN_PENDING, json.dumps({"session_id": sid}), env=env, cwd=cwd, timeout=90,
    )

    after_metrics = read_streams(project, ["metrics"])
    after_audit = read_streams(project, ["audit"])
    new_metrics = after_metrics[len(before_metrics):]
    new_audit = after_audit[len(before_audit):]

    combined = r.stdout + r.stderr
    log_path = marker_dir / "last-test-run.log"
    log_text = log_path.read_text() if log_path.exists() else ""

    # --- the three shared positives: the runner RAN and FAILED ---
    assert log_path.exists(), f"last-test-run.log must exist under {marker_dir}"
    assert "test_pol5e_intentional_fail" in log_text, (
        f"the run log must name the failing test; log={log_text[:400]!r}"
    )
    assert ("FAILED" in log_text) or ("assert False" in log_text) or ("AssertionError" in log_text), (
        f"the run log must carry a pytest failure line; log={log_text[:400]!r}"
    )

    hook_exec_rows = [
        e for e in new_metrics
        if e.get("event") == "hook_execution" and e.get("hook_name") == "writ-run-pending-tests"
    ]
    assert hook_exec_rows, (
        f"a hook_execution row for writ-run-pending-tests must be recorded; "
        f"new metrics={new_metrics!r}"
    )
    assert hook_exec_rows[-1].get("result_code") not in (0, None), (
        f"result_code must be non-zero (the runner failed); row={hook_exec_rows[-1]!r}"
    )
    assert hook_exec_rows[-1].get("resolved_count") == 1, (
        f"resolved_count must be 1 (one marker entry resolved); row={hook_exec_rows[-1]!r}"
    )

    assert marker.read_text() == "", "the marker file must be truncated (consumed) after the run"

    # --- the polarity ---
    gate_rows = [
        e for e in new_audit
        if e.get("event") == "gate_decision" and e.get("gate") == "pending-tests"
    ]
    if expect_nag:
        assert r.returncode == expect_rc, f"exit {r.returncode}; out={combined[:400]!r}"
        assert "ENF-TEST-001" in combined, (
            f"a failure outside the testing phase must nag; out={combined[:400]!r}"
        )
        deny_rows = [g for g in gate_rows if g.get("decision") == "deny"]
        assert len(deny_rows) == 1, (
            f"expected exactly one new pending-tests deny row; got {deny_rows!r}"
        )
    else:
        assert r.returncode == expect_rc, f"exit {r.returncode}; out={combined[:400]!r}"
        assert "ENF-TEST-001" not in combined, (
            f"testing-phase RED tests must not surface as a Stop error; out={combined[:400]!r}"
        )
        assert "test failure" not in combined, f"out={combined[:400]!r}"
        assert gate_rows == [], (
            f"the testing phase must not deny the pending-tests gate; got {gate_rows!r}"
        )
