"""Orchestrates all 5 retrieval stages in sequence.

Stage 1: Domain Filter -- pre-filter to relevant domain subgraph.
Stage 2: BM25 Keyword Filter -- Tantivy sparse retrieval on trigger, statement, tags.
Stage 3: ANN Vector Search -- hnswlib in-process ANN on pre-computed embeddings.
Stage 4: Graph Traversal -- adjacency cache lookup from top-K results.
Stage 5: Ranking & Return -- RRF + metadata weighting, context budget applied.

The pipeline operates on domain rules only. Mandatory rules (ENF-*, mandatory: true)
are excluded before Stage 1.

Per PERF-IO-001: all indexes pre-warmed at startup. No I/O in the query path.
Per ARCH-DI-001: all dependencies injected via constructor.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sentence_transformers import SentenceTransformer

from writ.retrieval.embeddings import HnswlibStore, ScoredResult
from writ.retrieval.keyword import KeywordIndex
from writ.retrieval.ranking import (
    RankingWeights,
    apply_context_budget,
    compute_score,
    normalize_ranks,
)
from writ.retrieval.traversal import AdjacencyCache

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

# Per ARCH-CONST-001
BM25_CANDIDATE_LIMIT = 50
VECTOR_CANDIDATE_LIMIT = 10
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class RetrievalPipeline:
    """Full 5-stage hybrid retrieval pipeline.

    Built at startup with pre-warmed indexes. Query path is pure in-memory.
    """

    def __init__(
        self,
        keyword_index: KeywordIndex,
        vector_store: HnswlibStore,
        adjacency_cache: AdjacencyCache,
        embedding_model: SentenceTransformer,
        rule_metadata: dict[str, dict],
        weights: RankingWeights | None = None,
    ) -> None:
        self._keyword = keyword_index
        self._vector = vector_store
        self._cache = adjacency_cache
        self._model = embedding_model
        self._metadata = rule_metadata
        self._weights = weights or RankingWeights()

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

        # Stage 5: Ranking.
        scored_rules: list[dict] = []
        for rid, scores in candidate_ids.items():
            meta = self._metadata.get(rid, {})
            final_score = compute_score(
                bm25_norm=scores.get("bm25_norm", 0.0),
                vector_norm=scores.get("vector_norm", 0.0),
                severity=meta.get("severity", "medium"),
                confidence=meta.get("confidence", "production-validated"),
                weights=self._weights,
            )
            rule_entry = {
                "rule_id": rid,
                "score": round(final_score, 4),
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
) -> RetrievalPipeline:
    """Build the full pipeline with pre-warmed indexes.

    Called once at service startup. Per PERF-LAZY-001: expensive loading
    happens here, not at query time.
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
    model = SentenceTransformer(model_name)
    texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in rules]
    embeddings = model.encode(texts).tolist()
    rule_ids = [r["rule_id"] for r in rules]

    vector_store = HnswlibStore(dimensions=len(embeddings[0]) if embeddings else 384)
    vector_store.build_index(rule_ids, embeddings)

    # Build adjacency cache (Stage 4).
    adjacency_cache = AdjacencyCache()
    await adjacency_cache.build_from_db(db)

    return RetrievalPipeline(
        keyword_index=keyword_index,
        vector_store=vector_store,
        adjacency_cache=adjacency_cache,
        embedding_model=model,
        rule_metadata=rule_metadata,
        weights=weights,
    )
