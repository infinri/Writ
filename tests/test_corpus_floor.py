"""The corpus floor's pure properties: `is_complete` and `corpus_shortfall` over the
population `tests/_inventory.py::corpus_floor()` derives from the tracked
`writ-corpus.cypher`.

OUTSIDE `requires_bible` ON PURPOSE (plan.md, "What I decided NOT to do", item 8).
Every census below is built by the test itself and handed to a pure predicate, so
this module needs neither `bible/` nor Neo4j and runs on CI and on a clean checkout,
which is exactly where a floor derived from a gitignored tree would be underivable.

NO COUNT LITERAL APPEARS IN THIS FILE. Every expectation is derived from the floor,
so a label added to the corpus is covered here with no edit, and growing the corpus
can never redden these tests. The precision of the derivation itself is pinned
against synthetic dumps under `tmp_path` in `tests/test_count_pin_discipline.py`,
never against the live corpus's current size.
"""
from __future__ import annotations

import pytest

# The parametrize population's stand-in when the derivation is not there yet. An
# empty `argvalues` list makes pytest report a SKIP, which reads exactly like
# coverage that ran; one sentinel param reads as the RED it is.
_MISSING = "<corpus_floor() is unavailable>"


def _floor() -> dict[str, int]:
    """The derived floor, or a loud failure. Never silently empty.

    ANTI-VACUITY: a derivation that returned nothing would make every census built
    from it trivially satisfy every assertion in this file, which is the failure mode
    this repo has shipped before (a test passing because both sides were empty).
    """
    try:
        from tests._inventory import corpus_floor
    except ImportError as exc:
        pytest.fail(
            "skeleton: tests/_inventory.py has no corpus_floor() yet (plan.md ## Files "
            f"assigns the source-derived corpus population there): {exc}",
            pytrace=False,
        )
    floor = corpus_floor()
    assert floor, (
        "corpus_floor() derived no labels at all; every census assertion in this "
        "module would then pass on any tree"
    )
    return floor


def _floor_labels() -> list:
    """Collection-time label population for the parametrized cases."""
    try:
        from tests._inventory import corpus_floor

        labels = sorted(corpus_floor())
    except Exception:  # noqa: BLE001
        return [pytest.param(_MISSING, id="corpus-floor-unavailable")]
    return labels or [pytest.param(_MISSING, id="corpus-floor-empty")]


def _corpus():
    """`tests/_corpus.py` with this cycle's two predicates, or a loud failure."""
    import tests._corpus as corpus

    missing = [n for n in ("corpus_shortfall", "is_complete") if not hasattr(corpus, n)]
    if missing:
        pytest.fail(
            f"skeleton: tests/_corpus.py has no {', '.join(missing)} yet (plan.md "
            "## Files: is_complete is rebuilt on corpus_shortfall so every declared "
            "label is floored and the short ones are reportable by name)",
            pytrace=False,
        )
    return corpus


class TestEveryDeclaredLabelIsFloored:
    """Capability 5, and the defect this cycle exists to close: `is_complete`
    returned True on a graph holding ZERO AntiPattern, Technique, ForbiddenResponse,
    PressureScenario, Rationalization or WorkedExample, because it floored five
    labels while declaring eleven and naming neither Abstraction nor Category.

    The population IS the parametrization, so a label added to the corpus is checked
    here without anyone remembering to add it.
    """

    @pytest.mark.parametrize("label", _floor_labels())
    def test_zeroing_one_label_is_incomplete_and_names_that_label(self, label) -> None:
        floor = _floor()
        corpus = _corpus()
        counts = dict(floor)
        counts[label] = 0

        assert corpus.is_complete(counts) is False, (
            f"a census holding ZERO {label} read as complete; that label is declared "
            "by the corpus floor but nothing floors it"
        )
        shortfall = corpus.corpus_shortfall(counts)
        assert shortfall == {label: (0, floor[label])}, (
            f"the shortfall must name {label} alone, with its live and required "
            f"count: {shortfall!r}"
        )

    @pytest.mark.parametrize("label", _floor_labels())
    def test_a_label_absent_from_the_census_entirely_is_short(self, label) -> None:
        """The measured shape of the defect: `_LABELS` omitted Abstraction and
        Category, so a census projected onto it carried no KEY for them at all. A
        floor that only compares the keys it is handed cannot see that.
        """
        floor = _floor()
        corpus = _corpus()
        counts = {k: v for k, v in floor.items() if k != label}

        assert corpus.is_complete(counts) is False, (
            f"a census with no {label} key at all read as complete"
        )
        assert corpus.corpus_shortfall(counts) == {label: (0, floor[label])}


class TestACensusAtOrAboveTheFloorIsComplete:
    """Capability 6, and the refusal-direction half the plan commits to: `>=` is what
    makes an ADDITIVE tree safe. A `bible/` holding MORE nodes than the tracked dump
    must not refuse, or every corpus edit made before `writ export-cypher` runs would
    stop the suite.
    """

    def test_a_census_exactly_at_the_floor_is_complete(self) -> None:
        floor = _floor()
        corpus = _corpus()
        assert corpus.is_complete(dict(floor)) is True
        assert corpus.corpus_shortfall(dict(floor)) == {}

    def test_an_additive_census_above_the_floor_does_not_refuse(self) -> None:
        floor = _floor()
        corpus = _corpus()
        counts = {label: required + 3 for label, required in floor.items()}
        assert corpus.is_complete(counts) is True, (
            "a tree holding MORE nodes than the tracked dump must not refuse: adding "
            "to bible/ without re-exporting is the common case and is safe under >="
        )
        assert corpus.corpus_shortfall(counts) == {}

    def test_an_unknown_extra_label_in_the_census_is_not_a_shortfall(self) -> None:
        floor = _floor()
        corpus = _corpus()
        counts = dict(floor)
        counts["WidgetFixtureLabel"] = 0
        assert corpus.corpus_shortfall(counts) == {}
        assert corpus.is_complete(counts) is True


class TestShortfallNamesTheShortLabelsAndNothingElse:
    """Capability 7. The verdict is held as a MAP keyed by label rather than a bool,
    which is what lets the session-start refusal name WHICH label is short instead of
    saying "incomplete".
    """

    def test_it_reports_exactly_the_labels_below_the_floor(self) -> None:
        floor = _floor()
        corpus = _corpus()
        assert len(floor) >= 2, (
            f"this case needs at least two declared labels to have a 'some short, "
            f"some met' census at all: {floor!r}"
        )
        ordered = sorted(floor)
        zeroed = ordered[::2]
        counts = {label: 0 for label in zeroed}
        counts.update({label: floor[label] for label in ordered[1::2]})

        assert corpus.corpus_shortfall(counts) == {
            label: (0, floor[label]) for label in zeroed
        }

    def test_one_node_below_the_floor_is_short_in_every_label(self) -> None:
        """The only tree that can newly refuse: a `bible/` holding FEWER nodes of a
        label than the tracked dump. One node is the whole margin.
        """
        floor = _floor()
        corpus = _corpus()
        counts = {label: required - 1 for label, required in floor.items()}
        assert corpus.corpus_shortfall(counts) == {
            label: (required - 1, required) for label, required in floor.items()
        }
        assert corpus.is_complete(counts) is False

    def test_an_empty_census_is_short_in_every_declared_label(self) -> None:
        floor = _floor()
        corpus = _corpus()
        assert corpus.corpus_shortfall({}) == {
            label: (0, required) for label, required in floor.items()
        }
        assert corpus.is_complete({}) is False


class TestCorpusConstantsComeFromTheFloor:
    """Capability 9, pure half. `_LABELS` was hand-written and named eleven labels,
    omitting Abstraction and Category; `EXPECTED` was hand-written and named four.
    Both now come from the corpus, so the label LIST grows with the corpus too.

    THERE IS NO LIVE SIBLING FOR "a key for each", and there deliberately is not.
    `methodology_counts()` builds its keys from `_LABELS`, so once the assertion below
    holds, "the census carries a key for every declared label" is structurally true and
    a test of it cannot fail, even against an empty graph. What CAN fail lives where it
    can be reached: the session-start preflight in tests/conftest.py refuses the whole
    run, naming each short label with its live and required count, when the census
    reports fewer nodes than the floor for one, and
    tests/test_pol2b_suite_perf.py::TestE5CountsBatched::test_methodology_counts_correct
    reads the live census against an independent direct count.
    """

    def test_labels_covers_every_label_the_floor_declares(self) -> None:
        floor = _floor()
        import tests._corpus as corpus

        assert set(corpus._LABELS) == set(floor), (
            "_LABELS and the corpus floor name different label sets; missing from "
            f"_LABELS: {sorted(set(floor) - set(corpus._LABELS))}, declared by "
            f"_LABELS but not by the corpus: {sorted(set(corpus._LABELS) - set(floor))}"
        )

    def test_expected_is_the_derived_floor_and_min_rules_is_its_rule_entry(self) -> None:
        floor = _floor()
        import tests._corpus as corpus

        assert corpus.EXPECTED == floor, (
            "EXPECTED is still hand-written; it must be the map corpus_floor() derives "
            f"from the tracked dump. Difference: "
            f"{sorted(set(floor.items()) ^ set(corpus.EXPECTED.items()))}"
        )
        assert corpus.MIN_RULES == floor["Rule"]
