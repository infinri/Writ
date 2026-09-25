"""Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 16: the ADR's census was
prose ("found ONE other site with this shape", "every other match passes its
payload on stdin or passes bounded values") and both halves were false within one
cycle. This module replaces the prose with a MAP of CensusSite entries, each holding a
script, a code ANCHOR, a status and a reason, resolved against
`tests/_inventory.py::exec_boundary_payload_sites`, a derived population rather
than a list, so a site that DECAYS reddens this test instead of sitting stale
until the next person re-derives it by hand.

THE KEY IS A SLUG AND THE LOCATOR IS AN ANCHOR, NOT A LINE NUMBER, changed
2026-09-16. Line numbers are OUTPUT now (failure messages, "go look here"), never
authored input. The reason is measured: the daemon-down can-write crossing moved
1811 to 1923 to 1960 across three cycles, byte-identical each time, because
unrelated edits added lines above it, and the last move reddened the whole suite
mid-cycle. Worse, all three `fixed` entries had drifted onto COMMENT lines, so
their assertions passed because a comment is not a crossing rather than because
the fix held, and they would have kept passing if the crossing came back three
lines away. An anchor distinguishes the four cases a line number cannot: MOVED
resolves to one site and stays green, VANISHED or rewritten-in-place resolves to
zero and fails by slug, REPLACED reds twice (the old entry by slug, the new site
as unmapped), and AMBIGUOUS resolves to more than one and names every match.

The `open` and `bounded` entries are regression pins: true today, untouched by
the cycle that introduced anchors, so they must stay true. Each `fixed` entry
anchors on its RETIRED spelling and must resolve to ZERO, which is the classic
unfailable shape, so each is paired with a reintroduction fixture that proves it
can redden.

The detector itself is proved CONDITIONAL, not merely present, against five
adversarial fixtures living under `tmp_path` (never under `hooks/scripts/`, so
neither the real census nor any other inventory-driven test can see them): an
env-crossing site (flagged), a stdin site (not flagged), a `$(pwd)`-derived value
(not flagged), a transitively derived value (flagged), and a map entry whose site
no longer exists (fails as decayed). A detector that cannot be made to redden by
any of the five is worse than the prose it replaces, per the plan's own words,
and would be reported rather than shipped.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from tests._inventory import exec_boundary_payload_sites, nested_program_splice_sites

REPO = Path(__file__).resolve().parent.parent
HOOK_SCRIPTS_DIR = REPO / "hooks" / "scripts"
BIN_LIB_DIR = REPO / "bin" / "lib"

FIXED = "fixed"
OPEN = "open"
BOUNDED = "bounded"

# The census, held as a MAP (site -> (status, reason)), not a list. Merges the
# two real scan roots this tree needs (`hooks/scripts/`, and `bin/lib/` for the
# one site the ADR's own sentence scoped itself out of, `common.sh:1811`), each
# looked up in ITS OWN derived population below rather than a merged one, so a
# site moving between the two roots is still caught by the per-root lookup.
# The ONE canonical count of exec-boundary payload crossings the detector finds across both
# roots, 64 under hooks/scripts and 2 under bin/lib as of this cycle (68 until the per-Read
# session-id/file_path parse in writ-read-rag.sh and the SubagentStart extractions moved
# off two crossings). Held here and nowhere
# else: a duplicated count pin is what this repo's "a broken count pin is usually a
# DUPLICATE" lesson is about. Most of these carry values bounded by construction, which is
# why the map below classifies only the ones that matter while this number forces a look at
# anything new. Lower it when a crossing is removed; add a CENSUS entry when one appears.
DERIVED_SITE_COUNT = 66


class CensusSite(NamedTuple):
    """An entry's whole authored identity: a place to look, a text to look for, a
    verdict, a paragraph. No line number, in any field.

    A NamedTuple rather than a bare 4-tuple because every failure this module
    raises reads the fields back out by name: `entry.anchor` in a message says
    which text went looking and came back empty, where `entry[1]` says nothing.
    """

    script: str
    anchor: str
    status: str
    reason: str


class SpliceSite(NamedTuple):
    """The same identity for the nested-splice map, minus the status: every entry
    there records a live splice, so there is no third verdict to carry."""

    script: str
    anchor: str
    reason: str


def _resolve(
    entry: CensusSite | SpliceSite, population: dict[str, dict[str, object]]
) -> list[str]:
    """The derived sites an entry's anchor names: same script, anchor present in the
    text the detector MATCHED ON.

    Reading the derivation's own matched text rather than a fresh read of the file
    is what makes a mention in a comment structurally unable to satisfy an anchor,
    which is how three `fixed` entries came to be green against comment lines.
    """
    return sorted(
        key
        for key, record in population.items()
        if record.get("script") == entry.script
        and entry.anchor in str(record.get("text") or "")
    )


def _resolution_failure(
    slug: str, entry: CensusSite | SpliceSite, matches: list[str]
) -> str:
    """The one diagnostic every resolution assertion carries, kept apart from the
    assertions that use it.

    "Fails naming the slug, the script and the anchor" is a claim about what a
    reader is handed when this map decays, and an assertion that built its own
    f-string inline would be grading a literal it wrote itself.
    """
    found = ", ".join(matches) if matches else "no derived site at all"
    return (
        f"{slug}: anchor `{entry.anchor}` in {entry.script} resolved to "
        f"{len(matches)} derived site(s): {found}. ZERO means the crossing was "
        "deleted or REWRITTEN IN PLACE, so decide whether the recorded reason "
        "still describes anything before touching this entry. MORE THAN ONE means "
        "the anchor stopped naming a single crossing and now names a family. For a "
        "fixed entry, any match at all means the retired transport is back, "
        "wherever in the script it landed."
    )


CENSUS: dict[str, CensusSite] = {
    # -- Fixed this cycle (plan.md ## Files) --------------------------------
    "bash-gate-extractor-command": CensusSite(
        script="writ-bash-write-gate.sh",
        anchor='WRIT_BASH_CMD="$CMD"',
        status=FIXED,
        reason=(
            "the extractor's command crosses on a mktemp file path (WRIT_BASH_CMD_FILE), "
            "bounded by construction, instead of as the WRIT_BASH_CMD env string"
        ),
    ),
    "dispatch-translator-result-and-check-body": CensusSite(
        script="writ-pre-write-dispatch.sh",
        anchor='"$RESULT" "$CHECK_BODY"',
        status=FIXED,
        reason=(
            "RESULT and CHECK_BODY cross on the translator's stdin as two "
            "NUL-separated records instead of as two argv strings"
        ),
    ),
    "worktree-safety-command": CensusSite(
        script="writ-worktree-safety.sh",
        anchor='WRIT_WT_CMD="$CMD"',
        status=FIXED,
        reason=(
            "same command-file transport and sentinel as the Bash gate, replacing "
            "WRIT_WT_CMD=\"$CMD\""
        ),
    ),
    # -- Recorded open, each with the plan's own reason ----------------------
    "state-write-gate-target-path": CensusSite(
        script="writ-state-write-gate.sh",
        anchor='WRIT_TGT="$FILE" WRIT_DIR_PROT="$PROTECTED_DIR"',
        status=BOUNDED,
        reason=(
            "the only unbounded value crossing is the TARGET PATH, and a path long "
            "enough to break execve (over 131072 bytes) is far past the kernel's "
            "PATH_MAX (4096), so no write can land there; the gate would allow a "
            "write that cannot happen"
        ),
    ),
    "bash-gate-pytest-venv-swap": CensusSite(
        script="writ-bash-write-gate.sh",
        anchor='WRIT_PARSED_ENVELOPE="$HOOK_ENVELOPE"',
        status=OPEN,
        reason=(
            "the pytest venv swap (WRIT_PARSED_ENVELOPE): the whole envelope crosses "
            "as an env string; over the cap the swap silently does not happen and "
            "pytest runs on the system interpreter, a convenience regression, and "
            "the arm exits 0 either way with no write-decision change"
        ),
    ),
    "dispatch-fallback-parsed-input": CensusSite(
        script="writ-pre-write-dispatch.sh",
        anchor='[ -z "$PARSED_INPUT" ] && PARSED_INPUT=$(python3 -c',
        status=OPEN,
        reason=(
            "the fallback PARSED_INPUT parse (STDIN_DATA/SKILL_DIR as argv): only "
            "reached when jq is absent or failed, and closing it means deciding what "
            "a hook does when it cannot parse its own envelope at all, a different "
            "question from 'the decider could not run', needing its own tests "
            "against the jq/python parity pins in test_pre_write_parse_parity.py"
        ),
    ),
    "common-sh-daemon-down-can-write-body": CensusSite(
        script="common.sh",
        anchor="cw_post_body=$(WRIT_SD=",
        status=BOUNDED,
        reason=(
            "the daemon-down can-write fallback body: over the cap the swap does not "
            "happen and the body falls back WITHOUT skill_dir, which loses the "
            "skill-dir exemption and therefore fails CLOSED, not open. Line number "
            "updated from 1811 to 1923 when that cycle's common.sh additions "
            "(writ_decider_fault, the stdin transports) moved it down, and from 1923 "
            "to 1960 when the lazy-seed failure row added lines above it. The crossing "
            "itself is untouched both times, verified byte-identical against the "
            "previous revision, and the update is the decay detection working. TWICE "
            "NOW, WHICH IS THE POINT: this key is a LINE NUMBER, so any edit above the "
            "site decays it. Deriving the site from its code rather than its line is "
            "recorded in RESUME.md as the durable fix. THAT FIX IS THIS ENTRY: the key "
            "is now the slug above and the anchor beside it, so a fourth shift of the "
            "same untouched crossing moves only the line this test REPORTS, and the "
            "history in this paragraph is kept as the evidence for why"
        ),
    ),
    # -- Recorded bounded (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ---------
    "memory-guard-deny-friction-row": CensusSite(
        script="writ-memory-policy-guard.sh",
        anchor='MATCHED_RAW="$MATCHED"',
        status=BOUNDED,
        reason=(
            "this hook's one SURVIVING argv/env crossing after the nested-splice "
            "transport fix: the deny-path friction row's env prefix "
            "(SESSION_ID/FILE_PATH/MATCHED_RAW) into the row-builder heredoc, "
            "untouched by the merge (only the override pre-filter and the pattern "
            "scan move). MEASURED (not eight, per this cycle's own pattern-count "
            "correction): MATCHED snippets are truncated at 80 characters across at "
            "most NINE patterns, FILE_PATH is bounded by PATH_MAX (4096, the same "
            "argument writ-state-write-gate.sh:36 makes), and SESSION_ID is capped "
            "at 128 characters by the session cache. Worst case is loss of the "
            "audit ROW, not the decision: DENY_REPLY is a separate quoted heredoc "
            "that interpolates nothing, so the denial still reaches the model. The "
            "line number MOVED from 112 to 127 when the merge landed, exactly as "
            "this entry predicted (SESSION_ID moved above the scan and the retired "
            "heredocs were replaced by a longer explanation of why); the key was "
            "updated per this entry's own instruction rather than the shift being "
            "read as a decayed site, and the crossing itself is untouched. NO SUCH "
            "UPDATE IS OWED AGAIN: the key is the slug above, so the next shift of "
            "this same crossing changes only the line this test reports"
        ),
    ),
}


@pytest.fixture(scope="module")
def hook_scripts_population() -> dict[str, dict[str, object]]:
    return exec_boundary_payload_sites(scripts_dir=HOOK_SCRIPTS_DIR)


@pytest.fixture(scope="module")
def bin_lib_population() -> dict[str, dict[str, object]]:
    return exec_boundary_payload_sites(scripts_dir=BIN_LIB_DIR)


# --------------------------------------------------------------------------- #
# The census itself: every mapped entry checked against the REAL derived
# population. The two per-site membership assertions that used to live here
# are now resolved through the anchor instead of through a line key, in
# TestAnchorResolutionAgainstTheRealPopulation below, which is the only place
# that can tell a MOVE from a VANISH.
# --------------------------------------------------------------------------- #

class TestCensusAgreesWithTheDerivedPopulation:
    def test_every_census_entry_carries_a_non_empty_reason(self) -> None:
        assert all(entry.reason for entry in CENSUS.values())

    def test_every_census_status_is_one_of_the_three_declared(self) -> None:
        assert all(entry.status in (FIXED, OPEN, BOUNDED) for entry in CENSUS.values())

    def test_a_new_payload_crossing_site_cannot_appear_unreviewed(
        self, hook_scripts_population, bin_lib_population,
    ) -> None:
        """The census's own blind spot, closed by a COUNT rather than by classifying
        every site.

        Every other test here iterates over the map's OWN keys, so all of them are
        regression pins on sites someone already looked at. A brand-new payload-crossing
        site added tomorrow appears in the derived population and in NO test, which is
        this repo's "a hardcoded population is BLIND, not just stale" failure wearing a
        derived-looking hat. Review confirmed it.

        Classifying all of the derived sites is the wrong price: most carry values bounded
        by construction (a path, a session id, a mode word) and the map would become a
        transcription chore that decays. A single canonical count is enough to force a
        LOOK: add an exec-boundary payload crossing anywhere under these roots and this
        goes red, naming the sites that are not in the map.

        WHAT IT CATCHES, stated at the detector's real scope rather than wider. The
        derivation traces from three roots (`HOOK_ENVELOPE`, `HOOK_COMMAND`,
        `HOOK_FILE_PATH`) plus a bare `NAME=$(cat)`, and follows values derived from them.
        MEASURED against copies of the real tree, not reasoned: appending
        `WRIT_PROBE="$CMD" python3 -c ...` to `writ-bash-write-gate.sh`, where `CMD` comes
        from `HOOK_COMMAND`, takes the count 66 to 67 and reddens this test; appending
        `WRIT_PROBE="literal" python3 -c ...` to the same file leaves it at 66, correctly
        ignored. My first attempt at that proof used a `$CMD` in a script where the name
        derives from no root, the count did not move, and the honest conclusion was that
        the probe was wrong rather than the detector, which is why the scope is written
        down here instead of implied.

        SO A CROSSING WHOSE VALUE THE DETECTOR CANNOT TRACE STILL SLIPS PAST, and one is
        already known: the memory-policy guard's nested `python3 -c` inside an unquoted
        heredoc body, which `exec_boundary_payload_sites` documents as a limit and the
        census carries by hand.
        """
        derived = set(hook_scripts_population) | set(bin_lib_population)
        claimed: set[str] = set()
        for entry in CENSUS.values():
            claimed |= set(_resolve(entry, _population_for_script(
                entry.script, hook_scripts_population, bin_lib_population
            )))
        unmapped = sorted(derived - claimed)
        assert len(derived) == DERIVED_SITE_COUNT, (
            "the derived exec-boundary population changed from %d to %d sites. If you "
            "ADDED a payload crossing, give it a CENSUS entry with a status and a reason; "
            "if you removed or fixed one, lower the count. Sites not in the map: %s"
            % (DERIVED_SITE_COUNT, len(derived), unmapped)
        )

    def test_the_census_is_non_empty(self) -> None:
        """The anti-vacuity floor `envelope_emitting_scripts`'s own docstring
        argues for: a census that silently emptied out would make every
        assertion above pass on any tree."""
        assert CENSUS

    def test_the_three_fixed_sites_are_exactly_the_plan_files_sites(self) -> None:
        fixed = {slug for slug, entry in CENSUS.items() if entry.status == FIXED}
        assert fixed == {
            "bash-gate-extractor-command",
            "dispatch-translator-result-and-check-body",
            "worktree-safety-command",
        }


# --------------------------------------------------------------------------- #
# The detector is proved CONDITIONAL against five adversarial fixtures, none
# of which live under hooks/scripts/, so neither this test nor any other
# inventory-driven one can see them by accident.
# --------------------------------------------------------------------------- #

class TestDetectorIsConditionalNotVacuous:
    def _scan(self, tmp_path: Path, body: str) -> dict[str, dict[str, object]]:
        (tmp_path / "fixture.sh").write_text(body)
        return exec_boundary_payload_sites(scripts_dir=tmp_path)

    def test_an_env_crossing_site_is_flagged(self, tmp_path: Path) -> None:
        """Direct root reference in an env-var prefix on the exec line itself,
        the exact shape of the three fixed sites."""
        sites = self._scan(tmp_path, (
            'HOOK_COMMAND="$1"\n'
            "NAME=\"$HOOK_COMMAND\" python3 <<'PY'\n"
            "print(1)\n"
            "PY\n"
        ))
        assert any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_stdin_site_is_not_flagged(self, tmp_path: Path) -> None:
        """The payload reaches python3 through a PIPE (its stdin), which the ADR
        names as the safe, unbounded channel -- the value crosses to `printf`'s
        own argv, not python3's."""
        sites = self._scan(tmp_path, (
            'HOOK_COMMAND="$1"\n'
            'printf \'%s\' "$HOOK_COMMAND" | python3 -c "import sys; print(sys.stdin.read())"\n'
        ))
        assert not any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_pwd_derived_value_is_not_flagged(self, tmp_path: Path) -> None:
        """A value with no path back to any payload root at all must never be
        flagged, however it is used."""
        sites = self._scan(tmp_path, (
            'HOOK_COMMAND="$1"\n'
            'X="$(pwd)"\n'
            'python3 -c "print(1)" "$X"\n'
        ))
        assert not any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_transitively_derived_value_is_flagged(self, tmp_path: Path) -> None:
        """Copied through TWO plain assignments before reaching the exec line,
        the shape `writ-pre-write-dispatch.sh`'s own CHECK_BODY takes (three
        assignments from STDIN_DATA) -- a single-hop-only detector would miss
        this and, with it, the write door's real fixed site."""
        sites = self._scan(tmp_path, (
            'HOOK_COMMAND="$1"\n'
            'A="$HOOK_COMMAND"\n'
            'B="$A"\n'
            'python3 -c "print(1)" "$B"\n'
        ))
        assert any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_map_entry_whose_site_no_longer_exists_fails_as_decayed(
        self, tmp_path: Path
    ) -> None:
        """Not a detector behavior at all -- the CENSUS TEST's own decay
        detection, exercised directly here against an empty fixture tree so it
        does not depend on any real site ever actually disappearing. Proves
        `test_an_open_or_bounded_site_still_shows_a_payload_crossing`'s own
        assertion shape reddens when a site is missing, rather than passing
        vacuously on an empty population."""
        population = exec_boundary_payload_sites(scripts_dir=tmp_path)
        decayed_site = "writ-bash-write-gate.sh:1107"
        with pytest.raises(AssertionError):
            assert decayed_site in population, "simulated decay"


# --------------------------------------------------------------------------- #
# The nested-program splice map (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3,
# `tests/_inventory.py::nested_program_splice_sites`). A MAP, not a list, for
# the same reason CENSUS above is one: one reason per site, checked by SET
# EQUALITY against the derived population rather than by membership alone, so
# a site the derivation finds and this map does not name cannot hide.
# --------------------------------------------------------------------------- #

SPLICE_MAP: dict[str, SpliceSite] = {
    "pressure-audit-session-report": SpliceSite(
        script="writ-pressure-audit.sh",
        anchor='mod._read_cache("$SESSION_ID")',
        reason=(
            "splices a path and a session id into the program's own text (no nested "
            "command substitution, unlike the two sites this cycle fixes), decides "
            "nothing (SessionEnd, observational, always exits 0), and its own splice "
            "of $SESSION_ID into a Python string literal is a real LATENT injection "
            "surface this map records rather than closes: a session id containing a "
            "quote or a backslash would corrupt the embedded string. Out of scope "
            "this cycle (plan.md's own 'Out of scope' section): fixing it needs its "
            "own session-id validation decision, not a transport swap."
        ),
    ),
}


class TestNestedProgramSpliceMapMatchesTheDerivedPopulation:
    """The set-equality assertion that used to live here is now the BIJECTION pair
    in TestSpliceEntriesResolveAsABijection below: every entry resolves to exactly
    one derived site, every derived site is claimed by exactly one entry. Same
    property, resolved through the anchor rather than through a line key."""

    def test_every_map_entry_carries_a_non_empty_reason(self) -> None:
        assert all(entry.reason for entry in SPLICE_MAP.values())

    def test_the_map_is_non_empty(self) -> None:
        """The same anti-vacuity floor CENSUS's own test argues for: a map that
        silently emptied out would make the set-equality test above pass on any
        tree that also happened to derive an empty population."""
        assert SPLICE_MAP


# --------------------------------------------------------------------------- #
# The nested-splice detector is proved CONDITIONAL against five adversarial
# fixtures, none of which live under hooks/scripts/ or bin/lib/, so neither
# this test nor the real census above can see them by accident.
# --------------------------------------------------------------------------- #

class TestNestedSpliceDetectorIsConditionalNotVacuous:
    def _scan(self, tmp_path: Path, body: str) -> dict[str, dict[str, object]]:
        (tmp_path / "fixture.sh").write_text(body)
        return nested_program_splice_sites(scripts_dir=tmp_path)

    def test_an_unquoted_body_containing_a_command_substitution_is_flagged(
        self, tmp_path: Path
    ) -> None:
        sites = self._scan(tmp_path, (
            "VAL=$(python3 <<PY\n"
            "content = $(echo hi)\n"
            "PY\n"
            ")\n"
        ))
        assert any(k.startswith("fixture.sh:") for k in sites), sites

    def test_an_unquoted_body_containing_a_bare_variable_is_flagged(
        self, tmp_path: Path
    ) -> None:
        sites = self._scan(tmp_path, (
            "VAL=$(python3 <<PY\n"
            "content = $BAR\n"
            "PY\n"
            ")\n"
        ))
        assert any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_quoted_heredoc_body_containing_a_command_substitution_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """The distinction this detector exists to draw: bash does not expand
        anything inside a QUOTED heredoc delimiter, so the same hazardous text
        that reddens the test above is inert here."""
        sites = self._scan(tmp_path, (
            "VAL=$(python3 <<'PY'\n"
            "content = $(echo hi)\n"
            "PY\n"
            ")\n"
        ))
        assert not any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_single_quoted_dash_c_program_containing_a_variable_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """No heredoc at all, so the mechanism this detector finds cannot be
        present: a single-quoted `-c` argument is never re-expanded by bash,
        the DIFFERENT shape and different fix the plan's Decision 1 discusses
        for the two apostrophe-carrying patterns."""
        sites = self._scan(tmp_path, "python3 -c 'print(\"$BAR\")'\n")
        assert not any(k.startswith("fixture.sh:") for k in sites), sites

    def test_a_map_entry_whose_site_no_longer_exists_fails_as_decayed(
        self, tmp_path: Path
    ) -> None:
        """Not a detector behavior -- the MAP TEST's own decay detection,
        exercised directly against an empty fixture tree so it does not depend
        on the real writ-pressure-audit.sh site ever actually disappearing."""
        population = nested_program_splice_sites(scripts_dir=tmp_path)
        decayed_site = "writ-pressure-audit.sh:19"
        with pytest.raises(AssertionError):
            assert decayed_site in population, "simulated decay"


# --------------------------------------------------------------------------- #
# Plan 2412ba38-51e1-4b73-895b-7b240a3c21d3, defect 1: the census above is keyed
# by `script:LINE`, which is an INCIDENTAL property of a crossing. It changes when
# anything above the site changes (common.sh's daemon-down body has moved 1811 ->
# 1923 -> 1960 across three cycles, byte identical every time) and it does NOT
# change when the site is rewritten in place. MEASURED this session: all three
# `fixed` keys point at ordinary COMMENT lines today
# (writ-bash-write-gate.sh:1107, writ-pre-write-dispatch.sh:258,
# writ-worktree-safety.sh:74), so those assertions are green because a comment is
# not a crossing, not because the fix holds; reintroducing the crossing three
# lines away would keep them green.
#
# The replacement names an entry by an authored SLUG and locates it by a code
# ANCHOR resolved against the derivation's own matched text. The tests below are
# written against that shape and fail until it exists.
# --------------------------------------------------------------------------- #

_SKELETON_SLUG = "<CENSUS-not-re-keyed-on-slug-plus-anchor>"

# The synthetic fixture family. MOVED and VANISHED are proved against the SAME
# family so neither direction is claimed on its own: a resolver that always
# returns one site passes the move case and fails the vanish case, and a resolver
# that always returns none does the reverse.
#
# Every string here is typed out in full. None is sliced from a derived record
# and none is composed from another, because an anchor computed from the
# population it is checked against is unfailable by construction.
_PROBE_ROOT_LINE = 'HOOK_COMMAND="$1"'
_PROBE_CROSSING = 'WRIT_PROBE_CMD="$HOOK_COMMAND" python3 -c "print(1)"'
_PROBE_SECOND_CROSSING = 'WRIT_PROBE_CMD="$HOOK_COMMAND" python3 -c "print(2)"'
_PROBE_REWRITTEN = 'WRIT_PROBE_ENVELOPE="$HOOK_COMMAND" python3 -c "print(1)"'
_PROBE_UNRELATED = 'WRIT_PROBE_OTHER="$HOOK_COMMAND" python3 -c "print(1)"'
_PROBE_ANCHOR = 'WRIT_PROBE_CMD="$HOOK_COMMAND"'
_PROBE_LINE = 2
_PAD_LINES = 40
_PROBE_MOVED_LINE = _PROBE_LINE + _PAD_LINES

# One synthetic tree per `fixed` entry, each REINTRODUCING that entry's retired
# spelling as a real crossing under the script's own name. This is what makes the
# fixed assertion ("this anchor resolves to zero sites") conditional rather than
# unfailable: a string that is absent from every tree anyone ever runs the test
# against proves nothing at all.
#
# The crossing lines are typed from the prose that still quotes the retired
# transport in the tree (writ-bash-write-gate.sh:1109, writ-worktree-safety.sh:105,
# and writ-pre-write-dispatch.sh's own census reason), never read back out of
# `CENSUS[slug].anchor`, so the two sides of the reintroduction assertion are
# independently authored.
_REINTRODUCED_CROSSINGS: dict[str, tuple[str, str]] = {
    "bash-gate-extractor-command": (
        "writ-bash-write-gate.sh",
        'HOOK_COMMAND="$1"\n'
        'CMD="$HOOK_COMMAND"\n'
        'WRIT_BASH_CMD="$CMD" python3 -c "print(1)"\n',
    ),
    "worktree-safety-command": (
        "writ-worktree-safety.sh",
        'HOOK_COMMAND="$1"\n'
        'CMD="$HOOK_COMMAND"\n'
        'WRIT_WT_CMD="$CMD" python3 -c "print(1)"\n',
    ),
    "dispatch-translator-result-and-check-body": (
        "writ-pre-write-dispatch.sh",
        'STDIN_DATA=$(cat)\n'
        'RESULT="$STDIN_DATA"\n'
        'CHECK_BODY="$STDIN_DATA"\n'
        'python3 -c "print(1)" "$RESULT" "$CHECK_BODY"\n',
    ),
}

_SPLICE_FIXTURE = (
    "VAL=$(python3 <<PY\n"
    'content = "$BAR"\n'
    "PY\n"
    ")\n"
)
_SPLICE_FIXTURE_OPENING = "VAL=$(python3 <<PY"
_SPLICE_FIXTURE_BODY = 'content = "$BAR"'


def _entry_has_the_new_shape(value: object) -> bool:
    return all(hasattr(value, name) for name in ("script", "anchor", "reason"))


def _census_slugs(*statuses: str) -> list:
    """Collection-time slug population, never an empty argvalues list.

    An empty parametrization produces a SKIP that reads like coverage in the run
    output, which is the vacuity `_floor_labels()` in tests/test_corpus_floor.py
    guards against; a sentinel param produces a named RED instead.
    """
    try:
        items = [(slug, e) for slug, e in CENSUS.items() if _entry_has_the_new_shape(e)]
    except Exception:  # noqa: BLE001
        return [pytest.param(_SKELETON_SLUG, id="census-unavailable")]
    if statuses:
        items = [(s, e) for s, e in items if getattr(e, "status", None) in statuses]
    slugs = sorted(s for s, _e in items)
    label = "-".join(statuses) if statuses else "any"
    return slugs or [pytest.param(_SKELETON_SLUG, id=f"census-has-no-{label}-entry-in-the-new-shape")]


def _splice_slugs() -> list:
    try:
        slugs = sorted(s for s, e in SPLICE_MAP.items() if _entry_has_the_new_shape(e))
    except Exception:  # noqa: BLE001
        return [pytest.param(_SKELETON_SLUG, id="splice-map-unavailable")]
    return slugs or [pytest.param(_SKELETON_SLUG, id="splice-map-has-no-entry-in-the-new-shape")]


def _fixed_slugs() -> list:
    return _census_slugs(FIXED)


def _entry(slug: str):
    """The `CensusSite` for `slug`, or a loud skeleton failure naming what is missing."""
    entry = CENSUS.get(slug) if isinstance(CENSUS, dict) else None
    if not _entry_has_the_new_shape(entry):
        pytest.fail(
            f"skeleton: CENSUS[{slug!r}] is not a CensusSite(script, anchor, status, "
            "reason). plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files re-keys "
            "this map by an authored slug carrying a code anchor, because a "
            "`script:LINE` key names a coordinate rather than a crossing",
            pytrace=False,
        )
    return entry


def _splice_entry(slug: str):
    entry = SPLICE_MAP.get(slug) if isinstance(SPLICE_MAP, dict) else None
    if not _entry_has_the_new_shape(entry):
        pytest.fail(
            f"skeleton: SPLICE_MAP[{slug!r}] is not a SpliceSite(script, anchor, "
            "reason). plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files gives the "
            "splice map the same slug-plus-anchor identity the census gets",
            pytrace=False,
        )
    return entry


def _resolver():
    """`_resolve(entry, population) -> list[str]`, or a loud skeleton failure."""
    fn = globals().get("_resolve")
    if not callable(fn):
        pytest.fail(
            "skeleton: this module has no `_resolve(entry, population) -> list[str]` "
            "yet (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3: the population keys "
            "whose record has record['script'] == entry.script and entry.anchor in "
            "record['text'])",
            pytrace=False,
        )
    return fn


def _failure_message():
    """`_resolution_failure(slug, entry, matches) -> str`, or a loud skeleton failure.

    The message is a separate producer on purpose. "Fails naming the slug, the
    script and the anchor" is a claim about what a reader is handed, and a test
    that asserted an f-string it built inline would be checking a literal it wrote
    itself, which is the shape this session has already shipped twice.
    """
    fn = globals().get("_resolution_failure")
    if not callable(fn):
        pytest.fail(
            "skeleton: this module has no `_resolution_failure(slug, entry, matches) "
            "-> str` yet. plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 converts the "
            "four map assertions to resolve through the anchor helper, and the "
            "message those assertions carry must name the slug, entry.script, "
            "entry.anchor and every key in matches, so a VANISHED, a REPLACED and an "
            "AMBIGUOUS entry cannot be confused for one another",
            pytrace=False,
        )
    return fn


def _census_site_type():
    site = globals().get("CensusSite")
    if site is None:
        pytest.fail(
            "skeleton: this module declares no CensusSite(script, anchor, status, "
            "reason) yet (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files)",
            pytrace=False,
        )
    return site


def _population_for_script(script: str, hook_pop: dict, lib_pop: dict) -> dict:
    """Routing by the entry's own script name, replacing the key-coordinate lookup
    on a line key. The two roots stay separate for the reason the existing map
    states: a site that MOVED between them must not be found by accident."""
    return lib_pop if script == "common.sh" else hook_pop


def _scan(tmp_path: Path, name: str, body: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(body)
    return exec_boundary_payload_sites(scripts_dir=tmp_path)


def _probe_source(pad: int = 0, crossing: str = _PROBE_CROSSING, extra: str = "") -> str:
    return (
        _PROBE_ROOT_LINE + "\n"
        + "# pad\n" * pad
        + crossing + "\n"
        + extra
    )


@pytest.fixture(scope="module")
def splice_hook_population() -> dict:
    return nested_program_splice_sites(scripts_dir=HOOK_SCRIPTS_DIR)


@pytest.fixture(scope="module")
def splice_lib_population() -> dict:
    return nested_program_splice_sites(scripts_dir=BIN_LIB_DIR)


class TestACensusEntryIsNamedBySlugAndLocatedByAnchor:
    """Capability 1 and capability 8: an entry's authored data is a slug, a script,
    an anchor and a paragraph, and a line number appears nowhere in it."""

    @staticmethod
    def _is_a_line_coordinate(value: str) -> bool:
        head, sep, tail = str(value).rpartition(":")
        return bool(sep) and head.endswith(".sh") and tail.isdigit()

    @pytest.mark.parametrize("slug", _census_slugs())
    def test_no_authored_field_is_a_script_line_coordinate(self, slug) -> None:
        entry = _entry(slug)
        coordinates = [
            field
            for field in (slug, entry.script, entry.anchor)
            if self._is_a_line_coordinate(field)
        ]
        assert not coordinates, (
            f"{slug}: a line number is OUTPUT, never authored INPUT; these authored "
            f"fields are still site coordinates: {coordinates}"
        )

    @pytest.mark.parametrize("slug", _census_slugs())
    def test_the_slug_names_the_site_in_words(self, slug) -> None:
        _entry(slug)
        assert ":" not in slug, (
            f"{slug!r} still carries a coordinate separator; a slug names the site in "
            "words so a failure says WHICH crossing, not which line"
        )

    @pytest.mark.parametrize("slug", _census_slugs())
    def test_every_anchor_sits_inside_one_physical_line(self, slug) -> None:
        """The plan's authoring rule. Both derivations join backslash continuations
        with a single space, so an anchor spanning a join would be checking the
        joiner rather than the code."""
        entry = _entry(slug)
        assert entry.anchor and "\n" not in entry.anchor, (
            f"{slug}: the anchor must be a non-empty substring of ONE physical line "
            f"of the crossing: {entry.anchor!r}"
        )

    @pytest.mark.parametrize("slug", _census_slugs())
    def test_every_entry_keeps_its_full_prose_reason(self, slug) -> None:
        """Capability 8. The paragraphs narrating their own line bumps (common.sh
        1811 -> 1923 -> 1960, the memory guard's 112 -> 127) are this cycle's own
        evidence, so a future edit must not be able to drop them quietly."""
        entry = _entry(slug)
        assert entry.reason and entry.reason.strip(), (
            f"{slug} carries no reason; a census entry without its paragraph is a "
            "coordinate again"
        )

    @pytest.mark.parametrize("slug", _splice_slugs())
    def test_every_splice_entry_keeps_its_full_prose_reason(self, slug) -> None:
        entry = _splice_entry(slug)
        assert entry.reason and entry.reason.strip(), (
            f"{slug} carries no reason"
        )


class TestAnchorResolutionAgainstTheRealPopulation:
    """Capabilities 3, 5, 6 and 10 against the live derivations.

    The anchor is a LITERAL typed into this module from the shell source; the
    population is computed by the derivation walking real script text. Neither
    side is read off the other.
    """

    @pytest.mark.parametrize("slug", _census_slugs(OPEN, BOUNDED))
    def test_an_open_or_bounded_entry_resolves_to_exactly_one_site(
        self, slug, hook_scripts_population, bin_lib_population
    ) -> None:
        resolve = _resolver()
        entry = _entry(slug)
        population = _population_for_script(
            entry.script, hook_scripts_population, bin_lib_population
        )
        matches = resolve(entry, population)
        assert len(matches) == 1, _failure_message()(slug, entry, matches)

    @pytest.mark.parametrize("slug", _fixed_slugs())
    def test_a_fixed_entry_resolves_to_zero_sites(
        self, slug, hook_scripts_population, bin_lib_population
    ) -> None:
        """The retired spelling, asserted absent everywhere in the script rather
        than absent at one coordinate. Proved CONDITIONAL by
        TestTheFourResolutionOutcomes' reintroduction case, because an
        always-absent string is the classic unfailable assertion."""
        resolve = _resolver()
        entry = _entry(slug)
        population = _population_for_script(
            entry.script, hook_scripts_population, bin_lib_population
        )
        matches = resolve(entry, population)
        assert matches == [], _failure_message()(slug, entry, matches)

    def test_no_two_entries_resolve_to_the_same_site(
        self, hook_scripts_population, bin_lib_population
    ) -> None:
        resolve = _resolver()
        claimed: dict[str, str] = {}
        collisions: list[str] = []
        for slug in sorted(CENSUS):
            entry = _entry(slug)
            population = _population_for_script(
                entry.script, hook_scripts_population, bin_lib_population
            )
            for key in resolve(entry, population):
                if key in claimed:
                    collisions.append(f"{key} is claimed by both {claimed[key]} and {slug}")
                claimed[key] = slug
        assert not collisions, "\n".join(collisions)

    def test_every_derived_record_carries_the_text_its_detector_matched(
        self, hook_scripts_population, bin_lib_population
    ) -> None:
        """Capability 10. Without the matched text there is nothing for an anchor
        to be checked against except a fresh read of the file, which is how a
        COMMENT came to satisfy three `fixed` entries."""
        records = {**hook_scripts_population, **bin_lib_population}
        assert records, "both derivations returned nothing, so this test is vacuous"
        missing = sorted(k for k, r in records.items() if not (r.get("text") or "").strip())
        assert not missing, (
            "these derived records carry no non-empty `text` field: "
            f"{missing[:10]} ({len(missing)} total)"
        )

    def test_every_derived_record_key_is_its_own_script_and_line(
        self, hook_scripts_population, bin_lib_population
    ) -> None:
        """Capability 11's shipped half: adding the text field must not move a site.
        The key set's SIZE is pinned once, by DERIVED_SITE_COUNT above; this pins
        that each key is still built from the record it points at."""
        records = {**hook_scripts_population, **bin_lib_population}
        assert records, "both derivations returned nothing, so this test is vacuous"
        wrong = sorted(
            k for k, r in records.items() if k != f"{r.get('script')}:{r.get('line')}"
        )
        assert not wrong, f"these keys disagree with their own record: {wrong}"


class TestTheFourResolutionOutcomes:
    """The safety property, one test per outcome, against synthetic trees under
    `tmp_path` so no case depends on a real crossing ever actually moving.

    MOVED and VANISHED share one fixture family on purpose: a resolver that always
    finds one site passes the move case and fails the vanish case, and one that
    always finds none does the reverse, so neither claim stands on its own.
    """

    def _entry_for(self, anchor: str, script: str = "fixture.sh"):
        return _census_site_type()(
            script=script,
            anchor=anchor,
            status=OPEN,
            reason="synthetic fixture entry for the resolution outcome proofs",
        )

    def test_moved_a_crossing_pushed_down_still_resolves_to_exactly_one_site(
        self, tmp_path: Path
    ) -> None:
        """M3 and P4. Forty comment lines above the crossing: the derived LINE must
        change and the resolution must not."""
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)

        before = _scan(tmp_path / "before", "fixture.sh", _probe_source())
        after = _scan(tmp_path / "after", "fixture.sh", _probe_source(pad=_PAD_LINES))

        assert list(before) == [f"fixture.sh:{_PROBE_LINE}"], before
        assert list(after) == [f"fixture.sh:{_PROBE_MOVED_LINE}"], after
        assert resolve(entry, after) == [f"fixture.sh:{_PROBE_MOVED_LINE}"], (
            "the crossing only moved: the anchor must still resolve to exactly one "
            f"site, and the reported line moves on its own. got {resolve(entry, after)!r}"
        )

    def test_vanished_a_crossing_rewritten_in_place_resolves_to_zero(
        self, tmp_path: Path
    ) -> None:
        """P1, and the half the line key could never see: the site is still AT the
        same coordinate, with different text. A crossing whose text changed is a
        crossing whose recorded status was verified against text that no longer
        exists, so a rewrite is deliberately a vanish."""
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)
        population = _scan(
            tmp_path, "fixture.sh", _probe_source(crossing=_PROBE_REWRITTEN)
        )

        assert list(population) == [f"fixture.sh:{_PROBE_LINE}"], population
        matches = resolve(entry, population)
        assert matches == [], f"expected zero matches, got {matches!r}"

        message = _failure_message()("probe-rewritten-in-place", entry, matches)
        for token in ("probe-rewritten-in-place", entry.script, entry.anchor):
            assert token in message, (
                f"the vanish failure must name {token!r} so the reader can decide "
                f"whether the recorded reason still describes anything: {message!r}"
            )

    def test_vanished_a_deleted_crossing_resolves_to_zero(self, tmp_path: Path) -> None:
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)
        population = _scan(tmp_path, "fixture.sh", _PROBE_ROOT_LINE + "\n")

        matches = resolve(entry, population)
        assert matches == [], f"expected zero matches on a tree with no crossing: {matches!r}"

    def test_replaced_reds_twice_as_a_lost_entry_and_an_unmapped_site(
        self, tmp_path: Path
    ) -> None:
        """The outcome the line key could not express at all: `common.sh:1960`
        reddening says nothing about which of moved, vanished or replaced happened.
        Here the coordinate is REUSED by an unrelated crossing, and the two reds
        must be distinguishable."""
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)

        original = _scan(tmp_path / "original", "fixture.sh", _probe_source())
        replaced = _scan(
            tmp_path / "replaced", "fixture.sh", _probe_source(crossing=_PROBE_UNRELATED)
        )

        assert list(replaced) == list(original), (
            "this fixture must reuse the same coordinate, which is the whole point: "
            f"original={list(original)} replaced={list(replaced)}"
        )

        matches = resolve(entry, replaced)
        assert matches == [], f"red 1, the old entry by slug: {matches!r}"

        claimed = set(matches)
        unmapped = sorted(set(replaced) - claimed)
        assert unmapped == list(replaced), (
            "red 2, the new site reported by name as claimed by no entry: "
            f"{unmapped!r}"
        )

    def test_ambiguous_two_crossings_carrying_the_anchor_name_every_match(
        self, tmp_path: Path
    ) -> None:
        """P2. An anchor that has stopped identifying one site must not silently
        start identifying a family."""
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)
        population = _scan(
            tmp_path,
            "fixture.sh",
            _probe_source(extra="# gap\n" + _PROBE_SECOND_CROSSING + "\n"),
        )

        assert len(population) == 2, (
            f"the fixture declares two crossings; the derivation found {population!r}"
        )
        matches = resolve(entry, population)
        assert set(matches) == set(population), (
            f"both crossings carry the anchor: {matches!r}"
        )

        message = _failure_message()("probe-ambiguous", entry, matches)
        for key in sorted(population):
            assert key in message, (
                f"the ambiguity failure must name every match, and {key!r} is absent "
                f"from: {message!r}"
            )

    def test_an_anchor_appearing_only_in_a_comment_resolves_to_zero(
        self, tmp_path: Path
    ) -> None:
        """The live instance this cycle exists to make unrepeatable: all three
        `fixed` keys point at COMMENT lines in the tree today, so they pass because
        a comment is not a crossing.

        This is also capability 10 stated as behaviour: resolution reads the
        derivation's own matched text, never a fresh read of the file, and the file
        here DOES contain the anchor while the population does not.
        """
        resolve = _resolver()
        entry = self._entry_for(_PROBE_ANCHOR)
        source = (
            _PROBE_ROOT_LINE + "\n"
            + "# " + _PROBE_CROSSING + " was the retired transport, named here in prose\n"
            + _PROBE_REWRITTEN + "\n"
        )
        population = _scan(tmp_path, "fixture.sh", source)

        assert _PROBE_ANCHOR in (tmp_path / "fixture.sh").read_text(), (
            "the fixture must carry the anchor in its FILE text, or this test proves "
            "nothing about where resolution reads from"
        )
        assert population, "the fixture must still hold one real crossing"
        matches = resolve(entry, population)
        assert matches == [], (
            f"a mention in a comment is not a crossing, but it resolved: {matches!r}"
        )

    @pytest.mark.parametrize("slug", _fixed_slugs())
    def test_a_fixed_entry_reds_when_its_retired_spelling_reappears(
        self, slug, tmp_path: Path
    ) -> None:
        """P3, and constraint on the whole `fixed` family: "this anchor resolves to
        zero sites" is the classic unfailable assertion, and today's three are green
        by accident. Reintroduce the retired spelling as a real crossing, on a line
        number that has nothing to do with the retired one, and the entry must red."""
        resolve = _resolver()
        entry = _entry(slug)
        fixture = _REINTRODUCED_CROSSINGS.get(slug)
        assert fixture is not None, (
            f"{slug} is declared fixed and has no reintroduction fixture, so its "
            "zero-match assertion is unproven"
        )
        script, body = fixture
        population = _scan(tmp_path, script, body)

        assert population, (
            f"the reintroduction fixture for {slug} is not seen as a crossing at all, "
            "so it proves nothing"
        )
        matches = resolve(entry, population)
        assert len(matches) == 1, (
            f"{slug}: the retired spelling is back as a real crossing and the fixed "
            f"entry did not red: {matches!r} against {sorted(population)}"
        )

    def test_every_fixed_entry_has_a_reintroduction_fixture(self) -> None:
        """A new `fixed` entry must not be able to ship with an unfailable
        assertion. The population is read from CENSUS, never from the fixture map,
        so adding an entry and forgetting the fixture reds here."""
        declared = {slug for slug in _census_slugs(FIXED) if isinstance(slug, str)}
        assert declared, (
            "skeleton: CENSUS declares no fixed entry in the CensusSite(script, "
            "anchor, status, reason) shape yet, so this guard would pass on an empty "
            "set (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files)"
        )
        unproven = sorted(declared - set(_REINTRODUCED_CROSSINGS))
        assert not unproven, (
            "these fixed entries have no reintroduction fixture, so nothing proves "
            f"their zero-match assertion can fail: {unproven}"
        )


class TestSpliceEntriesResolveAsABijection:
    """Capability 9: every derived splice site claimed exactly once, every entry
    claiming exactly one site."""

    @pytest.mark.parametrize("slug", _splice_slugs())
    def test_every_splice_entry_resolves_to_exactly_one_site(
        self, slug, splice_hook_population, splice_lib_population
    ) -> None:
        resolve = _resolver()
        entry = _splice_entry(slug)
        population = _population_for_script(
            entry.script, splice_hook_population, splice_lib_population
        )
        matches = resolve(entry, population)
        assert len(matches) == 1, _failure_message()(slug, entry, matches)

    def test_every_derived_splice_site_is_claimed_by_exactly_one_entry(
        self, splice_hook_population, splice_lib_population
    ) -> None:
        resolve = _resolver()
        derived = {**splice_hook_population, **splice_lib_population}
        assert derived, "the splice derivation returned nothing, so this test is vacuous"
        claims: dict[str, list[str]] = {key: [] for key in derived}
        for slug in sorted(SPLICE_MAP):
            entry = _splice_entry(slug)
            population = _population_for_script(
                entry.script, splice_hook_population, splice_lib_population
            )
            for key in resolve(entry, population):
                claims.setdefault(key, []).append(slug)
        broken = {key: owners for key, owners in claims.items() if len(owners) != 1}
        assert not broken, (
            "every derived splice site must be claimed exactly once: "
            f"{ {k: sorted(v) for k, v in broken.items()} }"
        )

    def test_a_derived_splice_record_carries_its_opening_line_and_its_body(
        self, tmp_path: Path
    ) -> None:
        """Capability 10 for the splice derivation: the interpolation lives in the
        BODY, so a record carrying only the opening line could never anchor on the
        thing the entry's prose is about."""
        (tmp_path / "fixture.sh").write_text(_SPLICE_FIXTURE)
        population = nested_program_splice_sites(scripts_dir=tmp_path)
        assert list(population) == ["fixture.sh:1"], population
        text = population["fixture.sh:1"].get("text") or ""
        assert _SPLICE_FIXTURE_OPENING in text, (
            f"the record's text drops the heredoc opening line: {text!r}"
        )
        assert _SPLICE_FIXTURE_BODY in text, (
            f"the record's text drops the body, where the interpolation is: {text!r}"
        )
