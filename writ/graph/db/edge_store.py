"""Corpus + record edge creation (incl. the wire_* wrappers).

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

from writ.graph.db._common import (
    ALLOWED_EDGE_TYPES,
    _RECORD_EDGE_ENDPOINTS,
    _id_field_for_label,
    _id_or_match,
)


class EdgeStoreMixin:
    async def create_edge(
        self, edge_type: str, source_id: str, target_id: str, project: str = "writ"
    ) -> None:
        """Create a typed edge between two nodes. Idempotent via MERGE.

        Source/target nodes are matched by their `<type>_id` property; any node
        label with the expected property key is eligible (Rule, Skill, Playbook,
        etc.). This lets Phase 1 methodology edges link across node labels.

        M.1: the edge carries a `project` property (default 'writ') so the
        project-scoped reconcile/parity reads can filter edges, not just nodes.
        """
        if edge_type not in ALLOWED_EDGE_TYPES:
            raise ValueError(f"Unknown edge type: {edge_type}")
        # Cypher does not support parameterized relationship types, but edge_type
        # is validated against the allowed set above. Match any labeled node that
        # carries a primary-id property matching source_id / target_id. The OR-match
        # is derived from NODE_ID_FIELDS via _id_or_match (Contract C), not a
        # hardcoded list, so a new node type propagates automatically.
        # M.2: endpoints are matched WITHIN $project, so an edge never resolves to
        # another project's node that happens to share the same id.
        source_match = _id_or_match("a", "source_id")
        target_match = _id_or_match("b", "target_id")
        query = f"""
            MATCH (a) WHERE a.project = $project AND ({source_match})
            MATCH (b) WHERE b.project = $project AND ({target_match})
            MERGE (a)-[e:{edge_type}]->(b)
            SET e.project = $project
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query, source_id=source_id, target_id=target_id, project=project
            )

    async def create_record_edge(
        self, edge_type: str, *, src_label: str, src_id_field: str, src_id: str,
        tgt_label: str, tgt_id_field: str, tgt_id: str, project: str,
    ) -> None:
        """Create a typed edge between two RECORD endpoints, matched by explicit
        label + id-field. Idempotent via MERGE.

        Unlike create_edge (which resolves endpoints via the NODE_ID_FIELDS
        OR-match, excluding decision_id), this matches each endpoint by its given
        label and id-field, so it resolves Decision/Project/Rule record nodes.
        Labels and id-field names come from controlled callers and interpolate the
        same way batch_create_edges interpolates labels; id VALUES and project are
        bound parameters. Each endpoint is matched by its unique id field alone
        (no project filter), so GOVERNED_BY wires cross-project to the shared rule
        corpus and HAS_DECISION wires to the registered Project, with no
        duplication since every id is unique.
        """
        if edge_type not in ALLOWED_EDGE_TYPES:
            raise ValueError(f"Unknown edge type: {edge_type}")
        for label, id_field in ((src_label, src_id_field), (tgt_label, tgt_id_field)):
            if _RECORD_EDGE_ENDPOINTS.get(label) != id_field:
                raise ValueError(f"Unknown record-edge endpoint: {label}.{id_field}")
        src_clause = self._record_endpoint_clause("a", src_label, src_id_field, "src_id")
        tgt_clause = self._record_endpoint_clause("b", tgt_label, tgt_id_field, "tgt_id")
        query = f"""
            {src_clause}
            {tgt_clause}
            MERGE (a)-[e:{edge_type}]->(b)
            SET e.project = $project
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(
                query, src_id=src_id, tgt_id=tgt_id, project=project
            )

    @staticmethod
    def _record_endpoint_clause(var: str, label: str, id_field: str, id_param: str) -> str:
        """Build the MATCH clause for one record-edge endpoint. Label + id-field are
        controlled (interpolated); the id VALUE is a bound parameter.

        Every endpoint is matched by its unique id field alone, no project filter:
        rules live in the shared corpus and a Decision is scoped to its repo project,
        so GOVERNED_BY is inherently cross-project; each id is unique, so id-only
        matching wires exactly one edge with no duplication.
        """
        return f"MATCH ({var}:{label} {{{id_field}: ${id_param}}})"

    async def wire_has_decision(
        self, project_name: str, decision_id: str, project: str
    ) -> None:
        """Wire Project -[HAS_DECISION]-> Decision (Project matched by name)."""
        await self.create_record_edge(
            "HAS_DECISION",
            src_label="Project", src_id_field="name", src_id=project_name,
            tgt_label="Decision", tgt_id_field="decision_id", tgt_id=decision_id,
            project=project,
        )

    async def wire_governed_by(
        self, decision_id: str, rule_id: str, project: str
    ) -> None:
        """Wire Decision -[GOVERNED_BY]-> Rule (both matched within project)."""
        await self.create_record_edge(
            "GOVERNED_BY",
            src_label="Decision", src_id_field="decision_id", src_id=decision_id,
            tgt_label="Rule", tgt_id_field="rule_id", tgt_id=rule_id,
            project=project,
        )

    async def wire_has_change(
        self, project_name: str, change_id: str, project: str
    ) -> None:
        """Wire Project -[HAS_CHANGE]-> FileChange (Project matched by name)."""
        await self.create_record_edge(
            "HAS_CHANGE",
            src_label="Project", src_id_field="name", src_id=project_name,
            tgt_label="FileChange", tgt_id_field="change_id", tgt_id=change_id,
            project=project,
        )

    async def wire_has_commit(
        self, project_name: str, commit_hash: str, project: str
    ) -> None:
        """Wire Project -[HAS_COMMIT]-> Commit (Project matched by name)."""
        await self.create_record_edge(
            "HAS_COMMIT",
            src_label="Project", src_id_field="name", src_id=project_name,
            tgt_label="Commit", tgt_id_field="commit_hash", tgt_id=commit_hash,
            project=project,
        )

    async def wire_includes(
        self, commit_hash: str, change_id: str, project: str
    ) -> None:
        """Wire Commit -[INCLUDES]-> FileChange."""
        await self.create_record_edge(
            "INCLUDES",
            src_label="Commit", src_id_field="commit_hash", src_id=commit_hash,
            tgt_label="FileChange", tgt_id_field="change_id", tgt_id=change_id,
            project=project,
        )

    async def wire_motivated_by(
        self, change_id: str, decision_id: str, project: str
    ) -> None:
        """Wire FileChange -[MOTIVATED_BY]-> Decision."""
        await self.create_record_edge(
            "MOTIVATED_BY",
            src_label="FileChange", src_id_field="change_id", src_id=change_id,
            tgt_label="Decision", tgt_id_field="decision_id", tgt_id=decision_id,
            project=project,
        )

    async def wire_about(
        self, question_id: str, target_label: str, target_id: str, project: str
    ) -> None:
        """Wire OpenQuestion -[ABOUT]-> Rule or Decision; any other target label raises."""
        if target_label not in ("Rule", "Decision"):
            raise ValueError(
                f"ABOUT target for {question_id} must be Rule or Decision, not {target_label!r}")
        await self.create_record_edge(
            "ABOUT",
            src_label="OpenQuestion", src_id_field="question_id", src_id=question_id,
            tgt_label=target_label, tgt_id_field=_RECORD_EDGE_ENDPOINTS[target_label],
            tgt_id=target_id, project=project,
        )

    async def wire_realizes(
        self, commit_hash: str, decision_id: str, project: str
    ) -> None:
        """Wire Commit -[REALIZES]-> Decision."""
        await self.create_record_edge(
            "REALIZES",
            src_label="Commit", src_id_field="commit_hash", src_id=commit_hash,
            tgt_label="Decision", tgt_id_field="decision_id", tgt_id=decision_id,
            project=project,
        )

    async def batch_create_edges(
        self, edges: list[dict], project: str = "writ", id_to_label: dict | None = None
    ) -> tuple[int, int]:
        """Bulk-create typed edges in ONE write transaction via UNWIND-grouped MERGE
        (B5.2). edges: list of {"type","source","target"} as derive_edges produces.
        Returns (created, write_dangling): created counts every valid-type edge (an
        edge whose endpoints don't resolve no-ops its MERGE but is still counted, as
        the per-edge create_edge loop did); write_dangling counts unknown-type edges
        (create_edge's ValueError path). Endpoints matched within $project (M.2).

        id_to_label maps an endpoint id to its node label. When BOTH endpoints of an
        edge resolve, the match is label-scoped + uses the (id, project) index (the
        FAST path: ~3x over the label-less scan). Endpoints absent from the map fall
        back to the label-less OR-match (the general path: any id, no index) -- so the
        method is correct for any caller; ingest_edges supplies a complete map.
        """
        from collections import defaultdict

        id_to_label = id_to_label or {}
        # (etype, src_label, tgt_label) -> rows  [index-using fast path]
        resolved: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        # etype -> rows  [label-less general path]
        fallback: dict[str, list[dict]] = defaultdict(list)
        write_dangling = 0
        for e in edges:
            etype = e["type"]
            if etype not in ALLOWED_EDGE_TYPES:
                write_dangling += 1
                continue
            sl = id_to_label.get(e["source"])
            tl = id_to_label.get(e["target"])
            if sl and tl:
                resolved[(etype, sl, tl)].append(
                    {"s": e["source"], "t": e["target"], "project": project}
                )
            else:
                fallback[etype].append(
                    {"source_id": e["source"], "target_id": e["target"], "project": project}
                )
        created = sum(len(r) for r in resolved.values()) + sum(len(r) for r in fallback.values())
        if not resolved and not fallback:
            return created, write_dangling

        async def _work(tx) -> None:
            # Fast path: label-scoped, index-using seeks. Labels/id-fields are
            # controlled (from id_to_label / METHODOLOGY_NODE_ID_FIELDS), not user
            # input -- safe to interpolate, as create_methodology_node does.
            for (etype, sl, tl), rows in resolved.items():
                sidf = _id_field_for_label(sl)
                tidf = _id_field_for_label(tl)
                query = (
                    "UNWIND $rows AS row "
                    f"MATCH (a:{sl} {{{sidf}: row.s, project: row.project}}) "
                    f"MATCH (b:{tl} {{{tidf}: row.t, project: row.project}}) "
                    f"MERGE (a)-[e:{etype}]->(b) "
                    "SET e.project = row.project"
                )
                await tx.run(query, rows=rows)
            # General path: label-less OR-match for any unresolved endpoint. The
            # OR-clause is derived from NODE_ID_FIELDS via _id_or_match (Contract C),
            # not a hardcoded list -- the same derivation create_edge uses -- so the
            # two paths can never drift from the schema.
            src_match = _id_or_match("a", "row.source_id").replace("$row.", "row.")
            tgt_match = _id_or_match("b", "row.target_id").replace("$row.", "row.")
            for etype, rows in fallback.items():
                query = f"""
                    UNWIND $rows AS row
                    MATCH (a) WHERE a.project = row.project AND ({src_match})
                    MATCH (b) WHERE b.project = row.project AND ({tgt_match})
                    MERGE (a)-[e:{etype}]->(b)
                    SET e.project = row.project
                """
                await tx.run(query, rows=rows)

        async with self._driver.session(database=self._database) as session:
            await session.execute_write(_work)
        return created, write_dangling
