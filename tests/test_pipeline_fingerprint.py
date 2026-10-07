"""Program item 2 (C1): one content hash per source, each moving only with its source.

No graph: PipelineInputs is built by hand, because the claim is about the fingerprint
function, not about what the graph returns.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import writ.retrieval.pipeline as pipeline_mod
from writ.retrieval.pipeline import fingerprint_digest
from writ.retrieval.pipeline import PipelineInputs, pipeline_fingerprint
from writ.retrieval.traversal import AdjacencyCache
from writ.retrieval.trigger_index import MethodologyTriggerIndex

RULE_KEYS = {"index_version", "bm25", "hnsw", "metadata", "edges", "routes", "abstractions"}
# Program item 5 (workstream D): the document half moves only with chunk edits.
DOCUMENT_KEYS = {"documents_bm25", "documents_hnsw", "documents_meta"}
KEYS = RULE_KEYS | DOCUMENT_KEYS


def _chunk(text: str = "alpha beta", breadcrumb: str = "Doc > Part", next_id=None) -> dict:
    return {
        "rule_id": "p:docs/x#0000", "chunk_id": "p:docs/x#0000", "trigger": breadcrumb,
        "statement": text, "tags": "docs x", "body": "", "mandatory": False,
        "node_type": "Chunk", "project": "p", "doc_id": "docs/x", "ordinal": 0,
        "breadcrumb": breadcrumb, "text": text, "title": "Doc", "path": "docs/x",
        "kind": "docs", "next_id": next_id, "prev_id": None,
    }


def _inputs(statement: str = "s", severity: str = "medium", edge: str = "RELATED_TO",
            routes=None, abstractions=None, chunks=None) -> PipelineInputs:
    candidates = [{
        "rule_id": "R-1", "trigger": "t", "statement": statement, "tags": "",
        "body": "", "mandatory": False, "project": "writ", "severity": severity,
    }]
    cache = AdjacencyCache()
    cache._neighbors = {"R-1": [{
        "rule_id": "R-2", "edge_type": edge, "direction": "outgoing",
        "node_type": "Rule", "project": "writ",
    }]}
    return PipelineInputs(
        candidates=candidates,
        rule_metadata={c["rule_id"]: c for c in candidates},
        adjacency_cache=cache,
        abstractions=abstractions or [],
        node_routes=routes,
        **({"chunks": chunks, "chunks_stamp": fingerprint_digest(chunks)} if chunks else {}),
    )


def _moved(a: PipelineInputs, b: PipelineInputs) -> set[str]:
    fa, fb = pipeline_fingerprint(a), pipeline_fingerprint(b)
    return {k for k in fa if fa[k] != fb[k]}


def test_the_keys_are_exactly_the_sources() -> None:
    assert set(pipeline_fingerprint(_inputs())) == KEYS
    assert set(pipeline_mod.DOCUMENT_FINGERPRINT_KEYS) == DOCUMENT_KEYS


def test_a_chunk_edit_beyond_the_embedded_head_moves_bm25_and_meta_but_not_hnsw() -> None:
    from writ.retrieval.documents import CHUNK_EMBED_HEAD_CHARS
    head = "w " * (CHUNK_EMBED_HEAD_CHARS // 2)
    before = _inputs(chunks=[_chunk(text=head + "tail one")])
    after = _inputs(chunks=[_chunk(text=head + "tail two")])
    assert _moved(before, after) == {"documents_bm25", "documents_meta"}


def test_a_chunk_edit_inside_the_head_moves_every_document_key_and_no_rule_key() -> None:
    moved = _moved(_inputs(chunks=[_chunk("alpha")]), _inputs(chunks=[_chunk("omega")]))
    assert moved == DOCUMENT_KEYS


def test_a_neighbour_link_change_moves_only_the_document_meta() -> None:
    moved = _moved(_inputs(chunks=[_chunk()]), _inputs(chunks=[_chunk(next_id="p:docs/x#0001")]))
    assert moved == {"documents_meta"}


def test_a_rule_edit_moves_no_document_key() -> None:
    chunks = [_chunk()]
    moved = _moved(_inputs(chunks=chunks), _inputs(statement="changed", chunks=chunks))
    assert moved == {"bm25", "hnsw", "metadata"}
    assert not moved & DOCUMENT_KEYS


def test_the_document_keys_are_the_persisted_cache_keys_and_the_collector_stamp() -> None:
    from writ.retrieval.documents import document_vector_text
    inputs = _inputs(chunks=[_chunk()])
    fp = pipeline_fingerprint(inputs)
    ids = [c["rule_id"] for c in inputs.chunks]
    assert fp["documents_bm25"] == pipeline_mod._compute_bm25_hash(inputs.chunks)
    assert fp["documents_hnsw"] == pipeline_mod._compute_corpus_hash_from_text(
        ids, [document_vector_text(c) for c in inputs.chunks])
    assert fp["documents_meta"] == inputs.chunks_stamp == fingerprint_digest(inputs.chunks)


def test_the_rule_keys_are_unchanged_by_the_presence_of_chunks() -> None:
    plain, with_chunks = pipeline_fingerprint(_inputs()), pipeline_fingerprint(
        _inputs(chunks=[_chunk()]))
    assert {k: plain[k] for k in RULE_KEYS} == {k: with_chunks[k] for k in RULE_KEYS}


def test_identical_inputs_give_identical_fingerprints() -> None:
    assert pipeline_fingerprint(_inputs()) == pipeline_fingerprint(copy.deepcopy(_inputs()))


def test_the_index_caches_are_keyed_by_their_existing_hashes() -> None:
    inputs = _inputs()
    fp = pipeline_fingerprint(inputs)
    ids = [c["rule_id"] for c in inputs.candidates]
    texts = [f"{c['trigger']} {c['statement']}" for c in inputs.candidates]
    assert fp["bm25"] == pipeline_mod._compute_bm25_hash(inputs.candidates)
    assert fp["hnsw"] == pipeline_mod._compute_corpus_hash_from_text(ids, texts)


def test_a_statement_edit_moves_both_indexes_and_the_metadata_only() -> None:
    assert _moved(_inputs(), _inputs(statement="changed")) == {"bm25", "hnsw", "metadata"}


def test_a_severity_edit_moves_only_the_metadata() -> None:
    assert _moved(_inputs(), _inputs(severity="high")) == {"metadata"}


def test_an_edge_change_moves_only_the_edges() -> None:
    assert _moved(_inputs(), _inputs(edge="CONFLICTS_WITH")) == {"edges"}


def test_a_route_change_moves_only_the_routes() -> None:
    assert _moved(_inputs(), _inputs(routes={"R-1": ["semantic"]})) == {"routes"}


def test_an_abstraction_change_moves_only_the_abstractions() -> None:
    assert _moved(_inputs(), _inputs(abstractions=[{"abstraction_id": "A-1"}])) == {"abstractions"}


def test_the_index_version_moves_only_its_own_key(monkeypatch) -> None:
    before = pipeline_fingerprint(_inputs())
    bm25_key = pipeline_mod._compute_bm25_hash(_inputs().candidates)
    monkeypatch.setattr(pipeline_mod, "RETRIEVAL_INDEX_VERSION",
                        pipeline_mod.RETRIEVAL_INDEX_VERSION + 1)
    after = pipeline_fingerprint(_inputs())
    assert {k for k in before if before[k] != after[k]} == {"index_version"}
    assert pipeline_mod._compute_bm25_hash(_inputs().candidates) == bm25_key, (
        "the index version must never reach a persisted cache key"
    )


def test_a_datetime_valued_property_fingerprints_without_error() -> None:
    inputs = _inputs()
    inputs.rule_metadata["R-1"]["last_seen"] = datetime(2026, 10, 6, tzinfo=timezone.utc)
    assert set(pipeline_fingerprint(inputs)) == KEYS


def test_the_snapshots_are_the_canonical_state() -> None:
    cache = _inputs().adjacency_cache
    assert cache.snapshot() is cache._neighbors
    node = {"id": "SKL-1", "node_type": "Skill", "floor_modes": [], "action_triggers": [],
            "trigger_keywords": ["deploy"], "trigger": "t", "statement": "s",
            "severity": None, "domain": None}
    index = MethodologyTriggerIndex([node])
    assert index.snapshot() is index._nodes
