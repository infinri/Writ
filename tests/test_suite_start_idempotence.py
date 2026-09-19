"""The live-graph half of suite-start idempotence (plan.md, cycle 9).

Pins the capabilities.md "Graph idempotence" section's non-operational items:
the preflight actually wipes RECORD_LABELS residue and rewarms the corpus
against the real disposable Neo4j instance, a refused wipe reaches the
operator as `pytest.UsageError` rather than an INTERNALERROR, and the
preflight prints exactly one report line naming what it deleted.

SAFETY. This file, like tests/test_cycle8_graph_isolation.py, must never
open, write to, or wipe `bolt://localhost:7687` (the live production graph).
Every test here is guarded by `_require_disposable_graph`, which FAILS (never
silently proceeds) if the resolved URI is production, and SKIPS only when the
disposable instance at bolt://localhost:7688 does not answer: an environment
fact, not a failed measurement.

WHY THESE ARE INTEGRATION TESTS, PER ENF-SYS-005. The claim under test is
idempotence of a real whole-graph DELETE plus rebuild; a mocked driver would
return whatever the test told it to and would prove nothing about that claim.
Mocking is used for exactly one behavior here: the refusal-to-`UsageError`
conversion, which needs `FullWipeRefused` to fire without a production
instance to refuse against, and the plan names this as the one place a mock
is licensed.

WHY THE SEEDING MATTERS. A preflight that wipes nothing, run against a graph
that happens to already be free of RECORD_LABELS residue, looks identical to
one that worked. Every test that asserts "the graph is now free of X" seeds
at least one node of X first, so the assertion cannot be satisfied by a
no-op.

RED TODAY. `tests/_graph.py` does not yet define `wipe_everything` or
`label_census`, and `tests/conftest.py::_preflight_isolated_graph` does not
yet call either one. Every test below that depends on one of those two names
existing calls `_require_seam` first, which turns the resulting ImportError/
AttributeError into a `pytest.fail("skeleton: ...")` naming exactly what is
missing, rather than skipping or failing with an unrelated traceback. Tests
that exercise `_preflight_isolated_graph`'s CURRENT (pre-fix) behavior
directly (the report-line tests) fail on a plain assertion instead, because
the function they call already exists; only its body is incomplete.

Run: .venv/bin/python -m pytest tests/test_suite_start_idempotence.py -v
"""

from __future__ import annotations

import contextlib
import io
import re
import uuid
from pathlib import Path

import pytest

from writ.graph.db._common import RECORD_LABELS

REPO_ROOT = Path(__file__).resolve().parent.parent


def _require_seam(module, name: str):
    """Fetch `name` off `module`, or fail loudly naming exactly what is missing.

    Never skips: an absent seam is unfinished implementation, not an
    environment fact, so the nine sites and the two idempotence helpers this
    cycle adds must show up as a named RED, not a silent pass-through.
    """
    if not hasattr(module, name):
        pytest.fail(f"skeleton: {module.__name__}.{name} is not implemented yet")
    return getattr(module, name)


def _create_marker_node(label: str, marker: str) -> None:
    """CREATE one throwaway node of `label`, tagged `marker`. Test seeding only.

    CREATE, not MERGE: this file's whole point is proving a wipe deletes real
    residue, so a duplicate-safe write would undercount what a broken wipe
    left behind.
    """
    import asyncio

    from tests._graph import connection

    async def _seed() -> None:
        db = connection()
        try:
            async with db._driver.session(database=db._database) as s:
                await (
                    await s.run(f"CREATE (n:{label} {{marker: $marker}})", marker=marker)
                ).consume()
        finally:
            await db.close()

    asyncio.run(_seed())


def _seed_one_node_per_record_label(marker: str) -> None:
    """One throwaway node per RECORD_LABELS, so "the graph is now free of
    RECORD_LABELS residue" cannot be satisfied by a graph that already was."""
    for label in RECORD_LABELS:
        _create_marker_node(label, marker)


def _count_marker_nodes(label: str, marker: str) -> int:
    from tests._graph import count

    return count(
        f"MATCH (n:{label} {{marker: $marker}}) RETURN count(n) AS c", marker=marker
    )


def _delete_marker_nodes(marker: str) -> None:
    """Best-effort cleanup for whatever a test seeded and a not-yet-wired wipe
    left behind. A no-op once the real wipe already deleted them."""
    import asyncio

    from tests._graph import connection

    async def _delete() -> None:
        db = connection()
        try:
            async with db._driver.session(database=db._database) as s:
                await (
                    await s.run(
                        "MATCH (n {marker: $marker}) DETACH DELETE n", marker=marker
                    )
                ).consume()
        finally:
            await db.close()

    asyncio.run(_delete())


def _record_label_total() -> int:
    """Total node count across every RECORD_LABELS label, unfiltered by marker.

    Deliberately independent of `label_census` (the function under test in
    TestLabelCensus): using the SUT as its own oracle would let a bug shared
    by both sides cancel out and read green.
    """
    from tests._graph import count

    return count(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) RETURN count(n) AS c",
        labels=sorted(RECORD_LABELS),
    )


@pytest.fixture(scope="module", autouse=True)
def _require_disposable_graph():
    """Refuse production outright; skip only when the disposable instance is
    unreachable, matching the "unreachable is the one legitimate skip" contract
    tests/_corpus.py::classify_corpus_state already states for this suite."""
    from tests._corpus import neo4j_reachable
    from tests._graph import targets_production

    if targets_production():
        pytest.fail(
            "SAFETY: the resolved graph URI targets the production instance. "
            "Refusing to run whole-graph-wipe tests against it; check "
            "WRIT_NEO4J_URI / WRIT_SUITE_IS_ISOLATED."
        )
    if not neo4j_reachable():
        pytest.skip("disposable Neo4j instance (bolt://localhost:7688) unreachable")


@pytest.fixture(scope="module")
def _preflight_run(_require_disposable_graph):
    """Seed one node per RECORD_LABELS, run the real (possibly still
    unfixed) `_preflight_isolated_graph` once, and hand every test in
    TestPreflightWipesResidueAndRewarms the state captured immediately
    afterward.

    Module-scoped and shared: the SUT does a real whole-graph delete plus a
    corpus rebuild, so running it once for six assertions is the difference
    between one preflight and six.

    THE ORDER BELOW IS LOAD-BEARING. The post-preflight census and
    completeness check are taken and stored BEFORE this fixture's own
    cleanup runs. A `finally: _delete_marker_nodes(marker)` placed ahead of
    that capture would delete this fixture's OWN seeded nodes regardless of
    whether the wipe under test ever ran, and every test below would then
    read a graph this fixture had already cleaned itself, passing whether
    or not `_preflight_isolated_graph` does the work. Capturing first and
    cleaning up after is what keeps that from being possible.
    """
    marker = f"idempotence-{uuid.uuid4().hex}"
    _seed_one_node_per_record_label(marker)

    import tests.conftest as conftest_mod

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            conftest_mod._preflight_isolated_graph()

        from tests._corpus import is_complete

        record_total_after = _record_label_total()
        complete_after = is_complete()
    finally:
        # Safety net, deliberately reached only AFTER the capture above:
        # whether or not the wipe under test ran, and even if the preflight
        # raised, this fixture's own seeded nodes must not become permanent
        # residue on the shared instance.
        _delete_marker_nodes(marker)

    return {
        "marker": marker,
        "stdout": buf.getvalue(),
        "record_total_after": record_total_after,
        "complete_after": complete_after,
    }


class TestPreflightWipesResidueAndRewarms:
    """capabilities.md: "leaves zero Memory, Decision, FileChange, Commit and
    Project nodes", "the corpus is complete...", and the report-line item."""

    def test_leaves_zero_record_label_nodes(self, _preflight_run) -> None:
        assert _preflight_run["record_total_after"] == 0, (
            "RECORD_LABELS residue must be zero immediately after the "
            "preflight returns; a seeded Memory/Decision/FileChange/Commit/"
            "Project node survived, so the wipe did not run or did not "
            "reach every label"
        )

    def test_leaves_the_corpus_complete(self, _preflight_run) -> None:
        assert _preflight_run["complete_after"], (
            "the preflight must rebuild the corpus after wiping, so a run "
            "never starts against an empty graph: 'wiped everything and "
            "rebuilt nothing' must not be able to pass the test above"
        )

    def test_emits_exactly_one_report_line(self, _preflight_run) -> None:
        lines = [ln for ln in _preflight_run["stdout"].splitlines() if "wiped" in ln]
        assert len(lines) == 1, (
            f"expected exactly one report line naming the wiped count; found "
            f"{len(lines)} in stdout: {_preflight_run['stdout']!r}"
        )

    def test_report_line_names_a_nonzero_wiped_count(self, _preflight_run) -> None:
        match = re.search(r"wiped\s+(\d+)\s+nodes?", _preflight_run["stdout"])
        assert match, (
            f"report line must name the number of nodes wiped; stdout was: "
            f"{_preflight_run['stdout']!r}"
        )
        assert int(match.group(1)) > 0, (
            "the wiped count must be positive: this run seeded five marker "
            "nodes, so a zero count is a wipe that did not do the work, not "
            "a clean graph"
        )

    def test_report_line_names_the_rebuild_seconds(self, _preflight_run) -> None:
        assert re.search(r"rebuilt in\s+[\d.]+s", _preflight_run["stdout"]), (
            f"report line must name the corpus rebuild time in seconds; "
            f"stdout was: {_preflight_run['stdout']!r}"
        )

    def test_report_line_names_the_per_label_census(self, _preflight_run) -> None:
        stdout = _preflight_run["stdout"]
        assert any(label in stdout for label in RECORD_LABELS), (
            "report line must name at least one RECORD_LABELS entry from the "
            f"census of what was deleted; stdout was: {stdout!r}"
        )


class TestWipeEverythingRoutesThroughClearAll:
    """capabilities.md: "the wipe routes through clear_all(preserve_labels=
    frozenset())..." and the everything-vs-records-only distinction."""

    def test_refuses_without_the_disposable_marker(self, monkeypatch) -> None:
        import tests._graph as graph_mod

        wipe_everything = _require_seam(graph_mod, "wipe_everything")
        from writ.graph.db._safety import FullWipeRefused

        monkeypatch.delenv("WRIT_TEST_GRAPH", raising=False)
        marker = f"idem-refuse-{uuid.uuid4().hex}"
        _seed_one_node_per_record_label(marker)
        try:
            with pytest.raises(FullWipeRefused):
                wipe_everything()
            remaining = sum(
                _count_marker_nodes(label, marker) for label in RECORD_LABELS
            )
            assert remaining == len(RECORD_LABELS), (
                "a refused wipe must delete nothing; every seeded marker node "
                f"should still be present, found {remaining} of "
                f"{len(RECORD_LABELS)}"
            )
        finally:
            _delete_marker_nodes(marker)

    def test_deletes_seeded_record_labels_when_permitted(self) -> None:
        import tests._graph as graph_mod

        wipe_everything = _require_seam(graph_mod, "wipe_everything")
        marker = f"idem-wipe-{uuid.uuid4().hex}"
        _seed_one_node_per_record_label(marker)
        try:
            wipe_everything()
            assert _record_label_total() == 0, (
                "wipe_everything() must leave zero RECORD_LABELS nodes when "
                "permitted to run"
            )
        finally:
            _delete_marker_nodes(marker)
            from tests._corpus import ensure_corpus

            ensure_corpus()

    def test_deletes_a_non_record_label_too(self) -> None:
        """Distinguishes wipe_everything from the pre-existing wipe_corpus(),
        which spares RECORD_LABELS via clear_all's default preserve set.
        wipe_everything must pass preserve_labels=frozenset(), a genuine
        everything-wipe, so a throwaway node OUTSIDE RECORD_LABELS must be
        gone afterwards too."""
        import tests._graph as graph_mod

        wipe_everything = _require_seam(graph_mod, "wipe_everything")
        marker = f"idem-nonrecord-{uuid.uuid4().hex}"
        _create_marker_node("IdempotenceProbe", marker)
        try:
            wipe_everything()
            assert _count_marker_nodes("IdempotenceProbe", marker) == 0, (
                "wipe_everything must delete labels outside RECORD_LABELS "
                "too, or it is indistinguishable from the record-preserving "
                "wipe_corpus()"
            )
        finally:
            _delete_marker_nodes(marker)
            from tests._corpus import ensure_corpus

            ensure_corpus()


class TestLabelCensus:
    """capabilities.md: the report line's census half needs an unfiltered
    label count; RECORD_LABELS is outside tests._corpus's methodology-only
    label list, so methodology_counts() cannot serve as the census."""

    def test_counts_seeded_record_labels(self) -> None:
        import tests._graph as graph_mod

        label_census = _require_seam(graph_mod, "label_census")
        marker = f"idem-census-{uuid.uuid4().hex}"
        _seed_one_node_per_record_label(marker)
        try:
            census = label_census()
            assert isinstance(census, dict)
            for label in RECORD_LABELS:
                assert census.get(label, 0) >= 1, (
                    f"label_census() must count {label}: it is the "
                    "unfiltered census the wipe report depends on, not the "
                    "methodology-only tests._corpus.methodology_counts scan"
                )
        finally:
            _delete_marker_nodes(marker)


class TestPreflightRefusesBeforeWiping:
    """capabilities.md: "the wipe is issued only after the isolation
    classifier returns the isolated state, so a production or unreachable
    target is refused with zero delete statements sent"."""

    def test_production_target_is_refused_before_any_wipe_is_issued(
        self, monkeypatch
    ) -> None:
        import tests._graph as graph_mod
        import tests.conftest as conftest_mod

        _require_seam(graph_mod, "wipe_everything")
        calls: list[int] = []
        monkeypatch.setattr(graph_mod, "wipe_everything", lambda: calls.append(1))
        monkeypatch.setattr(graph_mod, "targets_production", lambda uri=None: True)

        with pytest.raises(pytest.UsageError):
            conftest_mod._preflight_isolated_graph()

        assert calls == [], (
            "a target classified as production must be refused before the "
            "wipe helper is ever called, and reachability is not even probed "
            "for a production target, so zero delete statements can be sent"
        )

    def test_unreachable_target_is_refused_before_any_wipe_is_issued(
        self, monkeypatch
    ) -> None:
        import tests._corpus as corpus_mod
        import tests._graph as graph_mod
        import tests.conftest as conftest_mod

        _require_seam(graph_mod, "wipe_everything")
        calls: list[int] = []
        monkeypatch.setattr(graph_mod, "wipe_everything", lambda: calls.append(1))
        monkeypatch.setattr(corpus_mod, "neo4j_reachable", lambda: False)

        with pytest.raises(pytest.UsageError):
            conftest_mod._preflight_isolated_graph()

        assert calls == [], (
            "an instance classified unreachable must be refused before the "
            "wipe helper is ever called"
        )


class TestRefusalReachesTheOperatorAsUsageError:
    """capabilities.md: "a refused wipe reaches the operator as a
    pytest.UsageError carrying the isolation remedy text, not as an
    INTERNALERROR traceback".

    Per ENF-SYS-005, this is the one behavior in this file proven with a
    mock: FullWipeRefused needs a production instance to raise for real, and
    this file must never connect to one (see module docstring). Every other
    test here proves idempotence against the real disposable instance.
    """

    def test_full_wipe_refused_becomes_a_usage_error_naming_the_remedy(
        self, monkeypatch
    ) -> None:
        import tests._graph as graph_mod
        import tests.conftest as conftest_mod
        from writ.graph.db._safety import FullWipeRefused

        _require_seam(graph_mod, "wipe_everything")

        def _raise_refused():
            raise FullWipeRefused("mocked refusal: no disposable marker")

        monkeypatch.setattr(graph_mod, "wipe_everything", _raise_refused)

        with pytest.raises(pytest.UsageError) as excinfo:
            conftest_mod._preflight_isolated_graph()

        message = str(excinfo.value)
        assert "make test-graph-up" in message or "WRIT_TEST_NO_ISOLATION" in message, (
            "a refused wipe must reach the operator carrying the isolation "
            f"remedy text (isolation_refusal_message), not a bare exception: "
            f"{message!r}"
        )
