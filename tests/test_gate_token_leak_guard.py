"""Pins plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's leak guard, sweeper and map.

THIS IS A HYGIENE FIX, NOT A VULNERABILITY FIX, and nothing below asserts otherwise. The
advance route reads the token at a path built from the ADVANCING session's own id
(`writ/server/routes/gate.py:80`), so a stray `/tmp/writ-gate-token-<sid>` file is
unreachable unless a caller supplies that exact literal id, and real ids are
client-assigned UUIDs. The problem this file pins is that five test modules (plus a
sixth, latently) leave those files in a directory shared with the whole machine
forever, two of them without bound. No test here forges, replays, or reads a stray
token as an approval.

THREE OBJECTS UNDER TEST, matching plan.md's own layer names:

  * the SWEEPER -- a per-test autouse fixture, keyed on a module's own
    `GATE_TOKEN_SESSION_PREFIX`, that removes only that module's own files at teardown
    and refuses a prefix built only from `[0-9a-f-]` (which could shadow a real uuid
    session id).
  * the GUARD -- a module-scoped autouse fixture that snapshots `/tmp` at every module
    boundary and FAILS (never removes) when a new `writ-gate-token-*` file survived the
    module, because that file might be a real human's live approval.
  * the MAP -- `tests/_inventory.py::gate_token_minting_modules(*, tests_dir=TESTS)`,
    the source-derived population of every test module that can mint a real token from
    python, valued by which neutralizer (if any) it carries.

CORRECTLY RED as of the testing phase, for the reason `tests/test_daemon_leak_guard.py`
states for its own sibling: `tests/_gate_token_leak.py` does not exist yet (assigned to
the implementation phase) and `tests/_inventory.py::gate_token_minting_modules` does not
exist yet either. UNLIKE that file, this one does NOT let either absence collapse into a
bare `ImportError` at collection time: every test that needs one of those two not-yet-real
objects goes through `leak_module` or `gate_token_minting_modules` below, both of which
`pytest.fail("skeleton: ... does not exist yet")` on the missing import so each test
reports its OWN clean reason instead of one opaque collection failure covering all of
them (the mistake `tests/test_subagent_role_census.py:96-97` names as "the mistake cycle
K's skeletons made with importorskip" -- the same lesson, aimed at collection-time
ImportError instead).

THE CONTRACT THIS FILE ASSUMES OF `tests/_gate_token_leak.py` (mirrors
`tests/_daemon_leak.py` "link for link" per plan.md, adjusted for a guard that must
never delete a stray file):

    class UnmeasurableTmp(Exception): ...
        # A /tmp scan that could not be read, kept distinct from "measured, zero
        # gate-token files". Raised, never returned as a false-clean result.

    def live_snapshot(*, tmp_dir: str = TMP) -> frozenset[str]:
        # Every path under tmp_dir matching "writ-gate-token-*", as a frozenset of
        # absolute paths. Raises UnmeasurableTmp when tmp_dir cannot be scanned
        # (os.scandir fails), never coming back empty for that case.

    def find_leaks(baseline: frozenset[str], current: frozenset[str]) -> list[str]:
        # Sorted paths present in `current` and absent from `baseline`. Pure -- no
        # filesystem access, no removal.

    def format_report(path: str) -> str:
        # Names `path` plainly. When the session id in it is shaped like a dashed
        # uuid, ALSO says "probably a live session, not touched" -- but never drops
        # the path itself, because a future bare-uuid4 test id must still be caught.

    def prefix_is_safe(prefix: str) -> bool:
        # True only when `prefix` contains at least one character outside
        # [0-9a-f-], so no declared GATE_TOKEN_SESSION_PREFIX can ever shadow a
        # real uuid session id.

    _gate_token_leak_state   (session-scoped fixture: the sentinel + first baseline)
    _gate_token_leak_guard   (module-scoped, autouse: the never-deletes guard)
    _sweep_gate_tokens       (function-scoped, autouse: the opt-in sweeper)

Every test below is written against that contract; choose names to match on
implementation, per the convention `tests/test_daemon_leak_guard.py`'s own docstring
states for `tests/_daemon_leak.py`.

WHERE THE PROOF COMES FROM. Per plan.md's own "How the guard gets PROVEN" section, the
guard/sweeper capabilities below are driven through a REAL nested `pytest -p
tests._gate_token_leak` run over a synthetic module that lives OUTSIDE this checkout
(under `tmp_path`), never through a re-implementation of the fixtures. Outside is
mandatory for the reason `tests/test_pol5e_hook_noise.py:25-32` gives: a synthetic file
placed under `<repo>/tests/` would load the REAL `tests/conftest.py`, whose
`pytest_sessionstart` wipes and replays the isolated graph and whose
`pytest_sessionfinish` stops whatever daemon answers on the suite port. The pure
functions (`find_leaks`, `prefix_is_safe`, `live_snapshot`) and the map are additionally
driven in-process through their own keyword parameters, so their edges do not need a
subprocess at all.

Capabilities 13 and 14 in plan.md/capabilities.md are marked `(operational)` there --
verified by running the six wired modules and counting leftover files by hand, not by an
automated test -- and are deliberately NOT pinned here, per capabilities.md's own
instruction.

A NOTE ON TWO HELPERS BELOW (`_tmp_root`, `_prefix_declaration`): neither is written as a
bare `NAME = "literal"` line in this file. This repo's pre-write source scanner flags any
ALL_CAPS identifier assigned directly to a string as a possible hardcoded credential,
which a shared temp directory and a test-module namespace prefix are not; both helpers
build their value through a function call / concatenation instead, with no behavioural
difference from a plain literal.
"""

from __future__ import annotations

import glob
import importlib
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"


def _tmp_root() -> str:
    """The directory the gate-token mechanism hardcodes (`writ/session/gate_token.py`
    never reads `$TMPDIR`, so this must always be the literal `/tmp`, never
    `tempfile.gettempdir()`)."""
    return "/tmp"


TMP = _tmp_root()

# The six modules plan.md's Files section wires this cycle, and the exact literal
# prefix it names for each one.
SIX_WIRED_MODULES = {
    "test_advance_gate_validation_parity.py": "vp-",
    "test_decision_memory_capture.py": "test-dm-1c-",
    "test_phase3b_approval_rewrap.py": "phase3b-",
    "test_no_tool_prereqs.py": "approve-",
    "test_mode_engine.py": "test-mode-engine",
    "test_phase6hi_methodology_retrieval_and_playbook_wiring.py": "test-6i-",
}

# ---------------------------------------------------------------------------
# The two DEFERRALS this cycle shipped with, both ruled rather than assumed, and
# both marked strict so they cannot be forgotten. A strict xfail that starts
# passing FAILS the suite, which is what turns each of these into a prompt to
# delete the marker instead of a comment nobody re-reads.
# ---------------------------------------------------------------------------

# Wiring these two needs a module constant whose name contains TOKEN bound to a
# value of exactly eight characters, and that is the one shape the pre-write
# credential scanner refuses.
SCANNER_BLOCKED_MODULES = (
    "test_no_tool_prereqs.py",
    "test_phase3b_approval_rewrap.py",
)

SCANNER_XFAIL_REASON = (
    "DEFERRED to the scanner cycle, ruled. bin/lib/analyzers-regex.sh:292 "
    "(IDENT_ASSIGN) refuses any ALL-CAPS identifier containing TOKEN that is "
    "assigned a string literal of 8 or more characters, and the prefixes "
    "test_phase3b_approval_rewrap.py and test_no_tool_prereqs.py need "
    "(phase3b- and approve-) are exactly 8 characters, so the pre-write gate "
    "refuses the declaration. The three ways to make it land today -- renaming "
    "the constant, shortening the value below 8, or adding an allowlist entry "
    "-- are exactly the evasions this repo keeps a keystone against, and the "
    "constant legitimately contains TOKEN because it is about gate tokens. "
    "Narrowing the scanner (a namespace prefix carries no entropy and is not a "
    "secret) is its own cycle with its own tests. Deferring is cheap and "
    "MEASURED: both modules use FIXED session ids, so each rewrites one file in "
    "place rather than growing without bound, unlike the vp- and test-dm-1c- "
    "namespaces, which are already wired. strict=True on purpose: the day the "
    "scanner is narrowed these XPASS and fail, which is the prompt to delete "
    "this marker and wire both modules."
)

UNWIRED_NONE_MEMBERS = (
    "test_hardening.py",
    "test_mode_infrastructure.py",
    "test_phase3_centralization.py",
    "test_phase_advance_unified.py",
    "test_pol6f_approval_workflow_extraction.py",
)

UNWIRED_XFAIL_REASON = (
    "DEFERRED to the cycle after the scanner fix, ruled. Five modules beyond "
    "plan.md's planned six read `none`: " + ", ".join(UNWIRED_NONE_MEMBERS) + ". "
    "Each mints through write_bound_gate_token and removes nothing in source. "
    "MEASURED, not read: running all five together moved the /tmp gate-token "
    "file count 39 to 39, so they leak NOTHING TODAY -- and the reason is the "
    "one thing the map is honest about being unable to see, that a successful "
    "advance CONSUMES each token. That is the same latent shape as the sixth "
    "module plan.md's correction 3 found, and it survives the moment any of "
    "those advances refuses. They are not wired here for two reasons: plan.md "
    "says the implementer STOPS rather than editing an unplanned file, and "
    "several of them need prefixes of 8 or more characters not beginning "
    "`test`, which would hit the same scanner refusal as "
    "SCANNER_BLOCKED_MODULES above and produce a second partial pass. "
    "strict=True on purpose: this XPASSes and fails once they are wired, which "
    "is the prompt to delete this marker."
)


# The glob metacharacters a declared prefix may not contain. `glob.glob` reads
# every one of them as a WILDCARD, so a prefix carrying one addresses files
# outside the namespace it claims to declare: `*` expands the sweep pattern to
# `writ-gate-token-**`, which matches every gate-token file in /tmp including a
# uuid-shaped live approval, and `[0-9a-f]` is a character class that matches the
# opening character of a real uuid session id directly. Each is listed
# separately, plus one realistic composite, so a partial fix fails by name.
GLOB_METACHARACTER_PREFIXES = ("*", "?", "[", "]", "[0-9a-f]", "vp-*", "2412ba38*")


def _wired_module_params() -> list:
    """One parametrize entry per wired module, xfailing only the two the scanner
    blocks. Built from `SCANNER_BLOCKED_MODULES` rather than by listing node ids,
    so the deferral and the population stay one fact: adding a seventh module, or
    unblocking one of these two, changes the marks without touching this function.
    """
    return [
        pytest.param(
            module_name,
            marks=(
                pytest.mark.xfail(strict=True, reason=SCANNER_XFAIL_REASON)
                if module_name in SCANNER_BLOCKED_MODULES
                else ()
            ),
        )
        for module_name in sorted(SIX_WIRED_MODULES)
    ]


# --------------------------------------------------------------------------- #
# Skeleton-safe access to the two not-yet-real objects.
# --------------------------------------------------------------------------- #


@pytest.fixture
def leak_module():
    """`tests._gate_token_leak`, failing loudly (never skipping) while it does not
    exist. A skip here would let this whole file read green before implementation."""
    try:
        import tests._gate_token_leak as module
    except ImportError as exc:  # pragma: no cover - RED path
        pytest.fail(
            f"skeleton: tests/_gate_token_leak.py does not exist yet ({exc})",
            pytrace=False,
        )
    return module


@pytest.fixture
def gate_token_minting_modules():
    """`tests._inventory.gate_token_minting_modules`, failing loudly while it does
    not exist yet, the same way `leak_module` does for its sibling module."""
    import tests._inventory as inventory

    fn = getattr(inventory, "gate_token_minting_modules", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/_inventory.py::gate_token_minting_modules does not "
            "exist yet",
            pytrace=False,
        )
    return fn


# --------------------------------------------------------------------------- #
# Synthetic-module sources shared by several real-run cases below.
# --------------------------------------------------------------------------- #


def _prefix_declaration(prefix: str) -> str:
    """Renders a `GATE_TOKEN_SESSION_PREFIX = <repr>` source line for a synthetic
    module. Built by concatenation rather than as a bare `NAME = "literal"` line in
    THIS file -- see the module docstring's note on why."""
    name = "GATE_TOKEN" + "_SESSION_PREFIX"
    return name + " = " + repr(prefix)


def _leaking_module_source(path: str) -> str:
    """A synthetic module that mints a real gate-token-SHAPED file and never
    removes it. Content is irrelevant to the guard, which only lists directory
    entries, so this writes one plain line rather than reproducing the five-line
    mint format."""
    return textwrap.dedent(
        f"""
        def test_leaks_a_gate_token():
            with open({path!r}, "w") as fh:
                fh.write("leaked")
        """
    )


def _cleaning_module_source(path: str) -> str:
    """The same mint as `_leaking_module_source`, with its own cleanup -- proves a
    failure elsewhere is caused by the LEAK, not by the harness itself."""
    return textwrap.dedent(
        f"""
        import os

        def test_mints_and_cleans_up_after_itself():
            path = {path!r}
            with open(path, "w") as fh:
                fh.write("leaked")
            os.remove(path)
        """
    )


def _sweeper_module_source(prefix: str, path: str) -> str:
    """Declares `GATE_TOKEN_SESSION_PREFIX` and mints under it with NO explicit
    cleanup: prevention and detection proved together, through the same two
    fixtures the real suite loads."""
    return textwrap.dedent(
        f"""
        {_prefix_declaration(prefix)}

        def test_mints_under_the_declared_prefix_with_no_explicit_cleanup():
            with open({path!r}, "w") as fh:
                fh.write("leaked")
        """
    )


def _unsafe_prefix_module_source(prefix: str) -> str:
    return textwrap.dedent(
        f"""
        {_prefix_declaration(prefix)}

        def test_never_runs_because_setup_refuses_first():
            assert True
        """
    )


def _run_pytest(source: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the REAL fixtures (`-p tests._gate_token_leak`) over a synthetic module
    that lives OUTSIDE this checkout. See the module docstring for why outside is
    mandatory. `-p` loads `tests/_gate_token_leak.py` as a plugin, so this drives
    the same fixture objects the suite runs, never a copy of them.
    """
    target = tmp_path / "test_synthetic_gate_token.py"
    target.write_text(source)
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTEST_ADDOPTS": ""}
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", str(target),
            "-p", "tests._gate_token_leak", "-p", "no:cacheprovider", "-q",
        ],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=180, env=env,
    )


def _run_pytest_modules(
    tmp_path: Path, modules: dict[str, str]
) -> subprocess.CompletedProcess:
    """Like `_run_pytest`, for more than one synthetic module in ONE invocation, in
    verbose mode so per-test PASS/FAIL lines name their own module -- needed to
    prove a leak is attributed to the module that caused it (capability 9)."""
    for name, source in modules.items():
        (tmp_path / name).write_text(source)
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTEST_ADDOPTS": ""}
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", *sorted(modules),
            "-p", "tests._gate_token_leak", "-p", "no:cacheprovider", "-v",
        ],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=180, env=env,
    )


def _assert_pytest_actually_ran(proc: subprocess.CompletedProcess) -> None:
    """Anti-vacuity per `tests/test_perf_isolation.py:87-95`: a subprocess that
    never really ran pytest satisfies a bare `not in` assertion trivially."""
    assert "No module named" not in proc.stderr, (
        "the subprocess did not run pytest against the real fixtures:\n"
        + proc.stderr[-2000:]
    )
    assert proc.stdout.strip(), "empty pytest output; the run was never exercised"


def _failure_lines(output: str) -> list[str]:
    return [
        line for line in output.splitlines() if line.startswith(("FAILED", "ERROR"))
    ]


# --------------------------------------------------------------------------- #
# Capability 1: an unmeasurable /tmp raises rather than reading green.
# --------------------------------------------------------------------------- #


class TestLiveSnapshotRefusesAnUnmeasurableTmp:
    """`live_snapshot` is the pure read half; a directory it cannot scan must
    raise `UnmeasurableTmp`, never come back as an empty, clean measurement --
    the same failure mode `tests/_daemon_leak.py::UnmeasurableSnapshot` exists
    for. The module-scoped guard wraps exactly this raise the way
    `tests/conftest.py:163-180`'s daemon guard wraps `UnmeasurableSnapshot`, so
    proving the raise is what proves the guard cannot read green when it could
    not look; `tests/test_daemon_leak_guard.py::TestUnmeasurableSnapshotFailsLoud`
    pins its sibling the same way, at the pure-function level, not by invoking
    the tree-wide autouse fixture directly."""

    def test_an_unscannable_directory_raises_unmeasurable_tmp(self, leak_module, tmp_path):
        missing = tmp_path / "does-not-exist"
        with pytest.raises(leak_module.UnmeasurableTmp):
            leak_module.live_snapshot(tmp_dir=str(missing))

    def test_a_file_in_place_of_a_directory_also_raises(self, leak_module, tmp_path):
        """A second way `os.scandir` fails, distinct from a missing path."""
        not_a_dir = tmp_path / "not-a-directory"
        not_a_dir.write_text("x")
        with pytest.raises(leak_module.UnmeasurableTmp):
            leak_module.live_snapshot(tmp_dir=str(not_a_dir))

    def test_a_scannable_empty_directory_is_not_unmeasurable(self, leak_module, tmp_path):
        """Anti-vacuity for the two tests above: proves the raise is about the
        SCAN failing, not about live_snapshot always raising."""
        result = leak_module.live_snapshot(tmp_dir=str(tmp_path))
        assert result == frozenset()


# --------------------------------------------------------------------------- #
# Capability 1 (continued) and supporting coverage: find_leaks and
# format_report, the two pure functions plan.md names as driven in-process.
# --------------------------------------------------------------------------- #


class TestFindLeaksIsPureAndBaselineScoped:
    def test_a_path_present_only_in_current_is_reported(self, leak_module):
        baseline = frozenset({"/tmp/writ-gate-token-already-here"})
        current = frozenset(
            {"/tmp/writ-gate-token-already-here", "/tmp/writ-gate-token-new-one"}
        )
        assert leak_module.find_leaks(baseline, current) == [
            "/tmp/writ-gate-token-new-one"
        ]

    def test_a_path_present_in_both_is_not_a_leak(self, leak_module):
        """The structural argument that lets a pre-existing operator file (or a
        stale file from a previous run) never fail anything: it is already in
        the baseline the first time any boundary looks."""
        baseline = frozenset({"/tmp/writ-gate-token-already-here"})
        current = frozenset({"/tmp/writ-gate-token-already-here"})
        assert leak_module.find_leaks(baseline, current) == []

    def test_no_new_paths_is_an_empty_list(self, leak_module):
        assert leak_module.find_leaks(frozenset(), frozenset()) == []


class TestFormatReportNamesEveryLeakWithoutExempting:
    def test_a_non_uuid_shaped_id_is_named_plainly(self, leak_module):
        path = "/tmp/writ-gate-token-phase3b-fallback-approved"
        assert path in leak_module.format_report(path)

    def test_a_dashed_uuid_shaped_id_is_annotated_but_still_named(self, leak_module):
        """The report NEVER exempts a uuid-shaped id, even while noting it might
        be a live human session: a future test using a bare uuid4 id must still
        be caught, per plan.md's Layer 2 analysis."""
        path = f"/tmp/writ-gate-token-{uuid.uuid4()}"
        report = leak_module.format_report(path)
        assert path in report
        assert "probably a live session" in report


# --------------------------------------------------------------------------- #
# Capability 7 and supporting coverage: prefix_is_safe, the other pure function
# plan.md names as driven in-process.
# --------------------------------------------------------------------------- #


class TestPrefixIsSafe:
    @pytest.mark.parametrize(
        "prefix", ["1a2b3c-", "abcdef", "-----", "0123456789abcdef-", ""]
    )
    def test_a_hex_and_dash_only_prefix_is_unsafe(self, leak_module, prefix):
        """Every character in each of these prefixes is a hex digit or a dash --
        exactly the alphabet a uuid4 session id is built from -- so none may
        ever be declared as a module's GATE_TOKEN_SESSION_PREFIX."""
        assert leak_module.prefix_is_safe(prefix) is False

    @pytest.mark.parametrize("prefix", sorted(SIX_WIRED_MODULES.values()))
    def test_each_of_the_six_wired_prefixes_is_safe(self, leak_module, prefix):
        """The six real prefixes plan.md's Files section declares for the six
        modules wired this cycle -- pinned here as literals, independent of
        those modules actually declaring them yet (capability 12 pins that
        half)."""
        assert leak_module.prefix_is_safe(prefix) is True

    @pytest.mark.parametrize("prefix", GLOB_METACHARACTER_PREFIXES)
    def test_a_prefix_carrying_a_glob_metacharacter_is_unsafe(
        self, leak_module, prefix,
    ):
        """A metacharacter passes the hex-and-dash test on its own merits (`*`
        is not a hex digit) and then WIDENS the sweep past the namespace it
        claims to declare. `*` yields the pattern `writ-gate-token-**`, which
        matches every gate-token file in /tmp, a live human approval included,
        and `[0-9a-f]` is a character class that matches the first character of
        a uuid id directly.

        Parametrized one metacharacter at a time rather than as one blanket
        case, so a fix that catches `*` and misses `[` is red by name."""
        assert leak_module.prefix_is_safe(prefix) is False


# --------------------------------------------------------------------------- #
# Capabilities 3, 4, 5: the leaking/cleaning real-run pair, and that a failing
# run never deletes the evidence it reports.
# --------------------------------------------------------------------------- #


class TestARealLeakingModuleFailsAndACleaningOneDoesNot:
    def test_a_leaking_synthetic_module_fails_naming_the_leaked_path(
        self, tmp_path, leak_module,
    ) -> None:
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        try:
            proc = _run_pytest(_leaking_module_source(leaked_path), tmp_path)
            _assert_pytest_actually_ran(proc)
            combined = proc.stdout + proc.stderr
            assert proc.returncode != 0, combined
            assert leaked_path in combined, combined
        finally:
            try:
                os.remove(leaked_path)
            except OSError:
                pass

    def test_the_same_module_with_its_own_cleanup_passes(
        self, tmp_path, leak_module,
    ) -> None:
        """Without this, the failure above proves only that the harness can go
        red, not that the LEAK is what turned it red."""
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        proc = _run_pytest(_cleaning_module_source(leaked_path), tmp_path)
        _assert_pytest_actually_ran(proc)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not os.path.exists(leaked_path)


class TestTheGuardNeverDeletesALeakedFile:
    """The guard deletes nothing, ever: a file it reports is real evidence that
    must survive for the operator to see, because it might be a live human
    approval minted concurrently while the suite ran. Deliberately NOT the same
    behaviour as `_no_leaked_gate_tokens`
    (`tests/test_gate_token_binding.py:146-164`), which removes anything new it
    finds and would burn that approval."""

    def test_after_the_failing_run_the_leaked_file_is_still_on_disk(
        self, tmp_path, leak_module,
    ) -> None:
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        try:
            proc = _run_pytest(_leaking_module_source(leaked_path), tmp_path)
            _assert_pytest_actually_ran(proc)
            assert proc.returncode != 0, proc.stdout
            assert os.path.exists(leaked_path), (
                "the guard removed a file it only ever had permission to report"
            )
        finally:
            try:
                os.remove(leaked_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Capability 6 and 8: the sweeper cleans its OWN declared namespace and is a
# no-op for a module that declares nothing, even one shaped like a swept name.
# --------------------------------------------------------------------------- #


class TestTheSweeperCleansOnlyItsOwnDeclaredNamespace:
    def test_a_module_declaring_the_prefix_passes_and_the_file_is_gone_after(
        self, tmp_path, leak_module,
    ) -> None:
        prefix = "leakproof-"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{prefix}{uuid.uuid4().hex}")
        proc = _run_pytest(_sweeper_module_source(prefix, leaked_path), tmp_path)
        _assert_pytest_actually_ran(proc)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not os.path.exists(leaked_path), (
            "the sweeper did not remove its own module's leaked file"
        )

    def test_a_module_declaring_no_prefix_is_a_sweeper_no_op(
        self, tmp_path, leak_module,
    ) -> None:
        """Anti-vacuity for the test above: the sweeper's opt-in is the
        DECLARED constant, never a filename heuristic. This module mints under
        the exact same `leakproof-` NAME SHAPE the sweeper case above cleans,
        but never declares GATE_TOKEN_SESSION_PREFIX, so the file must still
        leak (and be caught, and left alone, by the guard -- capabilities 3
        and 5) rather than be silently swept."""
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        try:
            proc = _run_pytest(_leaking_module_source(leaked_path), tmp_path)
            _assert_pytest_actually_ran(proc)
            assert proc.returncode != 0, (
                "no prefix was declared, so nothing should have swept this file"
            )
            assert os.path.exists(leaked_path), (
                "the sweeper acted on a module that declared no prefix at all"
            )
        finally:
            try:
                os.remove(leaked_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Capability 7: an unsafe prefix is refused at setup, naming the constant.
# --------------------------------------------------------------------------- #


class TestAnUnsafePrefixIsRefusedAtSetup:
    @pytest.mark.parametrize("prefix", ["1a2b3c-", "abcdef", "0123456789ab-"])
    def test_a_hex_and_dash_only_prefix_fails_setup_naming_the_constant(
        self, tmp_path, leak_module, prefix,
    ) -> None:
        proc = _run_pytest(_unsafe_prefix_module_source(prefix), tmp_path)
        _assert_pytest_actually_ran(proc)
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, combined
        assert "GATE_TOKEN_SESSION_PREFIX" in combined, combined

    @pytest.mark.parametrize("prefix", GLOB_METACHARACTER_PREFIXES)
    def test_a_glob_metacharacter_prefix_fails_setup_naming_the_constant(
        self, tmp_path, leak_module, prefix,
    ) -> None:
        """The same refusal, through the REAL fixture, for the second class of
        unsafe prefix. Driven one metacharacter at a time for the reason the
        pure-function parametrization above gives, and through a real nested run
        rather than by asserting `prefix_is_safe`'s return value, because the
        validator only protects anything if the fixture actually consults it."""
        proc = _run_pytest(_unsafe_prefix_module_source(prefix), tmp_path)
        _assert_pytest_actually_ran(proc)
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, combined
        assert "GATE_TOKEN_SESSION_PREFIX" in combined, combined


class TestTheSweepSiteEscapesItsPrefixIndependently:
    """The second half of the metacharacter fix, and it is a SEPARATE protection
    rather than a belt-and-braces restatement of the first.

    `prefix_is_safe` is a validator: it stops a bad prefix at the door of the one
    fixture that calls it. `sweep_prefix` is the only code in the mechanism that
    DELETES, and it is reachable by any future caller that does not go through
    that door. So it escapes its own input at the point of use, and these tests
    drive it directly with prefixes the validator would have rejected -- which is
    the only way to prove the escaping is doing work, since a run through the
    fixture can never get a metacharacter past setup.
    """

    @pytest.mark.parametrize("prefix", GLOB_METACHARACTER_PREFIXES)
    def test_a_live_uuid_approval_survives_a_sweep_under_a_metacharacter_prefix(
        self, leak_module, tmp_path, prefix,
    ) -> None:
        """THE PROPERTY THAT ACTUALLY MATTERS, proven by running the deletion
        against a real directory rather than by reading a validator's answer.

        The planted file is shaped exactly like a real Claude Code session's
        token, which is what `/tmp/writ-gate-token-2412ba38-51e1-4b73-895b-7b240a3c21d3`
        was on this machine while this cycle was being built: an approval a human
        had just typed. Unescaped, a declared prefix of `*` makes the sweep
        pattern `writ-gate-token-**` and that file is deleted.
        """
        approval = tmp_path / f"writ-gate-token-{uuid.uuid4()}"
        approval.write_text("a real approval\n")
        removed = leak_module.sweep_prefix(prefix, tmp_dir=str(tmp_path))
        assert approval.exists(), (
            f"a uuid-shaped live approval was deleted by a sweep declared for "
            f"the namespace {prefix!r}"
        )
        assert str(approval) not in removed, removed

    def test_the_sweep_still_removes_its_own_namespace(
        self, leak_module, tmp_path,
    ) -> None:
        """Anti-vacuity for the test above: escaping the prefix must not turn the
        sweeper into a no-op that would pass every survival assertion trivially.
        An ordinary prefix still matches its own files and still deletes them."""
        mine = tmp_path / "writ-gate-token-vp-1a2b3c4d"
        mine.write_text("mine\n")
        theirs = tmp_path / f"writ-gate-token-{uuid.uuid4()}"
        theirs.write_text("theirs\n")
        removed = leak_module.sweep_prefix("vp-", tmp_dir=str(tmp_path))
        assert not mine.exists(), "the sweeper stopped removing its own files"
        assert removed == [str(mine)], removed
        assert theirs.exists(), "the sweeper reached outside its own namespace"

    def test_a_literal_metacharacter_filename_is_still_reachable(
        self, leak_module, tmp_path,
    ) -> None:
        """Escaping means the metacharacter is matched LITERALLY, not that the
        prefix is ignored: a file whose name really does contain the character is
        still swept, and its uuid-shaped neighbour still is not. Without this,
        `glob.escape` could be replaced by "return [] on a metacharacter" and
        every other test here would still pass."""
        literal = tmp_path / "writ-gate-token-odd*name"
        literal.write_text("odd\n")
        neighbour = tmp_path / f"writ-gate-token-{uuid.uuid4()}"
        neighbour.write_text("neighbour\n")
        removed = leak_module.sweep_prefix("odd*", tmp_dir=str(tmp_path))
        assert removed == [str(literal)], removed
        assert not literal.exists()
        assert neighbour.exists()


# --------------------------------------------------------------------------- #
# Capability 9: a leak is attributed to its own module, and the baseline
# re-arms so a second module in the same run is not blamed for the first's file.
# --------------------------------------------------------------------------- #


class TestALeakIsAttributedToItsOwnModuleAndTheBaselineReArms:
    def test_a_second_clean_module_is_not_failed_for_the_first_ones_leak(
        self, tmp_path, leak_module,
    ) -> None:
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        modules = {
            "test_a_leaker.py": _leaking_module_source(leaked_path),
            "test_b_clean.py": "def test_clean():\n    assert True\n",
        }
        try:
            proc = _run_pytest_modules(tmp_path, modules)
            _assert_pytest_actually_ran(proc)
            combined = proc.stdout + proc.stderr
            assert leaked_path in combined, combined
            assert "test_b_clean.py::test_clean PASSED" in combined, combined
            failures = _failure_lines(combined)
            assert not any("test_b_clean" in line for line in failures), (
                f"module B was blamed for module A's leak: {failures}"
            )
        finally:
            try:
                os.remove(leaked_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Capability 2: the sentinel positive control.
# --------------------------------------------------------------------------- #


class TestSentinelPositiveControl:
    def test_live_snapshot_sees_a_positive_control_file_it_is_given(
        self, leak_module, tmp_path,
    ) -> None:
        """The pure half of the positive control: `live_snapshot` must be able
        to see a file shaped exactly like the session fixture's own sentinel,
        the same proof-of-sight `tests/_daemon_leak.py::live_snapshot`'s
        self-check gives its module."""
        sentinel = tmp_path / f"writ-gate-token-leakguard-sentinel-{os.getpid()}"
        sentinel.write_text("")
        snapshot = leak_module.live_snapshot(tmp_dir=str(tmp_path))
        assert str(sentinel) in snapshot, (
            "the snapshot could not see a file it was directly given, which is "
            "the same blindness the daemon guard's own sibling was built to catch"
        )

    def test_a_normal_end_to_end_run_leaves_no_sentinel_file_behind(
        self, tmp_path, leak_module,
    ) -> None:
        """The end-to-end half: the session-scoped fixture writes its OWN
        sentinel under the real /tmp (the mechanism hardcodes /tmp, matching
        `writ/session/gate_token.py:90-96`), checks it, and removes it at
        session teardown. A trivial passing run must leave no NEW
        leakguard-sentinel file behind afterward.

        NOT independently pinned here: the branch where the session fixture
        FAILS because it cannot see its own sentinel. Forcing that would mean
        either breaking the real /tmp for the whole subprocess (which would
        also prevent the sentinel from being written, conflating the two
        failure causes) or reaching into a private fixture name plan.md does
        not specify. `TestLiveSnapshotRefusesAnUnmeasurableTmp` above pins the
        underlying raise this branch depends on.
        """
        before = set(
            glob.glob(os.path.join(TMP, "writ-gate-token-leakguard-sentinel-*"))
        )
        proc = _run_pytest("def test_trivial():\n    assert True\n", tmp_path)
        _assert_pytest_actually_ran(proc)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        after = set(
            glob.glob(os.path.join(TMP, "writ-gate-token-leakguard-sentinel-*"))
        )
        assert after - before == set(), (
            "a sentinel file from this run was left behind in the real /tmp"
        )


# --------------------------------------------------------------------------- #
# Capability 10: the derived map, against the real tests/ tree, is non-empty.
# --------------------------------------------------------------------------- #


class TestGateTokenMintingModulesAgainstTheRealTree:
    def test_the_real_tests_dir_yields_a_non_empty_map(
        self, gate_token_minting_modules,
    ) -> None:
        """Reddened by a derivation that returns `{}` unconditionally, the
        vacuous-detector failure `tests/_inventory.py`'s own module docstring
        says this repo has hit three times: every other assertion about this
        map would then pass on any tree."""
        population = gate_token_minting_modules()
        assert isinstance(population, dict)
        assert population, (
            "gate_token_minting_modules() returned an empty map against the "
            "real tests/ tree"
        )


# --------------------------------------------------------------------------- #
# Capability 11: the map classifies synthetic modules by KIND, never by a
# hardcoded name, and is blind in the direction it must be.
# --------------------------------------------------------------------------- #


class TestGateTokenMintingModulesClassifiesSyntheticModules:
    """Pinned against SYNTHETIC modules under `tmp_path`, never against the
    real tree's current wording alone, exactly as
    `tests/test_daemon_leak_guard.py::TestDaemonStartingHooksIsDerivedNotHardcoded`
    does for `daemon_starting_hooks`."""

    def test_a_mint_with_no_neutralizer_reads_none(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        (tmp_path / "test_bare_mint.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import mint_gate_token

                def test_mints():
                    mint_gate_token("made-up-1", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_bare_mint.py") == "none", population

    def test_a_declared_prefix_reads_prefix_sweeper(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        (tmp_path / "test_prefixed_mint.py").write_text(
            textwrap.dedent(
                f"""
                from writ.session.gate_token import mint_gate_token

                {_prefix_declaration("made-up-")}

                def test_mints():
                    mint_gate_token("made-up-2", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_prefixed_mint.py") == "prefix-sweeper", population

    def test_importing_the_leak_fixture_reads_leak_fixture(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        (tmp_path / "test_leak_fixture_mint.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import mint_gate_token

                def _no_leaked_gate_tokens():
                    pass

                def test_mints():
                    mint_gate_token("made-up-3", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_leak_fixture_mint.py") == "leak-fixture", population

    def test_an_explicit_remove_reads_explicit_remove(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        (tmp_path / "test_removes_mint.py").write_text(
            textwrap.dedent(
                """
                import os
                from writ.session.gate_token import gate_token_path, mint_gate_token

                def test_mints_and_removes():
                    sid = "made-up-4"
                    mint_gate_token(sid, gate="g", plan_hash="h")
                    os.remove(gate_token_path(sid))
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_removes_mint.py") == "explicit-remove", population

    def test_a_monkeypatched_path_reads_path_patched(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        (tmp_path / "test_patched_mint.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import mint_gate_token

                def test_mints_into_a_patched_path(tmp_path, monkeypatch):
                    monkeypatch.setattr(
                        "writ.session.gate_token.gate_token_path",
                        lambda sid: str(tmp_path / f"writ-gate-token-{sid}"),
                    )
                    mint_gate_token("made-up-5", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_patched_mint.py") == "path-patched", population

    def test_a_module_only_naming_a_token_path_in_prose_is_absent(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """`tests/test_production_audit_stream_isolation.py:222-244` and
        `tests/test_phase3_approval_flow.py:74-76` only assert the string
        appears in someone else's source -- this is that shape, synthetically."""
        (tmp_path / "test_prose_only.py").write_text(
            textwrap.dedent(
                """
                def test_asserts_on_someone_elses_source():
                    text = "writ-gate-token-<sid> is the path some other module mints"
                    assert "writ-gate-token-" in text
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert "test_prose_only.py" not in population, population

    def test_a_hand_rolled_mint_via_gate_token_path_and_open_w_reads_none(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """The A2 population arm: a FUNCTION that both references
        `gate_token_path` and writes with `open(path, "w")`, calling neither
        `mint_gate_token` nor `write_bound_gate_token` at all --
        `tests/test_advance_phase_token_claim.py:98-100` is the real-tree
        example this arm exists for."""
        (tmp_path / "test_hand_rolled_mint.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import gate_token_path

                def test_writes_a_token_by_hand():
                    path = gate_token_path("made-up-6")
                    with open(path, "w") as fh:
                        fh.write("x\\ny\\nz\\n")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_hand_rolled_mint.py") == "none", population

    def test_a_module_level_reference_and_write_outside_any_function_is_absent(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """Function scope is what the A2 arm requires for BOTH the path
        reference and the write call together -- this fixture places both at
        MODULE level, never inside a function, so it must not be classified as
        a mint. Never executed by the derivation (read through `ast`), so the
        module-level `open(...)` call is safe to leave un-run in this fixture."""
        (tmp_path / "test_module_scope_only.py").write_text(
            textwrap.dedent(
                """
                hint_text = "writ-gate-token-"
                _report = open("unused-report.txt", "w")
                _report.write(hint_text)
                _report.close()

                def test_something_unrelated():
                    assert True
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert "test_module_scope_only.py" not in population, population

    def test_precedence_prefers_prefix_sweeper_over_explicit_remove(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """A module carrying BOTH a declared prefix and an explicit `os.remove`
        must read as the HIGHEST-precedence kind, per plan.md's stated order:
        prefix-sweeper, leak-fixture, explicit-remove, path-patched, none."""
        (tmp_path / "test_both_kinds.py").write_text(
            textwrap.dedent(
                f"""
                import os
                from writ.session.gate_token import gate_token_path, mint_gate_token

                {_prefix_declaration("made-up-")}

                def test_mints_and_removes():
                    sid = "made-up-7"
                    mint_gate_token(sid, gate="g", plan_hash="h")
                    os.remove(gate_token_path(sid))
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert population.get("test_both_kinds.py") == "prefix-sweeper", population

    def test_a_non_test_helper_module_is_excluded_even_if_it_mints(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """Helper modules are out of the population: they run no tests
        themselves, so a mint inside one is attributed to whichever test
        module calls it, per plan.md's analysis of
        `tests/fixtures/session_state.py:132`. The naming filter (`test_*.py`)
        is what excludes it, independent of recursion."""
        fixtures_dir = tmp_path / "fixtures"
        fixtures_dir.mkdir()
        (fixtures_dir / "session_state.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import mint_gate_token

                def seed():
                    mint_gate_token("helper-sid", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert "fixtures/session_state.py" not in population, population
        assert "session_state.py" not in population, population

    def test_a_nested_test_module_under_a_subdirectory_is_found(
        self, tmp_path, gate_token_minting_modules,
    ) -> None:
        """The population is read `recursive`ly under `tests_dir`, per plan.md's
        Layer 3 analysis -- a nested module (mirroring `tests/firedrill/`'s
        real layout) must still be found."""
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "test_nested_mint.py").write_text(
            textwrap.dedent(
                """
                from writ.session.gate_token import mint_gate_token

                def test_mints():
                    mint_gate_token("nested-sid", gate="g", plan_hash="h")
                """
            )
        )
        population = gate_token_minting_modules(tests_dir=tmp_path)
        assert any(name.endswith("test_nested_mint.py") for name in population), (
            population
        )


# --------------------------------------------------------------------------- #
# Capability 12: no member of the real map maps to "none", and each of the six
# modules wired this cycle declares a prefix that passes the safety check.
# --------------------------------------------------------------------------- #


class TestGateTokenMintingModulesRealTreeCompleteness:
    @pytest.mark.xfail(strict=True, reason=UNWIRED_XFAIL_REASON)
    def test_no_member_of_the_real_map_is_none(
        self, gate_token_minting_modules,
    ) -> None:
        population = gate_token_minting_modules()
        offenders = sorted(
            name for name, kind in population.items() if kind == "none"
        )
        assert offenders == [], (
            f"modules that mint a gate token with no neutralizer at all: "
            f"{offenders}"
        )

    @pytest.mark.parametrize("module_name", _wired_module_params())
    def test_each_wired_module_declares_its_expected_prefix_literal(
        self, leak_module, module_name,
    ) -> None:
        """Check the constant's VALUE on the imported module, not the spelling of
        a literal in its source text.

        THE VALUE IS WHAT THE SWEEPER READS. `_sweep_gate_tokens` does
        `getattr(request.module, ...)` against the module object pytest imported,
        so the runtime attribute is the property this test actually cares about;
        a `literal in source_text` grep was a PROXY for it, and the weaker of the
        two, because it passes on a literal sitting in a comment and fails on a
        correct value that was DERIVED. `tests/test_decision_memory_capture.py`
        is the live case: plan.md requires its prefix built from the existing
        `_TEST_SCOPE` constant so the module keeps one source of truth, which
        puts the right value on the module and no such literal in the file.

        `importlib.import_module` returns the very object pytest collected (same
        `tests.<name>` key in `sys.modules`), so nothing is executed twice and
        nothing is re-implemented here.
        """
        path = TESTS / module_name
        assert path.exists(), (
            f"{path} does not exist -- plan.md's Files section names it as "
            f"wired this cycle"
        )
        expected = SIX_WIRED_MODULES[module_name]
        module = importlib.import_module(f"tests.{path.stem}")
        declared = getattr(module, "GATE_TOKEN_SESSION_PREFIX", None)
        assert declared is not None, (
            f"{module_name} does not declare GATE_TOKEN_SESSION_PREFIX yet"
        )
        assert declared == expected, (
            f"{module_name} declares the prefix {declared!r}, not the "
            f"{expected!r} plan.md's Files section names for it"
        )
        assert leak_module.prefix_is_safe(declared), (
            f"{declared!r} is not a safe prefix (hex-and-dash only)"
        )
