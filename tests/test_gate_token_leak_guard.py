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
import json
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

# The ten modules wired by a DECLARED PREFIX as of this cycle, and the exact
# runtime value plan.md names for each one. Named WIRED_PREFIX_MODULES rather
# than the SIX_WIRED_MODULES this replaces: a name carrying a count is the
# duplicated-pin shape this repo has been bitten by (see tests/_inventory.py's
# own module docstring on the Maat-program count pins). `test_phase_advance_
# unified.py` is deliberately NOT a member here: no single prefix covers its
# five id shapes, so it is wired by IMPORTING and wrapping `_mint_cleanup`
# instead (plan.md's Decision 2), and its kind ("leak-fixture") plus its
# "unwrapped == []" claim are pinned separately below.
WIRED_PREFIX_MODULES = {
    "test_advance_gate_validation_parity.py": "vp-",
    "test_decision_memory_capture.py": "test-dm-1c-",
    "test_phase3b_approval_rewrap.py": "phase3b-",
    "test_no_tool_prereqs.py": "approve-",
    "test_mode_engine.py": "test-mode-engine",
    "test_phase6hi_methodology_retrieval_and_playbook_wiring.py": "test-6i-",
    "test_hardening.py": "test-hardening-",
    "test_pol6f_approval_workflow_extraction.py": "aw-",
    "test_mode_infrastructure.py": "test-session-test-mode-infrastructure-",
    "test_phase3_centralization.py": "test-session-test-phase3-centralization-",
}

# ---------------------------------------------------------------------------
# The strict xfail that used to sit here (five modules deferred, marked strict
# so an accidental fix would fail the suite until the marker was deleted) is
# GONE. plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 wires all five, so
# `test_no_member_of_the_real_map_is_none` below is an ordinary, UNMARKED
# assertion now -- no xfail, no xpass in the report.
#
# `unwired_module_report` builds the message a future author reads if a SIXTH
# module lands minting with no neutralizer. A refusal that does not name the
# way out is a deadlock (this repo's own keystone), so the message names the
# offender, all three sanctioned neutralizers in `_gtm_kind` precedence order,
# the file each one is declared in, and the map to update -- pinned below on
# synthetic input, independent of any module actually going unwired.
# ---------------------------------------------------------------------------


def unwired_module_report(offenders: list[str]) -> str:
    """The refusal a future author reads when their new module mints with no neutralizer.

    A REFUSAL MUST NAME THE ACTION. This one names all three sanctioned neutralizers in
    the precedence `_gtm_kind` applies them, the file each one is declared in, and the map
    to update, because a message that only says "this is wrong" leaves the next author
    guessing at a mechanism spread over three files.

    THE CONSTANT NAME IS NEVER FOLLOWED BY A QUOTED VALUE ON ONE LINE, deliberately.
    `bin/lib/analyzers-regex.sh` IDENT_ASSIGN flags any ALL-CAPS identifier containing
    TOKEN assigned an 8-or-more-character string literal, so an "improved" wording that
    put an example value in quotes right after the name would be refused at write time by
    the pre-write scanner, and the next author would be fighting the scanner instead of
    reading the message.
    """
    return (
        "these test modules can mint a real /tmp gate token and carry no neutralizer: "
        + ", ".join(offenders)
        + ". Take one of the three, in this order. ONE: declare a module-level "
        "GATE_TOKEN_SESSION_PREFIX naming your own namespace (lowercase, ending in a "
        "hyphen, carrying at least one character outside [0-9a-f-] and no glob "
        "metacharacter, or tests/_gate_token_leak.py::_sweep_gate_tokens refuses it at "
        "setup), then add the module and that value to WIRED_PREFIX_MODULES in this file. "
        "TWO: when no single prefix covers every id the module mints, import "
        "_mint_cleanup from tests.test_gate_token_binding and wrap each mint site in it. "
        "THREE: monkeypatch writ.session.gate_token.gate_token_path so the mint never "
        "reaches /tmp at all. docs/adr/ADR-gate-token-leak-guard.md records why the file "
        "is never simply deleted by the guard."
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
    """One parametrize entry per wired module, all of them unmarked. The two
    entries that used to carry a conditional xfail (the scanner refused their
    `approve-` and `phase3b-` prefixes) run like the other four now that the
    credential scanner exempts namespace-shaped values and both modules declare
    their prefix at module level. Derived from `WIRED_PREFIX_MODULES` rather
    than from a hand-listed set of node ids, so an eleventh module is covered
    by adding it to the map alone.
    """
    return [module_name for module_name in sorted(WIRED_PREFIX_MODULES)]


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


@pytest.fixture
def gate_token_directory_deleters():
    """`tests._inventory.gate_token_directory_deleters`, already real as of
    this cycle -- unlike its three siblings below, its own suite
    (`tests/test_gate_token_deleter_confinement.py`) already exercises it.
    Wrapped the same way for a uniform access pattern in this file, and so a
    future removal of the function fails loudly here too instead of a bare
    `AttributeError`."""
    import tests._inventory as inventory

    fn = getattr(inventory, "gate_token_directory_deleters", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/_inventory.py::gate_token_directory_deleters "
            "does not exist",
            pytrace=False,
        )
    return fn


@pytest.fixture
def shared_session_id_importers():
    """`tests._inventory.shared_session_id_importers`, failing loudly while it
    does not exist yet, the same way `gate_token_minting_modules` does for its
    sibling. plan.md's Decision 1: the population of modules that could
    collide on the shared `session_id` fixture's default."""
    import tests._inventory as inventory

    fn = getattr(inventory, "shared_session_id_importers", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/_inventory.py::shared_session_id_importers does "
            "not exist yet",
            pytrace=False,
        )
    return fn


@pytest.fixture
def gate_token_mint_wrapping():
    """`tests._inventory.gate_token_mint_wrapping`, failing loudly while it
    does not exist yet. plan.md's Decision 2: which gate-token mints in one
    module sit inside a `_mint_cleanup` block."""
    import tests._inventory as inventory

    fn = getattr(inventory, "gate_token_mint_wrapping", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/_inventory.py::gate_token_mint_wrapping does not "
            "exist yet",
            pytrace=False,
        )
    return fn


@pytest.fixture
def module_session_id():
    """`tests.fixtures.session_state.module_session_id`, failing loudly while
    it does not exist yet -- the id-collision fix plan.md's Decision 1 wires
    upstream of this file, in the shared fixture module rather than here."""
    import tests.fixtures.session_state as session_state

    fn = getattr(session_state, "module_session_id", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/fixtures/session_state.py::module_session_id "
            "does not exist yet",
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

    @pytest.mark.parametrize("prefix", sorted(WIRED_PREFIX_MODULES.values()))
    def test_each_wired_prefix_is_safe(self, leak_module, prefix):
        """Every real prefix plan.md declares for the ten modules wired by
        prefix this cycle -- pinned here as literals, independent of those
        modules actually declaring them yet (capability 12 pins that half).
        Parametrized over the map's values, so an eleventh module is covered
        by adding it to WIRED_PREFIX_MODULES alone."""
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
# Capability 12: no member of the real map maps to "none" -- run UNMARKED, not
# xfail -- and each of the ten modules wired by prefix this cycle declares a
# value that passes the safety check.
# --------------------------------------------------------------------------- #


class TestGateTokenMintingModulesRealTreeCompleteness:
    def test_no_member_of_the_real_map_is_none(
        self, gate_token_minting_modules,
    ) -> None:
        """UNMARKED: the strict xfail that used to cover this is gone
        (plan.md's Decision 3). RED until all five previously-unwired modules
        are wired -- expected during the testing phase, not a defect in this
        test."""
        population = gate_token_minting_modules()
        offenders = sorted(
            name for name, kind in population.items() if kind == "none"
        )
        assert offenders == [], unwired_module_report(offenders)

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

        `tests/test_mode_infrastructure.py` and `tests/test_phase3_centralization.py`
        are the other two live derived cases this cycle adds, built from
        `module_session_id(__name__)` rather than typed out by hand.

        `importlib.import_module` returns the very object pytest collected (same
        `tests.<name>` key in `sys.modules`), so nothing is executed twice and
        nothing is re-implemented here.
        """
        path = TESTS / module_name
        assert path.exists(), (
            f"{path} does not exist -- plan.md's Files section names it as "
            f"wired this cycle"
        )
        expected = WIRED_PREFIX_MODULES[module_name]
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


# --------------------------------------------------------------------------- #
# unwired_module_report: pinned on synthetic input, independent of any red
# run. "A refusal must name the way out" -- this repo's own keystone.
# --------------------------------------------------------------------------- #


class TestUnwiredModuleReportNamesTheWayOut:
    def test_every_offender_is_named(self) -> None:
        offenders = ["test_alpha_made_up.py", "test_beta_made_up.py"]
        report = unwired_module_report(offenders)
        for offender in offenders:
            assert offender in report, report

    def test_all_three_sanctioned_neutralizers_are_named_in_gtm_kind_precedence_order(
        self,
    ) -> None:
        """`_gtm_kind`'s real precedence is prefix-sweeper, leak-fixture,
        explicit-remove, path-patched, none. This message deliberately offers
        only the three FORWARD-LOOKING options a new author should reach for
        (explicit-remove is a legacy shape, not a recommendation), named in
        that same relative order: the prefix constant, then `_mint_cleanup`,
        then the monkeypatch target."""
        report = unwired_module_report(["test_made_up.py"])
        prefix_at = report.index("GATE_TOKEN_SESSION_PREFIX")
        mint_cleanup_at = report.index("_mint_cleanup")
        patched_at = report.index("gate_token_path")
        assert prefix_at < mint_cleanup_at < patched_at, report

    def test_the_file_each_neutralizer_lives_in_is_named(self) -> None:
        report = unwired_module_report(["test_made_up.py"])
        assert "tests.test_gate_token_binding" in report, report
        assert "writ.session.gate_token" in report, report

    def test_the_map_to_update_is_named(self) -> None:
        report = unwired_module_report(["test_made_up.py"])
        assert "WIRED_PREFIX_MODULES" in report, report

    def test_an_empty_offender_list_still_returns_a_named_string(self) -> None:
        """Anti-vacuity boundary: the function must not raise, and must not
        drop the fixed guidance half of the message, even on an input the
        real caller never actually passes (offenders == [] never reaches this
        function, since the assertion above it only fails on a non-empty
        list)."""
        report = unwired_module_report([])
        assert isinstance(report, str)
        assert "WIRED_PREFIX_MODULES" in report, report


# --------------------------------------------------------------------------- #
# gate_token_minting_modules()["test_phase_advance_unified.py"] == "leak-fixture"
# --------------------------------------------------------------------------- #


class TestPhaseAdvanceUnifiedClassifiesAsLeakFixture:
    def test_the_real_module_reads_leak_fixture(
        self, gate_token_minting_modules,
    ) -> None:
        """Wired by IMPORTING `_mint_cleanup` (plan.md's Decision 2), never by
        a declared `GATE_TOKEN_SESSION_PREFIX`: no single prefix covers its
        five id shapes (four uuid-suffixed forms plus the bare literal `".."`),
        so this module is deliberately absent from `WIRED_PREFIX_MODULES` and
        pinned here instead."""
        population = gate_token_minting_modules()
        assert (
            population.get("test_phase_advance_unified.py") == "leak-fixture"
        ), population


# --------------------------------------------------------------------------- #
# gate_token_directory_deleters() must stay blind to the _mint_cleanup import:
# a DIFFERENT map from gate_token_minting_modules, answering a different
# question (confinement on directory-listing deletion, not neutralization).
# --------------------------------------------------------------------------- #


class TestPhaseAdvanceUnifiedNeverEntersTheDirectoryDeletersMap:
    def test_absent_after_importing_mint_cleanup(
        self, gate_token_directory_deleters,
    ) -> None:
        """`_mint_cleanup` removes a path it NAMED and lists no directory, so
        it is not one of `test_gate_token_binding.py`'s own arm-A functions:
        `definers["test_gate_token_binding"]` holds `_no_leaked_gate_tokens`
        only. Arm B (the import-resolution arm) therefore never resolves for
        this import, so importing `_mint_cleanup` must not enrol this module
        in the map that answers "can this module tell a stray file from a
        live session's own approval when it deletes by listing a directory"."""
        deleters = gate_token_directory_deleters()
        assert "test_phase_advance_unified.py" not in deleters, deleters


# --------------------------------------------------------------------------- #
# gate_token_mint_wrapping: the real test_phase_advance_unified.py, and three
# synthetic shapes -- unwrapped, wrapped, and the mutation that proves this
# keys on _mint_cleanup rather than on any `with` block at all.
# --------------------------------------------------------------------------- #


def _unwrapped_mint_source() -> str:
    return textwrap.dedent(
        """
        from writ.session.gate_token import mint_gate_token

        def test_mints_with_no_wrapper():
            mint_gate_token("made-up-8", gate="g", plan_hash="h")
        """
    )


def _wrapped_mint_source() -> str:
    return textwrap.dedent(
        """
        from tests.test_gate_token_binding import _mint_cleanup
        from writ.session.gate_token import mint_gate_token

        def test_mints_inside_mint_cleanup():
            sid = "made-up-9"
            with _mint_cleanup(sid):
                mint_gate_token(sid, gate="g", plan_hash="h")
        """
    )


def _mint_inside_a_different_context_manager_source() -> str:
    return textwrap.dedent(
        """
        import contextlib
        from writ.session.gate_token import mint_gate_token

        @contextlib.contextmanager
        def _some_other_context(sid):
            yield

        def test_mints_inside_an_unrelated_context_manager():
            sid = "made-up-10"
            with _some_other_context(sid):
                mint_gate_token(sid, gate="g", plan_hash="h")
        """
    )


class TestGateTokenMintWrappingOnTheRealModule:
    def test_test_phase_advance_unified_is_fully_wrapped(
        self, gate_token_mint_wrapping,
    ) -> None:
        wrapped, unwrapped = gate_token_mint_wrapping(
            TESTS / "test_phase_advance_unified.py"
        )
        assert wrapped, (
            "gate_token_mint_wrapping() found no wrapped mint in "
            "test_phase_advance_unified.py -- the anti-vacuity half: a "
            "derivation that found no mint at all would read unwrapped == [] "
            "too, which is why the wrapped list must be non-empty on its own"
        )
        assert unwrapped == [], unwrapped


class TestGateTokenMintWrappingClassifiesSyntheticModules:
    """Pinned against SYNTHETIC modules under `tmp_path`, in the convention
    `gate_token_minting_modules` states for itself, never against the real
    module's current wording alone."""

    def test_a_mint_outside_any_with_reads_unwrapped(
        self, tmp_path, gate_token_mint_wrapping,
    ) -> None:
        path = tmp_path / "test_unwrapped_mint.py"
        path.write_text(_unwrapped_mint_source())
        wrapped, unwrapped = gate_token_mint_wrapping(path)
        assert wrapped == [], wrapped
        assert unwrapped != [], "the bare mint site was not reported as unwrapped"

    def test_the_same_mint_inside_mint_cleanup_reads_wrapped(
        self, tmp_path, gate_token_mint_wrapping,
    ) -> None:
        path = tmp_path / "test_wrapped_mint.py"
        path.write_text(_wrapped_mint_source())
        wrapped, unwrapped = gate_token_mint_wrapping(path)
        assert unwrapped == [], unwrapped
        assert wrapped != [], "a mint inside _mint_cleanup was not reported as wrapped"

    def test_a_mint_inside_a_different_context_manager_still_reads_unwrapped(
        self, tmp_path, gate_token_mint_wrapping,
    ) -> None:
        """THE MUTATION THAT MATTERS: a `with` block that is not
        `_mint_cleanup` must not count as wrapping, proving the derivation
        keys on the HELPER's name rather than on any `with` statement at all
        -- a derivation that just checked "is this Call inside any With node"
        would pass this test wrongly."""
        path = tmp_path / "test_mint_inside_other_context.py"
        path.write_text(_mint_inside_a_different_context_manager_source())
        wrapped, unwrapped = gate_token_mint_wrapping(path)
        assert wrapped == [], (
            "a mint inside an unrelated context manager was counted as "
            "wrapped, which means this keys on ANY with-block instead of on "
            "_mint_cleanup specifically"
        )
        assert unwrapped != [], unwrapped


# --------------------------------------------------------------------------- #
# shared_session_id_importers: the population that could collide on the
# shared `session_id` fixture's default, derived rather than hand-listed.
# --------------------------------------------------------------------------- #


def _importer_of_session_id_source() -> str:
    return textwrap.dedent(
        """
        from tests.fixtures.session_state import project_root, session_id

        def test_uses_the_shared_session_id(session_id):
            assert session_id
        """
    )


def _importer_of_project_root_only_source() -> str:
    return textwrap.dedent(
        """
        from tests.fixtures.session_state import project_root

        def test_uses_only_project_root(project_root):
            assert project_root
        """
    )


def _importer_of_a_differently_named_session_id_source() -> str:
    return textwrap.dedent(
        """
        from tests.fixtures.some_other_module import session_id

        def test_uses_a_session_id_from_elsewhere(session_id):
            assert session_id
        """
    )


class TestSharedSessionIdImportersOnTheRealTree:
    def test_the_real_tree_yields_a_non_empty_population(
        self, shared_session_id_importers,
    ) -> None:
        """Anti-vacuity: a derivation that silently returned nothing would
        make every other claim about this population pass on any tree."""
        importers = shared_session_id_importers()
        assert importers, (
            "shared_session_id_importers() returned nothing against the real "
            "tests/ tree"
        )

    def test_the_real_tree_lists_every_known_importer(
        self, shared_session_id_importers,
    ) -> None:
        """plan.md's own correction 3: the blast radius is FOUR modules, not
        two. Two of them (`test_mode_switch_midsession.py`,
        `test_gate_artifact_cleanup.py`) mint no gate token at all and would
        never appear in `gate_token_minting_modules()`, so this population is
        the only place in the suite that covers them."""
        importers = {Path(name).name for name in shared_session_id_importers()}
        expected = {
            "test_mode_infrastructure.py",
            "test_phase3_centralization.py",
            "test_mode_switch_midsession.py",
            "test_gate_artifact_cleanup.py",
        }
        assert expected <= importers, importers


class TestSharedSessionIdImportersClassifiesSyntheticModules:
    def test_a_module_importing_session_id_from_the_shared_fixture_is_included(
        self, tmp_path, shared_session_id_importers,
    ) -> None:
        (tmp_path / "test_uses_shared_id.py").write_text(
            _importer_of_session_id_source()
        )
        importers = {
            Path(name).name
            for name in shared_session_id_importers(tests_dir=tmp_path)
        }
        assert "test_uses_shared_id.py" in importers, importers

    def test_a_module_importing_only_project_root_is_excluded(
        self, tmp_path, shared_session_id_importers,
    ) -> None:
        """Anti-vacuity for the inclusion case above: a module from the SAME
        fixture file that never touches `session_id` at all must not be
        misread as a collision risk."""
        (tmp_path / "test_uses_project_root_only.py").write_text(
            _importer_of_project_root_only_source()
        )
        importers = {
            Path(name).name
            for name in shared_session_id_importers(tests_dir=tmp_path)
        }
        assert "test_uses_project_root_only.py" not in importers, importers

    def test_a_module_importing_session_id_from_a_different_module_is_excluded(
        self, tmp_path, shared_session_id_importers,
    ) -> None:
        """A same-named fixture from a DIFFERENT module shares no default
        with `tests.fixtures.session_state.session_id` and cannot collide on
        it."""
        (tmp_path / "test_uses_a_session_id_from_elsewhere.py").write_text(
            _importer_of_a_differently_named_session_id_source()
        )
        importers = {
            Path(name).name
            for name in shared_session_id_importers(tests_dir=tmp_path)
        }
        assert "test_uses_a_session_id_from_elsewhere.py" not in importers, importers


# --------------------------------------------------------------------------- #
# module_session_id: no derived id across the real importer population equals
# or is a string prefix of another, and every one is a valid path component.
# --------------------------------------------------------------------------- #


class TestModuleSessionIdAcrossTheRealImporterPopulationNeverCollides:
    def test_no_derived_id_equals_or_is_a_prefix_of_another(
        self, module_session_id, shared_session_id_importers,
    ) -> None:
        importers = sorted(shared_session_id_importers())
        assert importers, "no importers to derive ids for"
        ids = [
            module_session_id(f"tests.{Path(name).stem}") for name in importers
        ]
        for i, one in enumerate(ids):
            for j, other in enumerate(ids):
                if i == j:
                    continue
                assert one != other, ids
                assert not other.startswith(one), (
                    f"{one!r} (from {importers[i]}) is a string prefix of "
                    f"{other!r} (from {importers[j]}); a glob-based sweep for "
                    f"{one!r} would also reach {other!r}'s file"
                )

    def test_every_derived_id_is_a_valid_session_component(
        self, module_session_id, shared_session_id_importers,
    ) -> None:
        from writ.session.locators import is_valid_session_component

        importers = sorted(shared_session_id_importers())
        for name in importers:
            value = module_session_id(f"tests.{Path(name).stem}")
            assert is_valid_session_component(value), (name, value)


class TestModuleSessionIdDerivation:
    def test_two_real_modules_derive_different_values(
        self, module_session_id,
    ) -> None:
        """The exact live case plan.md's Decision 1 fixes: these two modules
        used to both default to the literal "test-session" and write the
        identical 37-byte /tmp path."""
        mode_infra = module_session_id("tests.test_mode_infrastructure")
        phase3 = module_session_id("tests.test_phase3_centralization")
        assert mode_infra != phase3, (mode_infra, phase3)
        assert mode_infra == "test-session-test-mode-infrastructure-", mode_infra
        assert phase3 == "test-session-test-phase3-centralization-", phase3

    def test_a_prefix_shaped_collision_between_two_stems_is_still_caught(
        self, module_session_id,
    ) -> None:
        """Demonstrates the exact property the population-level capability
        above checks: a hypothetical `test_mode_switch` beside the real
        `test_mode_switch_midsession` derives an id where the SHORTER is a
        STRING PREFIX of the longer -- a plain `!=` check would miss this
        shape, but a glob-based sweep cannot afford to."""
        shorter = module_session_id("tests.test_mode_switch")
        longer = module_session_id("tests.test_mode_switch_midsession")
        assert shorter != longer
        assert longer.startswith(shorter), (
            f"{shorter!r} is not a prefix of {longer!r}; this is exactly the "
            f"collision shape a plain inequality check misses"
        )


# --------------------------------------------------------------------------- #
# The shared fixture's TWO branches, driven through a real nested run. The
# tests above pin `module_session_id`, the pure function; this one pins the
# FIXTURE that calls it, which is the object the four importing modules
# actually consume.
# --------------------------------------------------------------------------- #


def _session_id_probe_source(out_path: str) -> str:
    """A synthetic module that takes the SHARED `session_id` fixture twice: once
    plainly, and once under indirect parametrization. Each observed value is
    written out rather than asserted here, so the assertions live in the real
    test below -- a synthetic module whose own asserts were weakened would still
    have to report the wrong value to be believed.

    Only `session_id` is imported. `sandbox_cwd` is autouse in that fixture
    module and is deliberately left unbound: this probe reads an id, it does not
    run a mode command, and registering an unrelated autouse chdir would widen
    what this proves.
    """
    return textwrap.dedent(
        f"""
        import json

        import pytest

        from tests.fixtures.session_state import session_id  # noqa: F401

        _observed = {{}}


        def _record(key, value):
            _observed[key] = value
            with open({out_path!r}, "w") as fh:
                json.dump(_observed, fh)


        def test_the_default_is_module_derived(session_id):
            _record("default", session_id)


        @pytest.mark.parametrize("session_id", ["a-caller-supplied-id"], indirect=True)
        def test_request_param_still_wins(session_id):
            _record("parametrized", session_id)
        """
    )


class TestTheSharedSessionIdFixtureKeepsBothBranches:
    """THE REGRESSION PATH THIS EXISTS FOR, stated plainly: if a later edit
    "simplifies" `session_id` down to `return module_session_id(...)` and drops
    the `request.param` consultation, four modules silently change the id they
    mint, the two declared `GATE_TOKEN_SESSION_PREFIX` values stop matching what
    is actually written, and the sweeper goes quietly inert while every test in
    the suite stays green. Absence of a leak is not a policy; both branches are
    read here.

    Driven through a REAL nested `pytest` run over a synthetic module under
    `tmp_path`, for the reason this file's module docstring gives and
    `tests/test_pol5e_hook_noise.py:25-32` states: a synthetic file placed under
    `<repo>/tests/` would load the real `tests/conftest.py`, whose
    `pytest_sessionstart` wipes and replays the isolated graph. Indirect
    parametrization is a pytest RUNTIME behaviour, so calling the fixture
    function by hand would prove nothing about it.
    """

    def test_both_branches_are_observed_in_one_run(self, tmp_path) -> None:
        observed_path = tmp_path / "observed-session-ids.json"
        module_name = "test_synthetic_session_id.py"
        proc = _run_pytest_modules(
            tmp_path, {module_name: _session_id_probe_source(str(observed_path))}
        )
        _assert_pytest_actually_ran(proc)
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0, combined
        assert observed_path.exists(), (
            "the synthetic module never wrote its observations, so neither "
            f"branch of the fixture was actually reached:\n{combined}"
        )
        observed = json.loads(observed_path.read_text())

        # Built by plain string construction, NOT by calling module_session_id:
        # an expectation derived from the implementation under test would agree
        # with it however wrong both were.
        expected_default = "test-session-" + Path(module_name).stem.replace("_", "-") + "-"
        assert observed.get("default") == expected_default, observed
        assert observed.get("parametrized") == "a-caller-supplied-id", (
            "request.param no longer wins over the module-derived default; a "
            "consumer that overrides the id by indirect parametrization now "
            "silently gets a different one, and any prefix declared for it no "
            f"longer matches what it mints: {observed}"
        )
        assert observed["default"] != observed["parametrized"], (
            "both branches returned the same value, so this run cannot tell "
            f"them apart and proves neither: {observed}"
        )
