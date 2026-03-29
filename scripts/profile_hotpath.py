"""Profile the retrieval pipeline hot path with pyinstrument.

Runs 100 queries and prints a tree showing where time is spent.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyinstrument import Profiler

from writ.graph.db import Neo4jConnection
from writ.retrieval.pipeline import build_pipeline

QUERIES = [
    "controller SQL query",
    "dependency injection",
    "plugin observer",
    "error handling try catch",
    "unit test isolation",
    "named bind parameters",
    "async event loop blocking",
    "security authorization",
    "performance optimization",
    "magic number constant",
]
ITERATIONS = 10  # 10 x 10 queries = 100 total


async def main():
    db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
    print("Building pipeline...")
    pipeline = await build_pipeline(db)

    # Warmup
    for q in QUERIES:
        pipeline.query(q)

    # Profile
    profiler = Profiler()
    latencies = []

    profiler.start()
    for _ in range(ITERATIONS):
        for q in QUERIES:
            start = time.perf_counter()
            pipeline.query(q)
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)
    profiler.stop()

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    print(f"\nLatency: p50={p50:.2f}ms, p95={p95:.2f}ms, p99={p99:.2f}ms")
    print(f"Total queries: {len(latencies)}\n")

    profiler.print()

    await db.close()


asyncio.run(main())
