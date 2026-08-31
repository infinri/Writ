"""Cycle K skeletons: one canonical count per population, no duplicates to go stale.

Four count pins broke during this program, each costing a full suite run to find. Adding
one doctor check in cycle I meant editing a count, a second count, a name set, and a test
whose NAME said "seventeen": four edits for one change, three of them copies.

THE DIAGNOSIS WAS CORRECTED TWICE, and both corrections shaped this file.
  1. I called it "stale line-number pins". An AST scan found three of the four were COUNTS.
  2. So I planned to remove the numbers. `tests/test_phase51_doc_counts.py` refuted that:
     it declares itself a "source-derived count regression gate", and it is the test that
     caught generated-doc drift earlier the same day. Deleting it removes a real control.

So the defect is DUPLICATION, measured: 44 (hook registrations) is asserted in 4 test files,
12 (hook events) in 7. The fix keeps ONE canonical assertion per population and derives
everywhere else from `tests/_inventory.py`.

WHY THERE IS NO GENERAL DETECTOR HERE, and why one capability from the plan is gone. The
plan said "the number 12 appears as an assertion literal in exactly ONE test file". That is
unimplementable: 12 means a hex tail length in test_decision_memory_phase3a, ENF methodology
files in test_inc11, retrieval candidates in test_retrieval_quality_telemetry, a hook count
in test_subagent_roles_location, and hook events in test_plugin_manifest. A scan by numeric
value conflates populations, and a scan that tried to classify them is the enumeration trap
this program hit four times (21, then 12, then 10, then 8 as spellings were added). These
tests therefore assert per FILE, which is a claim about cleanup that actually happened, plus
one end-to-end test of the property that makes duplication unnecessary.

Per ENF-GATE-007: skeletons written and approved before implementation.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _inventory():
    """Import the inventory or FAIL. Deliberately not importorskip: a skipped skeleton is
    not RED, and 8 of these tests skipped silently on the first run."""
    try:
        import tests._inventory as inventory
    except ImportError as exc:
        pytest.fail(f"skeleton: tests/_inventory.py does not exist yet ({exc})")
    return inventory


def _exact_count_literals(path: Path) -> list[tuple[int, int]]:
    """(lineno, value) for each `len(...) == N` / `x.count(...) == N` assertion, N >= 3.

    The same AST shape the scope was measured with, so the before and after numbers are
    comparable rather than two different questions.
    """
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return []
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for cmp in ast.walk(node.test):
            if not isinstance(cmp, ast.Compare):
                continue
            left = cmp.left
            is_len = isinstance(left, ast.Call) and getattr(left.func, "id", "") == "len"
            is_count = isinstance(left, ast.Call) and getattr(left.func, "attr", "") == "count"
            # A bare name counts too: test_pol5b4 asserts `n == 44` where n was computed
            # earlier, and the len()-only scan passed that file vacuously.
            is_name = isinstance(left, (ast.Name, ast.Subscript, ast.Attribute))
            if not (is_len or is_count or is_name):
                continue
            for op, right in zip(cmp.ops, cmp.comparators):
                if (isinstance(op, ast.Eq) and isinstance(right, ast.Constant)
                        and isinstance(right.value, int) and right.value >= 3):
                    out.append((node.lineno, right.value))
    return out


# --------------------------------------------------------------------------- #
# Capability 1, 2: the inventory derives, and a broken derivation fails loudly
# --------------------------------------------------------------------------- #

class TestTheInventoryDerivesFromTheSource:

    def test_it_exists_with_the_four_derivations(self) -> None:
        inventory = _inventory()
        _require(inventory, "hook_registrations", "hook_events", "doctor_check_names",
                 "route_tuples")

    @pytest.mark.parametrize("name", ["hook_registrations", "hook_events",
                                      "doctor_check_names", "route_tuples",
                                      "envelope_emitting_scripts", "style_swept_docs"])
    def test_each_derivation_is_non_empty(self, name) -> None:
        """ANTI-VACUITY, and it is the whole risk of this design: a derivation that
        silently returned nothing would make every dependent assertion pass on any tree,
        which is worse than the duplicated literals it replaced.

        envelope_emitting_scripts (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, CHECK 2)
        and style_swept_docs (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, style sweep
        increment 1) join the other four for the same reason: if a future edit breaks the
        scanner's own matching, this population empties out and the suite must go red
        here, which is the positive signal a check that only ever asserts emptiness
        cannot give.
        """
        inventory = _inventory()
        _require(inventory, name)
        value = getattr(inventory, name)()
        assert value, f"{name}() derived nothing"
        assert len(value) >= 3, f"{name}() derived implausibly few: {value!r}"

    def test_the_check_names_match_the_registry_exactly(self) -> None:
        """The derivation must be the SOURCE, not a copy of it."""
        inventory = _inventory()
        from writ.session import doctor

        _require(inventory, "doctor_check_names")
        assert inventory.doctor_check_names() == [name for name, _fn in doctor._CHECKS]

    def test_the_registrations_match_the_manifest(self) -> None:
        import json

        inventory = _inventory()
        _require(inventory, "hook_registrations")
        doc = json.loads((REPO / "hooks" / "hooks.json").read_text())
        commands = sum(
            1
            for entries in (doc.get("hooks") or {}).values()
            for entry in entries
            for hook in (entry.get("hooks") or [])
            if hook.get("command")
        )
        assert len(inventory.hook_registrations()) == commands


# --------------------------------------------------------------------------- #
# Capability 3, 4, 5, 6: the duplicates are gone, per file
# --------------------------------------------------------------------------- #

class TestTheDuplicatesAreGone:
    """Asserted per FILE rather than per NUMBER, because the same integer names different
    populations across the suite. Each entry below is a site this cycle de-pinned.
    """

    # (file, count, how many assertions of it may remain). Zero means the population's
    # canonical literal lives in another file; ONE means this file IS the canonical place,
    # so the number is the review gate and only the copies were removed.
    #   test_server_split_seam keeps one 57 because the ROUTE_BASELINE list it guards is
    #   defined in that same file; a second `== 57` beside it merely restated the first.
    @pytest.mark.parametrize("filename,count,allowed", [
        ("test_doctor.py", 18, 0),
        ("test_pol5b4_context_tracker_removed.py", 44, 0),
        ("test_server_split_seam.py", 57, 1),
        ("test_settings_template_sync.py", 12, 0),
        ("test_writ_install.py", 12, 0),
        ("test_plugin_manifest.py", 12, 0),
    ])
    def test_the_file_holds_no_duplicate_of_the_count(self, filename, count, allowed) -> None:
        path = TESTS / filename
        if not path.is_file():
            path = TESTS / "plugin" / filename
        found = [ln for ln, value in _exact_count_literals(path) if value == count]
        assert len(found) <= allowed, (
            f"{filename} asserts == {count} at line(s) {found}, more than the {allowed} "
            "canonical assertion(s) it is allowed; the rest should derive from "
            "tests/_inventory.py"
        )

    def test_the_plugin_routing_duplicate_is_gone(self) -> None:
        found = [ln for ln, v in _exact_count_literals(TESTS / "plugin" / "test_hooks_routing.py")
                 if v == 44]
        assert not found, f"tests/plugin/test_hooks_routing.py still asserts == 44 at {found}"

    def test_no_test_name_states_a_count(self) -> None:
        """`test_returns_exactly_seventeen_results` had to be renamed when an 18th check
        landed. A name that encodes a number is a pin the AST scan cannot see."""
        # SCOPED to the files this cycle de-pins. An earlier draft scanned the whole
        # suite and flagged test_twenty_element_p50 and test_default_limit_is_twenty, which
        # name a FIXTURE size and a default value: neither is a repo population that grows.
        owned = {"test_doctor.py", "test_settings_template_sync.py", "test_writ_install.py",
                 "test_plugin_manifest.py"}
        words = ("seventeen", "eighteen", "nineteen", "twelve", "forty")
        offenders = []
        for path in sorted(TESTS.rglob("test_*.py")):
            if path.name not in owned:
                continue
            for match in re.finditer(r"def (test_\w+)", path.read_text(errors="replace")):
                lowered = match.group(1).lower()
                if any(word in lowered for word in words):
                    offenders.append(f"{path.name}::{match.group(1)}")
        assert not offenders, f"test names encoding a count: {offenders}"

    def test_the_route_baseline_survives(self) -> None:
        """MUST-NOT-REGRESS: dropping the literal must not drop the review gate. The
        baseline LIST is what makes a route change visible."""
        source = (TESTS / "test_server_split_seam.py").read_text()
        assert "ROUTE_BASELINE" in source
        assert "len(ROUTE_BASELINE)" in source


# --------------------------------------------------------------------------- #
# Capability 7, 8: the property that makes duplication unnecessary
# --------------------------------------------------------------------------- #

class TestGrowthCostsOneEdit:

    def test_adding_a_check_moves_every_derived_expectation_together(self, monkeypatch) -> None:
        """THE POINT OF THE CYCLE, end to end. With a synthetic check appended to the
        registry, the derived inventory must follow, so a test built on it needs no edit.
        Before this cycle the same addition broke a count, a second count and a name set.
        """
        inventory = _inventory()
        from writ.session import doctor

        _require(inventory, "doctor_check_names")
        before = len(inventory.doctor_check_names())
        monkeypatch.setattr(
            doctor, "_CHECKS", list(doctor._CHECKS) + [("synthetic-probe", lambda o: None)]
        )
        after = inventory.doctor_check_names()
        assert len(after) == before + 1, after
        assert "synthetic-probe" in after

    def test_a_population_change_still_fails_the_canonical_tripwire(self, monkeypatch) -> None:
        """MUST-NOT-REGRESS, and the reason this cycle removes copies rather than checks:
        the ONE canonical assertion must still refuse a silent population change."""
        import json

        doc = json.loads((REPO / "hooks" / "hooks.json").read_text())
        real = sum(
            1
            for entries in (doc.get("hooks") or {}).values()
            for entry in entries
            for hook in (entry.get("hooks") or [])
            if hook.get("command")
        )
        source = (TESTS / "test_phase51_doc_counts.py").read_text()
        assert f"== {real}" in source or f"== {real}," in source, (
            "the canonical hook-registration tripwire no longer pins the real count; "
            "removing duplicates must not remove the review gate itself"
        )


class TestFixtureCountsAreUntouched:
    """Left alone on purpose: these count what the test itself built, so no amount of
    development can make them stale, and rewriting them would make them weaker.
    """

    @pytest.mark.parametrize("filename,expected", [
        ("test_event_buffer_durability.py", 8),
        ("test_hnsw_persistence.py", 7),
        ("test_daemon_report_truth.py", 3),
    ])
    def test_the_conservation_count_is_still_asserted(self, filename, expected) -> None:
        values = [v for _ln, v in _exact_count_literals(TESTS / filename)]
        assert expected in values, (
            f"{filename} no longer asserts == {expected}; this cycle was not supposed to "
            "touch fixture conservation counts"
        )
