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
        """Create or update a Rule node. Idempotent via MERGE on rule_id."""
        query = """
            MERGE (r:Rule {rule_id: $rule_id})
            SET r += $props
            RETURN r.rule_id AS rule_id
        """
        props = {k: v for k, v in rule_data.items() if k != "rule_id"}
        # Convert date objects to ISO strings for Neo4j storage.
        for key, val in props.items():
            if hasattr(val, "isoformat"):
                props[key] = val.isoformat()
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                query, rule_id=rule_data["rule_id"], props=props
            )
            record = await result.single()
            return record["rule_id"]

    async def create_edge(self, edge_type: str, source_id: str, target_id: str) -> None:
        """Create a typed edge between two Rule nodes. Idempotent via MERGE."""
        # Per ARCH-CONST-001: edge types are validated against allowed set.
        allowed = {
            "DEPENDS_ON", "PRECEDES", "CONFLICTS_WITH", "SUPPLEMENTS",
            "SUPERSEDES", "RELATED_TO", "APPLIES_TO", "ABSTRACTS", "JUSTIFIED_BY",
        }
        if edge_type not in allowed:
            raise ValueError(f"Unknown edge type: {edge_type}")
        # Cypher does not support parameterized relationship types,
        # but edge_type is validated against the allowed set above.
        query = f"""
            MATCH (a:Rule {{rule_id: $source_id}})
            MATCH (b:Rule {{rule_id: $target_id}})
            MERGE (a)-[:{edge_type}]->(b)
        """
        async with self._driver.session(database=self._database) as session:
            await session.run(query, source_id=source_id, target_id=target_id)

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
        """Apply uniqueness constraint and performance indexes. Idempotent via IF NOT EXISTS."""
        statements = [
            "CREATE CONSTRAINT rule_id_unique IF NOT EXISTS FOR (r:Rule) REQUIRE r.rule_id IS UNIQUE",
            "CREATE INDEX rule_domain IF NOT EXISTS FOR (r:Rule) ON (r.domain)",
            "CREATE INDEX rule_mandatory IF NOT EXISTS FOR (r:Rule) ON (r.mandatory)",
        ]
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
