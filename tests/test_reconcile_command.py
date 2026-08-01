"""Tests for the `writ reconcile` CLI command (ITEM 1).

RED today: the `reconcile` command does not exist in writ/cli.py.
The command should call the library `reconcile(path, db)` and print what it cleared.

Library signature (from writ/graph/methodology_ingest.py:505):
    async def reconcile(path: Path, db: Neo4jConnection, project: str = "writ") -> dict

Returns: {"deleted_nodes": [...], "deleted_edges": [(type, src, tgt), ...], "cleared_props": {...}}
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker
from writ.graph.methodology_ingest import compute_expected_graph, ingest_path

BIBLE = Path(__file__).resolve().parent.parent / "bible"

# Same real-node pair used in test_phase010_reconcile.py -- both exist in the
# corpus, and ("COUNTERS", REAL_A, REAL_B) is not in the oracle.
REAL_A = "SEC-INJ-SQL-001"
REAL_B = "DOC-ONBOARD-001"


def _rule(rid: str) -> dict:
    return {
        "rule_id": rid, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
    }


async def _edge_exists(db: Neo4jConnection, etype: str, src: str, tgt: str) -> bool:
    async with db._driver.session(database=db._database) as s:
        res = await s.run(
            f"MATCH (a)-[r:`{etype}`]->(b) "
            "WHERE (a.rule_id = $src OR a.category_id = $src) "
            "AND (b.rule_id = $tgt OR b.category_id = $tgt) RETURN count(r) AS c",
            src=src, tgt=tgt,
        )
        return (await res.single())["c"] > 0


@pytest_asyncio.fixture()
async def db_corpus():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    if not BIBLE.exists():
        await db.close()
        pytest.skip("requires the untracked bible/ source tree (regenerate with `writ export`)")
    await db.clear_all()
    await ingest_path(BIBLE, db)
    yield db
    # Leave the full corpus intact after each test.
    await db.close()


# ---------------------------------------------------------------------------
# ITEM 1a: the command is registered in the Typer app
# RED today: no `reconcile` command exists in cli.py.
# ---------------------------------------------------------------------------

class TestReconcileCommandRegistered:

    def test_reconcile_command_registered(self) -> None:
        """The Typer app exposes a 'reconcile' command.

        RED today: the command is absent from writ/cli.py.
        """
        from writ.cli import app
        names = [cmd.name for cmd in app.registered_commands]  # type: ignore[attr-defined]
        assert "reconcile" in names, (
            f"'reconcile' not found in registered commands: {names}. "
            "Add a @app.command(name='reconcile') in writ/cli.py."
        )

    def test_reconcile_help_exits_zero(self) -> None:
        """Running `writ reconcile --help` exits with code 0.

        RED today: the command does not exist, so --help returns a non-zero
        exit code (or the command is missing entirely from the help output).
        """
        from typer.testing import CliRunner
        from writ.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["reconcile", "--help"])
        assert result.exit_code == 0, (
            f"'writ reconcile --help' exited {result.exit_code}. "
            f"Output: {result.output}"
        )


# ---------------------------------------------------------------------------
# ITEM 1b: the command removes a seeded stale edge (live Neo4j)
# RED today: the command does not exist.
# ---------------------------------------------------------------------------

class TestReconcileCommandLive:

    @pytest.mark.asyncio
    async def test_reconcile_removes_stale_edge(
        self, db_corpus: Neo4jConnection, tmp_path: Path
    ) -> None:
        """Invoking the reconcile command removes a stale edge not in source.

        Precondition: ("COUNTERS", REAL_A, REAL_B) is absent from the bible oracle.
        The test seeds it, invokes the CLI's underlying reconcile logic (calls
        the library function directly as the command will), then asserts the
        stale edge is gone.

        RED today: the command does not exist; this test calls the would-be
        command function which will be imported from writ.cli once wired.
        """
        # Verify precondition: the edge is NOT in the expected oracle.
        _, oracle_edges = compute_expected_graph(BIBLE)
        assert ("COUNTERS", REAL_A, REAL_B) not in oracle_edges, (
            "Precondition failed: COUNTERS edge already in oracle -- choose different nodes"
        )

        # Seed the stale edge.
        await db_corpus.create_edge("COUNTERS", REAL_A, REAL_B)
        assert await _edge_exists(db_corpus, "COUNTERS", REAL_A, REAL_B), (
            "Setup failed: stale edge was not created"
        )

        # Invoke the ACTUAL CLI `reconcile` command (the wired command path, not
        # just the library). The command builds its own Neo4jConnection against the
        # same database and calls reconcile(BIBLE_DIR, db) internally. Run it in a
        # worker thread: the command uses asyncio.run, which cannot be called from
        # this test's already-running event loop.
        import asyncio as _asyncio

        from typer.testing import CliRunner

        from writ.cli import app

        runner = CliRunner()
        result = await _asyncio.to_thread(
            runner.invoke, app, ["reconcile", "--bible-dir", str(BIBLE)]
        )
        assert result.exit_code == 0, (
            f"`writ reconcile` exited {result.exit_code}. Output: {result.output}"
        )
        # The command's summary must report the stale edge it deleted.
        assert f"COUNTERS {REAL_A} -> {REAL_B}" in result.output, (
            f"reconcile command did not report deleting the seeded stale edge. "
            f"Output: {result.output}"
        )

        # And must be gone from the graph.
        assert not await _edge_exists(db_corpus, "COUNTERS", REAL_A, REAL_B), (
            "Stale edge still exists in graph after reconcile"
        )

        # The edge parity checker must now report None (clean).
        checker = IntegrityChecker(db_corpus._driver, db_corpus._database)
        assert await checker.detect_edge_parity(BIBLE) is None, (
            "Edge parity is not clean after reconcile"
        )
