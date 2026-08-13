"""Cypher-script graph dump: pure rendering tests plus one live round-trip.

Requires Neo4j running for TestCypherDumpRoundTrip only; the literal/render
tests below it are pure functions and need no database.
"""

from __future__ import annotations

import re

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


@pytest.fixture(scope="module", autouse=True)
def _restore_corpus_after_module():
    """Leave the graph complete for whatever module collects next (cycle 8).

    Two `db` fixtures in this file (TestCypherDumpRoundTrip, and
    TestRecordPreservationOnReplay below it) call
    `clear_all(preserve_labels=frozenset())` -- the explicit EVERYTHING-wipe -- both
    before and after every test they serve, so the last thing they do is leave the
    graph empty.

    That never mattered before, because it never ran. `disposable_graph` skips unless
    `full_wipe_allowed` holds, and against the production instance it cannot: the
    marker records intent while the instance comparison is env-blind. Isolation makes
    both halves true, so those two classes RUN for the first time and their empty
    graph becomes the starting state for everything collected after this module.

    ensure_corpus() is a no-op (one census read) when the graph is already complete,
    returns immediately when Neo4j is unreachable, and replays writ-corpus.cypher
    only when it has to -- so the pure rendering classes above, which need no
    database at all, pay one reachability probe at module teardown and nothing more.
    Module scope rather than function scope on purpose: the cost is bounded by module
    count, not by test count.
    """
    yield

    from tests._corpus import ensure_corpus

    ensure_corpus()


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
    async def db(self, disposable_graph):
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        # Explicit full wipe: clear_all preserves runtime records by default, so a
        # fixture that asserts on record COUNTS must opt into wiping them or the
        # previous test's records leak into this one's arithmetic.
        #
        # That wipe is unrecoverable for runtime records, so `disposable_graph`
        # skips unless a separate, explicitly-marked Neo4j instance is configured
        # (see tests/conftest.py). The pure-rendering classes above need no database
        # and keep running unconditionally.
        await conn.clear_all(preserve_labels=frozenset())
        yield conn
        await conn.clear_all(preserve_labels=frozenset())
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
    async def db(self, disposable_graph):
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        # Explicit full wipe: clear_all preserves runtime records by default, so a
        # fixture that asserts on record COUNTS must opt into wiping them or the
        # previous test's records leak into this one's arithmetic.
        #
        # `disposable_graph` skips unless a separate, explicitly-marked Neo4j
        # instance is configured (see tests/conftest.py). Note the irony this
        # encodes: these tests EXIST to prove records survive a corpus replay, and
        # the setup they used to prove it with is what destroyed the real ones.
        await conn.clear_all(preserve_labels=frozenset())
        yield conn
        await conn.clear_all(preserve_labels=frozenset())
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
    async def test_dump_enumeration_excludes_runtime_records(self, db) -> None:
        # writ-corpus.cypher is shipped and git-tracked; mirrored memories are
        # private operational state. They also carry no NODE_ID_FIELDS key, so a
        # leaked record would arrive with a null id and break the sorted render.
        await db.create_rule({"rule_id": "R-DUMP-SCOPE-1", "statement": "s"})
        await db.create_memory(
            name="must-not-be-dumped", project="-test-dump-records", description="d",
            type="project", body="secret body", links=[], path="/tmp/x.md",
            session_id="s", updated_at="2026-08-06T00:00:00Z", status="live")

        nodes = await db.get_all_nodes_for_dump()
        labels = {n["label"] for n in nodes}
        assert "Memory" not in labels, f"the dump must exclude records; got {labels}"
        assert not [n for n in nodes if n["id"] is None], "no null-id rows may reach the render"
        script = render_cypher_dump(nodes, await db.get_all_edges_cross_type())
        assert "must-not-be-dumped" not in script and "secret body" not in script

    @pytest.mark.asyncio
    async def test_clear_all_preserves_records_by_default(self, db) -> None:
        # The ~100 bare clear_all() call sites are test fixtures and admin corpus
        # rebuilds; none of them means "destroy operational records", so the safe
        # posture is the default rather than per-caller opt-in.
        await db.create_rule({"rule_id": "R-DEFAULT-1", "statement": "s"})
        await db.create_memory(
            name="survives-default-wipe", project="-test-dump-records", description="d",
            type="project", body="b", links=[], path="/tmp/x.md", session_id="s",
            updated_at="2026-08-06T00:00:00Z", status="live")
        await db.clear_all()
        assert await db.count_rules() == 0, "the corpus must still be wiped"
        assert await self._memory_count(db, "-test-dump-records") == 1, (
            "a bare clear_all() must not destroy runtime records"
        )

    @pytest.mark.asyncio
    async def test_clear_all_empty_preserve_set_wipes_everything(self, db) -> None:
        await db.create_memory(
            name="explicit-full-wipe", project="-test-dump-records", description="d",
            type="project", body="b", links=[], path="/tmp/x.md", session_id="s",
            updated_at="2026-08-06T00:00:00Z", status="live")
        await db.clear_all(preserve_labels=frozenset())
        assert await self._memory_count(db, "-test-dump-records") == 0, (
            "an explicit empty preserve set must still wipe everything"
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


class TestNoRawWholeGraphDeletes:
    """A raw whole-graph delete bypasses clear_all's record-preserving default.

    One did (a cypher-shell `MATCH (n) DETACH DELETE n` in a test helper), and it
    destroyed every mirrored memory on each suite run. Records have no dump home,
    so the corpus wipe must always route through the guard or spell out the same
    label exemption.
    """

    # Files where the unscoped delete is legitimate, each mapped to a SENTINEL
    # string that must still appear in it. The exemption is scoped to what the
    # file currently IS, not to its name: if the file stops being the thing that
    # earned the exemption, test_every_exemption_still_earns_itself goes red and
    # the exemption has to be re-justified. An allowlist keyed on filename alone
    # is how a real violation gets grandfathered in under a name that once meant
    # something else.
    _ALLOWED = {
        # clear_all itself: the guard's implementation, where the raw form belongs.
        "writ/graph/db/maintenance_store.py": "assert_full_wipe_allowed",
        # The wipe guard's own tests. The statement is the EXPECTED VALUE of an
        # assertion about what clear_all produced -- and in the refusal cases,
        # proof that it was never produced. It is asserted against, never
        # executed: those tests drive clear_all through a fake driver that
        # records statements rather than a Neo4j session.
        "tests/test_graph_wipe_guard.py": "FullWipeRefused",
    }

    # A QUERY string starts with the MATCH clause; prose that merely mentions the
    # statement (a docstring, an assertion message) does not. Keying on that keeps
    # the guard from policing documentation, and the unscoped shape below excludes
    # every legitimate id-scoped delete (`MATCH (n) WHERE n.rule_id IN $ids ...`).
    _UNSCOPED = re.compile(r"^MATCH \(n\)\s+DETACH DELETE n", re.IGNORECASE)

    def _query_strings(self, path):
        """Every non-docstring string constant in a module, f-strings included."""
        import ast

        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            return []
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                found.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                # An f-string's leading literal carries the clause we key on.
                parts = [v.value for v in node.values
                         if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                if parts:
                    found.append("".join(parts))
        return found

    def test_no_unguarded_whole_graph_delete_outside_clear_all(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in list(root.glob("tests/**/*.py")) + list(root.glob("writ/**/*.py")) \
                + list(root.glob("benchmarks/**/*.py")) + list(root.glob("scripts/**/*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in self._ALLOWED:
                continue
            for text in self._query_strings(path):
                if self._UNSCOPED.match(text.strip()):
                    offenders.append(f"{rel}: {text.strip()[:60]}")
        assert not offenders, (
            "unscoped whole-graph deletes bypass clear_all's record preservation; "
            f"route them through clear_all() or spell out the label exemption: {offenders}"
        )

    def test_every_exemption_still_earns_itself(self) -> None:
        """An exemption expires when the file stops being what justified it.

        Without this, `tests/test_graph_wipe_guard.py` would be permanently
        licensed to contain raw whole-graph deletes -- including after someone
        repurposes or rewrites it into something that is no longer the wipe
        guard's test.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        stale = []
        for rel, sentinel in self._ALLOWED.items():
            path = root / rel
            if not path.exists():
                stale.append(f"{rel}: exempted file no longer exists")
            elif sentinel not in path.read_text(encoding="utf-8"):
                stale.append(f"{rel}: no longer contains {sentinel!r}")
        assert not stale, (
            "these raw-delete exemptions no longer describe the files they "
            f"exempt; re-justify or remove them: {stale}"
        )

    def test_the_guard_would_catch_a_planted_raw_delete(self, tmp_path) -> None:
        # Teeth, independent of the tree's current state: the unscoped form is
        # caught, while the exempted form and an id-scoped delete are not.
        planted = tmp_path / "planted.py"
        planted.write_text('async def f(s):\n    await s.run("MATCH (n) DETACH DELETE n")\n')
        assert [t for t in self._query_strings(planted) if self._UNSCOPED.match(t.strip())]

        clean = tmp_path / "clean.py"
        clean.write_text(
            'async def f(s):\n'
            '    await s.run("MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN $p) '
            'DETACH DELETE n")\n'
            '    await s.run("MATCH (n) WHERE n.rule_id IN $ids DETACH DELETE n")\n'
        )
        assert not [t for t in self._query_strings(clean) if self._UNSCOPED.match(t.strip())]


class TestNoSecondGraphTransport:
    """A test reaches Neo4j through Neo4jConnection, never through a container.

    The 2026-08-08 leak: two modules ran `docker exec writ-neo4j cypher-shell`, and
    `writ-neo4j` is the PRODUCTION container. Every driver connection honoured
    WRIT_NEO4J_URI=bolt://localhost:7688 while those two files DETACH DELETEd the
    corpus on 7687. The connection-level tripwire could not see it, because a
    subprocess never touches Neo4jConnection.__init__.

    This is the regression pin, not the fix. The fix is that the second transport is
    gone; this fails the build when one comes back.
    """

    _FORBIDDEN_HEADS = ("docker", "docker-compose", "cypher-shell", "neo4j-admin")

    # Ships EMPTY, deliberately. After this cycle no test file has a list literal
    # whose FIRST element is one of the heads above: the tool-prerequisite tests
    # pass ["bash", "docker", "git"] as a PATH allowlist, where "docker" is never
    # the head. Inventing an exemption the tree does not need would license exactly
    # the shape being forbidden. Same reasoning, same shape, as _ALLOWED above:
    # an entry must name a file AND a sentinel that keeps earning it.
    _ALLOWED: dict[str, str] = {}

    def _forbidden_head_lists(self, path):
        """Every list literal in a module whose FIRST element is a forbidden head.

        Keys on the head -- index zero -- never on the substring appearing
        anywhere in the list, so a tool-name allowlist like
        ["bash", "docker", "git"] is not caught: "docker" sits at index 1,
        never index 0.
        """
        import ast

        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            return []
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and node.elts:
                head = node.elts[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str) \
                        and head.value in self._FORBIDDEN_HEADS:
                    found.append((node.lineno, head.value))
        return found

    def test_no_second_transport_argv_in_tests(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("tests/**/*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in self._ALLOWED:
                continue
            for lineno, head in self._forbidden_head_lists(path):
                offenders.append(f"{rel}:{lineno}: argv headed by {head!r}")
        assert not offenders, (
            "a test reaches Neo4j through a second transport instead of "
            "Neo4jConnection; route it through the driver or spell out the "
            f"exemption: {offenders}"
        )

    def test_allowed_is_empty(self) -> None:
        assert self._ALLOWED == {}, (
            "after cycle 8 no test file has an argv list headed by a forbidden "
            "transport; an exemption the tree does not need would license "
            f"exactly the shape being forbidden: {self._ALLOWED}"
        )

    def test_every_exemption_still_earns_itself(self) -> None:
        """An exemption expires when the file stops being what justified it.

        _ALLOWED ships empty, so this is vacuously true today. It exists so that
        if an entry is ever added, it must name a file that still exists AND a
        sentinel string that still appears in it -- the same discipline
        TestNoRawWholeGraphDeletes applies to its own exemptions.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        stale = []
        for rel, sentinel in self._ALLOWED.items():
            path = root / rel
            if not path.exists():
                stale.append(f"{rel}: exempted file no longer exists")
            elif sentinel not in path.read_text(encoding="utf-8"):
                stale.append(f"{rel}: no longer contains {sentinel!r}")
        assert not stale, (
            "these second-transport exemptions no longer describe the files "
            f"they exempt; re-justify or remove them: {stale}"
        )

    def test_the_guard_would_catch_a_planted_second_transport(self, tmp_path) -> None:
        # Teeth, independent of the tree's current state: an argv list HEADED
        # by a forbidden transport is caught, while a tool-name allowlist
        # carrying the same string at a later position is not.
        planted = tmp_path / "planted.py"
        planted.write_text(
            'import subprocess\n'
            'def f():\n'
            '    subprocess.run(["docker", "exec", "writ-neo4j", "cypher-shell"])\n'
        )
        assert self._forbidden_head_lists(planted)

        clean = tmp_path / "clean.py"
        clean.write_text(
            'import subprocess\n'
            'def f():\n'
            '    subprocess.run(["bash", "docker", "git"])\n'
        )
        assert not self._forbidden_head_lists(clean)

    def test_no_false_positive_on_real_tool_allowlists(self) -> None:
        """`["bash", "docker", "git"]` is a list of tool NAMES, not an invocation.

        tests/test_bootstrap.py:246 and about six lines in
        tests/test_no_tool_prereqs.py pass this shape as a PATH allowlist for the
        preflight check; "docker" is never the head of that list, so the guard
        must not fire on it. This is the single most likely way the guard gets
        written wrong (keying on the substring "docker" appearing anywhere) and
        then deleted by an annoyed contributor.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for rel in ("tests/test_bootstrap.py", "tests/test_no_tool_prereqs.py"):
            path = root / rel
            offenders = self._forbidden_head_lists(path)
            assert not offenders, (
                f"{rel} is a tool-name allowlist, not a transport invocation, "
                f"and must not trip the guard: {offenders}"
            )


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

    def test_it_still_refuses_when_an_optional_dependency_is_missing(self, tmp_path) -> None:
        """The refusal must not depend on the module's own imports succeeding.

        The guard used to sit at the BOTTOM of scale_benchmark.py, below module-scope
        imports of numpy, sentence_transformers and writ.config. On an interpreter
        without the optional embedding extra the script died with ModuleNotFoundError
        before ever reaching the guard: no refusal, no mention of --run, just a
        traceback. Nothing was wiped, but only because a crashed import cannot wipe
        anything either, and a destructive-operation guard should not rest on that.

        THE TEST ABOVE CANNOT CATCH THIS, and did not: it passes on any machine where
        the extra happens to be installed, which is every developer machine here. CI
        installs `.[dev]`, which does not pull the fallback extra, so the defect only
        ever appeared there. This one shadows the module with one that raises on
        import, so the failing condition is reproduced anywhere the suite runs.
        """
        import os
        import subprocess
        import sys
        from pathlib import Path

        shadow = tmp_path / "sentence_transformers"
        shadow.mkdir()
        (shadow / "__init__.py").write_text(
            'raise ImportError("simulated: optional extra not installed")\n'
        )
        repo = Path(__file__).resolve().parent.parent
        env = {**os.environ, "WRIT_NO_AUTOSTART": "1", "PYTHONPATH": str(tmp_path)}

        blocked = subprocess.run([sys.executable, "-c", "import sentence_transformers"],
                                 capture_output=True, text=True, env=env, timeout=60)
        assert "ImportError" in blocked.stderr, (
            "the shadow did not take effect, so this test proves nothing"
        )

        r = subprocess.run(
            [sys.executable, str(repo / "benchmarks" / "scale_benchmark.py")],
            capture_output=True, text=True, timeout=300, cwd=str(repo), env=env,
        )
        out = r.stdout + r.stderr
        assert r.returncode != 0, "a bare invocation must refuse"
        assert "--run" in out, f"the refusal must name the flag; got: {out[:300]!r}"
        assert "Traceback" not in out, (
            f"the guard was reached only after an import blew up: {out[:300]!r}"
        )
