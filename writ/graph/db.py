"""Neo4j connection layer -- bolt protocol, connection pool, async session management.

Per PY-ASYNC-001: all Neo4j operations use AsyncSession (neo4j.AsyncGraphDatabase).
Sync drivers must never be used in async call chains.

Per PERF-IO-001: no sync I/O in the hot path (anything reachable from FastAPI endpoints).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from neo4j import AsyncGraphDatabase

if TYPE_CHECKING:
    from neo4j import AsyncDriver


# --- Phase 1 additions: methodology node labels + expanded edge allowlist ---
# Per plan Section 3.1 (15 edge types total: 6 existing + 8 new per schema proposal).

METHODOLOGY_NODE_LABELS: frozenset[str] = frozenset({
    "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
    "Phase", "Rationalization", "PressureScenario", "WorkedExample", "SubagentRole",
})

METHODOLOGY_NODE_ID_FIELDS: dict[str, str] = {
    "Skill": "skill_id",
    "Playbook": "playbook_id",
    "Technique": "technique_id",
    "AntiPattern": "antipattern_id",
    "ForbiddenResponse": "forbidden_id",
    "Phase": "phase_id",
    "Rationalization": "rationalization_id",
    "PressureScenario": "scenario_id",
    "WorkedExample": "example_id",
    "SubagentRole": "role_id",
}

ALLOWED_EDGE_TYPES: frozenset[str] = frozenset({
    # Pre-existing
    "DEPENDS_ON", "PRECEDES", "CONFLICTS_WITH", "SUPPLEMENTS",
    "SUPERSEDES", "RELATED_TO", "APPLIES_TO", "ABSTRACTS", "JUSTIFIED_BY",
    # Phase 1 additions per plan Section 3.1
    "TEACHES", "COUNTERS", "DEMONSTRATES", "DISPATCHES",
    "GATES", "PRESSURE_TESTS", "CONTAINS", "ATTACHED_TO",
})


def _coerce_neo4j_value(v):
    """Convert Python objects to Neo4j-compatible property values.

    Dates → ISO strings. Nested dicts → JSON strings (Neo4j doesn't store maps
    as node properties). Lists of primitives pass through.
    """
    import json
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, dict):
        return json.dumps(v)
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return json.dumps(v)
    return v


class GraphConnection(Protocol):
    """Connection interface for graph database operations.

    Per PY-PROTO-001: Protocol over ABC for pure interfaces.
    """

    async def get_rule(self, rule_id: str) -> dict | None: ...
    async def create_rule(self, rule_data: dict) -> str: ...
    async def create_edge(self, edge_type: str, source_id: str, target_id: str) -> None: ...
    async def traverse_neighbors(self, rule_id: str, hops: int) -> list[dict]: ...
    async def close(self) -> None: ...


class Neo4jConnection:
    """Neo4j implementation of GraphConnection.

    Uses MERGE for idempotent writes (preparation for Phase 3 migration).
    Per ARCH-DI-001: receives connection config via constructor.
    """

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    async def get_rule(self, rule_id: str) -> dict | None:
        """Fetch a single rule node by rule_id. Returns None if not found."""
        query = "MATCH (r:Rule {rule_id: $rule_id}) RETURN r"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            record = await result.single()
            if record is None:
                return None
            return dict(record["r"])

    async def create_rule(self, rule_data: dict) -> str:
        """Create or update a Rule node. Idempotent via MERGE on rule_id.

        Phase 1 Rule carries rationalization_counters (list[dict]) and nested
        structures that Neo4j can't store natively; _coerce_neo4j_value serializes
        those to JSON strings.
        """
        query = """
            MERGE (r:Rule {rule_id: $rule_id})
            SET r += $props
            RETURN r.rule_id AS rule_id
        """
        props = {k: _coerce_neo4j_value(v) for k, v in rule_data.items() if k != "rule_id"}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query, rule_id=rule_data["rule_id"], props=props
            )
            record = await result.single()
            return record["rule_id"]

    async def create_edge(self, edge_type: str, source_id: str, target_id: str) -> None:
        """Create a typed edge between two nodes. Idempotent via MERGE.

        Source/target nodes are matched by their `<type>_id` property; any node
        label with the expected property key is eligible (Rule, Skill, Playbook,
        etc.). This lets Phase 1 methodology edges link across node labels.
        """
        if edge_type not in ALLOWED_EDGE_TYPES:
            raise ValueError(f"Unknown edge type: {edge_type}")
        # Cypher does not support parameterized relationship types, but edge_type
        # is validated against the allowed set above. Match any labeled node that
        # carries a primary-id property matching source_id / target_id.
        query = f"""
            MATCH (a) WHERE a.rule_id = $source_id
                OR a.skill_id = $source_id OR a.playbook_id = $source_id
                OR a.technique_id = $source_id OR a.antipattern_id = $source_id
                OR a.forbidden_id = $source_id OR a.phase_id = $source_id
                OR a.rationalization_id = $source_id OR a.scenario_id = $source_id
                OR a.example_id = $source_id OR a.role_id = $source_id
            MATCH (b) WHERE b.rule_id = $target_id
                OR b.skill_id = $target_id OR b.playbook_id = $target_id
                OR b.technique_id = $target_id OR b.antipattern_id = $target_id
                OR b.forbidden_id = $target_id OR b.phase_id = $target_id
                OR b.rationalization_id = $target_id OR b.scenario_id = $target_id
                OR b.example_id = $target_id OR b.role_id = $target_id
            MERGE (a)-[:{edge_type}]->(b)
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(query, source_id=source_id, target_id=target_id)

    async def create_methodology_node(self, node_type: str, data: dict) -> str:
        """Create or update a methodology node (non-Rule type). Idempotent via MERGE.

        node_type is the Pydantic class name (e.g. 'Skill'), which becomes the
        Neo4j label. data must contain the type-specific primary id field.
        """
        if node_type not in METHODOLOGY_NODE_LABELS:
            raise ValueError(f"Unknown methodology node_type: {node_type}")
        id_field = METHODOLOGY_NODE_ID_FIELDS[node_type]
        if id_field not in data:
            raise ValueError(f"{node_type} data missing required {id_field}")
        node_id = data[id_field]
        # Drop id_field from props since it's matched on MERGE.
        props = {k: _coerce_neo4j_value(v) for k, v in data.items() if k != id_field}
        query = f"""
            MERGE (n:{node_type} {{{id_field}: $node_id}})
            SET n += $props
            RETURN n.{id_field} AS id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, node_id=node_id, props=props)
            record = await result.single()
            return record["id"]

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
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            records = [record.data() async for record in result]
            return records

    async def count_rules(self) -> int:
        """Return total Rule node count."""
        query = "MATCH (r:Rule) RETURN count(r) AS count"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            record = await result.single()
            return record["count"]

    async def get_all_rules(self) -> list[dict]:
        """Fetch all Rule nodes. Returns list of property dicts."""
        query = "MATCH (r:Rule) RETURN r ORDER BY r.rule_id"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return [dict(record["r"]) async for record in result]

    async def get_all_edges(self) -> list[dict]:
        """Fetch all edges between Rule nodes.

        Returns list of dicts with from_id, to_id, edge_type.
        """
        query = """
            MATCH (a:Rule)-[rel]->(b:Rule)
            RETURN a.rule_id AS from_id, b.rule_id AS to_id, type(rel) AS edge_type
            ORDER BY a.rule_id, b.rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return [record.data() async for record in result]

    async def create_abstraction(self, data: dict) -> str:
        """Create or update an Abstraction node. Idempotent via MERGE."""
        query = """
            MERGE (a:Abstraction {abstraction_id: $abstraction_id})
            SET a += $props
            RETURN a.abstraction_id AS abstraction_id
        """
        props = {k: v for k, v in data.items() if k != "abstraction_id"}
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query, abstraction_id=data["abstraction_id"], props=props
            )
            record = await result.single()
            return record["abstraction_id"]

    async def create_abstracts_edge(self, abstraction_id: str, rule_id: str) -> None:
        """Create ABSTRACTS edge from Abstraction to Rule. Idempotent via MERGE."""
        query = """
            MATCH (a:Abstraction {abstraction_id: $abstraction_id})
            MATCH (r:Rule {rule_id: $rule_id})
            MERGE (a)-[:ABSTRACTS]->(r)
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(query, abstraction_id=abstraction_id, rule_id=rule_id)

    async def get_all_abstractions(self) -> list[dict]:
        """Fetch all Abstraction nodes with member rule_ids."""
        query = """
            MATCH (a:Abstraction)
            OPTIONAL MATCH (a)-[:ABSTRACTS]->(r:Rule)
            RETURN a, collect(r.rule_id) AS member_ids
            ORDER BY a.abstraction_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            abstractions = []
            async for record in result:
                data = dict(record["a"])
                data["member_ids"] = record["member_ids"]
                abstractions.append(data)
            return abstractions

    async def get_abstraction(self, abstraction_id: str) -> dict | None:
        """Fetch a single Abstraction with member rule details."""
        query = """
            MATCH (a:Abstraction {abstraction_id: $abstraction_id})
            OPTIONAL MATCH (a)-[:ABSTRACTS]->(r:Rule)
            RETURN a, collect(r {.*}) AS members
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, abstraction_id=abstraction_id)
            record = await result.single()
            if record is None:
                return None
            data = dict(record["a"])
            data["members"] = [dict(m) for m in record["members"]]
            return data

    async def delete_abstractions(self) -> int:
        """Delete all Abstraction nodes and their ABSTRACTS edges. Rules unaffected."""
        query = "MATCH (a:Abstraction) DETACH DELETE a RETURN count(a) AS deleted"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            record = await result.single()
            return record["deleted"]

    async def get_rule_abstraction(self, rule_id: str) -> dict | None:
        """Return abstraction membership for a rule: abstraction_id + sibling rule_ids.

        Returns None if the rule is not a member of any abstraction.
        """
        query = """
            MATCH (a:Abstraction)-[:ABSTRACTS]->(r:Rule {rule_id: $rule_id})
            OPTIONAL MATCH (a)-[:ABSTRACTS]->(sibling:Rule)
            WHERE sibling.rule_id <> $rule_id
            RETURN a.abstraction_id AS abstraction_id,
                   collect(sibling.rule_id) AS sibling_rule_ids
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            record = await result.single()
            if record is None or record["abstraction_id"] is None:
                return None
            return {
                "abstraction_id": record["abstraction_id"],
                "sibling_rule_ids": sorted(record["sibling_rule_ids"]),
            }

    async def apply_constraints(self) -> None:
        """Apply uniqueness constraint and performance indexes. Idempotent via IF NOT EXISTS.

        Includes Phase 1 constraints for all 10 new methodology labels.
        """
        statements = [
            "CREATE CONSTRAINT rule_id_unique IF NOT EXISTS FOR (r:Rule) REQUIRE r.rule_id IS UNIQUE",
            "CREATE INDEX rule_domain IF NOT EXISTS FOR (r:Rule) ON (r.domain)",
            "CREATE INDEX rule_mandatory IF NOT EXISTS FOR (r:Rule) ON (r.mandatory)",
        ]
        # Phase 1 uniqueness constraints per methodology label. Each label has its
        # own *_id primary key per docs/phase-0-schema-proposal.md decision 2.
        for label, id_field in METHODOLOGY_NODE_ID_FIELDS.items():
            constraint_name = f"{label.lower()}_{id_field}_unique"
            statements.append(
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{id_field} IS UNIQUE"
            )
            statements.append(
                f"CREATE INDEX {label.lower()}_domain IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.domain)"
            )
        async with self._driver.session(database=self._database) as session:
            for stmt in statements:
                await session.run(stmt)

    async def list_constraints(self) -> list[dict]:
        """Return all constraints. For verification/testing."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run("SHOW CONSTRAINTS")
            return [record.data() async for record in result]

    async def list_indexes(self) -> list[dict]:
        """Return all indexes. For verification/testing."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run("SHOW INDEXES")
            return [record.data() async for record in result]

    async def get_rules_by_authority(self, authority: str) -> list[dict]:
        """Fetch all Rule nodes with a given authority value."""
        query = """
            MATCH (r:Rule)
            WHERE r.authority = $authority
            RETURN r
            ORDER BY r.last_validated DESC
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, authority=authority)
            return [dict(record["r"]) async for record in result]

    async def update_rule_authority(self, rule_id: str, authority: str) -> bool:
        """Update the authority property on a Rule node. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.authority = $authority
            RETURN r.rule_id AS rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id, authority=authority)
            record = await result.single()
            return record is not None

    async def update_rule_confidence(self, rule_id: str, confidence: str) -> bool:
        """Update the confidence property on a Rule node. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.confidence = $confidence
            RETURN r.rule_id AS rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id, confidence=confidence)
            record = await result.single()
            return record is not None

    async def increment_positive(self, rule_id: str) -> bool:
        """Increment times_seen_positive and update last_seen. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.times_seen_positive = coalesce(r.times_seen_positive, 0) + 1,
                r.last_seen = datetime()
            RETURN r.rule_id AS rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            record = await result.single()
            return record is not None

    async def increment_negative(self, rule_id: str) -> bool:
        """Increment times_seen_negative and update last_seen. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            SET r.times_seen_negative = coalesce(r.times_seen_negative, 0) + 1,
                r.last_seen = datetime()
            RETURN r.rule_id AS rule_id
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            record = await result.single()
            return record is not None

    async def delete_rule(self, rule_id: str) -> bool:
        """Delete a Rule node and all its edges. Returns True if found."""
        query = """
            MATCH (r:Rule {rule_id: $rule_id})
            DETACH DELETE r
            RETURN count(r) AS deleted
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, rule_id=rule_id)
            record = await result.single()
            return record["deleted"] > 0

    async def count_by_authority(self) -> dict[str, int]:
        """Count rules grouped by authority value."""
        query = """
            MATCH (r:Rule)
            RETURN coalesce(r.authority, 'human') AS authority, count(r) AS count
            ORDER BY authority
        """
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            return {record["authority"]: record["count"] async for record in result}

    async def clear_all(self) -> None:
        """Delete all nodes and edges. For test cleanup only."""
        async with self._driver.session(database=self._database) as session:
            await session.run("MATCH (n) DETACH DELETE n")

    async def close(self) -> None:
        """Close the driver connection pool."""
        await self._driver.close()
