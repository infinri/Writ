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

        Phase 1 expansion: matches any labeled node (Rule, Skill, Playbook,
        AntiPattern, etc.) so methodology edges surface during Stage 4
        enrichment. Cache key is the node's primary id field value.

        Returns the number of nodes with at least one neighbor.
        """
        start = time.perf_counter()
        # Accept any label; project the primary id via coalesce across all
        # known id fields. This handles the Phase 1 methodology labels without
        # requiring a label-per-query Cypher expansion.
        query = """
            MATCH (a)-[r]->(b)
            WITH a, r, b,
                 coalesce(a.rule_id, a.skill_id, a.playbook_id, a.technique_id,
                          a.antipattern_id, a.forbidden_id, a.phase_id,
                          a.rationalization_id, a.scenario_id, a.example_id,
                          a.role_id) AS src_id,
                 coalesce(b.rule_id, b.skill_id, b.playbook_id, b.technique_id,
                          b.antipattern_id, b.forbidden_id, b.phase_id,
                          b.rationalization_id, b.scenario_id, b.example_id,
                          b.role_id) AS tgt_id
            WHERE src_id IS NOT NULL AND tgt_id IS NOT NULL
            RETURN src_id AS source, type(r) AS edge_type, tgt_id AS target
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

    def get_bundle(self, rule_id: str, max_depth: int = 2) -> set[str]:
        """Return all node IDs reachable within max_depth hops, including the seed.

        Phase 1 addition per plan Section 3.1: methodology edges (TEACHES, GATES,
        COUNTERS, PRESSURE_TESTS, CONTAINS, ATTACHED_TO, DISPATCHES, DEMONSTRATES)
        form multi-hop rationalization-counter chains. 2-hop is the default
        because a Playbook→AntiPattern→Skill pattern needs depth 2 to traverse.
        """
        bundle = {rule_id}
        frontier = {rule_id}
        for _ in range(max_depth):
            nxt: set[str] = set()
            for nid in frontier:
                for neighbor in self._neighbors.get(nid, []):
                    if neighbor["rule_id"] not in bundle:
                        bundle.add(neighbor["rule_id"])
                        nxt.add(neighbor["rule_id"])
            frontier = nxt
            if not frontier:
                break
        return bundle

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
