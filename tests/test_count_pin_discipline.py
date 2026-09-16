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


def _call_can_write_importers(tests_dir: Path = TESTS) -> set[str]:
    """AST ORACLE for the write-gate surface population (plan.md
    dfacff61-23d5-474e-846c-2e2f0f0ea482, D2). Repo-relative POSIX paths of every
    `test_*.py` module whose PARSED imports pull `call_can_write` out of
    `tests.fixtures.session_state`.

    Lives HERE, in the test file, not in `tests/_inventory.py`: an oracle sharing a
    module with the derivation could be broken by the same edit that breaks the
    derivation, which would make it witness nothing.

    `ast.walk(tree)`, over the WHOLE tree, deliberately not `tree.body`. One real
    importer (`tests/test_w5_fixture_dedup_c.py:197`) imports inside a function
    body rather than at module level, so a body-only walk would see three
    importers instead of four and this oracle would under-report by exactly the
    case the population's derivation is supposed to still catch.

    MUST NOT reimplement the derivation's regex here: resolving `ast.ImportFrom`
    nodes is a mechanism the raw-text scan does not share, which is what lets this
    function witness membership rather than restate the scan it is checking.
    """
    importers: set[str] = set()
    for path in sorted(Path(tests_dir).rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "tests.fixtures.session_state":
                continue
            if any(alias.name == "call_can_write" for alias in node.names):
                importers.add(str(path.relative_to(REPO).as_posix()))
                break
    return importers


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
                                      "envelope_emitting_scripts", "style_swept_docs",
                                      "can_write_surface_modules", "write_gate_hook_modules",
                                      "write_gate_regression_modules", "corpus_floor"])
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

        can_write_surface_modules, write_gate_hook_modules and write_gate_regression_modules
        (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, the write-gate regression population)
        join for the same reason, with a limit stated plainly rather than implied: this
        `len(value) >= 3` floor is a floor of THREE against a population measured at
        roughly 71 members. A scan regression that narrowed the real population all the
        way down to five members would still pass this parametrize case. That gap is
        exactly why the per-pattern precision, import-oracle, subdirectory-floor and
        union-contract guards below exist; this test's non-emptiness check does not
        cover what those four cover, and nothing in this docstring should be read as
        implying otherwise.
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
# The write-gate regression population (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482):
# two arms plus a union, and the four guards that give the derivation teeth. NO COUNT
# IS PINNED ANYWHERE BELOW; every numeric literal here is a fixture the test itself
# built under tmp_path, never a measurement of the real tree.
# --------------------------------------------------------------------------- #

class TestWriteGateDerivationsExist:
    """Capability 1: the three names, and the shape every other derivation in this
    module already returns (a sorted list of `str`, forward-slash separated, no
    repeats). Reds today on the missing attribute; once implemented, reds again
    if the return shape drifts from that convention.
    """

    def test_it_exists_with_the_three_write_gate_derivations(self) -> None:
        # MUTATION: rename or remove any one of the three functions.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules", "write_gate_hook_modules",
                 "write_gate_regression_modules")

    @pytest.mark.parametrize("name", [
        "can_write_surface_modules", "write_gate_hook_modules",
        "write_gate_regression_modules",
    ])
    def test_the_derivation_returns_sorted_posix_paths_with_no_repeats(self, name) -> None:
        inventory = _inventory()
        _require(inventory, name)
        value = getattr(inventory, name)()
        assert isinstance(value, list), f"{name}() returned {type(value)}, not a list"
        assert all(isinstance(item, str) for item in value), (
            f"{name}() returned a non-string member: {value!r}"
        )
        assert value == sorted(value), f"{name}() is not sorted: {value!r}"
        assert all("\\" not in item for item in value), (
            f"{name}() returned a non-POSIX separator: {value!r}"
        )
        assert len(value) == len(set(value)), f"{name}() repeats a path: {value!r}"


class TestPerPatternPrecisionOnASyntheticTree:
    """Capabilities 3 and 4: one witness per authored alternative, per arm, built
    under `tmp_path` only. Each fixture module names exactly ONE alternative and
    nothing else either pattern recognizes, so dropping any single alternative
    from either regex reds exactly the one parametrize case built on that
    alternative, never its siblings and never the non-emptiness guard above (the
    real tree stays large enough to satisfy that regardless of which single
    alternative broke). The COUNT of witnesses (four plus four) is the predicate's
    own arity, not a snapshot of today's real-tree membership, which is why a
    literal count is legitimate in this class and nowhere else in this file.
    """

    _SURFACE_ALTERNATIVES = ("_can_write_check", "cmd_can_write", "call_can_write",
                              "can-write")
    _HOOK_ALTERNATIVES = ("writ-bash-write-gate", "pre-write-check",
                          "writ-state-write-gate", "writ-pre-write-dispatch")

    def _plant(self, tmp_path: Path, marker: str) -> str:
        """A single `test_*.py` module naming exactly `marker`, as a bare comment
        so the fixture stays valid Python with no other executable content."""
        stem = re.sub(r"[^a-z0-9]+", "_", marker.lower()).strip("_")
        name = f"test_{stem}.py"
        (tmp_path / name).write_text(f"# fixture: mentions {marker} once, nothing else\n")
        return name

    @pytest.mark.parametrize("alternative", _SURFACE_ALTERNATIVES)
    def test_each_surface_alternative_is_found_alone(self, tmp_path, alternative) -> None:
        # MUTATION: delete `alternative` from the surface pattern's alternation.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules")
        name = self._plant(tmp_path, alternative)
        found = inventory.can_write_surface_modules(tests_dir=tmp_path)
        assert len(found) == 1 and found[0].endswith(name), (
            f"{alternative!r} alone did not surface {name}: {found!r}"
        )

    @pytest.mark.parametrize("alternative", _HOOK_ALTERNATIVES)
    def test_each_hook_alternative_is_found_alone(self, tmp_path, alternative) -> None:
        # MUTATION: delete `alternative` from the hook pattern's alternation, or
        # drop `writ-pre-write-dispatch` specifically (the measured correction).
        inventory = _inventory()
        _require(inventory, "write_gate_hook_modules")
        name = self._plant(tmp_path, alternative)
        found = inventory.write_gate_hook_modules(tests_dir=tmp_path)
        assert len(found) == 1 and found[0].endswith(name), (
            f"{alternative!r} alone did not surface {name}: {found!r}"
        )


class TestTheImportOracleWitnessesSurfaceMembership:
    """Capability 5: an oracle independent of the regex. `_call_can_write_importers`
    (this file, above) resolves `ast.ImportFrom` nodes; the derivation is a regex
    over raw file text; the two disagree by construction rather than one
    restating the other, so this is not the tautology "x is in scan(x)".
    """

    def test_every_real_importer_of_call_can_write_is_in_the_surface_population(self) -> None:
        # MUTATION: drop `call_can_write` from the surface pattern's alternation.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules")
        importers = _call_can_write_importers()
        assert importers, (
            "the oracle itself found no real importer of call_can_write to witness "
            "with; that is a fact about the tree, not about the derivation, and "
            "would make this guard vacuous"
        )
        population = set(inventory.can_write_surface_modules())
        missing = importers - population
        assert not missing, (
            f"real importers of call_can_write are missing from "
            f"can_write_surface_modules(): {sorted(missing)}"
        )

    def test_the_oracle_itself_finds_the_nested_function_body_importer(self) -> None:
        """PASSES ALREADY, because `_call_can_write_importers` above is written
        correctly (ast.walk over the WHOLE tree). This is not a skeleton gap; it
        guards the oracle helper's own correctness against a future edit that
        narrows it to `tree.body` only, which would make it silently under-report
        by exactly the one importer whose import statement sits inside a function
        (tests/test_w5_fixture_dedup_c.py:197) rather than at module level.

        MUTATION: change `_call_can_write_importers` to iterate `tree.body`
        instead of `ast.walk(tree)`.
        """
        importers = _call_can_write_importers()
        assert "tests/test_w5_fixture_dedup_c.py" in importers, importers


class TestUnionCarriesBothTestSubpackages:
    """Capability 6: the floor floors DIRECTORIES, not module names. Narrowing the
    population's `rglob("test_*.py")` to `glob("test_*.py")` is a one-character
    edit that silently drops both `tests/firedrill/` and `tests/plugin/` while
    leaving roughly 60 members in place, so non-emptiness, every synthetic
    per-pattern case above and the import oracle all stay green; this is the only
    guard among the four that would notice.
    """

    def test_at_least_one_member_is_under_tests_firedrill(self) -> None:
        # MUTATION: change the population's `rglob` to `glob`.
        inventory = _inventory()
        _require(inventory, "write_gate_regression_modules")
        modules = inventory.write_gate_regression_modules()
        firedrill = [m for m in modules if m.startswith("tests/firedrill/")]
        assert firedrill, f"no write-gate member under tests/firedrill/: {modules!r}"

    def test_at_least_one_member_is_under_tests_plugin(self) -> None:
        # MUTATION: change the population's `rglob` to `glob`.
        # tests/plugin/ contributes exactly one member today (test_hooks_routing.py),
        # so this arm of the floor is tight by construction, not generous.
        inventory = _inventory()
        _require(inventory, "write_gate_regression_modules")
        modules = inventory.write_gate_regression_modules()
        plugin = [m for m in modules if m.startswith("tests/plugin/")]
        assert plugin, f"no write-gate member under tests/plugin/: {modules!r}"


class TestUnionEqualsTheSetUnionOfBothArms:
    """Capability 7: restates the implementation, on purpose. A union that
    silently returned a single arm would pass non-emptiness, every synthetic
    per-pattern case and the import oracle above; nothing else in this file
    would see it, which is why the union gets its own explicit contract.
    """

    def test_union_equals_the_set_union_of_the_two_arms(self) -> None:
        # MUTATION: make write_gate_regression_modules `return` one arm verbatim.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules", "write_gate_hook_modules",
                 "write_gate_regression_modules")
        surface = set(inventory.can_write_surface_modules())
        hooks = set(inventory.write_gate_hook_modules())
        union = set(inventory.write_gate_regression_modules())
        assert union == surface | hooks, (
            f"union is not the set union of the two arms: union has "
            f"{union ^ (surface | hooks)} extra or missing"
        )
        assert surface <= union, "the surface arm is not a subset of the union"
        assert hooks <= union, "the hook arm is not a subset of the union"

    def test_union_has_no_repeated_path(self) -> None:
        inventory = _inventory()
        _require(inventory, "write_gate_regression_modules")
        union = inventory.write_gate_regression_modules()
        assert len(union) == len(set(union)), f"union repeats a path: {union!r}"


class TestCommentAndDocstringMentionsCountTowardMembership:
    """Capability 8, and the polarity argument stated as an executable assertion
    rather than left in prose: CHECK 1, CHECK 2, CHECK 3 and the style ratchet in
    `tests/_inventory.py` report VIOLATIONS, where a comment match is a false
    alarm that would block a correct change, which is why `_blanked_source`
    exists for them. This population instead SELECTS TESTS TO RUN: a false
    negative here is a shipped regression, which is the defect class this whole
    cycle exists to close. So the derivation must read raw file text and must
    NOT reuse `_blanked_source`.
    """

    def test_a_pattern_named_only_in_a_module_docstring_is_a_member(self, tmp_path) -> None:
        # MUTATION: narrow the derivation to skip triple-quoted string literals
        # (docstrings) before scanning, e.g. by extracting only executable source
        # via an AST body walk. This is a DIFFERENT narrowing than reading
        # `_blanked_source` (below), which blanks only whole-line `#` comments and
        # leaves docstrings untouched.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules")
        (tmp_path / "test_docstring_only.py").write_text(
            '"""This module only discusses call_can_write in its module docstring."""\n'
        )
        found = inventory.can_write_surface_modules(tests_dir=tmp_path)
        assert len(found) == 1 and found[0].endswith("test_docstring_only.py"), found

    def test_a_pattern_named_only_in_a_comment_is_a_member(self, tmp_path) -> None:
        # MUTATION: change the derivation to scan `_blanked_source(path)` instead
        # of the file's raw text; `_blanked_source` blanks whole-line `#` comments,
        # which is exactly what this fixture's only mention sits inside.
        inventory = _inventory()
        _require(inventory, "write_gate_hook_modules")
        (tmp_path / "test_comment_only.py").write_text(
            "# this test only names writ-pre-write-dispatch in a trailing comment\n"
        )
        found = inventory.write_gate_hook_modules(tests_dir=tmp_path)
        assert len(found) == 1 and found[0].endswith("test_comment_only.py"), found


class TestEmptyTreeAndTheHelperPyBoundary:
    """Capability 9: the two edge cases at the population's own boundary. An
    empty synthetic tests directory must degrade to `[]` on all three
    derivations rather than raising, and a same-directory `helper.py` that names
    a pattern is not a member, because it is not a `test_*.py` module and pytest
    itself would never collect it either.
    """

    def test_all_three_derivations_return_empty_list_on_an_empty_tree(self, tmp_path) -> None:
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules", "write_gate_hook_modules",
                 "write_gate_regression_modules")
        assert inventory.can_write_surface_modules(tests_dir=tmp_path) == []
        assert inventory.write_gate_hook_modules(tests_dir=tmp_path) == []
        assert inventory.write_gate_regression_modules(tests_dir=tmp_path) == []

    def test_a_non_test_helper_module_naming_a_pattern_is_not_a_member(self, tmp_path) -> None:
        # MUTATION: widen the collection glob from `test_*.py` to `*.py`.
        inventory = _inventory()
        _require(inventory, "can_write_surface_modules")
        (tmp_path / "helper.py").write_text("call_can_write = None  # not a test module\n")
        assert inventory.can_write_surface_modules(tests_dir=tmp_path) == []


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


# --------------------------------------------------------------------------- #
# The corpus floor (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, defect 1).
# `corpus_floor(*, dump=None)` derives label to count from the tracked
# writ-corpus.cypher. EVERY exact map below is asserted against a dump this test
# wrote under tmp_path, so the literals describe the FIXTURE and never the repo's
# current corpus size; the only assertion made against the real dump is
# derived-against-derived, because a test asserting the real per-label number
# would recreate the hand-written population this cycle removes.
# --------------------------------------------------------------------------- #


def _create_line(label: str, key: str) -> str:
    """One node-creating line in the dump's own shape."""
    return "CREATE (:%s {name: '%s', _dump_id: '%s'});" % (label, key, key)


def _write_dump(tmp_path: Path, lines: list[str]) -> Path:
    dump = tmp_path / "writ-corpus.cypher"
    dump.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dump


def _create_lines_in(path: Path) -> int:
    """An ORACLE for the real dump's node-creating line count, deliberately not the
    derivation's mechanism: a plain `str.startswith` over the file's lines, no regex,
    no label capture. It answers "how many lines create a node" without answering
    "which label", which is what lets it witness the derivation's total instead of
    restating it.
    """
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("CREATE (:")
    )


class TestCorpusFloorDerivesFromTheDump:
    """Capabilities 1 and 3: the derivation reads the tracked dump, is exact about
    what it counts, and refuses with a remedy when the dump is not there.
    """

    def test_it_returns_exactly_the_labels_and_counts_in_a_synthetic_dump(
        self, tmp_path
    ) -> None:
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        dump = _write_dump(tmp_path, [
            _create_line("Rule", "R-1"),
            _create_line("Rule", "R-2"),
            _create_line("Rule", "R-3"),
            _create_line("Widget", "W-1"),
            _create_line("Widget", "W-2"),
            _create_line("Gadget", "G-1"),
        ])
        assert inventory.corpus_floor(dump=dump) == {"Rule": 3, "Widget": 2, "Gadget": 1}

    def test_the_label_list_comes_from_the_dump_not_from_a_declared_list(
        self, tmp_path
    ) -> None:
        """The second-order half of defect 1: `_LABELS` could not floor Abstraction or
        Category because nobody had written them down. A label the derivation has never
        heard of must enter the population purely because the dump creates it.
        """
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        dump = _write_dump(tmp_path, [_create_line("NeverDeclaredAnywhere", "X-1")])
        assert inventory.corpus_floor(dump=dump) == {"NeverDeclaredAnywhere": 1}

    def test_lines_that_do_not_create_a_node_are_not_counted(self, tmp_path) -> None:
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        dump = _write_dump(tmp_path, [
            "// a comment naming CREATE (:Rule { and nothing else",
            ":begin",
            "CREATE INDEX rule_id_idx FOR (n:Rule) ON (n.rule_id);",
            _create_line("Rule", "R-1"),
            "MATCH (a:Rule), (b:Rule) CREATE (a)-[:RELATES_TO]->(b);",
            "  " + _create_line("Rule", "R-indented"),
            ":commit",
        ])
        assert inventory.corpus_floor(dump=dump) == {"Rule": 1}

    def test_a_multi_label_node_line_raises_and_names_the_line(self, tmp_path) -> None:
        """Capability 2, the one shape that could make this derivation UNDER-count in
        silence. `CREATE (:A:B {` is not matched by a single-label pattern, so it would
        drop out of the population without a trace; the sum-versus-lines reconciliation
        turns that into a refusal that names the offending line.
        """
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        offending = "CREATE (:Rule:Deprecated {name: 'R-2', _dump_id: 'R-2'});"
        dump = _write_dump(tmp_path, [_create_line("Rule", "R-1"), offending])

        with pytest.raises(Exception) as excinfo:
            inventory.corpus_floor(dump=dump)
        message = str(excinfo.value)
        assert offending in message, (
            "the refusal must quote the line it could not classify, or the reader has "
            f"to find it themselves: {message!r}"
        )

    def test_an_absent_dump_raises_naming_the_path_and_the_way_out(
        self, tmp_path
    ) -> None:
        """Capability 3. This derivation runs at IMPORT of tests/_corpus.py, so a bare
        traceback here surfaces as a collection error with no remedy attached, which is
        the deadlock shape this repo has already shipped once.
        """
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        absent = tmp_path / "writ-corpus.cypher"

        with pytest.raises(Exception) as excinfo:
            inventory.corpus_floor(dump=absent)
        message = str(excinfo.value)
        assert str(absent) in message, f"the refusal must name the path it read: {message!r}"
        assert "export-cypher" in message, (
            "a guard that names no action is a deadlock: the refusal must name the "
            f"command that produces the dump: {message!r}"
        )

    def test_on_the_real_dump_the_map_sums_to_the_dumps_own_node_line_count(self) -> None:
        """DERIVED AGAINST DERIVED, the only assertion this file makes about the real
        corpus. Both sides move together when the corpus grows, so no number here can
        go stale; what it catches is the derivation silently dropping node lines, which
        is the failure that would re-empty the floor.
        """
        inventory = _inventory()
        _require(inventory, "corpus_floor")
        from tests._graph import DUMP_FILENAME

        dump = REPO / DUMP_FILENAME
        assert dump.is_file(), f"the tracked corpus dump is missing at {dump}"
        floor = inventory.corpus_floor()
        assert floor, "corpus_floor() derived no labels from the real dump"
        assert sum(floor.values()) == _create_lines_in(dump), (
            "the derived floor does not account for every node-creating line in the "
            f"dump: derived {sum(floor.values())} across {sorted(floor)}, but the dump "
            f"has {_create_lines_in(dump)} node-creating lines"
        )
