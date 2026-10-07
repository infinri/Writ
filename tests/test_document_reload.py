"""Program item 5 (workstream D), phase 3: the document half of the live-reload generation.

One handle, one install, one swap: RetrievalHandle gains `documents` (default None), and
build_retrieval_handle rebuilds only the half whose fingerprint keys moved.

  * documents-only change: the rule pipeline object is the previous one (assemble_pipeline is
    not called) and the document pipeline is new
  * rules-only change: the previous document pipeline is reused
  * unchanged graph: no handle
  * a document build that raises never blocks the rule half: the handle carries the previous
    document pipeline with the previous document keys (None and the string "unbuilt" on first
    start), an exception row is written, and the next reload retries the build
  * the document build receives the rule pipeline's own encoder (no second model)

Everything is spied at the reload module's seams (load_pipeline_inputs, the trigger-index
build, assemble_pipeline, assemble_document_pipeline) with hand-built PipelineInputs and the
REAL pipeline_fingerprint, so the decision table is exercised against the real keys. No graph
and no index is built; the statement counts and the end-to-end reload live in
tests/test_document_ingest_graph.py (ENF-SYS-005: nothing here claims atomicity or
concurrency).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import writ.server as server
import writ.server.reload as reload_mod
from tests._document_fixtures import make_document
from writ.retrieval.pipeline import fingerprint_digest
from writ.retrieval.pipeline import PipelineInputs, pipeline_fingerprint
from writ.retrieval.traversal import AdjacencyCache

DOC_KEYS = ("documents_bm25", "documents_hnsw", "documents_meta")
RULE_KEYS = ("index_version", "bm25", "hnsw", "metadata", "edges", "routes", "abstractions")


def _chunks(text: str = "alpha beta gamma") -> list[dict]:
    return make_document("p", "docs/x.md", [("Doc > A", text), ("Doc > B", "second chunk text")], title="Doc")


def _inputs(statement: str = "s", chunk_text: str = "alpha beta gamma", with_chunks: bool = True) -> PipelineInputs:
    candidates = [{"rule_id": "R-1", "trigger": "t", "statement": statement, "tags": "", "body": "",
                   "mandatory": False, "project": "writ", "severity": "medium"}]
    chunks = _chunks(chunk_text) if with_chunks else []
    return PipelineInputs(
        candidates=candidates, rule_metadata={"R-1": candidates[0]}, adjacency_cache=AdjacencyCache(),
        abstractions=[], node_routes=None, chunks=chunks, chunks_stamp=fingerprint_digest(chunks),
    )


class _Spies:
    def __init__(self, monkeypatch, inputs_queue: list[PipelineInputs]):
        self.inputs_queue = inputs_queue
        self.load_kwargs: list[dict] = []
        self.rule_builds: list[dict] = []
        self.doc_builds: list[dict] = []
        self.exceptions: list[tuple] = []
        self.doc_error: Exception | None = None
        self.rule_error: Exception | None = None
        self.encoder = object()

        async def load(db, **kwargs):
            self.load_kwargs.append(kwargs)
            return self.inputs_queue[min(len(self.load_kwargs), len(self.inputs_queue)) - 1]

        async def triggers(db):
            return SimpleNamespace(snapshot=lambda: {"trigger": "stable"})

        def assemble_rules(inputs, **kwargs):
            if self.rule_error is not None:
                raise self.rule_error
            self.rule_builds.append(kwargs)
            return SimpleNamespace(kind="rules", encoder=kwargs.get("embedding_model") or self.encoder)

        def assemble_docs(chunks, **kwargs):
            if self.doc_error is not None:
                raise self.doc_error
            self.doc_builds.append({"chunks": chunks, **kwargs})
            return SimpleNamespace(kind="documents", chunks=chunks, encoder=kwargs.get("embedding_model"))

        monkeypatch.setattr(reload_mod, "load_pipeline_inputs", load)
        monkeypatch.setattr(reload_mod.MethodologyTriggerIndex, "build_from_db", triggers)
        monkeypatch.setattr(reload_mod, "assemble_pipeline", assemble_rules)
        monkeypatch.setattr(reload_mod, "assemble_document_pipeline", assemble_docs)
        monkeypatch.setattr(reload_mod, "emit_exception",
                            lambda where, exc, *a, **k: self.exceptions.append((where, exc)))


async def _build(previous, generation: int = 1):
    return await reload_mod.build_retrieval_handle(
        object(), previous, generation, abstention_threshold=0.3, authority_preference_threshold=0.0)


@pytest.fixture()
def spies_for(monkeypatch):
    def make(*inputs: PipelineInputs) -> _Spies:
        return _Spies(monkeypatch, list(inputs))
    return make


class TestHandleShape:
    def test_documents_is_a_defaulted_last_field(self):
        fields = list(reload_mod.RetrievalHandle.__dataclass_fields__)
        assert fields[-1] == "documents"
        handle = reload_mod.RetrievalHandle(generation=1, pipeline=object(), trigger_index=object(),
                                            fingerprint={}, built_at="t")
        assert handle.documents is None

    def test_the_handle_stays_frozen(self):
        handle = reload_mod.RetrievalHandle(1, object(), object(), {}, "t")
        with pytest.raises(Exception):
            handle.documents = object()


class TestFirstBuild:
    @pytest.mark.asyncio
    async def test_it_loads_inputs_with_documents_and_builds_both_halves(self, spies_for):
        spies = spies_for(_inputs())
        handle = await _build(None)
        assert spies.load_kwargs == [{"with_documents": True}]
        assert len(spies.rule_builds) == 1 and len(spies.doc_builds) == 1
        assert handle.pipeline.kind == "rules" and handle.documents.kind == "documents"
        assert set(DOC_KEYS) <= set(handle.fingerprint)
        assert handle.fingerprint["documents_meta"] == _inputs().chunks_stamp

    @pytest.mark.asyncio
    async def test_the_document_build_receives_the_rule_pipelines_own_encoder(self, spies_for):
        spies = spies_for(_inputs())
        handle = await _build(None)
        assert spies.doc_builds[0]["embedding_model"] is handle.pipeline.encoder is spies.encoder

    @pytest.mark.asyncio
    async def test_first_start_with_no_chunks_yields_an_empty_document_pipeline_and_real_keys(
            self, spies_for, monkeypatch):
        from writ.retrieval import documents as documents_mod
        spies = spies_for(_inputs(with_chunks=False))
        monkeypatch.setattr(reload_mod, "assemble_document_pipeline", documents_mod.assemble_document_pipeline)
        handle = await _build(None)
        assert isinstance(handle.documents, documents_mod.DocumentPipeline)
        assert handle.documents.query("anything", project="p", exclude_ids=set())["chunks"] == []
        assert all(handle.fingerprint[k] != "unbuilt" for k in DOC_KEYS)
        assert spies.exceptions == []


class TestPartialRebuilds:
    @pytest.mark.asyncio
    async def test_a_documents_only_change_reuses_the_rule_pipeline_and_builds_a_new_document_half(self, spies_for):
        spies = spies_for(_inputs(), _inputs(chunk_text="changed words entirely"))
        first = await _build(None)
        spies.rule_builds.clear(); spies.doc_builds.clear()
        second = await _build(first, generation=2)
        assert second is not None and second.generation == 2
        assert spies.rule_builds == [], "assemble_pipeline must not run for a documents-only change"
        assert second.pipeline is first.pipeline
        assert second.documents is not first.documents
        assert len(spies.doc_builds) == 1
        assert spies.doc_builds[0]["chunks"] == _inputs(chunk_text="changed words entirely").chunks
        assert spies.doc_builds[0]["embedding_model"] is first.pipeline.encoder
        moved = {k for k in second.fingerprint if second.fingerprint[k] != first.fingerprint[k]}
        assert moved and moved <= set(DOC_KEYS)

    @pytest.mark.asyncio
    async def test_a_rules_only_change_reuses_the_document_pipeline(self, spies_for):
        spies = spies_for(_inputs(), _inputs(statement="a different statement"))
        first = await _build(None)
        spies.rule_builds.clear(); spies.doc_builds.clear()
        second = await _build(first, generation=2)
        assert second is not None
        assert spies.doc_builds == [], "the document build must not run for a rules-only change"
        assert second.documents is first.documents
        assert second.pipeline is not first.pipeline
        assert len(spies.rule_builds) == 1
        assert spies.rule_builds[0]["embedding_model"] is first.pipeline.encoder
        assert all(second.fingerprint[k] == first.fingerprint[k] for k in DOC_KEYS)

    @pytest.mark.asyncio
    async def test_an_unchanged_graph_returns_no_handle_and_builds_nothing(self, spies_for):
        spies = spies_for(_inputs(), _inputs())
        first = await _build(None)
        spies.rule_builds.clear(); spies.doc_builds.clear()
        assert await _build(first, generation=2) is None
        assert spies.rule_builds == [] and spies.doc_builds == []

    @pytest.mark.asyncio
    async def test_a_change_to_both_halves_builds_both_and_moves_both_key_sets(self, spies_for):
        spies = spies_for(_inputs(), _inputs(statement="new rule text", chunk_text="new chunk text"))
        first = await _build(None)
        spies.rule_builds.clear(); spies.doc_builds.clear()
        second = await _build(first, generation=2)
        assert len(spies.rule_builds) == 1 and len(spies.doc_builds) == 1
        assert second.pipeline is not first.pipeline and second.documents is not first.documents

    @pytest.mark.asyncio
    async def test_the_graph_is_read_once_per_build_with_documents_included(self, spies_for):
        spies = spies_for(_inputs(), _inputs())
        first = await _build(None)
        await _build(first, generation=2)
        assert spies.load_kwargs == [{"with_documents": True}, {"with_documents": True}]


class TestDocumentFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_failed_first_document_build_still_starts_the_daemon_with_unbuilt_keys(self, spies_for):
        spies = spies_for(_inputs())
        spies.doc_error = RuntimeError("chunk index corrupt")
        handle = await _build(None)
        assert handle is not None
        assert handle.pipeline.kind == "rules"
        assert handle.documents is None
        assert {handle.fingerprint[k] for k in DOC_KEYS} == {"unbuilt"}
        assert [where for where, _ in spies.exceptions] == ["retrieval.documents.build"]
        assert "corrupt" in str(spies.exceptions[0][1])

    @pytest.mark.asyncio
    async def test_a_later_failure_keeps_the_previous_document_half_and_its_keys(self, spies_for):
        spies = spies_for(_inputs(), _inputs(statement="new rule text", chunk_text="new chunk text"))
        first = await _build(None)
        spies.rule_builds.clear()
        spies.doc_error = RuntimeError("boom")
        second = await _build(first, generation=2)
        assert second is not None, "a bad chunk must never block a rule reload"
        assert second.pipeline is not first.pipeline and len(spies.rule_builds) == 1
        assert second.documents is first.documents
        assert {k: second.fingerprint[k] for k in DOC_KEYS} == {k: first.fingerprint[k] for k in DOC_KEYS}
        assert all(second.fingerprint[k] != first.fingerprint[k] for k in ("bm25", "metadata"))
        assert [w for w, _ in spies.exceptions] == ["retrieval.documents.build"]

    @pytest.mark.asyncio
    async def test_the_next_reload_retries_the_failed_document_build(self, spies_for):
        spies = spies_for(_inputs(), _inputs())
        spies.doc_error = RuntimeError("transient")
        degraded = await _build(None)
        assert degraded.documents is None
        spies.doc_error = None
        spies.rule_builds.clear(); spies.doc_builds.clear()
        healed = await _build(degraded, generation=2)
        assert healed is not None, "stored 'unbuilt' keys differ from the graph's, so the next reload must act"
        assert len(spies.doc_builds) == 1 and spies.rule_builds == []
        assert healed.documents is not None and healed.pipeline is degraded.pipeline
        assert all(healed.fingerprint[k] != "unbuilt" for k in DOC_KEYS)

    @pytest.mark.asyncio
    async def test_a_documents_only_failure_still_advances_the_generation_with_the_old_half(self, spies_for):
        spies = spies_for(_inputs(), _inputs(chunk_text="edited chunk words"))
        first = await _build(None)
        spies.doc_error = RuntimeError("boom")
        second = await _build(first, generation=2)
        assert second is not None and second.documents is first.documents and second.pipeline is first.pipeline
        assert {k: second.fingerprint[k] for k in DOC_KEYS} == {k: first.fingerprint[k] for k in DOC_KEYS}

    @pytest.mark.asyncio
    async def test_a_rule_build_failure_still_fails_the_whole_reload(self, spies_for):
        spies = spies_for(_inputs(), _inputs(statement="changed rule"))
        first = await _build(None)
        spies.rule_error = RuntimeError("rule index failed")
        with pytest.raises(RuntimeError, match="rule index failed"):
            await _build(first, generation=2)


class TestInstall:
    def test_the_server_module_declares_a_documents_alias_defaulting_to_none(self):
        assert "_documents" in vars(server), "writ.server must define the _documents alias beside _pipeline"
        assert vars(server)["_documents"] is None or hasattr(vars(server)["_documents"], "query")

    def test_install_assigns_pipeline_trigger_index_and_documents_from_one_handle(self, monkeypatch):
        for name in ("_retrieval", "_pipeline", "_trigger_index", "_documents"):
            monkeypatch.setattr(server, name, getattr(server, name, None), raising=False)
        handle = reload_mod.RetrievalHandle(
            generation=3, pipeline=object(), trigger_index=object(), fingerprint={}, built_at="t",
            documents=object())
        server._install_retrieval(handle)
        assert server._retrieval is handle
        assert server._pipeline is handle.pipeline
        assert server._trigger_index is handle.trigger_index
        assert server._documents is handle.documents

    def test_install_with_no_document_half_clears_the_alias(self, monkeypatch):
        for name in ("_retrieval", "_pipeline", "_trigger_index"):
            monkeypatch.setattr(server, name, getattr(server, name, None), raising=False)
        monkeypatch.setattr(server, "_documents", object(), raising=False)
        server._install_retrieval(reload_mod.RetrievalHandle(1, object(), object(), {}, "t"))
        assert server._documents is None

    def test_install_has_no_await_so_the_three_names_change_together(self):
        src = inspect.getsource(server._install_retrieval)
        assert "await" not in src
        assert not inspect.iscoroutinefunction(server._install_retrieval)
        for assignment in ("_pipeline = handle.pipeline", "_trigger_index = handle.trigger_index",
                           "_documents = handle.documents"):
            assert assignment in src
        assert "global _retrieval, _pipeline, _trigger_index, _documents" in src
