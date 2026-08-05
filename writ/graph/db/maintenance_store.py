"""Bulk clear (test/admin support).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations



class MaintenanceStoreMixin:
    async def clear_all(self, preserve_labels: frozenset[str] = frozenset()) -> None:
        """Delete ALL nodes and edges across EVERY project. For test cleanup and
        the explicit --all-projects path only. Project-scoped callers use
        clear_project (M.1) so wiping one project never touches another.

        `preserve_labels`: labels exempted from the wipe. The corpus-replay path
        (import_cypher_dump) passes the runtime-record labels absent from its dump:
        a corpus dump is not the whole graph, and operational records must survive
        a corpus replay. Default empty keeps test-cleanup semantics unchanged.
        """
        async with self._driver.session(database=self._database) as session:
            if preserve_labels:
                await session.run(
                    "MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN $preserve) "
                    "DETACH DELETE n",
                    preserve=sorted(preserve_labels),
                )
            else:
                await session.run("MATCH (n) DETACH DELETE n")

    async def execute(self, statement: str) -> None:
        """Run a single raw Cypher statement with no return value expected.

        For the graph-dump import path (writ/graph/dump.py), which replays a
        pre-rendered script of literal CREATE/MATCH statements.
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(statement)

    async def clear_project(self, project: str = "writ") -> int:
        """Delete all nodes (and their edges) for one project. M.1: the scoped
        analog of clear_all -- the safe default once the graph holds >1 project.
        Returns the node count deleted."""
        rec = await self._run_single(
            "MATCH (n) WHERE n.project = $project "
            "WITH collect(n) AS ns, count(n) AS c "
            "FOREACH (x IN ns | DETACH DELETE x) "
            "RETURN c AS deleted",
            project=project,
        )
        return rec["deleted"] if rec else 0
