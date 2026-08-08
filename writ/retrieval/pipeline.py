"""Orchestrates all retrieval stages in sequence.

Stage 1: Domain Filter -- pre-filter to relevant domain subgraph.
Stage 2: BM25 Keyword Filter -- Tantivy sparse retrieval on trigger, statement, tags.
Stage 3: ANN Vector Search -- hnswlib in-process ANN on pre-computed embeddings.
Stage 4: Graph Traversal -- adjacency cache lookup from top-K results.
Stage 5a: First-pass ranking -- reciprocal-rank + metadata weighting (no graph proximity).
Stage 5b: Graph proximity -- compute proximity scores from top-3 first-pass results.
Stage 5c: Final ranking -- re-score with graph proximity, context budget applied.

The pipeline operates on domain rules only. Mandatory rules (ENF-*, mandatory: true)
are excluded before Stage 1.

Per PERF-IO-001: all indexes pre-warmed at startup. No I/O in the query path.
Per ARCH-DI-001: all dependencies injected via constructor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from writ.config import get_hnsw_cache_dir
from writ.graph.predicates import RANKED_INCLUDE_WHERE
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
from writ.shared.logging import emit, emit_exception

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection

# Preferred ONNX model directory.
_ONNX_DIR = DEFAULT_ONNX_DIR

# Per ARCH-CONST-001
BM25_CANDIDATE_LIMIT = 50
VECTOR_CANDIDATE_LIMIT = 10
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
FIRST_PASS_TOP_N = 3
# S4 CRAG abstention operating point for the rule-injection path only. Measured
# (KG #85) do-no-harm point on the retrieval gold set: gold hit@5/ambiguous MRR@5
# unchanged from baseline, false-injection 100%->30%. It is applied ONLY where
# rules are retrieved for injection (the daemon /query path and `writ query`),
# NOT baked into build_pipeline's default -- authoring (suggest_relationships)
# and offline diagnostics use the same factory and were not part of that
# measurement, so they must stay ungated (abstention_threshold=0.0).
RULE_INJECTION_ABSTENTION_THRESHOLD = 0.30
# Domains excluded by the semantic-mode legacy fallback (node_routes absent).
_METHODOLOGY_EXCLUDE_DOMAINS = {"process", "communication", "meta-authoring"}


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
        # Start a new tie group. The group max is the score of the group's first
        # element (input is sorted descending), so every member is within
        # STICKY_TIEBREAK_THRESHOLD of the group's HIGHEST score.
        #
        # PRECONDITION: scored_rules is sorted DESCENDING here, so scored_rules[group_start]
        # is the group max. query() satisfies this by calling this function immediately
        # after its descending sort, with NO reordering pass in between. That ordering is
        # deliberate: apply_authority_preference does reorder, and running it first (the
        # pre-2026-08-06 order) left this function grouping a non-descending list, which
        # inverted the very preference it had just applied. Any new pass inserted between
        # the sort and this call must preserve descending order, or this grouping must be
        # changed to take max(group) explicitly.
        group_start = i
        group_max = scored_rules[group_start]["score"]
        i += 1

        # Extend the group: each next element must be within threshold of the
        # GROUP MAX, not of the previous adjacent element. Bounding by the max
        # stops a harmonically-decaying tail from chaining transitively into one
        # group, which let a rule far below the top score be promoted above it.
        while i < n and (group_max - scored_rules[i]["score"]) <= STICKY_TIEBREAK_THRESHOLD + _TIEBREAK_EPSILON:
            i += 1

        group = scored_rules[group_start:i]

        if len(group) > 1:
            # Stable reorder: preferred rules first, ordered by their position in
            # prefer_rule_ids; non-preferred rules keep original relative order.
            # Two-pass: extract preferred and non-preferred, then interleave.
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
        abstention_threshold: float = 0.0,
        abstractions: list[dict] | None = None,
        node_routes: dict[str, list[str]] | None = None,
    ) -> None:
        self._keyword = keyword_index
        self._vector = vector_store
        self._cache = adjacency_cache
        self._model = embedding_model
        self._metadata = rule_metadata
        self._weights = weights or RankingWeights()
        self._authority_preference_threshold = authority_preference_threshold
        # S4 CRAG abstention: return no rules when the top raw vector cosine is
        # below this (0.0 = off).
        self._abstention_threshold = abstention_threshold
        self._abstractions = abstractions or []
        # Phase 0 T0.4: maps a node's identity (node_type label or node_id) to
        # the route tags of its category. When present, the default semantic
        # filter admits a candidate only if its route list contains 'semantic'.
        # When None, the pipeline keeps the pre-Phase-0 Rule-only + domain-exclude
        # fallback so pre-migration graphs (no route metadata) behave unchanged.
        self._node_routes = node_routes

    def _routes_for(self, rule_id: str) -> list[str]:
        """Resolve a candidate's category route tags for the Stage 1 filter.

        Lookup order in self._node_routes: the candidate's own id first (per-node
        override), then its node_type label. Falls back to the candidate's own
        'routes' metadata if the map carries no entry. Returns [] when no routes
        are known, which the semantic route filter treats as not-admitted.
        """
        routes_map = self._node_routes or {}
        if rule_id in routes_map:
            return routes_map[rule_id]
        meta = self._metadata.get(rule_id, {})
        node_type = meta.get("node_type", "Rule")
        if node_type in routes_map:
            return routes_map[node_type]
        return meta.get("routes", []) or []

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
        project: str | None = None,
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

        allowed_types, methodology_domain_exclude, route_filter = self._resolve_stage1_filter(
            node_types, retrieval_mode,
        )
        # M.3 project scope: admit only the caller's project + the shared corpus --
        # the anti-leak guarantee. project=None preserves search-all.
        allowed_projects = {project, "_shared"} if project is not None else None
        domain_lower = domain.lower() if domain else None  # A8: hoist query-constant

        # Stage 2: BM25 keyword search.
        bm25_results = self._keyword.search(query_text, limit=BM25_CANDIDATE_LIMIT)
        bm25_results = self._filter_candidates(
            bm25_results, lambda r: r["rule_id"], exclude, domain_lower, allowed_types,
            methodology_domain_exclude, route_filter, allowed_projects,
        )

        # Stage 3: ANN vector search.
        query_vector = self._model.encode(query_text).tolist()
        vector_results: list[ScoredResult] = self._vector.search(query_vector, k=VECTOR_CANDIDATE_LIMIT)
        # S4 CRAG abstention gate: the raw cosine of the single best semantic
        # match. self._vector.search returns descending, so [0].score is top-1.
        # When even the best match is weak (below the threshold), no rule is
        # relevant -- return an empty set rather than injecting a false positive.
        top_raw_cosine = max((r.score for r in vector_results), default=0.0)
        if self._abstention_threshold > 0.0 and top_raw_cosine < self._abstention_threshold:
            return {
                "rules": [],
                "mode": "abstained",
                "total_candidates": 0,
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                "abstain_signal": round(top_raw_cosine, 6),
            }
        vector_results = self._filter_candidates(
            vector_results, lambda r: r.rule_id, exclude, domain_lower, allowed_types,
            methodology_domain_exclude, route_filter, allowed_projects,
        )

        # Merge + reciprocal-rank normalize candidates from both stages.
        candidate_ids = self._merge_and_normalize(bm25_results, vector_results)

        # Stage 4: Graph traversal enrichment (from adjacency cache).
        enrichment = self._cache.get_enrichment(list(candidate_ids.keys()))

        # Stage 5a: First-pass ranking (without graph proximity, INV-4).
        first_pass_scores = self._first_pass_rank(candidate_ids, active_weights)

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
        scored_rules = self._final_rank(
            candidate_ids, active_weights, proximity, enrichment,
        )

        # Sort by score descending.
        scored_rules.sort(key=lambda r: r["score"], reverse=True)

        # Sticky rules tie-breaking: reorder adjacent rules within 0.02 score
        # of each other to match the prefer_rule_ids ordering. This stabilizes
        # the injection order across turns for prompt-cache friendliness.
        #
        # ORDER MATTERS: this runs on the freshly sorted list, BEFORE the authority
        # pass, because its grouping treats the first element of each run as the
        # group max, which is valid only on descending input. The authority pass
        # reorders, so running it first left this one grouping a non-descending
        # list. Measured over an exhaustive 12,960-configuration comparison of the
        # two orders, 4,923 diverge; the worst shape inverted the preference
        # outright, promoting a preferred ai-provisional rule above every human
        # rule in its tie group. Order is load-bearing, not cosmetic:
        # apply_context_budget trims by taking the first N in list order.
        if prefer_rule_ids:
            scored_rules = _apply_sticky_tiebreak(scored_rules, prefer_rule_ids)

        # Phase 3b: hard authority preference -- human outranks ai-provisional.
        # Applied LAST so the hard preference is the final word: it is allowed to
        # override the tiebreak's prompt-cache ordering, but only within its own
        # score threshold. Off by default (threshold 0.0 = no reordering).
        scored_rules = apply_authority_preference(
            scored_rules, self._authority_preference_threshold,
        )

        # Apply context budget. Abstraction summaries (when present in the
        # graph and the budget triggers summary mode) replace raw rule
        # renders.
        trimmed, mode = apply_context_budget(scored_rules, budget_tokens, self._abstractions)

        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "rules": trimmed,
            "mode": mode,
            "total_candidates": len(candidate_ids),
            "latency_ms": round(elapsed_ms, 3),
            "abstain_signal": round(top_raw_cosine, 6),
        }

    def _resolve_stage1_filter(self, node_types, retrieval_mode):
        """Resolve the Stage 1 candidate filter. Precedence (plan Section 4):
          1. Explicit node_types passed to query() wins -- caller-chosen
             whitelist, bypasses the route filter entirely.
          2. literal mode unlocks the full candidate pool (no filter).
          3. semantic mode (default):
             a. node_routes present  -> route filter: admit a candidate only
                if its category route list contains 'semantic'.
             b. node_routes None     -> legacy fallback: {Rule}-only + exclude
                process/communication/meta-authoring domains. Preserves
                pre-Phase-0 behavior for pre-migration graphs.
        Returns (allowed_types, methodology_domain_exclude, route_filter).
        """
        route_filter = False
        if node_types is not None:
            allowed_types = set(node_types)
            methodology_domain_exclude = False
        elif retrieval_mode == "literal":
            allowed_types = None
            methodology_domain_exclude = False
        elif self._node_routes is not None:
            allowed_types = None
            methodology_domain_exclude = False
            route_filter = True
        else:
            allowed_types = {"Rule"}
            methodology_domain_exclude = True
        return allowed_types, methodology_domain_exclude, route_filter

    def _filter_candidates(self, results, id_of, exclude, domain_lower, allowed_types,
                           methodology_domain_exclude, route_filter, allowed_projects):
        """Stage 1 candidate filter. The BM25 hits are dict-shaped and the vector hits
        are ScoredResult-shaped, so the identical predicate chain is parameterized only
        by how each carries its rule_id (id_of). All filters are pure AND-ed predicates,
        so the result set is independent of their order.

        A8: single pass with ONE metadata lookup per candidate (was up to 5 dict.get +
        a per-element domain.lower()). M.3 project scope is the last filter.
        """
        out = []
        for r in results:
            rid = id_of(r)
            if rid in exclude:
                continue
            m = self._metadata.get(rid, {})
            if domain_lower is not None and m.get("domain", "").lower() != domain_lower:
                continue
            if allowed_types is not None and m.get("node_type", "Rule") not in allowed_types:
                continue
            if methodology_domain_exclude and m.get("domain", "").lower() in _METHODOLOGY_EXCLUDE_DOMAINS:
                continue
            if route_filter and "semantic" not in self._routes_for(rid):
                continue
            if allowed_projects is not None and m.get("project", "writ") not in allowed_projects:
                continue
            out.append(r)
        return out

    def _merge_and_normalize(self, bm25_results, vector_results) -> dict:
        """Merge BM25 + vector hits into candidate_ids and attach reciprocal-rank
        normalized scores (bm25_norm / vector_norm) in place. Returns candidate_ids."""
        candidate_ids: dict[str, dict] = {}
        bm25_scores = {r["rule_id"]: r["score"] for r in bm25_results}
        vector_scores = {r.rule_id: r.score for r in vector_results}

        # ORDERED union, never `set(a) | set(b)`. Python randomizes string hashing
        # per process, so a set's iteration order changes between daemon starts, and
        # this order is load-bearing twice: ids_list below inherits it and
        # normalize_ranks breaks ties with a STABLE sort (so a rule's bm25_norm
        # actually changes), then _final_rank inherits it again and
        # apply_context_budget trims by list position. Measured before the fix, over
        # the 193-query gold set at PYTHONHASHSEED 0 vs 7: 30 queries returned a
        # different SET of top-5 rules, i.e. a different rulebook reached the model
        # after a restart. Discovery order (BM25 rank, then vector-only rank) is both
        # deterministic and meaningful: a tie resolves toward the candidate the
        # keyword stage surfaced first. Regression: tests/test_retrieval_determinism.py.
        all_ids = list(bm25_scores) + [r for r in vector_scores if r not in bm25_scores]
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
        return candidate_ids

    def _first_pass_rank(self, candidate_ids, active_weights) -> list:
        """Stage 5a: first-pass ranking without graph proximity (INV-4).
        Returns [(rule_id, score)] sorted by score descending."""
        fp_bm25, fp_vec, fp_sev, fp_conf = active_weights.first_pass_weights()
        first_pass_weights = RankingWeights(
            w_bm25=fp_bm25, w_vector=fp_vec, w_severity=fp_sev, w_confidence=fp_conf,
            w_graph=0.0,
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
                times_seen_positive=meta.get("times_seen_positive", 0) or 0,
                times_seen_negative=meta.get("times_seen_negative", 0) or 0,
            )
            first_pass_scores.append((rid, fp_score))

        first_pass_scores.sort(key=lambda x: x[1], reverse=True)
        return first_pass_scores

    def _final_rank(self, candidate_ids, active_weights, proximity, enrichment) -> list:
        """Stage 5c: final ranking with graph proximity.
        Returns the scored rule-entry dicts (unsorted; the caller sorts)."""
        scored_rules: list[dict] = []
        for rid, scores in candidate_ids.items():
            meta = self._metadata.get(rid, {})
            final_score = compute_score(
                bm25_norm=scores.get("bm25_norm", 0.0),
                vector_norm=scores.get("vector_norm", 0.0),
                severity=meta.get("severity", "medium"),
                confidence=meta.get("confidence", "production-validated"),
                graph_proximity=proximity.get(rid, 0.0),
                weights=active_weights,
                times_seen_positive=meta.get("times_seen_positive", 0) or 0,
                times_seen_negative=meta.get("times_seen_negative", 0) or 0,
            )
            rule_entry = {
                "rule_id": rid,
                "node_type": meta.get("node_type", "Rule"),
                "score": round(final_score, 4),
                "authority": meta.get("authority", "human"),
                # severity + domain surfaced so the render shows them instead of "(?, ?, ?)".
                "severity": meta.get("severity", "medium"),
                "domain": meta.get("domain", ""),
                "statement": meta.get("statement", ""),
                "trigger": meta.get("trigger", ""),
                "violation": meta.get("violation", ""),
                "pass_example": meta.get("pass_example", ""),
                "rationale": meta.get("rationale", ""),
                "relationships": enrichment.get(rid, []),
            }
            scored_rules.append(rule_entry)
        return scored_rules


_BM25_SIDECAR = "writ_bm25.json"


def _compute_bm25_hash(candidates: list[dict]) -> str:
    """Cache key over exactly what the BM25 index consumes.

    The HNSW hash covers trigger+statement only (all the embedding sees); BM25
    additionally indexes tags and body and excludes mandatory rules, so reusing
    the HNSW key would serve a stale keyword index after a tags/body/mandatory
    edit. Sorted by rule_id so candidate order cannot change the key.
    """
    payload = [
        (
            r["rule_id"],
            r.get("trigger", ""),
            r.get("statement", ""),
            str(r.get("tags", "")),
            r.get("body", "") or "",
            bool(r.get("mandatory", False)),
        )
        for r in sorted(candidates, key=lambda r: r["rule_id"])
    ]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def _load_or_build_keyword_index(
    candidates: list[dict], cache_root: str | Path
) -> tuple[KeywordIndex, str]:
    """Open the persisted BM25 index when its sidecar hash matches, else rebuild.

    Mirrors the HNSW cache discipline below: hash first, open on hit, rebuild
    into a fresh directory on miss (tantivy appends, so building into a
    non-empty index would duplicate documents), sidecar written last so a
    crash mid-build reads as a miss. Returns (index, outcome) where outcome is
    "hit", "miss", or "nocache" (persistence unavailable; in-memory build --
    the pre-persistence behavior, never a pipeline failure).
    """
    bm25_dir = Path(cache_root) / "bm25"
    sidecar = bm25_dir / _BM25_SIDECAR
    corpus_hash = _compute_bm25_hash(candidates)

    try:
        if sidecar.is_file() and json.loads(sidecar.read_text()).get("corpus_hash") == corpus_hash:
            index = KeywordIndex(index_dir=bm25_dir)
            index.reload()
            _logger.info("Loaded BM25 index from cache (hash=%s)", corpus_hash[:12])
            return index, "hit"
    except Exception as exc:
        _logger.debug("BM25 cache open failed, rebuilding: %s", exc)

    try:
        shutil.rmtree(bm25_dir, ignore_errors=True)
        bm25_dir.mkdir(parents=True, exist_ok=True)
        index = KeywordIndex(index_dir=bm25_dir)
        index.build(candidates)
        sidecar.write_text(json.dumps(
            {"corpus_hash": corpus_hash, "count": len(candidates)}
        ))
        return index, "miss"
    except Exception as exc:
        _logger.warning("BM25 persistence unavailable, building in memory: %s", exc)
        index = KeywordIndex()
        index.build(candidates)
        return index, "nocache"


def _compute_corpus_hash_from_text(rule_ids: list[str], texts: list[str]) -> str:
    """Compute a SHA-256 hash of the corpus from rule_id + text pairs.

    Invalidation semantics key on text because the embedding is a deterministic
    function of text + model: any text change produces a different embedding, and
    we already pin the model via DEFAULT_ONNX_DIR. The advantage of hashing text
    directly: callers can compute the cache key BEFORE running the expensive
    `encode_batch` pass, then skip encoding entirely on a cache hit.

    Item 4 (Approach C, 2026-05-15): per-stage instrumentation showed
    `encode_batch` is ~84% of cold-start at 276 rules (~2.3s) and scales
    linearly with rule count. At 10K rules it would dominate cold-start
    at ~80-100s. Hashing from text lets the HNSW cache short-circuit the
    encode on subsequent cold-starts, cutting steady-state cold-start
    from O(N) to O(model_load) + O(load_index).
    """
    pairs = sorted(zip(rule_ids, texts))
    digest_input = "|".join(f"{rid}:{txt}" for rid, txt in pairs)
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


async def _load_candidates(db: Neo4jConnection) -> tuple[list[dict], dict]:
    """Load Rule + retrievable methodology nodes from Neo4j into the candidate pool.

    Returns (all_candidates, rule_metadata). The Rule exclusion predicate is the
    single-source RANKED_INCLUDE_WHERE constant so the {excluded-from-ranked}=={mandatory}
    validator (integrity.py) checks the exact predicate the pool uses (WRIT-BLUEPRINT 3.5/3.6a).
    """
    # ORDER BY is not cosmetic: this load order becomes the BM25 and vector index
    # build order, and index build order can decide ties among equal-scoring hits.
    # Cypher gives no ordering guarantee without it, so leaving it out would make the
    # determinism this module now promises depend on the database's whim across
    # restarts. Sorted by the canonical id, which is unique, so the order is total.
    query = f"""
        MATCH (r:Rule)
        WHERE {RANKED_INCLUDE_WHERE}
        RETURN r
        ORDER BY r.rule_id
    """
    rules: list[dict] = []
    async with db._driver.session(database=db._database) as session:
        result = await session.run(query)
        async for record in result:
            rules.append(dict(record["r"]))

    # Load retrievable methodology nodes. Each becomes a candidate alongside Rules.
    retrievable_methodology_labels = ("Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse")
    # Reuse the canonical label->id-field registry rather than a local copy.
    from writ.graph.schema import NODE_ID_FIELDS
    retrievable_id_fields = {label: NODE_ID_FIELDS[label] for label in retrievable_methodology_labels}
    methodology_nodes: list[dict] = []
    for label in retrievable_methodology_labels:
        id_field = retrievable_id_fields[label]
        # Ordered for the same reason as the Rule query above: this feeds index
        # build order, which can decide ties among equal-scoring candidates.
        q = f"MATCH (n:{label}) RETURN n ORDER BY n.{id_field}"
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
    return all_candidates, rule_metadata


def _resolve_encoder(embedding_model, model_name: str, texts: list[str], loaded_from_cache: bool):
    """Select the embedding model and (on cache miss) compute corpus embeddings. Three states:
      1. embedding_model passed in -> use it (DI path for tests / pre-warmed servers).
      2. ONNX construction succeeds -> production path.
      3. ONNX construction fails -> raise unless WRIT_ALLOW_EMBEDDING_FALLBACK=1.

    Prior behavior silently swallowed FileNotFoundError / ImportError and fell through to
    SentenceTransformer, making production daemons and CI answer to the same name while
    running different code. The override env var keeps the dev-only fallback available but
    requires an explicit opt-in so the operational risk is visible.

    Returns (query_encoder, embeddings); embeddings is None on a cache hit (loaded_from_cache).
    """
    onnx_model = None
    onnx_construction_error: Exception | None = None
    if embedding_model is None:
        try:
            onnx_model = OnnxEmbeddingModel(_ONNX_DIR)
        except (FileNotFoundError, ImportError) as exc:
            onnx_construction_error = exc

    # Embeddings only needed when we have to rebuild the HNSW index (cache miss).
    embeddings: list[list[float]] | None = None

    if onnx_model is not None:
        # ONNX for everything: cached single encode at query time. Bulk encode only on
        # HNSW cache miss. No PyTorch/sentence-transformers in the runtime path.
        query_encoder = CachedEncoder(onnx_model)
        if not loaded_from_cache:
            embeddings = onnx_model.encode_batch(texts)
    elif embedding_model is not None:
        # Pre-loaded model passed in (tests, server reuse). Bypasses ONNX auto-detect.
        raw_model = embedding_model
        if isinstance(embedding_model, CachedEncoder):
            raw_model = embedding_model._model
        query_encoder = (
            embedding_model if isinstance(embedding_model, CachedEncoder)
            else CachedEncoder(embedding_model)
        )
        if not loaded_from_cache:
            if isinstance(raw_model, OnnxEmbeddingModel):
                embeddings = raw_model.encode_batch(texts)
            else:
                embeddings = raw_model.encode(texts).tolist()
    elif os.environ.get("WRIT_ALLOW_EMBEDDING_FALLBACK") == "1":
        # Dev opt-in: ONNX unavailable, fallback explicitly permitted. Logged at WARNING on
        # every startup so the operator sees the divergence from the production path.
        _logger.warning(
            "ONNX embedding model unavailable (%s: %s); using "
            "SentenceTransformer fallback because "
            "WRIT_ALLOW_EMBEDDING_FALLBACK=1. Production latency and "
            "memory numbers will NOT apply on this run. Unset the env "
            "var to restore the production-required path.",
            type(onnx_construction_error).__name__,
            onnx_construction_error,
        )
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            # Approach C (Finding D): sentence-transformers moved from core deps to the
            # [fallback] optional-dependencies group because the production runtime no
            # longer imports it. The operator set the env var but did not install the
            # extras group; the fallback cannot actually run. Raise the same actionable shape.
            raise RuntimeError(
                "WRIT_ALLOW_EMBEDDING_FALLBACK=1 was set, but the "
                "sentence-transformers library could not be imported: "
                f"{type(exc).__name__}: {exc}. "
                "Production installs deliberately exclude this library "
                "(see pyproject.toml: it lives in the [fallback] extras "
                "group, not core dependencies). To enable the fallback "
                "path for local development, run "
                "`pip install -e '.[fallback]'`. To use the production "
                "ONNX path instead, ensure the ONNX model is exported "
                "(run scripts/export_onnx.py) and unset "
                "WRIT_ALLOW_EMBEDDING_FALLBACK."
            ) from exc

        model = SentenceTransformer(model_name)
        query_encoder = CachedEncoder(model)
        if not loaded_from_cache:
            embeddings = model.encode(texts).tolist()
    else:
        raise RuntimeError(
            "ONNX embedding model unavailable: "
            f"{type(onnx_construction_error).__name__}: {onnx_construction_error}. "
            f"Production startup requires the ONNX model at {_ONNX_DIR} "
            "plus onnxruntime and tokenizers in the active interpreter. "
            "Run `python scripts/export_onnx.py` to produce the model, "
            "and verify the venv has onnxruntime + tokenizers installed "
            "(`pip install -e .[dev]` or run scripts/bootstrap.sh). "
            "To allow the SentenceTransformer fallback for local "
            "development only, set WRIT_ALLOW_EMBEDDING_FALLBACK=1 "
            "(NOT recommended for production; latency and memory numbers "
            "will diverge from the production-path measurements)."
        )
    return query_encoder, embeddings


async def build_pipeline(
    db: Neo4jConnection,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    weights: RankingWeights | None = None,
    embedding_model: object | None = None,
    authority_preference_threshold: float = 0.0,
    # Neutral factory: the gate is OFF by default so authoring
    # (suggest_relationships) and offline diagnostics that share this factory stay
    # ungated. Rule-injection callers opt in with RULE_INJECTION_ABSTENTION_THRESHOLD.
    abstention_threshold: float = 0.0,
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
    all_candidates, rule_metadata = await _load_candidates(db)

    # Build BM25 index (Stage 2) -- includes methodology body per plan Section 3.2.
    # Persisted and keyed like the HNSW index below, so a warm cold-start skips
    # the rebuild; any cache trouble degrades to the old in-memory build.
    keyword_index, bm25_outcome = _load_or_build_keyword_index(
        all_candidates, Path(get_hnsw_cache_dir()).parent
    )
    emit("metrics", "bm25_cache", "", None, outcome=bm25_outcome)

    # Build vector index (Stage 3).
    texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in all_candidates]
    rule_ids = [r["rule_id"] for r in all_candidates]

    # Item 4 (Approach C, 2026-05-15): compute the HNSW cache key from
    # rule text BEFORE running the expensive encode_batch pass, then
    # check the cache. On a cache hit, the HNSW persistence already
    # contains the vectors -- we still need the model loaded for
    # query-time encoding, but the corpus-wide encode_batch is the
    # cold-start bottleneck (~84% of total at 276 rules per
    # scripts/instrument-cold-start.py). Skipping it on cache hit cuts
    # warm-cache cold-start from O(N) embedding cost down to O(1) cache
    # load plus the fixed model load. At 10K rules this is the
    # difference between ~80s and ~3s.
    cache_dir = get_hnsw_cache_dir()
    corpus_hash = _compute_corpus_hash_from_text(rule_ids, texts)
    # 384 is the fixed output dimensionality of all-MiniLM-L6-v2 (the
    # only model the pipeline supports). Hardcoding lets us initialize
    # the vector store before we have any embeddings to inspect.
    vector_store = HnswlibStore(dimensions=384, cache_dir=cache_dir)
    loaded_from_cache = False
    try:
        vector_store.load_index(corpus_hash=corpus_hash)
        loaded_from_cache = True
        _logger.info(
            "Loaded HNSW index from cache (hash=%s); skipping encode_batch",
            corpus_hash[:12],
        )
        # The hit is recorded too, for the same reason every query is: without the
        # denominator, "two misses" cannot be read as either routine (two restarts after
        # corpus edits) or alarming (two misses out of two hundred starts).
        emit("metrics", "hnsw_cache", "", None,
             outcome="hit", corpus_hash=corpus_hash[:12])
    except Exception as exc:
        _logger.debug("HNSW cache miss: %s", exc)
        # P1 deferred this pair as "the least silent of the set" because they already
        # call _logger. But _logger.debug is invisible at the default level, and a cache
        # miss means the process is about to bulk-encode the whole corpus: the single
        # biggest cold-start cost. Recorded on metrics (once per process build, so the
        # volume is trivial) to make a slow startup explainable after the fact.
        emit(
            "metrics", "hnsw_cache", "", None,
            outcome="miss", corpus_hash=corpus_hash[:12], reason=str(exc)[:200],
        )

    # Select the embedding model and (on cache miss) bulk-encode the corpus.
    query_encoder, embeddings = _resolve_encoder(
        embedding_model, model_name, texts, loaded_from_cache,
    )

    if not loaded_from_cache:
        # Cache miss: we have just-computed embeddings; build + persist
        # the HNSW index so the next cold-start can skip encode_batch.
        if embeddings is None:
            # Defensive: only reachable if a branch above failed to set
            # embeddings on the cache-miss path. Surface as a clear
            # invariant violation rather than letting build_index hit a
            # None deref.
            raise RuntimeError(
                "HNSW cache miss but no embeddings produced. This is an "
                "invariant violation in build_pipeline -- one of the "
                "model-selection branches did not encode on cache miss."
            )
        vector_store.build_index(rule_ids, embeddings)
        try:
            vector_store.save_index(corpus_hash=corpus_hash)
            _logger.info("Saved HNSW index to cache (hash=%s)", corpus_hash[:12])
        except Exception as exc:
            _logger.warning("Failed to save HNSW index: %s", exc)
            # A failed save is not cosmetic: every future cold start pays the full
            # encode again, and a _logger.warning nobody is watching is how that
            # becomes "Writ feels slow" with no trail.
            emit_exception(
                "retrieval.hnsw.save", exc, "", None, corpus_hash=corpus_hash[:12],
            )

    # Build adjacency cache (Stage 4).
    adjacency_cache = AdjacencyCache()
    await adjacency_cache.build_from_db(db)

    # Load Abstraction nodes so summary-mode can return abstraction summaries
    # instead of raw rule renders when budget_tokens < SUMMARY_THRESHOLD.
    abstractions = await db.get_all_abstractions()

    node_routes = await db.get_category_routes_by_node()
    if not node_routes:
        # Empty map = graph carries no BELONGS_TO/Category routing data (e.g. a
        # Rules-only test graph). Fall back to the legacy Stage-1 filter.
        node_routes = None
    else:
        missing = [rid for rid in rule_metadata if not node_routes.get(rid)]
        if missing:
            # Incomplete coverage: some candidate has no Category route (e.g. a
            # graph-authored rule from `writ add` / `/propose`, which never sets a
            # Category). _routes_for is fail-closed, so wiring a partial map would
            # silently drop these ids from every semantic query. Fall back to the
            # legacy Stage-1 filter for the whole pipeline -- behaviorally identical
            # to the route map on a fully categorized corpus -- rather than failing
            # the build: build_pipeline runs inside the FastAPI lifespan, so a raise
            # here would crash-loop the daemon. `writ validate`
            # (detect_category_reachability) surfaces the gap for a human to fix.
            _logger.warning(
                "node_routes incomplete: %d/%d candidate ids have no Category route "
                "(e.g. %s); falling back to the legacy Rule-only/domain-exclude Stage-1 "
                "filter. Assign a Category to these nodes to enable data-driven routing.",
                len(missing), len(rule_metadata), missing[:5],
            )
            node_routes = None

    return RetrievalPipeline(
        keyword_index=keyword_index,
        vector_store=vector_store,
        adjacency_cache=adjacency_cache,
        embedding_model=query_encoder,
        rule_metadata=rule_metadata,
        weights=weights,
        authority_preference_threshold=authority_preference_threshold,
        abstention_threshold=abstention_threshold,
        abstractions=abstractions,
        node_routes=node_routes,
    )
