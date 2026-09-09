"""Plan dfacff61-23d5-474e-846c-2e2f0f0ea482, capability 16: the ADR's census was
prose ("found ONE other site with this shape", "every other match passes its
payload on stdin or passes bounded values") and both halves were false within one
cycle. This module replaces the prose with a MAP of site to (status, reason) held
against `tests/_inventory.py::exec_boundary_payload_sites`, a derived population
rather than a list, so a site that DECAYS -- disappears from the source, or keeps
a `fixed` status the detector still sees crossing -- reddens this test instead of
sitting stale until the next person re-derives it by hand.

RED today for every `fixed` entry: the census correctly reports today's SOURCE,
which has not yet had the transport swap. Three sites (the ones plan.md's `##
Files` fixes) are asserted `fixed`, and this suite will not go green on those
three assertions until the implementation phase lands. The `open`/`bounded`
entries are regression pins: they are true today and the plan does not touch
those sites this cycle, so they must stay true after the fix too.

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
# roots, 66 under hooks/scripts and 2 under bin/lib as of this cycle. Held here and nowhere
# else: a duplicated count pin is what this repo's "a broken count pin is usually a
# DUPLICATE" lesson is about. Most of these carry values bounded by construction, which is
# why the map below classifies only the ones that matter while this number forces a look at
# anything new. Lower it when a crossing is removed; add a CENSUS entry when one appears.
DERIVED_SITE_COUNT = 68

CENSUS: dict[str, tuple[str, str]] = {
    # -- Fixed this cycle (plan.md ## Files) --------------------------------
    "writ-bash-write-gate.sh:1107": (
        FIXED,
        "the extractor's command crosses on a mktemp file path (WRIT_BASH_CMD_FILE), "
        "bounded by construction, instead of as the WRIT_BASH_CMD env string",
    ),
    "writ-pre-write-dispatch.sh:258": (
        FIXED,
        "RESULT and CHECK_BODY cross on the translator's stdin as two "
        "NUL-separated records instead of as two argv strings",
    ),
    "writ-worktree-safety.sh:74": (
        FIXED,
        "same command-file transport and sentinel as the Bash gate, replacing "
        "WRIT_WT_CMD=\"$CMD\"",
    ),
    # -- Recorded open, each with the plan's own reason ----------------------
    "writ-state-write-gate.sh:36": (
        BOUNDED,
        "the only unbounded value crossing is the TARGET PATH, and a path long "
        "enough to break execve (over 131072 bytes) is far past the kernel's "
        "PATH_MAX (4096), so no write can land there; the gate would allow a "
        "write that cannot happen",
    ),
    "writ-bash-write-gate.sh:1027": (
        OPEN,
        "the pytest venv swap (WRIT_PARSED_ENVELOPE): the whole envelope crosses "
        "as an env string; over the cap the swap silently does not happen and "
        "pytest runs on the system interpreter, a convenience regression, and "
        "the arm exits 0 either way with no write-decision change",
    ),
    "writ-pre-write-dispatch.sh:120": (
        OPEN,
        "the fallback PARSED_INPUT parse (STDIN_DATA/SKILL_DIR as argv): only "
        "reached when jq is absent or failed, and closing it means deciding what "
        "a hook does when it cannot parse its own envelope at all, a different "
        "question from 'the decider could not run', needing its own tests "
        "against the jq/python parity pins in test_pre_write_parse_parity.py",
    ),
    "common.sh:1923": (
        BOUNDED,
        "the daemon-down can-write fallback body: over the cap the swap does not "
        "happen and the body falls back WITHOUT skill_dir, which loses the "
        "skill-dir exemption and therefore fails CLOSED, not open. Line number "
        "updated from 1811 when this cycle's own common.sh additions "
        "(writ_decider_fault, the stdin transports) moved it down; the crossing "
        "itself is untouched, and the update is the decay detection working",
    ),
    # -- Recorded bounded (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ---------
    "writ-memory-policy-guard.sh:127": (
        BOUNDED,
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
        "read as a decayed site, and the crossing itself is untouched.",
    ),
}

# Which root each key above is looked up against.
_ROOT_FOR_KEY: dict[str, Path] = {
    key: (BIN_LIB_DIR if key.startswith("common.sh:") else HOOK_SCRIPTS_DIR)
    for key in CENSUS
}


@pytest.fixture(scope="module")
def hook_scripts_population() -> dict[str, dict[str, object]]:
    return exec_boundary_payload_sites(scripts_dir=HOOK_SCRIPTS_DIR)


@pytest.fixture(scope="module")
def bin_lib_population() -> dict[str, dict[str, object]]:
    return exec_boundary_payload_sites(scripts_dir=BIN_LIB_DIR)


def _population_for(key: str, hook_pop: dict, lib_pop: dict) -> dict:
    return lib_pop if _ROOT_FOR_KEY[key] == BIN_LIB_DIR else hook_pop


# --------------------------------------------------------------------------- #
# The census itself: every mapped entry checked against the REAL derived
# population, one assertion per entry so a single decayed site names itself
# in the failure rather than hiding inside a loop's aggregate.
# --------------------------------------------------------------------------- #

class TestCensusAgreesWithTheDerivedPopulation:
    @pytest.mark.parametrize("site", sorted(k for k, v in CENSUS.items() if v[0] == FIXED))
    def test_a_fixed_site_no_longer_shows_a_payload_crossing(
        self, site, hook_scripts_population, bin_lib_population
    ) -> None:
        """RED today for all three: the transport swap has not shipped yet.
        Reddened forever after by a regression that reintroduces the crossing at
        this exact site."""
        population = _population_for(site, hook_scripts_population, bin_lib_population)
        assert site not in population, (
            f"{site} is declared fixed but the detector still sees a payload "
            f"crossing there: {population.get(site)}"
        )

    @pytest.mark.parametrize("site", sorted(k for k, v in CENSUS.items() if v[0] in (OPEN, BOUNDED)))
    def test_an_open_or_bounded_site_still_shows_a_payload_crossing(
        self, site, hook_scripts_population, bin_lib_population
    ) -> None:
        """A regression pin: these sites are deliberately NOT touched this cycle,
        so the detector must still find them. Reddened when the site DECAYS --
        moves, is renamed, or is fixed by a future cycle without this map being
        updated to say so."""
        population = _population_for(site, hook_scripts_population, bin_lib_population)
        assert site in population, (
            f"{site} is declared {CENSUS[site][0]} but the detector no longer "
            f"finds it: has it moved, been renamed, or been fixed without "
            f"updating this census?"
        )

    def test_every_census_entry_carries_a_non_empty_reason(self) -> None:
        assert all(reason for _status, reason in CENSUS.values())

    def test_every_census_status_is_one_of_the_three_declared(self) -> None:
        assert all(status in (FIXED, OPEN, BOUNDED) for status, _reason in CENSUS.values())

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
        unmapped = sorted(derived - set(CENSUS))
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
        fixed = {k for k, v in CENSUS.items() if v[0] == FIXED}
        assert fixed == {
            "writ-bash-write-gate.sh:1107",
            "writ-pre-write-dispatch.sh:258",
            "writ-worktree-safety.sh:74",
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

SPLICE_MAP: dict[str, str] = {
    "writ-pressure-audit.sh:19": (
        "splices a path and a session id into the program's own text (no nested "
        "command substitution, unlike the two sites this cycle fixes), decides "
        "nothing (SessionEnd, observational, always exits 0), and its own splice "
        "of $SESSION_ID into a Python string literal is a real LATENT injection "
        "surface this map records rather than closes: a session id containing a "
        "quote or a backslash would corrupt the embedded string. Out of scope "
        "this cycle (plan.md's own 'Out of scope' section): fixing it needs its "
        "own session-id validation decision, not a transport swap."
    ),
}


class TestNestedProgramSpliceMapMatchesTheDerivedPopulation:
    def test_the_map_equals_the_derived_population_by_set_equality(self) -> None:
        """RED until the implementation phase lands: today the derivation ALSO
        finds writ-memory-policy-guard.sh:52 and :71 (the two live splice sites
        this cycle removes), so the map -- which holds only the one site this
        cycle does NOT touch -- is a strict subset of the real population until
        those two sites are gone. Reddened forever after by reintroducing the
        splice anywhere under either root, and by fixing writ-pressure-audit.sh
        without updating this map."""
        derived = (
            set(nested_program_splice_sites(scripts_dir=HOOK_SCRIPTS_DIR))
            | set(nested_program_splice_sites(scripts_dir=BIN_LIB_DIR))
        )
        assert derived == set(SPLICE_MAP), (
            f"derived={sorted(derived)} map={sorted(SPLICE_MAP)}"
        )

    def test_every_map_entry_carries_a_non_empty_reason(self) -> None:
        assert all(reason for reason in SPLICE_MAP.values())

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
