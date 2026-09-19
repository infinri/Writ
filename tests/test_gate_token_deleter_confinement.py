"""Pins plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's confinement of the five
unscoped gate-token deleters, so a per-test cleanup fixture can never delete a
live human approval again.

THE HAZARD THIS CLOSES. Five test modules
(`tests/test_gate_token_binding.py`, `tests/test_approval_evidence.py`,
`tests/test_replan_reopen_planning.py`, `tests/test_review_promote_authority.py`,
and `tests/test_phase_machine_reset.py` by IMPORT) each ran an autouse
`_no_leaked_gate_tokens` fixture that globbed the WHOLE shared
`/tmp/writ-gate-token-*` namespace before and after every test and deleted
everything that appeared in between. It could not tell a file the test wrote
from one a real Claude Code session minted in another window while the suite
ran, and deleted both. This cycle makes the deletion discriminate on the SHAPE
of the session id: a complete id drawn only from `[0-9a-f-]` could BE a real
uuid session's id, so the file carrying it is left on disk and reported
instead of removed.

FOUR NEW OBJECTS UNDER TEST, all added to the ALREADY-EXISTING
`tests/_gate_token_leak.py` (this cycle only adds to that module; it does not
build it from scratch, unlike the previous cycle's own skeleton):

    class ForeignGateTokenWarning(UserWarning): ...
        # Raised (by the rewired fixtures, not by confined_leak_sweep itself)
        # naming every left-alone path. Never a failure.

    def session_id_from_path(path: str) -> str: ...
        # The <sid> half of ".../writ-gate-token-<sid>". A basename without
        # the marker is returned whole.

    def could_be_a_live_session(session_id: str) -> bool: ...
        # True only when session_id is non-empty and drawn solely from
        # [0-9a-f-], the alphabet a real uuid4 session id is built from.

    def confined_leak_sweep(before, *, tmp_dir=TMP) -> tuple[list[str], list[str]]: ...
        # (removed, left_alone). Raises UnmeasurableTmp rather than reading an
        # empty, clean sweep.

CORRECTLY RED as of the testing phase, for the reason
`tests/test_gate_token_leak_guard.py` states for its own not-yet-real
production symbols: none of the four objects above exist in
`tests/_gate_token_leak.py` yet, and `tests/_inventory.py::gate_token_directory_deleters`
does not exist yet either. Neither absence is allowed to collapse into a bare
ImportError at collection time: `tests/_gate_token_leak.py` and
`tests/_inventory.py` ALREADY EXIST (unlike the module built from scratch in
the previous cycle), so importing them is safe; only the specific NEW
attribute is missing, and every test that needs one goes through `_require`
below, which `pytest.fail`s with its own clean "does not exist yet" reason.

THE SAFETY RULE THAT OUTRANKS EVERYTHING ELSE IN THIS FILE. The real `/tmp`
holds leftover test tokens kept as evidence for a later cycle. No test below
lists, globs, enumerates, or `os.listdir`/`os.scandir`s anything in the real
`/tmp`. ALL decision logic (`session_id_from_path`, `could_be_a_live_session`,
`confined_leak_sweep`, and the derived map) is driven under `tmp_path` through
an injected `tmp_dir=`/`tests_dir=` keyword. Only
`TestTheRealBindingFixtureConfinesItsDeletion`'s two tests touch the real
`/tmp`, and they follow the pattern `tests/test_gate_token_leak_guard.py:505-520`
already uses: plant exactly ONE file under a name the test mints itself (a
unique `uuid4()`/`uuid4().hex` suffix), remove that EXACT path in a `finally`,
and never list the directory. If you are tempted to add a `glob(...)` or
`os.listdir(...)` against `TMP`/`"/tmp"` anywhere else in this file, that is
the defect under repair, not a test for it.

WHERE THE TWO END-TO-END PROOFS COME FROM. Per plan.md's "How a still-broken
copy FAILS" section, they drive the REAL `_no_leaked_gate_tokens` fixture
exactly as pytest registers it, through a nested
`pytest -p tests.test_gate_token_binding` run over a synthetic module that
lives OUTSIDE this checkout (under `tmp_path`), mirroring `_run_pytest` at
`tests/test_gate_token_leak_guard.py:328-343`: PYTHONPATH pinned to the repo,
`PYTEST_ADDOPTS` cleared, cwd under `tmp_path` so the real `tests/conftest.py`
is never loaded. `-p tests.test_gate_token_binding` imports that module (whose
own top-level imports are stdlib plus pytest only) and registers its autouse
fixtures for the run; only the synthetic file's path is passed to collect, so
none of that module's own test classes run.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TMP = "/tmp"

# The four modules that define the fixture directly (rewired this cycle).
# tests/test_phase_machine_reset.py is deliberately NOT here: it registers the
# fixture by IMPORT (tests/test_phase_machine_reset.py:37) and must be covered
# by the map's "inherits:" arm with zero edits of its own.
DEFINING_MODULES = (
    "test_gate_token_binding.py",
    "test_approval_evidence.py",
    "test_replan_reopen_planning.py",
    "test_review_promote_authority.py",
)

# THE WHOLE REAL-TREE POPULATION, PINNED AS A MAP RATHER THAN PROBED ONE KEY AT A TIME.
# SIX members, not five, and the sixth is ACCEPTED rather than accidental: THIS module
# reads "confined" because its own unit tests below call `confined_leak_sweep` directly
# to drive it, and the derivation keys on the MECHANISM (a function that hands the
# listing and the removal to that sweeper) rather than on whether the caller happens to
# be a per-test cleanup fixture. The classification is true on its face -- those tests
# really do delete files they discovered by listing a directory, under `tmp_path` -- and
# narrowing the arm to "only fixtures" would put a NAME back in the middle of a
# population that exists precisely to survive being renamed.
#
# The assertions below probe individual keys; this map is what catches the case they
# structurally cannot: a member APPEARING or DISAPPEARING. An unpinned population that
# quietly grows or shrinks under a change to the matching is this program's recurring
# failure, and a hardcoded population is blind rather than merely stale, so the pin sits
# beside the derivation's own synthetic-module proofs instead of replacing them.
REAL_TREE_DIRECTORY_DELETERS = {
    "test_approval_evidence.py": "confined",
    "test_gate_token_binding.py": "confined",
    "test_gate_token_deleter_confinement.py": "confined",
    "test_phase_machine_reset.py": "inherits:test_gate_token_binding",
    "test_replan_reopen_planning.py": "confined",
    "test_review_promote_authority.py": "confined",
}

# The five real test-id shapes the discriminator must reject, per plan.md's
# capability list. Four are produced by a callable `_sid(label)` helper
# (TestCouldBeALiveSessionRejectsTheRealTestIdHelpers calls each one live,
# rather than restating its shape as a literal); the fifth
# (`advance-from-complete-`) is built inline at
# tests/test_phase_machine_reset.py:139 with no helper to call, so only its
# literal prefix can be pinned -- plan.md's own disclosed gap.
LIVE_SESSION_SHAPE_PREFIXES = (
    "gtb-",
    "evidence-",
    "replan-",
    "reviewpromote-",
    "advance-from-complete-",
)


# --------------------------------------------------------------------------- #
# Skeleton-safe access to the not-yet-real symbols.
# --------------------------------------------------------------------------- #


@pytest.fixture
def leak_module():
    """`tests._gate_token_leak` already exists (this cycle only ADDS four new
    objects to it), so importing the module itself cannot go red the way
    `tests/test_gate_token_leak_guard.py`'s own `leak_module` fixture reports
    for a module built from scratch. Each new symbol is fetched through
    `_require` at its own call site instead, so a missing one reports its own
    clean reason rather than one opaque collection failure covering every
    test in this file."""
    import tests._gate_token_leak as module

    return module


def _require(module, name: str):
    """Fail loudly -- never via a bare AttributeError, and never by skipping
    -- while one of this cycle's new `tests/_gate_token_leak.py` symbols does
    not exist yet. Mirrors the skeleton-safe access
    `tests/test_gate_token_leak_guard.py::gate_token_minting_modules` already
    uses for its own not-yet-real production symbol."""
    obj = getattr(module, name, None)
    if obj is None:
        pytest.fail(
            f"skeleton: tests/_gate_token_leak.py::{name} does not exist yet",
            pytrace=False,
        )
    return obj


@pytest.fixture
def gate_token_directory_deleters():
    """`tests._inventory.gate_token_directory_deleters`, failing loudly (never
    skipping) while it does not exist yet -- the same skeleton-safe access
    `tests/test_gate_token_leak_guard.py::gate_token_minting_modules` uses for
    its own not-yet-real derivation."""
    import tests._inventory as inventory

    fn = getattr(inventory, "gate_token_directory_deleters", None)
    if fn is None:
        pytest.fail(
            "skeleton: tests/_inventory.py::gate_token_directory_deleters "
            "does not exist yet",
            pytrace=False,
        )
    return fn


# --------------------------------------------------------------------------- #
# Capability 1: live_snapshot and glob.glob enumerate the same set. EXECUTED,
# not reasoned about -- this repo's own keystone that equivalence must be run
# and diffed, never asserted from reading two implementations side by side.
# --------------------------------------------------------------------------- #


class TestLiveSnapshotMatchesGlobExactly:
    """plan.md's Design question 1 claims `live_snapshot()` and
    `glob.glob(os.path.join(tmp_dir, "writ-gate-token-*"))` enumerate the same
    set for any directory. Driven under `tmp_path`, never against the real
    `/tmp`."""

    def test_the_two_readers_agree_on_four_kinds_of_file(self, leak_module, tmp_path):
        gate_token_file = tmp_path / "writ-gate-token-gtb-probe-1a2b3c4d"
        gate_token_file.write_text("a token file\n")
        sentinel = tmp_path / "writ-gate-token-leakguard-sentinel-12345"
        sentinel.write_text("not-a-token\n")
        unrelated = tmp_path / "unrelated-file.txt"
        unrelated.write_text("nothing to do with this\n")
        dotfile = tmp_path / ".writ-gate-token-hidden"
        dotfile.write_text("a dotfile\n")

        via_snapshot = leak_module.live_snapshot(tmp_dir=str(tmp_path))
        via_glob = frozenset(
            glob.glob(os.path.join(str(tmp_path), "writ-gate-token-*"))
        )

        assert via_snapshot == via_glob, (sorted(via_snapshot), sorted(via_glob))
        assert via_snapshot == {str(gate_token_file), str(sentinel)}, sorted(
            via_snapshot
        )


# --------------------------------------------------------------------------- #
# Capability 2: session_id_from_path.
# --------------------------------------------------------------------------- #


class TestSessionIdFromPath:
    def test_returns_the_suffix_after_the_marker_for_a_marker_carrying_path(
        self, leak_module,
    ) -> None:
        session_id_from_path = _require(leak_module, "session_id_from_path")
        path = "/tmp/writ-gate-token-abc-123-def"
        assert session_id_from_path(path) == "abc-123-def"

    def test_returns_the_basename_unchanged_for_a_path_without_the_marker(
        self, leak_module,
    ) -> None:
        session_id_from_path = _require(leak_module, "session_id_from_path")
        path = "/tmp/unrelated-file.txt"
        assert session_id_from_path(path) == "unrelated-file.txt"

    def test_uses_the_basename_not_the_whole_path_when_there_is_no_marker(
        self, leak_module,
    ) -> None:
        """A path whose DIRECTORY happens to contain the marker but whose
        basename does not must still return the basename alone, never an
        empty string a caller did not mean to produce."""
        session_id_from_path = _require(leak_module, "session_id_from_path")
        path = "/tmp/writ-gate-token-dir/plain-file"
        assert session_id_from_path(path) == "plain-file"


# --------------------------------------------------------------------------- #
# Capabilities 3 and 4: could_be_a_live_session's edges, and the empty-string
# divergence from prefix_is_safe.
# --------------------------------------------------------------------------- #


class TestCouldBeALiveSession:
    def test_true_for_a_dashed_uuid(self, leak_module) -> None:
        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(str(uuid.uuid4())) is True

    @pytest.mark.parametrize(
        "session_id", ["1a2b3c-", "abcdef", "-----", "0123456789abcdef-"]
    )
    def test_true_for_any_non_empty_hex_and_dash_only_string(
        self, leak_module, session_id,
    ) -> None:
        """Every character here is a hex digit or a dash -- exactly the
        alphabet a uuid4 session id is built from -- so each COULD be the
        opening or the whole of a real session's id."""
        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(session_id) is True

    @pytest.mark.parametrize(
        "session_id", list(LIVE_SESSION_SHAPE_PREFIXES) + ["test-session"]
    )
    def test_false_for_each_real_test_id_shape(self, leak_module, session_id) -> None:
        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(session_id) is False

    def test_empty_string_is_false_for_the_opposite_reason_prefix_is_safe_gives(
        self, leak_module,
    ) -> None:
        """Both functions answer False for "" and for OPPOSITE reasons, and
        the divergence is the point, not a coincidence.
        `prefix_is_safe("")` is False because an empty PREFIX is the opening
        of every id there is, so sweeping inside it is unsafe.
        `could_be_a_live_session("")` is False because an empty COMPLETE id
        cannot BE a uuid, so a file literally named
        `/tmp/writ-gate-token-` is this suite's own leak and must be removed
        and reported like any other."""
        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session("") is False
        assert leak_module.prefix_is_safe("") is False


# --------------------------------------------------------------------------- #
# Capability 5: the real callable id helpers, called live.
# --------------------------------------------------------------------------- #


class TestCouldBeALiveSessionRejectsTheRealTestIdHelpers:
    """Calls the FOUR real callable `_sid(label)` helpers live, rather than
    restating their shapes as literals, so the day one of them is rewritten as
    a bare `uuid4()` this test says so instead of quietly passing on a stale
    shape and silently un-covering that module's leaks. The fifth module
    (`tests/test_phase_machine_reset.py:139`) builds its id inline with no
    helper to call, so its literal prefix is pinned separately -- plan.md's
    own disclosed gap."""

    def test_the_gate_token_binding_helper_produces_a_rejected_id(
        self, leak_module,
    ) -> None:
        from tests.test_gate_token_binding import _sid

        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(_sid("probe")) is False

    def test_the_approval_evidence_helper_produces_a_rejected_id(
        self, leak_module,
    ) -> None:
        from tests.test_approval_evidence import _sid

        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(_sid("probe")) is False

    def test_the_replan_reopen_planning_helper_produces_a_rejected_id(
        self, leak_module,
    ) -> None:
        from tests.test_replan_reopen_planning import _sid

        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(_sid("probe")) is False

    def test_the_review_promote_authority_helper_produces_a_rejected_id(
        self, leak_module,
    ) -> None:
        from tests.test_review_promote_authority import _sid

        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        assert could_be_a_live_session(_sid("probe")) is False

    def test_the_disclosed_gap_literal_for_phase_machine_reset_is_also_rejected(
        self, leak_module,
    ) -> None:
        """`tests/test_phase_machine_reset.py:139` has no helper function to
        call; this pins the literal prefix plan.md names for it instead, and
        is the one shape in this class that a future edit to that inline
        construction cannot be seen by."""
        could_be_a_live_session = _require(leak_module, "could_be_a_live_session")
        session_id = f"advance-from-complete-{uuid.uuid4().hex[:8]}"
        assert could_be_a_live_session(session_id) is False


# --------------------------------------------------------------------------- #
# Capabilities 6, 7 and 8: confined_leak_sweep's discrimination, driven under
# tmp_path.
# --------------------------------------------------------------------------- #


class TestConfinedLeakSweepDiscriminatesOnShape:
    def test_leaves_a_uuid_shaped_new_file_on_disk_and_reports_it_left_alone(
        self, leak_module, tmp_path,
    ) -> None:
        confined_leak_sweep = _require(leak_module, "confined_leak_sweep")
        before = frozenset()
        survivor = tmp_path / f"writ-gate-token-{uuid.uuid4()}"
        survivor.write_text("a live approval\n")

        removed, left_alone = confined_leak_sweep(before, tmp_dir=str(tmp_path))

        assert removed == [], removed
        assert left_alone == [str(survivor)], left_alone
        assert survivor.exists(), (
            "a file shaped like a live session's own approval was deleted"
        )

    def test_deletes_a_non_uuid_shaped_new_file_and_reports_it_removed(
        self, leak_module, tmp_path,
    ) -> None:
        confined_leak_sweep = _require(leak_module, "confined_leak_sweep")
        before = frozenset()
        leaked = tmp_path / "writ-gate-token-gtb-probe-1a2b3c4d"
        leaked.write_text("a test's own leak\n")

        removed, left_alone = confined_leak_sweep(before, tmp_dir=str(tmp_path))

        assert left_alone == [], left_alone
        assert removed == [str(leaked)], removed
        assert not leaked.exists(), "a non-uuid-shaped leaked file was not removed"

    @pytest.mark.parametrize("shape", ["uuid", "non_uuid"])
    def test_a_file_already_in_before_is_touched_by_neither_list(
        self, leak_module, tmp_path, shape,
    ) -> None:
        """Whatever its shape, a file already present at the before-snapshot
        is not this test's to judge -- the structural argument that lets a
        pre-existing leftover, or the operator's own live approval minted
        before the run started, never fail (or lose) anything."""
        confined_leak_sweep = _require(leak_module, "confined_leak_sweep")
        if shape == "uuid":
            existing = tmp_path / f"writ-gate-token-{uuid.uuid4()}"
        else:
            existing = tmp_path / "writ-gate-token-gtb-preexisting-1a2b3c4d"
        existing.write_text("pre-existing\n")
        before = frozenset({str(existing)})

        removed, left_alone = confined_leak_sweep(before, tmp_dir=str(tmp_path))

        assert removed == [], removed
        assert left_alone == [], left_alone
        assert existing.exists()


# --------------------------------------------------------------------------- #
# Capability 9: an unmeasurable tmp_dir raises rather than reading green.
# --------------------------------------------------------------------------- #


class TestConfinedLeakSweepRefusesAnUnmeasurableTmp:
    def test_raises_rather_than_reading_an_empty_clean_sweep(
        self, leak_module, tmp_path,
    ) -> None:
        confined_leak_sweep = _require(leak_module, "confined_leak_sweep")
        missing = tmp_path / "does-not-exist"
        with pytest.raises(leak_module.UnmeasurableTmp):
            confined_leak_sweep(frozenset(), tmp_dir=str(missing))

    def test_a_scannable_empty_directory_does_not_raise(
        self, leak_module, tmp_path,
    ) -> None:
        """Anti-vacuity for the test above: proves the raise is about the
        SCAN failing, not about confined_leak_sweep always raising."""
        confined_leak_sweep = _require(leak_module, "confined_leak_sweep")
        removed, left_alone = confined_leak_sweep(frozenset(), tmp_dir=str(tmp_path))
        assert removed == []
        assert left_alone == []


# --------------------------------------------------------------------------- #
# Capabilities 10, 11 and 12: THE TWO TESTS THAT CARRY THIS CYCLE. Both drive
# the REAL _no_leaked_gate_tokens fixture exactly as pytest registers it, and
# both are the ONLY tests in this file that touch the real /tmp -- by
# planting one uniquely named file and removing that exact path in a
# `finally`, never listing the directory.
# --------------------------------------------------------------------------- #


def _run_pytest_through_the_real_binding_fixture(
    source: str, tmp_path: Path,
) -> subprocess.CompletedProcess:
    """Runs the REAL `_no_leaked_gate_tokens` fixture as pytest registers it,
    by loading `tests.test_gate_token_binding` as a plugin over a synthetic
    test file that lives OUTSIDE this checkout. Mirrors `_run_pytest` at
    `tests/test_gate_token_leak_guard.py:328-343`: PYTHONPATH pinned to the
    repo, `PYTEST_ADDOPTS` cleared, cwd under `tmp_path` so the real
    `tests/conftest.py` is never loaded. `-p tests.test_gate_token_binding`
    imports that module and registers its autouse fixtures for the whole run;
    only the synthetic file's path is passed to collect, so none of that
    module's own test classes run. `-rw` forces the warnings summary into the
    output so a `ForeignGateTokenWarning` is visible even under `-q`.
    """
    target = tmp_path / "test_synthetic_confinement.py"
    target.write_text(source)
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTEST_ADDOPTS": ""}
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", str(target),
            "-p", "tests.test_gate_token_binding", "-p", "no:cacheprovider",
            "-rw", "-q",
        ],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=180, env=env,
    )


class TestTheRealBindingFixtureConfinesItsDeletion:
    """THE TWO TESTS THAT CARRY THIS CYCLE. Both plant exactly ONE uniquely
    named file under the real `/tmp` (the mechanism this fixture polices
    hardcodes `/tmp`; see `tests/_gate_token_leak.py`'s own docstring for
    why), remove that exact path in a `finally`, and never list the
    directory -- the pattern `tests/test_gate_token_leak_guard.py:505-520`
    already uses. The leftover tokens held as evidence for a later cycle are
    unreachable by construction: nothing here can see them.
    """

    def test_a_uuid_shaped_file_minted_mid_test_survives_and_the_run_exits_zero(
        self, tmp_path,
    ) -> None:
        """Capabilities 11 and 10 (positive half). A uuid-shaped file is what
        a real Claude Code session's own approval looks like, so the REAL
        fixture must LEAVE it (not delete it), the nested run must still exit
        0 (a confined fixture does not fail the test for a file it chose to
        leave alone), and the left-alone path must be named inside a
        `ForeignGateTokenWarning` somewhere in the run's output. A copy still
        doing the unconfined delete fails this by deleting the file and
        failing the nested test."""
        survivor_id = str(uuid.uuid4())
        survivor_path = os.path.join(TMP, f"writ-gate-token-{survivor_id}")
        source = textwrap.dedent(
            f"""
            def test_mints_a_uuid_shaped_file_mid_test():
                with open({survivor_path!r}, "w") as fh:
                    fh.write("a live approval, minted mid-test\\n")
            """
        )
        try:
            proc = _run_pytest_through_the_real_binding_fixture(source, tmp_path)
            combined = proc.stdout + proc.stderr
            assert combined.strip(), (
                "empty pytest output; the run was never exercised"
            )
            assert proc.returncode == 0, combined
            assert os.path.exists(survivor_path), (
                "the fixture deleted a file shaped like a live session's own "
                "approval"
            )
            assert "ForeignGateTokenWarning" in combined, combined
            assert survivor_path in combined, combined
        finally:
            try:
                os.remove(survivor_path)
            except OSError:
                pass

    def test_a_non_uuid_shaped_file_minted_mid_test_is_deleted_and_the_run_fails(
        self, tmp_path,
    ) -> None:
        """Capabilities 12 and 10 (negative half): the ANTI-VACUITY ARM.
        Without this, a fixture that simply stopped deleting anything at all
        would still pass the test above. A non-uuid-shaped file is still this
        test's own leak: the REAL fixture must delete it, the nested run must
        exit non-zero, the failure text must name the path, and -- because
        nothing was left alone -- no `ForeignGateTokenWarning` may appear."""
        leak_id = f"leakproof-{uuid.uuid4().hex}"
        leaked_path = os.path.join(TMP, f"writ-gate-token-{leak_id}")
        source = textwrap.dedent(
            f"""
            def test_mints_a_non_uuid_shaped_file_mid_test():
                with open({leaked_path!r}, "w") as fh:
                    fh.write("a test's own leak, minted mid-test\\n")
            """
        )
        try:
            proc = _run_pytest_through_the_real_binding_fixture(source, tmp_path)
            combined = proc.stdout + proc.stderr
            assert combined.strip(), (
                "empty pytest output; the run was never exercised"
            )
            assert proc.returncode != 0, combined
            assert leaked_path in combined, combined
            assert not os.path.exists(leaked_path), (
                "a non-uuid-shaped file this test leaked was not removed"
            )
            assert "ForeignGateTokenWarning" not in combined, combined
        finally:
            try:
                os.remove(leaked_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Capabilities 13, 14, 15 and 16: gate_token_directory_deleters against the
# real tests/ tree.
# --------------------------------------------------------------------------- #


class TestGateTokenDirectoryDeletersAgainstTheRealTree:
    def test_the_real_tests_dir_yields_a_non_empty_map(
        self, gate_token_directory_deleters,
    ) -> None:
        """Reddened by a derivation that returns `{}` unconditionally, the
        vacuous-detector failure this repo's own `tests/_inventory.py` module
        docstring says it has hit three times: every other assertion about
        this map would then pass on any tree."""
        population = gate_token_directory_deleters()
        assert isinstance(population, dict)
        assert population, (
            "gate_token_directory_deleters() returned an empty map against "
            "the real tests/ tree"
        )

    def test_no_member_of_the_real_map_reads_unconfined(
        self, gate_token_directory_deleters,
    ) -> None:
        population = gate_token_directory_deleters()
        offenders = sorted(
            name for name, kind in population.items() if kind == "unconfined"
        )
        assert offenders == [], (
            f"still-unconfined directory-listing token deleters: {offenders}"
        )

    @pytest.mark.parametrize("module_name", DEFINING_MODULES)
    def test_each_defining_module_reads_confined_by_name(
        self, gate_token_directory_deleters, module_name,
    ) -> None:
        population = gate_token_directory_deleters()
        assert population.get(module_name) == "confined", (
            f"{module_name} reads {population.get(module_name)!r}, not "
            f"'confined': {population}"
        )

    def test_phase_machine_reset_inherits_from_gate_token_binding(
        self, gate_token_directory_deleters,
    ) -> None:
        """The fifth module registers the fixture by IMPORT
        (`tests/test_phase_machine_reset.py:37`), so it must be covered with
        zero edits of its own."""
        population = gate_token_directory_deleters()
        assert (
            population.get("test_phase_machine_reset.py")
            == "inherits:test_gate_token_binding"
        ), population.get("test_phase_machine_reset.py")

    def test_every_inherits_member_resolves_to_a_member_that_reads_confined(
        self, gate_token_directory_deleters,
    ) -> None:
        """What actually keeps `test_phase_machine_reset.py` covered without
        editing it: the module it inherits from must itself read 'confined',
        so the day `test_gate_token_binding.py` decays this fails by name."""
        population = gate_token_directory_deleters()
        inheritors = {
            name: kind
            for name, kind in population.items()
            if kind.startswith("inherits:")
        }
        assert inheritors, (
            "no member of the real map registers the fixture purely by "
            "import; test_phase_machine_reset.py is expected here"
        )
        for name, kind in inheritors.items():
            defining_stem = kind.split(":", 1)[1]
            matches = [key for key in population if Path(key).stem == defining_stem]
            assert matches, (
                f"{name} reads {kind!r} but no member of the map has the "
                f"stem {defining_stem!r}"
            )
            assert all(population[key] == "confined" for key in matches), (
                f"{name} inherits from {defining_stem!r}, which does not "
                f"read 'confined': {[population[key] for key in matches]}"
            )

    def test_the_real_map_membership_is_exactly_the_pinned_population(
        self, gate_token_directory_deleters,
    ) -> None:
        """The pin the key-by-key assertions above structurally cannot make: a
        member APPEARING or DISAPPEARING. `test_gate_token_deleter_confinement.py`
        is in it -- this very module, classified by the mechanism it tests,
        because its unit tests call `confined_leak_sweep` directly -- and that
        sixth entry is accepted, stated, and now guarded rather than left as an
        undocumented side effect of the delegation arm. See
        `REAL_TREE_DIRECTORY_DELETERS` for why the arm is not narrowed to fix it.
        A future change to the matching that grows or shrinks this population
        fails HERE, by name, instead of silently."""
        population = gate_token_directory_deleters()
        assert population == REAL_TREE_DIRECTORY_DELETERS, (
            "the real-tree directory-deleter population changed; every member and "
            "its classification is deliberate, so add or remove the entry here "
            "only with the reason:\n"
            f"  derived: {sorted(population.items())}\n"
            f"  pinned : {sorted(REAL_TREE_DIRECTORY_DELETERS.items())}"
        )

    def test_gate_token_protection_is_absent_from_the_map(
        self, gate_token_directory_deleters,
    ) -> None:
        """The safe pattern (`_mint_cleanup`: one named path, no listing) must
        never enter the population."""
        population = gate_token_directory_deleters()
        assert "test_gate_token_protection.py" not in population, population


# --------------------------------------------------------------------------- #
# Capability 17: the map classifies synthetic modules by MECHANISM, never by
# a hardcoded name.
# --------------------------------------------------------------------------- #


class TestGateTokenDirectoryDeletersClassifiesSyntheticModules:
    """Pinned against SYNTHETIC modules under `tmp_path`, never against the
    real tree's current wording alone, exactly as
    `tests/test_daemon_leak_guard.py::TestDaemonStartingHooksIsDerivedNotHardcoded`
    does for `daemon_starting_hooks` and
    `tests/test_gate_token_leak_guard.py::TestGateTokenMintingModulesClassifiesSyntheticModules`
    does for `gate_token_minting_modules`."""

    def test_a_name_list_and_remove_function_with_no_discriminator_reads_unconfined(
        self, tmp_path, gate_token_directory_deleters,
    ) -> None:
        (tmp_path / "test_bare_deleter.py").write_text(
            textwrap.dedent(
                """
                import glob
                import os

                def test_something():
                    for path in glob.glob("/tmp/writ-gate-token-*"):
                        os.remove(path)
                """
            )
        )
        population = gate_token_directory_deleters(tests_dir=tmp_path)
        assert population.get("test_bare_deleter.py") == "unconfined", population

    def test_the_same_function_calling_the_discriminator_reads_confined(
        self, tmp_path, gate_token_directory_deleters,
    ) -> None:
        (tmp_path / "test_confined_deleter.py").write_text(
            textwrap.dedent(
                """
                import glob
                import os
                from tests._gate_token_leak import could_be_a_live_session

                def test_something():
                    for path in glob.glob("/tmp/writ-gate-token-*"):
                        sid = path.rsplit("writ-gate-token-", 1)[-1]
                        if not could_be_a_live_session(sid):
                            os.remove(path)
                """
            )
        )
        population = gate_token_directory_deleters(tests_dir=tmp_path)
        assert population.get("test_confined_deleter.py") == "confined", population

    def test_a_module_importing_such_a_name_reads_inherits_with_the_stem(
        self, tmp_path, gate_token_directory_deleters,
    ) -> None:
        """The IMPORT arm this cycle needs: `tests/test_phase_machine_reset.py:37`
        registers the fixture by importing it rather than copying its body, so
        a derivation matching only the copied text would miss it."""
        (tmp_path / "test_defines_the_deleter.py").write_text(
            textwrap.dedent(
                """
                import glob
                import os
                from tests._gate_token_leak import could_be_a_live_session

                def _the_directory_deleter():
                    for path in glob.glob("/tmp/writ-gate-token-*"):
                        sid = path.rsplit("writ-gate-token-", 1)[-1]
                        if not could_be_a_live_session(sid):
                            os.remove(path)
                """
            )
        )
        (tmp_path / "test_imports_the_deleter.py").write_text(
            textwrap.dedent(
                """
                from test_defines_the_deleter import _the_directory_deleter  # noqa: F401

                def test_uses_it():
                    assert True
                """
            )
        )
        population = gate_token_directory_deleters(tests_dir=tmp_path)
        assert population.get("test_defines_the_deleter.py") == "confined", population
        assert (
            population.get("test_imports_the_deleter.py")
            == "inherits:test_defines_the_deleter"
        ), population

    def test_a_module_that_removes_only_a_named_path_is_absent(
        self, tmp_path, gate_token_directory_deleters,
    ) -> None:
        """The safe pattern (`_mint_cleanup` and its four siblings): removes a
        path it named in advance and lists no directory, so it must never
        enter the population."""
        (tmp_path / "test_named_path_only.py").write_text(
            textwrap.dedent(
                """
                import os

                def test_something():
                    path = "/tmp/writ-gate-token-fixed-name"
                    with open(path, "w") as fh:
                        fh.write("x")
                    os.remove(path)
                """
            )
        )
        population = gate_token_directory_deleters(tests_dir=tmp_path)
        assert "test_named_path_only.py" not in population, population

    def test_a_module_only_mentioning_the_prefix_in_a_string_is_absent(
        self, tmp_path, gate_token_directory_deleters,
    ) -> None:
        (tmp_path / "test_prose_only.py").write_text(
            textwrap.dedent(
                """
                def test_something():
                    text = "writ-gate-token-<sid> is mentioned here only"
                    assert "writ-gate-token-" in text
                """
            )
        )
        population = gate_token_directory_deleters(tests_dir=tmp_path)
        assert "test_prose_only.py" not in population, population
