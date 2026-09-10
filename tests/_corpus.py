"""INC-1: deterministic-corpus helpers for the test suite.

Centralizes "is the live Neo4j graph complete?" so graph-dependent tests can self-heal a
wiped graph (re-import bible/) instead of silently skipping it -- the masking class that let
a real FIX-5 failure read as a skip in full-suite runs.

`classify_corpus_state` is pure (no I/O) and is the anti-masking contract: a reachable but
empty/partial graph is 'empty' (tests must FAIL), never 'unreachable' (the only skip).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import subprocess
from pathlib import Path

from tests._writ_cmd import WRIT_CMD_PREFIX

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Complete-corpus expectations; kept in step with the bible/ corpus.
EXPECTED = {"SubagentRole": 5, "Playbook": 15, "Skill": 13, "Phase": 20}
MIN_RULES = 280

_LABELS = [
    "Rule", "Skill", "Playbook", "AntiPattern", "Phase", "SubagentRole",
    "Technique", "ForbiddenResponse", "PressureScenario", "Rationalization",
    "WorkedExample",
]


def classify_corpus_state(
    reachable: bool,
    rule_count: int,
    subagent_count: int,
    *,
    min_rules: int = MIN_RULES,
    expect_subagent: int = EXPECTED["SubagentRole"],
) -> str:
    """Return 'unreachable' | 'empty' | 'ready'.

    'unreachable' (Neo4j down) is the ONLY legitimate skip for a graph-dependent test.
    A reachable graph that is empty or partial is 'empty' -- tests must FAIL, because that
    is exactly the state that previously masked a real regression as a skip.
    """
    if not reachable:
        return "unreachable"
    if rule_count >= min_rules and subagent_count >= expect_subagent:
        return "ready"
    return "empty"


def _connection():
    """Delegates to tests/_graph.py so ONE factory serves the whole suite (cycle 8).

    It used to build its own Neo4jConnection here, which was harmless in itself
    (it resolved through writ.config, same as _graph.connection does) but meant
    the suite had more than one place that decided where the graph is. That is
    the exact shape of the 2026-08-08 leak, where a second target resolution
    (a container NAME) sent DETACH DELETEs to production while every driver
    connection honoured the isolated URI. One factory, one answer.
    """
    from tests._graph import connection

    return connection()


def _run_coro(factory):
    """Run an async factory to completion from sync code, loop running or not.

    `asyncio.run` RAISES RuntimeError when a loop already owns this thread, and
    the coroutine it was handed is then dropped unawaited. The three callers
    below each ended in a bare `asyncio.run(_q())`, and `neo4j_reachable`
    additionally wrapped it in `except Exception: return False` -- so a probe
    called from inside a loop reported the graph UNREACHABLE rather than
    admitting it never asked. Measured against one reachable graph: True from
    outside a loop, False from inside it.

    That is not a cosmetic difference. `classify_corpus_state` above makes
    'unreachable' the ONE state that may skip, precisely so an empty graph
    cannot mask a regression, so a dispatch failure returning False hands every
    caller the single answer licensed to skip.

    When a loop is running, borrow a thread that has none. `pytest_sessionstart`
    already does exactly this for the same reason, so this is the suite's
    existing idiom rather than a new one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(factory())).result()


def neo4j_reachable() -> bool:
    """True if the Neo4j graph answers a trivial query.

    A False here means the QUERY failed, which is what callers read it as. It no
    longer also means "this was called from a running loop": see _run_coro.
    """
    async def _q() -> bool:
        db = _connection()
        try:
            async with db._driver.session(database=db._database) as s:
                res = await s.run("RETURN 1 AS ok")
                await res.consume()
            return True
        finally:
            await db.close()

    try:
        return _run_coro(_q)
    except Exception:  # noqa: BLE001
        return False


def methodology_counts() -> dict[str, int]:
    """Live node counts by label for the methodology + rule node types.

    One scan, one round-trip (`UNWIND labels(n)`) instead of a per-label query loop -- this is on
    the corpus_ready path of every graph-dependent test (POL-2b/E5).
    """
    async def _q() -> dict[str, int]:
        db = _connection()
        out: dict[str, int] = {lbl: 0 for lbl in _LABELS}
        try:
            async with db._driver.session(database=db._database) as s:
                res = await s.run(
                    "MATCH (n) UNWIND labels(n) AS lbl RETURN lbl AS label, count(*) AS c"
                )
                async for rec in res:
                    label = rec["label"]
                    if label in out:
                        out[label] = rec["c"]
            return out
        finally:
            await db.close()

    return _run_coro(_q)


def clear_label(label: str) -> int:
    """DETACH DELETE all nodes of `label`; return how many existed. Test-support only."""
    async def _q() -> int:
        db = _connection()
        try:
            async with db._driver.session(database=db._database) as s:
                res = await s.run(f"MATCH (n:{label}) RETURN count(n) AS c")
                c = [r async for r in res][0]["c"]
                await (await s.run(f"MATCH (n:{label}) DETACH DELETE n")).consume()
                return c
        finally:
            await db.close()

    return _run_coro(_q)


def is_complete(counts: dict[str, int] | None = None) -> bool:
    """True if the live graph has the full corpus (all methodology types at expected counts)."""
    c = counts if counts is not None else methodology_counts()
    return c.get("Rule", 0) >= MIN_RULES and all(c.get(k, 0) >= v for k, v in EXPECTED.items())


def graph_is_warm() -> bool:
    """True when the live graph is reachable AND already complete (POL-2b/E3).

    The warmth predicate pytest_sessionstart uses to skip a redundant import-markdown: if the
    graph is already complete the MERGE-only import is a no-op, so the INC-1 complete-start
    guarantee still holds. Any Neo4j error -> not warm -> the caller imports as usual.
    """
    try:
        return neo4j_reachable() and is_complete()
    except Exception:  # noqa: BLE001
        return False


def require_population(count_query: str, subject: str) -> int:
    """The ONE synchronous "count, repair, fail loud with the number it saw"
    precondition for a graph population a test selects from. Returns the count.

    Callers are fixtures that START A DAEMON, and the ORDER is the reason this is
    synchronous and separate from `ensure_corpus`: the daemon builds its indexes
    AT STARTUP, so this has to run and finish BEFORE the start, and a repair
    afterwards is invisible to the process that needed it. That also rules out
    reusing `tests/fixtures/server_routes.py::require_injection_population`,
    which asserts nearly the right thing but is async and does not repair.

    THE REPAIR IS DELEGATED, NOT REIMPLEMENTED: `ensure_corpus` above is the one
    owner of "refill a wiped graph" (conftest and a dozen modules call it), and
    it re-imports `bible/` FIRST, the source of truth and MERGE-only, falling
    back to the tracked dump only where `bible/` is absent, as it is on the
    disposable test instance.

    THE LOUD HALF IS HERE, because `ensure_corpus` returns SILENTLY when it
    cannot heal and leaves the verdict to the caller. It FAILS rather than skips,
    which is this module's stated contract (`classify_corpus_state`: a reachable
    but empty graph is 'empty' and must fail; only unreachable may skip), and the
    count and the total travel in the message so a future empty graph reports a
    missing PRECONDITION rather than surfacing as an empty-list assertion three
    layers down.
    """
    import pytest

    from tests._graph import count

    present = count(count_query)
    if present:
        return present
    ensure_corpus()
    present = count(count_query)
    if not present:
        pytest.fail(
            f"corpus precondition unmet: the graph holds {present} node(s) in "
            f"{subject}, even after tests/_corpus.py::ensure_corpus, and "
            f"{count('MATCH (n) RETURN count(n)')} nodes in total. A daemon started "
            f"now would index an empty corpus, so every retrieval assertion behind "
            f"this precondition would fail as an empty RESULT instead of as a missing "
            f"PRECONDITION. Restore with tests/_corpus.py::ensure_corpus() or any "
            f"pytest session (its session-start preflight rebuilds). The query was: "
            f"{count_query}",
            pytrace=False,
        )
    return present


def ensure_corpus() -> None:
    """Self-heal: if the live graph is missing methodology nodes, refill it.

    bible/ first (it is the source of truth and MERGE-only, so re-importing it is
    idempotent), then the tracked writ-corpus.cypher dump. No-op when already
    complete or when Neo4j is unreachable (the caller decides whether to skip).

    Cycle 8 added the dump fallback, and it is what makes this function work at
    all on a clean checkout and on the disposable test instance, where bible/ does
    not exist: bible/ is gitignored, so before the fallback a wiped graph plus an
    absent bible/ made this return SILENTLY. That turns the anti-masking contract
    this module exists for into a scatter of failures with no cause attached --
    the caller believes it healed the graph, and every assertion downstream fails
    on an empty one.
    """
    if not neo4j_reachable():
        return
    if is_complete():
        return
    if (_REPO_ROOT / "bible").exists():
        subprocess.run(
            # --no-export: self-heal only needs the graph repopulated, not bible/ source
            # rewritten from the graph (B5: avoids the export round-trip on every heal).
            [*WRIT_CMD_PREFIX, "import-markdown", "bible/", "--no-export"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            timeout=120,
            check=False,
        )
        return
    from tests._graph import replay_dump

    replay_dump(_REPO_ROOT)
