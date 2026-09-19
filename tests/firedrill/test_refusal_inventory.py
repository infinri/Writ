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

from tests.firedrill._census import (
    ACTION_MARKERS,
    DEFERRED_SCRIPTS,
    by_id,
    deferred_scripts,
    matches_action_marker,
    refusing_scripts,
)

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


def _marker_map() -> dict:
    """`ACTION_MARKERS` as the marker-to-owners map, or a loud failure.

    The structural half of this cycle's defect 2 (plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3): the marker set was a bare tuple with the
    owning hook named only in a trailing comment, so nothing verified that the named
    hook exists, that a trigger for it exists, or that the attribution was ever true.
    """
    if not isinstance(ACTION_MARKERS, dict):
        pytest.fail(
            f"skeleton: ACTION_MARKERS is a {type(ACTION_MARKERS).__name__}, not the "
            "map from marker phrase to the census refusal ids that must emit it "
            "(plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 ## Files, "
            "tests/firedrill/_census.py)",
            pytrace=False,
        )
    assert ACTION_MARKERS, (
        "the marker map is empty, so every guard over it below would pass on any tree "
        "and matches_action_marker would match nothing at all"
    )
    return ACTION_MARKERS


class TestEveryActionMarkerHasAResolvableOwner:
    """Capability 11: a marker's owner is a census refusal id, so the claim
    "this hook emits this phrase" is something a test can drive rather than a comment.

    An id and not a script name, because an id carries a real `setup(iso)`; a script
    name alone does not say which trigger to build, and two of these scripts refuse on
    more than one path.
    """

    def test_the_marker_population_is_a_non_empty_map(self) -> None:
        markers = _marker_map()
        assert all(isinstance(owners, (tuple, list)) for owners in markers.values()), (
            "every marker's owners must be a sequence of census refusal ids: "
            f"{ {m: o for m, o in markers.items() if not isinstance(o, (tuple, list))} }"
        )

    def test_every_marker_declares_an_owner_that_the_census_resolves_with_a_setup(
        self,
    ) -> None:
        markers = _marker_map()
        problems = []
        for marker, owners in markers.items():
            if not owners:
                problems.append(f"{marker!r}: declares no owning refusal id")
                continue
            for refusal_id in owners:
                try:
                    entry = by_id(refusal_id)
                except KeyError:
                    problems.append(
                        f"{marker!r}: owner {refusal_id!r} is not a refusal this census "
                        "declares"
                    )
                    continue
                if not callable(entry.setup):
                    problems.append(
                        f"{marker!r}: owner {refusal_id!r} carries no callable setup, so "
                        "no test can drive it to prove the marker is still emitted"
                    )
        assert not problems, "\n".join(problems)

    def test_every_marker_is_lowercase(self) -> None:
        """Capability 12. `matches_action_marker` lowercases the REASON and compares it
        against the markers as written, so a marker carrying a capital letter can never
        match anything: it would be unmatchable by construction, which is the silent
        weakening this map exists to prevent.
        """
        markers = _marker_map()
        uppercase = [m for m in markers if m != m.lower()]
        assert not uppercase, (
            f"these markers can never match a lowercased reason: {uppercase}"
        )


class TestMatchesActionMarkerKeepsItsContractOverTheMap:
    """Capability 15: iterating a dict yields its keys, so converting the tuple to a map
    must leave the runtime predicate's behaviour exactly as it was.
    """

    def test_a_reason_carrying_a_declared_marker_matches_whatever_its_case(self) -> None:
        markers = _marker_map()
        failed = [
            marker
            for marker in markers
            if not matches_action_marker(f"ENF-TEST-000: {marker.upper()} and re-run.")
        ]
        assert not failed, (
            f"a reason carrying these declared markers did not match: {failed}"
        )

    def test_a_reason_carrying_no_declared_marker_does_not_match(self) -> None:
        markers = _marker_map()
        reason = "ENF-TEST-000: this refusal names a quokka-shaped counterweight."
        assert not matches_action_marker(reason), (
            "a reason naming no declared action matched anyway, so the predicate cannot "
            f"tell a refusal that names a way out from one that does not: {sorted(markers)}"
        )


# ── The decoy guard (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, defect 2) ────
#
# `matches_action_marker` is a plain lowercased substring test and stays one: word
# boundaries were measured and rejected, because they fix `prefix` and BREAK
# `templates/`, and a false negative here reds a refusal that genuinely names a way
# out. The DATA is what tightens.
#
# Each decoy below is written out IN FULL, never generated as `marker + "d"`, for
# the same reason an anchor is never sliced out of the population it is checked
# against: a decoy composed from the marker is the marker again, and the two sides
# of the assertion would be one side.
#
# MEASURED BEFORE THE CHANGE, so the guard is known to have been capable of
# failing: every decoy below returns True against today's map, via the loose token
# named as its value ('prefix handling changed' -> 'fix', 'bypassed' -> 'bypass',
# 'templates/' -> 'template', 're-sends' -> 're-send').
_TIGHTENED_MARKER_DECOYS: dict[str, str] = {
    "prefix handling changed": "fix",
    "bypassed": "bypass",
    "templates/": "template",
    "re-sends": "re-send",
}

# The reason the credential arm of writ-bash-write-gate.sh really emits
# (writ-bash-write-gate.sh:3260), carrying the action in its PLURAL form. This is
# the case that kills the word-boundary alternative: the phrase fix keeps the
# refusal, the boundary fix would have lost it.
_PLURAL_FORM_REASON = "Name non-secret templates .env.example / .env.sample / *.pub."
_PLURAL_FORM_MARKER = "name non-secret templates"


def _decoys() -> list:
    """Never an empty argvalues list: an empty parametrization SKIPS, and a skip in
    the run output reads like coverage."""
    return sorted(_TIGHTENED_MARKER_DECOYS) or [
        pytest.param("<no decoy declared>", id="decoys-empty")
    ]


class TestEachTightenedMarkerNoLongerMatchesItsMeasuredDecoy:
    """Capabilities 13 and 14, and the half that makes the tightening real rather
    than cosmetic.

    The two sides are independently produced: the decoy is a literal string typed
    here, and the other side is the real predicate run over the live marker map. A
    tightening that only reworded a comment would leave the decoy matching.
    """

    @pytest.mark.parametrize("decoy", _decoys())
    def test_the_decoy_satisfies_no_action_marker(self, decoy) -> None:
        markers = _marker_map()
        assert not matches_action_marker(decoy), (
            f"{decoy!r} still satisfies an action marker, so a refusal reason that "
            "names no way out at all would read as marker-carrying here and in the "
            "generic loop alike. It matched: "
            f"{[m for m in markers if m in decoy.lower()]}"
        )

    @pytest.mark.parametrize("decoy", _decoys())
    def test_the_loose_token_the_decoy_exploited_is_no_longer_declared(self, decoy) -> None:
        markers = _marker_map()
        loose = _TIGHTENED_MARKER_DECOYS[decoy]
        assert loose not in markers, (
            f"{loose!r} is still a declared marker as a bare token, which is what lets "
            f"{decoy!r} satisfy it. The plan tightens it to a phrase copied from its "
            "owner's real emitted reason, and the liveness test in "
            "tests/firedrill/test_bash_refusals.py drives every owner through the real "
            "subprocess to confirm the phrase is still emitted"
        )

    def test_a_reason_naming_its_action_in_the_plural_still_matches(self) -> None:
        """Capability 15. `matches_action_marker` stays a substring test, so the
        credential gate's real reason keeps its match through the plural, and the
        phrase it matches is the tightened one rather than the bare token."""
        markers = _marker_map()
        matched = sorted(m for m in markers if m in _PLURAL_FORM_REASON.lower())
        assert matched == [_PLURAL_FORM_MARKER], (
            "the credential gate's real reason must match the tightened phrase and "
            f"nothing looser: {matched}"
        )

    def test_the_branch_coupled_re_issue_marker_is_deliberately_left_loose(self) -> None:
        """Capability 16, and the one marker this cycle does NOT tighten.

        MEASURED, not reasoned. Driving the census refusal `read-junk-enforce`
        through the real `run_hook` subprocess three times reaches
        writ-read-junk-gate.sh's SIZE arm every time, emitting "Re-issue with
        offset+limit to proceed." But the arm is chosen by a `git check-ignore`
        that runs whenever the fixture's directory is inside a work tree, and the
        harness's `.git` MARKER does not prevent that: an empty `.git` directory is
        not a repository, and git's discovery walks PAST it to a real parent work
        tree (measured: `rev-parse --is-inside-work-tree` returns true). Placing the
        same fixture under a work tree whose .gitignore carries `*.log` flips it to
        the junk arm, which emits "re-issue the Read after stating why" instead.

        So the branch is environment-coupled, and per the plan's own rule a phrase
        that would red a correct refusal on someone else's machine is not shipped:
        `re-issue` keeps its loose spelling, and `re-issued` keeps matching it. That
        is recorded here as a decision rather than left as an accident, which is why
        `re-issued` is absent from the decoy map above.
        """
        markers = _marker_map()
        assert "re-issue" in markers, (
            "re-issue must stay declared as the loose token until the junk-gate arm "
            "the census fixture reaches is stable across environments"
        )
        assert matches_action_marker("re-issued"), (
            "re-issued is the known, accepted false positive of leaving re-issue "
            "loose; if it stopped matching, the marker was tightened without the "
            "branch measurement being redone"
        )
