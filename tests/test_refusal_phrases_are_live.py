"""Every phrase a refusal tells the user to type must still mint something.

Finding 1 of plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, measured: three live refusals
instruct the user to reply `manual testing approved`, which the 2026-08-23 narrowing
removed. `tests/test_approval_exact_only.py` lists that phrase among the eleven that no
longer grant and pins `manual test approved` as the only one that does. A user who follows
the instruction is not refused twice, they are refused forever, because nothing they were
told to do can change the state. That is this repo's own keystone (a refusal naming no way
out is a deadlock rather than a control) regressing in three places the keystone was never
applied to, which is why the detector below is a POPULATION rather than three assertions.

THE VERDICT COMES FROM THE REAL PREDICATES, NEVER FROM A COPY OF A PHRASE LIST. The
left-hand side is the phrase lifted out of the refusal text; the right-hand side is the four
live minting predicates, imported from the modules the hooks actually load by path. An
assertion whose right-hand side is computed from its left cannot fail, and this session
shipped that shape twice.

THE PREDICATE SET IS A MAP KEYED BY FUNCTION NAME, so a predicate that is deleted or
renamed fails by name instead of quietly shrinking the union that decides whether a phrase
is live.

RED TODAY, and the red is the deliverable: the three sites named above quote a dead phrase,
and `DECLARED_PHRASE_SITES["test-skeletons-no-test"]` anchors a refusal
`writ/session/approval_workflow.py` does not emit yet (finding 2 adds it).
"""
from __future__ import annotations

import os
import sys

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BIN_LIB = os.path.join(SKILL_ROOT, "bin", "lib")
if BIN_LIB not in sys.path:
    sys.path.insert(0, BIN_LIB)

from tests._inventory import (  # noqa: E402
    DECLARED_PHRASE_SITES,
    user_directed_phrase_sites,
    user_directed_phrases,
)

# The four predicates that can turn a typed phrase into state. Declared as (module,
# attribute) pairs and resolved with getattr rather than imported by name, so a predicate
# that was renamed or deleted is reported BY NAME below instead of raising at import time
# and taking the whole module's coverage with it.
DECLARED_MINT_PREDICATES: tuple[tuple[str, str], ...] = (
    ("approval_match", "is_approval"),
    ("approval_match", "is_replan_request"),
    ("approval_match", "is_override"),
    ("manual_test_grant", "is_grant_phrase"),
)

# A phrase no predicate in this repository accepts, used to prove the union is CONDITIONAL
# rather than answering "live" for anything it is handed.
SENTINEL_DEAD_PHRASE = "quokka testing approved"


def live_mint_predicates() -> dict[str, object]:
    """`{function name: the real callable}`, with None for one that no longer exists."""
    import approval_match
    import manual_test_grant

    modules = {"approval_match": approval_match, "manual_test_grant": manual_test_grant}
    return {name: getattr(modules[module], name, None) for module, name in DECLARED_MINT_PREDICATES}


def _verdicts(phrase: str) -> tuple[list[str], list[str], list[str]]:
    """(accepted by, rejected by, missing) for `phrase`, each a list of predicate names."""
    accepted: list[str] = []
    rejected: list[str] = []
    missing: list[str] = []
    for name, predicate in live_mint_predicates().items():
        if predicate is None:
            missing.append(name)
        elif predicate(phrase):
            accepted.append(name)
        else:
            rejected.append(name)
    return accepted, rejected, missing


def _site_params() -> list:
    """Collection-time parameters, one per derived site, never an empty list.

    The shape `tests/test_corpus_floor.py::_floor_labels` uses: a population that came back
    empty yields one sentinel case that FAILS, because a parametrized test over nothing
    passes on any tree and reads exactly like coverage.
    """
    try:
        sites = user_directed_phrase_sites()
    except Exception as exc:  # noqa: BLE001
        return [pytest.param("<derivation raised>", str(exc), id="derivation-unavailable")]
    if not sites:
        return [pytest.param("<no site derived>", "", id="derivation-empty")]
    return [pytest.param(site, phrase, id=site) for site, phrase in sorted(sites.items())]


def _declared_params() -> list:
    if not DECLARED_PHRASE_SITES:
        return [pytest.param("<no site declared>", id="declared-map-empty")]
    return [pytest.param(site_id, id=site_id) for site_id in sorted(DECLARED_PHRASE_SITES)]


class TestTheFourPredicatesAreTheRealCallables:
    """The right-hand side of every verdict below is these four functions, so this class
    proves they resolve and that none of them is an always-False stub."""

    @pytest.mark.parametrize(
        ("module_name", "function_name"),
        DECLARED_MINT_PREDICATES,
        ids=[f"{m}.{f}" for m, f in DECLARED_MINT_PREDICATES],
    )
    def test_the_declared_predicate_still_exists(self, module_name, function_name) -> None:
        predicate = live_mint_predicates()[function_name]
        assert callable(predicate), (
            f"{module_name}.{function_name} is not a callable on the module the hooks load "
            "by path, so the union that decides whether a refusal's phrase is live just "
            "got one predicate narrower without anything failing"
        )

    def test_each_predicate_accepts_the_phrase_its_own_module_declares(self) -> None:
        """A positive control. Four predicates that all returned False would make every
        site below fail for the wrong reason, and four that all returned True would make
        every site pass for the wrong reason. The phrases come from the modules' own
        constants where they have one.
        """
        import approval_match
        import manual_test_grant

        live = {
            "is_approval": "approved",
            "is_replan_request": approval_match.REPLAN_PHRASE,
            "is_override": approval_match.OVERRIDE_PHRASE,
            "is_grant_phrase": manual_test_grant.GRANT_PHRASES[0],
        }
        predicates = live_mint_predicates()
        for name, phrase in live.items():
            predicate = predicates[name]
            assert callable(predicate) and predicate(phrase) is True, (
                f"{name} rejects {phrase!r}, the phrase its own module declares, so it "
                "cannot be the predicate the hook mints from"
            )

    def test_the_union_rejects_a_phrase_no_predicate_accepts(self) -> None:
        accepted, _, missing = _verdicts(SENTINEL_DEAD_PHRASE)
        assert not missing, f"predicates missing entirely: {missing}"
        assert accepted == [], (
            f"{SENTINEL_DEAD_PHRASE!r} was accepted by {accepted}, so this union answers "
            "'live' for a phrase this repository never mints from and cannot judge a "
            "refusal at all"
        )


class TestEveryUserDirectedPhraseStillMints:
    """Capability 1. The population IS the parametrization, so a fourth refusal added next
    month is judged without anyone remembering to register it, and it fails BY NAME.

    TRIAGE RULE FOR A RED HERE: the refusal quotes a phrase nothing mints from, which is a
    deadlock for whoever hits it. Fix the REFUSAL to name a live phrase. Widening
    `GRANT_PHRASES` or `is_approval` to make this green would undo an explicit user
    directive and restore the accident where a message merely DISCUSSING a phrase minted a
    real grant.
    """

    @pytest.mark.parametrize(("site", "phrase"), _site_params())
    def test_the_phrase_this_site_quotes_is_accepted_by_a_live_predicate(
        self, site, phrase
    ) -> None:
        assert not site.startswith("<"), (
            f"the site derivation produced no population ({site}: {phrase}), so every "
            "case in this class would pass on any tree"
        )
        accepted, rejected, missing = _verdicts(phrase)
        assert accepted, (
            f"{site} tells the user to type {phrase!r}, and no live minting predicate "
            f"accepts it: rejected by {sorted(rejected)}"
            + (f"; missing entirely: {sorted(missing)}" if missing else "")
            + ". A refusal that names a phrase nothing mints from is a deadlock rather "
            "than a control."
        )


class TestTheDerivationIsConditional:
    """Capability 2. A derivation that matched nothing, or that reported every line it was
    shown, would make the class above pass on any tree. Both halves are proved against
    synthetic sources under tmp_path, never by editing the real tree.
    """

    def _synthetic_root(self, tmp_path, text: str):
        scripts = tmp_path / "hooks" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "synthetic-gate.sh").write_text(text)
        return ((scripts, "*.sh"),)

    def test_a_synthetic_refusal_quoting_a_dead_phrase_is_reported(self, tmp_path) -> None:
        spec = self._synthetic_root(
            tmp_path,
            '#!/bin/bash\n'
            f'REASON="Refusing this write: ask the user to reply \\"{SENTINEL_DEAD_PHRASE}\\"."\n',
        )
        sites = user_directed_phrase_sites(spec=spec, base=tmp_path)

        assert sites == {"hooks/scripts/synthetic-gate.sh:2": SENTINEL_DEAD_PHRASE}, (
            "the derivation did not report a refusal that quotes a dead phrase on a source "
            f"line it was handed: {sites!r}"
        )
        accepted, _, _ = _verdicts(next(iter(sites.values())))
        assert accepted == [], (
            "the synthetic phrase was accepted by a live predicate, so this case proves "
            "nothing about the detector"
        )

    def test_a_synthetic_refusal_quoting_a_live_phrase_is_reported_and_passes(
        self, tmp_path
    ) -> None:
        """The other half of conditional: the same grammar over a LIVE phrase yields a site
        the predicate union accepts, so the class above is not failing everything it sees.
        """
        spec = self._synthetic_root(
            tmp_path, '#!/bin/bash\nREASON="ask the user to reply \\"approved\\"."\n'
        )
        sites = user_directed_phrase_sites(spec=spec, base=tmp_path)

        assert sites == {"hooks/scripts/synthetic-gate.sh:2": "approved"}, (
            f"the derivation did not report the live-phrase directive: {sites!r}"
        )
        accepted, _, _ = _verdicts("approved")
        assert accepted, "no live predicate accepts `approved`"

    def test_a_source_with_no_directive_yields_no_site(self, tmp_path) -> None:
        spec = self._synthetic_root(
            tmp_path, '#!/bin/bash\nREASON="Refusing this write. Nothing to type here."\n'
        )
        assert user_directed_phrase_sites(spec=spec, base=tmp_path) == {}


class TestAgentDirectedSayFormsAreExcludedStructurally:
    """Capability 2's other side, and the two false positives this cycle MEASURED.

    `writ/session/gates.py` and `hooks/scripts/validate-exit-plan.sh` both carry
    `Say "Say approved to proceed"`, which instructs the AGENT about what to tell the user
    rather than naming a phrase the user types. A naive quoted-span scan flags both.

    THE EXCLUSION IS STRUCTURAL, NOT AN ALLOWLIST ENTRY: neither line carries a `reply`
    verb, and `user and say:` is not `user to say`. Each trap is located by its own anchor
    text rather than by a line literal, so this class survives the lines moving, and the
    three real sites are asserted PRESENT in the same population so the exclusion cannot be
    passing because the derivation found nothing.
    """

    TRAPS: tuple[tuple[str, str], ...] = (
        ("writ/session/gates.py", 'Present your plan to the user and say:'),
        ("hooks/scripts/validate-exit-plan.sh", '2. Say \\"Say approved to proceed\\"'),
    )

    REAL_SITES: tuple[tuple[str, str], ...] = (
        ("hooks/scripts/writ-state-write-gate.sh", "is Writ gate state. Approvals, mode"),
        ("hooks/scripts/writ-bash-write-gate.sh", "it names Writ gate state"),
        ("hooks/scripts/writ-bash-write-gate.sh", "it writes to Writ gate state"),
    )

    @staticmethod
    def _line_of(relpath: str, anchor: str) -> int:
        path = os.path.join(SKILL_ROOT, relpath)
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if anchor in line:
                    return number
        pytest.fail(
            f"the anchor {anchor!r} is no longer in {relpath}, so this case cannot say "
            "anything about that site. Re-point the anchor at the sentence that file now "
            "carries; do not delete the case.",
            pytrace=False,
        )

    @pytest.mark.parametrize(
        ("relpath", "anchor"), TRAPS, ids=["gates-say-form", "exit-plan-say-form"]
    )
    def test_the_agent_directed_say_form_is_not_a_site(self, relpath, anchor) -> None:
        line = self._line_of(relpath, anchor)
        sites = user_directed_phrase_sites()

        assert f"{relpath}:{line}" not in sites, (
            f"{relpath}:{line} instructs the AGENT to tell the user something; it is not a "
            "phrase the user types, and the grammar must exclude it without an allowlist "
            f"entry. It was derived as {sites.get(f'{relpath}:{line}')!r}"
        )

    @pytest.mark.parametrize(
        ("relpath", "anchor"),
        REAL_SITES,
        ids=["state-write-gate", "bash-write-state-text", "bash-write-state-target"],
    )
    def test_the_real_refusal_site_is_in_the_population(self, relpath, anchor) -> None:
        """The non-vacuity half: an exclusion that excluded everything would pass the cases
        above while seeing none of the sites this cycle exists to judge."""
        line = self._line_of(relpath, anchor)
        sites = user_directed_phrase_sites()

        assert f"{relpath}:{line}" in sites, (
            f"{relpath}:{line} is a refusal that tells the user what to reply, and the "
            "derivation did not see it, so every verdict above is blind to this site. "
            f"Derived sites in that file: "
            f"{sorted(k for k in sites if k.startswith(relpath + ':'))}"
        )


class TestEveryDeclaredSiteStillResolves:
    """Capability 3. `DECLARED_PHRASE_SITES` is the blindness guard: each id must resolve to
    a derived site in the named file whose line carries its anchor, so a derivation that
    went blind fails by NAME rather than by returning a shorter map nobody counted.
    """

    @pytest.mark.parametrize("site_id", _declared_params())
    def test_the_declared_site_resolves_to_a_derived_site_carrying_its_anchor(
        self, site_id
    ) -> None:
        assert not site_id.startswith("<"), (
            "DECLARED_PHRASE_SITES is empty, so this class checks nothing"
        )
        relpath, anchor = DECLARED_PHRASE_SITES[site_id]
        path = os.path.join(SKILL_ROOT, relpath)
        assert os.path.isfile(path), f"{site_id}: {relpath} does not exist"

        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        anchored = {number for number, line in enumerate(lines, start=1) if anchor in line}
        assert anchored, (
            f"{site_id}: no line in {relpath} carries the anchor {anchor!r}. Either the "
            "refusal was reworded (re-point the anchor at the sentence it now emits) or "
            "the refusal is gone. Deleting this entry to go green is the loosening the map "
            "exists to stop."
        )

        sites = user_directed_phrase_sites()
        resolved = sorted(
            site for site in sites
            if site.rsplit(":", 1)[0] == relpath and int(site.rsplit(":", 1)[1]) in anchored
        )
        assert resolved, (
            f"{site_id}: {relpath} carries the anchor on line(s) {sorted(anchored)}, but "
            "the derivation reports no user-directed phrase there, so that refusal names "
            "no phrase the user can type. Derived sites in that file: "
            f"{sorted(k for k in sites if k.startswith(relpath + ':'))}"
        )
