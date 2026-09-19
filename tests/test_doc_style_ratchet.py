"""Tier B style sweep, increment 1: the derived lexical ratchet
(plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482).

The user's standing rule: no em dash, no double hyphen used as an em-dash
substitute; hyphens only to join words, with commas, colons, semicolons or
parentheses for clause breaks. This module pins the two new source-derived
populations in `tests/_inventory.py`:

  `style_swept_docs()`: the guarded markdown population, derived from
      `git ls-files`, never a filesystem glob.
  `emdash_substitute_hits()`: the findings; must be empty for the real
      population, which is the ratchet.

Skeleton, per ENF-GATE-007: written and approved before implementation, so
every test below FAILS LOUDLY (pytest.fail, never skip) until the two names
exist. Most of this file is RED on the unimplemented tree.

WHY SYNTHETIC FIXTURES for the exemption behaviors rather than the real
HANDBOOK.md text: the standing directive in this suite is that no test may
assert on documentation prose (doc staleness is not a code defect; see
tests/test_gate_token_protection.py). A synthetic tmp_path file makes the
exemption a property of the scanner, provable independent of any live
document's wording, and it needs no fixture file added to the repo.

ON THIS FILE'S OWN USE OF THE FORBIDDEN TOKEN: this is the one file in the
repo where writing about the pattern and reproducing it as test DATA are both
required. The runtime Stop gate scans only the assistant's reply text, and
the static ratchet under test here scans only tracked markdown, so neither
mechanism inspects this module's source, but the same three characters
would still read as sloppy if they sat in a docstring or a comment. So the
literal three-character sequence never appears typed directly in this file;
every occurrence used as test data is assembled at runtime from single
hyphen characters (`_SEP` below), the same way `writ-comms-output-gate.sh`
builds its EM/EN dash constants from `chr(...)` rather than embedding the
literal glyph in its own source.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Assembled, not typed: a single space, two hyphens, a single space. This is
# the exact separator the scanner must find in prose and must not find inside
# a fenced block, an inline code span, or a bare long-option flag.
_HYPHEN = chr(0x2D)
_SEP = " " + (_HYPHEN * 2) + " "


def _inventory():
    """Import the inventory module or fail loudly. Deliberately not
    importorskip: a skipped skeleton is not RED."""
    try:
        import tests._inventory as inventory
    except ImportError as exc:
        pytest.fail(f"skeleton: tests/_inventory.py failed to import ({exc})")
    return inventory


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: tests._inventory has no {', '.join(missing)} yet")


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _population(inventory) -> set[str]:
    """The derived population as strings, guarded by a non-emptiness
    precondition. Every absence/exclusion check below asks "is X missing
    from this set"; on a silently broken derivation returning [], every such
    check would pass for the wrong reason. This is the value an absent
    feature would yield, so it is asserted away first rather than assumed."""
    docs = inventory.style_swept_docs()
    assert docs, "style_swept_docs() derived nothing; absence checks below would be vacuous"
    return {str(p) for p in docs}


# --------------------------------------------------------------------------- #
# Property 1: the population comes from `git ls-files`, never a filesystem walk
# --------------------------------------------------------------------------- #

class TestPopulationComesFromGitLsFiles:
    def test_style_swept_docs_is_non_empty_markdown(self) -> None:
        """Shape check only; the anti-vacuity emptiness assertion itself lives
        in test_count_pin_discipline.py's shared parametrize list, so it is
        asserted exactly once rather than duplicated here."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        docs = [str(p) for p in inventory.style_swept_docs()]
        assert docs, "style_swept_docs() derived nothing"
        non_markdown = [p for p in docs if not p.endswith(".md")]
        assert not non_markdown, (
            f"style_swept_docs() must be markdown-only; found: {non_markdown}"
        )

    def test_a_known_gitignored_path_is_confirmed_ignored_first(self) -> None:
        """Precondition for the next test: RESUME.md must actually be
        gitignored right now, or the absence assertion below would pass for
        the wrong reason (the file merely vanished, or was never ignored)."""
        proc = subprocess.run(
            ["git", "check-ignore", "-v", "RESUME.md"],
            cwd=str(REPO), capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "RESUME.md is expected to be gitignored (.gitignore:81); "
            f"git check-ignore returned {proc.returncode}: {proc.stderr!r}"
        )
        assert ".gitignore" in proc.stdout

    def test_gitignored_resume_md_is_absent_from_the_population(self) -> None:
        """THE PROBE. A filesystem-walk derivation would include RESUME.md
        (it is a real file carrying the pattern); a `git ls-files` derivation
        cannot, because untracked content never enters that population."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        assert "RESUME.md" not in _population(inventory)

    def test_untracked_root_plan_md_is_absent(self) -> None:
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        assert "plan.md" not in _population(inventory)

    def test_untracked_archived_plan_is_absent(self) -> None:
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        archived = [p for p in _population(inventory) if p.endswith(".ARCHIVED.md")]
        assert not archived, f"untracked archived plan(s) leaked into the population: {archived}"

    def test_bible_tree_is_absent(self) -> None:
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        under_bible = [p for p in _population(inventory) if p.startswith("bible/")]
        assert not under_bible, f"gitignored bible/ content leaked into the population: {under_bible}"


# --------------------------------------------------------------------------- #
# Property 4: the exclusions are categories with reasons
# --------------------------------------------------------------------------- #

class TestExclusionCategories:
    def test_plan_template_is_excluded(self) -> None:
        """Live format spec: writ/session/approval_workflow.py:62's
        _FILES_LINE_RE parses every plan's ## Files bullet on this exact
        separator, and tests/test_plan_template.py:105 runs the real
        _validate_phase_a against the template itself."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        assert "templates/plan-template.md" not in _population(inventory)

    def test_plan_template_exclusion_is_not_vacuous(self) -> None:
        """The exclusion must be doing real work: templates/plan-template.md
        genuinely carries the pattern in prose today, so excluding a file
        that had zero matches would be a no-op dressed up as a category."""
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        target = REPO / "templates" / "plan-template.md"
        assert target.is_file()
        findings = inventory.emdash_substitute_hits(docs=[target])
        assert findings, (
            "templates/plan-template.md was expected to carry at least one "
            "real occurrence outside a code span; the exclusion category has "
            "nothing to exclude if this is empty"
        )

    def test_agents_directory_is_excluded(self) -> None:
        """Byte-compared mirror of the graph's ROL nodes
        (tests/test_fix5_role_coverage.py); a doc edit here would drift from
        the export it is checked against."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        under_agents = [p for p in _population(inventory) if p.startswith("agents/")]
        assert not under_agents, f"agents/ path(s) leaked into the population: {under_agents}"

    def test_session_metrics_is_excluded(self) -> None:
        """Mechanically appended by the session-end hook's printf, so it
        re-emits the pattern on every append regardless of any sweep."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        assert ".claude/session-metrics.md" not in _population(inventory)

    def test_pressure_runs_tree_is_excluded(self) -> None:
        """Append-only evidence that quotes verbatim harness output; sweeping
        it would falsify the recorded evidence. Deferred to increment 2, not
        a permanent exemption."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs")
        under_pressure_runs = [p for p in _population(inventory) if p.startswith("docs/pressure-runs/")]
        assert not under_pressure_runs, (
            f"docs/pressure-runs/ path(s) leaked into the population: {under_pressure_runs}"
        )


# --------------------------------------------------------------------------- #
# Property 3: a positive control, and the real-population ratchet
# --------------------------------------------------------------------------- #

class TestPositiveControlAndRealPopulation:
    def test_synthetic_prose_violation_is_found(self, tmp_path: Path) -> None:
        """A scanner that finds nothing looks identical to a scanner that
        scans nothing. This is the positive signal the emptiness assertions
        elsewhere cannot give on their own."""
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        doc = _write(tmp_path, "prose.md", [
            "# Heading",
            "",
            f"A clause break{_SEP}written the forbidden way.",
        ])
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
        found_path, found_line = findings[0]
        assert str(found_path) == str(doc)
        assert found_line == 3

    def test_real_derived_population_has_no_finding(self) -> None:
        """THE RATCHET. Guarded by a non-emptiness precondition on the
        population itself, so a broken derivation returning [] cannot make
        this pass for the wrong reason."""
        inventory = _inventory()
        _require(inventory, "style_swept_docs", "emdash_substitute_hits")
        population = inventory.style_swept_docs()
        assert population, "style_swept_docs() derived nothing; cannot exercise the ratchet"
        findings = inventory.emdash_substitute_hits()
        assert findings == [], (
            f"the swept population must carry no remaining occurrence outside a "
            f"code span; found: {findings!r}"
        )


# --------------------------------------------------------------------------- #
# Property 2: code spans are exempt by property, not by exception
# --------------------------------------------------------------------------- #

class TestCodeSpanExemptions:
    def test_fenced_block_occurrence_yields_no_finding(self, tmp_path: Path) -> None:
        doc = _write(tmp_path, "fenced.md", [
            "# Heading",
            "```",
            f"command{_SEP}example inside a fence",
            "```",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert findings == [], f"fenced-block occurrence must be exempt; found: {findings!r}"

    def test_inline_span_occurrence_yields_no_finding(self, tmp_path: Path) -> None:
        """This is precisely what exempts HANDBOOK.md:300 without a listed
        exclusion entry: the pattern sits inside backticks there too."""
        doc = _write(tmp_path, "inline.md", [
            "# Heading",
            f"A row documenting the trigger: `{_SEP.strip()}` in prose.",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert findings == [], f"inline-span occurrence must be exempt; found: {findings!r}"

    def test_mixed_fence_span_and_prose_reports_only_prose(self, tmp_path: Path) -> None:
        """The consolidated property check: one file carrying all three
        shapes at once must report exactly the prose line, by its real line
        number, and nothing else."""
        doc = _write(tmp_path, "mixed.md", [
            "# Heading",
            "```",
            f"fenced{_SEP}occurrence, exempt",
            "```",
            f"An inline `{_SEP.strip()}` mention, also exempt.",
            f"But this clause break{_SEP}is real prose and must be reported.",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
        found_path, found_line = findings[0]
        assert str(found_path) == str(doc)
        assert found_line == 6

    def test_bare_long_option_flag_yields_no_finding(self, tmp_path: Path) -> None:
        """A CLI flag written with two hyphens and no surrounding space on
        both sides is not the em-dash-substitute pattern; naming a flag in
        prose is not a violation."""
        doc = _write(tmp_path, "flag.md", [
            "# Heading",
            "Run the command with --verbose to see more output.",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert findings == [], f"a bare long-option flag must not be flagged; found: {findings!r}"

    def test_unclosed_fence_never_silently_hides_a_later_violation(self, tmp_path: Path) -> None:
        """CRITICAL blind spot found in review: the fence toggle flips on
        each fence marker with no check that it ever closes, so an unclosed
        fence flips it on permanently and blanks every line after it,
        including a real violation in the tail.

        Paired with a CONTROL carrying the identical prose and violation
        with no fence at all: a scanner that silently reports nothing for
        every input could otherwise pass the unclosed-fence half by
        accident, and only the control proves the scanner is actually
        looking rather than scanning nothing.

        Written against the PROPERTY, not one implementation, because two
        fixes are both acceptable:
          1. the tail after an unclosed fence is still scanned as prose, and
             the violation is reported with its real line number, or
          2. the file is treated as unscannable and that is surfaced loudly
             (an exception), rather than folded into a silent empty list.
        Only a silent empty list for the unclosed file, while the control
        still finds its own violation, fails this test.
        """
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")

        violation_line = f"Prose with a clause break{_SEP}right here."
        control = _write(tmp_path, "control.md", ["# Heading", violation_line])
        unclosed = _write(tmp_path, "unclosed.md", ["```", violation_line])

        control_findings = inventory.emdash_substitute_hits(docs=[control])
        assert len(control_findings) == 1, (
            f"the control, with no fence, must report its own violation; "
            f"got {control_findings!r}"
        )
        control_path, control_line = control_findings[0]
        assert str(control_path) == str(control)
        assert control_line == 2

        try:
            unclosed_findings = inventory.emdash_substitute_hits(docs=[unclosed])
        except Exception:
            return  # behavior 2: unscannable, surfaced loudly instead of silently
        # Membership, not an exact count: an implementation may legitimately add a
        # second finding for the unterminated marker itself alongside the real
        # violation. Only the violation's own line is the property under test.
        violation_lines = [ln for path, ln in unclosed_findings if str(path) == str(unclosed)]
        assert 2 in violation_lines, (
            "an unclosed fence must not silently hide a real violation in the "
            f"tail (behavior 1: scan the tail as prose); got {unclosed_findings!r}"
        )


# --------------------------------------------------------------------------- #
# Line-number correctness and boundary inputs
# --------------------------------------------------------------------------- #

class TestLineNumbersAndBoundaries:
    def test_line_number_survives_a_stripped_fence_above_it(self, tmp_path: Path) -> None:
        """Fence contents are blanked IN PLACE rather than deleted, so a
        violation after a fenced block reports its real line, not a line
        number shifted by the fence's removal."""
        doc = _write(tmp_path, "shifted.md", [
            "# Heading",
            "```",
            "line two of the fence",
            "line three of the fence",
            "```",
            f"Real violation on line six{_SEP}reported here.",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
        _found_path, found_line = findings[0]
        assert found_line == 6

    def test_empty_file_yields_no_finding_and_raises_nothing(self, tmp_path: Path) -> None:
        doc = tmp_path / "empty.md"
        doc.write_bytes(b"")
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert findings == []

    def test_match_free_file_yields_no_finding(self, tmp_path: Path) -> None:
        doc = _write(tmp_path, "clean.md", [
            "# Heading",
            "This paragraph joins words with hyphens, like well-known and",
            "state-of-the-art, and uses commas, colons, and semicolons for breaks.",
        ])
        inventory = _inventory()
        _require(inventory, "emdash_substitute_hits")
        findings = inventory.emdash_substitute_hits(docs=[doc])
        assert findings == []
