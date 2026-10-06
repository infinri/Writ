"""Shared retrieval scorers (DRY-DUP-001).

Two pure functions extracted from the duplicated MRR@5 / hit-rate@5 scoring
loops previously copy-pasted in ``tests/test_graph_proximity.py`` and
``benchmarks/bench_targets.py``. Both are plain functions over a duck-typed
pipeline (``pipeline.query(text) -> {"rules": [{"rule_id": ...}, ...]}``) and a
list of query dicts exposing ``query`` / ``expected_rule_id`` / ``id``.

No pytest, fixture or corpus coupling, and no driver import, so this module is
import-safe from ``tests/``, ``benchmarks/`` and ``scripts/``. The one graph-reading
helper, ``routed_target_findings``, runs its query through a session the caller opened.
"""
from __future__ import annotations

import math


def per_query_reciprocal_ranks(pipeline, queries) -> list[float]:
    """Per-query reciprocal rank at 5 over ``queries``.

    Single source of the reciprocal-rank math (DRY-DUP-001): for each query the
    expected rule's reciprocal rank (1/(idx+1)) is scored only when it lands in
    the top-5, otherwise 0.0. Returns one float per query, in query order.
    """
    reciprocal_ranks: list[float] = []
    for q in queries:
        result = pipeline.query(q["query"])
        top5_ids = [r["rule_id"] for r in result["rules"][:5]]
        expected = q["expected_rule_id"]
        if expected in top5_ids:
            rank = top5_ids.index(expected) + 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)
    return reciprocal_ranks


def mrr_at_5(pipeline, queries) -> tuple[float, list[str]]:
    """Mean reciprocal rank at 5 over ``queries``.

    For each query, the expected rule's reciprocal rank (1/(idx+1)) is counted
    only when it lands in the top-5; otherwise it scores 0.0 and the query's id
    is recorded as a miss. Returns (mrr5, miss_ids). An empty query list returns
    (0.0, []). Reuses ``per_query_reciprocal_ranks`` so RR math lives once.
    """
    if not queries:
        return 0.0, []

    reciprocal_ranks = per_query_reciprocal_ranks(pipeline, queries)
    miss_ids = [q["id"] for q, rr in zip(queries, reciprocal_ranks) if rr == 0.0]
    mrr5 = sum(reciprocal_ranks) / len(reciprocal_ranks)
    return mrr5, miss_ids


def per_query_ndcg10(pipeline, queries) -> list[float]:
    """Per-query nDCG@10 over ``queries`` (single-gold).

    With exactly one relevant item per query IDCG = 1, so nDCG@10 collapses to
    ``1/log2(rank+1)`` when the gold id is in the top-10 and ``0.0`` otherwise.
    Returns one float per query, in query order.
    """
    values: list[float] = []
    for q in queries:
        result = pipeline.query(q["query"])
        top10_ids = [r["rule_id"] for r in result["rules"][:10]]
        expected = q["expected_rule_id"]
        if expected in top10_ids:
            rank = top10_ids.index(expected) + 1
            values.append(1.0 / math.log2(rank + 1))
        else:
            values.append(0.0)
    return values


def ndcg_at_10(pipeline, queries) -> tuple[float, list[str]]:
    """Mean single-gold nDCG@10 over ``queries``.

    Returns (mean_ndcg10, miss_ids), where miss_ids are the ids whose gold rule
    is absent from the top-10. An empty query list returns (0.0, []).
    """
    if not queries:
        return 0.0, []

    values = per_query_ndcg10(pipeline, queries)
    miss_ids = [q["id"] for q, v in zip(queries, values) if v == 0.0]
    return sum(values) / len(values), miss_ids


def paired_sign_test(scores_a, scores_b) -> tuple[int, int, float]:
    """Two-sided exact paired sign test between two equal-length score lists.

    Counts positive (a > b) and negative (a < b) differences; ties (a == b) are
    dropped. Returns (n_pos, n_neg, p_value). With n = n_pos + n_neg non-tie
    pairs and k = min(n_pos, n_neg),
    ``p = min(1.0, 2 * sum(C(n, i) for i in range(k+1)) * 0.5**n)``. When n == 0
    (all ties or empty input) returns (n_pos, n_neg, 1.0). Raises ValueError if
    the two lists differ in length.
    """
    if len(scores_a) != len(scores_b):
        raise ValueError(
            f"paired_sign_test requires equal-length lists, got "
            f"{len(scores_a)} and {len(scores_b)}"
        )

    n_pos = sum(1 for a, b in zip(scores_a, scores_b) if a > b)
    n_neg = sum(1 for a, b in zip(scores_a, scores_b) if a < b)
    n = n_pos + n_neg
    if n == 0:
        return n_pos, n_neg, 1.0

    k = min(n_pos, n_neg)
    p = 2.0 * sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
    return n_pos, n_neg, min(1.0, p)


def hit_rate_at_5(pipeline, queries) -> tuple[float, list[str]]:
    """Hit rate at 5 over ``queries``.

    Counts each query where the expected rule appears anywhere in the top-5;
    otherwise records the query's id as a miss. Returns (hit_rate, miss_ids). An
    empty query list returns (0.0, []).
    """
    if not queries:
        return 0.0, []

    hits = 0
    miss_ids: list[str] = []
    for q in queries:
        result = pipeline.query(q["query"])
        top5_ids = [r["rule_id"] for r in result["rules"][:5]]
        if q["expected_rule_id"] in top5_ids:
            hits += 1
        else:
            miss_ids.append(q["id"])

    hit_rate = hits / len(queries)
    return hit_rate, miss_ids


def split_by_channel(queries, mandatory_ids, routes_map) -> tuple[list, list, list]:
    """Partition gold queries by delivery channel (HANDBOOK section 10).

    eligible: semantic-mode /query can return the expected rule, so rank metrics apply.
    Eligibility is the ROUTE data (Stage 1 admits a candidate only when its category
    routes include 'semantic'), not candidate-set membership: the pipeline's metadata
    holds every candidate, but the route filter runs at query time. A rule with no route
    entry is universal (eligible).
    always_on: the expected rule is mandatory; delivered via /always-on, never ranked.
    routed: category routes exclude 'semantic'; delivered via the state/action/pull
    channels (methodology companion), unreachable by /query BY DESIGN.

    ``routes_map`` is the pipeline's node-routes map (rule id -> route tags). Returns
    (eligible, always_on, routed), each a list of the query dicts in input order.
    """
    eligible, always_on, routed = [], [], []
    for q in queries:
        rid = q["expected_rule_id"]
        routes = routes_map.get(rid)
        if rid in mandatory_ids:
            always_on.append(q)
        elif routes is not None and "semantic" not in routes:
            routed.append(q)
        else:
            eligible.append(q)
    return eligible, always_on, routed


async def routed_target_findings(session, rule_ids, routes_map) -> dict[str, list[str]]:
    """Delivery check for routed targets: deliberate channel members, never accidents.

    orphans: rules with NO BELONGS_TO category edge (the subset-import edge bug class),
    so their routes were never deliberately declared. channelless: rules whose route list
    is empty, unreachable by ANY channel. ``session`` is an open session the caller owns.
    Both lists are sorted; a rule id with no node yields no row and is in neither.
    """
    ids = sorted(set(rule_ids))
    result = await session.run(
        "UNWIND $ids AS rid MATCH (n:Rule {rule_id: rid}) "
        "OPTIONAL MATCH (n)-[:BELONGS_TO]->(c:Category) "
        "RETURN rid AS rid, count(c) AS edges", ids=ids)
    orphans = sorted([rec["rid"] async for rec in result if rec["edges"] == 0])
    channelless = [rid for rid in ids if not routes_map.get(rid)]
    return {"orphans": orphans, "channelless": channelless}
