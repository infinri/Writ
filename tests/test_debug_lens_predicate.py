"""Skip the runtime-lens read gate's expensive check when the lens provably cannot deny.

Plan: .claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md. This module is the
CENTRAL TEST the dispatch brief demands: it never encodes the plan author's reasoning
about which (mode, source_type) pairs can deny. It runs the REAL bash predicate
(`writ_runtime_lens_check_required`, a subprocess) and the REAL python gate
(`gates._can_read_code_check`) over a matrix DERIVED from the same sources production
code reads (`VALID_MODES`, `_VALID_SOURCE_TYPES`, and the `Grep|Read|Glob` matcher in
hooks/hooks.json), and compares their verdicts case for case. Neither side is a second
implementation of the other's logic (ENF-SYS-005): a python re-implementation of the
predicate would only prove two copies of one mistake agree.

THE PREDICATE (bin/lib/common.sh, not yet written):
    writ_runtime_lens_check_required <session_id>
        exit 0  the expensive check is REQUIRED
        exit 1  the lens provably cannot deny; skipping is safe

Every test below is RED until that function and the hook's call site exist. Collection
must still succeed: every RED path fails via a real assertion (a wrong exit code, an
absent function reported by `pytest.fail`), never `pass`/`...`/`assert True`.

HARNESS TRAPS this module obeys (dispatch brief; each already cost this program time):
  1. HOME is pinned in every child env (`_predicate_required`, the firedrill harness).
  2. WRIT_FRICTION_LOG is never set here; `pytestmark` opts out of the autouse redirect
     and every stream read goes through `writ.shared.logging.read_streams`, never grep.
  3. Every seeded cache state is a FILE inside the child's own WRIT_CACHE_DIR: a
     subprocess does not share this process's state.
  4. Before any equality assertion this module makes, it asserts the precondition that
     makes the comparison meaningful (non-empty result sets, at minimum), because a
     vacuous pass is worse than a failure.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._inventory import matcher_tools_for_script
from tests.firedrill._harness import build_env, make_isolation, read_cache, run_hook, write_cache
from writ.session import gates
from writ.session.mode_engine import VALID_MODES, _VALID_SOURCE_TYPES
from writ.shared.logging import read_streams

# WRIT_FRICTION_LOG collapses every typed stream into one file, which would make every
# "landed on the audit stream" assertion below vacuous (dispatch brief, harness trap 2).
pytestmark = pytest.mark.no_friction_isolation

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "bin" / "lib" / "common.sh"
HOOK_NAME = "writ-debug-code-gate.sh"
HOOK = REPO / "hooks" / "scripts" / HOOK_NAME
FLUSH = REPO / "bin" / "lib" / "writ-flush-events.py"
GATE_NAME = "debug-code-read"

# A "pre-evidence" debug.md: both '## Evidence' and '## Narrowing' exist but carry no
# real content, so _validate_evidence_narrowing always fails and the runtime lens
# never opens by accident (tests/test_inv9_debug_code_gate.py's own EVIDENCE_EMPTY).
EVIDENCE_EMPTY = "## Symptom\nx\n\n## Evidence\n\n## Narrowing\n\n## Root cause\n\n"


# --------------------------------------------------------------------------------- #
# THE DERIVED MATRIX. Modes/source types/tools come from the same sources production
# code reads, never a hand-typed list, so a state that starts denying (a new mode with
# a static source_type of "runtime" in MODE_CONFIG, a matcher edit) enters the matrix
# automatically instead of being silently invisible to it. Counts are asserted at
# MODULE level (collection time), so a shrinking matrix fails loudly rather than
# quietly testing less (plan Decision 3).
# --------------------------------------------------------------------------------- #
MODES = sorted(VALID_MODES) + [None]
SOURCE_TYPES = sorted(_VALID_SOURCE_TYPES) + [None]
TOOLS = matcher_tools_for_script(HOOK_NAME)

assert len(MODES) == 6, f"expected 6 modes (5 declared + absent), got {sorted(MODES, key=str)}"
assert len(SOURCE_TYPES) == 4, (
    f"expected 4 source types (3 declared + absent), got {sorted(SOURCE_TYPES, key=str)}"
)
assert TOOLS, "no matcher tools derived for writ-debug-code-gate.sh from hooks/hooks.json"
assert len(TOOLS) == 3, f"expected 3 matcher tools (Grep|Read|Glob), got {TOOLS}"

CROSS_PRODUCT = list(itertools.product(MODES, SOURCE_TYPES, TOOLS))
assert len(CROSS_PRODUCT) == 72, f"expected 72 cases, derived {len(CROSS_PRODUCT)}"

_VALID_SHAPES = list(itertools.product(MODES, SOURCE_TYPES))
assert len(_VALID_SHAPES) == 24


# --------------------------------------------------------------------------------- #
# Shared fixtures / helpers.
# --------------------------------------------------------------------------------- #


def _write_cache_file(cache_dir: Path, session_id: str, mode, source_type) -> None:
    payload: dict = {}
    if mode is not None:
        payload["mode"] = mode
    if source_type is not None:
        payload["source_type"] = source_type
    (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(payload))


def _predicate_required(session_id: str, cache_dir: Path, *, no_jq: bool = False) -> bool:
    """Run the REAL bash predicate as a subprocess. True = check required, False = skip.

    HOME is pinned to a throwaway dir beside the cache (harness trap 1): the predicate
    is not expected to read blackbox capture's sentinel, but nothing in this repo's
    hooks may be trusted to leave $HOME alone by omission.
    """
    home_dir = cache_dir.parent / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir), "HOME": str(home_dir)}
    if no_jq:
        env["WRIT_NO_JQ"] = "1"
    else:
        env.pop("WRIT_NO_JQ", None)
    script = f'set -euo pipefail\nsource "{COMMON}"\nwrit_runtime_lens_check_required "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "_", session_id],
        capture_output=True, text=True, env=env, timeout=20,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    pytest.fail(
        f"writ_runtime_lens_check_required exited {result.returncode} (expected 0="
        f"required or 1=skip) for session_id={session_id!r} cache_dir={cache_dir}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _worst_case_project(root: Path) -> Path:
    """A project with a PRE-EVIDENCE debug.md and a code file: the worst-case envelope
    (plan Decision 3, property 1/2) that reliably denies whenever the effective source
    type is 'runtime', so the predicate's REQUIRED set can be compared to the real
    gate's DENY set case for case rather than only in the direction that is easy to
    satisfy by never skipping."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / "debug.md").write_text(EVIDENCE_EMPTY)
    (root / "app.py").write_text("# code\n")
    return root


def _worst_case_envelope(tool: str, proj: Path) -> dict:
    if tool == "Grep":
        return {"tool_name": "Grep", "tool_input": {"pattern": "foo", "path": str(proj)}}
    if tool == "Glob":
        return {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py", "path": str(proj)}}
    if tool == "Read":
        return {"tool_name": "Read", "tool_input": {"file_path": str(proj / "app.py")}}
    raise ValueError(f"no worst-case envelope defined for derived tool {tool!r}")


# --------------------------------------------------------------------------------- #
# Capabilities 1 and 2: the property over the full 72-case cross product.
# --------------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def matrix_results(tmp_path_factory) -> dict:
    """One sweep over the 72-case matrix: the REAL bash predicate (subprocess) and the
    REAL python gate (gates._can_read_code_check), computed once and shared by both
    property tests below so 72 cases cost 72 bash spawns, not 144."""
    results: dict = {}
    for i, (mode, source_type, tool) in enumerate(CROSS_PRODUCT):
        case_dir = tmp_path_factory.mktemp(f"matrix-{i}")
        cache_dir = case_dir / "cache"
        cache_dir.mkdir()
        sid = "matrix-case"
        _write_cache_file(cache_dir, sid, mode, source_type)
        required = _predicate_required(sid, cache_dir)

        proj = _worst_case_project(case_dir / "proj")
        envelope = _worst_case_envelope(tool, proj)
        previous = os.environ.get("WRIT_CACHE_DIR")
        os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
        try:
            result = gates._can_read_code_check(sid, envelope, "")
        finally:
            if previous is None:
                os.environ.pop("WRIT_CACHE_DIR", None)
            else:
                os.environ["WRIT_CACHE_DIR"] = previous

        results[(mode, source_type, tool)] = {
            "required": required,
            "denies": result["can_read"] is False,
        }
    return results


class TestPredicateSafetyAndExactness:
    """Capabilities 1 and 2 (plan Decision 3, properties 1 and 2)."""

    def test_safety_no_skipped_case_can_deny(self, matrix_results) -> None:
        assert len(matrix_results) == 72
        unsafe = sorted(
            case for case, r in matrix_results.items() if not r["required"] and r["denies"]
        )
        assert unsafe == [], (
            f"UNSAFE: the predicate skipped {len(unsafe)} case(s) the real gate denies: "
            f"{unsafe}"
        )

    def test_exactness_the_skip_and_deny_sets_exactly_partition_the_matrix(
        self, matrix_results
    ) -> None:
        assert len(matrix_results) == 72
        # Anti-vacuity precondition (harness trap 4): a predicate that disagreed with
        # the real gate on an EQUAL number of false-skips and false-requires could still
        # make the two SET equalities below hold by coincidence unless every individual
        # case agrees too.
        mismatches = sorted(
            case for case, r in matrix_results.items() if r["required"] != r["denies"]
        )
        assert mismatches == [], f"predicate and real gate disagree on: {mismatches}"

        skip_set = {c for c, r in matrix_results.items() if not r["required"]}
        deny_set = {c for c, r in matrix_results.items() if r["denies"]}
        assert not (skip_set & deny_set), sorted(skip_set & deny_set)
        assert len(skip_set) == 51, sorted(skip_set)
        assert len(deny_set) == 21, sorted(deny_set)
        assert len(skip_set) + len(deny_set) == 72


# --------------------------------------------------------------------------------- #
# Capability 3: every unresolvable cache state is "check required".
# --------------------------------------------------------------------------------- #


def _setup_absent(cache_dir: Path, sid: str) -> None:
    pass  # no file is created at all


def _setup_unreadable(cache_dir: Path, sid: str) -> None:
    path = cache_dir / f"writ-session-{sid}.json"
    path.write_text(json.dumps({"mode": "debug"}))
    path.chmod(0o000)


def _setup_not_json(cache_dir: Path, sid: str) -> None:
    (cache_dir / f"writ-session-{sid}.json").write_text("{not json at all")


def _setup_json_array(cache_dir: Path, sid: str) -> None:
    (cache_dir / f"writ-session-{sid}.json").write_text(json.dumps(["not", "an", "object"]))


_UNRESOLVABLE_CASES = [
    pytest.param(_setup_absent, id="file-absent"),
    pytest.param(_setup_unreadable, id="file-unreadable"),
    pytest.param(_setup_not_json, id="bytes-not-json"),
    pytest.param(_setup_json_array, id="top-level-json-array"),
]
assert len(_UNRESOLVABLE_CASES) == 4


class TestFailDirectionOnUnresolvableCacheStates:
    """Capability 3. Skipping on uncertainty would be a silent gate bypass (plan
    Analysis): uncertainty always pays the expensive path."""

    def test_four_shapes_are_pinned(self) -> None:
        assert len(_UNRESOLVABLE_CASES) == 4

    @pytest.mark.parametrize("setup", _UNRESOLVABLE_CASES)
    def test_each_unresolvable_shape_requires_the_check(self, tmp_path, setup) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        sid = "unresolvable-case"
        setup(cache_dir, sid)
        assert _predicate_required(sid, cache_dir) is True

    def test_an_unresolvable_session_id_also_requires_the_check(self, tmp_path) -> None:
        """Named alongside the four pinned shapes in the dispatch brief's fail-direction
        list, kept outside the 4-count pin above because capabilities.md names exactly
        those four: a session id that cannot resolve to a valid path is a different kind
        of uncertainty than a malformed file at a valid one."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        assert _predicate_required("../../../etc/passwd", cache_dir) is True


# --------------------------------------------------------------------------------- #
# Capability 4: the jq arm and the WRIT_NO_JQ=1 fallback arm must always agree.
# --------------------------------------------------------------------------------- #

_FALSY_SPELLINGS = ("missing", "null", "empty", "false")


def _valid_cache_payloads() -> list:
    """The 24 valid (mode, source_type) cache shapes. Where a field is absent (mode or
    source_type is None), the absence is spelled a DIFFERENT way each time it occurs
    (missing key, JSON null, empty string, JSON false, cycling in that order), so the 24
    shapes collectively drive every falsy spelling the WRIT_NO_JQ seam is documented to
    disagree on (plan Decision 2), without multiplying the pinned count of 24."""
    payloads = []
    spelling_i = 0
    for mode, source_type in _VALID_SHAPES:
        payload: dict = {}
        if mode is not None:
            payload["mode"] = mode
        else:
            spelling = _FALSY_SPELLINGS[spelling_i % len(_FALSY_SPELLINGS)]
            spelling_i += 1
            if spelling == "null":
                payload["mode"] = None
            elif spelling == "empty":
                payload["mode"] = ""
            elif spelling == "false":
                payload["mode"] = False
            # "missing": the key is simply omitted.
        if source_type is not None:
            payload["source_type"] = source_type
        else:
            spelling = _FALSY_SPELLINGS[spelling_i % len(_FALSY_SPELLINGS)]
            spelling_i += 1
            if spelling == "null":
                payload["source_type"] = None
            elif spelling == "empty":
                payload["source_type"] = ""
            elif spelling == "false":
                payload["source_type"] = False
        payloads.append(((mode, source_type), payload))
    return payloads


class TestSeamParityBetweenJqAndPythonFallback:
    """Capability 4. Every one of the 24 valid shapes plus the 4 unresolvable ones (28
    total, count asserted) must answer identically with jq and with WRIT_NO_JQ=1."""

    def test_28_cases_are_pinned(self) -> None:
        assert len(_valid_cache_payloads()) == 24
        assert len(_UNRESOLVABLE_CASES) == 4
        assert 24 + 4 == 28

    @pytest.mark.parametrize(
        "case_id,payload",
        [
            pytest.param(cid, payload, id=f"valid-mode={cid[0]}-source={cid[1]}")
            for cid, payload in _valid_cache_payloads()
        ],
    )
    def test_valid_shapes_agree_across_both_arms(self, tmp_path, case_id, payload) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        sid = "seam-valid-case"
        (cache_dir / f"writ-session-{sid}.json").write_text(json.dumps(payload))
        jq_answer = _predicate_required(sid, cache_dir, no_jq=False)
        py_answer = _predicate_required(sid, cache_dir, no_jq=True)
        assert jq_answer == py_answer, (
            f"jq and python-fallback disagree for mode={case_id[0]!r} "
            f"source_type={case_id[1]!r} payload={payload!r}: jq={jq_answer} "
            f"python={py_answer}"
        )

    @pytest.mark.parametrize("setup", _UNRESOLVABLE_CASES)
    def test_unresolvable_shapes_agree_across_both_arms(self, tmp_path, setup) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        sid = "seam-unresolvable-case"
        setup(cache_dir, sid)
        jq_answer = _predicate_required(sid, cache_dir, no_jq=False)
        py_answer = _predicate_required(sid, cache_dir, no_jq=True)
        assert jq_answer == py_answer


# --------------------------------------------------------------------------------- #
# Capability 5: the skip is a PROCESS fact (an argv shim), never a timing fact.
# --------------------------------------------------------------------------------- #


class TestSpawnProofSkipIsReal:
    """Capability 5. A python3 shim on PATH records every invocation's argv and execs
    the real interpreter, so the hook still completes normally while this test can see
    exactly which processes it started."""

    def _shim_dir(self, tmp_path: Path, log: Path) -> Path:
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text(
            "#!/bin/bash\n"
            f'printf \'%s\\n\' "$*" >> "{log}"\n'
            f'exec "{sys.executable}" "$@"\n'
        )
        shim.chmod(0o755)
        return shim_dir

    def _run_with_shim(self, iso, envelope: dict, log: Path):
        env = build_env(iso)
        shim_dir = self._shim_dir(iso.tmp_path, log)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(envelope),
            capture_output=True, text=True, env=env, cwd=str(iso.project_root), timeout=30,
        )

    def test_skip_state_makes_no_can_read_code_invocation(self, tmp_path) -> None:
        iso = make_isolation(tmp_path)
        write_cache(iso, {"mode": "work"})  # source_type absent, mode != debug -> skip
        log = tmp_path / "argv.log"
        log.write_text("")
        envelope = {
            "session_id": iso.session_id, "hook_event_name": "PreToolUse",
            "tool_name": "Read", "tool_input": {"file_path": str(iso.project_root / "app.py")},
        }
        proc = self._run_with_shim(iso, envelope, log)
        assert proc.returncode == 0, proc.stderr
        invocations = [ln for ln in log.read_text().splitlines() if ln.strip()]
        assert invocations, "the python3 shim recorded no invocations at all"
        assert not any("can-read-code" in ln for ln in invocations), (
            f"a skippable state (mode=work, no source_type) still invoked can-read-code: "
            f"{invocations}"
        )

    def test_check_required_state_makes_exactly_one_can_read_code_invocation(
        self, tmp_path
    ) -> None:
        iso = make_isolation(tmp_path)
        write_cache(iso, {"mode": "debug"})  # source_type absent, mode debug -> required
        log = tmp_path / "argv.log"
        log.write_text("")
        envelope = {
            "session_id": iso.session_id, "hook_event_name": "PreToolUse",
            "tool_name": "Read", "tool_input": {"file_path": str(iso.project_root / "app.py")},
        }
        proc = self._run_with_shim(iso, envelope, log)
        assert proc.returncode == 0, proc.stderr
        hits = [ln for ln in log.read_text().splitlines() if "can-read-code" in ln]
        assert len(hits) == 1, (
            f"a check-required state (mode=debug) must invoke can-read-code exactly "
            f"once, saw {len(hits)}: {hits}"
        )


# --------------------------------------------------------------------------------- #
# Capability 6: the skip arm's gate_decision row matches the un-skipped allow arm's.
# --------------------------------------------------------------------------------- #


class TestRecordEquivalenceAcrossSkipAndAllow:
    """Capability 6.

    PARTIALLY VACUOUS TODAY, DOCUMENTED: before this cycle both scenarios below run the
    IDENTICAL, un-forked code (there is no separate skip arm yet), so of course the two
    rows match; that is not yet evidence the bypass writes an equivalent row, only that
    one code path is consistent with itself. It becomes a genuine regression fence the
    moment the skip arm exists as independent code that calls log_gate_decision directly
    instead of going through cmd_can_read_code.

    The SAME session id and SAME target path are reused for both runs (only the cache's
    `mode` differs between them) specifically so `target` is comparable regardless of
    what that field actually carries today (the session id, per the hook's own comment
    on why SID keys its telemetry): the equality is meaningful either way.
    """

    def test_skip_arm_and_check_required_allow_arm_write_the_same_row(self, tmp_path) -> None:
        iso = make_isolation(tmp_path)
        target = str(iso.project_root / "server.log")  # non-code: allows even when required
        envelope = {
            "session_id": iso.session_id, "hook_event_name": "PreToolUse",
            "tool_name": "Read", "tool_input": {"file_path": target},
        }
        env = build_env(iso)

        write_cache(iso, {"mode": "work"})  # skip arm
        skip_res = run_hook(HOOK_NAME, envelope, iso)
        assert skip_res.returncode == 0, skip_res.stderr
        subprocess.run(
            [sys.executable, str(FLUSH), iso.session_id], env=env,
            capture_output=True, text=True, timeout=30,
        )
        skip_rows = [
            r for r in read_streams(iso.log_project, ["audit"]) if r.get("gate") == GATE_NAME
        ]
        assert skip_rows, f"the skip arm wrote no {GATE_NAME} gate_decision row"
        skip_row = skip_rows[-1]

        write_cache(iso, {"mode": "debug"})  # check-required, but target still allows
        required_res = run_hook(HOOK_NAME, envelope, iso)
        assert required_res.returncode == 0, required_res.stderr
        subprocess.run(
            [sys.executable, str(FLUSH), iso.session_id], env=env,
            capture_output=True, text=True, timeout=30,
        )
        all_rows = [
            r for r in read_streams(iso.log_project, ["audit"]) if r.get("gate") == GATE_NAME
        ]
        assert len(all_rows) >= 2, (
            f"the check-required arm wrote no NEW {GATE_NAME} gate_decision row: {all_rows}"
        )
        required_row = all_rows[-1]

        assert skip_row["decision"] == required_row["decision"] == "allow"
        assert skip_row["reason"] == required_row["reason"]
        assert skip_row["target"] == required_row["target"]
        assert skip_row["gate"] == required_row["gate"] == GATE_NAME


# --------------------------------------------------------------------------------- #
# Capability 7: the deny path is unchanged. Real today, stays real after.
# --------------------------------------------------------------------------------- #


class TestDenyPathIsUnchanged:
    """Capability 7. This cycle touches only the allow branches; a real deny must keep
    its exact shape and must still land synchronously (log_gate_decision's own
    documented split: anything but "allow" is emitted immediately, never buffered).
    Passes against TODAY's code too: it is the regression fence for this reordering.
    """

    def _deny_envelope(self, iso) -> dict:
        return {
            "session_id": iso.session_id, "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "foo", "path": str(iso.project_root)},
        }

    def test_grep_deny_in_the_runtime_lens_keeps_its_shape(self, tmp_path) -> None:
        iso = make_isolation(tmp_path)
        write_cache(iso, {"mode": "debug"})
        (iso.project_root / "debug.md").write_text(EVIDENCE_EMPTY)
        result = run_hook(HOOK_NAME, self._deny_envelope(iso), iso)
        assert result.returncode == 0, result.stderr
        assert result.permission_decision() == "deny"
        assert "DEBUG-EVIDENCE-FIRST" in result.permission_reason()
        doc = result.stdout_json()
        assert doc is not None, f"stdout was not JSON: {result.stdout!r}"
        hso = doc.get("hookSpecificOutput") or {}
        assert hso.get("additionalContext") == (
            "Runtime (debug) lens: read debug.md / logs / non-code and gather runtime "
            "evidence via Bash first, record Evidence + Narrowing in debug.md, then "
            "read code."
        )

    def test_the_deny_record_lands_synchronously_with_no_flush(self, tmp_path) -> None:
        iso = make_isolation(tmp_path)
        write_cache(iso, {"mode": "debug"})
        (iso.project_root / "debug.md").write_text(EVIDENCE_EMPTY)
        result = run_hook(HOOK_NAME, self._deny_envelope(iso), iso)
        assert result.returncode == 0, result.stderr
        rows = [
            r for r in result.audit()
            if r.get("gate") == GATE_NAME and r.get("session") == iso.session_id
        ]
        assert rows, (
            "no synchronous gate_decision row for a deny, with NO flush called: a "
            "deny must never depend on a later Stop/SessionEnd drain"
        )
        assert rows[-1]["decision"] == "deny"


# --------------------------------------------------------------------------------- #
# Capability 8: the fire drill's census entry needs no edit.
# --------------------------------------------------------------------------------- #


class TestCensusEntryStaysACheckRequiredState:
    """Capability 8. The drill's declared setup for this script (mode=debug, no
    source_type) is the predicate's SECOND arm, so it must remain a check-required
    combination, never edited to keep exercising the full expensive path."""

    def test_the_declared_setup_seeds_a_check_required_combination(self, tmp_path) -> None:
        from tests.firedrill._census import _setup_debug_code_gate

        iso = make_isolation(tmp_path)
        _setup_debug_code_gate(iso)
        cache = read_cache(iso)
        assert cache is not None, "the census setup wrote no session cache"
        assert cache.get("mode") == "debug"
        assert not cache.get("source_type"), (
            "the census entry now seeds an explicit source_type; it must stay absent "
            "for the drill to keep exercising the full expensive (check-required) path"
        )
        assert _predicate_required(iso.session_id, iso.cache_dir) is True, (
            "the census entry's seeded state is no longer a CHECK-REQUIRED combination; "
            "the drill's declared refusal would silently start skipping the expensive "
            "check it exists to exercise"
        )


# --------------------------------------------------------------------------------- #
# Capability 9: a missing/unsourced common.sh must never cause a skip.
# --------------------------------------------------------------------------------- #


class TestFallbackWhenCommonShIsUnavailable:
    """Capability 9. Two proofs. STATIC (matching this repo's own established style in
    tests/test_hook_instrumentation.py's `_calls`): the hook's source must guard the
    predicate call with the same `type <name> >/dev/null 2>&1` idiom it already uses for
    hook_instrument, so an unsourced common.sh (undefined function) falls to the
    expensive path. DYNAMIC: the guard IDIOM itself, in isolation, proven to take the
    non-skip branch when the named function does not exist, independent of whether
    common.sh happens to define it in this test run. A full end-to-end simulation of "a
    hook's common.sh fails to source while every OTHER helper the hook needs still
    resolves" is not reproducible without editing common.sh itself, which is out of
    this test-writer's scope; these two proofs are the closest honest substitute.
    """

    def test_the_hook_guards_the_predicate_call_like_hook_instrument(self) -> None:
        assert HOOK.is_file(), f"{HOOK} does not exist"
        text = HOOK.read_text()
        assert re.search(
            r"type\s+writ_runtime_lens_check_required\s*>/dev/null\s*2>&1", text
        ), (
            "writ-debug-code-gate.sh must guard the predicate call with "
            "`type writ_runtime_lens_check_required >/dev/null 2>&1`, the same idiom "
            "already used for hook_instrument, so an unsourced common.sh (an undefined "
            "function) falls to the expensive path rather than skipping"
        )

    def test_an_undefined_predicate_function_takes_the_non_skip_branch(self, tmp_path) -> None:
        probe = tmp_path / "probe.sh"
        probe.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "if type writ_runtime_lens_check_required_DOES_NOT_EXIST "
            ">/dev/null 2>&1 && writ_runtime_lens_check_required_DOES_NOT_EXIST; then\n"
            "  echo SKIPPED\n"
            "else\n"
            "  echo REQUIRED\n"
            "fi\n"
        )
        result = subprocess.run(
            ["bash", str(probe)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "REQUIRED"


# --------------------------------------------------------------------------------- #
# Capability 10: WRIT_CACHE_DIR moves the predicate's read and the python check's read
# together.
# --------------------------------------------------------------------------------- #


class TestCacheDirParityBetweenPredicateAndRealCheck:
    """Capability 10. Both reads resolve through `writ_session_cache_dir` /
    `writ.session.cache._cache_dir()`, pinned equal by
    tests/test_session_cache_dir_parity.py, so a WRIT_CACHE_DIR override can never
    desync the two: the predicate would look one place and the real gate another."""

    def test_a_custom_cache_dir_is_seen_by_both_reads_for_the_same_case(
        self, tmp_path, monkeypatch
    ) -> None:
        custom_dir = tmp_path / "somewhere-else"
        custom_dir.mkdir()
        sid = "cache-dir-parity-case"
        _write_cache_file(custom_dir, sid, "debug", None)  # a check-required combination
        assert _predicate_required(sid, custom_dir) is True

        monkeypatch.setenv("WRIT_CACHE_DIR", str(custom_dir))
        proj = _worst_case_project(tmp_path / "proj")
        envelope = _worst_case_envelope("Read", proj)
        result = gates._can_read_code_check(sid, envelope, "")
        assert result["can_read"] is False, (
            "the python gate did not see the same custom WRIT_CACHE_DIR the bash "
            "predicate read from: the two reads have desynced"
        )
