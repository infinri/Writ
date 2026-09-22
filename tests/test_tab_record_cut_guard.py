"""tests/test_tab_record_cut_guard.py

Pins plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3's structural half of the fix: every
`cut -f` site that parses a tab-separated record built the way
`bin/lib/gate_advance_outcome.py` prints one (fixed fields first, one unbounded and
possibly multi-line error field last) must read its FIXED fields from a first-line
restriction of the raw value, and its TRAILING unbounded field must not be so
restricted (## Analysis: "the property is one sentence: the four FIXED fields are read
from the first line only, and the trailing unbounded field is not").

THE DETECTOR IS DERIVED, not a per-file assertion, per ## Analysis ("The detector, and
why it is derived"): `tests/_inventory.py` gains a scan spec over `hooks/scripts/*.sh`
and `bin/lib/*.sh` -- two trees, no filename named anywhere in the derivation, in the
shape this repo's other populations already use (comments blanked in place so line
numbers stay true, a `spec`/`base` keyword pair so the same functions can be pointed at
a synthetic tree under `tmp_path`). This module imports `tests._inventory` as a module
(`import tests._inventory as inventory`) rather than naming its individual functions at
module scope, because those functions do not exist on disk until the implementation
cycle lands them: a `from tests._inventory import ...` here would fail COLLECTION
itself, not just the tests, and this suite is a pre-implementation skeleton (TEST-TDD-001
/ SKL-PROC-WRIT-FAILURE-001).

THE FOUR FUNCTIONS THIS MODULE EXERCISES (## Analysis, named so a reader does not have
to cross-reference plan.md to follow the assertions below):
  * `inventory.tab_record_cut_sites(*, spec=None, base=None)` -- the full population,
    `{"<relpath>:<line>": {...}}`, one entry per `cut` invocation carrying a `-f`
    selector, each classified `kind` ("fixed" or "trailing") and `guard` ("slice",
    "head" or empty).
  * `inventory.unguarded_fixed_field_cuts(*, spec=None, base=None)` -- the `fixed`
    sites whose input is neither a first-line-restricted variable nor piped through
    `head -1`/`head -n 1`. Must be empty on the real tree.
  * `inventory.truncating_trailing_cuts(*, spec=None, base=None)` -- the `trailing`
    sites, scoped to a record family that also has a fixed-field read, whose input IS
    line-restricted (which would truncate the very error text the trailing read exists
    to keep whole). Must be empty on the real tree.
  * `inventory.DECLARED_CUT_SITES` -- the known sites, each anchored on a stable clause
    of its own line, so the derivation's precision is provable and not merely assumed.

THE MUTATION PROOF NEEDS THE MUTANT TO RUN (plan.md's non-negotiables). A synthetic
scripts tree built purely from string literals under `tmp_path` cannot `source
common.sh`, but `tab_record_cut_sites` and its two derived detectors are pure source
scans -- they never execute the shell they read -- so no symlinked `bin`/`writ` layout
is needed here the way `tests/test_approval_rejection_reason_recorded.py`'s runtime
proof needs one. What DOES matter for a true proof is that the synthetic tree matches
the shape `_CUT_SCAN_SPEC` globs (`hooks/scripts/*.sh` and `bin/lib/*.sh`), so a
detector pointed at a `spec` override built from `tmp_path` is the SAME code path the
real-tree assertions exercise, not a hand-rolled parallel implementation of the rule
(a "guard that MODELS a path is blind to it").

Every negative assertion below (population empty, detector silent) has a positive
control in the same class or its adjacent sibling class, per this cycle's
non-negotiables: an always-empty detector must be shown to redden on a synthetic
violation before its silence on the real tree means anything.

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
Every test body below was `raise NotImplementedError` when this module was approved; the
implementation cycle filled them in against the derivation plan.md's ## Analysis names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tests._inventory as inventory  # noqa: F401 -- real import; attributes accessed
# only inside test bodies, never at collection time, because `tab_record_cut_sites` and
# its siblings did not exist on disk when this module was approved and collection had to
# succeed before the implementation cycle landed them.

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPTS_DIR = REPO_ROOT / "hooks" / "scripts"
LIB_DIR = REPO_ROOT / "bin" / "lib"
HOOK_PATH = HOOK_SCRIPTS_DIR / "auto-approve-gate.sh"


# ---------------------------------------------------------------------------
# Four synthetic single-file sources, one per direction the mutation proof (plan.md
# ## Capabilities item 10 / ## Test design) enumerates. Named at module scope, plain
# strings, so a reviewer can diff the four against each other by eye: each changes
# exactly ONE thing relative to `_CONTROL_SOURCE`, which is the shape that obeys both
# rules and must report nothing.
# ---------------------------------------------------------------------------

# A record family: RAW is the raw multi-line value, RAW_LINE is its first-line slice.
# The control reads every FIXED field from RAW_LINE and the TRAILING field from RAW
# itself -- the property this whole detector exists to hold.
_CONTROL_SOURCE = """#!/bin/bash
RAW=$(printf 'rejected\\t\\t\\t\\tfirst line of the error\\nsecond line')
RAW_LINE=${RAW%%$'\\n'*}
OUTCOME=$(printf '%s' "$RAW_LINE" | cut -f1)
VALIDATED=$(printf '%s' "$RAW_LINE" | cut -f3)
TOKEN_SPENT=$(printf '%s' "$RAW_LINE" | cut -f4)
GATE_ERROR=$(printf '%s' "$RAW" | cut -f5-)
"""

# Violates the FIXED-field rule: OUTCOME reads $RAW directly, with no slice and no
# `head -1` anywhere in its pipeline. This is the defect plan.md pins verbatim (the
# pre-fix line 491).
_UNRESTRICTED_FIXED_READ_SOURCE = """#!/bin/bash
RAW=$(printf 'rejected\\t\\t\\t\\tfirst line of the error\\nsecond line')
OUTCOME=$(printf '%s' "$RAW" | cut -f1)
"""

# Violates the TRAILING-field rule via the slice mechanism: GATE_ERROR reads
# $RAW_LINE, the same first-line-restricted variable the fixed fields use, so a
# multi-line error would be truncated to its first line -- the opposite defect from the
# one this cycle fixes, and the reason `truncating_trailing_cuts` exists at all.
_TRAILING_FROM_SLICE_SOURCE = """#!/bin/bash
RAW=$(printf 'rejected\\t\\t\\t\\tfirst line of the error\\nsecond line')
RAW_LINE=${RAW%%$'\\n'*}
OUTCOME=$(printf '%s' "$RAW_LINE" | cut -f1)
GATE_ERROR=$(printf '%s' "$RAW_LINE" | cut -f5-)
"""

# Violates the TRAILING-field rule via the other line-restriction mechanism: GATE_ERROR
# is piped through `head -1` before `cut -f5-`, which truncates it exactly as the slice
# variant above does, by a different route.
_TRAILING_WITH_HEAD_SOURCE = """#!/bin/bash
RAW=$(printf 'rejected\\t\\t\\t\\tfirst line of the error\\nsecond line')
OUTCOME=$(printf '%s' "$RAW" | head -1 | cut -f1)
GATE_ERROR=$(printf '%s' "$RAW" | head -1 | cut -f5-)
"""


def _write_synthetic_cut_tree(tmp_path: Path, hook_source: str) -> tuple[Path, tuple[tuple[Path, str], ...]]:
    """Write `hook_source` under `<tmp_path>/hooks/scripts/synthetic.sh`, matching the
    two-tree glob shape `_CUT_SCAN_SPEC` uses in `tests/_inventory.py`
    (`hooks/scripts/*.sh` and `bin/lib/*.sh`); return the written file's path and a
    `spec` tuple pointed at `tmp_path`'s two trees, ready to pass as
    `inventory.tab_record_cut_sites(spec=spec, base=tmp_path)`.

    The `bin/lib` half of the spec is created empty (directory only, no `.sh` files),
    so a caller asserting on the synthetic tree sees only the one file it wrote and the
    detector still has both directories to glob without raising on a missing one.
    """
    scripts = tmp_path / "hooks" / "scripts"
    lib = tmp_path / "bin" / "lib"
    scripts.mkdir(parents=True, exist_ok=True)
    lib.mkdir(parents=True, exist_ok=True)
    written = scripts / "synthetic.sh"
    written.write_text(hook_source, encoding="utf-8")
    return written, ((scripts, "*.sh"), (lib, "*.sh"))


def _cut_site_findings_for(tmp_path: Path, hook_source: str) -> dict:
    """Run `inventory.tab_record_cut_sites`, `inventory.unguarded_fixed_field_cuts`
    and `inventory.truncating_trailing_cuts` against a synthetic tree built from
    `hook_source` via `_write_synthetic_cut_tree`; return
    `{"sites": ..., "unguarded_fixed": ..., "truncating_trailing": ...}` so a test can
    assert on whichever finding set its scenario is about without repeating the
    three-call boilerplate."""
    _written, spec = _write_synthetic_cut_tree(tmp_path, hook_source)
    return {
        "sites": inventory.tab_record_cut_sites(spec=spec, base=tmp_path),
        "unguarded_fixed": inventory.unguarded_fixed_field_cuts(spec=spec, base=tmp_path),
        "truncating_trailing": inventory.truncating_trailing_cuts(spec=spec, base=tmp_path),
    }


# ---------------------------------------------------------------------------
# Capability 7: the population itself -- glob-derived, non-empty, classified, and the
# declared sites resolve.
# ---------------------------------------------------------------------------


class TestCutSitePopulationIsGlobDerivedAndNonEmpty:
    """`inventory.tab_record_cut_sites()` on the REAL tree finds every `cut -f` site by
    globbing `hooks/scripts/*.sh` and `bin/lib/*.sh` -- no filename named anywhere in
    the derivation -- is non-empty, and classifies every site `kind` "fixed" or
    "trailing" (plan.md ## Capabilities item 7).

    PASSES VACUOUSLY IF: the population were allowed to be empty and every per-site
    assertion elsewhere in this module still reported green on zero iterations -- this
    class's non-emptiness check is what closes that gap for every other class here.
    """

    def test_the_population_is_non_empty_on_the_real_tree(self) -> None:
        sites = inventory.tab_record_cut_sites()
        assert sites, (
            "the derived cut-site population is empty on the real tree, so every "
            "per-site assertion in this module would report green on zero iterations"
        )

    def test_every_site_in_the_population_is_classified_fixed_or_trailing(self) -> None:
        sites = inventory.tab_record_cut_sites()
        assert sites
        for key, site in sites.items():
            assert site["kind"] in ("fixed", "trailing"), (
                f"{key} is classified {site['kind']!r}, which is neither a bounded "
                "fixed-field read nor a trailing unbounded one"
            )
            # The classification must follow the SELECTOR, not a name: an open range is
            # the unbounded field and nothing else is.
            assert site["kind"] == (
                "trailing" if site["selector"].endswith("-") else "fixed"
            ), f"{key} selector {site['selector']!r} disagrees with kind {site['kind']!r}"

    def test_the_known_hook_sites_at_lines_491_495_496_498_and_500_are_all_present(
        self,
    ) -> None:
        """The five sites plan.md's ## Analysis names by line number
        (`hooks/scripts/auto-approve-gate.sh:491,495,496,498,500`) must all appear in
        the derived population under their real, current line numbers -- the positive
        control that the glob actually reaches the hook script and not just the lib
        tree."""
        expressions = {
            'OUTCOME=$(printf \'%s\' "$OUTCOME_LINE" | cut -f1)': "fixed",
            'VALIDATED=$(printf \'%s\' "$OUTCOME_LINE" | cut -f3)': "fixed",
            'TOKEN_SPENT=$(printf \'%s\' "$OUTCOME_LINE" | cut -f4)': "fixed",
            'ADVANCED_TO=$(printf \'%s\' "$OUTCOME_LINE" | cut -f2)': "fixed",
            'GATE_ERROR=$(printf \'%s\' "$OUTCOME_RAW" | cut -f5-)': "trailing",
        }
        rel = HOOK_PATH.relative_to(REPO_ROOT).as_posix()
        lines = HOOK_PATH.read_text(encoding="utf-8").split("\n")
        sites = inventory.tab_record_cut_sites()
        records = set()
        for expression, kind in expressions.items():
            # The LINE is derived from the file, never written down here: a literal would
            # go stale on the next edit above the block while saying nothing about which
            # site it meant.
            numbers = [n for n, line in enumerate(lines, start=1) if expression in line]
            assert len(numbers) == 1, (
                f"expected exactly one line carrying {expression!r} in {rel}; "
                f"found {numbers}"
            )
            key = f"{rel}:{numbers[0]}"
            assert key in sites, (
                f"the derivation did not reach {key} ({expression!r}), so the glob is "
                "not seeing the hook script"
            )
            assert sites[key]["kind"] == kind, (
                f"{key} must be classified {kind!r}; got {sites[key]['kind']!r}"
            )
            records.add(sites[key]["record"])
        assert len(records) == 1, (
            "the five sites parse ONE record, so a read of the first-line derivative and "
            f"a read of the raw value must resolve to one family; got {sorted(records)}"
        )

    def test_the_population_is_unaffected_by_which_file_is_named_first_on_disk(
        self, tmp_path,
    ) -> None:
        """Two synthetic files, each holding one known-violating pattern, in
        alphabetically REVERSED order relative to a second run -- the population must
        contain both regardless of directory iteration order, proving the derivation
        globs rather than hardcoding a single expected filename."""
        def findings_for(root: Path, first_name: str, second_name: str) -> set:
            scripts = root / "hooks" / "scripts"
            lib = root / "bin" / "lib"
            scripts.mkdir(parents=True, exist_ok=True)
            lib.mkdir(parents=True, exist_ok=True)
            (scripts / first_name).write_text(
                _UNRESTRICTED_FIXED_READ_SOURCE, encoding="utf-8"
            )
            (scripts / second_name).write_text(
                _TRAILING_FROM_SLICE_SOURCE, encoding="utf-8"
            )
            spec = ((scripts, "*.sh"), (lib, "*.sh"))
            reported = set(inventory.unguarded_fixed_field_cuts(spec=spec, base=root))
            reported |= set(inventory.truncating_trailing_cuts(spec=spec, base=root))
            return {key.split("/")[-1].split(":")[0] for key in reported}

        forward = findings_for(tmp_path / "forward", "aaa-first.sh", "zzz-second.sh")
        reversed_order = findings_for(tmp_path / "reversed", "zzz-first.sh", "aaa-second.sh")
        assert forward == {"aaa-first.sh", "zzz-second.sh"}, (
            f"both synthetic files must be judged, whatever their names: {forward}"
        )
        assert reversed_order == {"zzz-first.sh", "aaa-second.sh"}, (
            "swapping which violation sits in the alphabetically first file must change "
            f"nothing about what is found: {reversed_order}"
        )


class TestDeclaredCutSitesResolveByPathLineAndAnchor:
    """`inventory.DECLARED_CUT_SITES` is non-empty, and every declared site resolves:
    its path exists under the scan spec's trees, its line number is within that file's
    length, and the anchor text plan.md requires ("a stable clause of its own line") is
    found verbatim on that line (plan.md ## Capabilities item 7, "resolves every
    declared known site by path, line and anchor").

    PASSES VACUOUSLY IF: only the COUNT of declared sites were checked -- a declared
    site with a stale line number (the file moved, the anchor no longer sits there)
    would still count correctly while resolving to the wrong text.
    """

    def test_declared_cut_sites_is_non_empty(self) -> None:
        assert inventory.DECLARED_CUT_SITES, (
            "no known site is declared, so nothing proves the derivation is not blind"
        )

    def test_every_declared_site_path_exists_under_the_scan_specs_trees(self) -> None:
        directories = [root for root, _pattern in inventory._CUT_SCAN_SPEC]
        assert directories
        for name, (rel, _anchor) in inventory.DECLARED_CUT_SITES.items():
            path = REPO_ROOT / rel
            assert path.is_file(), f"declared site {name} names a missing file: {rel}"
            assert path.parent in directories, (
                f"declared site {name} sits at {rel}, which is outside the scan spec's "
                f"trees {[str(d) for d in directories]}, so the derivation never reads it"
            )

    def test_every_declared_sites_anchor_text_is_found_verbatim_on_its_declared_line(
        self,
    ) -> None:
        sites = inventory.tab_record_cut_sites()
        for name, (rel, anchor) in inventory.DECLARED_CUT_SITES.items():
            matches = [
                key for key, site in sites.items()
                if site["path"] == rel and anchor in site["text"]
            ]
            assert len(matches) == 1, (
                f"declared site {name} resolves to {len(matches)} derived site(s) in "
                f"{rel} carrying {anchor!r}: {matches}. A red here means the derivation "
                "went blind or the site was rewritten; re-point the anchor."
            )
            line_number = sites[matches[0]]["line"]
            lines = (REPO_ROOT / rel).read_text(encoding="utf-8").split("\n")
            assert 1 <= line_number <= len(lines), (
                f"declared site {name} resolved to line {line_number}, outside {rel}"
            )
            assert anchor in lines[line_number - 1], (
                f"declared site {name}'s anchor is not on the line the derivation "
                f"reports ({rel}:{line_number}): {lines[line_number - 1]!r}"
            )


# ---------------------------------------------------------------------------
# Capability 8: every fixed-field site reads a first-line-restricted input.
# ---------------------------------------------------------------------------


class TestUnguardedFixedFieldCutsIsEmptyOnTheRealTree:
    """`inventory.unguarded_fixed_field_cuts()` is empty on the real tree: every
    `fixed`-kind `cut -f` site reads either a first-line-slice variable or a value
    piped through `head -1`/`head -n 1` (plan.md ## Capabilities item 8). This is the
    property the fix (## Analysis: the `OUTCOME_LINE=${OUTCOME_RAW%%$'\\n'*}` slice
    feeding all four fixed reads) exists to establish; this test is RED against the
    hook as it stands today, where line 491's `OUTCOME` read carries neither guard.

    PASSES VACUOUSLY IF: a finding's identity (path and line) were never checked -- a
    detector that always returns an empty list regardless of input would pass this
    class alone; `TestDerivationConditionalByMutationOnASyntheticTree` is what proves
    this detector can also find something.
    """

    def test_no_fixed_field_site_in_the_real_tree_reads_an_unrestricted_input(
        self,
    ) -> None:
        findings = inventory.unguarded_fixed_field_cuts()
        assert findings == {}, (
            "a fixed classifier field is read from the whole record, so a multi-line "
            "error smears into it and no outcome comparison can match: "
            + ", ".join(f"{key} ({site['text']})" for key, site in sorted(findings.items()))
        )
        # The silence above means something only if the population it filtered is real.
        assert inventory.tab_record_cut_sites()

    def test_a_real_finding_would_name_the_offending_path_and_line(self, tmp_path) -> None:
        """A finding, when the detector produces one at all, is keyed
        `"<relpath>:<line>"` (matching `tab_record_cut_sites`' own key shape) so a
        reader is told exactly where to look rather than merely that something is
        wrong -- exercised here against `_UNRESTRICTED_FIXED_READ_SOURCE` on a
        synthetic tree, since the real tree must produce none."""
        written, _spec = _write_synthetic_cut_tree(tmp_path, _UNRESTRICTED_FIXED_READ_SOURCE)
        findings = _cut_site_findings_for(tmp_path, _UNRESTRICTED_FIXED_READ_SOURCE)
        reported = findings["unguarded_fixed"]
        assert len(reported) == 1, (
            f"the unrestricted fixed read must be reported exactly once: {reported}"
        )
        key, site = next(iter(reported.items()))
        rel = written.relative_to(tmp_path).as_posix()
        cut_lines = [
            number for number, line in enumerate(
                written.read_text(encoding="utf-8").split("\n"), start=1,
            )
            if "cut -f1" in line
        ]
        assert cut_lines == [site["line"]], (
            f"the finding names line {site['line']}, the file cuts at {cut_lines}"
        )
        assert key == f"{rel}:{site['line']}", (
            "a finding must be keyed '<relpath>:<line>' so a reader is told exactly "
            f"where to look; got {key!r}"
        )
        assert site["path"] == rel


# ---------------------------------------------------------------------------
# Capability 9: every trailing read in a record family that also has a fixed-field read
# takes the unrestricted raw value.
# ---------------------------------------------------------------------------


class TestTruncatingTrailingCutsIsEmptyOnTheRealTree:
    """`inventory.truncating_trailing_cuts()` is empty on the real tree: no
    `trailing`-kind `cut -f` site in a record family that also has a fixed-field read
    is itself line-restricted (plan.md ## Capabilities item 9). Scoped to a "record
    family" (## Analysis: "a raw variable that also feeds a fixed-field read through
    its first-line derivative") so this rule cannot false-red on an unrelated
    `cut -f2-` elsewhere in the tree that has nothing to do with this record shape.

    PASSES VACUOUSLY IF: the record-family scoping were dropped and the detector
    matched every trailing `cut -f` in the tree indiscriminately -- it would then also
    redden on a first-line-restricted trailing read that belongs to a DIFFERENT record
    with no sibling fixed-field read, which is not the defect this cycle is about.
    """

    def test_no_trailing_read_in_a_record_family_is_line_restricted_on_the_real_tree(
        self,
    ) -> None:
        findings = inventory.truncating_trailing_cuts()
        assert findings == {}, (
            "a trailing unbounded read is restricted to line one, which truncates the "
            "very error text that read exists to carry whole: "
            + ", ".join(f"{key} ({site['text']})" for key, site in sorted(findings.items()))
        )
        trailing = [
            key for key, site in inventory.tab_record_cut_sites().items()
            if site["kind"] == "trailing"
        ]
        assert trailing, (
            "the real tree holds no trailing read at all, so this detector filtered an "
            "empty set and its silence says nothing"
        )

    def test_adding_a_first_line_guard_to_the_hooks_error_read_would_redden_this_detector(
        self, tmp_path,
    ) -> None:
        """The property is proved CONDITIONAL, not merely quiet, by re-running the
        detector against a synthetic copy of the hook's own record family with a
        first-line guard added to the trailing read (mirroring
        `_TRAILING_FROM_SLICE_SOURCE`) and asserting the finding appears -- so a future
        change that "fixes" a perceived truncation bug by guarding line 500 the same
        way the fixed fields are guarded would be caught before it silently truncates
        every multi-line refusal to its first line."""
        # Derived from the hook's OWN parse block, not hand-copied: a model of the block
        # would prove the detector reddens on a shape the hook does not have.
        source = HOOK_PATH.read_text(encoding="utf-8")
        guarded = source.replace(
            'GATE_ERROR=$(printf \'%s\' "$OUTCOME_RAW" | cut -f5-)',
            'GATE_ERROR=$(printf \'%s\' "$OUTCOME_LINE" | cut -f5-)',
        )
        assert guarded != source, (
            "the mutation changed nothing: the hook no longer carries the trailing read "
            "this test guards, so the mutant proves nothing about the detector"
        )
        findings = _cut_site_findings_for(tmp_path, guarded)
        reported = findings["truncating_trailing"]
        assert len(reported) == 1, (
            f"guarding the error read must redden this detector exactly once: {reported}"
        )
        site = next(iter(reported.values()))
        assert site["kind"] == "trailing" and site["guard"] == "slice", (
            f"the finding must be the first-line-guarded trailing read: {site!r}"
        )
        assert "cut -f5-" in site["text"], site["text"]
        # For ITS reason: the fixed reads in the same mutated block stay guarded, so this
        # finding is the trailing read moving and not the whole block collapsing.
        assert findings["unguarded_fixed"] == {}, findings["unguarded_fixed"]

    def test_a_trailing_read_with_no_sibling_fixed_field_read_is_out_of_scope(
        self, tmp_path,
    ) -> None:
        """A synthetic file holding ONLY a line-restricted trailing `cut -f2-`, with no
        fixed-field read anywhere in the same file, must NOT be reported: it is not a
        record family in plan.md's sense, and the record-family scoping this class's
        docstring names is what keeps such a site out of scope."""
        lone_trailing = (
            "#!/bin/bash\n"
            "RAW=$(printf 'one\\ttwo\\tfirst line of the tail\\nsecond line')\n"
            "RAW_LINE=${RAW%%$'\\n'*}\n"
            "TAIL=$(printf '%s' \"$RAW_LINE\" | cut -f2-)\n"
        )
        findings = _cut_site_findings_for(tmp_path, lone_trailing)
        assert len(findings["sites"]) == 1, (
            f"the one cut site must still be in the population: {findings['sites']}"
        )
        assert next(iter(findings["sites"].values()))["kind"] == "trailing"
        assert findings["truncating_trailing"] == {}, (
            "a line-restricted trailing read with no fixed-field sibling is not this "
            f"record shape and must not be reported: {findings['truncating_trailing']}"
        )


# ---------------------------------------------------------------------------
# Capability 10: the derivation is proved conditional by mutation, in both directions,
# on a synthetic tree.
# ---------------------------------------------------------------------------


class TestDerivationConditionalByMutationOnASyntheticTree:
    """The four detectors above are proved CONDITIONAL by mutation on a synthetic
    `tmp_path` tree (plan.md ## Capabilities item 10): an unrestricted fixed-field read
    is reported, a trailing read fed by the first-line slice is reported, a trailing
    read piped through `head -1` is reported, and a control file obeying both rules
    reports nothing. The control is what keeps an always-red detector from silently
    "passing" the three violation scenarios by reporting everything regardless of
    input.

    PASSES VACUOUSLY IF: the control scenario were omitted -- a detector hardcoded to
    report every `cut -f` site it sees, guarded or not, would pass all three violation
    scenarios below and never be caught, because nothing would assert it can also stay
    silent on a file that does everything right.
    """

    @pytest.mark.parametrize(
        "scenario,hook_source",
        [
            ("unrestricted_fixed_read", _UNRESTRICTED_FIXED_READ_SOURCE),
            ("trailing_from_first_line_slice", _TRAILING_FROM_SLICE_SOURCE),
            ("trailing_with_head_minus_1", _TRAILING_WITH_HEAD_SOURCE),
        ],
    )
    def test_each_synthetic_violation_is_reported_by_its_matching_detector(
        self, tmp_path, scenario, hook_source,
    ) -> None:
        expected = {
            "unrestricted_fixed_read": "unguarded_fixed",
            "trailing_from_first_line_slice": "truncating_trailing",
            "trailing_with_head_minus_1": "truncating_trailing",
        }[scenario]
        findings = _cut_site_findings_for(tmp_path, hook_source)
        assert findings["sites"], f"{scenario}: the synthetic file was not scanned at all"
        assert len(findings[expected]) == 1, (
            f"{scenario} must be reported by {expected}; got {findings[expected]}"
        )

    def test_the_control_file_obeying_both_rules_reports_nothing_on_either_detector(
        self, tmp_path,
    ) -> None:
        findings = _cut_site_findings_for(tmp_path, _CONTROL_SOURCE)
        assert len(findings["sites"]) == 4, (
            "the control's four cut sites must all be in the population, or its silence "
            f"is the silence of a file nothing read: {findings['sites']}"
        )
        assert findings["unguarded_fixed"] == {}, findings["unguarded_fixed"]
        assert findings["truncating_trailing"] == {}, findings["truncating_trailing"]

    def test_the_populations_findings_are_disjoint_across_the_three_violation_scenarios(
        self, tmp_path,
    ) -> None:
        """Each violation scenario trips ONLY its own detector -- the unrestricted-
        fixed-read source must not also appear in `truncating_trailing_cuts`, and
        neither trailing scenario may appear in `unguarded_fixed_field_cuts` -- so a
        detector that conflates the two finding sets (e.g. reporting every `cut -f`
        site in `unguarded_fixed_field_cuts` regardless of `kind`) is caught rather
        than passing by accident because SOME finding set was non-empty."""
        scenarios = {
            "unrestricted_fixed_read": _UNRESTRICTED_FIXED_READ_SOURCE,
            "trailing_from_first_line_slice": _TRAILING_FROM_SLICE_SOURCE,
            "trailing_with_head_minus_1": _TRAILING_WITH_HEAD_SOURCE,
        }
        silent = {
            "unrestricted_fixed_read": "truncating_trailing",
            "trailing_from_first_line_slice": "unguarded_fixed",
            "trailing_with_head_minus_1": "unguarded_fixed",
        }
        for scenario, hook_source in scenarios.items():
            findings = _cut_site_findings_for(tmp_path / scenario, hook_source)
            assert findings[silent[scenario]] == {}, (
                f"{scenario} tripped {silent[scenario]}, which is not its rule: "
                f"{findings[silent[scenario]]}"
            )
