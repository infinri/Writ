"""Section 10 contractual performance targets -- automated benchmark suite.

Measures all metrics from RAG_arch_handbook.md Section 10. These are
pass/fail gates: if any target is missed, the pipeline must be re-architected
before proceeding to Phases 6-9.

Requires Neo4j running with migrated rules (80-rule corpus).
Does NOT clear the database -- reads from migrated state only.

Run with: pytest benchmarks/bench_targets.py -v -s
"""

from __future__ import annotations

import json
import resource
import time
from pathlib import Path

import pytest
import pytest_asyncio
from sentence_transformers import SentenceTransformer

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import validate_parsed_rule
from writ.graph.integrity import IntegrityChecker
from writ.retrieval.pipeline import build_pipeline
from writ.retrieval.ranking import (
    RankingWeights,
    apply_context_budget,
    compute_score,
    normalize_ranks,
)

# All async tests share the module-scoped event loop so that
# module-scoped async fixtures (db, pipeline) work correctly.
pytestmark = pytest.mark.asyncio(loop_scope="module")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

GROUND_TRUTH_PATH = Path("tests/fixtures/ground_truth_queries.json")

# Per ARCH-CONST-001: benchmark budgets from handbook Section 10.
LATENCY_P95_BUDGET_MS = 10.0
COLD_START_BUDGET_S = 3.0
MEMORY_BUDGET_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
INTEGRITY_BUDGET_MS = 500.0
INGESTION_BUDGET_S = 2.0
MRR5_THRESHOLD = 0.78
HIT_RATE_THRESHOLD = 0.90
BM25_BUDGET_MS = 2.0
VECTOR_BUDGET_MS = 3.0
CACHE_BUDGET_MS = 3.0
RANKING_BUDGET_MS = 1.0

BENCHMARK_ITERATIONS = 100


# ---------------------------------------------------------------------------
# Fixtures -- shared across all benchmarks, module-scoped for efficiency.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    """Shared Neo4j connection. Does NOT clear the database."""
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    count = await conn.count_rules()
    if count == 0:
        pytest.skip("Neo4j has no rules. Run: python scripts/migrate.py")
    yield conn
    await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pipeline(db):
    """Pre-warmed retrieval pipeline."""
    p = await build_pipeline(db)
    yield p


@pytest.fixture(scope="module")
def ground_truth():
    """Load ground-truth queries from fixture file."""
    data = json.loads(GROUND_TRUTH_PATH.read_text())
    return data["queries"]


# ---------------------------------------------------------------------------
# Benchmark 1: Integrity check duration (< 500ms on 80-rule corpus)
# ---------------------------------------------------------------------------

class TestIntegrityBenchmark:

    async def test_integrity_check_duration(self, db) -> None:
        checker = IntegrityChecker(db._driver, db._database)

        latencies: list[float] = []
        for _ in range(10):
            start = time.perf_counter()
            await checker.run_all_checks(skip_redundancy=True)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nIntegrity check: median={median:.1f}ms, p95={p95:.1f}ms (budget: {INTEGRITY_BUDGET_MS}ms)")
        assert p95 < INTEGRITY_BUDGET_MS, (
            f"Integrity check p95 {p95:.1f}ms exceeds {INTEGRITY_BUDGET_MS}ms budget"
        )


# ---------------------------------------------------------------------------
# Benchmark 2: Single rule ingestion (< 2s including embed)
# ---------------------------------------------------------------------------

class TestIngestionBenchmark:

    async def test_single_rule_ingestion(self, db) -> None:
        model = SentenceTransformer("all-MiniLM-L6-v2")

        synthetic_rule = {
            "rule_id": "BENCH-INGEST-001",
            "domain": "Benchmark",
            "severity": "medium",
            "scope": "file",
            "trigger": "Benchmark trigger for ingestion timing",
            "statement": "Benchmark statement for ingestion timing",
            "violation": "Bad.",
            "pass_example": "Good.",
            "enforcement": "Benchmark.",
            "rationale": "Benchmark.",
            "mandatory": False,
            "confidence": "production-validated",
            "evidence": "doc:benchmark",
            "staleness_window": 365,
            "last_validated": "2026-03-15",
        }

        latencies: list[float] = []
        for _ in range(10):
            start = time.perf_counter()
            validate_parsed_rule(synthetic_rule)
            await db.create_rule(synthetic_rule)
            text = f"{synthetic_rule['trigger']} {synthetic_rule['statement']}"
            model.encode(text)
            elapsed_s = time.perf_counter() - start
            latencies.append(elapsed_s)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nSingle rule ingestion: median={median:.3f}s, p95={p95:.3f}s (budget: {INGESTION_BUDGET_S}s)")
        assert p95 < INGESTION_BUDGET_S, (
            f"Ingestion p95 {p95:.3f}s exceeds {INGESTION_BUDGET_S}s budget"
        )


# ---------------------------------------------------------------------------
# Benchmark 3: Cold start / pipeline build (< 3s)
# ---------------------------------------------------------------------------

class TestColdStartBenchmark:

    async def test_cold_start(self, db) -> None:
        # Pre-load model once (matches server.py lifespan pattern).
        model = SentenceTransformer("all-MiniLM-L6-v2")

        latencies: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            await build_pipeline(db, embedding_model=model)
            elapsed_s = time.perf_counter() - start
            latencies.append(elapsed_s)

        latencies.sort()
        best = latencies[0]
        worst = latencies[-1]
        print(f"\nCold start (build_pipeline, model reused): best={best:.2f}s, worst={worst:.2f}s (budget: {COLD_START_BUDGET_S}s)")
        assert worst < COLD_START_BUDGET_S, (
            f"Cold start {worst:.2f}s exceeds {COLD_START_BUDGET_S}s budget"
        )


# ---------------------------------------------------------------------------
# Benchmark 4: Memory footprint (< 2 GB RSS after pipeline build)
# ---------------------------------------------------------------------------

class TestMemoryBenchmark:

    def test_memory_footprint(self, pipeline) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = usage.ru_maxrss * 1024  # ru_maxrss is in KB on Linux
        rss_mb = rss_bytes / (1024 * 1024)
        budget_mb = MEMORY_BUDGET_BYTES / (1024 * 1024)
        print(f"\nMemory footprint: {rss_mb:.0f} MB RSS (budget: {budget_mb:.0f} MB)")
        assert rss_bytes < MEMORY_BUDGET_BYTES, (
            f"Memory {rss_mb:.0f} MB exceeds {budget_mb:.0f} MB budget"
        )


# ---------------------------------------------------------------------------
# Benchmark 5: MRR@5 retrieval precision + hit rate regression
# ---------------------------------------------------------------------------

class TestRetrievalPrecision:

    def test_mrr5_ambiguous_set(self, pipeline, ground_truth) -> None:
        """MRR@5 on ambiguous held-out queries. Canonical gate metric."""
        ambiguous = [q for q in ground_truth if q["set"] == "ambiguous"]
        assert len(ambiguous) >= 15, f"Expected >= 15 ambiguous queries, got {len(ambiguous)}"

        reciprocal_ranks: list[float] = []
        for q in ambiguous:
            result = pipeline.query(q["query"])
            top5_ids = [r["rule_id"] for r in result["rules"][:5]]
            expected = q["expected_rule_id"]
            if expected in top5_ids:
                rank = top5_ids.index(expected) + 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)

        mrr5 = sum(reciprocal_ranks) / len(reciprocal_ranks)
        hits = sum(1 for rr in reciprocal_ranks if rr > 0)
        print(f"\nMRR@5 (ambiguous, n={len(ambiguous)}): {mrr5:.4f} (threshold: {MRR5_THRESHOLD})")
        print(f"  Hits in top 5: {hits}/{len(ambiguous)}")

        misses = [
            q["id"] for q, rr in zip(ambiguous, reciprocal_ranks) if rr == 0.0
        ]
        if misses:
            print(f"  Misses: {', '.join(misses)}")

        assert mrr5 >= MRR5_THRESHOLD, (
            f"MRR@5 {mrr5:.4f} below {MRR5_THRESHOLD} threshold. Misses: {misses}"
        )

    def test_hit_rate_all_queries(self, pipeline, ground_truth) -> None:
        """Regression check: expected rule in top 5 for > 90% of all queries."""
        hits = 0
        misses: list[str] = []
        for q in ground_truth:
            result = pipeline.query(q["query"])
            top5_ids = [r["rule_id"] for r in result["rules"][:5]]
            if q["expected_rule_id"] in top5_ids:
                hits += 1
            else:
                misses.append(q["id"])

        total = len(ground_truth)
        hit_rate = hits / total
        print(f"\nHit rate (all {total} queries): {hits}/{total} = {hit_rate:.2%} (threshold: {HIT_RATE_THRESHOLD:.0%})")
        if misses:
            print(f"  Misses: {', '.join(misses)}")

        assert hit_rate >= HIT_RATE_THRESHOLD, (
            f"Hit rate {hit_rate:.2%} below {HIT_RATE_THRESHOLD:.0%}. Misses: {misses}"
        )


# ---------------------------------------------------------------------------
# Benchmark 6: Context-stuffing vs. retrieval comparison
# ---------------------------------------------------------------------------

class TestContextReduction:

    def test_context_stuffing_ratio(self, pipeline) -> None:
        """Measure token reduction: all rules raw vs. retrieval result."""
        # Use pipeline's pre-loaded metadata (all non-mandatory rules).
        rules = list(pipeline._metadata.values())

        # Approximate token count: chars / 4 (standard GPT-family approximation).
        full_text = ""
        for rule in rules:
            for field in ("rule_id", "domain", "trigger", "statement", "violation",
                          "pass_example", "enforcement", "rationale"):
                full_text += str(rule.get(field, "")) + "\n"
        stuffing_tokens = len(full_text) // 4

        # Run representative queries through the pipeline.
        latencies: list[float] = []
        retrieval_tokens_list: list[int] = []
        test_queries = [
            "dependency injection constructor",
            "SQL string concatenation",
            "async blocking event loop",
            "test isolation shared state",
            "plugin targeting interface",
        ]
        for q in test_queries:
            start = time.perf_counter()
            result = pipeline.query(q)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

            result_text = ""
            for rule in result["rules"][:5]:
                for field in ("rule_id", "statement", "trigger", "violation",
                              "pass_example", "rationale"):
                    result_text += str(rule.get(field, "")) + "\n"
            retrieval_tokens_list.append(len(result_text) // 4)

        avg_retrieval_tokens = sum(retrieval_tokens_list) // len(retrieval_tokens_list)
        avg_latency = sum(latencies) / len(latencies)
        ratio = stuffing_tokens / avg_retrieval_tokens if avg_retrieval_tokens > 0 else float("inf")

        print("\n--- Context Reduction ---")
        print(f"  Context-stuffing: {stuffing_tokens:,} tokens ({len(rules)} rules)")
        print(f"  Writ retrieval:   {avg_retrieval_tokens:,} tokens (5 rules, {avg_latency:.1f}ms)")
        print(f"  Ratio:            {ratio:.0f}x reduction")
        print(f"  Latency:          {avg_latency:.1f}ms vs. ~0ms (but context window cost is invisible)")

        assert ratio > 1.0, "Retrieval should return fewer tokens than full context stuffing"


# ---------------------------------------------------------------------------
# Benchmark 7: Per-stage latency isolation
# ---------------------------------------------------------------------------

class TestPerStageBenchmarks:
    """Isolate each pipeline stage to identify degradation sources."""

    def test_stage2_bm25_latency(self, pipeline) -> None:
        """Stage 2: BM25 keyword search via Tantivy. Budget < 2ms."""
        queries = [
            "dependency injection", "SQL query", "async event loop",
            "test isolation", "plugin observer", "error handling",
            "magic number", "performance optimization", "security auth",
            "message queue",
        ]
        latencies: list[float] = []
        for _ in range(BENCHMARK_ITERATIONS // len(queries)):
            for q in queries:
                start = time.perf_counter()
                pipeline._keyword.search(q, limit=50)
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nStage 2 (BM25): median={median:.3f}ms, p95={p95:.3f}ms (budget: {BM25_BUDGET_MS}ms)")
        assert p95 < BM25_BUDGET_MS, (
            f"BM25 p95 {p95:.3f}ms exceeds {BM25_BUDGET_MS}ms budget"
        )

    def test_stage3_vector_latency(self, pipeline) -> None:
        """Stage 3: ANN vector search via hnswlib. Budget < 3ms."""
        queries = [
            "dependency injection", "SQL query", "async event loop",
            "test isolation", "plugin observer", "error handling",
            "magic number", "performance optimization", "security auth",
            "message queue",
        ]
        latencies: list[float] = []
        for _ in range(BENCHMARK_ITERATIONS // len(queries)):
            for q in queries:
                vec = pipeline._model.encode(q).tolist()
                start = time.perf_counter()
                pipeline._vector.search(vec, k=10)
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nStage 3 (vector): median={median:.3f}ms, p95={p95:.3f}ms (budget: {VECTOR_BUDGET_MS}ms)")
        assert p95 < VECTOR_BUDGET_MS, (
            f"Vector p95 {p95:.3f}ms exceeds {VECTOR_BUDGET_MS}ms budget"
        )

    def test_stage4_cache_latency(self, pipeline) -> None:
        """Stage 4: Adjacency cache lookup. Budget < 3ms."""
        sample_ids = list(pipeline._metadata.keys())[:20]
        latencies: list[float] = []
        for _ in range(BENCHMARK_ITERATIONS):
            start = time.perf_counter()
            pipeline._cache.get_enrichment(sample_ids)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nStage 4 (cache): median={median:.3f}ms, p95={p95:.3f}ms (budget: {CACHE_BUDGET_MS}ms)")
        assert p95 < CACHE_BUDGET_MS, (
            f"Cache p95 {p95:.3f}ms exceeds {CACHE_BUDGET_MS}ms budget"
        )

    def test_stage5_ranking_latency(self, pipeline) -> None:
        """Stage 5: Ranking computation. Budget < 1ms."""
        candidates = []
        sample_ids = list(pipeline._metadata.keys())[:50]
        for rid in sample_ids:
            meta = pipeline._metadata.get(rid, {})
            candidates.append({
                "rule_id": rid,
                "bm25_score": 5.0,
                "vector_score": 0.85,
                "severity": meta.get("severity", "medium"),
                "confidence": meta.get("confidence", "production-validated"),
            })

        latencies: list[float] = []
        weights = RankingWeights()
        for _ in range(BENCHMARK_ITERATIONS):
            start = time.perf_counter()
            bm25_raw = [c["bm25_score"] for c in candidates]
            vector_raw = [c["vector_score"] for c in candidates]
            bm25_norm = normalize_ranks(bm25_raw)
            vector_norm = normalize_ranks(vector_raw)
            scored = []
            for i, c in enumerate(candidates):
                score = compute_score(
                    bm25_norm=bm25_norm[i],
                    vector_norm=vector_norm[i],
                    severity=c["severity"],
                    confidence=c["confidence"],
                    weights=weights,
                )
                scored.append((c["rule_id"], score))
            scored.sort(key=lambda x: x[1], reverse=True)
            apply_context_budget(
                [{"rule_id": rid, "score": s, "statement": "x", "trigger": "y",
                  "violation": "z", "pass_example": "w", "rationale": "r",
                  "relationships": []} for rid, s in scored],
                budget_tokens=5000,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nStage 5 (ranking): median={median:.3f}ms, p95={p95:.3f}ms (budget: {RANKING_BUDGET_MS}ms)")
        assert p95 < RANKING_BUDGET_MS, (
            f"Ranking p95 {p95:.3f}ms exceeds {RANKING_BUDGET_MS}ms budget"
        )

    def test_end_to_end_p95(self, pipeline) -> None:
        """Full pipeline p95. Budget < 10ms."""
        queries = [
            "controller SQL query", "dependency injection", "plugin observer",
            "error handling try catch", "unit test isolation", "named bind parameters",
            "async event loop blocking", "security authorization", "performance optimization",
            "magic number constant",
        ]
        latencies: list[float] = []
        for _ in range(BENCHMARK_ITERATIONS // len(queries)):
            for q in queries:
                start = time.perf_counter()
                pipeline.query(q)
                elapsed_ms = (time.perf_counter() - start) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[p95_idx]
        median = latencies[len(latencies) // 2]
        print(f"\nEnd-to-end: median={median:.1f}ms, p95={p95:.1f}ms (budget: {LATENCY_P95_BUDGET_MS}ms)")
        assert p95 < LATENCY_P95_BUDGET_MS, (
            f"E2E p95 {p95:.1f}ms exceeds {LATENCY_P95_BUDGET_MS}ms budget"
        )
