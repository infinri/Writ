"""The detector that keeps the "unowned integration test" convention from
regrowing (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, decision 1-3).

`tests/_inventory.py::daemon_reachability_skip_sites(*, tests_dir=TESTS)` is
the derived MAP from test module to its suite-port-gated skip SITE LINES and
to one of three OWNER states, keyed on the ADDRESS a skip's reaching condition
resolves to rather than on a class name or a reason string:

    {
        "<module.py>": {
            "owner": "sanctioned" | "self-started" | None,
            "sites": [<1-based line number>, ...],
        },
        ...
    }

Keys are POSIX-relative paths to `tests_dir` ITSELF (not `tests_dir.parent`),
so a real-tree call keys on e.g. "test_diagnose_playbooks.py" and a synthetic
call with `tests_dir=tmp_path` keys on the bare filename planted there. This
is the one place a signature was ambiguous in the plan; the implementer
should match it, because every test below asserts on it.

THREE ASSERTIONS MAKE THIS A GUARD RATHER THAN A REPORT, and each closes a way
a derived detector has failed silently in this repo's history:

1. The unowned set (`owner is None`) is EMPTY on the real tree.
2. The pre-filter population (the map itself) is NON-EMPTY on the real tree,
   so assertion 1 cannot pass because the scan matched nothing -- the
   vacuous-detector failure this repo has hit three times.
3. A five-module SYNTHETIC tree under `tmp_path` pins one recognized shape
   each, so precision is proven rather than assumed.

Plus the MAP property (removing a sanctioned import must move a module into
the unowned set, never drop it from the population), and the Neo4j
discrimination asserted rather than argued.

RED until `tests/_inventory.py` defines `daemon_reachability_skip_sites`;
every test below raises AttributeError until it lands.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import tests._inventory as inventory

TESTS_DIR = Path(__file__).resolve().parent


def _unowned(sites: dict) -> dict:
    return {module: info for module, info in sites.items() if info.get("owner") is None}


# --------------------------------------------------------------------------- #
# 1 + 2. The real tree: the unowned set is empty, and it is empty because the
# scan matched something, not because it matched nothing.
# --------------------------------------------------------------------------- #
class TestRealTreeIsFullyOwned:
    def test_scanned_population_is_non_empty(self) -> None:
        """REPLACES `test_pre_filter_population_is_non_empty` (RETIRED, not
        merely renamed): plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 converts
        the entire CURRENT population of suite-port-gated skips (four
        modules), so `daemon_reachability_skip_sites()` is now legitimately
        `{}` on the real tree BY SUCCESS: a floor that required it non-empty
        would demand a live module stay defective forever, which is a
        property this program just finished removing and cannot honestly
        restore.

        What the retired floor actually protected (the emptiness verdict
        came from a scan that looked, not a scan that matched nothing) is
        re-based onto the SAME walk the detector iterates,
        `daemon_reachability_scanned_modules()`, so the matched map and this
        floor cannot disagree about what was scanned: one walk, two views.

        MUTATION: a narrowed glob, a wrong default `tests_dir`, or a scan that
        silently reaches nothing all zero this out, exactly as they would
        have zeroed the retired floor.
        """
        scanned = inventory.daemon_reachability_scanned_modules()
        assert scanned, (
            "daemon_reachability_scanned_modules() returned no modules at "
            "all on the real tree; test_unowned_set_is_empty beside it would "
            "pass vacuously on a scan that matched nothing"
        )

    def test_scanned_population_is_empty_on_an_empty_directory(self, tmp_path: Path) -> None:
        """Non-vacuity's other half: a function that always answered
        non-empty regardless of what tree it was pointed at would satisfy
        the assertion above without ever having looked. MUTATION: the same
        failure `test_empty_tree_returns_no_sites` guards for the matched map
        below."""
        assert list(inventory.daemon_reachability_scanned_modules(tests_dir=tmp_path)) == []

    def test_matched_map_is_a_subset_of_the_scanned_population(self) -> None:
        """`daemon_reachability_skip_sites` ITERATES the scanned list rather
        than re-deriving its own glob, so every matched module must also be a
        scanned one. MUTATION: the two functions independently walking
        different globs would let a matched module escape this population
        without failing here."""
        scanned = set(inventory.daemon_reachability_scanned_modules())
        matched = set(inventory.daemon_reachability_skip_sites())
        assert matched <= scanned, (
            f"daemon_reachability_skip_sites() matched modules the scan "
            f"never walked, which can only happen if the two functions use "
            f"different globs: {matched - scanned}"
        )

    def test_unowned_set_is_empty(self) -> None:
        """Every module carrying a suite-port-gated skip site has an owner:
        sanctioned or self-started. A module that keeps its site and loses its
        owner (a decayed conversion, or a new `...Live` class written without
        one) fails this BY NAME rather than dropping out of the population.

        On the real tree this is EMPTY BY SUCCESS after
        plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3: the four modules that
        used to populate it were converted onto a sanctioned owner or an
        OS-assigned port, so none of them names the suite address any more.
        This test keeps asserting only the UNOWNED arm, never `sites == {}`,
        because a future sanctioned or self-started member is legitimate and
        must not redden a guard aimed at a different defect. The guard's
        LIVENESS stays provable forever via two things that do not depend on
        the real tree staying non-empty: the synthetic tree's planted unowned
        modules (`TestSyntheticTreePrecision`) and the map property
        (`TestOwnerIsAMapPropertyNotAList`).
        """
        sites = inventory.daemon_reachability_skip_sites()
        unowned = _unowned(sites)
        assert unowned == {}, (
            f"these modules carry a daemon-reachability skip site with no "
            f"owner (sanctioned or self-started), so every test behind it has "
            f"never executed: {unowned}"
        )

    def test_empty_tree_returns_no_sites(self, tmp_path: Path) -> None:
        """MUTATION: pointing the scan at an empty directory is exactly the
        failure `test_scanned_population_is_non_empty` guards against: a
        detector silently scanning the wrong tree, or a glob narrowed to match
        nothing, would zero out the real-tree population and let
        `test_unowned_set_is_empty` pass vacuously.
        """
        assert inventory.daemon_reachability_skip_sites(tests_dir=tmp_path) == {}


# --------------------------------------------------------------------------- #
# 3. The synthetic five-module tree: one recognized shape each.
# --------------------------------------------------------------------------- #
class _SyntheticTree:
    """One shape per file, matching this module's own convention
    (`tests/test_count_pin_discipline.py::TestPerPatternPrecisionOnASyntheticTree`):
    each fixture module names exactly one alternative, so no synthetic file
    can mask another's absence.
    """

    IF_PROBE = "test_if_probe_unowned.py"
    EXCEPT_HANDLER = "test_except_handler_unowned.py"
    SANCTIONED = "test_sanctioned_owner.py"
    NEO4J_SKIP = "test_neo4j_unreachable_skip.py"
    PKILL_SKIP = "test_pkill_tool_skip.py"

    # Deliberately no `_port()` literal address, no write-gate alternative name
    # and no `http://localhost:8765` constant anywhere below: this module is
    # itself scanned by `daemon_reachability_skip_sites()` on the real tree
    # (the default `tests_dir=TESTS` walk reaches this file), and by Group D's
    # guard (`test_no_module_binds_a_constant_to_the_production_daemon_port`),
    # so the fixture SOURCE TEXT below is built with f-strings and function
    # calls rather than a literal that could be mistaken for a real site.

    IF_PROBE_SOURCE = (
        "import pytest\n"
        "from tests._daemon import _port\n\n\n"
        "def _probe_up() -> bool:\n"
        "    import urllib.request\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://localhost:{_port()}/health', timeout=1\n"
        "        ):\n"
        "            return True\n"
        "    except Exception:\n"
        "        return False\n\n\n"
        "def test_something():\n"
        "    if not _probe_up():\n"
        "        pytest.skip('daemon unreachable')\n"
        "    assert True\n"
    )

    EXCEPT_HANDLER_SOURCE = (
        "import urllib.error\n"
        "import urllib.request\n\n"
        "import pytest\n\n"
        "from tests._daemon import _port\n\n\n"
        "def test_something():\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://localhost:{_port()}/thing', timeout=1\n"
        "        ):\n"
        "            pass\n"
        "    except (urllib.error.URLError, OSError) as exc:\n"
        "        pytest.skip(f'unreachable: {exc}')\n"
        "    assert True\n"
    )

    SANCTIONED_SOURCE = (
        "import pytest\n\n"
        "from tests._daemon import _port, start_isolated_daemon\n\n\n"
        "def _probe_up() -> bool:\n"
        "    import urllib.request\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://localhost:{_port()}/health', timeout=1\n"
        "        ):\n"
        "            return True\n"
        "    except Exception:\n"
        "        return False\n\n\n"
        "def test_something():\n"
        "    if not _probe_up():\n"
        "        pytest.skip('daemon unreachable')\n"
        "    assert True\n"
    )

    NEO4J_SKIP_SOURCE = (
        "import pytest\n\n"
        "from tests._corpus import neo4j_reachable\n\n\n"
        "def test_something():\n"
        "    if not neo4j_reachable():\n"
        "        pytest.skip('Neo4j unreachable')\n"
        "    assert True\n"
    )

    PKILL_SKIP_SOURCE = (
        "import shutil\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.skipif(shutil.which('pkill') is None, reason='pkill required')\n"
        "def test_something():\n"
        "    assert True\n"
    )

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / self.IF_PROBE).write_text(self.IF_PROBE_SOURCE)
        (root / self.EXCEPT_HANDLER).write_text(self.EXCEPT_HANDLER_SOURCE)
        (root / self.SANCTIONED).write_text(self.SANCTIONED_SOURCE)
        (root / self.NEO4J_SKIP).write_text(self.NEO4J_SKIP_SOURCE)
        (root / self.PKILL_SKIP).write_text(self.PKILL_SKIP_SOURCE)


@pytest.fixture()
def synthetic_tree(tmp_path: Path) -> Path:
    _SyntheticTree(tmp_path)
    return tmp_path


class TestSyntheticTreePrecision:
    """Decision 2's Neo4j discriminator and decision 3's owner classification,
    each asserted on a tree the detector has never seen, not the real one."""

    def test_if_probe_shape_is_a_member_with_no_owner(self, synthetic_tree: Path) -> None:
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert _SyntheticTree.IF_PROBE in sites, (
            f"an `if <probe>` guard resolving to _port() must be a member; "
            f"saw {sorted(sites)}"
        )
        assert sites[_SyntheticTree.IF_PROBE]["owner"] is None

    def test_except_handler_shape_is_a_member_with_no_owner(self, synthetic_tree: Path) -> None:
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert _SyntheticTree.EXCEPT_HANDLER in sites, (
            f"a pytest.skip inside an except handler around a _port()-derived "
            f"address must be a member; saw {sorted(sites)}"
        )
        assert sites[_SyntheticTree.EXCEPT_HANDLER]["owner"] is None

    def test_sanctioned_owner_shape_is_a_member_with_sanctioned_owner(
        self, synthetic_tree: Path
    ) -> None:
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert _SyntheticTree.SANCTIONED in sites, (
            f"a module naming start_isolated_daemon must be a member; "
            f"saw {sorted(sites)}"
        )
        assert sites[_SyntheticTree.SANCTIONED]["owner"] == "sanctioned", (
            f"a module that imports start_isolated_daemon must classify as "
            f"'sanctioned'; got {sites[_SyntheticTree.SANCTIONED]!r}"
        )

    def test_neo4j_unreachable_skip_is_not_a_member(self, synthetic_tree: Path) -> None:
        """MUTATION: the discriminator this asserts against. A driver
        exception carries no HTTP address and never reaches `_port()`, so a
        future widening that starts catching Neo4j sites would put this file
        in the map and redden this test -- the exact widening decision 2
        warns against.
        """
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert _SyntheticTree.NEO4J_SKIP not in sites, (
            f"a pytest.skip('Neo4j unreachable') under not neo4j_reachable() "
            f"carries no HTTP address and must never enter the population; "
            f"saw {sorted(sites)}"
        )

    def test_pkill_tool_skip_is_not_a_member(self, synthetic_tree: Path) -> None:
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert _SyntheticTree.PKILL_SKIP not in sites, (
            f"skipif(shutil.which('pkill') is None) is a missing-TOOL skip, "
            f"not an address probe, and must never enter the population; "
            f"saw {sorted(sites)}"
        )

    def test_exactly_the_three_planted_sites_are_members(self, synthetic_tree: Path) -> None:
        """Precision in both directions at once: the two non-member shapes
        above are not smuggled in through some other match, and the map holds
        nothing this synthetic tree did not plant.
        """
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert set(sites) == {
            _SyntheticTree.IF_PROBE,
            _SyntheticTree.EXCEPT_HANDLER,
            _SyntheticTree.SANCTIONED,
        }, (
            f"expected exactly the three address-probe shapes as members; "
            f"got {sorted(sites)}"
        )
        # The SITE LINES half of the map's contract: each member carries at
        # least one 1-based line number, not just an owner verdict.
        for module, info in sites.items():
            lines = info.get("sites")
            assert isinstance(lines, list) and lines, (
                f"{module} must carry a non-empty list of skip site line "
                f"numbers; got {lines!r}"
            )
            assert all(isinstance(n, int) and n > 0 for n in lines), (
                f"{module}'s site lines must be positive 1-based integers; "
                f"got {lines!r}"
            )

    def test_scanned_population_holds_all_five_while_matched_map_holds_three(
        self, synthetic_tree: Path
    ) -> None:
        """The re-based floor's own precision arm (plan.md
        2412ba38-51e1-4b73-895b-7b240a3c21d3, decision 5): `daemon_reachability_scanned_modules`
        is the SAME walk `daemon_reachability_skip_sites` iterates, so on this
        five-module synthetic tree the scanned population must hold all five
        planted modules while the matched map holds only the three
        address-probe shapes, the superset relation that makes "scanned but
        not matched" (the Neo4j and pkill-tool shapes) a state distinguishable
        from "never scanned at all", rather than an inference.
        """
        scanned = set(inventory.daemon_reachability_scanned_modules(tests_dir=synthetic_tree))
        sites = inventory.daemon_reachability_skip_sites(tests_dir=synthetic_tree)
        assert scanned == {
            _SyntheticTree.IF_PROBE,
            _SyntheticTree.EXCEPT_HANDLER,
            _SyntheticTree.SANCTIONED,
            _SyntheticTree.NEO4J_SKIP,
            _SyntheticTree.PKILL_SKIP,
        }, f"expected all five planted modules to be SCANNED; got {sorted(scanned)}"
        assert set(sites) == {
            _SyntheticTree.IF_PROBE,
            _SyntheticTree.EXCEPT_HANDLER,
            _SyntheticTree.SANCTIONED,
        }, f"expected exactly three planted modules to be MATCHED; got {sorted(sites)}"
        assert set(sites) < scanned, (
            "the matched map must be a STRICT subset of the scanned "
            "population, so 'scanned but not matched' is a distinguishable "
            "state rather than an inference"
        )


# --------------------------------------------------------------------------- #
# The MAP property: a module that keeps its site and loses its owner fails BY
# NAME, rather than dropping out of the population silently.
# --------------------------------------------------------------------------- #
class TestOwnerIsAMapPropertyNotAList:
    def test_removing_the_owner_import_moves_the_module_into_the_unowned_set(
        self, tmp_path: Path
    ) -> None:
        """MUTATION, applied directly rather than described: the sanctioned
        module's own `start_isolated_daemon` import is deleted (the site
        itself, the `if not _probe_up(): pytest.skip(...)` guard, is left in
        place) and the tree is rescanned. A detector holding a LIST of unowned
        modules -- rather than a MAP over every site -- would let this module
        simply vanish from a report instead of reappearing with `owner=None`.
        """
        _SyntheticTree(tmp_path)
        sanctioned_path = tmp_path / _SyntheticTree.SANCTIONED
        decayed = sanctioned_path.read_text().replace(
            "from tests._daemon import _port, start_isolated_daemon\n",
            "from tests._daemon import _port\n",
        )
        assert "start_isolated_daemon" not in decayed, (
            "the fixture's own replace() must remove every mention of the "
            "sanctioned import, or this mutation proves nothing"
        )
        sanctioned_path.write_text(decayed)

        sites = inventory.daemon_reachability_skip_sites(tests_dir=tmp_path)
        assert _SyntheticTree.SANCTIONED in sites, (
            "the module must still be a MEMBER after losing its owner "
            "import, not disappear from the population"
        )
        assert sites[_SyntheticTree.SANCTIONED]["owner"] is None, (
            "a module that keeps its daemon-reaching skip site but loses its "
            "sanctioned-owner import must move to the unowned arm, by name"
        )


# --------------------------------------------------------------------------- #
# The suite port is a MECHANISM the detector resolves, never a literal it
# spells: tests.conftest.TEST_DAEMON_PORT is the one source, so a change to
# the suite port cannot leave a stale literal in the detector.
# --------------------------------------------------------------------------- #
class TestSuitePortIsImportedNeverSpelled:
    def test_inventory_module_does_not_hardcode_the_suite_port(self) -> None:
        from tests.conftest import TEST_DAEMON_PORT

        src = (TESTS_DIR / "_inventory.py").read_text(encoding="utf-8")
        assert TEST_DAEMON_PORT not in src, (
            f"tests/_inventory.py must not hardcode the suite port literal "
            f"{TEST_DAEMON_PORT!r}; a change to tests.conftest.TEST_DAEMON_PORT "
            f"must not leave a stale literal behind here"
        )
        assert "TEST_DAEMON_PORT" in src, (
            "tests/_inventory.py must import tests.conftest.TEST_DAEMON_PORT "
            "so the suite port is a resolved mechanism, never a spelled literal"
        )


# --------------------------------------------------------------------------- #
# The D2 ratchet (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, decision 6):
# `pkill_invocation_sites()` maps module -> the line of every COMMAND LIST
# whose first element is the constant "pkill", read off the AST. That
# predicate is the MECHANISM (a process kill being spawned), not a name: it
# catches `subprocess.run(["pkill", ...])` however the pattern string is
# spelled, and deliberately does not catch `shutil.which("pkill")`, which is
# a missing-TOOL probe and stays legitimate in
# `tests/test_fix2_cache_alignment.py`'s class mark.
#
# Unlike `daemon_reachability_skip_sites`, this walk is not limited to
# `test_*.py`: `tests/_daemon.py` (not a `test_*.py` module) is one of the
# sites today and the ONLY one this cycle leaves behind, so a `test_*.py`-only
# glob would structurally miss the one legitimate spawner.
# --------------------------------------------------------------------------- #
class _PkillSyntheticTree:
    """One shape each: a module that SPAWNS pkill (a member) and a module
    that only probes for the pkill TOOL via `shutil.which` (never a member,
    the discrimination D2's own docstring states).
    """

    SPAWNS_PKILL = "test_pkill_spawn_unowned.py"
    TOOL_PROBE_ONLY = "test_pkill_tool_probe_only.py"

    SPAWNS_PKILL_SOURCE = (
        "import subprocess\n\n\n"
        "def _stop(port: int) -> None:\n"
        "    subprocess.run(['pkill', '-f', f'writ serve --port {port}'],\n"
        "                   capture_output=True)\n\n\n"
        "def test_something():\n"
        "    _stop(1)\n"
        "    assert True\n"
    )

    TOOL_PROBE_ONLY_SOURCE = (
        "import shutil\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.skipif(shutil.which('pkill') is None, reason='pkill required')\n"
        "def test_something():\n"
        "    assert True\n"
    )

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / self.SPAWNS_PKILL).write_text(self.SPAWNS_PKILL_SOURCE)
        (root / self.TOOL_PROBE_ONLY).write_text(self.TOOL_PROBE_ONLY_SOURCE)


@pytest.fixture()
def pkill_synthetic_tree(tmp_path: Path) -> Path:
    _PkillSyntheticTree(tmp_path)
    return tmp_path


class TestPkillInvocationSites:
    """RED until `tests/_inventory.py` defines `pkill_invocation_sites`; every
    test below raises AttributeError until it lands.
    """

    def test_real_tree_key_set_is_daemon_only(self) -> None:
        """Today the population is five files and six call sites
        (`tests/_daemon.py:219`, `tests/test_fix2_cache_alignment.py:57,58`,
        `tests/test_methodology_companion_orchestrator.py:132`,
        `tests/test_orchestrator_injection.py:110`,
        `tests/test_phase3b_approval_rewrap.py:95`). After this cycle's four
        conversions, `tests/_daemon.py` (the one module that builds its
        pattern from `_serve_pattern` and verifies the result) is the ONLY
        one left.

        MUTATION: reintroducing any `subprocess.run(["pkill", ...])` call to
        a module under `tests/` adds a second key and reddens this equality;
        deleting the one remaining call from `tests/_daemon.py` empties it.
        The assertion is on the KEY SET, never on the line numbers, so
        editing `tests/_daemon.py` cannot break it on its own; the lines
        travel in the failure message.
        """
        sites = inventory.pkill_invocation_sites()
        assert set(sites) == {"_daemon.py"}, (
            f"expected the pkill-invocation key set to be exactly "
            f"{{'_daemon.py'}} after this cycle's conversions; got "
            f"{sorted(sites)} (lines: {sites})"
        )

    def test_real_tree_scan_is_not_a_constant(self, tmp_path: Path) -> None:
        """Non-vacuity companion to the real-tree assertion above, proven the
        same way the skip detector's is: a function that always answered
        `{"_daemon.py"}` regardless of what tree it was pointed at would
        satisfy that equality without ever having scanned anything.
        MUTATION: a scan hardcoded to that literal, or one that silently
        reaches nothing, would still pass the assertion above; pointing the
        SAME function at an empty directory and requiring `{}` catches both.
        """
        assert inventory.pkill_invocation_sites(tests_dir=tmp_path) == {}

    def test_module_that_spawns_pkill_is_a_member(
        self, pkill_synthetic_tree: Path
    ) -> None:
        sites = inventory.pkill_invocation_sites(tests_dir=pkill_synthetic_tree)
        assert _PkillSyntheticTree.SPAWNS_PKILL in sites, (
            f"a subprocess.run(['pkill', ...]) command list must be a "
            f"member whatever its pattern string spells; saw {sorted(sites)}"
        )
        lines = sites[_PkillSyntheticTree.SPAWNS_PKILL]
        assert isinstance(lines, list) and lines and all(
            isinstance(n, int) and n > 0 for n in lines
        ), f"expected a non-empty list of positive 1-based line numbers; got {lines!r}"

    def test_tool_probe_only_is_not_a_member(self, pkill_synthetic_tree: Path) -> None:
        """MUTATION: the discrimination this asserts against. A future
        widening that started treating `shutil.which("pkill")` as a spawn
        would put this file in the map and redden this test, exactly the
        over-widening that would also break
        `test_fix2_cache_alignment.py`'s legitimate class mark."""
        sites = inventory.pkill_invocation_sites(tests_dir=pkill_synthetic_tree)
        assert _PkillSyntheticTree.TOOL_PROBE_ONLY not in sites, (
            f"shutil.which('pkill') is a missing-TOOL probe, not a spawn, "
            f"and must never enter the population; saw {sorted(sites)}"
        )

    def test_exactly_the_one_planted_spawn_is_a_member(
        self, pkill_synthetic_tree: Path
    ) -> None:
        """Precision in both directions at once, matching
        `TestSyntheticTreePrecision::test_exactly_the_three_planted_sites_are_members`'s
        convention: the non-member shape above is not smuggled in through
        some other match, and the map holds nothing this synthetic tree did
        not plant."""
        sites = inventory.pkill_invocation_sites(tests_dir=pkill_synthetic_tree)
        assert set(sites) == {_PkillSyntheticTree.SPAWNS_PKILL}, (
            f"expected exactly the one planted pkill-spawning module as a "
            f"member; got {sorted(sites)}"
        )
