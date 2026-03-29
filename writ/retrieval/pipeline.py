"""Orchestrates all retrieval stages in sequence.

Stage 1: Domain Filter -- pre-filter to relevant domain subgraph.
Stage 2: BM25 Keyword Filter -- Tantivy sparse retrieval on trigger, statement, tags.
Stage 3: ANN Vector Search -- hnswlib in-process ANN on pre-computed embeddings.
Stage 4: Graph Traversal -- adjacency cache lookup from top-K results.
Stage 5a: First-pass ranking -- RRF + metadata weighting (no graph proximity).
Stage 5b: Graph proximity -- compute proximity scores from top-3 first-pass results.
Stage 5c: Final ranking -- re-score with graph proximity, context budget applied.

The pipeline operates on domain rules only. Mandatory rules (ENF-*, mandatory: true)
are excluded before Stage 1.

Per PERF-IO-001: all indexes pre-warmed at startup. No I/O in the query path.
Per ARCH-DI-001: all dependencies injected via constructor.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from writ.retrieval.embeddings import (
    DEFAULT_ONNX_DIR,
    CachedEncoder,
    HnswlibStore,
    OnnxEmbeddingModel,
    ScoredResult,
)
from writ.retrieval.keyword import KeywordIndex
from writ.retrieval.ranking import (
    RankingWeights,
    apply_authority_preference,
    apply_context_budget,
    compute_score,
    filter_proximity_seeds,
    normalize_ranks,
)
from writ.retrieval.traversal import AdjacencyCache

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

# Preferred ONNX model directory.
_ONNX_DIR = DEFAULT_ONNX_DIR

# Per ARCH-CONST-001
BM25_CANDIDATE_LIMIT = 50
VECTOR_CANDIDATE_LIMIT = 10
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
FIRST_PASS_TOP_N = 3


def compute_graph_proximity(
    candidate_ids: list[str],
    top3_ids: list[str],
    cache: AdjacencyCache,
) -> dict[str, float]:
    """Compute graph proximity scores for candidates relative to top-3 rules.

    Returns dict[rule_id, proximity] where proximity is in {0.0, 0.5, 1.0}.
    Per INV-2: 1.0 = 1-hop neighbor of a top-3 rule, 0.5 = 2-hop only, 0.0 = none.
    Per INV-4: top-3 rules themselves get 0.0 (no self-boost).
    If a candidate is 1-hop to one top-3 and 2-hop to another, max wins.
    """
    top3_set = set(top3_ids)
    proximity: dict[str, float] = {}

    # Collect 1-hop neighbors of all top-3 rules.
    top3_1hop: set[str] = set()
    top3_2hop: set[str] = set()
    for tid in top3_ids:
        for neighbor in cache.get_neighbors(tid):
            nid = neighbor["rule_id"]
            if nid not in top3_set:
                top3_1hop.add(nid)

    # Collect 2-hop neighbors (neighbors of 1-hop, excluding 1-hop and top-3).
    for nid in top3_1hop:
        for neighbor in cache.get_neighbors(nid):
            n2id = neighbor["rule_id"]
            if n2id not in top3_set and n2id not in top3_1hop:
                top3_2hop.add(n2id)

    for rid in candidate_ids:
        if rid in top3_set:
            proximity[rid] = 0.0
        elif rid in top3_1hop:
            proximity[rid] = 1.0
        elif rid in top3_2hop:
            proximity[rid] = 0.5
        else:
            proximity[rid] = 0.0

    return proximity


class RetrievalPipeline:
    """Full 5-stage hybrid retrieval pipeline.

    Built at startup with pre-warmed indexes. Query path is pure in-memory.
    """

    def __init__(
        self,
        keyword_index: KeywordIndex,
        vector_store: HnswlibStore,
        adjacency_cache: AdjacencyCache,
        embedding_model: CachedEncoder,
        rule_metadata: dict[str, dict],
        weights: RankingWeights | None = None,
        authority_preference_threshold: float = 0.0,
    ) -> None:
        self._keyword = keyword_index
        self._vector = vector_store
        self._cache = adjacency_cache
        self._model = embedding_model
        self._metadata = rule_metadata
        self._weights = weights or RankingWeights()
        self._authority_preference_threshold = authority_preference_threshold

    def query(
        self,
        query_text: str,
        domain: str | None = None,
        budget_tokens: int | None = None,
        exclude_rule_ids: list[str] | None = None,
    ) -> dict:
        """Execute the full 5-stage pipeline.

        Returns dict with rules, mode, total_candidates, latency_ms.
        """
        start = time.perf_counter()
        exclude = set(exclude_rule_ids or [])

        # Stage 1: Domain filter.
        # Applied as post-filter on BM25/vector results since indexes
        # contain all non-mandatory rules.

        # Stage 2: BM25 keyword search.
        bm25_results = self._keyword.search(query_text, limit=BM25_CANDIDATE_LIMIT)
        bm25_results = [r for r in bm25_results if r["rule_id"] not in exclude]
        if domain:
            bm25_results = [
                r for r in bm25_results
                if self._metadata.get(r["rule_id"], {}).get("domain", "").lower() == domain.lower()
            ]

        # Stage 3: ANN vector search.
        query_vector = self._model.encode(query_text).tolist()
        vector_results: list[ScoredResult] = self._vector.search(query_vector, k=VECTOR_CANDIDATE_LIMIT)
        vector_results = [r for r in vector_results if r.rule_id not in exclude]
        if domain:
            vector_results = [
                r for r in vector_results
                if self._metadata.get(r.rule_id, {}).get("domain", "").lower() == domain.lower()
            ]

        # Merge candidates from both stages.
        candidate_ids: dict[str, dict] = {}
        bm25_scores = {r["rule_id"]: r["score"] for r in bm25_results}
        vector_scores = {r.rule_id: r.score for r in vector_results}

        all_ids = set(bm25_scores.keys()) | set(vector_scores.keys())
        for rid in all_ids:
            candidate_ids[rid] = {
                "bm25_score": bm25_scores.get(rid, 0.0),
                "vector_score": vector_scores.get(rid, 0.0),
            }

        # Normalize BM25 and vector scores via reciprocal rank.
        if candidate_ids:
            ids_list = list(candidate_ids.keys())
            bm25_raw = [candidate_ids[rid]["bm25_score"] for rid in ids_list]
            vector_raw = [candidate_ids[rid]["vector_score"] for rid in ids_list]
            bm25_norm = normalize_ranks(bm25_raw)
            vector_norm = normalize_ranks(vector_raw)
            for i, rid in enumerate(ids_list):
                candidate_ids[rid]["bm25_norm"] = bm25_norm[i]
                candidate_ids[rid]["vector_norm"] = vector_norm[i]

        # Stage 4: Graph traversal enrichment (from adjacency cache).
        enrichment = self._cache.get_enrichment(list(candidate_ids.keys()))

        # Stage 5a: First-pass ranking (without graph proximity, INV-4).
        fp_bm25, fp_vec, fp_sev, fp_conf = self._weights.first_pass_weights()
        first_pass_weights = RankingWeights(
            w_bm25=fp_bm25, w_vector=fp_vec, w_severity=fp_sev, w_confidence=fp_conf, w_graph=0.0,
        )
        first_pass_scores: list[tuple[str, float]] = []
        for rid, scores in candidate_ids.items():
            meta = self._metadata.get(rid, {})
            fp_score = compute_score(
                bm25_norm=scores.get("bm25_norm", 0.0),
                vector_norm=scores.get("vector_norm", 0.0),
                severity=meta.get("severity", "medium"),
                confidence=meta.get("confidence", "production-validated"),
                weights=first_pass_weights,
            )
            first_pass_scores.append((rid, fp_score))

        first_pass_scores.sort(key=lambda x: x[1], reverse=True)

        # Phase 3c: exclude ai-provisional from proximity seeding.
        first_pass_with_auth = [
            (rid, score, self._metadata.get(rid, {}).get("authority", "human"))
            for rid, score in first_pass_scores
        ]
        top3_ids = filter_proximity_seeds(first_pass_with_auth, FIRST_PASS_TOP_N)

        # Stage 5b: Compute graph proximity from top-3.
        all_candidate_list = list(candidate_ids.keys())
        proximity = compute_graph_proximity(all_candidate_list, top3_ids, self._cache)

        # Stage 5c: Final ranking with graph proximity.
        scored_rules: list[dict] = []
        for rid, scores in candidate_ids.items():
            meta = self._metadata.get(rid, {})
            final_score = compute_score(
                bm25_norm=scores.get("bm25_norm", 0.0),
                vector_norm=scores.get("vector_norm", 0.0),
                severity=meta.get("severity", "medium"),
                confidence=meta.get("confidence", "production-validated"),
                graph_proximity=proximity.get(rid, 0.0),
                weights=self._weights,
            )
            rule_entry = {
                "rule_id": rid,
                "score": round(final_score, 4),
                "authority": meta.get("authority", "human"),
                "statement": meta.get("statement", ""),
                "trigger": meta.get("trigger", ""),
                "violation": meta.get("violation", ""),
                "pass_example": meta.get("pass_example", ""),
                "rationale": meta.get("rationale", ""),
                "relationships": enrichment.get(rid, []),
            }
            scored_rules.append(rule_entry)

        # Sort by score descending.
        scored_rules.sort(key=lambda r: r["score"], reverse=True)

        # Phase 3b: hard authority preference -- human outranks ai-provisional.
        scored_rules = apply_authority_preference(
            scored_rules, self._authority_preference_threshold,
        )

        # Apply context budget.
        trimmed, mode = apply_context_budget(scored_rules, budget_tokens)

        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "rules": trimmed,
            "mode": mode,
            "total_candidates": len(candidate_ids),
            "latency_ms": round(elapsed_ms, 3),
        }


async def build_pipeline(
    db: Neo4jConnection,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    weights: RankingWeights | None = None,
    embedding_model: object | None = None,
) -> RetrievalPipeline:
    """Build the full pipeline with pre-warmed indexes.

    Called once at service startup. Per PERF-LAZY-001: expensive loading
    happens here, not at query time.

    Model selection: ONNX Runtime preferred (no PyTorch dependency).
    Falls back to SentenceTransformer if ONNX model not exported.
    """
    # Load all non-mandatory rules from Neo4j.
    query = """
        MATCH (r:Rule)
        WHERE r.mandatory IS NULL OR r.mandatory = false
        RETURN r
    """
    rules: list[dict] = []
    async with db._driver.session(database=db._database) as session:
        result = await session.run(query)
        async for record in result:
            rules.append(dict(record["r"]))

    # Build metadata lookup.
    rule_metadata: dict[str, dict] = {r["rule_id"]: r for r in rules}

    # Build BM25 index (Stage 2).
    keyword_index = KeywordIndex()
    keyword_index.build(rules)

    # Build vector index (Stage 3).
    texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in rules]
    rule_ids = [r["rule_id"] for r in rules]

    # Auto-detect ONNX model when no model is passed.
    onnx_model = None
    if embedding_model is None:
        try:
            onnx_model = OnnxEmbeddingModel(_ONNX_DIR)
        except (FileNotFoundError, ImportError):
            pass

    if onnx_model is not None:
        # ONNX for everything: bulk encode at startup + cached single encode at query time.
        # No PyTorch/sentence-transformers in the runtime path.
        embeddings = onnx_model.encode_batch(texts)
        query_encoder = CachedEncoder(onnx_model)
    elif embedding_model is not None:
        # Pre-loaded model passed in (tests, server reuse).
        raw_model = embedding_model
        if isinstance(embedding_model, CachedEncoder):
            raw_model = embedding_model._model
        if isinstance(raw_model, OnnxEmbeddingModel):
            embeddings = raw_model.encode_batch(texts)
        else:
            embeddings = raw_model.encode(texts).tolist()
        query_encoder = (
            embedding_model if isinstance(embedding_model, CachedEncoder)
            else CachedEncoder(embedding_model)
        )
    else:
        # Fallback: SentenceTransformer (imports PyTorch -- avoid in production).
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)
        embeddings = model.encode(texts).tolist()
        query_encoder = CachedEncoder(model)

    vector_store = HnswlibStore(dimensions=len(embeddings[0]) if embeddings else 384)
    vector_store.build_index(rule_ids, embeddings)

    # Build adjacency cache (Stage 4).
    adjacency_cache = AdjacencyCache()
    await adjacency_cache.build_from_db(db)

    return RetrievalPipeline(
        keyword_index=keyword_index,
        vector_store=vector_store,
        adjacency_cache=adjacency_cache,
        embedding_model=query_encoder,
        rule_metadata=rule_metadata,
        weights=weights,
    )
