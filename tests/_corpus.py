"""INC-1: deterministic-corpus helpers for the test suite.

Centralizes "is the live Neo4j graph complete?" so graph-dependent tests can self-heal a
wiped graph (re-import bible/) instead of silently skipping it -- the masking class that let
a real FIX-5 failure read as a skip in full-suite runs.

`classify_corpus_state` is pure (no I/O) and is the anti-masking contract: a reachable but
empty/partial graph is 'empty' (tests must FAIL), never 'unreachable' (the only skip).
"""
from __future__ import annotations

import asyncio
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


def neo4j_reachable() -> bool:
    """True if the Neo4j graph answers a trivial query."""
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
        return asyncio.run(_q())
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

    return asyncio.run(_q())


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

    return asyncio.run(_q())


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
