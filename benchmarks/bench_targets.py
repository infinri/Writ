"""Section 10 contractual performance targets -- automated benchmark suite.

Measures all metrics from HANDBOOK.md Section "By the numbers". These are
pass/fail gates: if any target is missed, the pipeline must be re-architected
before proceeding to Phases 6-9.

Requires Neo4j running with migrated rules (80-rule corpus).
Does NOT clear the database -- reads from migrated state only.

Run with: pytest benchmarks/bench_targets.py -v -s
"""

from __future__ import annotations

import json
import os
import resource
import time
from pathlib import Path

import pytest
import pytest_asyncio

from tests.fixtures.regression_floors import (
    DOMAIN_HIT_RATE_TOP5_FLOOR,
    ELIGIBLE_HIT_RATE_FLOOR,
    HIT_RATE_FLOOR,
    MRR5_FLOOR,
)
from tests.fixtures.retrieval_scoring import hit_rate_at_5, mrr_at_5
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
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

# build_pipeline() needs the ONNX embedding model; skip this whole module
# (rather than erroring) when onnxruntime is not installed in the active
# interpreter (KG step 0b).
pytest.importorskip("onnxruntime")

# All async tests share the module-scoped event loop so that
# module-scoped async fixtures (db, pipeline) work correctly.
pytestmark = pytest.mark.asyncio(loop_scope="module")

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()

GROUND_TRUTH_PATH = Path("tests/fixtures/ground_truth_queries.json")

# Per ARCH-CONST-001: benchmark budgets from handbook Section 10.
LATENCY_P95_BUDGET_MS = 10.0
# Cold-start budget for build_pipeline() on the ONNX path.
#
# Measurement (2026-05-13, 276-rule corpus, ONNX path, dev machine):
#   10-run distribution: min 1.84s, median 2.20s, max 2.64s, p95 ~2.60s
#   Cold-cold (no HNSW cache): 1.86s
#
# Budget is 3.5s, not 2.7-3.0s, deliberately. CI runners are typically
# slower and noisier than the measurement environment; a budget equal
# to local p95 will flake, and a flaky hard-blocking gate erodes its
# own authority within weeks (people start adding "rerun CI" as a
# reflex, then ignoring the failure, and the gate becomes documentation
# again). 0.5s of slack on an operation that runs once per machine
# reboot costs nothing operationally and preserves the gate.
#
# Re-measure and reconsider if the corpus grows substantially or the
# embedding model changes. Do not tighten without a corresponding
# re-measurement; "feels generous" is not a reason.
COLD_START_BUDGET_S = 3.5
# Cold start is the one target where raw hardware dominates (first ONNX model
# load + index build from a cold FS cache). Shared CI runners measured 5.5s
# where the reference machine measures under 1s, with identical warm behavior
# (best iteration ~0.2s both places). WRIT_BENCH_BUDGET_SCALE stretches ONLY
# this budget on known-slow hardware (pr.yml sets 3); it defaults to 1 so the
# local contract is unchanged.
_BUDGET_SCALE = float(os.environ.get("WRIT_BENCH_BUDGET_SCALE", "1"))
COLD_START_BUDGET_SCALED_S = COLD_START_BUDGET_S * _BUDGET_SCALE
MEMORY_BUDGET_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
INTEGRITY_BUDGET_MS = 500.0
INGESTION_BUDGET_S = 2.0
# MRR@5 ambiguous-set floor and hit-rate floor live in
# tests/fixtures/regression_floors.py (single source of truth shared
# with tests/test_graph_proximity.py).
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
        pytest.skip("Neo4j has no rules. Run: `writ import-markdown`")
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

        # One untimed warmup: the target is steady-state duration, and the first
        # pass against a cold Neo4j (fresh CI service container, or a just-restarted
        # daemon's first touch) measures cache population instead. Same rationale as
        # test_end_to_end_p95's warmup. Observed cold outliers: 1.9s local, 3.2s CI,
        # vs ~0.2s p95 warm.
        await checker.run_all_checks(skip_redundancy=True)

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
        from writ.retrieval.embeddings import CachedEncoder, DEFAULT_ONNX_DIR, OnnxEmbeddingModel

        try:
            model = CachedEncoder(OnnxEmbeddingModel(DEFAULT_ONNX_DIR))
        except (FileNotFoundError, ImportError):
            from sentence_transformers import SentenceTransformer
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
        try:
            for _ in range(10):
                start = time.perf_counter()
                validate_parsed_rule(synthetic_rule)
                await db.create_rule(synthetic_rule)
                text = f"{synthetic_rule['trigger']} {synthetic_rule['statement']}"
                model.encode(text)
                elapsed_s = time.perf_counter() - start
                latencies.append(elapsed_s)
        finally:
            # Clean up synthetic rule so it doesn't leak into the production
            # graph. Runs even on a mid-loop failure (TEST-ISOLATE-001); the
            # cleanup is a single bounded delete, so it never masks the
            # original exception, which still propagates after this block.
            query = "MATCH (r:Rule {rule_id: $rule_id}) DETACH DELETE r"
            async with db._driver.session(database=db._database) as session:
                await session.run(query, rule_id="BENCH-INGEST-001")

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
        # Measures production cold start path (auto-detects ONNX or SentenceTransformer).
        latencies: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            await build_pipeline(db)
            elapsed_s = time.perf_counter() - start
            latencies.append(elapsed_s)

        latencies.sort()
        best = latencies[0]
        worst = latencies[-1]
        print(f"\nCold start (build_pipeline): best={best:.2f}s, worst={worst:.2f}s (budget: {COLD_START_BUDGET_SCALED_S}s)")
        assert worst < COLD_START_BUDGET_SCALED_S, (
            f"Cold start {worst:.2f}s exceeds {COLD_START_BUDGET_SCALED_S}s budget"
            + (f" (scale {_BUDGET_SCALE}x via WRIT_BENCH_BUDGET_SCALE)" if _BUDGET_SCALE != 1 else "")
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

        mrr5, misses = mrr_at_5(pipeline, ambiguous)
        hits = len(ambiguous) - len(misses)
        print(f"\nMRR@5 (ambiguous, n={len(ambiguous)}): {mrr5:.4f} (floor: {MRR5_FLOOR})")
        print(f"  Hits in top 5: {hits}/{len(ambiguous)}")
        if misses:
            print(f"  Misses: {', '.join(misses)}")

        assert mrr5 >= MRR5_FLOOR, (
            f"MRR@5 {mrr5:.4f} below {MRR5_FLOOR} floor. Misses: {misses}"
        )

    def test_hit_rate_all_queries(self, pipeline, ground_truth) -> None:
        """Regression check: expected rule in top 5 for at least HIT_RATE_FLOOR (0.75) of all queries."""
        hit_rate, misses = hit_rate_at_5(pipeline, ground_truth)
        total = len(ground_truth)
        hits = total - len(misses)
        print(f"\nHit rate (all {total} queries): {hits}/{total} = {hit_rate:.2%} (floor: {HIT_RATE_FLOOR:.0%})")
        if misses:
            print(f"  Misses: {', '.join(misses)}")

        assert hit_rate >= HIT_RATE_FLOOR, (
            f"Hit rate {hit_rate:.2%} below floor {HIT_RATE_FLOOR:.0%}. Misses: {misses}"
        )

    async def _mandatory_ids(self, db) -> set[str]:
        async with db._driver.session(database=db._database) as s:
            r = await s.run("MATCH (r:Rule {mandatory: true}) RETURN r.rule_id AS id")
            return {rec["id"] async for rec in r}

    def _split_by_channel(self, pipeline, ground_truth, mandatory_ids):
        """Partition targets by delivery channel (HANDBOOK section 10).

        eligible: semantic-mode /query can return the expected rule -- rank
        metrics apply. Eligibility is the ROUTE data (Stage 1 admits a candidate
        only when its category routes include 'semantic'), not candidate-set
        membership: _metadata holds every candidate, but the route filter runs
        at query time. A rule with no route entry is universal (eligible).
        always_on: expected rule is mandatory -- delivered via /always-on, never ranked.
        routed: category routes exclude 'semantic' -- delivered via the state/
        action/pull channels (methodology companion), unreachable by /query BY DESIGN.
        """
        routes_map = getattr(pipeline, "_node_routes", None) or {}
        eligible, always_on, routed = [], [], []
        for q in ground_truth:
            rid = q["expected_rule_id"]
            routes = routes_map.get(rid)
            if rid in mandatory_ids:
                always_on.append(q)
            elif routes is not None and "semantic" not in routes:
                routed.append(q)
            else:
                eligible.append(q)
        return eligible, always_on, routed

    async def test_hit_rate_index_eligible_targets(self, db, pipeline, ground_truth) -> None:
        """Rank metric scored ONLY over targets the index can contain.

        Scoring /query against always-on or routed targets measures the wrong
        delivery channel; those pools get delivery checks below instead.
        """
        mandatory_ids = await self._mandatory_ids(db)
        eligible, always_on, routed = self._split_by_channel(pipeline, ground_truth, mandatory_ids)
        assert len(eligible) >= 100, f"eligible pool suspiciously small: {len(eligible)}"

        hit_rate, misses = hit_rate_at_5(pipeline, eligible)
        hits = len(eligible) - len(misses)
        print(f"\nHit rate (index-eligible, n={len(eligible)}): {hits}/{len(eligible)} = "
              f"{hit_rate:.2%} (floor: {ELIGIBLE_HIT_RATE_FLOOR:.0%}) "
              f"[always-on targets: {len(always_on)}, routed targets: {len(routed)}]")
        if misses:
            print(f"  Misses: {', '.join(misses)}")
        assert hit_rate >= ELIGIBLE_HIT_RATE_FLOOR, (
            f"Eligible hit rate {hit_rate:.2%} below floor {ELIGIBLE_HIT_RATE_FLOOR:.0%}. "
            f"Misses: {misses}"
        )

    async def test_always_on_targets_are_in_the_injection_floor(self, db, pipeline, ground_truth) -> None:
        """Delivery check for mandatory targets: the guarantee is bundle membership,
        not rank. INJECTION_RULE_WHERE is the single source for floor membership."""
        from writ.graph.predicates import INJECTION_RULE_WHERE

        mandatory_ids = await self._mandatory_ids(db)
        _, always_on, _ = self._split_by_channel(pipeline, ground_truth, mandatory_ids)
        assert always_on, "expected at least one always-on target in the ground truth"

        async with db._driver.session(database=db._database) as s:
            r = await s.run(f"MATCH (r:Rule) WHERE {INJECTION_RULE_WHERE} RETURN r.rule_id AS id")
            floor_ids = {rec["id"] async for rec in r}
        undelivered = sorted({q["expected_rule_id"] for q in always_on} - floor_ids)
        print(f"\nAlways-on targets delivered via injection floor: "
              f"{len(always_on) - len(undelivered)}/{len(always_on)} queries")
        assert not undelivered, (
            f"mandatory targets absent from the injection floor (stranded): {undelivered}"
        )

    async def test_routed_targets_are_deliberate_channel_members(self, db, pipeline, ground_truth) -> None:
        """A routed target must carry a real category edge with non-semantic
        routes (deliberate channel membership), never be an accidental orphan
        (the subset-import edge bug class), and must be reachable through at
        least one declared channel."""
        mandatory_ids = await self._mandatory_ids(db)
        _, _, routed = self._split_by_channel(pipeline, ground_truth, mandatory_ids)
        assert routed, (
            "expected the process-domain targets in the routed pool; an empty "
            "pool here means the classifier or the routing data regressed"
        )
        ids = sorted({q["expected_rule_id"] for q in routed})
        routes_map = getattr(pipeline, "_node_routes", None) or {}
        async with db._driver.session(database=db._database) as s:
            r = await s.run(
                "UNWIND $ids AS rid MATCH (n:Rule {rule_id: rid}) "
                "OPTIONAL MATCH (n)-[:BELONGS_TO]->(c:Category) "
                "RETURN rid AS rid, count(c) AS edges", ids=ids)
            orphans = [rec["rid"] async for rec in r if rec["edges"] == 0]
        channelless = [rid for rid in ids if not routes_map.get(rid)]
        print(f"\nRouted targets (non-semantic channels): "
              f"{ {rid: routes_map.get(rid) for rid in ids} }")
        assert not orphans, (
            f"routed targets with NO category edge -- accidental orphans: {orphans}"
        )
        assert not channelless, (
            f"routed targets with an empty route list -- unreachable by ANY "
            f"channel: {channelless}"
        )

    def test_domain_hit_rate_top5(self, db, pipeline, ground_truth) -> None:
        """Domain hit rate at top-5. Floor 90% (Item 3, 2026-05-15).

        Domain hit rate = % of queries where at least one rule from the
        expected rule's domain appears in top-5 of pipeline.query results.

        Catches the synthetic-scale-curve concern (100% at N=80 dropped to
        90% at N>=500 in SCALE_BENCHMARK_RESULTS.md) as a real-corpus gate.
        Measured 94.5% on the 164-query ground truth at the production
        276-rule corpus; floor at 90% leaves ~4.5pp headroom and surfaces
        a regression of ~7 queries before failing.
        """
        import asyncio

        async def _domains() -> dict[str, str]:
            d: dict[str, str] = {}
            async with db._driver.session(database=db._database) as s:
                r = await s.run(
                    "MATCH (r:Rule) RETURN r.rule_id AS id, r.domain AS domain"
                )
                async for rec in r:
                    d[rec["id"]] = rec["domain"] or ""
            return d

        rule_domains = asyncio.get_event_loop().run_until_complete(_domains())

        hits = 0
        total = 0
        misses: list[str] = []
        for q in ground_truth:
            expected_domain = rule_domains.get(q["expected_rule_id"])
            if not expected_domain:
                # Skip queries whose expected_rule_id is not in the corpus.
                # This is a data hygiene issue worth its own surfacing but
                # not a domain-hit-rate failure.
                continue
            total += 1
            result = pipeline.query(q["query"])
            top5_domains = [
                rule_domains.get(r["rule_id"], "") for r in result["rules"][:5]
            ]
            if expected_domain in top5_domains:
                hits += 1
            else:
                misses.append(q["id"])

        rate = hits / total if total else 0.0
        print(
            f"\nDomain hit rate top-5 (all {total} queries): "
            f"{hits}/{total} = {rate:.2%} (floor: {DOMAIN_HIT_RATE_TOP5_FLOOR:.0%})"
        )
        if misses:
            print(f"  Misses: {', '.join(misses[:20])}{'...' if len(misses) > 20 else ''}")

        assert rate >= DOMAIN_HIT_RATE_TOP5_FLOOR, (
            f"Domain hit rate {rate:.2%} below floor "
            f"{DOMAIN_HIT_RATE_TOP5_FLOOR:.0%}. Misses: {misses}"
        )

    def test_ndcg10_no_regression(self, pipeline, ground_truth) -> None:
        """nDCG@10 regression gate (KG step 0b).

        RED today: `ndcg_at_10` and `NDCG10_FLOOR` do not exist yet in
        `tests/fixtures/retrieval_scoring.py` / `tests/fixtures/regression_floors.py`.
        Read-only: reuses the module-scoped `pipeline` / `ground_truth`
        fixtures (which skip when Neo4j has zero rules) and does not wipe
        the graph.
        """
        from tests.fixtures.regression_floors import NDCG10_FLOOR  # noqa: PLC0415
        from tests.fixtures.retrieval_scoring import ndcg_at_10  # noqa: PLC0415

        ndcg10, misses = ndcg_at_10(pipeline, ground_truth)
        total = len(ground_truth)
        hits = total - len(misses)
        print(f"\nnDCG@10 (all {total} queries): {ndcg10:.4f} (floor: {NDCG10_FLOOR})")
        print(f"  Hits in top 10: {hits}/{total}")
        if misses:
            print(f"  Misses: {', '.join(misses[:20])}{'...' if len(misses) > 20 else ''}")

        assert ndcg10 >= NDCG10_FLOOR, (
            f"nDCG@10 {ndcg10:.4f} below {NDCG10_FLOOR} floor. Misses: {misses}"
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
        """Full pipeline p95. Budget < 10ms (steady-state production latency).

        Production daemons serve queries continuously; this benchmark
        measures steady-state p95, not the cold-cache latency a query
        sees on the very first request after daemon startup. Cold-start
        is covered separately by test_cold_start with its own budget.

        Per-query instrumentation on 2026-05-15 (Item 2 follow-up)
        showed every cold-cache outlier (15-36x slower than warm) was
        from iteration 0 of each query. Without the warmup loop below,
        iteration-0 outliers dominate the p95 calculation: 10 queries x
        10 iterations = 100 samples; the 5 slowest (p95 index 95-99)
        are all iteration-0 cold-cache hits, not representative of
        production. Adding a warmup pass per query before the timed
        loop is the standard fix for this benchmark category. The
        recorded SCALE_BENCHMARK_RESULTS.md p95 of 0.590 ms was the
        steady-state number; the pre-warmup test was reporting
        ~10 ms not because latency regressed but because the test
        was measuring something different.
        """
        queries = [
            "controller SQL query", "dependency injection", "plugin observer",
            "error handling try catch", "unit test isolation", "named bind parameters",
            "async event loop blocking", "security authorization", "performance optimization",
            "magic number constant",
        ]
        # Warmup: each query once so the cold-cache outlier does not
        # contaminate the timed measurements.
        for q in queries:
            pipeline.query(q)

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
