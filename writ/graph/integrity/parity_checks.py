"""Node/category plumbing + parity/reconcile oracle (PARITY_EXEMPT_PROVENANCE keystone).

Moved verbatim from the former writ/graph/integrity.py (Wave 2 mixin split); methods read self._driver / self._database set by IntegrityChecker.__init__."""
from __future__ import annotations

from pathlib import Path

from writ.graph.integrity._common import (
    MANAGED_PROP_NAMES,
    NODE_ID_FIELDS,
    ORACLE_BLIND_LABELS,
    PARITY_EXEMPT_PROVENANCE,
    _GRAPH_ID_COALESCE,
    _coerce_neo4j_value,
    compute_expected_graph,
    expected_managed_props,
    parse_nodes_from_file,
    parse_rules_from_file,
    parse_source,
    read_live_edges,
    read_live_nodes_with_keys,
    read_oracle_blind_node_ids,
)


class ParityChecksMixin:
    async def get_all_nodes(self) -> list[dict]:
        """Return every node in the graph as {type, id} dicts.

        Mirrors db.get_all_nodes (which keys the primary label under 'label')
        and normalizes to the {type, id} shape used by parity/reachability.
        """
        query = "MATCH (n) RETURN n, labels(n) AS labels"
        result: list[dict] = []
        async with self._driver.session(database=self._database) as session:
            res = await session.run(query)
            async for record in res:
                labels = record["labels"]
                label = labels[0] if labels else None
                props = dict(record["n"])
                id_field = NODE_ID_FIELDS.get(label) if label else None
                node_id = props.get(id_field) if id_field else None
                if node_id is None:
                    continue
                result.append({
                    "type": label, "id": node_id,
                    "project": props.get("project", "writ"),  # M.1
                    "provenance": props.get("provenance"),  # 6.4: parity exemption axis
                })
        return result

    async def get_category_count(self) -> int:
        """Count Category nodes in the graph."""
        if self._driver is None:
            return 0
        query = "MATCH (c:Category) RETURN count(c) AS count"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            record = await result.single()
            return record["count"]

    async def _get_nodes_without_belongs_to(self) -> list[dict]:
        """Return non-Category nodes that have no BELONGS_TO edge to a Category.

        Single graph scan. Was get_all_nodes() + one exists()-query per node
        (an N+1: O(corpus) round-trips); this is one round-trip. A node whose
        label the markdown ingest cannot author (ORACLE_BLIND_LABELS, cycle 7 --
        Abstraction today) is a graph-derived materialized view, not a routed
        source node, so it is exempt from the BELONGS_TO requirement, same as
        Category. The `{nid} IS NOT NULL` clause reproduces get_all_nodes' skip
        of nodes with no known primary-id field.
        """
        nid = _GRAPH_ID_COALESCE.format(v="n")
        query = (
            "MATCH (n) "
            "WHERE NOT n:Category "
            "AND NONE(l IN labels(n) WHERE l IN $blind_labels) "
            f"AND {nid} IS NOT NULL "
            "AND NOT (n)-[:BELONGS_TO]->(:Category) "
            f"RETURN labels(n)[0] AS type, {nid} AS id, "
            "coalesce(n.project, 'writ') AS project, n.provenance AS provenance"
        )
        async with self._driver.session(database=self._database) as session:
            res = await session.run(query, blind_labels=sorted(ORACLE_BLIND_LABELS))
            return [
                {
                    "type": r["type"], "id": r["id"],
                    "project": r["project"], "provenance": r["provenance"],
                }
                async for r in res
            ]

    async def detect_category_reachability(self) -> dict:
        """Check that every non-Category node has a BELONGS_TO category edge.

        Skips (with a reason) when no Category nodes exist; otherwise reports
        nodes_without_category and categories_without_route.
        """
        category_count = await self.get_category_count()
        if category_count == 0:
            return {
                "skipped": True,
                "reason": "no Category nodes exist; reachability check not applicable",
            }

        nodes_without_category = await self._get_nodes_without_belongs_to()
        categories_without_route = await self._get_categories_without_route()
        return {
            "skipped": False,
            "nodes_without_category": nodes_without_category,
            "categories_without_route": categories_without_route,
        }

    async def _get_categories_without_route(self) -> list[str]:
        """Return category_ids whose routes property is empty or absent."""
        if self._driver is None:
            return []
        query = """
            MATCH (c:Category)
            WHERE c.routes IS NULL OR size(c.routes) = 0
            RETURN c.category_id AS category_id
            ORDER BY category_id
        """
        return [record["category_id"] for record in await self._run(query)]

    async def detect_parity_violations(
        self, bible_dir: Path | None = None, project: str = "writ"
    ) -> list[dict]:
        """Find graph nodes absent from every *.md file under bible_dir.

        A parity violation is a node present in the graph but not declared in
        any markdown file. Returns [{type, id}]; empty when all present.

        When bible_dir is None there is no markdown corpus to compare against,
        so the check falls back to self._default_bible_dir (if set) and
        otherwise returns an empty list.

        M.1: scoped to `project` -- only THIS project's nodes are checked against
        THIS project's bible, so another project's nodes are never false-flagged
        as "absent from markdown".
        """
        if bible_dir is None:
            bible_dir = self._default_bible_dir
        if bible_dir is None:
            return []

        graph_nodes = [
            n for n in await self.get_all_nodes() if n.get("project", "writ") == project
        ]

        declared: set[str] = set()
        markdown_text: list[str] = []
        bible_path = Path(bible_dir)
        for md_file in sorted(bible_path.rglob("*.md")):
            try:
                markdown_text.append(md_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            for node in parse_nodes_from_file(md_file):
                node_type = node.get("node_type")
                id_field = NODE_ID_FIELDS.get(node_type) if node_type else None
                if id_field and node.get(id_field):
                    declared.add(node[id_field])
            for rule in parse_rules_from_file(md_file):
                if rule.get("rule_id"):
                    declared.add(rule["rule_id"])

        violations: list[dict] = []
        for node in graph_nodes:
            node_id = node["id"]
            # Change B, generalized in cycle 7: a node whose label the markdown
            # ingest cannot author has no markdown home by design (Abstraction
            # nodes are materialized from bible/abstractions.json, never
            # authored into bible/ source). They are not parity drift; exempt
            # them before any markdown comparison.
            if node.get("type") in ORACLE_BLIND_LABELS:
                continue
            if node_id in declared:
                continue
            # 6.4: a graph-first node (provenance proposed | graduation_pending) has
            # NO markdown home BY DESIGN -- transient, pre-promotion. Exempt it so it is
            # not flagged as drift. A graduated | hand-authored node, by contrast, MUST
            # have a source home: if it is absent here, graduation failed to close the
            # export loop (or a source node was deleted) -- that IS the violation to flag.
            if node.get("provenance") in PARITY_EXEMPT_PROVENANCE:
                continue
            # Fall back to a literal text scan: a node id mentioned anywhere in
            # a markdown file (e.g. a `rule_id: X` line outside a parsed block)
            # is present in the bible even when the structural parse misses it.
            if any(node_id in text for text in markdown_text):
                continue
            violations.append({"type": node["type"], "id": node_id})
        return violations

    async def detect_edge_parity(
        self, bible_dir: Path | None = None, project: str = "writ"
    ) -> dict | None:
        """Edge analog of detect_parity_violations (0.10-E).

        Live edges must match the reconcile oracle (compute_expected_graph),
        EXCEPT edges incident to a graph-authored node (intentionally graph-only).
        A `stale` edge is live but not in the oracle (the upsert-only no-prune
        drift, e.g. a renamed source edge); a `missing` edge is in the oracle but
        not live (the source declares it but the graph lacks it). Either fails
        validate -- run `writ reconcile` to restore parity. Returns None when in
        parity, else {"stale": [...], "missing": [...]}.
        """
        if bible_dir is None:
            bible_dir = self._default_bible_dir
        if bible_dir is None or self._driver is None:
            return None
        _expected_nodes, expected_edges = compute_expected_graph(Path(bible_dir))
        live = await read_live_edges(self._driver, self._database, project)
        live_edges = {(t, s, tg) for (t, s, tg, _sp, _tp) in live}
        # 6.4: an edge incident to a graph-first node (proposed | graduation_pending) is
        # graph-only by design -> exempt. Widens 0.10's source_origin=='graph-authored' bit
        # to the authoritative provenance axis.
        exempt = {
            (t, s, tg)
            for (t, s, tg, sp, tp) in live
            if sp in PARITY_EXEMPT_PROVENANCE or tp in PARITY_EXEMPT_PROVENANCE
        }
        # Cycle 7: an edge incident to a node the markdown oracle cannot author is
        # not drift either, it is outside the oracle's field of view. Without this,
        # 186 correct ABSTRACTS edges read as stale and the printed remedy
        # (writ reconcile) deletes them. Abstraction integrity is checked instead by
        # detect_artifact_abstracts_parity, which holds the artifact they come from.
        blind_ids = await read_oracle_blind_node_ids(
            self._driver, self._database, project
        )
        exempt |= {
            (t, s, tg)
            for (t, s, tg, _sp, _tp) in live
            if s in blind_ids or tg in blind_ids
        }
        stale = sorted((live_edges - expected_edges) - exempt)
        missing = sorted(expected_edges - live_edges)
        if not stale and not missing:
            return None
        return {"stale": stale, "missing": missing}

    async def detect_prop_parity(
        self, bible_dir: Path | None = None, project: str = "writ"
    ) -> dict | None:
        """Property analog of detect_edge_parity (0.10 prop completion).

        A live node carrying a MANAGED prop ABSENT from its source frontmatter is
        stale -- the upsert-only no-prune drift at the property level (e.g. an
        action_triggers left behind after a frontmatter edit; the 1.8b META-AUTH
        class). Only MANAGED_PROP_NAMES are checked, so runtime/observation props
        (times_seen_*, last_seen, source_origin) and any non-model prop are never
        flagged; graph-authored nodes are exempt; a node unknown to the oracle is
        skipped. `writ reconcile` clears the drift. Returns None in parity, else
        {node_id: [stale_prop, ...]}.
        """
        if bible_dir is None:
            bible_dir = self._default_bible_dir
        if bible_dir is None or self._driver is None:
            return None
        expected = expected_managed_props(Path(bible_dir))
        rows = await read_live_nodes_with_keys(self._driver, self._database, project)
        violations: dict[str, list[str]] = {}
        for node_id, keys, provenance in rows:
            # 6.4: graph-first nodes (proposed | graduation_pending) are field-parity
            # exempt (their props have no source to drift from); a node unknown to the
            # oracle is skipped here (node-presence is detect_parity_violations' job).
            if provenance in PARITY_EXEMPT_PROVENANCE or node_id not in expected:
                continue
            stale = sorted(
                {k for k in (keys or []) if k in MANAGED_PROP_NAMES} - expected[node_id]
            )
            if stale:
                violations[node_id] = stale
        return violations or None

    async def detect_methodology_field_drift(
        self, bible_dir: Path | None = None, project: str = "writ"
    ) -> dict | None:
        """Bidirectional field-level parity for methodology nodes (5.2a keystone).

        detect_prop_parity is one-directional: it flags MANAGED props the graph holds
        but the source no longer declares (stale, reconcile-clearable). It never
        compares VALUES, so it cannot see (i) VALUE-drift (graph severity='low', source
        severity='high') nor (ii) MISSING props (source declares a managed field the
        graph lacks). Neither is reconcile-clearable -- both require re-authoring -- so
        they get their own finding key.

        Reuses parse_source (the same oracle reconcile + expected_managed_props use) so
        the comparison basis cannot diverge from ingest, and _coerce_neo4j_value so the
        source value is coerced exactly as the writer stored it (dates -> ISO strings,
        dict/list-of-dict -> JSON strings). Provenance-aware: graph-first nodes
        (proposed | graduation_pending) are exempt, and ids unknown to the source oracle
        are skipped (node-presence is detect_parity_violations' job). Only fields PRESENT
        in source are checked -- a graph-only managed field is detect_prop_parity's stale
        case, not re-reported here. Scoped to methodology node types (Rule is covered by
        the export-diff path). Returns None in parity, else
        {node_id: {field: {"graph": <gval>, "source": <sval>}, ...}, ...}.
        """
        if bible_dir is None:
            bible_dir = self._default_bible_dir
        if bible_dir is None or self._driver is None:
            return None
        source_props = self._load_methodology_source_props(bible_dir)
        if not source_props:
            return None
        rows = await self._fetch_graph_nodes(project)
        drift = self._diff_node_props(rows, source_props)
        return drift or None

    @staticmethod
    def _load_methodology_source_props(bible_dir: Path) -> dict[str, dict]:
        """Parse the bible and map each methodology node id -> its parsed source dict (skips
        Rule, covered by the export-diff path). Mirrors expected_managed_props' inclusion +
        id-resolution logic so the comparison basis cannot diverge from ingest."""
        methodology_types = {nt for nt in NODE_ID_FIELDS if nt != "Rule"}
        parsed_nodes, _edges = parse_source(Path(bible_dir))
        source_props: dict[str, dict] = {}
        for node in parsed_nodes:
            nt = node.get("node_type", "Rule")
            if nt not in methodology_types:
                continue
            id_field = NODE_ID_FIELDS.get(nt)
            if not id_field or id_field not in node:
                continue
            source_props[node[id_field]] = node
        return source_props

    async def _fetch_graph_nodes(self, project: str) -> list:
        """Fetch (id, properties, provenance) for every node scoped to `project`."""
        nid = _GRAPH_ID_COALESCE.format(v="n")
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                f"MATCH (n) WHERE n.project = $project "
                f"RETURN {nid} AS id, properties(n) AS props, n.provenance AS provenance",
                project=project,
            )
            return [
                (r["id"], r["props"], r["provenance"])
                async for r in result
                if r["id"] is not None
            ]

    @staticmethod
    def _diff_node_props(rows: list, source_props: dict[str, dict]) -> dict:
        """Value-level diff of MANAGED_PROP_NAMES per node. Returns {node_id: {field:
        {"graph": gval, "source": sval}}} (possibly empty; the caller maps {} -> None)."""
        _MISSING = object()
        drift: dict[str, dict[str, dict]] = {}
        for node_id, graph_props, provenance in rows:
            # 6.4: graph-first nodes (proposed | graduation_pending) have no source home
            # to drift from -> exempt. A node unknown to the oracle is skipped (node-presence
            # is detect_parity_violations' job).
            if provenance in PARITY_EXEMPT_PROVENANCE or node_id not in source_props:
                continue
            src_node = source_props[node_id]
            node_drift: dict[str, dict] = {}
            for field in MANAGED_PROP_NAMES:
                # Only fields PRESENT in source are value-checked here; a graph-only
                # managed field is detect_prop_parity's stale case (do not double-report).
                if field not in src_node:
                    continue
                sval = _coerce_neo4j_value(src_node[field])
                gval = (graph_props or {}).get(field, _MISSING)
                # The writer drops None-valued props (`SET n += $props` removes a key set
                # to null in Neo4j), so a source field that coerces to None AND is absent
                # from the graph is NOT drift. But source=null with a non-null graph value
                # IS value drift -- a graph edit not reflected in source -- so only the
                # source-null/graph-absent quadrant is skipped.
                if sval is None and gval is _MISSING:
                    continue
                if gval is _MISSING:
                    node_drift[field] = {"graph": None, "source": sval}
                elif gval != sval:
                    node_drift[field] = {"graph": gval, "source": sval}
            if node_drift:
                drift[node_id] = node_drift
        return drift
