"""Writ benchmark suite.

Neo4j traversal benchmarks at 1K, 10K, 100K, 1M synthetic nodes.
Per PERF-OPT-001: optimization decisions require measurement.

Run with: pytest benchmarks/run_benchmarks.py -v
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from writ.graph.db import Neo4jConnection

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

EDGES_PER_NODE = 4
BENCHMARK_ITERATIONS = 100


def _generate_synthetic_graph(node_count: int) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Generate synthetic rule nodes and edges for benchmarking."""
    nodes: list[dict] = []
    edges: list[tuple[str, str, str]] = []
    edge_types = ["DEPENDS_ON", "SUPPLEMENTS", "RELATED_TO", "CONFLICTS_WITH"]

    for i in range(node_count):
        rule_id = f"BENCH-RULE-{i:07d}"
        nodes.append({
            "rule_id": rule_id,
            "domain": "Benchmark",
            "severity": "medium",
            "scope": "file",
            "trigger": f"Benchmark trigger {i}",
            "statement": f"Benchmark statement {i}",
            "violation": "Bad.",
            "pass_example": "Good.",
            "enforcement": "Benchmark.",
            "rationale": "Benchmark.",
            "mandatory": False,
            "confidence": "production-validated",
            "evidence": "doc:benchmark",
            "staleness_window": 365,
            "last_validated": "2026-03-15",
        })

    for i in range(node_count):
        for _ in range(EDGES_PER_NODE):
            target = random.randint(0, node_count - 1)
            if target != i:
                edge_type = random.choice(edge_types)
                edges.append((
                    f"BENCH-RULE-{i:07d}",
                    f"BENCH-RULE-{target:07d}",
                    edge_type,
                ))

    return nodes, edges


async def _setup_graph(db: Neo4jConnection, node_count: int) -> None:
    """Insert synthetic graph data in batches."""
    nodes, edges = _generate_synthetic_graph(node_count)

    # Batch insert nodes.
    batch_size = 500
    for i in range(0, len(nodes), batch_size):
        batch = nodes[i:i + batch_size]
        query = """
            UNWIND $batch AS rule
            MERGE (r:Rule {rule_id: rule.rule_id})
            SET r += rule
        """
        async with db._driver.session(database=db._database) as session:
            await session.run(query, batch=batch)

    # Batch insert edges.
    edge_batches: dict[str, list[dict]] = {}
    for src, tgt, etype in edges:
        edge_batches.setdefault(etype, []).append({"src": src, "tgt": tgt})

    for etype, batch_edges in edge_batches.items():
        for i in range(0, len(batch_edges), batch_size):
            batch = batch_edges[i:i + batch_size]
            query = f"""
                UNWIND $batch AS edge
                MATCH (a:Rule {{rule_id: edge.src}})
                MATCH (b:Rule {{rule_id: edge.tgt}})
                MERGE (a)-[:{etype}]->(b)
            """
            async with db._driver.session(database=db._database) as session:
                await session.run(query, batch=batch)


async def _benchmark_traversal(db: Neo4jConnection, node_count: int, hops: int) -> dict:
    """Run traversal benchmark and return latency stats."""
    # Pick random start nodes for benchmarking.
    start_ids = [f"BENCH-RULE-{random.randint(0, node_count - 1):07d}" for _ in range(BENCHMARK_ITERATIONS)]

    latencies: list[float] = []
    for rule_id in start_ids:
        start = time.perf_counter()
        await db.traverse_neighbors(rule_id, hops=hops)
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)

    latencies.sort()
    p50_idx = int(len(latencies) * 0.50)
    p95_idx = int(len(latencies) * 0.95)
    p99_idx = int(len(latencies) * 0.99)

    return {
        "node_count": node_count,
        "hops": hops,
        "iterations": len(latencies),
        "p50_ms": round(latencies[p50_idx], 3),
        "p95_ms": round(latencies[p95_idx], 3),
        "p99_ms": round(latencies[p99_idx], 3),
        "min_ms": round(latencies[0], 3),
        "max_ms": round(latencies[-1], 3),
    }


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestTraversalBenchmarks:
    """Neo4j traversal benchmarks at multiple scales."""

    @pytest.mark.asyncio
    async def test_benchmark_1k_nodes(self) -> None:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            await db.clear_all()
            await _setup_graph(db, 1000)

            for hops in (1, 2):
                stats = await _benchmark_traversal(db, 1000, hops)
                print(f"\n1K nodes, {hops}-hop: {stats}")
                # Stage 4 budget: < 3ms
                if stats["p95_ms"] > 3.0:
                    print(f"  WARNING: p95 {stats['p95_ms']}ms exceeds 3ms budget")
        finally:
            await db.clear_all()
            await db.close()

    @pytest.mark.asyncio
    async def test_benchmark_10k_nodes(self) -> None:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            await db.clear_all()
            await _setup_graph(db, 10_000)

            for hops in (1, 2):
                stats = await _benchmark_traversal(db, 10_000, hops)
                print(f"\n10K nodes, {hops}-hop: {stats}")
                if stats["p95_ms"] > 3.0:
                    print(f"  WARNING: p95 {stats['p95_ms']}ms exceeds 3ms budget")
        finally:
            await db.clear_all()
            await db.close()
