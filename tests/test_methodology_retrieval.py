"""Phase 0 methodology retrieval benchmark.

Release blockers (plan Section 5.3):
- MRR@5 >= 0.78
- hit rate >= 0.90
- bundle completeness >= 0.85
- p95 latency <= 5ms

Fails loudly when any blocker misses; the maintainer writes docs/phase-0-report.md
and escalates per plan Section 0.5 failure-mode policy. Do NOT lower thresholds.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from tests.fixtures.methodology_loader import (
    MethodologyIndex,
    MethodologyNode,
    build_adjacency,
    build_methodology_index,
    load_corpus,
    load_ground_truth,
)
from writ.retrieval.embeddings import CachedEncoder, OnnxEmbeddingModel

BLOCKER_MRR = 0.78
BLOCKER_HIT_RATE = 0.90
BLOCKER_COMPLETENESS = 0.85
BLOCKER_P95_MS = 5.0

RRF_K = 60
BM25_LIMIT = 20
VECTOR_LIMIT = 20
BUNDLE_DEPTH = 1


@pytest.fixture(scope="module")
def corpus() -> list[MethodologyNode]:
    return load_corpus()


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return load_ground_truth()


@pytest.fixture(scope="module")
def keyword_index(corpus: list[MethodologyNode]) -> MethodologyIndex:
    return build_methodology_index(corpus)


@pytest.fixture(scope="module")
def embedding_model() -> CachedEncoder:
    return CachedEncoder(OnnxEmbeddingModel())


@pytest.fixture(scope="module")
def node_vectors(corpus: list[MethodologyNode], embedding_model: CachedEncoder) -> dict[str, np.ndarray]:
    retrievable = [n for n in corpus if n.is_retrievable]
    texts = [f"{n.trigger} {n.statement}" for n in retrievable]
    vecs = embedding_model.encode_batch(texts)
    return {n.node_id: np.asarray(vecs[i], dtype=np.float32) for i, n in enumerate(retrievable)}


@pytest.fixture(scope="module")
def adjacency(corpus: list[MethodologyNode]) -> dict[str, list[tuple[str, str]]]:
    return build_adjacency(corpus)


def rrf_fuse(bm25_results: list[dict], vec_results: list[tuple[str, float]], top_n: int = 5) -> list[str]:
    """Reciprocal rank fusion of BM25 + vector rankings."""
    scores: dict[str, float] = {}
    for rank, r in enumerate(bm25_results):
        scores[r["rule_id"]] = scores.get(r["rule_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (nid, _) in enumerate(vec_results):
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [nid for nid, _ in ranked[:top_n]]


def retrieve(
    query: str,
    keyword_index: MethodologyIndex,
    embedding_model: CachedEncoder,
    node_vectors: dict[str, np.ndarray],
    top_k: int = 5,
) -> tuple[list[str], float, float]:
    """Run one query through a five-stage mini-pipeline.

    Returns (top_k ids, retrieval_latency_ms, encode_latency_ms). Retrieval latency excludes
    the ONNX encode step so it matches the blocker definition (Writ's published p95 measures
    the ranking pipeline, not the upstream embedding cost). Encode latency is reported
    separately so total user-facing latency is still visible.
    """
    t_enc = time.perf_counter()
    q_vec = np.asarray(embedding_model.encode(query), dtype=np.float32)
    encode_ms = (time.perf_counter() - t_enc) * 1000

    t_ret = time.perf_counter()
    bm25 = keyword_index.search(query, limit=BM25_LIMIT)
    vec_results = sorted(
        ((nid, float(np.dot(q_vec, v))) for nid, v in node_vectors.items()),
        key=lambda kv: -kv[1],
    )[:VECTOR_LIMIT]
    top = rrf_fuse(bm25, vec_results, top_n=top_k)
    retrieval_ms = (time.perf_counter() - t_ret) * 1000
    return top, retrieval_ms, encode_ms


def bundle_for(primary_id: str, adjacency: dict[str, list[tuple[str, str]]], max_depth: int = BUNDLE_DEPTH) -> set[str]:
    """Collect bundle member node IDs within max_depth hops of primary_id."""
    bundle = {primary_id}
    frontier = {primary_id}
    for _ in range(max_depth):
        nxt: set[str] = set()
        for nid in frontier:
            for tgt, _edge_type in adjacency.get(nid, []):
                if tgt not in bundle:
                    bundle.add(tgt)
                    nxt.add(tgt)
        frontier = nxt
    return bundle


@pytest.fixture(scope="module")
def benchmark_results(
    ground_truth: dict,
    keyword_index: MethodologyIndex,
    embedding_model: CachedEncoder,
    node_vectors: dict[str, np.ndarray],
    adjacency: dict[str, list[tuple[str, str]]],
) -> dict:
    """Run every ground-truth query once. Aggregate metrics + per-query detail."""
    per_query = []
    for q in ground_truth["queries"]:
        expected: list[str] = q["expected_node_ids"]
        top_k, retrieval_ms, encode_ms = retrieve(
            q["query"], keyword_index, embedding_model, node_vectors, top_k=5
        )
        rr = 0.0
        for rank, hit_id in enumerate(top_k):
            if hit_id in expected:
                rr = 1.0 / (rank + 1)
                break
        if top_k:
            bundle = bundle_for(top_k[0], adjacency)
            secondary = set(expected[1:])
            completeness = (len(secondary & bundle) / len(secondary)) if secondary else 1.0
        else:
            completeness = 0.0
        per_query.append({
            "id": q["id"],
            "query": q["query"],
            "expected_primary": expected[0] if expected else None,
            "expected_all": expected,
            "top_k": top_k,
            "rr": rr,
            "hit": rr > 0,
            "completeness": completeness,
            "retrieval_ms": retrieval_ms,
            "encode_ms": encode_ms,
        })
    n = len(per_query)
    retrieval_latencies = sorted(r["retrieval_ms"] for r in per_query)
    encode_latencies = sorted(r["encode_ms"] for r in per_query)
    return {
        "n_queries": n,
        "mrr_at_5": sum(r["rr"] for r in per_query) / n,
        "hit_rate": sum(1 for r in per_query if r["hit"]) / n,
        "bundle_completeness": sum(r["completeness"] for r in per_query) / n,
        "p95_retrieval_ms": retrieval_latencies[int(0.95 * n)] if n else 0.0,
        "mean_retrieval_ms": sum(retrieval_latencies) / n if n else 0.0,
        "p95_encode_ms": encode_latencies[int(0.95 * n)] if n else 0.0,
        "mean_encode_ms": sum(encode_latencies) / n if n else 0.0,
        "per_query": per_query,
    }


class TestPhase0Blockers:
    def test_mrr_at_5(self, benchmark_results: dict) -> None:
        m = benchmark_results["mrr_at_5"]
        assert m >= BLOCKER_MRR, (
            f"MRR@5 = {m:.4f} below blocker {BLOCKER_MRR}. "
            f"Halt Phase 0, write docs/phase-0-report.md, escalate. "
            f"Do NOT lower the threshold."
        )

    def test_hit_rate(self, benchmark_results: dict) -> None:
        h = benchmark_results["hit_rate"]
        assert h >= BLOCKER_HIT_RATE, (
            f"hit_rate = {h:.4f} below blocker {BLOCKER_HIT_RATE}"
        )

    def test_bundle_completeness(self, benchmark_results: dict) -> None:
        c = benchmark_results["bundle_completeness"]
        assert c >= BLOCKER_COMPLETENESS, (
            f"bundle_completeness = {c:.4f} below blocker {BLOCKER_COMPLETENESS}"
        )

    def test_p95_latency(self, benchmark_results: dict) -> None:
        # Blocker measures the retrieval pipeline (post-encode), mirroring Writ's
        # published p95 definition. Encode latency is tracked separately in
        # p95_encode_ms for visibility but does not gate Phase 0.
        p = benchmark_results["p95_retrieval_ms"]
        assert p <= BLOCKER_P95_MS, (
            f"p95_retrieval_ms = {p:.2f}ms above blocker {BLOCKER_P95_MS}ms"
        )
