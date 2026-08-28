"""Completeness: the census names the refusing-script set exactly, and the
non-refusing complement is non-empty (so the enumeration is provably complete
rather than sampled). Also pins that `pytest -m firedrill` selects this package
and nothing outside it.

Capabilities covered (plan.md / capabilities.md):
  - `pytest -m firedrill` selects every test under tests/firedrill/ and nothing
    outside it, including a drill module added without its own pytestmark.
  - The refusing-script set derived from hook source matches the declared census
    exactly, so adding a script with a refusal path and not declaring it fails
    the drill.
  - The non-refusing registered hooks are derived as the complement and the set
    is non-empty, so the enumeration is provably complete rather than sampled.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.firedrill._census import DEFERRED_SCRIPTS, deferred_scripts, refusing_scripts

REPO = Path(__file__).resolve().parent.parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"

_COMMAND_SCRIPT_RE = re.compile(r"hooks/scripts/([\w.-]+\.sh)")


def _registered_hook_scripts() -> set[str]:
    """Every hook script named in hooks/hooks.json's command strings.

    Read directly from the real repo config here (not through tests/_inventory.py):
    this is a plain, single-purpose parse of a checked-in JSON file, not the
    refusing-script derivation the plan assigns a canonical home. Duplicating THAT
    derivation would be the same defect a prior cycle already named and fixed
    (one population, one canonical count); reading hooks.json's own registration
    list is a different population (all registered scripts, not just refusing
    ones) with no existing home to duplicate.
    """
    data = json.loads(HOOKS_JSON.read_text())
    scripts: set[str] = set()
    for entries in data.get("hooks", {}).values():
        for entry in entries:
            for h in entry.get("hooks", []):
                m = _COMMAND_SCRIPT_RE.search(h.get("command", ""))
                if m:
                    scripts.add(m.group(1))
    return scripts


def _derive_refusing_scripts():
    try:
        from tests._inventory import derive_refusing_scripts
    except ImportError as exc:
        pytest.fail(
            "skeleton: tests/_inventory.py has no derive_refusing_scripts() yet "
            f"(plan.md ## Files assigns the refusing-script derivation there): {exc}"
        )
    return derive_refusing_scripts()


class TestCensusMatchesSourceDerivedSet:
    def test_declared_census_equals_the_source_derived_refusing_set(self) -> None:
        derived = set(_derive_refusing_scripts())
        declared = refusing_scripts()
        deferred = deferred_scripts()
        assert derived == declared | deferred, (
            "the census and the source-derived refusing-script set disagree.\n"
            f"in derived, neither declared nor deferred: {sorted(derived - declared - deferred)}\n"
            f"declared or deferred but not derived: {sorted((declared | deferred) - derived)}"
        )

    def test_a_deferred_script_is_named_debt_not_a_silent_gap(self) -> None:
        """A deferred entry must be a real refusing script with a stated reason.

        Guards the two ways this escape hatch could rot: a name parked here that
        the derivation no longer finds (stale debt, silently forgiven), and a
        name parked here with no reason (a filter list wearing a docstring).
        """
        deferred = deferred_scripts()
        assert deferred, "the deferred set must not be empty while coverage is partial"
        derived = set(_derive_refusing_scripts())
        assert deferred <= derived, (
            "a deferred script is no longer derived as refusing, so this entry is "
            f"stale and must be deleted: {sorted(deferred - derived)}"
        )
        assert not (deferred & refusing_scripts()), (
            "a script cannot be both declared and deferred: "
            f"{sorted(deferred & refusing_scripts())}"
        )
        for script, reason in DEFERRED_SCRIPTS.items():
            assert reason.strip(), f"{script} is deferred with no reason"

    def test_a_registered_script_with_no_refusal_path_exists_in_the_complement(
        self,
    ) -> None:
        registered = _registered_hook_scripts()
        derived_refusing = set(_derive_refusing_scripts())
        assert derived_refusing, "the source-derived refusing set must not be empty"
        non_refusing = registered - derived_refusing
        assert non_refusing, (
            "every registered hook was classified as refusing -- the derivation is "
            "sampled or wrong, not complete"
        )
        # And the complement must be a REAL subset of what is actually registered,
        # not an artifact of a derivation that invents script names.
        assert non_refusing <= registered


class TestFiredrillMarkerScopesThisPackage:
    """`pytest -m firedrill` selects this package and nothing outside it.

    None of the modules in this package declare their own `pytestmark`; the
    property under test is that `tests/firedrill/conftest.py` alone is what makes
    them selectable, which is exactly the "module added without its own
    pytestmark" case the capability names.
    """

    def test_dash_m_firedrill_selects_only_tests_under_this_directory(self) -> None:
        selected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "firedrill"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=180,
        )
        # "::" excludes the warnings-summary lines pytest -q also prints (e.g. a
        # PytestUnknownMarkWarning naming tests/firedrill/conftest.py:35), which start
        # with "tests/" too but are not collected node ids.
        node_ids = [
            line
            for line in selected.stdout.splitlines()
            if line.startswith("tests/") and "::" in line
        ]
        assert node_ids, (
            "no tests were collected under -m firedrill; stdout=" + selected.stdout[-2000:]
        )
        not_ours = [n for n in node_ids if not n.startswith("tests/firedrill/")]
        assert not not_ours, f"-m firedrill selected tests outside tests/firedrill/: {not_ours}"

    def test_dash_m_not_firedrill_selects_none_of_this_directory(self) -> None:
        # Collection is SCOPED to this package on purpose. The property is "no item in
        # here escapes the marker", which this directory alone answers; the sibling test
        # above is the one that needs the whole tree, because its property is about items
        # OUTSIDE. Collecting all of tests/ here cost 13.4s and made this the third
        # slowest test in the suite, for an answer the narrow collection gives in about a
        # second. The two tests are NOT redundant with each other: -m firedrill selecting
        # only this directory would still pass if a module in here carried no marker at
        # all, which is exactly the regression this one catches.
        excluded = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-m", "not firedrill", "tests/firedrill"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=180,
        )
        leaked = [
            line
            for line in excluded.stdout.splitlines()
            if line.startswith("tests/firedrill/") and "::" in line
        ]
        assert not leaked, f"a firedrill test leaked into -m 'not firedrill': {leaked}"
