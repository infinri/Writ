"""Bulk clear (test/admin support).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations



class MaintenanceStoreMixin:
    async def clear_all(self, preserve_labels: frozenset[str] | None = None) -> None:
        """Delete the corpus graph across EVERY project. For test cleanup and the
        explicit --all-projects path only. Project-scoped callers use clear_project
        (M.1) so wiping one project never touches another.

        Runtime records are preserved BY DEFAULT (`preserve_labels=None` resolves to
        RECORD_LABELS: Memory, Decision, FileChange, Commit). They are operational
        state with no bible or dump home, so nothing that rebuilds the CORPUS should
        take them with it -- and every one of this method's ~100 call sites is a test
        fixture or an admin corpus rebuild, which is why the safe posture belongs in
        the default rather than in each caller. Records were destroyed three times in
        one session before this default existed: `writ memory backfill` rebuilt them
        from files each time, but a Decision record has no such source.

        Pass an explicit `frozenset()` for a genuine everything-wipe; pass a specific
        set (as import_cypher_dump does, computing RECORD_LABELS minus the labels its
        dump actually creates) to keep exact-replace semantics for labels the caller
        is itself restoring.
        """
        from writ.graph.db._common import RECORD_LABELS

        preserve = RECORD_LABELS if preserve_labels is None else preserve_labels
        async with self._driver.session(database=self._database) as session:
            # CONSUME, do not fire and forget. `session.run` returns a lazy result: the
            # statement is dispatched but this coroutine can return before the delete has
            # finished server-side. import_cypher_dump then opens NEW sessions and replays
            # CREATE statements, so the wipe and the rebuild can overlap, and a create
            # landing mid-delete is exactly how a DETACH DELETE ends up chasing a node id
            # that no longer resolves (Neo.ClientError.Statement.EntityNotFound).
            # rule_store and node_store already consume; this file did not, and it is the
            # one that deletes.
            if preserve:
                result = await session.run(
                    "MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN $preserve) "
                    "DETACH DELETE n",
                    preserve=sorted(preserve),
                )
            else:
                result = await session.run("MATCH (n) DETACH DELETE n")
            await result.consume()

    async def execute(self, statement: str) -> None:
        """Run a single raw Cypher statement with no return value expected.

        For the graph-dump import path (writ/graph/dump.py), which replays a
        pre-rendered script of literal CREATE/MATCH statements.

        Consumed for the same reason clear_all is: the replay is a sequence of separate
        sessions, so each statement must actually finish before the next one starts.
        Returning early makes the replay order advisory, which for a script whose
        MATCH statements depend on earlier CREATEs is not a property to leave to timing.
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(statement)
            await result.consume()

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
