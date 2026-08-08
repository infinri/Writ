"""Generic node, methodology-node, category, and graph-browse methods.

Moved verbatim from the former writ/graph/db.py (Wave 2 mixin split); methods read self._driver / self._database set by Neo4jConnection.__init__."""
from __future__ import annotations

from writ.graph.db._common import (
    ALLOWED_EDGE_TYPES,
    RECORD_LABELS,
    _GRAPH_ID_COALESCE,
    _node_write_spec,
)


class NodeStoreMixin:
    async def get_subagent_role(self, name: str) -> dict | None:
        """Resolve a SubagentRole by name, by role_id, or by the derived
        ROL-<UPPER>-001 convention (e.g. 'writ-explorer' -> ROL-EXPLORER-001).

        Single source for the resolution clause that the CLI `role-prompt`
        command and the `/subagent-role` route both need (server/CLI parity);
        each caller projects/formats the returned fields itself. Returns None
        if no role matches.
        """
        query = """
            MATCH (r:SubagentRole)
            WHERE r.name = $name
               OR r.role_id = $name
               OR r.role_id = 'ROL-' + toUpper(replace($name, 'writ-', '')) + '-001'
            RETURN r.role_id AS role_id, r.name AS name,
                   r.prompt_template AS prompt_template,
                   r.model_preference AS model_preference,
                   r.dispatched_by AS dispatched_by
            LIMIT 1
        """
        rec = await self._run_single(query, name=name)
        if rec is None:
            return None
        return {
            "role_id": rec["role_id"],
            "name": rec["name"],
            "prompt_template": rec["prompt_template"],
            "model_preference": rec["model_preference"],
            "dispatched_by": rec["dispatched_by"],
        }

    async def create_methodology_node(
        self, node_type: str, data: dict, source_origin: str = "ingest"
    ) -> str:
        """Create or update a methodology node (non-Rule type). Idempotent via MERGE.

        node_type is the Pydantic class name (e.g. 'Skill'), which becomes the
        Neo4j label. data must contain the type-specific primary id field.

        source_origin (0.10): ingest-from-markdown leaves the default 'ingest'. There is
        no graph-first methodology authoring path today (only Rules are proposed), but the
        kwarg is here so a future authoring path passes 'graph-authored' the same way.
        """
        # Identity + props via the shared spec (B5.2); validates node_type +
        # required id_field, applies the 0.10 prop-parity + M.2 composite-key rules.
        label, id_field, node_id, project, props = _node_write_spec(
            node_type, data, source_origin
        )
        query = f"""
            MERGE (n:{label} {{{id_field}: $node_id, project: $project}})
            SET n += $props
            RETURN n.{id_field} AS id
        """
        record = await self._run_single(
            query, node_id=node_id, project=project, props=props
        )
        return record["id"]

    async def batch_create_nodes(
        self, node_specs: list[tuple[str, dict]], source_origin: str = "ingest"
    ) -> dict[str, int]:
        """Bulk-ingest nodes in ONE write transaction via UNWIND-grouped MERGE,
        instead of a session + auto-commit per node (B5.2: ~1,444 round-trips ->
        a handful). Semantically identical to create_rule / create_methodology_node
        -- shares _node_write_spec -- so the graph is byte-identical; the 0.10
        oracle (compute_expected_graph == Neo4j read-back) gates that.

        node_specs: list of (node_type, data), the same args the per-node creates take.
        Grouped by (label, id_field) because MERGE can't parameterize the label and
        methodology types use different id-field property names.

        Returns Neo4j's own write counters {"nodes_created", "properties_set"}. The
        ingest report used to count PARSED nodes, so a run whose `SET n +=` applied
        nothing still printed a full success report -- there was no way to tell an
        applied edit from a silent no-op (2026-08-06 stale-apply observation). The
        counters make that distinguishable at the source.
        """
        from collections import defaultdict

        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for node_type, data in node_specs:
            label, id_field, node_id, project, props = _node_write_spec(
                node_type, data, source_origin
            )
            groups[(label, id_field)].append(
                {"id": node_id, "project": project, "props": props}
            )
        if not groups:
            return {"nodes_created": 0, "properties_set": 0}

        totals = {"nodes_created": 0, "properties_set": 0}

        async def _work(tx) -> None:
            for (label, id_field), rows in groups.items():
                # label + id_field are controlled (validated in _node_write_spec /
                # METHODOLOGY_NODE_ID_FIELDS), not user input -- safe to interpolate,
                # exactly as create_methodology_node does.
                query = (
                    "UNWIND $rows AS row "
                    f"MERGE (n:{label} {{{id_field}: row.id, project: row.project}}) "
                    "SET n += row.props"
                )
                result = await tx.run(query, rows=rows)
                summary = await result.consume()
                counters = getattr(summary, "counters", None)
                totals["nodes_created"] += getattr(counters, "nodes_created", 0) or 0
                totals["properties_set"] += getattr(counters, "properties_set", 0) or 0

        async with self._driver.session(database=self._database) as session:
            await session.execute_write(_work)
        return totals

    async def get_all_nodes(self, label: str | None = None) -> list[dict]:
        """Fetch all nodes across every label, optionally filtered to one label.

        Each returned dict is the node's properties plus a 'label' key holding
        its primary label (labels(n)[0]).
        """
        if label is not None:
            query = f"MATCH (n:{label}) RETURN n, labels(n) AS labels"
        else:
            query = "MATCH (n) RETURN n, labels(n) AS labels"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            nodes = []
            async for record in result:
                data = dict(record["n"])
                labels = record["labels"]
                data["label"] = labels[0] if labels else None
                nodes.append(data)
            return nodes

    async def get_all_nodes_by_type(self, node_type: str) -> list[dict]:
        """Fetch all nodes carrying the given label. Convenience wrapper."""
        return await self.get_all_nodes(label=node_type)

    async def get_nodes_by_category(
        self, category: str, project: str = "writ", exclude_id: str | None = None
    ) -> list[dict]:
        """Nodes (any label) carrying `category`, scoped to `project`. Used by the
        6.3b promotion review artifact to surface same-category conflict candidates.
        Returns [{id, statement, type}]; excludes `exclude_id` (the candidate itself)."""
        id_coalesce = _GRAPH_ID_COALESCE.format(v="n")
        query = (
            f"MATCH (n) WHERE n.category = $category AND coalesce(n.project, 'writ') = $project "
            f"RETURN {id_coalesce} AS id, n.statement AS statement, labels(n)[0] AS type"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, category=category, project=project)
            out = []
            async for r in result:
                nid = r["id"]
                if nid is None or nid == exclude_id:
                    continue
                out.append({"id": nid, "statement": r["statement"] or "", "type": r["type"]})
            return out

    async def get_all_edges_cross_type(self) -> list[dict]:
        """Fetch every edge in the graph with src/dst ids + labels and edge type.

        Each dict carries: source_id, source_label, target_id, target_label, type.
        Node id is the first present primary-id property among known *_id fields.
        """
        a_id = _GRAPH_ID_COALESCE.format(v="a")
        b_id = _GRAPH_ID_COALESCE.format(v="b")
        query = f"""
            MATCH (a)-[rel]->(b)
            WHERE NOT any(l IN labels(a) WHERE l IN $record_labels)
              AND NOT any(l IN labels(b) WHERE l IN $record_labels)
            RETURN
                {a_id} AS source_id,
                labels(a)[0] AS source_label,
                {b_id} AS target_id,
                labels(b)[0] AS target_label,
                type(rel) AS type
        """
        rows = await self._run(query, record_labels=sorted(RECORD_LABELS))
        return [record.data() for record in rows]

    async def get_all_nodes_for_dump(self) -> list[dict]:
        """Fetch every CORPUS node's full properties keyed by its portable business id.

        Each dict carries: id, label, props. Unlike `get_all_nodes`, this always
        includes the coalesced business id (never Neo4j's internal element id),
        so a dump taken here stays valid when replayed into a different Neo4j
        instance -- for the Cypher dump/restore path (writ/graph/dump.py).

        Runtime records (RECORD_LABELS) are EXCLUDED. The dump is the shipped,
        git-tracked corpus; mirrored memories and decision records are private
        operational state that must never be serialized into it. They also carry
        none of the NODE_ID_FIELDS keys, so they would arrive with a null id and
        break the id-sorted render besides.
        """
        nid = _GRAPH_ID_COALESCE.format(v="n")
        query = (
            f"MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN $record_labels) "
            f"RETURN {nid} AS id, labels(n)[0] AS label, properties(n) AS props"
        )
        rows = await self._run(query, record_labels=sorted(RECORD_LABELS))
        return [record.data() for record in rows]

    async def get_category_routes_by_node(self) -> dict[str, list[str]]:
        """Map every node's primary id to its Category's routes via BELONGS_TO.

        One bulk full-graph join over ALL labels (Rule + methodology), feeding
        RetrievalPipeline.node_routes in build_pipeline.
        """
        nid = _GRAPH_ID_COALESCE.format(v="n")
        query = f"MATCH (n)-[:BELONGS_TO]->(c:Category) RETURN {nid} AS id, c.routes AS routes"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            rows = [record.data() async for record in result]
        return {r["id"]: r["routes"] for r in rows if r.get("id") is not None and r.get("routes")}

    async def get_graph_nodes_and_edges(
        self, project: str = "writ", node_limit: int = 5000
    ) -> tuple[list[dict], list[dict]]:
        """Read-only project-scoped node + edge census for the graph explorer.

        Returns (nodes, edges) where each node is
        {id, type, domain, severity, mandatory, provenance} and each edge is
        {source, target, type}. The query is parameterized and project-scoped;
        node_limit is a hard cap (interpolated only as an integer literal, never
        user text) so a pathological graph can't return an unbounded result set.
        Filtering, the caller-facing limit, truncation, and dangling-edge removal
        are applied by the server route, not here -- this returns the raw scoped
        corpus. ALLOWED_EDGE_TYPES bounds which relationship types are returned.
        """
        node_cap = max(1, int(node_limit))
        nid = _GRAPH_ID_COALESCE.format(v="n")
        node_query = (
            f"MATCH (n) WHERE coalesce(n.project, 'writ') = $project "
            f"RETURN {nid} AS id, labels(n)[0] AS type, n.domain AS domain, "
            f"n.severity AS severity, n.mandatory AS mandatory, "
            f"n.provenance AS provenance "
            f"ORDER BY id LIMIT {node_cap}"
        )
        a_id = _GRAPH_ID_COALESCE.format(v="a")
        b_id = _GRAPH_ID_COALESCE.format(v="b")
        edge_query = (
            f"MATCH (a)-[rel]->(b) "
            f"WHERE coalesce(a.project, 'writ') = $project "
            f"AND coalesce(b.project, 'writ') = $project "
            f"AND type(rel) IN $edge_types "
            f"RETURN {a_id} AS source, {b_id} AS target, type(rel) AS type"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(node_query, project=project)
            nodes = [record.data() async for record in result]
            result = await session.run(
                edge_query, project=project, edge_types=sorted(ALLOWED_EDGE_TYPES)
            )
            edges = [record.data() async for record in result]
        nodes = [n for n in nodes if n.get("id") is not None]
        edges = [
            e for e in edges
            if e.get("source") is not None and e.get("target") is not None
        ]
        return nodes, edges

    async def get_node_with_neighbors(
        self, node_id: str, project: str = "writ"
    ) -> dict | None:
        """Read-only fetch of one node's full props + its incident edges.

        Returns {id, type, props, neighbors:[{id, type, edge_type, direction}]}
        or None when no node with `node_id` exists in `project`. Matching is by
        the coalesced primary id across every known label (NODE_ID_FIELDS), so a
        Rule, Skill, Playbook, etc. all resolve. Parameterized + project-scoped;
        ALLOWED_EDGE_TYPES bounds the neighbor relationship types.
        """
        nid = _GRAPH_ID_COALESCE.format(v="n")
        node_query = (
            f"MATCH (n) WHERE {nid} = $node_id "
            f"AND coalesce(n.project, 'writ') = $project "
            f"RETURN n, labels(n)[0] AS type LIMIT 1"
        )
        other_id = _GRAPH_ID_COALESCE.format(v="other")
        neighbor_query = (
            f"MATCH (n) WHERE {nid} = $node_id "
            f"AND coalesce(n.project, 'writ') = $project "
            f"MATCH (n)-[rel]-(other) "
            f"WHERE coalesce(other.project, 'writ') = $project "
            f"AND type(rel) IN $edge_types "
            f"RETURN {other_id} AS id, labels(other)[0] AS type, "
            f"type(rel) AS edge_type, "
            f"CASE WHEN startNode(rel) = n THEN 'out' ELSE 'in' END AS direction"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(node_query, node_id=node_id, project=project)
            record = await result.single()
            if record is None or record.get("n") is None:
                return None
            props = dict(record["n"])
            node_type = record["type"]
            result = await session.run(
                neighbor_query,
                node_id=node_id,
                project=project,
                edge_types=sorted(ALLOWED_EDGE_TYPES),
            )
            neighbors = [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "edge_type": r["edge_type"],
                    "direction": r["direction"],
                }
                async for r in result
                if r["id"] is not None
            ]
        return {
            "id": node_id,
            "type": node_type,
            "props": props,
            "neighbors": neighbors,
        }

    async def traverse_neighbors(self, rule_id: str, hops: int = 1) -> list[dict]:
        """Return neighbors within N hops, including edge types.

        Each result dict contains: rule_id, edge_type, from_id, to_id.
        Neo4j does not allow parameterized relationship lengths,
        so hops is validated and interpolated as a literal.
        """
        max_hops = 3
        if not (1 <= hops <= max_hops):
            raise ValueError(f"hops must be between 1 and {max_hops}")
        query = f"""
            MATCH (start:Rule {{rule_id: $rule_id}})-[rel*1..{hops}]-(neighbor:Rule)
            WITH neighbor, rel
            UNWIND rel AS r
            RETURN DISTINCT
                neighbor.rule_id AS rule_id,
                type(r) AS edge_type,
                startNode(r).rule_id AS from_id,
                endNode(r).rule_id AS to_id
        """
        rows = await self._run(query, rule_id=rule_id)
        records = [record.data() for record in rows]
        return records
