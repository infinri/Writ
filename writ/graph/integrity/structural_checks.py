"""Structural/original checks (conflicts, orphans, staleness, redundancy).

Moved verbatim from the former writ/graph/integrity.py (Wave 2 mixin split); methods read self._driver / self._database set by IntegrityChecker.__init__."""
from __future__ import annotations

from writ.graph.integrity._common import (
    NODE_ID_FIELDS,
    ORACLE_BLIND_LABELS,
    REDUNDANCY_SIMILARITY_THRESHOLD,
    date,
    timedelta,
)


class StructuralChecksMixin:
    async def detect_conflicts(self) -> list[dict]:
        """Find all rule pairs connected by CONFLICTS_WITH edges."""
        query = """
            MATCH (a:Rule)-[:CONFLICTS_WITH]->(b:Rule)
            WHERE a.rule_id < b.rule_id
            RETURN a.rule_id AS rule_a, b.rule_id AS rule_b
            ORDER BY rule_a
        """
        return [record.data() for record in await self._run(query)]

    async def detect_orphans(self) -> list[str]:
        """Find rules with zero edges (unreachable by traversal)."""
        query = """
            MATCH (r:Rule)
            WHERE NOT (r)--()
            RETURN r.rule_id AS rule_id
            ORDER BY rule_id
        """
        return [record["rule_id"] for record in await self._run(query)]

    async def detect_orphans_all_labels(self) -> tuple[list[dict], dict[str, int]]:
        """Find zero-edge nodes across ALL node types, not just Rule.

        The built-in detect_orphans() only audited :Rule, so a disconnected
        methodology node (Playbook, Skill, SubagentRole, ...) read as fine. This
        scans every label in the trusted NODE_ID_FIELDS map and returns the orphan
        list plus a per-type count. Label and id-field are interpolated from that
        constant (never user input), so the f-string carries no injection risk.
        """
        orphans: list[dict] = []
        counts: dict[str, int] = {}
        async with self._driver.session(database=self._database) as session:
            for label, id_field in NODE_ID_FIELDS.items():
                # Change B, generalized in cycle 7: a label the markdown ingest
                # cannot author has no required edges in the source graph, so it
                # is not an orphan by the source-parity rule.
                if label in ORACLE_BLIND_LABELS:
                    counts[label] = 0
                    continue
                query = (
                    f"MATCH (n:{label}) WHERE NOT (n)--() "
                    f"RETURN n.{id_field} AS id ORDER BY id"
                )
                result = await session.run(query)
                ids = [record["id"] async for record in result]
                counts[label] = len(ids)
                orphans.extend({"type": label, "id": node_id} for node_id in ids)
        return orphans, counts

    async def detect_dangling_dispatched_roles(self) -> list[dict]:
        """Flag Playbook.dispatched_roles entries that are not canonical role ids.

        A Playbook dispatches SubagentRoles via its dispatched_roles property. An
        entry that is a real SubagentRole.role_id (ROL-*) is fine; an entry that
        only matches a role's short name (e.g. 'writ-explorer') is a dangling ref
        -- the node exists under its id (ROL-EXPLORER-001) but is referenced by a
        name that is not an id, so no DISPATCHES edge gets created. Such an entry
        is 'resolvable' (we can name the intended target id) but still a defect.
        """
        async with self._driver.session(database=self._database) as session:
            pb_result = await session.run(
                "MATCH (p:Playbook) WHERE p.dispatched_roles IS NOT NULL "
                "RETURN p.playbook_id AS id, p.dispatched_roles AS roles ORDER BY id"
            )
            playbooks = [record.data() async for record in pb_result]
            role_result = await session.run(
                "MATCH (r:SubagentRole) RETURN r.role_id AS role_id, r.name AS name"
            )
            roles = [record.data() async for record in role_result]

        by_id = {r["role_id"] for r in roles}
        by_name = {r["name"]: r["role_id"] for r in roles if r["name"]}
        dangling: list[dict] = []
        for playbook in playbooks:
            for ref in playbook.get("roles") or []:
                if ref in by_id:
                    continue  # canonical reference -- a DISPATCHES edge can resolve it
                dangling.append(
                    {
                        "from": playbook["id"],
                        "field": "dispatched_roles",
                        "ref": ref,
                        "resolvable": ref in by_name,
                        "resolved_target_id": by_name.get(ref),
                    }
                )
        return dangling

    async def detect_stale(self) -> list[dict]:
        """Find nodes past their staleness window, across ALL node types.

        Previously :Rule-only, so a stale methodology node (Skill, Playbook,
        ...) read as fresh. This scans every label in the trusted
        NODE_ID_FIELDS map (label + id-field interpolated from that constant,
        never user input -> no injection risk). The `rule_id` key is retained
        (= the node id) for backward compatibility; `type` names the label.
        """
        today = date.today()
        stale: list[dict] = []
        async with self._driver.session(database=self._database) as session:
            for label, id_field in NODE_ID_FIELDS.items():
                query = (
                    f"MATCH (n:{label}) "
                    f"WHERE n.last_validated IS NOT NULL "
                    f"  AND n.staleness_window IS NOT NULL "
                    f"RETURN n.{id_field} AS node_id, "
                    f"       n.last_validated AS last_validated, "
                    f"       n.staleness_window AS staleness_window"
                )
                result = await session.run(query)
                async for record in result:
                    data = record.data()
                    last_val = data["last_validated"]
                    window = data["staleness_window"]
                    # Neo4j stores dates as strings from our migration.
                    if isinstance(last_val, str):
                        last_val = date.fromisoformat(last_val)
                    expiry = last_val + timedelta(days=int(window))
                    if expiry < today:
                        stale.append({
                            "type": label,
                            "rule_id": data["node_id"],
                            "last_validated": str(last_val),
                            "expired_on": str(expiry),
                        })
        return stale

    async def detect_redundant(self) -> list[dict]:
        """Find rule pairs with near-identical trigger+statement text.

        Uses embedding cosine similarity. Requires sentence-transformers.
        """
        query = """
            MATCH (r:Rule)
            WHERE r.mandatory IS NULL OR r.mandatory = false
            RETURN r.rule_id AS rule_id,
                   r.trigger AS trigger,
                   r.statement AS statement
        """
        rules = [record.data() for record in await self._run(query)]

        if len(rules) < 2:
            return []

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            # Approach C (Finding D): sentence-transformers moved out
            # of core deps. The redundancy check is opt-in by way of
            # this dependency. Returning [] here would silently degrade
            # into output indistinguishable from "no redundancies
            # found", which is the bug class fixed in commit dae679a
            # for the ONNX fallback. Raise with an actionable message
            # naming the cause, the [fallback] install command, and the
            # skip_redundancy=True opt-out for callers that intentionally
            # want to exclude this check (the integrity benchmark uses
            # this opt-out, for example).
            raise RuntimeError(
                "Redundancy check requires the sentence-transformers "
                f"library, which could not be imported ({type(exc).__name__}: "
                f"{exc}). "
                "Production installs deliberately exclude this library "
                "(see pyproject.toml: it lives in the [fallback] extras "
                "group, not core dependencies). To enable redundancy "
                "detection, run `pip install -e '.[fallback]'` and "
                "re-run the check. To intentionally exclude redundancy "
                "from an integrity scan (e.g., for benchmarking), pass "
                "skip_redundancy=True to run_all_checks()."
            ) from exc

        model = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [f"{r['trigger']} {r['statement']}" for r in rules]
        embeddings = model.encode(texts, normalize_embeddings=True)

        redundant: list[dict] = []
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                similarity = float(embeddings[i] @ embeddings[j])
                if similarity >= REDUNDANCY_SIMILARITY_THRESHOLD:
                    redundant.append({
                        "rule_a": rules[i]["rule_id"],
                        "rule_b": rules[j]["rule_id"],
                        "similarity": round(similarity, 4),
                    })
        return redundant

    async def detect_confidence_defaults(self) -> list[str]:
        """List rules still at migration default confidence."""
        query = """
            MATCH (r:Rule)
            WHERE r.confidence = 'production-validated'
            RETURN r.rule_id AS rule_id
            ORDER BY rule_id
        """
        return [record["rule_id"] for record in await self._run(query)]
