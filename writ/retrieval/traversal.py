"""Neo4j Cypher 1-2 hop traversal from candidate rule_ids.

Two backends:
1. Live Neo4j queries (used during ingest/validate, offline operations)
2. Pre-computed adjacency cache (used in hot path, built at startup)

Latency budget: < 3ms. Neo4j live queries exceed this (Phase 2 benchmarks).
The adjacency cache is the mitigation: traversal becomes a dict lookup (< 0.1ms).

Per ARCH-DI-001: receives db connection via constructor injection.
Per PERF-IO-001: hot-path traversal uses in-memory cache, no I/O.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.graph.db import GraphConnection


class AdjacencyCache:
    """In-memory adjacency list for hot-path traversal.

    Built from Neo4j at startup. Lookup is O(1) per rule_id.
    """

    def __init__(self) -> None:
        self._neighbors: dict[str, list[dict]] = {}
        self._build_time_ms: float = 0.0

    async def build_from_db(self, db: GraphConnection) -> int:
        """Load all edges from Neo4j into memory.

        Returns the number of rules with at least one neighbor.
        """
        start = time.perf_counter()
        # Fetch all edges in a single query.
        query = """
            MATCH (a:Rule)-[r]->(b:Rule)
            RETURN a.rule_id AS source, type(r) AS edge_type, b.rule_id AS target
        """
        async with db._driver.session(database=db._database) as session:
            result = await session.run(query)
            records = [record.data() async for record in result]

        self._neighbors.clear()
        for rec in records:
            src = rec["source"]
            tgt = rec["target"]
            edge_type = rec["edge_type"]
            # Store both directions for undirected lookup.
            self._neighbors.setdefault(src, []).append({
                "rule_id": tgt,
                "edge_type": edge_type,
                "direction": "outgoing",
            })
            self._neighbors.setdefault(tgt, []).append({
                "rule_id": src,
                "edge_type": edge_type,
                "direction": "incoming",
            })

        self._build_time_ms = (time.perf_counter() - start) * 1000
        return len(self._neighbors)

    def get_neighbors(self, rule_id: str) -> list[dict]:
        """Return cached neighbors for a rule_id. O(1) lookup."""
        return self._neighbors.get(rule_id, [])

    def get_enrichment(self, rule_ids: list[str]) -> dict[str, list[dict]]:
        """For each candidate rule_id, return its neighbors from cache."""
        return {rid: self.get_neighbors(rid) for rid in rule_ids}

    @property
    def build_time_ms(self) -> float:
        return self._build_time_ms

    @property
    def size(self) -> int:
        return len(self._neighbors)


class GraphTraverser:
    """Traverses the rule graph from candidate rule_ids.

    Per PY-ASYNC-001: all operations are async.
    """

    def __init__(self, db: GraphConnection) -> None:
        self._db = db

    async def get_neighbors(self, rule_id: str, hops: int = 1) -> list[dict]:
        """Return neighbor rules within N hops with edge type info."""
        return await self._db.traverse_neighbors(rule_id, hops=hops)

    async def enrich_candidates(
        self, rule_ids: list[str], hops: int = 1
    ) -> dict[str, list[dict]]:
        """For each candidate rule_id, fetch its neighbors.

        Returns a mapping of rule_id -> list of neighbor dicts.
        Used by Stage 4 of the retrieval pipeline.
        """
        result: dict[str, list[dict]] = {}
        for rule_id in rule_ids:
            result[rule_id] = await self._db.traverse_neighbors(rule_id, hops=hops)
        return result
