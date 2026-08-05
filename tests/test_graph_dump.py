"""Cypher-script graph dump: pure rendering tests plus one live round-trip.

Requires Neo4j running for TestCypherDumpRoundTrip only; the literal/render
tests below it are pure functions and need no database.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from typer.testing import CliRunner

from writ.cli import app
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.dump import cypher_literal, import_cypher_dump, render_cypher_dump

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

runner = CliRunner()


class TestCLIDefaultPaths:
    def test_export_cypher_help_shows_writ_corpus_cypher_default(self) -> None:
        result = runner.invoke(app, ["export-cypher", "--help"])
        assert "writ-corpus.cypher" in result.output

    def test_import_cypher_help_shows_writ_corpus_cypher_default(self) -> None:
        result = runner.invoke(app, ["import-cypher", "--help"])
        assert "writ-corpus.cypher" in result.output


class TestCypherLiteral:
    def test_renders_plain_string_single_quoted(self) -> None:
        assert cypher_literal("hello") == "'hello'"

    def test_escapes_single_quote(self) -> None:
        assert cypher_literal("O'Brien") == "'O\\'Brien'"

    def test_escapes_backslash_before_other_escapes(self) -> None:
        # a trailing literal backslash must not swallow the closing quote
        assert cypher_literal("a\\b") == "'a\\\\b'"

    def test_escapes_newline_and_tab(self) -> None:
        assert cypher_literal("line1\nline2\ttab") == "'line1\\nline2\\ttab'"

    def test_renders_int_without_quotes(self) -> None:
        assert cypher_literal(42) == "42"

    def test_renders_float_without_quotes(self) -> None:
        assert cypher_literal(3.5) == "3.5"

    def test_renders_bool_lowercase(self) -> None:
        assert cypher_literal(True) == "true"
        assert cypher_literal(False) == "false"

    def test_renders_list_of_strings(self) -> None:
        assert cypher_literal(["a", "b"]) == "['a', 'b']"


class TestRenderCypherDump:
    def _node(self, id_: str, label: str, **props: object) -> dict:
        return {"id": id_, "label": label, "props": props}

    def _edge(self, source_id: str, target_id: str, type_: str) -> dict:
        return {"source_id": source_id, "target_id": target_id, "type": type_}

    def test_single_node_renders_create_with_staging_property(self) -> None:
        nodes = [self._node("R-1", "Rule", rule_id="R-1", severity="high")]
        script = render_cypher_dump(nodes, [])
        assert "CREATE (:Rule {" in script
        assert "rule_id: 'R-1'" in script
        assert "severity: 'high'" in script
        assert "_dump_id: 'R-1'" in script

    def test_none_valued_property_is_omitted(self) -> None:
        nodes = [self._node("R-1", "Rule", rule_id="R-1", authority=None)]
        script = render_cypher_dump(nodes, [])
        assert "authority" not in script

    def test_single_edge_renders_match_by_staging_property_then_create(self) -> None:
        nodes = [
            self._node("R-1", "Rule", rule_id="R-1"),
            self._node("R-2", "Rule", rule_id="R-2"),
        ]
        edges = [self._edge("R-1", "R-2", "RELATED_TO")]
        script = render_cypher_dump(nodes, edges)
        assert (
            "MATCH (a {_dump_id: 'R-1'}), (b {_dump_id: 'R-2'}) "
            "CREATE (a)-[:RELATED_TO]->(b);" in script
        )

    def test_final_statement_removes_staging_property_exactly_once(self) -> None:
        nodes = [self._node("R-1", "Rule", rule_id="R-1")]
        script = render_cypher_dump(nodes, [])
        cleanup = "MATCH (n) WHERE n._dump_id IS NOT NULL REMOVE n._dump_id;"
        assert script.count(cleanup) == 1
        assert script.rstrip().endswith(cleanup)

    def test_output_is_deterministic_regardless_of_input_order(self) -> None:
        nodes_a = [self._node("R-2", "Rule", rule_id="R-2"), self._node("R-1", "Rule", rule_id="R-1")]
        nodes_b = list(reversed(nodes_a))
        assert render_cypher_dump(nodes_a, []) == render_cypher_dump(nodes_b, [])


class TestCypherDumpRoundTrip:
    """Requires Neo4j running."""

    @pytest_asyncio.fixture
    async def db(self):
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    @pytest.mark.asyncio
    async def test_dump_then_import_reproduces_node_and_edge_counts(self, db) -> None:
        await db.create_rule({"rule_id": "TEST-DUMP-001", "statement": "first"})
        await db.create_rule({"rule_id": "TEST-DUMP-002", "statement": "second"})
        await db.create_edge("RELATED_TO", "TEST-DUMP-001", "TEST-DUMP-002")

        nodes_before = await db.get_all_nodes_for_dump()
        edges_before = await db.get_all_edges_cross_type()

        script = render_cypher_dump(nodes_before, edges_before)
        await db.clear_all()
        await import_cypher_dump(db, script)

        nodes_after = await db.get_all_nodes_for_dump()
        edges_after = await db.get_all_edges_cross_type()
        assert len(nodes_after) == len(nodes_before)
        assert len(edges_after) == len(edges_before)

    @pytest.mark.asyncio
    async def test_imported_graph_has_no_staging_property_left(self, db) -> None:
        await db.create_rule({"rule_id": "TEST-DUMP-003", "statement": "third"})
        nodes_before = await db.get_all_nodes_for_dump()
        script = render_cypher_dump(nodes_before, [])
        await db.clear_all()
        await import_cypher_dump(db, script)

        nodes_after = await db.get_all_nodes_for_dump()
        assert all("_dump_id" not in n["props"] for n in nodes_after)


class TestRecordPreservationOnReplay:
    """Corpus replays must not destroy runtime records: a corpus dump is not
    the whole graph. Requires Neo4j running."""

    @pytest_asyncio.fixture
    async def db(self):
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    async def _memory_count(self, db, project: str) -> int:
        rows = await db._run(
            "MATCH (m:Memory {project: $p}) RETURN count(m) AS c", p=project)
        return [r["c"] for r in rows][0]

    @pytest.mark.asyncio
    async def test_corpus_only_replay_preserves_memory_records(self, db) -> None:
        await db.create_memory(
            name="survives-replay", project="-test-dump-records", description="d",
            type="project", body="b", links=[], path="/tmp/x.md", session_id="s",
            updated_at="2026-08-05T00:00:00Z", status="live")
        script = render_cypher_dump(
            [{"id": "R-CORPUS-1", "label": "Rule",
              "props": {"rule_id": "R-CORPUS-1", "statement": "s"}}], [])
        await import_cypher_dump(db, script)
        assert await self._memory_count(db, "-test-dump-records") == 1, (
            "a corpus-only replay deleted the Memory record; the wipe must "
            "preserve record labels absent from the incoming dump"
        )

    @pytest.mark.asyncio
    async def test_dump_carrying_memory_label_gets_exact_replace(self, db) -> None:
        await db.create_memory(
            name="pre-existing", project="-test-dump-records", description="old",
            type="project", body="b", links=[], path="/tmp/x.md", session_id="s",
            updated_at="2026-08-05T00:00:00Z", status="live")
        script = render_cypher_dump(
            [{"id": "from-dump", "label": "Memory",
              "props": {"name": "from-dump", "project": "-test-dump-records"}}], [])
        await import_cypher_dump(db, script)
        rows = await db._run(
            "MATCH (m:Memory {project: $p}) RETURN m.name AS name", p="-test-dump-records")
        names = sorted(r["name"] for r in rows)
        assert names == ["from-dump"], (
            "a dump that CARRIES the Memory label must get exact-replace "
            f"semantics for it, got {names!r}"
        )

    @pytest.mark.asyncio
    async def test_clear_all_preserve_labels_spares_only_named_labels(self, db) -> None:
        await db.create_rule({"rule_id": "R-WIPE-1", "statement": "s"})
        await db.create_memory(
            name="spared", project="-test-dump-records", description="d",
            type="project", body="b", links=[], path="/tmp/x.md", session_id="s",
            updated_at="2026-08-05T00:00:00Z", status="live")
        await db.clear_all(preserve_labels=frozenset({"Memory"}))
        assert await db.count_rules() == 0
        assert await self._memory_count(db, "-test-dump-records") == 1


class TestScaleBenchmarkRequiresExplicitRun:
    """The scale benchmark wipes the live graph; it must never start by accident.
    No Neo4j needed: argument handling fails before any connection."""

    def _invoke(self, *args: str):
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        return subprocess.run(
            [sys.executable, str(repo / "benchmarks" / "scale_benchmark.py"), *args],
            capture_output=True, text=True, timeout=300,
            cwd=str(repo), env={**os.environ, "WRIT_NO_AUTOSTART": "1"},
        )

    def test_no_args_refuses_and_names_the_flag(self) -> None:
        r = self._invoke()
        assert r.returncode != 0, "a bare invocation must refuse, not run the wipe"
        assert "--run" in (r.stdout + r.stderr)

    def test_help_exits_zero_without_running(self) -> None:
        r = self._invoke("--help")
        assert r.returncode == 0
        assert "DESTRUCTIVE" in (r.stdout + r.stderr)
        assert "snapshotted" not in (r.stdout + r.stderr).lower()
