#!/usr/bin/env python3
"""Measure retrieval before and after a ranking change.

Program rule (docs/programs/knowledge-engine-program.md): measure every ranking change,
report hit@5, MRR@5 and nDCG@10 with 95% bootstrap intervals; overlapping intervals mean
no decision.

Read-only. Builds the pipeline once against the ISOLATED test graph, runs every gold query
and every negative once with the gate off, and records each result's top-10 rule ids and its
abstain_signal (the best raw cosine that survived the Stage 1 filter). Every arm and every
sweep threshold is scored from that one recording with the benchmark's own scorers
(tests/fixtures/retrieval_scoring.py): at threshold t the pipeline abstains exactly when
abstain_signal < t and otherwise returns what it returns ungated, so a gated result is the
recorded result or nothing. One live gated pass at RULE_INJECTION_ABSTENTION_THRESHOLD checks
that equivalence, and the run fails naming any query that disagrees. The pipeline rounds the
signal to 6 decimals, so a signal within 5e-7 of a sweep threshold may replay on the other
side of it; the live check covers the served threshold only.

    python3 scripts/measure_retrieval.py measure OUT.json
    python3 scripts/measure_retrieval.py compare BEFORE.json AFTER.json DOC.md
    python3 scripts/measure_retrieval.py sweep RUN.json DOC.md [THRESHOLD ...]

Channels. Rank metrics are scored over ranked-eligible targets; always-on (mandatory) and
routed targets get delivery checks. Nothing here re-implements a check: the partition is
retrieval_scoring.split_by_channel and the routed check retrieval_scoring.
routed_target_findings (both shared with benchmarks/bench_targets.py), the mandatory ids are
frequency_checks.mandatory_rule_ids, and always-on delivery is IntegrityChecker.
detect_stranded_mandatory. The all-queries pool is still reported, labelled, for continuity
with records made before the split.

measure keeps, per arm (ungated, and gated at RULE_INJECTION_ABSTENTION_THRESHOLD) and per
pool, per-query hit@5 and nDCG@10, RR@5 over the ambiguous subset (MRR@5, the benchmark's
canonical gate metric, is its mean), a 95% percentile-bootstrap interval for each mean, and
the false-injection rate over the negatives with a 95% Wilson interval.

compare writes a markdown record: each metric before and after with its interval, a verdict
that names a direction only when the two intervals are disjoint, the exact paired sign test,
a paired bootstrap interval of the per-query differences, and every query whose value moved.

sweep reads one measure record and scores the gated arm at each threshold (default
DEFAULT_SWEEP_THRESHOLDS) with no graph and no pipeline. For choosing gate thresholds it
supersedes scripts/measure_false_injection.py's raw-cosine sweep.

The target is tests/_graph.py's isolated instance and nothing else. Start it with
`PYTHON=$(command -v python3) bash scripts/test-graph.sh up`.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GOLD_PATH = REPO_ROOT / "tests" / "fixtures" / "ground_truth_queries.json"
NEGATIVES_PATH = REPO_ROOT / "tests" / "fixtures" / "ground_truth_negatives.json"
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0
WILSON_Z = 1.96
RECORDED_TOP_K = 10
METRICS = ("hit@5", "mrr@5_ambiguous", "ndcg@10")
ARMS = ("ungated", "gated")
POOLS = ("eligible", "all")
CHANNELS = ("eligible", "always_on", "routed")
DEFAULT_SWEEP_THRESHOLDS = (0.0, 0.20, 0.24, 0.26, 0.28, 0.30, 0.32, 0.34, 0.36, 0.38, 0.40)
DEFINITION_NOTE = (
    "Definitional change (2026-10-06, program item 1f): the `eligible` pool scores only gold "
    "queries whose expected rule /query can return. Always-on (mandatory) and routed targets "
    "are scored as delivery checks, not ranks. The `all` pool keeps the pre-split definition "
    "for continuity and is not comparable to `eligible`."
)


class Queryable(Protocol):
    """What the benchmark scorers need: query text in, ranked rules out."""

    def query(self, query_text: str) -> dict: ...


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


def paired_bootstrap_ci(after: list[float], before: list[float]) -> tuple[float, float]:
    """95% percentile-bootstrap interval of the mean per-query difference AFTER minus BEFORE.

    Resamples queries, so each query's two scores stay paired.
    """
    if len(after) != len(before):
        raise ValueError(
            f"paired_bootstrap_ci requires equal-length lists, got {len(after)} and {len(before)}"
        )
    return bootstrap_ci([a - b for a, b in zip(after, before)])


def wilson_interval(successes: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """95% Wilson score interval of a proportion; (0.0, 0.0) when n is 0."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(per_query: dict[str, list[float]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, values in per_query.items():
        lo, hi = bootstrap_ci(values)
        mean = sum(values) / len(values) if values else 0.0
        out[name] = {"mean": mean, "ci95": [lo, hi], "n": len(values)}
    return out


def delivery_record(always_on: list[dict], routed: list[dict], stranded: set[str],
                    routed_findings: dict[str, list[str]]) -> dict:
    """The delivery checks for the two pools /query cannot rank, from the shared checks'
    results: a stranded always-on target is undelivered; routed findings pass through."""
    always_on_ids = sorted({q["expected_rule_id"] for q in always_on})
    routed_ids = sorted({q["expected_rule_id"] for q in routed})
    return {
        "always_on": {"queries": len(always_on), "rules": len(always_on_ids),
                      "undelivered": [rid for rid in always_on_ids if rid in stranded]},
        "routed": {"queries": len(routed), "rules": len(routed_ids),
                   "orphans": list(routed_findings["orphans"]),
                   "channelless": list(routed_findings["channelless"])},
    }


class ReplayPipeline:
    """One recorded ungated run, served back as the pipeline serves it at `threshold`."""

    def __init__(self, recorded: dict, threshold: float) -> None:
        self._by_text = {e["query"]: e for e in recorded["gold"] + recorded["negatives"]}
        self._threshold = threshold

    def query(self, query_text: str) -> dict:
        entry = self._by_text[query_text]
        if self._threshold > 0.0 and entry["signal"] < self._threshold:
            return {"rules": []}
        return {"rules": [{"rule_id": rid} for rid in entry["top"]]}


def record_run(pipeline: Queryable, gold: list[dict], negatives: list[dict]) -> dict:
    """Run every gold query and negative once; keep the top-10 ids and the abstain signal."""

    def _result(text: str) -> dict:
        result = pipeline.query(text)
        return {"top": [r["rule_id"] for r in result["rules"][:RECORDED_TOP_K]],
                "signal": float(result["abstain_signal"])}

    return {
        "gold": [{"id": q["id"], "set": q.get("set", ""), "query": q["query"],
                  "expected_rule_id": q["expected_rule_id"], **_result(q["query"])}
                 for q in gold],
        "negatives": [{"id": n["id"], "flavor": n.get("flavor", ""), "query": n["query"],
                       **_result(n["query"])}
                      for n in negatives],
    }


def check_replay(pipeline, recorded: dict, threshold: float) -> list[str]:
    """Ids whose live result at `threshold` differs from the replay of the recorded run."""
    replay = ReplayPipeline(recorded, threshold)
    previous = pipeline._abstention_threshold
    pipeline._abstention_threshold = threshold
    try:
        return [
            e["id"] for e in recorded["gold"] + recorded["negatives"]
            if [r["rule_id"] for r in pipeline.query(e["query"])["rules"][:RECORDED_TOP_K]]
            != [r["rule_id"] for r in replay.query(e["query"])["rules"]]
        ]
    finally:
        pipeline._abstention_threshold = previous


def score_pool(pipeline: Queryable, queries: list[dict]) -> dict:
    from tests.fixtures.retrieval_scoring import per_query_ndcg10, per_query_reciprocal_ranks

    ambiguous = [q for q in queries if q.get("set") == "ambiguous"]
    rr_all = per_query_reciprocal_ranks(pipeline, queries)
    per_query = {
        "hit@5": [1.0 if rr > 0.0 else 0.0 for rr in rr_all],
        "mrr@5_ambiguous": per_query_reciprocal_ranks(pipeline, ambiguous),
        "ndcg@10": per_query_ndcg10(pipeline, queries),
    }
    all_ids = [q["id"] for q in queries]
    return {
        "ids": {"hit@5": all_ids, "mrr@5_ambiguous": [q["id"] for q in ambiguous],
                "ndcg@10": all_ids},
        "per_query": per_query,
        "summary": summarize(per_query),
    }


def false_injection(pipeline: Queryable, negatives: list[dict]) -> dict:
    injected = sum(1 for n in negatives if pipeline.query(n["query"])["rules"])
    n = len(negatives)
    return {"injected": injected, "n": n, "rate": injected / n if n else 0.0,
            "ci95": list(wilson_interval(injected, n))}


def score_arm(recorded: dict, threshold: float, eligible_ids: list[str]) -> dict:
    replay = ReplayPipeline(recorded, threshold)
    wanted = set(eligible_ids)
    pools = {"eligible": [e for e in recorded["gold"] if e["id"] in wanted],
             "all": recorded["gold"]}
    return {
        "threshold": threshold,
        "pools": {name: score_pool(replay, pools[name]) for name in POOLS},
        "false_injection": false_injection(replay, recorded["negatives"]),
    }


def corpus_digest(metadata: dict[str, dict]) -> str:
    """The pipeline's own corpus hash over every ranked node's "trigger statement" text:
    the same function and text the vector-index cache keys on."""
    from writ.retrieval.pipeline import _compute_corpus_hash_from_text

    rule_ids = list(metadata)
    texts = [f"{metadata[rid].get('trigger', '')} {metadata[rid].get('statement', '')}"
             for rid in rule_ids]
    return _compute_corpus_hash_from_text(rule_ids, texts)


def build_record(recorded: dict, channels: dict[str, list[str]], delivery: dict,
                 thresholds: dict[str, float], vector_candidate_limit: int, digest: str) -> dict:
    return {
        "definition": DEFINITION_NOTE,
        "vector_candidate_limit": vector_candidate_limit,
        "corpus_digest": digest,
        "channels": channels,
        "delivery": delivery,
        "recorded": recorded,
        "arms": {arm: score_arm(recorded, thresholds[arm], channels["eligible"]) for arm in ARMS},
    }


async def _load(gold: list[dict]):
    """Pipeline, channel ids and delivery checks: four graph reads, all through shared code."""
    from tests._graph import ISOLATED_NEO4J_PASSWORD, ISOLATED_NEO4J_URI, ISOLATED_NEO4J_USER
    from tests.fixtures.retrieval_scoring import routed_target_findings, split_by_channel
    from writ.graph.db import Neo4jConnection
    from writ.graph.integrity import IntegrityChecker
    from writ.graph.integrity.frequency_checks import mandatory_rule_ids
    from writ.retrieval.pipeline import build_pipeline

    db = Neo4jConnection(ISOLATED_NEO4J_URI, ISOLATED_NEO4J_USER, ISOLATED_NEO4J_PASSWORD)
    try:
        pipeline = await build_pipeline(db)
        routes_map = getattr(pipeline, "_node_routes", None) or {}
        async with db._driver.session(database=db._database) as s:
            mandatory_ids = await mandatory_rule_ids(s)
        eligible, always_on, routed = split_by_channel(gold, mandatory_ids, routes_map)
        stranded = set(await IntegrityChecker(db._driver, db._database).detect_stranded_mandatory())
        async with db._driver.session(database=db._database) as s:
            findings = await routed_target_findings(
                s, [q["expected_rule_id"] for q in routed], routes_map,
            )
        channels = {"eligible": [q["id"] for q in eligible],
                    "always_on": [q["id"] for q in always_on],
                    "routed": [q["id"] for q in routed]}
        return pipeline, channels, delivery_record(always_on, routed, stranded, findings)
    finally:
        await db.close()


def measure(out_path: Path) -> None:
    from writ.retrieval.pipeline import RULE_INJECTION_ABSTENTION_THRESHOLD, VECTOR_CANDIDATE_LIMIT

    gold = json.loads(GOLD_PATH.read_text())["queries"]
    negatives = json.loads(NEGATIVES_PATH.read_text())["negatives"]
    pipeline, channels, delivery = asyncio.run(_load(gold))
    recorded = record_run(pipeline, gold, negatives)
    mismatched = check_replay(pipeline, recorded, RULE_INJECTION_ABSTENTION_THRESHOLD)
    if mismatched:
        raise SystemExit(
            f"replay disagrees with the live gate at {RULE_INJECTION_ABSTENTION_THRESHOLD} for "
            f"{mismatched}; the gated arm and the sweep cannot be scored from the recording"
        )
    record = build_record(
        recorded, channels, delivery,
        {"ungated": 0.0, "gated": RULE_INJECTION_ABSTENTION_THRESHOLD},
        VECTOR_CANDIDATE_LIMIT, corpus_digest(pipeline._metadata),
    )
    out_path.write_text(json.dumps(record, indent=2) + "\n")
    print(DEFINITION_NOTE)
    print("channels: " + ", ".join(f"{name} {len(ids)}" for name, ids in channels.items()))
    print("delivery: " + json.dumps(record["delivery"]))
    print("corpus digest: " + record["corpus_digest"])
    for arm in ARMS:
        data = record["arms"][arm]
        for pool in POOLS:
            for name in METRICS:
                s = data["pools"][pool]["summary"][name]
                print(f"{arm:8} {pool:9} {name:16} {s['mean']:.4f}  95% CI "
                      f"[{s['ci95'][0]:.4f}, {s['ci95'][1]:.4f}]  n={s['n']}")
        fi = data["false_injection"]
        print(f"{arm:8} {'negatives':9} {'false-injection':16} {fi['rate']:.4f}  95% Wilson "
              f"[{fi['ci95'][0]:.4f}, {fi['ci95'][1]:.4f}]  {fi['injected']}/{fi['n']}")


def _verdict(before: dict, after: dict) -> str:
    if after["ci95"][0] > before["ci95"][1]:
        return "improved"
    if after["ci95"][1] < before["ci95"][0]:
        return "regressed"
    return "no decision"


def _fmt_ci(ci) -> str:
    return f"[{ci[0]:.4f}, {ci[1]:.4f}]"


def render_comparison(before: dict, after: dict) -> str:
    from tests.fixtures.retrieval_scoring import paired_sign_test

    b_del, a_del = before["delivery"], after["delivery"]
    lines = [
        "# Retrieval measurement: before and after",
        "",
        "Generated by `scripts/measure_retrieval.py compare` from two `measure` runs against",
        "the isolated test graph. Intervals are 95% percentile bootstrap "
        f"({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}); false-injection intervals",
        "are 95% Wilson. Program rule: a metric moves only when its two intervals are disjoint;",
        "overlapping intervals are no decision. Those intervals are unpaired although the queries",
        "are paired, so the rule leans toward no decision. The paired column resamples queries and",
        "gives the interval of the mean per-query difference (AFTER minus BEFORE); it is supporting",
        "evidence and does not change the verdict.",
        "The sign test counts per-query wins and losses of AFTER over BEFORE, ties dropped.",
        "",
        DEFINITION_NOTE,
        "",
        f"VECTOR_CANDIDATE_LIMIT: before {before['vector_candidate_limit']}, "
        f"after {after['vector_candidate_limit']}.",
        f"Corpus digest: before {before['corpus_digest'][:12]}, "
        f"after {after['corpus_digest'][:12]}.",
        "Channels (queries): " + ", ".join(
            f"{name} {len(before['channels'][name])} -> {len(after['channels'][name])}"
            for name in CHANNELS
        ) + ".",
        f"Delivery: always-on undelivered {b_del['always_on']['undelivered']} -> "
        f"{a_del['always_on']['undelivered']}; routed orphans {b_del['routed']['orphans']} -> "
        f"{a_del['routed']['orphans']}, channelless {b_del['routed']['channelless']} -> "
        f"{a_del['routed']['channelless']}.",
        "",
        "| arm | pool | metric | before | before 95% CI | after | after 95% CI | verdict "
        "| sign test (wins/losses, p) | paired diff 95% CI |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    changed: list[str] = []
    for arm in ARMS:
        b_arm, a_arm = before["arms"][arm], after["arms"][arm]
        for pool in POOLS:
            b_pool, a_pool = b_arm["pools"][pool], a_arm["pools"][pool]
            for name in METRICS:
                if b_pool["ids"][name] != a_pool["ids"][name]:
                    raise ValueError(f"{arm}/{pool}/{name}: the two runs scored different queries")
                bv, av = b_pool["per_query"][name], a_pool["per_query"][name]
                b, a = b_pool["summary"][name], a_pool["summary"][name]
                wins, losses, p = paired_sign_test(av, bv)
                lines.append(
                    f"| {arm} | {pool} | {name} | {b['mean']:.4f} | {_fmt_ci(b['ci95'])} "
                    f"| {a['mean']:.4f} | {_fmt_ci(a['ci95'])} | {_verdict(b, a)} "
                    f"| {wins}/{losses}, p={p:.4f} | {_fmt_ci(paired_bootstrap_ci(av, bv))} |"
                )
                if pool == "all":
                    changed += [
                        f"| {arm} | {qid} | {name} | {x:.4f} | {y:.4f} |"
                        for qid, x, y in zip(b_pool["ids"][name], bv, av) if x != y
                    ]
        b_fi, a_fi = b_arm["false_injection"], a_arm["false_injection"]
        lines.append(
            f"| {arm} | negatives | false-injection rate | {b_fi['rate']:.4f} "
            f"| {_fmt_ci(b_fi['ci95'])} | {a_fi['rate']:.4f} | {_fmt_ci(a_fi['ci95'])} | | | |"
        )
    lines += ["", "## Queries whose value changed (all pool)", ""]
    if changed:
        lines += ["| arm | query | metric | before | after |", "|---|---|---|---|---|", *changed]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def compare(before_path: Path, after_path: Path, doc_path: Path) -> None:
    before = json.loads(before_path.read_text())
    after = json.loads(after_path.read_text())
    doc_path.write_text(render_comparison(before, after))
    print(doc_path.read_text())


def render_sweep(record: dict, thresholds) -> str:
    recorded = record["recorded"]
    eligible_ids = record["channels"]["eligible"]
    base = score_arm(recorded, 0.0, eligible_ids)
    rows = {t: score_arm(recorded, t, eligible_ids) for t in thresholds}
    lines = [
        "# Abstention threshold sweep",
        "",
        "Generated by `scripts/measure_retrieval.py sweep` from one `measure` record. Every row",
        "replays the same ungated run: a query gets its recorded result when its abstain_signal is",
        "at or above the threshold and nothing otherwise, which is the pipeline's gate. Metric",
        f"intervals are 95% percentile bootstrap ({BOOTSTRAP_RESAMPLES} resamples, seed "
        f"{BOOTSTRAP_SEED}); false-injection intervals are 95% Wilson. Lost: gold queries with a",
        "top-5 hit ungated and none at this threshold.",
        "",
        DEFINITION_NOTE,
        "",
        f"Corpus digest {record['corpus_digest'][:12]}; {len(recorded['gold'])} gold queries, "
        f"{len(recorded['negatives'])} negatives.",
    ]
    for pool in POOLS:
        base_pool = base["pools"][pool]
        base_hits = dict(zip(base_pool["ids"]["hit@5"], base_pool["per_query"]["hit@5"]))
        lines += [
            "",
            f"## Pool: {pool} (n={len(base_pool['ids']['hit@5'])})",
            "",
            "| threshold | hit@5 | MRR@5 ambiguous | nDCG@10 | false injection | lost |",
            "|---|---|---|---|---|---|",
        ]
        for t, arm in rows.items():
            p = arm["pools"][pool]
            cells = [f"{p['summary'][m]['mean']:.4f} {_fmt_ci(p['summary'][m]['ci95'])}"
                     for m in METRICS]
            fi = arm["false_injection"]
            lost = [qid for qid, v in zip(p["ids"]["hit@5"], p["per_query"]["hit@5"])
                    if base_hits[qid] > v]
            lines.append(
                f"| {t:.2f} | " + " | ".join(cells)
                + f" | {fi['injected']}/{fi['n']} {fi['rate']:.2f} {_fmt_ci(fi['ci95'])} "
                + f"| {', '.join(lost) or 'none'} |"
            )
    all_hits = dict(zip(base["pools"]["all"]["ids"]["hit@5"],
                        base["pools"]["all"]["per_query"]["hit@5"]))
    lowest = sorted((e["signal"], e["id"]) for e in recorded["gold"] if all_hits[e["id"]] > 0.0)[:5]
    negatives = sorted(recorded["negatives"], key=lambda e: e["signal"], reverse=True)
    lines += [
        "",
        "## Signals",
        "",
        "Negatives, highest first: "
        + ", ".join(f"{e['id']} ({e['flavor']}) {e['signal']:.3f}" for e in negatives) + ".",
        "",
        "Lowest signals among gold queries with an ungated top-5 hit: "
        + ", ".join(f"{qid} {s:.3f}" for s, qid in lowest) + ".",
    ]
    return "\n".join(lines) + "\n"


def sweep(run_path: Path, doc_path: Path, thresholds) -> None:
    """Write the abstention threshold sweep for one `measure` record.

    For choosing gate thresholds this supersedes scripts/measure_false_injection.py's
    raw-cosine sweep: that script sweeps the unfiltered top-1 cosine against the configured
    graph with no intervals, while this scores the gate the daemon serves (the filtered
    abstain_signal, program item 1d) on the isolated graph, with bootstrap and Wilson
    intervals. That script remains the score-distribution diagnostic.
    """
    record = json.loads(run_path.read_text())
    doc_path.write_text(render_sweep(record, thresholds))
    print(doc_path.read_text())


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "measure":
        measure(Path(argv[2]))
        return 0
    if len(argv) == 5 and argv[1] == "compare":
        compare(Path(argv[2]), Path(argv[3]), Path(argv[4]))
        return 0
    if len(argv) >= 4 and argv[1] == "sweep":
        thresholds = tuple(float(t) for t in argv[4:]) or DEFAULT_SWEEP_THRESHOLDS
        sweep(Path(argv[2]), Path(argv[3]), thresholds)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
