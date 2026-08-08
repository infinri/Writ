"""T0.A db RED gate -- Category db operations.

All tests here must FAIL until writ/graph/db.py gains:
  - create_methodology_node("Category", data) -> str  (idempotent MERGE on category_id)
  - BELONGS_TO added to ALLOWED_EDGE_TYPES
  - create_edge MATCH widened to match category_id
  - get_all_nodes(label=None) -> list[dict]  (optional label filter)
  - apply_constraints adds Category uniqueness constraint

Each test uses an isolated Neo4j fixture that clears the graph before and after.
When Neo4j is unreachable the entire module skips (autouse module-scoped guard),
matching the pattern used in test_graph_integrity_all_types.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

NEO4J_URI = get_neo4j_uri()
NEO4J_USER = get_neo4j_user()
NEO4J_PASSWORD = get_neo4j_password()


# --- Module-level reachability guard (sync, no running loop) -----------------

@pytest.fixture(scope="module", autouse=True)
def _require_neo4j():
    """Skip the entire module when Neo4j is not running.

    Calls neo4j_reachable() synchronously at module scope (no event loop yet),
    matching the guard pattern used in test_graph_integrity_all_types.py.
    """
    from tests._corpus import neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")


# --- Helpers -----------------------------------------------------------------

def _make_rule(rule_id: str) -> dict:
    from datetime import date

    return {
        "rule_id": rule_id,
        "domain": "Test",
        "severity": "medium",
        "scope": "file",
        "trigger": "Trigger text",
        "statement": "Statement text",
        "violation": "Violation text",
        "pass_example": "Pass example",
        "enforcement": "Enforcement text",
        "rationale": "Rationale text",
        "mandatory": False,
        "confidence": "production-validated",
        "evidence": "doc:original-bible",
        "staleness_window": 365,
        "last_validated": date.today().isoformat(),
    }


def _make_category(category_id: str = "CAT-CODING-001", routes: list[str] | None = None) -> dict:
    return {
        "category_id": category_id,
        "name": "Coding",
        "routes": routes if routes is not None else ["semantic"],
        "parent": None,
        "description": "",
    }


# --- Per-test isolated db fixture -------------------------------------------

@pytest_asyncio.fixture()
async def db(disposable_graph):
    """Isolated Neo4j connection; graph is cleared before and after each test.

    Explicit full wipe: clear_all preserves runtime records by default, and this
    module asserts on total node counts (including "empty graph returns []"), so
    preserved memories would leak into that arithmetic.

    Because that wipe is unrecoverable for runtime records, the `disposable_graph`
    fixture skips these tests unless a separate, explicitly-marked Neo4j instance is
    configured (see tests/conftest.py). This fixture is why the suite used to leave
    the live graph holding 2 Memory nodes against 98 on-disk memory files.
    """
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all(preserve_labels=frozenset())
    yield conn
    await conn.clear_all(preserve_labels=frozenset())
    await conn.close()


# --- Tests -------------------------------------------------------------------

class TestBelongsToEdge:
    """BELONGS_TO edge can be created between a Rule and a Category node."""

    @pytest.mark.asyncio
    async def test_belongs_to_edge_created(self, db: Neo4jConnection) -> None:
        """After creating a Rule and a Category, a BELONGS_TO edge can be created.

        RED today because BELONGS_TO is not in ALLOWED_EDGE_TYPES; create_edge
        raises ValueError on the first call. GREEN after BELONGS_TO is added to
        ALLOWED_EDGE_TYPES and create_edge MATCH is widened to category_id.
        """
        await db.create_rule(_make_rule("ARCH-ORG-001"))
        await db.create_methodology_node("Category", _make_category("CAT-CODING-001"))

        await db.create_edge("BELONGS_TO", "ARCH-ORG-001", "CAT-CODING-001")

        # Verify exactly 1 BELONGS_TO edge exists.
        async with db._driver.session(database=db._database) as session:
            result = await session.run(
                "MATCH (a)-[r:BELONGS_TO]->(b) RETURN count(r) AS n"
            )
            record = await result.single()
        assert record["n"] == 1

    def test_belongs_to_not_yet_in_allowed_edge_types(self) -> None:
        """Guard: BELONGS_TO is absent from ALLOWED_EDGE_TYPES today.

        This test is RED today (assertion fails). It turns GREEN when the
        implementation adds BELONGS_TO to the allowlist.
        """
        from writ.graph.db import ALLOWED_EDGE_TYPES

        assert "BELONGS_TO" in ALLOWED_EDGE_TYPES, (
            "BELONGS_TO is not yet in ALLOWED_EDGE_TYPES -- add it in T0.A"
        )


class TestGetAllNodes:
    """get_all_nodes returns nodes across all labels with optional label filter."""

    @pytest.mark.asyncio
    async def test_get_all_nodes_returns_all_labels(self, db: Neo4jConnection) -> None:
        """After inserting a Rule, a Skill, and a Category, get_all_nodes returns
        at least one node per label."""
        await db.create_rule(_make_rule("ARCH-ORG-001"))
        await db.create_methodology_node(
            "Skill",
            {
                "skill_id": "SKL-PROC-BRAIN-001",
                "name": "test skill",
                "summary": "A test skill",
                "domain": "Process",
            },
        )
        await db.create_methodology_node("Category", _make_category("CAT-CODING-001"))

        nodes = await db.get_all_nodes()

        labels_present = {n["label"] for n in nodes}
        assert "Rule" in labels_present, "Rule nodes must appear in get_all_nodes"
        assert "Skill" in labels_present, "Skill nodes must appear in get_all_nodes"
        assert "Category" in labels_present, "Category nodes must appear in get_all_nodes"

    @pytest.mark.asyncio
    async def test_get_all_nodes_single_label_filter(self, db: Neo4jConnection) -> None:
        """get_all_nodes(label='Category') returns only Category nodes."""
        await db.create_rule(_make_rule("ARCH-ORG-001"))
        await db.create_methodology_node("Category", _make_category("CAT-CODING-001"))
        await db.create_methodology_node("Category", _make_category("CAT-PROC-001", routes=["state"]))

        nodes = await db.get_all_nodes(label="Category")

        assert len(nodes) == 2
        # Every returned node must carry an identifiable Category marker.
        for node in nodes:
            assert node.get("label") == "Category" or "category_id" in node, (
                f"Expected a Category node, got: {node}"
            )

    @pytest.mark.asyncio
    async def test_get_all_nodes_empty_graph_returns_empty_list(self, db: Neo4jConnection) -> None:
        """get_all_nodes on a cleared graph returns an empty list (no error)."""
        nodes = await db.get_all_nodes()
        assert isinstance(nodes, list)
        assert len(nodes) == 0


class TestCategoryConstraint:
    """apply_constraints must include a uniqueness constraint on category_id."""

    @pytest.mark.asyncio
    async def test_apply_constraints_includes_category(self, db: Neo4jConnection) -> None:
        """After apply_constraints, list_constraints includes a constraint covering
        Category.category_id uniqueness."""
        await db.apply_constraints()

        constraints = await db.list_constraints()
        category_constraints = [
            c for c in constraints
            if "category" in str(c).lower()
        ]
        assert category_constraints, (
            "apply_constraints must create a uniqueness constraint for "
            f"Category.category_id; found constraints: "
            f"{[c.get('name', c) for c in constraints]}"
        )

    @pytest.mark.asyncio
    async def test_category_constraint_enforces_uniqueness(self, db: Neo4jConnection) -> None:
        """The Category uniqueness constraint makes MERGE idempotent: two calls with
        the same category_id result in exactly 1 node."""
        await db.apply_constraints()
        await db.create_methodology_node("Category", _make_category("CAT-CODING-001"))
        await db.create_methodology_node("Category", _make_category("CAT-CODING-001"))

        async with db._driver.session(database=db._database) as session:
            result = await session.run(
                "MATCH (c:Category {category_id: $cid}) RETURN count(c) AS n",
                cid="CAT-CODING-001",
            )
            record = await result.single()
        assert record["n"] == 1
