"""Comprehensive scale benchmark: 80, 500, 1K, 10K rules.

Measures per-stage latency, end-to-end latency, memory, cold start,
retrieval quality, compression, context reduction, and session behavior
at each corpus size. Results logged to SCALE_BENCHMARK_RESULTS.md.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCALE_LEVELS = [80, 500, 1_000, 10_000]
BENCHMARK_QUERIES = [
    ("architecture layer separation", "Architecture"),
    ("async blocking event loop", "Python / Async"),
    ("controller contains SQL query", "Architecture"),
    ("dependency injection constructor", "Architecture"),
    ("performance optimization caching", "Performance"),
    ("unit test isolation mocks", "Testing"),
    ("error handling propagation context", "Architecture"),
    ("magic numbers named constants", "Architecture"),
    ("function size limit decompose", "Architecture"),
    ("pydantic validation external data", "Python / Data Validation"),
]
LATENCY_ITERATIONS = 50
OUTPUT_FILE = Path("SCALE_BENCHMARK_RESULTS.md")

# Domains for synthetic rule generation.
SYNTHETIC_DOMAINS = [
    "Architecture", "Performance", "Testing", "Security",
    "Database", "Python / Async", "Python / Data Validation",
    "PHP / Error Handling", "PHP / Type Safety",
    "Frameworks / Magento", "Frameworks / NestJS",
    "DevOps / CI", "DevOps / Monitoring", "API Design",
    "Documentation", "Accessibility", "Logging",
]

SYNTHETIC_TRIGGERS = [
    "When writing a function that {action}.",
    "When a class {action}.",
    "When implementing {action}.",
    "When modifying code that {action}.",
    "When reviewing code that {action}.",
]

SYNTHETIC_ACTIONS = [
    "handles user input", "processes external data", "manages state transitions",
    "performs I/O operations", "catches exceptions", "creates database queries",
    "validates configuration", "initializes services", "transforms data types",
    "implements caching", "manages connections", "handles authentication",
    "processes webhooks", "generates reports", "manages file uploads",
    "implements pagination", "handles concurrency", "manages transactions",
    "implements retry logic", "handles timeouts", "validates schemas",
    "processes batch jobs", "manages queue consumers", "implements rate limiting",
    "handles graceful shutdown", "manages feature flags", "implements health checks",
    "processes notifications", "manages user sessions", "implements search",
]


# ---------------------------------------------------------------------------
# Synthetic rule generation
# ---------------------------------------------------------------------------

def generate_synthetic_rules(count: int, existing_rules: list[dict]) -> list[dict]:
    """Generate synthetic rules to reach target count."""
    needed = count - len(existing_rules)
    if needed <= 0:
        return existing_rules[:count]

    rng = np.random.default_rng(42)
    synthetic: list[dict] = []

    for i in range(needed):
        domain = SYNTHETIC_DOMAINS[i % len(SYNTHETIC_DOMAINS)]
        trigger_template = SYNTHETIC_TRIGGERS[i % len(SYNTHETIC_TRIGGERS)]
        action = SYNTHETIC_ACTIONS[i % len(SYNTHETIC_ACTIONS)]
        prefix = domain.split("/")[0].strip().upper()[:4]
        rule_id = f"{prefix}-SYN-{i + 1:04d}"

        severity = rng.choice(["critical", "high", "medium", "low"])
        scope = rng.choice(["file", "module", "slice"])

        synthetic.append({
            "rule_id": rule_id,
            "domain": domain,
            "severity": str(severity),
            "scope": str(scope),
            "trigger": trigger_template.format(action=action),
            "statement": f"Code must properly handle {action} in the {domain.lower()} domain. "
                         f"Failure to do so leads to maintainability and correctness issues.",
            "violation": f"Directly {action} without following the established pattern.",
            "pass_example": f"Using the approved pattern for {action} with proper error handling.",
            "enforcement": "Code review. Static analysis.",
            "rationale": f"Improper handling of {action} has caused production incidents. "
                         f"The approved pattern prevents common failure modes.",
            "mandatory": False,
            "confidence": "production-validated",
            "evidence": "doc:synthetic-benchmark",
            "staleness_window": 365,
            "last_validated": "2026-03-20",
        })

    return existing_rules + synthetic


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

async def benchmark_at_scale(
    scale: int,
    db,
    model: object,
    real_rules: list[dict],
    results: dict,
) -> None:
    """Run full benchmark suite at a given corpus size."""
    from writ.compression.abstractions import generate_abstractions, write_abstractions_to_graph
    from writ.compression.clusters import cluster_hdbscan
    from writ.retrieval.pipeline import build_pipeline
    from writ.retrieval.session import SessionTracker

    print(f"\n{'='*60}")
    print(f"  SCALE: {scale:,} rules")
    print(f"{'='*60}")

    # --- Setup: populate graph ---
    print(f"  Populating graph with {scale:,} rules...")
    await db.clear_all()

    all_rules = generate_synthetic_rules(scale, real_rules)
    domain_rules = [r for r in all_rules if not r.get("mandatory", False)]

    t0 = time.perf_counter()
    for rule in all_rules:
        await db.create_rule(rule)
    ingest_time = time.perf_counter() - t0
    print(f"  Ingested in {ingest_time:.2f}s ({scale / ingest_time:.0f} rules/sec)")

    rule_count = await db.count_rules()
    mandatory_count = sum(1 for r in all_rules if r.get("mandatory", False))
    domain_count = rule_count - mandatory_count

    # --- Cold start ---
    print("  Measuring cold start...")
    cold_starts = []
    for _ in range(3):
        gc.collect()
        t0 = time.perf_counter()
        pipeline = await build_pipeline(db)
        cold_starts.append(time.perf_counter() - t0)
    cold_start_median = statistics.median(cold_starts)
    cold_start_worst = max(cold_starts)

    # --- Memory ---
    gc.collect()
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    rss_mb = rss_bytes / (1024 * 1024)

    # --- Per-stage latency ---
    print("  Measuring per-stage latency...")
    bm25_times, vector_times, cache_times, ranking_times, e2e_times = [], [], [], [], []

    for _ in range(LATENCY_ITERATIONS):
        for query_text, _ in BENCHMARK_QUERIES[:5]:
            # BM25
            t0 = time.perf_counter()
            pipeline._keyword.search(query_text, limit=50)
            bm25_times.append((time.perf_counter() - t0) * 1000)

            # Vector
            qv = model.encode(query_text).tolist()
            t0 = time.perf_counter()
            pipeline._vector.search(qv, k=10)
            vector_times.append((time.perf_counter() - t0) * 1000)

            # Cache
            t0 = time.perf_counter()
            pipeline._cache.get_enrichment([all_rules[0]["rule_id"]])
            cache_times.append((time.perf_counter() - t0) * 1000)

            # Full e2e
            t0 = time.perf_counter()
            pipeline.query(query_text=query_text)
            e2e_times.append((time.perf_counter() - t0) * 1000)

    # Ranking = e2e minus other stages (approximation)
    ranking_times = [max(0.001, e - b - v - c)
                     for e, b, v, c in zip(e2e_times, bm25_times, vector_times, cache_times)]

    def pstats(times):
        s = sorted(times)
        return {
            "median": round(statistics.median(s), 3),
            "p95": round(s[int(len(s) * 0.95)], 3),
            "p99": round(s[int(len(s) * 0.99)], 3),
            "min": round(min(s), 3),
            "max": round(max(s), 3),
        }

    latency = {
        "bm25": pstats(bm25_times),
        "vector": pstats(vector_times),
        "cache": pstats(cache_times),
        "ranking": pstats(ranking_times),
        "e2e": pstats(e2e_times),
    }

    # --- Retrieval quality (only meaningful with real ground-truth at 80) ---
    print("  Measuring retrieval quality...")
    hit_count = 0
    total_queries = len(BENCHMARK_QUERIES)
    for query_text, expected_domain in BENCHMARK_QUERIES:
        result = pipeline.query(query_text=query_text)
        returned_domains = {
            pipeline._metadata.get(r["rule_id"], {}).get("domain", "")
            for r in result["rules"][:5]
        }
        if expected_domain in returned_domains or any(expected_domain.lower() in d.lower() for d in returned_domains):
            hit_count += 1
    domain_hit_rate = hit_count / total_queries

    # --- Context reduction ---
    print("  Measuring context reduction...")
    total_domain_tokens = sum(
        len(f"{r.get('statement', '')} {r.get('trigger', '')} {r.get('violation', '')} "
            f"{r.get('pass_example', '')} {r.get('rationale', '')}") // 4
        for r in domain_rules
    )
    sample_result = pipeline.query(query_text="architecture layer separation", budget_tokens=8001)
    retrieved_tokens = sum(
        len(f"{r.get('statement', '')} {r.get('trigger', '')} {r.get('violation', '')} "
            f"{r.get('pass_example', '')} {r.get('rationale', '')}") // 4
        for r in sample_result["rules"]
    )
    context_ratio = total_domain_tokens / max(retrieved_tokens, 1)

    # --- Compression ---
    print("  Measuring compression...")
    compression_data = {"clusters": 0, "ungrouped": 0, "silhouette": -1.0, "avg_ratio": 0.0}
    if domain_count >= 10:
        texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in domain_rules[:scale]]
        if hasattr(model, "encode_batch"):
            embeddings = np.array(model.encode_batch(texts[:len(domain_rules)]), dtype=np.float32)
        else:
            embeddings = np.array(model.encode(texts[:len(domain_rules)]), dtype=np.float32)
        rule_ids = [r["rule_id"] for r in domain_rules[:len(embeddings)]]

        t0 = time.perf_counter()
        cluster_result = cluster_hdbscan(rule_ids, embeddings)
        cluster_time = time.perf_counter() - t0

        abstractions = generate_abstractions(cluster_result, domain_rules[:len(embeddings)])
        avg_ratio = 0.0
        if abstractions:
            avg_ratio = sum(a["compression_ratio"] for a in abstractions) / len(abstractions)

        compression_data = {
            "clusters": len(cluster_result.clusters),
            "ungrouped": len(cluster_result.ungrouped),
            "silhouette": round(cluster_result.silhouette, 4),
            "avg_ratio": round(avg_ratio, 1),
            "cluster_time_s": round(cluster_time, 3),
        }

    # --- Session simulation (3-query) ---
    print("  Running 3-query session simulation...")
    tracker = SessionTracker(initial_budget=10000)
    session_rule_ids: list[str] = []
    session_queries = [
        "architecture layer separation",
        "performance optimization async",
        "testing isolation mocks",
    ]
    for sq in session_queries:
        payload = tracker.next_query(sq)
        sr = pipeline.query(
            query_text=payload["query"],
            budget_tokens=payload["budget_tokens"],
            loaded_rule_ids=payload["loaded_rule_ids"],
        )
        tracker.load_results(sr)
        for rule in sr["rules"]:
            if "rule_id" in rule:
                session_rule_ids.append(rule["rule_id"])

    session_data = {
        "total_rules_loaded": len(session_rule_ids),
        "unique_rules": len(set(session_rule_ids)),
        "duplicates": len(session_rule_ids) - len(set(session_rule_ids)),
        "budget_remaining": tracker.remaining_budget,
    }

    # --- Collect results ---
    results[scale] = {
        "corpus": {
            "total_rules": rule_count,
            "mandatory": mandatory_count,
            "domain": domain_count,
        },
        "ingest": {
            "time_s": round(ingest_time, 2),
            "rules_per_sec": round(scale / ingest_time, 0),
        },
        "cold_start": {
            "median_s": round(cold_start_median, 3),
            "worst_s": round(cold_start_worst, 3),
        },
        "memory_mb": round(rss_mb, 0),
        "latency": latency,
        "retrieval": {
            "domain_hit_rate": round(domain_hit_rate, 4),
        },
        "context": {
            "total_domain_tokens": total_domain_tokens,
            "retrieved_tokens": retrieved_tokens,
            "reduction_ratio": round(context_ratio, 1),
        },
        "compression": compression_data,
        "session": session_data,
    }

    print(f"  Done. e2e p95={latency['e2e']['p95']}ms, "
          f"context={context_ratio:.0f}x reduction, "
          f"memory={rss_mb:.0f}MB")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def write_report(results: dict) -> None:
    """Write results to Markdown file."""
    lines = [
        "# Writ Scale Benchmark Results",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Scales tested:** {', '.join(f'{s:,}' for s in sorted(results.keys()))}",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | " + " | ".join(f"{s:,}" for s in sorted(results.keys())) + " |",
        "|---|" + "|".join("---" for _ in results) + "|",
    ]

    scales = sorted(results.keys())
    def row(label, fn):
        vals = " | ".join(str(fn(results[s])) for s in scales)
        lines.append(f"| {label} | {vals} |")

    row("Domain rules", lambda r: r["corpus"]["domain"])
    row("Mandatory rules", lambda r: r["corpus"]["mandatory"])
    row("Ingest time", lambda r: f"{r['ingest']['time_s']}s")
    row("Ingest rate", lambda r: f"{r['ingest']['rules_per_sec']:.0f}/s")
    row("Cold start (median)", lambda r: f"{r['cold_start']['median_s']}s")
    row("Memory (RSS)", lambda r: f"{r['memory_mb']:.0f} MB")
    row("BM25 p95", lambda r: f"{r['latency']['bm25']['p95']}ms")
    row("Vector p95", lambda r: f"{r['latency']['vector']['p95']}ms")
    row("Cache p95", lambda r: f"{r['latency']['cache']['p95']}ms")
    row("Ranking p95", lambda r: f"{r['latency']['ranking']['p95']}ms")
    row("**E2E p95**", lambda r: f"**{r['latency']['e2e']['p95']}ms**")
    row("E2E median", lambda r: f"{r['latency']['e2e']['median']}ms")
    row("Domain hit rate", lambda r: f"{r['retrieval']['domain_hit_rate'] * 100:.1f}%")
    row("Context tokens (all)", lambda r: f"{r['context']['total_domain_tokens']:,}")
    row("Context tokens (retrieved)", lambda r: f"{r['context']['retrieved_tokens']:,}")
    row("**Context reduction**", lambda r: f"**{r['context']['reduction_ratio']}x**")
    row("Clusters", lambda r: r["compression"]["clusters"])
    row("Ungrouped", lambda r: r["compression"]["ungrouped"])
    row("Silhouette", lambda r: r["compression"]["silhouette"])
    row("Compression ratio", lambda r: f"{r['compression']['avg_ratio']}x")
    row("Session rules loaded", lambda r: r["session"]["total_rules_loaded"])
    row("Session duplicates", lambda r: r["session"]["duplicates"])
    row("Session budget remaining", lambda r: f"{r['session']['budget_remaining']:,}")

    lines.extend([
        "",
        "---",
        "",
        "## Per-Stage Latency Detail",
        "",
    ])

    for s in scales:
        r = results[s]
        lines.extend([
            f"### {s:,} rules",
            "",
            "| Stage | Median | p95 | p99 | Min | Max |",
            "|---|---|---|---|---|---|",
        ])
        for stage in ["bm25", "vector", "cache", "ranking", "e2e"]:
            d = r["latency"][stage]
            name = {"bm25": "BM25", "vector": "Vector", "cache": "Cache",
                    "ranking": "Ranking", "e2e": "**End-to-end**"}[stage]
            lines.append(
                f"| {name} | {d['median']}ms | {d['p95']}ms | {d['p99']}ms | {d['min']}ms | {d['max']}ms |"
            )
        lines.append("")

    lines.extend([
        "---",
        "",
        "## Scaling Analysis",
        "",
        "Key questions answered by this benchmark:",
        "",
        "1. **Does latency stay under 10ms at scale?** Check E2E p95 column.",
        "2. **Does context reduction improve at scale?** At 80 rules ~4x; at 10K ~700x.",
        "3. **Does memory stay under 2GB?** Check RSS column.",
        "4. **Does cold start stay under 3s?** Check cold start column.",
        "5. **Does compression improve at scale?** More rules = more clusters = higher compression.",
        "6. **Does session tracking prevent duplicates?** Check session duplicates = 0.",
        "",
        "---",
        "",
        f"Generated by `benchmarks/scale_benchmark.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ])

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nResults written to {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    from writ.graph.db import Neo4jConnection
    from writ.graph.ingest import discover_rule_files, parse_rules_from_file, validate_parsed_rule

    print("Loading embedding model (one-time)...")
    try:
        from writ.retrieval.embeddings import CachedEncoder, DEFAULT_ONNX_DIR, OnnxEmbeddingModel

        model = CachedEncoder(OnnxEmbeddingModel(DEFAULT_ONNX_DIR))
        print("  Using ONNX Runtime backend")
    except (FileNotFoundError, ImportError):
        model = SentenceTransformer("all-MiniLM-L6-v2")
        print("  Using PyTorch backend")

    print("Loading real rules from bible/...")
    real_rules: list[dict] = []
    bible = Path("bible/")
    for f in discover_rule_files(bible):
        for rd in parse_rules_from_file(f):
            validate_parsed_rule(rd)
            clean = {k: v for k, v in rd.items() if not k.startswith("_")}
            real_rules.append(clean)
    print(f"  Loaded {len(real_rules)} real rules")

    db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
    results: dict = {}

    try:
        for scale in SCALE_LEVELS:
            await benchmark_at_scale(scale, db, model, real_rules, results)
    finally:
        # Restore original 80 rules.
        print("\nRestoring original 80-rule corpus...")
        await db.clear_all()
        for rule in real_rules:
            await db.create_rule(rule)
        print(f"  Restored {await db.count_rules()} rules")
        await db.close()

    write_report(results)

    # Print summary to stdout.
    print("\n" + "=" * 60)
    print("  SCALE BENCHMARK COMPLETE")
    print("=" * 60)
    for s in sorted(results.keys()):
        r = results[s]
        print(f"\n  {s:>6,} rules: e2e p95={r['latency']['e2e']['p95']}ms, "
              f"context={r['context']['reduction_ratio']}x, "
              f"mem={r['memory_mb']:.0f}MB, "
              f"cold={r['cold_start']['median_s']}s")


if __name__ == "__main__":
    asyncio.run(main())
