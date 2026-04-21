"""Phase 0 standalone methodology retrieval benchmark runner.

Usage:
  .venv/bin/python benchmarks/methodology_bench.py            # summary table
  .venv/bin/python benchmarks/methodology_bench.py --verbose  # per-query detail
  .venv/bin/python benchmarks/methodology_bench.py --json     # full JSON for phase-0 report

Reads synthetic_methodology/*.md and ground_truth_proc.candidates.json, builds
an in-process BM25 + ONNX-vector pipeline (mirrors Writ's Stages 2-3-5), and
reports MRR@5, hit rate, bundle completeness, p95 latency against plan
Section 5.3 release-blocker thresholds.

Does NOT touch Neo4j. Does NOT use `writ serve`. Read-only to production per
plan Section 5.5.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root on sys.path for tests.* imports when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np

from tests.fixtures.methodology_loader import (
    build_adjacency,
    build_methodology_index,
    load_corpus,
    load_ground_truth,
)
from tests.test_methodology_retrieval import (
    BLOCKER_COMPLETENESS,
    BLOCKER_HIT_RATE,
    BLOCKER_MRR,
    BLOCKER_P95_MS,
    bundle_for,
    retrieve,
)
from writ.retrieval.embeddings import CachedEncoder, OnnxEmbeddingModel


def run() -> dict:
    corpus = load_corpus()
    retrievable = [n for n in corpus if n.is_retrievable]
    gt = load_ground_truth()

    idx = build_methodology_index(corpus)

    enc = CachedEncoder(OnnxEmbeddingModel())
    texts = [f"{n.trigger} {n.statement}" for n in retrievable]
    vecs = enc.encode_batch(texts)
    node_vectors = {n.node_id: np.asarray(vecs[i], dtype=np.float32) for i, n in enumerate(retrievable)}
    adjacency = build_adjacency(corpus)

    per_query = []
    for q in gt["queries"]:
        expected = q["expected_node_ids"]
        top_k, retrieval_ms, encode_ms = retrieve(q["query"], idx, enc, node_vectors, top_k=5)
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
    mrr = sum(r["rr"] for r in per_query) / n
    hit_rate = sum(1 for r in per_query if r["hit"]) / n
    completeness_mean = sum(r["completeness"] for r in per_query) / n
    p95_retrieval = retrieval_latencies[int(0.95 * n)]
    p95_encode = encode_latencies[int(0.95 * n)]

    return {
        "corpus": {
            "total_nodes": len(corpus),
            "retrievable_nodes": len(retrievable),
            "by_type": _count_by_type(corpus),
        },
        "query_set": {
            "n_queries": n,
            "source_pin": gt.get("_source_pin"),
            "status": gt.get("_status"),
        },
        "metrics": {
            "mrr_at_5": mrr,
            "hit_rate": hit_rate,
            "bundle_completeness": completeness_mean,
            "p95_retrieval_ms": p95_retrieval,
            "mean_retrieval_ms": sum(retrieval_latencies) / n,
            "p95_encode_ms": p95_encode,
            "mean_encode_ms": sum(encode_latencies) / n,
        },
        "blockers": {
            "mrr_at_5": {"measured": mrr, "threshold": BLOCKER_MRR, "pass": mrr >= BLOCKER_MRR},
            "hit_rate": {"measured": hit_rate, "threshold": BLOCKER_HIT_RATE, "pass": hit_rate >= BLOCKER_HIT_RATE},
            "bundle_completeness": {"measured": completeness_mean, "threshold": BLOCKER_COMPLETENESS, "pass": completeness_mean >= BLOCKER_COMPLETENESS},
            "p95_retrieval_ms": {"measured": p95_retrieval, "threshold": BLOCKER_P95_MS, "pass": p95_retrieval <= BLOCKER_P95_MS},
        },
        "per_query": per_query,
    }


def _count_by_type(nodes) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n.node_type] = counts.get(n.node_type, 0) + 1
    return dict(sorted(counts.items()))


def _print_summary(r: dict) -> None:
    c = r["corpus"]
    q = r["query_set"]
    m = r["metrics"]
    b = r["blockers"]
    print(f"Corpus:      {c['total_nodes']} total ({c['retrievable_nodes']} retrievable)")
    print(f"By type:     {c['by_type']}")
    print(f"Queries:     {q['n_queries']} from {q.get('source_pin')}")
    print()
    fmt = "  {:<22} {:>8}  (blocker {} {}, {})"
    print("Metrics and release blockers:")
    print(fmt.format("MRR@5",               f"{m['mrr_at_5']:.4f}",              ">=", BLOCKER_MRR,          "PASS" if b["mrr_at_5"]["pass"] else "FAIL"))
    print(fmt.format("Hit rate",            f"{m['hit_rate']:.4f}",              ">=", BLOCKER_HIT_RATE,     "PASS" if b["hit_rate"]["pass"] else "FAIL"))
    print(fmt.format("Bundle completeness", f"{m['bundle_completeness']:.4f}",   ">=", BLOCKER_COMPLETENESS, "PASS" if b["bundle_completeness"]["pass"] else "FAIL"))
    print(fmt.format("p95 retrieval (ms)",  f"{m['p95_retrieval_ms']:.2f}",      "<=", BLOCKER_P95_MS,       "PASS" if b["p95_retrieval_ms"]["pass"] else "FAIL"))
    print(f"  (mean retrieval {m['mean_retrieval_ms']:.2f}ms; encode p95 {m['p95_encode_ms']:.2f}ms mean {m['mean_encode_ms']:.2f}ms, reported for visibility — not gated)")
    all_pass = all(v["pass"] for v in b.values())
    print()
    print(f"Overall: {'ALL BLOCKERS PASS' if all_pass else 'BLOCKER FAILURE — write docs/phase-0-report.md and escalate'}")


def _print_verbose(r: dict) -> None:
    print()
    print("Per-query:")
    for pq in r["per_query"]:
        status = "HIT " if pq["hit"] else "MISS"
        print(f"  {status} {pq['id']:<5} rr={pq['rr']:.2f} comp={pq['completeness']:.2f} ret={pq['retrieval_ms']:5.1f}ms enc={pq['encode_ms']:5.1f}ms  {pq['query'][:60]}")
        if not pq["hit"]:
            print(f"         expected primary: {pq['expected_primary']}")
            print(f"         top 5:            {pq['top_k']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="Emit full JSON report on stdout")
    p.add_argument("--verbose", action="store_true", help="Emit per-query detail after summary")
    args = p.parse_args()
    r = run()
    if args.json:
        print(json.dumps(r, indent=2, default=float))
        return 0
    _print_summary(r)
    if args.verbose:
        _print_verbose(r)
    return 0 if all(v["pass"] for v in r["blockers"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
