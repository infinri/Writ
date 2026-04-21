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

import hashlib
import logging
import time
from typing import TYPE_CHECKING

from writ.config import get_hnsw_cache_dir
from writ.retrieval.embeddings import (
    DEFAULT_ONNX_DIR,
    CachedEncoder,
    HnswlibStore,
    OnnxEmbeddingModel,
    ScoredResult,
)

_logger = logging.getLogger(__name__)
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


STICKY_TIEBREAK_THRESHOLD = 0.02

# Small epsilon to handle floating point imprecision in threshold comparisons.
# Without this, 0.92 - 0.90 = 0.020000000000000018 would exceed 0.02.
_TIEBREAK_EPSILON = 1e-9


def _apply_sticky_tiebreak(
    scored_rules: list[dict],
    prefer_rule_ids: list[str],
) -> list[dict]:
    """Reorder adjacent rules within STICKY_TIEBREAK_THRESHOLD to match prefer_rule_ids order.

    This is a tie-breaker only: it never overrides genuine relevance differences
    exceeding the threshold. Rules not present in the result set are ignored
    (never promoted into the list).

    The algorithm: build groups of consecutive rules whose scores are all within
    the threshold of the group's maximum score, then sort each group by the
    order in prefer_rule_ids (non-preferred rules keep their original position
    within the group).
    """
    if not scored_rules or not prefer_rule_ids:
        return scored_rules

    pref_index = {rid: i for i, rid in enumerate(prefer_rule_ids)}
    n = len(scored_rules)
    result: list[dict] = []
    i = 0

    while i < n:
        # Start a new tie group
        group_start = i
        group_max_score = scored_rules[i]["score"]
        i += 1

        # Extend the group: each next element must be within threshold of the
        # previous element (adjacent pair comparison).
        while i < n and (scored_rules[i - 1]["score"] - scored_rules[i]["score"]) <= STICKY_TIEBREAK_THRESHOLD + _TIEBREAK_EPSILON:
            i += 1

        group = scored_rules[group_start:i]

        if len(group) > 1:
            # Stable sort: preferred rules ordered by their position in
            # prefer_rule_ids; non-preferred rules keep original relative order.
            def sort_key(rule: dict) -> tuple[int, int]:
                rid = rule["rule_id"]
                if rid in pref_index:
                    return (0, pref_index[rid])
                # Non-preferred: preserve original order via enumeration
                return (1, 0)

            # We need to preserve original positions for non-preferred rules.
            # Use a two-pass approach: extract preferred and non-preferred,
            # then interleave.
            preferred = [(r, pref_index[r["rule_id"]]) for r in group if r["rule_id"] in pref_index]
            non_preferred = [r for r in group if r["rule_id"] not in pref_index]

            # Sort preferred by their position in prefer_rule_ids
            preferred.sort(key=lambda x: x[1])

            # Merge: preferred first (by pref order), then non-preferred (original order)
            merged = [r for r, _ in preferred] + non_preferred
            result.extend(merged)
        else:
            result.extend(group)

    return result


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
        loaded_rule_ids: list[str] | None = None,
        prefer_rule_ids: list[str] | None = None,
        retrieval_mode: str = "semantic",
        node_types: list[str] | None = None,
    ) -> dict:
        """Execute the full 5-stage pipeline.

        retrieval_mode: "semantic" (default) or "literal". Literal mode
        rebalances BM25 and vector to equal weight, optimizing for exact-phrase
        / rationalization queries where BM25 carries the distinguishing signal.
        Semantic mode preserves the vector-dominant tune optimized for ambiguous
        coding-rule queries. Caller chooses based on query characteristics.

        node_types: optional whitelist of node_types to retrieve (e.g. ["Rule"]
        for coding-only, ["Skill", "Playbook", "Technique", "AntiPattern",
        "ForbiddenResponse"] for methodology-only, None for all). Stage 1 filter.

        Returns dict with rules, mode, total_candidates, latency_ms.
        """
        start = time.perf_counter()
        exclude = set(exclude_rule_ids or []) | set(loaded_rule_ids or [])
        # Select ranking weights for this query per retrieval_mode.
        if retrieval_mode == "literal":
            active_weights = RankingWeights.literal()
        else:
            active_weights = self._weights
        # Resolve allowed types. Explicit node_types wins. Otherwise:
        # - semantic mode (default) restricts to coding-rule corpus: {Rule} with
        #   domain NOT in methodology-domain set (process, communication, meta-authoring).
        #   This preserves pre-Phase-1 ambiguous-coding MRR as methodology nodes
        #   enter the graph but do not contaminate default queries.
        # - literal mode unlocks the full candidate pool (Rule + methodology types).
        if node_types is not None:
            allowed_types = set(node_types)
            methodology_domain_exclude = False
        elif retrieval_mode == "literal":
            allowed_types = None
            methodology_domain_exclude = False
        else:
            allowed_types = {"Rule"}
            methodology_domain_exclude = True

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
        if allowed_types is not None:
            bm25_results = [
                r for r in bm25_results
                if self._metadata.get(r["rule_id"], {}).get("node_type", "Rule") in allowed_types
            ]
        if methodology_domain_exclude:
            bm25_results = [
                r for r in bm25_results
                if self._metadata.get(r["rule_id"], {}).get("domain", "").lower() not in {"process", "communication", "meta-authoring"}
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
        if allowed_types is not None:
            vector_results = [
                r for r in vector_results
                if self._metadata.get(r.rule_id, {}).get("node_type", "Rule") in allowed_types
            ]
        if methodology_domain_exclude:
            vector_results = [
                r for r in vector_results
                if self._metadata.get(r.rule_id, {}).get("domain", "").lower() not in {"process", "communication", "meta-authoring"}
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
        fp_bm25, fp_vec, fp_sev, fp_conf = active_weights.first_pass_weights()
        first_pass_weights = RankingWeights(
            w_bm25=fp_bm25, w_vector=fp_vec, w_severity=fp_sev, w_confidence=fp_conf,
            w_graph=0.0, w_bundle_cohesion=0.0,
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

        # Stage 5b': Compute bundle cohesion per candidate (plan Section 3.2 deliverable 4).
        # A candidate gets a bonus proportional to the fraction of its bundle
        # members (1-hop neighbors) that are also in the top-N first-pass set.
        top_n_set = set(rid for rid, _ in first_pass_scores[:FIRST_PASS_TOP_N])
        bundle_cohesion: dict[str, float] = {}
        for rid in all_candidate_list:
            neighbors = self._cache.get_neighbors(rid)
            if not neighbors:
                bundle_cohesion[rid] = 0.0
                continue
            overlap = sum(1 for n in neighbors if n["rule_id"] in top_n_set)
            bundle_cohesion[rid] = overlap / len(neighbors)

        # Stage 5c: Final ranking with graph proximity + bundle cohesion.
        scored_rules: list[dict] = []
        for rid, scores in candidate_ids.items():
            meta = self._metadata.get(rid, {})
            final_score = compute_score(
                bm25_norm=scores.get("bm25_norm", 0.0),
                vector_norm=scores.get("vector_norm", 0.0),
                severity=meta.get("severity", "medium"),
                confidence=meta.get("confidence", "production-validated"),
                graph_proximity=proximity.get(rid, 0.0),
                bundle_cohesion=bundle_cohesion.get(rid, 0.0),
                weights=active_weights,
            )
            rule_entry = {
                "rule_id": rid,
                "node_type": meta.get("node_type", "Rule"),
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

        # Sticky rules tie-breaking: reorder adjacent rules within 0.02 score
        # of each other to match the prefer_rule_ids ordering. This stabilizes
        # the injection order across turns for prompt-cache friendliness.
        if prefer_rule_ids:
            scored_rules = _apply_sticky_tiebreak(scored_rules, prefer_rule_ids)

        # Apply context budget.
        trimmed, mode = apply_context_budget(scored_rules, budget_tokens)

        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "rules": trimmed,
            "mode": mode,
            "total_candidates": len(candidate_ids),
            "latency_ms": round(elapsed_ms, 3),
        }


def _compute_corpus_hash(rule_ids: list[str], vectors: list[list[float]]) -> str:
    """Compute a SHA-256 hash of the corpus for cache invalidation."""
    pairs = sorted(zip(rule_ids, [str(v) for v in vectors]))
    digest_input = "|".join(f"{rid}:{vec}" for rid, vec in pairs)
    return hashlib.sha256(digest_input.encode()).hexdigest()


def _fold_auxiliary_text_into_body(node: dict, label: str) -> str:
    """Concatenate type-specific searchable text into body for BM25 surfacing.

    Mirrors the Phase 0 MethodologyIndex._collect_body_text logic so production
    BM25 matches on the same text the Phase-0 benchmark validated against.
    """
    parts: list[str] = []
    existing_body = node.get("body") or ""
    if existing_body:
        parts.append(existing_body)
    if label == "ForbiddenResponse":
        # Fields may arrive as JSON strings from Neo4j (since we json.dumps'd
        # nested structures during ingest) or as native lists.
        phrases = node.get("forbidden_phrases")
        if isinstance(phrases, str):
            try:
                import json as _json
                phrases = _json.loads(phrases)
            except Exception:
                phrases = [phrases]
        if isinstance(phrases, list):
            parts.extend(str(p) for p in phrases)
        wts = node.get("what_to_say_instead")
        if wts:
            parts.append(str(wts))
    elif label == "AntiPattern":
        named = node.get("named_in")
        if named:
            parts.append(str(named))
    return " ".join(p for p in parts if p)


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

    Phase 1: loads Rule + all 5 retrievable methodology node types (Skill,
    Playbook, Technique, AntiPattern, ForbiddenResponse). Non-retrievable types
    (Phase, Rationalization, PressureScenario, WorkedExample, SubagentRole)
    enter Stage 4 via the adjacency cache but do not appear as candidates.
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

    # Load retrievable methodology nodes. Each becomes a candidate alongside Rules.
    retrievable_methodology_labels = ("Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse")
    retrievable_id_fields = {
        "Skill": "skill_id",
        "Playbook": "playbook_id",
        "Technique": "technique_id",
        "AntiPattern": "antipattern_id",
        "ForbiddenResponse": "forbidden_id",
    }
    methodology_nodes: list[dict] = []
    for label in retrievable_methodology_labels:
        id_field = retrievable_id_fields[label]
        q = f"MATCH (n:{label}) RETURN n"
        async with db._driver.session(database=db._database) as session:
            result = await session.run(q)
            async for record in result:
                node = dict(record["n"])
                # Normalize: carry both the original id field AND a `rule_id`
                # alias so BM25/vector stores (keyed on rule_id by convention)
                # can accept it without schema changes.
                node_id = node.get(id_field)
                if not node_id:
                    continue
                node["rule_id"] = node_id
                node["node_type"] = label
                # Methodology nodes are never "mandatory" in the coding-rule sense;
                # always_on governs the render path separately.
                node["mandatory"] = False
                # Phase 0 MethodologyIndex parity: fold type-specific text fields
                # into `body` so BM25 surfaces them. Without this, queries matching
                # a forbidden phrase literal (e.g. "you're absolutely right") miss
                # the corresponding FRB node because the phrase lives in a list
                # field, not the default indexed text.
                node["body"] = _fold_auxiliary_text_into_body(node, label)
                methodology_nodes.append(node)

    all_candidates = rules + methodology_nodes
    for r in rules:
        r.setdefault("node_type", "Rule")

    # Build metadata lookup (keyed by rule_id which now doubles as node_id).
    rule_metadata: dict[str, dict] = {r["rule_id"]: r for r in all_candidates}

    # Build BM25 index (Stage 2) -- includes methodology body per plan Section 3.2.
    keyword_index = KeywordIndex()
    keyword_index.build(all_candidates)

    # Build vector index (Stage 3).
    texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in all_candidates]
    rule_ids = [r["rule_id"] for r in all_candidates]

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

    # Compute corpus hash for HNSW cache lookup
    dims = len(embeddings[0]) if embeddings else 384
    cache_dir = get_hnsw_cache_dir()
    corpus_hash = _compute_corpus_hash(rule_ids, embeddings)

    vector_store = HnswlibStore(dimensions=dims, cache_dir=cache_dir)

    # Try loading cached index; fall back to rebuild + save
    loaded_from_cache = False
    try:
        vector_store.load_index(corpus_hash=corpus_hash)
        loaded_from_cache = True
        _logger.info("Loaded HNSW index from cache (hash=%s)", corpus_hash[:12])
    except Exception as exc:
        _logger.debug("HNSW cache miss: %s", exc)

    if not loaded_from_cache:
        vector_store.build_index(rule_ids, embeddings)
        try:
            vector_store.save_index(corpus_hash=corpus_hash)
            _logger.info("Saved HNSW index to cache (hash=%s)", corpus_hash[:12])
        except Exception as exc:
            _logger.warning("Failed to save HNSW index: %s", exc)

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
