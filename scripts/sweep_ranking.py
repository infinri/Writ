#!/usr/bin/env python3
"""Offline, read-only ranking-lever sweep (KG step 0, commit 4 evidence).

Sweeps two ranking levers against the 165-query gold set and prints the
measured MRR@5 (ambiguous subset) / hit-rate@5 (all queries) for each:

  1. w_graph in {0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20}, with
     the four non-graph weights proportionally rebalanced so RankingWeights
     still sums to 1.0.
  2. authority_preference_threshold in {0.0, 0.02, 0.05, 0.10}, with default
     weights. Measured 2026-08-06: byte-identical at every threshold. The corpus
     holds one ai-provisional node and its category is not semantic-routed, so
     the default query path never ranks one. The lever ships configurable and
     OFF; see benchmarks/RANKING-LEVERS-2026-08-06.md.

Indexes are built ONCE via build_pipeline; every combo reuses the same BM25 /
vector / adjacency / model / metadata / abstractions / node_routes, so only the
ranking parameters vary. Read-only: no graph wipe, no write, no external API.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Running `python scripts/sweep_ranking.py` puts scripts/ (not the repo root)
# on sys.path[0], so the tests.fixtures scorer import below would fail. Add the
# repo root explicitly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.fixtures.retrieval_scoring import (  # noqa: E402
    hit_rate_at_5,
    mrr_at_5,
    ndcg_at_10,
    paired_sign_test,
    per_query_reciprocal_ranks,
)
from writ.config import (  # noqa: E402
    get_neo4j_password,
    get_neo4j_uri,
    get_neo4j_user,
)
from writ.graph.db import Neo4jConnection  # noqa: E402
from writ.retrieval.pipeline import RetrievalPipeline, build_pipeline  # noqa: E402
from writ.retrieval.ranking import RankingWeights  # noqa: E402

GROUND_TRUTH_PATH = _REPO_ROOT / "tests" / "fixtures" / "ground_truth_queries.json"

# Base non-graph weights (sum 0.99); scaled by (1 - w_graph) / 0.99 so the
# five weights plus w_graph still sum to 1.0 at every sweep point.
_BASE_W_BM25 = 0.198
_BASE_W_VECTOR = 0.594
_BASE_W_SEVERITY = 0.099
_BASE_W_CONFIDENCE = 0.099
_BASE_SUM = _BASE_W_BM25 + _BASE_W_VECTOR + _BASE_W_SEVERITY + _BASE_W_CONFIDENCE

W_GRAPH_SWEEP = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
AUTHORITY_SWEEP = [0.0, 0.02, 0.05, 0.10]
BASELINE_W_GRAPH = 0.01


def _load_ground_truth() -> tuple[list[dict], list[dict]]:
    data = json.loads(GROUND_TRUTH_PATH.read_text())
    queries = data["queries"]
    ambiguous = [q for q in queries if q["set"] == "ambiguous"]
    return ambiguous, queries


def _rebalanced_weights(w_graph: float) -> RankingWeights:
    scale = (1.0 - w_graph) / _BASE_SUM
    return RankingWeights(
        w_bm25=_BASE_W_BM25 * scale,
        w_vector=_BASE_W_VECTOR * scale,
        w_severity=_BASE_W_SEVERITY * scale,
        w_confidence=_BASE_W_CONFIDENCE * scale,
        w_graph=w_graph,
    )


def _pipeline_with(
    p: RetrievalPipeline,
    weights: RankingWeights,
    authority_preference_threshold: float,
) -> RetrievalPipeline:
    """Clone the built pipeline's indexes with new ranking parameters."""
    return RetrievalPipeline(
        keyword_index=p._keyword,
        vector_store=p._vector,
        adjacency_cache=p._cache,
        embedding_model=p._model,
        rule_metadata=p._metadata,
        weights=weights,
        authority_preference_threshold=authority_preference_threshold,
        abstractions=p._abstractions,
        node_routes=p._node_routes,
    )


def _score(
    p: RetrievalPipeline, ambiguous: list[dict], queries: list[dict]
) -> tuple[float, float, float, list[float]]:
    mrr5, _ = mrr_at_5(p, ambiguous)
    hit, _ = hit_rate_at_5(p, queries)
    ndcg10, _ = ndcg_at_10(p, queries)
    # Per-query RR@5 over the full query set feeds the paired sign test so the
    # A/B comparison has statistical significance built in.
    rr = per_query_reciprocal_ranks(p, queries)
    return mrr5, hit, ndcg10, rr


async def main() -> None:
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        count = await db.count_rules()
        if count == 0:
            print("Neo4j has no rules. Run: `writ import-markdown bible/`")
            return

        ambiguous, queries = _load_ground_truth()
        print(
            f"Gold set: {len(queries)} queries "
            f"({len(ambiguous)} ambiguous for MRR@5, all for hit-rate@5)"
        )
        print("Building indexes once via build_pipeline (read-only)...")
        p = await build_pipeline(db)

        # Per-arm per-query RR@5, keyed by arm label, for the paired sign test.
        arm_rrs: dict[str, list[float]] = {}
        baseline_label = f"w_graph={BASELINE_W_GRAPH:.2f}"

        # ---- Sweep 1: w_graph (rebalanced non-graph weights) ----
        print("\n== w_graph sweep (non-graph weights rebalanced to sum 1.0) ==")
        print(f"  {'w_graph':>8}  {'MRR@5':>8}  {'hit@5':>8}  {'nDCG@10':>8}")
        w_graph_rows: list[tuple[float, float, float, float]] = []
        for wg in W_GRAPH_SWEEP:
            pipeline = _pipeline_with(p, _rebalanced_weights(wg), 0.0)
            mrr5, hit, ndcg10, rr = _score(pipeline, ambiguous, queries)
            w_graph_rows.append((wg, mrr5, hit, ndcg10))
            arm_rrs[f"w_graph={wg:.2f}"] = rr
            marker = "  <- baseline" if wg == BASELINE_W_GRAPH else ""
            print(f"  {wg:>8.2f}  {mrr5:>8.4f}  {hit:>8.4f}  {ndcg10:>8.4f}{marker}")

        # ---- Sweep 2: authority_preference_threshold (default weights) ----
        print("\n== authority_preference_threshold sweep (default weights) ==")
        print(f"  {'thresh':>8}  {'MRR@5':>8}  {'hit@5':>8}  {'nDCG@10':>8}")
        for thresh in AUTHORITY_SWEEP:
            pipeline = _pipeline_with(p, RankingWeights(), thresh)
            mrr5, hit, ndcg10, rr = _score(pipeline, ambiguous, queries)
            arm_rrs[f"authority={thresh:.2f}"] = rr
            print(f"  {thresh:>8.2f}  {mrr5:>8.4f}  {hit:>8.4f}  {ndcg10:>8.4f}")

        # ---- Paired sign test vs baseline (significance for future A/B) ----
        # Two-sided exact sign test on per-query RR@5, each arm vs the
        # w_graph=0.01 baseline. n_pos = queries where the arm beats baseline,
        # n_neg = queries where it loses; ties dropped. Low p => the arm's
        # per-query ranking differs from baseline beyond chance.
        baseline_rr = arm_rrs[baseline_label]
        print(
            f"\n== paired sign test: per-query RR@5 (all {len(queries)} queries) "
            f"vs baseline ({baseline_label}) =="
        )
        print(f"  {'arm':>18}  {'n_pos':>6}  {'n_neg':>6}  {'p_value':>9}")
        for label, rr in arm_rrs.items():
            if label == baseline_label:
                continue
            n_pos, n_neg, p_value = paired_sign_test(rr, baseline_rr)
            print(f"  {label:>18}  {n_pos:>6}  {n_neg:>6}  {p_value:>9.4f}")

        # ---- Do-no-harm winner ----
        # Best MRR@5 among w_graph rows that regress neither metric below the
        # w_graph=0.01 baseline (ties broken toward lower w_graph = smaller change).
        baseline = next(
            (r for r in w_graph_rows if r[0] == BASELINE_W_GRAPH), w_graph_rows[0]
        )
        _, base_mrr, base_hit, _base_ndcg = baseline
        candidates = [
            r for r in w_graph_rows if r[1] >= base_mrr and r[2] >= base_hit
        ]
        winner = max(candidates, key=lambda r: (r[1], -r[0])) if candidates else baseline
        print(
            f"\nBaseline (w_graph={BASELINE_W_GRAPH}): "
            f"MRR@5={base_mrr:.4f}, hit@5={base_hit:.4f}"
        )
        print(
            f"Do-no-harm winner: w_graph={winner[0]:.2f}  "
            f"MRR@5={winner[1]:.4f}  hit@5={winner[2]:.4f} "
            f"(both metrics >= baseline)"
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
