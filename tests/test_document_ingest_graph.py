"""Program item 5 (workstream D): documents on the REAL graph (isolated instance, port 7688).

ENF-SYS-005, declared. These claims cannot be proven with mocks, so every test here runs
against the isolated Neo4j reached through tests/_graph.py, under the shared flock, and never
with WRIT_TEST_NO_ISOLATION:

  * replace_document is ONE atomic statement: the new chunk set exactly, n CONTAINS and n - 1
    PRECEDES edges in ordinal order, no orphan Chunk left behind, a zero-chunk document still
    writes (capability 5)
  * two concurrent ingests of one document leave one Document and one chunk set, and two
    projects with the same path keep separate documents (capability 6)
  * Document and Chunk are record labels: clear_all() and a corpus-only replay keep them, and
    the dump (nodes, edges, rendered script) never carries them (capability 7)
  * adjacency, the category routes read, the integrity checks and a `writ ingest` reconcile
    behave exactly as before ingestion (capability 8)
  * statement counts (capability 33): counted at the driver (every session.run and every
    managed-transaction run), so the helpers cannot hide a statement
  * the reload end to end, and the abstention calibration with the REAL encoder, which is the
    session fixture `methodology_model` shared with the rest of the suite: no second
    OnnxEmbeddingModel is ever constructed here (capabilities 34 and 35)

The rule half of a handle is faked in the reload tests (assemble_pipeline is not the subject and
would build a second index over the corpus); the document half is always REAL.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import writ.retrieval.pipeline as pipeline_mod
import writ.server as server
import writ.server.reload as reload_mod
import writ.server.routes.query as qroute
from tests._graph import connection

REPO = Path(__file__).resolve().parent.parent
PROJECT_A = "test-docs-a"
PROJECT_B = "test-docs-b"
PROJECT_CAL = "test-docs-cal"
ALL_PROJECTS = (PROJECT_A, PROJECT_B, PROJECT_CAL)
DEFAULT_KINDS = frozenset({"docs", "adr", "readme"})


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
async def _reachable_or_skip(conn) -> None:
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable; run "
                    "`bash scripts/test-graph.sh up`")


async def _purge(conn) -> None:
    for project in ALL_PROJECTS:
        await conn._run("MATCH (n) WHERE (n:Document OR n:Chunk) AND n.project = $p DETACH DELETE n",
                        p=project)
        await conn.clear_project(project)
    await conn._run("MATCH (p:Project) WHERE p.name IN $names DETACH DELETE p", names=list(ALL_PROJECTS))


@pytest_asyncio.fixture()
async def db():
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.apply_constraints()
    await _purge(conn)
    yield conn
    await _purge(conn)
    await conn.close()


@pytest_asyncio.fixture()
async def wipe_db(disposable_graph):
    conn = connection()
    await _reachable_or_skip(conn)
    await conn.apply_constraints()
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await _purge(conn)
    await conn.close()


@pytest.fixture(scope="module", autouse=True)
def _restore_corpus_after_module():
    """The wipe tests leave the shared graph empty; put the corpus back for later modules."""
    yield
    from tests._corpus import ensure_corpus

    ensure_corpus()


def _records(project: str, doc_id: str, n: int, *, kind: str = "docs", word: str = "alpha"):
    from writ.graph.schema import Chunk, Document

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = Document(doc_id=doc_id, project=project, path=doc_id, kind=kind, title=f"Title {doc_id}",
                        source_hash=f"hash-{word}-{n}", chunk_count=n, ingested_at=now)
    chunks = [Chunk(chunk_id=f"{project}:{doc_id}#{i:04d}", project=project, doc_id=doc_id, ordinal=i,
                    breadcrumb=f"Title > Part {i}", text=f"{word} body {i}", est_tokens=3,
                    source_hash=document.source_hash) for i in range(n)]
    return document, chunks


async def _scalar(conn, query: str, **params):
    rows = list(await conn._run(query, **params))
    return rows[0][0] if rows else None


async def _count(conn, query: str, **params) -> int:
    return int(await _scalar(conn, query, **params))


async def _shape(conn, project: str, doc_id: str) -> dict:
    docs = await _count(conn, "MATCH (d:Document {project: $p, doc_id: $d}) RETURN count(d)", p=project, d=doc_id)
    chunks = await _count(conn, "MATCH (c:Chunk {project: $p, doc_id: $d}) RETURN count(c)", p=project, d=doc_id)
    contains = await _count(
        conn, "MATCH (:Document {project: $p, doc_id: $d})-[r:CONTAINS]->(:Chunk) RETURN count(r)",
        p=project, d=doc_id)
    precedes = await _count(
        conn, "MATCH (:Chunk {project: $p, doc_id: $d})-[r:PRECEDES]->(:Chunk) RETURN count(r)",
        p=project, d=doc_id)
    orphans = await _count(
        conn, "MATCH (c:Chunk {project: $p, doc_id: $d}) WHERE NOT (:Document)-[:CONTAINS]->(c) RETURN count(c)",
        p=project, d=doc_id)
    return {"documents": docs, "chunks": chunks, "contains": contains, "precedes": precedes, "orphans": orphans}


@contextlib.contextmanager
def _count_statements():
    """Count every statement that reaches the driver: session.run and managed-transaction run."""
    import neo4j
    from neo4j._async.work.transaction import AsyncTransactionBase

    state = {"n": 0, "queries": []}
    originals = {cls: cls.run for cls in (neo4j.AsyncSession, AsyncTransactionBase)}

    def _wrap(original):
        async def counted(self, query, *args, **kwargs):
            state["n"] += 1
            state["queries"].append(str(query))
            return await original(self, query, *args, **kwargs)
        return counted

    for cls, original in originals.items():
        cls.run = _wrap(original)
    try:
        yield state
    finally:
        for cls, original in originals.items():
            cls.run = original


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _fixture_repo(root: Path, **overrides: str) -> Path:
    files = {
        "README.md": "# Fixture\n\nThis repository documents quokka husbandry.\n",
        "docs/guide.md": ("# Quokka guide\n\n## Feeding\n\nQuokkas eat leaves and grasses at dusk, "
                          "with a feeding schedule of two meals.\n\n## Enclosure\n\n"
                          "The enclosure needs shade, dry bedding and fresh water.\n"),
        "docs/adr/ADR-001-bedding.md": "# Bedding ADR\n\nWe chose dry straw bedding for every enclosure.\n",
    }
    files.update(overrides)
    return _write_repo(root, files)


async def _ingest(db, project: str, repo_root: Path, kinds=DEFAULT_KINDS):
    from writ.documents.ingest import ingest_documents

    return await ingest_documents(db, project, repo_root, kinds, [])


async def _register(db, project: str, root: Path) -> None:
    await db.create_project(project, str(root), str(root / "bible"))


# --------------------------------------------------------------------------- #
# Schema: constraints and index (capability 4/5/6 prerequisites)
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_apply_constraints_creates_the_two_unique_constraints_and_the_project_index(self, db):
        names = {c.get("name") for c in await db.list_constraints()}
        assert {"document_doc_id_project_unique", "chunk_chunk_id_project_unique"} <= names, names
        assert "document_project" in {i.get("name") for i in await db.list_indexes()}

    @pytest.mark.asyncio
    async def test_the_constraints_are_keyed_on_the_id_and_the_project(self, db):
        rows = {c.get("name"): str(c) for c in await db.list_constraints()}
        for name, ident in (("document_doc_id_project_unique", "doc_id"),
                            ("chunk_chunk_id_project_unique", "chunk_id")):
            assert ident in rows[name] and "project" in rows[name], rows[name]

    @pytest.mark.asyncio
    async def test_a_second_apply_constraints_reports_no_failure(self, db):
        assert not await db.apply_constraints()


# --------------------------------------------------------------------------- #
# Capability 5: replace_document
# --------------------------------------------------------------------------- #
class TestReplaceDocument:
    @pytest.mark.asyncio
    async def test_a_new_document_writes_the_document_its_chunks_and_the_ordered_edges(self, db):
        document, chunks = _records(PROJECT_A, "docs/a.md", 3)
        assert await db.replace_document(PROJECT_A, document, chunks) == 3
        assert await _shape(db, PROJECT_A, "docs/a.md") == {
            "documents": 1, "chunks": 3, "contains": 3, "precedes": 2, "orphans": 0}
        chain = list(await db._run(
            "MATCH (a:Chunk {project: $p, doc_id: $d})-[:PRECEDES]->(b:Chunk) "
            "RETURN a.ordinal AS a, b.ordinal AS b ORDER BY a", p=PROJECT_A, d="docs/a.md"))
        assert [(r["a"], r["b"]) for r in chain] == [(0, 1), (1, 2)]

    @pytest.mark.asyncio
    async def test_the_stored_properties_follow_the_models_and_the_record_stamps(self, db):
        document, chunks = _records(PROJECT_A, "docs/a.md", 2, kind="adr")
        await db.replace_document(PROJECT_A, document, chunks)
        doc = dict((await db._run("MATCH (d:Document {project: $p}) RETURN properties(d) AS p", p=PROJECT_A))[0]["p"])
        assert doc["doc_id"] == "docs/a.md" and doc["kind"] == "adr" and doc["chunk_count"] == 2
        assert doc["source_hash"] == document.source_hash and doc["provenance"] == "record"
        assert doc["source_origin"] == "graph-authored"
        rows = await db._run("MATCH (c:Chunk {project: $p}) RETURN properties(c) AS p ORDER BY c.ordinal", p=PROJECT_A)
        props = [dict(r["p"]) for r in rows]
        assert [p["chunk_id"] for p in props] == [c.chunk_id for c in chunks]
        assert all(p["provenance"] == "record" and p["source_origin"] == "graph-authored" for p in props)
        assert props[1]["text"] == "alpha body 1" and props[1]["breadcrumb"] == "Title > Part 1"
        assert props[1]["est_tokens"] == 3 and props[1]["ordinal"] == 1

    @pytest.mark.asyncio
    async def test_the_whole_replace_is_one_statement(self, db):
        document, chunks = _records(PROJECT_A, "docs/a.md", 4)
        with _count_statements() as counted:
            await db.replace_document(PROJECT_A, document, chunks)
        assert counted["n"] == 1, counted["queries"]

    @pytest.mark.asyncio
    async def test_replacing_with_fewer_chunks_leaves_exactly_the_new_set_and_no_orphan(self, db):
        old, old_chunks = _records(PROJECT_A, "docs/a.md", 5, word="old")
        await db.replace_document(PROJECT_A, old, old_chunks)
        new, new_chunks = _records(PROJECT_A, "docs/a.md", 2, word="new")
        await db.replace_document(PROJECT_A, new, new_chunks)
        assert await _shape(db, PROJECT_A, "docs/a.md") == {
            "documents": 1, "chunks": 2, "contains": 2, "precedes": 1, "orphans": 0}
        texts = [r[0] for r in await db._run(
            "MATCH (c:Chunk {project: $p, doc_id: $d}) RETURN c.text ORDER BY c.ordinal", p=PROJECT_A, d="docs/a.md")]
        assert texts == ["new body 0", "new body 1"], "no chunk of the old text may survive"
        total = await _count(db, "MATCH (c:Chunk {project: $p}) RETURN count(c)", p=PROJECT_A)
        assert total == 2

    @pytest.mark.asyncio
    async def test_replacing_with_more_chunks_extends_the_chain(self, db):
        old, old_chunks = _records(PROJECT_A, "docs/a.md", 2)
        await db.replace_document(PROJECT_A, old, old_chunks)
        new, new_chunks = _records(PROJECT_A, "docs/a.md", 6, word="new")
        await db.replace_document(PROJECT_A, new, new_chunks)
        assert await _shape(db, PROJECT_A, "docs/a.md") == {
            "documents": 1, "chunks": 6, "contains": 6, "precedes": 5, "orphans": 0}

    @pytest.mark.asyncio
    async def test_a_zero_chunk_document_still_writes_its_document(self, db):
        document, _ = _records(PROJECT_A, "docs/empty.md", 0)
        assert await db.replace_document(PROJECT_A, document, []) == 0
        assert await _shape(db, PROJECT_A, "docs/empty.md") == {
            "documents": 1, "chunks": 0, "contains": 0, "precedes": 0, "orphans": 0}

    @pytest.mark.asyncio
    async def test_replacing_a_populated_document_with_zero_chunks_removes_them_all(self, db):
        old, old_chunks = _records(PROJECT_A, "docs/a.md", 3)
        await db.replace_document(PROJECT_A, old, old_chunks)
        empty, _ = _records(PROJECT_A, "docs/a.md", 0)
        await db.replace_document(PROJECT_A, empty, [])
        assert await _shape(db, PROJECT_A, "docs/a.md") == {
            "documents": 1, "chunks": 0, "contains": 0, "precedes": 0, "orphans": 0}

    @pytest.mark.asyncio
    async def test_a_single_chunk_document_has_no_precedes_edge(self, db):
        document, chunks = _records(PROJECT_A, "docs/one.md", 1)
        await db.replace_document(PROJECT_A, document, chunks)
        assert (await _shape(db, PROJECT_A, "docs/one.md"))["precedes"] == 0

    @pytest.mark.asyncio
    async def test_get_document_hashes_returns_the_stored_hash_and_kind_per_doc_id(self, db):
        a, ac = _records(PROJECT_A, "docs/a.md", 1, kind="docs")
        b, bc = _records(PROJECT_A, "README.md", 1, kind="readme")
        other, oc = _records(PROJECT_B, "docs/a.md", 1)
        for project, document, chunks in ((PROJECT_A, a, ac), (PROJECT_A, b, bc), (PROJECT_B, other, oc)):
            await db.replace_document(project, document, chunks)
        got = await db.get_document_hashes(PROJECT_A)
        assert got == {"docs/a.md": {"source_hash": a.source_hash, "kind": "docs"},
                       "README.md": {"source_hash": b.source_hash, "kind": "readme"}}
        assert await db.get_document_hashes("test-docs-nobody") == {}


class TestDeleteDocuments:
    @pytest.mark.asyncio
    async def test_it_removes_the_named_documents_and_their_chunks_and_nothing_else(self, db):
        for doc_id in ("docs/a.md", "docs/b.md", "docs/c.md"):
            document, chunks = _records(PROJECT_A, doc_id, 2)
            await db.replace_document(PROJECT_A, document, chunks)
        removed = await db.delete_documents(PROJECT_A, ["docs/a.md", "docs/b.md"])
        assert isinstance(removed, int) and removed >= 2
        assert (await _shape(db, PROJECT_A, "docs/a.md"))["chunks"] == 0
        assert (await _shape(db, PROJECT_A, "docs/b.md"))["documents"] == 0
        assert await _shape(db, PROJECT_A, "docs/c.md") == {
            "documents": 1, "chunks": 2, "contains": 2, "precedes": 1, "orphans": 0}

    @pytest.mark.asyncio
    async def test_it_never_touches_another_projects_document_with_the_same_id(self, db):
        for project in (PROJECT_A, PROJECT_B):
            document, chunks = _records(project, "README.md", 2)
            await db.replace_document(project, document, chunks)
        await db.delete_documents(PROJECT_A, ["README.md"])
        assert (await _shape(db, PROJECT_A, "README.md"))["documents"] == 0
        assert await _shape(db, PROJECT_B, "README.md") == {
            "documents": 1, "chunks": 2, "contains": 2, "precedes": 1, "orphans": 0}

    @pytest.mark.asyncio
    async def test_an_empty_list_returns_zero_without_a_statement(self, db):
        with _count_statements() as counted:
            assert await db.delete_documents(PROJECT_A, []) == 0
        assert counted["n"] == 0

    @pytest.mark.asyncio
    async def test_deleting_the_documents_is_one_statement(self, db):
        for doc_id in ("docs/a.md", "docs/b.md"):
            document, chunks = _records(PROJECT_A, doc_id, 2)
            await db.replace_document(PROJECT_A, document, chunks)
        with _count_statements() as counted:
            await db.delete_documents(PROJECT_A, ["docs/a.md", "docs/b.md"])
        assert counted["n"] == 1

    @pytest.mark.asyncio
    async def test_clear_project_removes_the_projects_documents_and_chunks_with_the_rest(self, db):
        document, chunks = _records(PROJECT_A, "docs/a.md", 2)
        await db.replace_document(PROJECT_A, document, chunks)
        other, other_chunks = _records(PROJECT_B, "docs/a.md", 2)
        await db.replace_document(PROJECT_B, other, other_chunks)
        await db.clear_project(PROJECT_A)
        assert (await _shape(db, PROJECT_A, "docs/a.md"))["chunks"] == 0
        assert (await _shape(db, PROJECT_B, "docs/a.md"))["chunks"] == 2


# --------------------------------------------------------------------------- #
# Capability 6: concurrency and project separation
# --------------------------------------------------------------------------- #
class TestConcurrency:
    @pytest.mark.asyncio
    async def test_eight_concurrent_replaces_of_one_document_leave_one_document_and_one_chunk_set(self, db):
        document, chunks = _records(PROJECT_A, "docs/race.md", 4)
        results = await asyncio.gather(
            *[db.replace_document(PROJECT_A, document, chunks) for _ in range(8)], return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        assert await _shape(db, PROJECT_A, "docs/race.md") == {
            "documents": 1, "chunks": 4, "contains": 4, "precedes": 3, "orphans": 0}

    @pytest.mark.asyncio
    async def test_concurrent_replaces_with_different_versions_converge_on_one_whole_version(self, db):
        versions = [_records(PROJECT_A, "docs/race.md", n, word=f"v{n}") for n in (2, 3, 5)]
        results = await asyncio.gather(
            *[db.replace_document(PROJECT_A, d, c) for d, c in versions * 2], return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        shape = await _shape(db, PROJECT_A, "docs/race.md")
        assert shape["documents"] == 1 and shape["orphans"] == 0
        assert shape["chunks"] in (2, 3, 5) and shape["contains"] == shape["chunks"]
        assert shape["precedes"] == shape["chunks"] - 1
        hashes = await db.get_document_hashes(PROJECT_A)
        texts = {r[0] for r in await db._run(
            "MATCH (c:Chunk {project: $p, doc_id: 'docs/race.md'}) RETURN c.text", p=PROJECT_A)}
        winner = hashes["docs/race.md"]["source_hash"].split("-")[1]
        assert all(t.startswith(winner) for t in texts), "chunks of two versions must never mix"

    @pytest.mark.asyncio
    async def test_concurrent_ingests_of_the_same_repo_leave_one_set_of_documents(self, db, tmp_path):
        repo = _fixture_repo(tmp_path / "repo")
        reports = await asyncio.gather(*[_ingest(db, PROJECT_A, repo) for _ in range(4)])
        assert all(r.scanned == 3 for r in reports)
        for doc_id in ("README.md", "docs/guide.md", "docs/adr/ADR-001-bedding.md"):
            shape = await _shape(db, PROJECT_A, doc_id)
            assert shape["documents"] == 1 and shape["orphans"] == 0
            assert shape["contains"] == shape["chunks"] >= 1
        assert await _count(db, "MATCH (d:Document {project: $p}) RETURN count(d)", p=PROJECT_A) == 3

    @pytest.mark.asyncio
    async def test_two_projects_with_the_same_path_keep_separate_documents_and_chunks(self, db):
        a, ac = _records(PROJECT_A, "README.md", 3, word="aaa")
        b, bc = _records(PROJECT_B, "README.md", 2, word="bbb")
        await asyncio.gather(db.replace_document(PROJECT_A, a, ac), db.replace_document(PROJECT_B, b, bc))
        assert (await _shape(db, PROJECT_A, "README.md"))["chunks"] == 3
        assert (await _shape(db, PROJECT_B, "README.md"))["chunks"] == 2
        await db.replace_document(PROJECT_B, *_records(PROJECT_B, "README.md", 1, word="bbb2"))
        assert (await _shape(db, PROJECT_A, "README.md"))["chunks"] == 3, "another project's replace must not reach it"
        ids = {r[0] for r in await db._run("MATCH (c:Chunk) WHERE c.project IN $ps RETURN c.chunk_id",
                                           ps=[PROJECT_A, PROJECT_B])}
        assert len(ids) == 4 and all(i.startswith((PROJECT_A + ":", PROJECT_B + ":")) for i in ids)


# --------------------------------------------------------------------------- #
# Capability 7: record labels, preserved by wipes, absent from the dump
# --------------------------------------------------------------------------- #
class TestRecordLabelPreservation:
    @staticmethod
    async def _seed(conn, project: str = PROJECT_A):
        document, chunks = _records(project, "docs/keep.md", 3)
        await conn.replace_document(project, document, chunks)
        return await _shape(conn, project, "docs/keep.md")

    @pytest.mark.asyncio
    async def test_a_default_clear_all_leaves_every_document_and_chunk(self, wipe_db):
        before = await self._seed(wipe_db)
        await wipe_db.clear_all()
        assert await wipe_db.count_rules() == 0, "the corpus itself must still be wiped"
        assert await _shape(wipe_db, PROJECT_A, "docs/keep.md") == before

    @pytest.mark.asyncio
    async def test_a_corpus_only_dump_replay_leaves_every_document_and_chunk(self, wipe_db):
        from writ.graph.dump import import_cypher_dump, render_cypher_dump

        before = await self._seed(wipe_db)
        script = render_cypher_dump(
            [{"id": "R-CORPUS-DOC-1", "label": "Rule",
              "props": {"rule_id": "R-CORPUS-DOC-1", "statement": "s"}}], [])
        await import_cypher_dump(wipe_db, script)
        assert await _shape(wipe_db, PROJECT_A, "docs/keep.md") == before
        props = dict((await wipe_db._run(
            "MATCH (c:Chunk {project: $p, ordinal: 1}) RETURN properties(c) AS p", p=PROJECT_A))[0]["p"])
        assert props["text"] == "alpha body 1", "a replay must not rewrite the chunk"

    @pytest.mark.asyncio
    async def test_the_dump_nodes_and_edges_carry_neither_label_nor_any_edge_touching_them(self, db):
        await self._seed(db)
        nodes = await db.get_all_nodes_for_dump()
        assert not {n["label"] for n in nodes} & {"Document", "Chunk"}
        assert not [n for n in nodes if n["id"] is None]
        edges = await db.get_all_edges_cross_type()
        flat = json.dumps(edges, default=str)
        assert f"{PROJECT_A}:docs/keep.md" not in flat and "docs/keep.md" not in flat

    @pytest.mark.asyncio
    async def test_the_rendered_script_holds_no_label_no_id_no_chunk_text_and_no_document_edge(self, db):
        from writ.graph.dump import render_cypher_dump

        document, chunks = _records(PROJECT_A, "docs/probe.md", 2, word="zzprobe")
        await db.replace_document(PROJECT_A, document, chunks)
        script = render_cypher_dump(await db.get_all_nodes_for_dump(), await db.get_all_edges_cross_type())
        for forbidden in (":Document", ":Chunk", "Document", "Chunk", "zzprobe", "docs/probe.md",
                          f"{PROJECT_A}:docs/probe.md#0000"):
            assert forbidden not in script, forbidden


# --------------------------------------------------------------------------- #
# Capability 8: nothing the rest of the system reads changes
# --------------------------------------------------------------------------- #
def _bible(tmp: Path, rule_ids: list[str]) -> Path:
    bible = tmp / "bible"
    bible.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"<!-- RULE START: {rid} -->\n## Rule {rid}\n\n**Domain**: security\n**Severity**: Medium\n"
        f"**Scope**: Component\n\n### Trigger\nWhen a thing happens.\n\n### Statement\nA statement for {rid}.\n\n"
        f"### Violation\n```python\nx = 1\n```\n\n### Pass\n```python\ny = 2\n```\n\n### Enforcement\n"
        f"Code review.\n\n### Rationale\nA rationale for {rid}.\n\n<!-- RULE END: {rid} -->\n"
        for rid in rule_ids)
    (bible / "rules.md").write_text(body, encoding="utf-8")
    return bible


async def _graph_reads(conn) -> dict:
    from writ.graph.integrity import IntegrityChecker
    from writ.retrieval.traversal import AdjacencyCache

    cache = AdjacencyCache()
    built = await cache.build_from_db(conn)
    checker = IntegrityChecker(conn._driver, conn._database)
    orphans_all, orphan_counts = await checker.detect_orphans_all_labels()
    return json.loads(json.dumps({
        "adjacency_built": built, "adjacency": cache.snapshot(),
        "routes": await conn.get_category_routes_by_node(),
        "orphans": await checker.detect_orphans(), "conflicts": await checker.detect_conflicts(),
        "orphans_all": orphans_all, "orphan_counts": orphan_counts,
    }, default=str, sort_keys=True))


class TestEverythingElseIsUnaffected:
    @pytest.mark.asyncio
    async def test_adjacency_routes_and_integrity_reads_are_identical_after_ingesting_a_repo(self, db, tmp_path):
        repo = _fixture_repo(tmp_path / "repo")
        before = await _graph_reads(db)
        report = await _ingest(db, PROJECT_A, repo)
        assert report.written == 3 and report.chunks >= 3
        after = await _graph_reads(db)
        assert after == before

    @pytest.mark.asyncio
    async def test_no_chunk_or_document_id_reaches_the_adjacency_cache(self, db, tmp_path):
        from writ.retrieval.traversal import AdjacencyCache

        await _ingest(db, PROJECT_A, _fixture_repo(tmp_path / "repo"))
        cache = AdjacencyCache()
        await cache.build_from_db(db)
        flat = json.dumps(cache.snapshot(), default=str)
        assert f"{PROJECT_A}:" not in flat and "docs/guide.md" not in flat

    @pytest.mark.asyncio
    async def test_a_writ_ingest_reconcile_leaves_every_document_and_chunk_in_place(self, db, tmp_path):
        from writ.graph.methodology_ingest import ingest_path, reconcile

        repo = _fixture_repo(tmp_path / "repo")
        await _ingest(db, PROJECT_A, repo)
        before = await _graph_reads_docs(db)
        bible = _bible(tmp_path, ["DOCS-RECON-001", "DOCS-RECON-002"])
        await ingest_path(bible, db, project=PROJECT_A)
        result = await reconcile(bible, db, project=PROJECT_A)
        assert await _graph_reads_docs(db) == before
        assert not [d for d in result["deleted_nodes"] if str(d).startswith(f"{PROJECT_A}:docs")]
        assert not [k for k in result["cleared_props"] if str(k).startswith(f"{PROJECT_A}:docs")]


async def _graph_reads_docs(conn) -> list:
    rows = await conn._run(
        "MATCH (n) WHERE (n:Document OR n:Chunk) AND n.project = $p "
        "RETURN labels(n)[0] AS l, properties(n) AS p ORDER BY l, coalesce(n.chunk_id, n.doc_id)", p=PROJECT_A)
    return json.loads(json.dumps([[r["l"], dict(r["p"])] for r in rows], default=str, sort_keys=True))


# --------------------------------------------------------------------------- #
# Capability 33: statement counts
# --------------------------------------------------------------------------- #
class TestStatementCounts:
    @staticmethod
    async def _constraint_statements() -> int:
        """C: what apply_constraints issues, measured, not written down."""
        probe = connection()
        try:
            with _count_statements() as counted:
                await probe.apply_constraints()
            return counted["n"]
        finally:
            await probe.close()

    @staticmethod
    async def _full_run(project: str, repo: Path):
        """One CLI-shaped run on a FRESH connection: registry read, constraints, ingest."""
        conn = connection()
        try:
            with _count_statements() as counted:
                resolved = await conn.resolve_project_for_cwd(str(repo))
                await conn.apply_constraints()
                report = await _ingest(conn, resolved, repo)
            return counted["n"], report
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_an_unchanged_reingest_issues_exactly_two_plus_c_statements(self, db, tmp_path):
        repo = _fixture_repo(tmp_path / "repo")
        await _register(db, PROJECT_A, repo)
        await self._full_run(PROJECT_A, repo)
        c = await self._constraint_statements()
        assert c > 0
        n, report = await self._full_run(PROJECT_A, repo)
        assert report.written == 0 and report.unchanged == 3 and report.deleted == 0
        assert n == 2 + c

    @pytest.mark.asyncio
    async def test_k_changed_documents_with_a_deletion_issue_two_plus_c_plus_k_plus_one(self, db, tmp_path):
        repo = _fixture_repo(tmp_path / "repo")
        await _register(db, PROJECT_A, repo)
        await self._full_run(PROJECT_A, repo)
        c = await self._constraint_statements()
        (repo / "docs" / "guide.md").write_text("# Quokka guide\n\nEdited body text.\n", encoding="utf-8")
        (repo / "README.md").write_text("# Fixture\n\nEdited readme text.\n", encoding="utf-8")
        (repo / "docs" / "adr" / "ADR-001-bedding.md").unlink()
        n, report = await self._full_run(PROJECT_A, repo)
        k = 2
        assert (report.written, report.deleted) == (k, 1)
        assert n == 2 + c + k + 1

    @pytest.mark.asyncio
    async def test_loading_the_inputs_with_documents_costs_exactly_one_more_read(self, db, tmp_path):
        from writ.retrieval.pipeline import load_pipeline_inputs

        await _ingest(db, PROJECT_A, _fixture_repo(tmp_path / "repo"))
        with _count_statements() as without:
            plain = await load_pipeline_inputs(db)
        with _count_statements() as with_docs:
            full = await load_pipeline_inputs(db, with_documents=True)
        assert with_docs["n"] == without["n"] + 1
        assert plain.chunks == [] and full.chunks, "documents are read only when asked for"
        assert full.chunks_stamp != plain.chunks_stamp

    @pytest.mark.asyncio
    async def test_the_chunk_read_is_one_statement_ordered_by_chunk_id(self, db, tmp_path):
        from writ.retrieval.pipeline import DOCUMENT_COLLECTOR

        await _ingest(db, PROJECT_A, _fixture_repo(tmp_path / "repo"))
        with _count_statements() as counted:
            collected = await DOCUMENT_COLLECTOR.run(db)
        assert counted["n"] == 1 and "ORDER BY c.chunk_id" in counted["queries"][0]
        ids = [u["chunk_id"] for u in collected.units]
        assert ids == sorted(ids) and len(ids) >= 3
        assert {"title", "path", "kind", "next_id", "prev_id", "project", "doc_id", "ordinal"} <= set(collected.units[0])
        with _count_statements() as stamped:
            assert collected.stamp
        assert stamped["n"] == 0, "the stamp is computed in process"


# --------------------------------------------------------------------------- #
# Capabilities 34 and 35: reload end to end, calibration. Real encoder, shared.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def real_encoder(methodology_model):
    return methodology_model


@pytest.fixture()
def isolated_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))


class _RuleSpy:
    """Stand-in for assemble_pipeline: counts builds, hands back the shared encoder."""

    def __init__(self, monkeypatch, encoder) -> None:
        self.calls = 0
        self.encoder = encoder

        def build(inputs, **kwargs):
            self.calls += 1
            return SimpleNamespace(kind="rules", encoder=kwargs.get("embedding_model") or self.encoder)

        monkeypatch.setattr(reload_mod, "assemble_pipeline", build)


async def _handle(db, previous, generation: int):
    return await reload_mod.build_retrieval_handle(
        db, previous, generation, abstention_threshold=0.3, authority_preference_threshold=0.0)


def _query(docs, prompt: str, project: str, exclude=()):
    return docs.query(prompt, project=project, exclude_ids=set(exclude))


class TestReloadEndToEnd:
    @pytest.mark.asyncio
    async def test_a_documents_query_returns_the_expected_chunk_for_its_project_and_nothing_for_another(
            self, db, tmp_path, monkeypatch, real_encoder, isolated_caches):
        _RuleSpy(monkeypatch, real_encoder)
        repo_a = _fixture_repo(tmp_path / "a")
        repo_b = _write_repo(tmp_path / "b", {"docs/volcano.md": (
            "# Volcano monitoring\n\nSeismometers record magma chamber tremors before an eruption.\n")})
        await _ingest(db, PROJECT_A, repo_a)
        await _ingest(db, PROJECT_B, repo_b)
        handle = await _handle(db, None, 1)
        assert handle.documents is not None and handle.documents.encoder is real_encoder
        prompt = "What feeding schedule do quokkas follow, and what do they eat at dusk?"
        mine = _query(handle.documents, prompt, PROJECT_A)
        assert mine["mode"] == "ranked"
        assert mine["chunks"][0]["id"] == f"{PROJECT_A}:docs/guide.md#0000" or \
            mine["chunks"][0]["doc_id"] == "docs/guide.md"
        assert all(c["project"] == PROJECT_A for c in mine["chunks"])
        theirs = _query(handle.documents, prompt, PROJECT_B)
        assert theirs["chunks"] == [] and theirs["mode"] == "abstained"
        volcano = _query(handle.documents, "How do seismometers detect magma chamber tremors?", PROJECT_A)
        assert volcano["chunks"] == [], "another project's document must never be served"

    @pytest.mark.asyncio
    async def test_after_one_file_changes_the_next_reload_rebuilds_only_the_document_half(
            self, db, tmp_path, monkeypatch, real_encoder, isolated_caches):
        spy = _RuleSpy(monkeypatch, real_encoder)
        repo = _fixture_repo(tmp_path / "a")
        await _ingest(db, PROJECT_A, repo)
        first = await _handle(db, None, 1)
        assert spy.calls == 1
        (repo / "docs" / "guide.md").write_text(
            "# Quokka guide\n\n## Feeding\n\nQuokkas now receive pellets of fortified biscuit every morning.\n",
            encoding="utf-8")
        report = await _ingest(db, PROJECT_A, repo)
        assert report.written == 1
        second = await _handle(db, first, 2)
        assert second is not None and spy.calls == 1, "the rule half must be reused: assemble_pipeline not called"
        assert second.pipeline is first.pipeline and second.documents is not first.documents
        assert second.documents.encoder is real_encoder
        served = _query(second.documents, "Which fortified biscuit pellets do quokkas receive each morning?", PROJECT_A)
        assert any("biscuit" in c["text"] for c in served["chunks"])
        stale = _query(second.documents, "Quokkas eat leaves and grasses at dusk with two meals", PROJECT_A)
        assert not any("leaves and grasses" in c["text"] for c in stale["chunks"]), "old text must be gone"
        assert await _handle(db, second, 3) is None, "an unchanged graph returns no handle"

    @pytest.mark.asyncio
    async def test_a_warm_documents_request_issues_zero_statements(
            self, db, tmp_path, monkeypatch, real_encoder, isolated_caches):
        _RuleSpy(monkeypatch, real_encoder)
        repo = _fixture_repo(tmp_path / "a")
        await _register(db, PROJECT_A, repo)
        await _ingest(db, PROJECT_A, repo)
        handle = await _handle(db, None, 1)
        cache = {"loaded_rule_ids_by_phase": {}, "current_phase": "", "loaded_rule_ids": [],
                 "remaining_budget": 8000, "last_injected_rule_ids": [], "detected_domain": "",
                 "context_percent": 0, "is_subagent": False, "compaction_epoch": 0, "injection_shown": {}}
        monkeypatch.setattr(server, "_db", db)
        monkeypatch.setattr(server, "_pipeline", object())
        monkeypatch.setattr(server, "_documents", handle.documents, raising=False)
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: dict(cache))
        monkeypatch.setattr(server.writ_session, "cmd_update", MagicMock())
        monkeypatch.setattr(qroute, "emit", MagicMock())
        from writ.server.models import PromptBundleRequest

        request = PromptBundleRequest(
            session_id="s-warm", prompt="What feeding schedule do quokkas follow at dusk?", mode="work",
            sections=["documents"], project_root=str(repo))
        await db.resolve_project_for_cwd(str(repo))
        with _count_statements() as counted:
            out = await qroute.prompt_bundle(request)
        assert out["documents_block"].startswith("--- WRIT DOCUMENTS"), out
        assert counted["n"] == 0, counted["queries"]


class TestCalibration:
    @pytest.mark.asyncio
    async def test_negatives_abstain_and_named_documents_land_in_the_top_three(
            self, db, monkeypatch, real_encoder, isolated_caches):
        from writ.retrieval import documents as docs_mod
        from writ.retrieval.pipeline import load_pipeline_inputs

        _RuleSpy(monkeypatch, real_encoder)
        report = await _ingest(db, PROJECT_CAL, REPO, frozenset({"docs", "adr"}))
        assert report.written > 20, "the repo's own docs/ must be ingested for the calibration to mean anything"
        inputs = await load_pipeline_inputs(db, with_documents=True)
        mine = [c for c in inputs.chunks if c["project"] == PROJECT_CAL]
        pipe = docs_mod.assemble_document_pipeline(mine, embedding_model=real_encoder)
        assert pipe.encoder is real_encoder

        negatives = json.loads((REPO / "tests/fixtures/ground_truth_negatives.json").read_text())["negatives"]
        abstained = sum(
            1 for n in negatives
            if _query(pipe, n["query"], PROJECT_CAL)["mode"] == "abstained")
        abstain_rate = abstained / len(negatives)

        queries = json.loads((REPO / "tests/fixtures/document_queries.json").read_text())["queries"]
        assert len(queries) == 12
        placed = 0
        misses = []
        for q in queries:
            result = _query(pipe, q["prompt"], PROJECT_CAL)
            top3 = [c["doc_id"] for c in result["chunks"] if c.get("similarity") is not None][:3]
            if q["document"] in top3:
                placed += 1
            else:
                misses.append((q["id"], q["document"], top3))
        threshold = docs_mod.DOCUMENT_ABSTENTION_THRESHOLD
        message = (f"threshold={threshold} abstain_rate={abstain_rate:.3f} ({abstained}/{len(negatives)}) "
                   f"placed={placed}/12 misses={misses}")
        assert abstain_rate >= 0.90, message
        assert placed >= 10, message

    def test_the_threshold_recorded_in_the_adr_is_the_one_in_the_code(self):
        from writ.retrieval import documents as docs_mod

        adr = (REPO / "docs" / "adr" / "ADR-document-retrieval.md").read_text(encoding="utf-8")
        assert "DOCUMENT_ABSTENTION_THRESHOLD" in adr
        assert re.search(rf"\b{re.escape(str(docs_mod.DOCUMENT_ABSTENTION_THRESHOLD))}0*\b", adr), (
            "the ADR must record the calibrated threshold value")
        assert re.search(r"abstain", adr, re.I) and re.search(r"top[- ]3|top three", adr, re.I)
