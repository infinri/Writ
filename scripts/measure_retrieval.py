#!/usr/bin/env python3
"""Measure retrieval before and after a ranking change.

Program rule (docs/programs/knowledge-engine-program.md): measure every ranking change,
report hit@5, MRR@5 and nDCG@10 with 95% bootstrap intervals; overlapping intervals mean
no decision.

Read-only. Builds the pipeline once against the ISOLATED test graph and scores the gold
set with the benchmark's own scorers (tests/fixtures/retrieval_scoring.py, the functions
benchmarks/bench_targets.py uses), so these are the benchmark's numbers with intervals.

    python3 scripts/measure_retrieval.py measure OUT.json
    python3 scripts/measure_retrieval.py compare BEFORE.json AFTER.json DOC.md

measure scores two arms: ungated (build_pipeline's default, what `make bench` measures)
and gated at RULE_INJECTION_ABSTENTION_THRESHOLD (what the daemon serves). Per arm it keeps
per-query hit@5 and nDCG@10 over every gold query, RR@5 over the ambiguous subset (MRR@5,
the benchmark's canonical gate metric, is its mean), the false-injection rate over the
negatives, and a 95% percentile-bootstrap interval for each mean.

compare writes a markdown record: each metric before and after with its interval, a
verdict that names a direction only when the two intervals are disjoint, and the exact
paired sign test over the per-query values.

The target is tests/_graph.py's isolated instance and nothing else. Start it with
`PYTHON=$(command -v python3) bash scripts/test-graph.sh up`.
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GOLD_PATH = REPO_ROOT / "tests" / "fixtures" / "ground_truth_queries.json"
NEGATIVES_PATH = REPO_ROOT / "tests" / "fixtures" / "ground_truth_negatives.json"
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0
METRICS = ("hit@5", "mrr@5_ambiguous", "ndcg@10")
ARMS = ("ungated", "gated")


def bootstrap_ci(values: list[float], resamples: int | None = None,
                 seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% percentile-bootstrap interval of the mean of `values`, deterministic per seed."""
    if not values:
        return (0.0, 0.0)
    resamples = resamples or BOOTSTRAP_RESAMPLES
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    return (means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1])


def summarize(per_query: dict[str, list[float]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, values in per_query.items():
        lo, hi = bootstrap_ci(values)
        mean = sum(values) / len(values) if values else 0.0
        out[name] = {"mean": mean, "ci95": [lo, hi], "n": len(values)}
    return out


def score_arm(pipeline, gold: list[dict], negatives: list[dict]) -> dict:
    from tests.fixtures.retrieval_scoring import per_query_ndcg10, per_query_reciprocal_ranks

    rr_all = per_query_reciprocal_ranks(pipeline, gold)
    ambiguous = [q for q in gold if q.get("set") == "ambiguous"]
    per_query = {
        "hit@5": [1.0 if rr > 0.0 else 0.0 for rr in rr_all],
        "mrr@5_ambiguous": per_query_reciprocal_ranks(pipeline, ambiguous),
        "ndcg@10": per_query_ndcg10(pipeline, gold),
    }
    injected = [1.0 if pipeline.query(n["query"])["rules"] else 0.0 for n in negatives]
    return {
        "per_query": per_query,
        "summary": summarize(per_query),
        "false_injection_rate": sum(injected) / len(injected) if injected else 0.0,
    }


async def _build_pipeline():
    from tests._graph import ISOLATED_NEO4J_PASSWORD, ISOLATED_NEO4J_URI, ISOLATED_NEO4J_USER
    from writ.graph.db import Neo4jConnection
    from writ.retrieval.pipeline import build_pipeline

    db = Neo4jConnection(ISOLATED_NEO4J_URI, ISOLATED_NEO4J_USER, ISOLATED_NEO4J_PASSWORD)
    try:
        return await build_pipeline(db)
    finally:
        await db.close()


def measure(out_path: Path) -> None:
    from writ.retrieval.pipeline import RULE_INJECTION_ABSTENTION_THRESHOLD, VECTOR_CANDIDATE_LIMIT

    gold = json.loads(GOLD_PATH.read_text())["queries"]
    negatives = json.loads(NEGATIVES_PATH.read_text())["negatives"]
    pipeline = asyncio.run(_build_pipeline())
    record: dict = {"vector_candidate_limit": VECTOR_CANDIDATE_LIMIT, "arms": {}}
    for arm, threshold in zip(ARMS, (0.0, RULE_INJECTION_ABSTENTION_THRESHOLD)):
        pipeline._abstention_threshold = threshold
        record["arms"][arm] = {"threshold": threshold, **score_arm(pipeline, gold, negatives)}
    out_path.write_text(json.dumps(record, indent=2) + "\n")
    for arm in ARMS:
        data = record["arms"][arm]
        for name in METRICS:
            s = data["summary"][name]
            print(f"{arm:8} {name:16} {s['mean']:.4f}  95% CI "
                  f"[{s['ci95'][0]:.4f}, {s['ci95'][1]:.4f}]  n={s['n']}")
        print(f"{arm:8} {'false-injection':16} {data['false_injection_rate']:.4f}")


def _verdict(before: dict, after: dict) -> str:
    if after["ci95"][0] > before["ci95"][1]:
        return "improved"
    if after["ci95"][1] < before["ci95"][0]:
        return "regressed"
    return "no decision"


def render_comparison(before: dict, after: dict) -> str:
    from tests.fixtures.retrieval_scoring import paired_sign_test

    lines = [
        "# Retrieval measurement: before and after",
        "",
        "Generated by `scripts/measure_retrieval.py compare` from two `measure` runs against",
        "the isolated test graph. Intervals are 95% percentile bootstrap "
        f"({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}). Program rule: a metric",
        "moves only when its two intervals are disjoint; overlapping intervals are no decision.",
        "The intervals are unpaired although the queries are paired, so the rule is conservative:",
        "it leans toward no decision.",
        "The sign test counts per-query wins and losses of AFTER over BEFORE, ties dropped.",
        "",
        f"VECTOR_CANDIDATE_LIMIT: before {before['vector_candidate_limit']}, "
        f"after {after['vector_candidate_limit']}.",
        "",
        "| arm | metric | before | before 95% CI | after | after 95% CI | verdict "
        "| sign test (wins/losses, p) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in ARMS:
        b_arm, a_arm = before["arms"][arm], after["arms"][arm]
        for name in METRICS:
            b, a = b_arm["summary"][name], a_arm["summary"][name]
            wins, losses, p = paired_sign_test(a_arm["per_query"][name], b_arm["per_query"][name])
            lines.append(
                f"| {arm} | {name} | {b['mean']:.4f} | [{b['ci95'][0]:.4f}, {b['ci95'][1]:.4f}] "
                f"| {a['mean']:.4f} | [{a['ci95'][0]:.4f}, {a['ci95'][1]:.4f}] "
                f"| {_verdict(b, a)} | {wins}/{losses}, p={p:.4f} |"
            )
        lines.append(
            f"| {arm} | false-injection rate (negatives) | {b_arm['false_injection_rate']:.4f} "
            f"| | {a_arm['false_injection_rate']:.4f} | | | |"
        )
    return "\n".join(lines) + "\n"


def compare(before_path: Path, after_path: Path, doc_path: Path) -> None:
    before = json.loads(before_path.read_text())
    after = json.loads(after_path.read_text())
    doc_path.write_text(render_comparison(before, after))
    print(doc_path.read_text())


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "measure":
        measure(Path(argv[2]))
        return 0
    if len(argv) == 5 and argv[1] == "compare":
        compare(Path(argv[2]), Path(argv[3]), Path(argv[4]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
