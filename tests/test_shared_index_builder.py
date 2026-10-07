"""Program item 5 (workstream D), phase 2: the shared, parametrized index builder.

assemble_pipeline's lower layer (BM25 load-or-build, HNSW load-or-build, encoder resolution)
is extracted into writ.retrieval.pipeline.build_search_indexes(candidates, location, text_of,
*, embedding_model=None, model_name=..., dimensions=384), parametrized by an IndexLocation
(cache root, HNSW dir, metric prefix) and a vector-text builder. The rule pipeline calls it
with the rule parameters; the document pipeline calls it with its own. This module pins:

  * the rule indexes are exactly what they were: same BM25 and HNSW cache keys, same build
    order, same metric names (capability 12), proven against the key functions the
    pre-extraction code used rather than against a recording of the old code
  * separate cache dirs never collide, and a document build never writes a rule cache file
    (capability 13); the document metrics carry the documents_ prefix
  * the injected encoder object is reused, no OnnxEmbeddingModel is constructed, and an empty
    chunk corpus builds nothing and encodes nothing (capability 14)
  * merge_ranked_hits is the one ordered union both pipelines share (the method delegates)

No graph. The real KeywordIndex and HnswlibStore run over tmp_path; the encoder is the
deterministic stand-in of tests/_document_fixtures.py wrapped in the real CachedEncoder.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import writ.retrieval.pipeline as pipeline_mod
from tests._document_fixtures import BagEncoder, make_document
from writ.retrieval.embeddings import CachedEncoder, ScoredResult
from writ.retrieval.keyword import KeywordIndex
from writ.retrieval.pipeline import (
    PipelineInputs,
    _compute_bm25_hash,
    _compute_corpus_hash_from_text,
    assemble_pipeline,
)
from writ.retrieval.traversal import AdjacencyCache


def _rules() -> list[dict]:
    return [
        {"rule_id": "T-SQL-001", "trigger": "When writing SQL strings", "statement":
         "Use parameterized queries only", "tags": "security sql", "body": "", "mandatory": False,
         "node_type": "Rule", "project": "writ"},
        {"rule_id": "T-ERR-002", "trigger": "When catching exceptions", "statement":
         "Errors propagate with context", "tags": "errors", "body": "", "mandatory": False,
         "node_type": "Rule", "project": "writ"},
        {"rule_id": "T-LOG-003", "trigger": "When logging user input", "statement":
         "Strip control characters from logged values", "tags": "logging", "body": "",
         "mandatory": False, "node_type": "Rule", "project": "writ"},
    ]


def _chunks() -> list[dict]:
    return (make_document("p", "docs/adr/ADR-budget.md", [
        ("Budget ADR > Sections", "The documents section has its own token budget of 2700 tokens."),
        ("Budget ADR > Hooks", "A fifth hook prints the fenced documents block each prompt."),
    ], kind="adr")
        + make_document("p", "docs/guide.md", [
            ("Guide > Install", "Run the bootstrap script then start the daemon service."),
        ]))


def _rule_text(c: dict) -> str:
    return f"{c.get('trigger', '')} {c.get('statement', '')}"


@pytest.fixture()
def encoder() -> CachedEncoder:
    return CachedEncoder(BagEncoder())


@pytest.fixture()
def emitted(monkeypatch) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    monkeypatch.setattr(pipeline_mod, "emit",
                        lambda stream, event, sid, mode, /, **f: rows.append((event, f)))
    return rows


@pytest.fixture()
def no_second_model(monkeypatch):
    """Constructing an OnnxEmbeddingModel anywhere in the pipeline module fails the test."""
    built: list[tuple] = []

    class _Boom:
        MAX_LENGTH = 128

        def __init__(self, *a, **k):
            built.append((a, k))
            raise AssertionError("a second OnnxEmbeddingModel was constructed")

    monkeypatch.setattr(pipeline_mod, "OnnxEmbeddingModel", _Boom)
    return built


def _builder():
    return pipeline_mod.build_search_indexes, pipeline_mod.IndexLocation


def _tree(root: Path) -> dict[str, str]:
    """relative path -> sha256 of every file under root."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class TestRuleIndexesAreUnchanged:
    def test_the_bm25_and_hnsw_cache_keys_are_the_pre_extraction_hashes(self, tmp_path, encoder, emitted):
        build, Location = _builder()
        rules = _rules()
        build(rules, Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
              embedding_model=encoder)
        sidecar = next((tmp_path / "bm25").glob("gen-*/" + pipeline_mod._BM25_SIDECAR))
        import json
        assert json.loads(sidecar.read_text())["corpus_hash"] == _compute_bm25_hash(rules)
        hnsw = json.loads((tmp_path / "hnsw" / "writ_hnsw.json").read_text())
        assert hnsw["corpus_hash"] == _compute_corpus_hash_from_text(
            [r["rule_id"] for r in rules], [_rule_text(r) for r in rules])

    def test_the_rule_vector_text_is_trigger_space_statement(self):
        assert pipeline_mod._rule_vector_text(_rules()[0]) == (
            "When writing SQL strings Use parameterized queries only")
        assert pipeline_mod._rule_vector_text({"rule_id": "X"}) == " "

    def test_vector_corpus_takes_a_text_builder_and_defaults_to_the_rule_text(self):
        rules = _rules()
        ids, texts = pipeline_mod._vector_corpus(rules)
        assert ids == [r["rule_id"] for r in rules]
        assert texts == [_rule_text(r) for r in rules]
        ids2, texts2 = pipeline_mod._vector_corpus(rules, lambda c: c["rule_id"].lower())
        assert ids2 == ids and texts2 == [r["rule_id"].lower() for r in rules]

    def test_the_corpus_is_encoded_once_in_candidate_order(self, tmp_path, encoder, emitted):
        build, Location = _builder()
        rules = _rules()
        build(rules, Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
              embedding_model=encoder)
        raw = encoder._model
        assert raw.batch_calls == [[_rule_text(r) for r in rules]]

    def test_rule_metric_names_have_no_prefix(self, tmp_path, encoder, emitted):
        build, Location = _builder()
        build(_rules(), Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
              embedding_model=encoder)
        assert [e for e, _ in emitted] == ["bm25_cache", "hnsw_cache"]
        assert dict(emitted)["bm25_cache"] == {"outcome": "miss"}
        assert dict(emitted)["hnsw_cache"]["outcome"] == "miss"

    def test_the_built_index_answers_like_a_directly_built_keyword_index(self, tmp_path, encoder, emitted):
        build, Location = _builder()
        rules = _rules()
        built = build(rules, Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
                      embedding_model=encoder)
        direct = KeywordIndex()
        direct.build(rules)
        for query in ("parameterized queries", "exceptions context", "control characters logged"):
            assert built.keyword_index.search(query, limit=5) == direct.search(query, limit=5)

    def test_assemble_pipeline_builds_the_same_caches_through_the_builder(self, tmp_path, encoder, emitted, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        rules = _rules()
        inputs = PipelineInputs(candidates=rules, rule_metadata={r["rule_id"]: r for r in rules},
                                adjacency_cache=AdjacencyCache(), abstractions=[], node_routes=None)
        pipe = assemble_pipeline(inputs, embedding_model=encoder)
        assert pipe.encoder is encoder
        assert [e for e, _ in emitted] == ["bm25_cache", "hnsw_cache"]
        import json
        assert json.loads((tmp_path / "hnsw" / "writ_hnsw.json").read_text())["corpus_hash"] == \
            _compute_corpus_hash_from_text([r["rule_id"] for r in rules], [_rule_text(r) for r in rules])
        assert (tmp_path / "bm25" / "CURRENT").is_file()

    def test_a_warm_second_build_hits_both_caches_and_does_not_encode_the_corpus(self, tmp_path, emitted):
        build, Location = _builder()
        first, second = CachedEncoder(BagEncoder()), CachedEncoder(BagEncoder())
        loc = Location(tmp_path, str(tmp_path / "hnsw"))
        build(_rules(), loc, pipeline_mod._rule_vector_text, embedding_model=first)
        emitted.clear()
        build(_rules(), loc, pipeline_mod._rule_vector_text, embedding_model=second)
        assert dict(emitted)["bm25_cache"] == {"outcome": "hit"}
        assert dict(emitted)["hnsw_cache"]["outcome"] == "hit"
        assert second._model.batch_calls == []

    def test_the_default_dimensions_constant_is_384(self):
        assert pipeline_mod.EMBEDDING_DIMENSIONS == 384


class TestSeparateCacheDirs:
    def test_document_indexes_persist_under_documents_bm25_and_hnsw_beside_the_rule_caches(
            self, tmp_path, encoder, emitted, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        docs_mod.assemble_document_pipeline(_chunks(), embedding_model=encoder)
        assert (tmp_path / "documents" / "bm25" / "CURRENT").is_file()
        assert (tmp_path / "documents" / "hnsw" / "writ_hnsw.bin").is_file()
        assert (tmp_path / "documents" / "hnsw" / "writ_hnsw.json").is_file()

    def test_document_index_location_reads_the_patched_cache_dir_through_the_pipeline_module(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        loc = docs_mod.document_index_location()
        assert Path(loc.bm25_root) == tmp_path / "documents"
        assert Path(loc.hnsw_dir) == tmp_path / "documents" / "hnsw"
        assert loc.metric_prefix == "documents_"

    def test_a_document_build_never_writes_a_rule_cache_file(self, tmp_path, encoder, emitted, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        build, Location = _builder()
        build(_rules(), Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
              embedding_model=encoder)
        before = {k: v for k, v in _tree(tmp_path).items()}
        docs_mod.assemble_document_pipeline(_chunks(), embedding_model=encoder)
        after = _tree(tmp_path)
        assert {k: v for k, v in after.items() if not k.startswith("documents/")} == before

    def test_a_rule_rebuild_never_touches_the_document_caches(self, tmp_path, encoder, emitted, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        docs_mod.assemble_document_pipeline(_chunks(), embedding_model=encoder)
        documents_before = _tree(tmp_path / "documents")
        build, Location = _builder()
        build(_rules(), Location(tmp_path, str(tmp_path / "hnsw")), pipeline_mod._rule_vector_text,
              embedding_model=encoder)
        assert _tree(tmp_path / "documents") == documents_before

    def test_document_metrics_carry_the_documents_prefix_and_rule_metrics_do_not(
            self, tmp_path, encoder, emitted, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        docs_mod.assemble_document_pipeline(_chunks(), embedding_model=encoder)
        assert [e for e, _ in emitted] == ["documents_bm25_cache", "documents_hnsw_cache"]

    def test_the_prefix_is_the_only_difference_between_two_locations_metric_names(
            self, tmp_path, encoder, emitted):
        build, Location = _builder()
        build(_chunks(), Location(tmp_path / "a", str(tmp_path / "a" / "h"), "zz_"),
              lambda c: c["breadcrumb"], embedding_model=encoder)
        assert [e for e, _ in emitted] == ["zz_bm25_cache", "zz_hnsw_cache"]


class TestEncoderIsShared:
    def test_the_injected_encoder_object_is_the_one_returned(self, tmp_path, encoder, emitted, no_second_model):
        build, Location = _builder()
        built = build(_rules(), Location(tmp_path, str(tmp_path / "hnsw")),
                      pipeline_mod._rule_vector_text, embedding_model=encoder)
        assert built.encoder is encoder
        assert no_second_model == []

    def test_the_document_pipeline_holds_the_rule_pipelines_encoder_and_builds_no_model(
            self, tmp_path, encoder, emitted, no_second_model, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        rules = _rules()
        rule_pipe = assemble_pipeline(
            PipelineInputs(candidates=rules, rule_metadata={r["rule_id"]: r for r in rules},
                           adjacency_cache=AdjacencyCache(), abstractions=[], node_routes=None),
            embedding_model=encoder)
        doc_pipe = docs_mod.assemble_document_pipeline(_chunks(), embedding_model=rule_pipe.encoder)
        assert doc_pipe.encoder is rule_pipe.encoder is encoder
        assert no_second_model == []

    def test_an_empty_chunk_corpus_builds_no_index_and_encodes_nothing(
            self, tmp_path, emitted, no_second_model, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "get_hnsw_cache_dir", lambda *a, **k: str(tmp_path / "hnsw"))
        docs_mod = importlib.import_module("writ.retrieval.documents")
        spy = CachedEncoder(BagEncoder())
        built: list = []
        monkeypatch.setattr(pipeline_mod, "build_search_indexes", lambda *a, **k: built.append(a))
        pipe = docs_mod.assemble_document_pipeline([], embedding_model=spy)
        assert built == []
        assert not spy._model.encoded_anything
        assert not (tmp_path / "documents").exists()
        assert emitted == []
        assert pipe.query("anything at all", project="p", exclude_ids=())["chunks"] == []
        assert not spy._model.encoded_anything, "an empty pipeline must not even encode the query"


class TestChunkIndexing:
    def test_chunk_text_is_searchable_in_the_statement_field_and_a_tail_only_term_is_found(
            self, tmp_path, encoder, emitted):
        build, Location = _builder()
        tail = "filler " * 120 + "zyxwvuterm"
        chunks = make_document("p", "docs/x.md", [("Doc > Long", tail), ("Doc > Other", "unrelated words")])
        built = build(chunks, Location(tmp_path, str(tmp_path / "h")),
                      lambda c: c["breadcrumb"], embedding_model=encoder)
        hits = [r["rule_id"] for r in built.keyword_index.search("zyxwvuterm", limit=5)]
        assert hits == [chunks[0]["rule_id"]]

    def test_the_breadcrumb_is_the_boosted_trigger(self, tmp_path, encoder, emitted):
        build, Location = _builder()
        chunks = make_document("p", "docs/x.md", [
            ("Doc > Quokka", "plain body text one"),
            ("Doc > Other", "quokka appears once in the body text"),
        ])
        built = build(chunks, Location(tmp_path, str(tmp_path / "h")),
                      lambda c: c["breadcrumb"], embedding_model=encoder)
        hits = [r["rule_id"] for r in built.keyword_index.search("quokka", limit=5)]
        assert hits[0] == chunks[0]["rule_id"], "the breadcrumb match must outrank a single body mention"

    def test_the_vector_text_is_the_breadcrumb_plus_the_embed_head(self):
        docs_mod = importlib.import_module("writ.retrieval.documents")
        head = docs_mod.CHUNK_EMBED_HEAD_CHARS
        assert head == pipeline_mod.OnnxEmbeddingModel.MAX_LENGTH * 4 == 512
        chunk = {"breadcrumb": "Doc > Part", "text": "t" * 2000}
        assert docs_mod.document_vector_text(chunk) == "Doc > Part\n" + "t" * head

    def test_an_edit_beyond_the_head_moves_the_bm25_hash_but_not_the_hnsw_hash(self):
        docs_mod = importlib.import_module("writ.retrieval.documents")
        head = docs_mod.CHUNK_EMBED_HEAD_CHARS
        a = make_document("p", "docs/x.md", [("Doc > A", "h" * head + "tail one")])
        b = make_document("p", "docs/x.md", [("Doc > A", "h" * head + "tail two")])
        assert _compute_bm25_hash(a) != _compute_bm25_hash(b)
        ids = [c["rule_id"] for c in a]
        assert _compute_corpus_hash_from_text(ids, [docs_mod.document_vector_text(c) for c in a]) == \
            _compute_corpus_hash_from_text(ids, [docs_mod.document_vector_text(c) for c in b])

    def test_an_edit_inside_the_head_moves_both_hashes(self):
        docs_mod = importlib.import_module("writ.retrieval.documents")
        a = make_document("p", "docs/x.md", [("Doc > A", "first words here")])
        b = make_document("p", "docs/x.md", [("Doc > A", "other words here")])
        ids = [c["rule_id"] for c in a]
        assert _compute_bm25_hash(a) != _compute_bm25_hash(b)
        assert _compute_corpus_hash_from_text(ids, [docs_mod.document_vector_text(c) for c in a]) != \
            _compute_corpus_hash_from_text(ids, [docs_mod.document_vector_text(c) for c in b])


class TestMergeRankedHits:
    @staticmethod
    def _inputs():
        bm25 = [{"rule_id": "B", "score": 9.0}, {"rule_id": "A", "score": 5.0}]
        vector = [ScoredResult(rule_id="A", score=0.81), ScoredResult(rule_id="C", score=0.44)]
        return bm25, vector

    def test_the_union_is_ordered_bm25_rank_then_vector_only_rank(self):
        merged = pipeline_mod.merge_ranked_hits(*self._inputs())
        assert list(merged) == ["B", "A", "C"]

    def test_each_candidate_carries_the_raw_cosine_or_none_for_a_keyword_only_hit(self):
        merged = pipeline_mod.merge_ranked_hits(*self._inputs())
        assert merged["A"]["similarity"] == 0.81
        assert merged["C"]["similarity"] == 0.44
        assert merged["B"]["similarity"] is None

    def test_scores_and_normalized_ranks_are_attached(self):
        merged = pipeline_mod.merge_ranked_hits(*self._inputs())
        assert merged["B"]["bm25_score"] == 9.0 and merged["B"]["vector_score"] == 0.0
        assert merged["A"]["vector_score"] == 0.81
        for entry in merged.values():
            assert {"bm25_norm", "vector_norm"} <= set(entry)

    def test_no_hits_gives_an_empty_mapping(self):
        assert pipeline_mod.merge_ranked_hits([], []) == {}

    def test_the_pipeline_method_is_a_delegate_with_an_identical_result(self):
        bm25, vector = self._inputs()
        stub = SimpleNamespace()
        direct = pipeline_mod.merge_ranked_hits(bm25, vector)
        via_method = pipeline_mod.RetrievalPipeline._merge_and_normalize(stub, bm25, vector)
        assert via_method == direct
        assert list(via_method) == list(direct)
