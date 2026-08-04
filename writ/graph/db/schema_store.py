"""Constraint/index schema setup (incl. project_name_unique).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

from writ.graph.db._common import METHODOLOGY_NODE_ID_FIELDS


class SchemaStoreMixin:
    async def apply_constraints(self) -> None:
        """Apply uniqueness constraint and performance indexes. Idempotent via IF NOT EXISTS.

        Includes Phase 1 constraints for all 10 new methodology labels.
        """
        # M.2: identity is composite (id, project), so two projects can hold the
        # same id (namespaced coexistence). Drop the old single-property
        # uniqueness constraints first -- they would forbid that -- then create
        # the composite ones. All idempotent (DROP/CREATE IF [NOT] EXISTS).
        drops = [
            "DROP CONSTRAINT rule_id_unique IF EXISTS",
            "DROP CONSTRAINT abstraction_id_unique IF EXISTS",
        ]
        for label, id_field in METHODOLOGY_NODE_ID_FIELDS.items():
            drops.append(f"DROP CONSTRAINT {label.lower()}_{id_field}_unique IF EXISTS")

        statements = [
            # C4 (audit): serialize concurrent create_project MERGEs on the registry's
            # single scope key. Without a uniqueness constraint, a MERGE on a
            # non-constrained key is not race-safe (concurrent creates fork duplicate
            # :Project nodes); the constraint makes the loser MATCH the winner's node.
            "CREATE CONSTRAINT project_name_unique IF NOT EXISTS "
            "FOR (p:Project) REQUIRE p.name IS UNIQUE",
            "CREATE CONSTRAINT rule_id_project_unique IF NOT EXISTS "
            "FOR (r:Rule) REQUIRE (r.rule_id, r.project) IS UNIQUE",
            "CREATE INDEX rule_domain IF NOT EXISTS FOR (r:Rule) ON (r.domain)",
            "CREATE INDEX rule_mandatory IF NOT EXISTS FOR (r:Rule) ON (r.mandatory)",
            "CREATE CONSTRAINT abstraction_id_project_unique IF NOT EXISTS "
            "FOR (a:Abstraction) REQUIRE (a.abstraction_id, a.project) IS UNIQUE",
            "CREATE INDEX abstraction_domain IF NOT EXISTS FOR (a:Abstraction) ON (a.domain)",
        ]
        # Composite uniqueness per methodology label: (id, project).
        for label, id_field in METHODOLOGY_NODE_ID_FIELDS.items():
            constraint_name = f"{label.lower()}_{id_field}_project_unique"
            statements.append(
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE (n.{id_field}, n.project) IS UNIQUE"
            )
            statements.append(
                f"CREATE INDEX {label.lower()}_domain IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.domain)"
            )
        # Decision-memory Phase 1a: record indexes bounding the downstream
        # recall/commit reads (records are off NODE_ID_FIELDS, so they are not
        # covered by the methodology constraint loop above).
        statements.extend([
            "CREATE INDEX filechange_project_path IF NOT EXISTS "
            "FOR (n:FileChange) ON (n.project, n.path)",
            "CREATE INDEX filechange_project_commit_hash IF NOT EXISTS "
            "FOR (n:FileChange) ON (n.project, n.commit_hash)",
            "CREATE INDEX commit_commit_hash IF NOT EXISTS "
            "FOR (n:Commit) ON (n.commit_hash)",
            "CREATE INDEX decision_project IF NOT EXISTS "
            "FOR (n:Decision) ON (n.project)",
            # Memory mirror: create_memory MERGEs on (name, project) from two
            # concurrent writers (the PostToolUse hook and backfill), and a MERGE
            # on a non-constrained key is not race-safe -- the constraint makes
            # the loser MATCH the winner's node instead of forking a duplicate.
            "CREATE CONSTRAINT memory_name_project_unique IF NOT EXISTS "
            "FOR (n:Memory) REQUIRE (n.name, n.project) IS UNIQUE",
            "CREATE INDEX memory_project IF NOT EXISTS "
            "FOR (n:Memory) ON (n.project)",
        ])
        async with self._driver.session(database=self._database) as session:
            for stmt in drops + statements:
                await session.run(stmt)

    async def list_constraints(self) -> list[dict]:
        """Return all constraints. For verification/testing."""
        rows = await self._run("SHOW CONSTRAINTS")
        return [record.data() for record in rows]

    async def list_indexes(self) -> list[dict]:
        """Return all indexes. For verification/testing."""
        rows = await self._run("SHOW INDEXES")
        return [record.data() for record in rows]
