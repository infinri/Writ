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
    def test_pre_filter_population_is_non_empty(self) -> None:
        """The population the emptiness assertion below depends on.

        Without this floor, `test_unowned_set_is_empty` could pass on a scan
        that matched nothing at all -- the vacuous-detector failure this
        program has hit three times in one session. MUTATION: this is the
        same one `test_empty_tree_returns_no_sites` names below: pointing the
        scan at a directory with no qualifying modules zeroes this out, and a
        detector whose matching silently broke in the real tree would do the
        same.
        """
        sites = inventory.daemon_reachability_skip_sites()
        assert sites != {}, (
            "daemon_reachability_skip_sites() returned no sites at all on the "
            "real tree; the unowned-set emptiness assertion beside this one "
            "would pass vacuously on a scan that matched nothing"
        )

    def test_unowned_set_is_empty(self) -> None:
        """Every module carrying a suite-port-gated skip site has an owner:
        sanctioned or self-started. A module that keeps its site and loses its
        owner (a decayed conversion, or a new `...Live` class written without
        one) fails this BY NAME rather than dropping out of the population.
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
        failure `test_pre_filter_population_is_non_empty` guards against -- a
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
