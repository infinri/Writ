"""Phase 6: Authoring workflow tests.

Tests writ add / writ edit logic: field validation, relationship suggestion,
redundancy detection, conflict warning, and graph writes.

Requires Neo4j running with migrated rules.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import discover_rule_files, parse_rules_from_file
from writ.graph.schema import Rule
from writ.retrieval.pipeline import build_pipeline
from writ.retrieval.traversal import AdjacencyCache

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    """Shared db with migrated rules."""
    from pathlib import Path

    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()

    bible_dir = Path("bible/")
    if bible_dir.exists():
        rule_ids = set()
        all_rules: list[dict] = []
        for f in discover_rule_files(bible_dir):
            for rule_data in parse_rules_from_file(f):
                clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                await conn.create_rule(clean)
                rule_ids.add(clean["rule_id"])
                all_rules.append(rule_data)
        for rule_data in all_rules:
            for ref_id in rule_data.get("_cross_references", []):
                if ref_id in rule_ids:
                    await conn.create_edge("RELATED_TO", rule_data["rule_id"], ref_id)

    yield conn
    await conn.clear_all()
    await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pipeline(db):
    p = await build_pipeline(db)
    yield p


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cache(db):
    c = AdjacencyCache()
    await c.build_from_db(db)
    yield c


def _make_new_rule_data() -> dict:
    """Valid rule data for authoring tests."""
    return {
        "rule_id": "TEST-AUTH-001",
        "domain": "Testing",
        "severity": "high",
        "scope": "file",
        "trigger": "When a test depends on execution order of other tests.",
        "statement": "Each test must be independently executable.",
        "violation": "Test B fails when Test A is skipped.",
        "pass_example": "Each test sets up and tears down its own state.",
        "enforcement": "CI runs tests in random order.",
        "rationale": "Order-dependent tests mask real failures and block parallelization.",
        "last_validated": "2026-03-16",
    }


# ---------------------------------------------------------------------------
# Schema validation at authoring boundary (INV-6)
# ---------------------------------------------------------------------------

class TestAuthoringValidation:
    pytestmark = []  # Override module-level asyncio mark for sync tests.

    def test_valid_rule_passes_schema(self) -> None:
        data = _make_new_rule_data()
        rule = Rule(**data)
        assert rule.rule_id == "TEST-AUTH-001"

    def test_missing_required_field_rejected(self) -> None:
        data = _make_new_rule_data()
        del data["trigger"]
        with pytest.raises(Exception):  # Pydantic ValidationError
            Rule(**data)

    def test_invalid_rule_id_format_rejected(self) -> None:
        data = _make_new_rule_data()
        data["rule_id"] = "bad-id"
        with pytest.raises(Exception):
            Rule(**data)

    def test_invalid_severity_rejected(self) -> None:
        data = _make_new_rule_data()
        data["severity"] = "catastrophic"
        with pytest.raises(Exception):
            Rule(**data)

    def test_empty_statement_rejected(self) -> None:
        data = _make_new_rule_data()
        data["statement"] = "   "
        with pytest.raises(Exception):
            Rule(**data)


# ---------------------------------------------------------------------------
# Relationship suggestion
# ---------------------------------------------------------------------------

class TestRelationshipSuggestion:

    async def test_suggest_returns_top5(self, pipeline) -> None:
        from writ.authoring import suggest_relationships

        data = _make_new_rule_data()
        suggestions = suggest_relationships(data, pipeline)
        assert len(suggestions) <= 5
        assert len(suggestions) > 0
        for s in suggestions:
            assert "rule_id" in s
            assert "score" in s

    async def test_suggest_excludes_self(self, pipeline, db) -> None:
        """If the rule already exists in the graph, it is excluded from suggestions."""
        from writ.authoring import suggest_relationships

        # Use a rule that exists in the graph.
        existing = await db.get_rule("ARCH-ORG-001")
        if existing is None:
            pytest.skip("ARCH-ORG-001 not in graph")

        suggestions = suggest_relationships(existing, pipeline)
        suggested_ids = [s["rule_id"] for s in suggestions]
        assert "ARCH-ORG-001" not in suggested_ids


# ---------------------------------------------------------------------------
# Redundancy detection
# ---------------------------------------------------------------------------

class TestRedundancyDetection:
    """Redundancy threshold is 0.95 cosine similarity per INV-5.

    Boundary behavior: >= 0.95 is flagged, < 0.95 is not flagged.
    """

    THRESHOLD = 0.95

    async def test_exact_duplicate_flagged(self, pipeline) -> None:
        """Verbatim copy of an existing rule's text exceeds 0.95 threshold."""
        from writ.authoring import check_redundancy

        existing = list(pipeline._metadata.values())[0]
        duplicate_data = {
            "trigger": existing.get("trigger", ""),
            "statement": existing.get("statement", ""),
        }
        flagged = check_redundancy(duplicate_data, pipeline, threshold=self.THRESHOLD)
        assert len(flagged) > 0
        assert flagged[0]["similarity"] >= self.THRESHOLD

    async def test_novel_rule_below_threshold(self, pipeline) -> None:
        """Unrelated text should score well below 0.95."""
        from writ.authoring import check_redundancy

        novel_data = {
            "trigger": "When deploying quantum entanglement microservices to Mars.",
            "statement": "All quantum state must be serialized before interplanetary transfer.",
        }
        flagged = check_redundancy(novel_data, pipeline, threshold=self.THRESHOLD)
        assert len(flagged) == 0

    async def test_boundary_below_threshold_not_flagged(self, pipeline) -> None:
        """A rule with minor semantic overlap should stay below 0.95."""
        from writ.authoring import check_redundancy

        # Paraphrase an existing rule loosely enough to stay under threshold.
        near_data = {
            "trigger": "When code is organized into layers.",
            "statement": "Layers should have clear boundaries between them.",
        }
        flagged = check_redundancy(near_data, pipeline, threshold=self.THRESHOLD)
        # If this fails, the paraphrase is too close -- adjust the text, not the threshold.
        assert all(f["similarity"] < self.THRESHOLD for f in flagged)


# ---------------------------------------------------------------------------
# Conflict path detection
# ---------------------------------------------------------------------------

class TestConflictDetection:

    async def test_conflict_path_detected(self, db, cache) -> None:
        from writ.authoring import check_conflicts

        # Create two rules with a CONFLICTS_WITH edge for testing.
        rule_a = {
            "rule_id": "TEST-CONF-001",
            "domain": "Testing",
            "severity": "high",
            "scope": "file",
            "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-16",
        }
        rule_b = {**rule_a, "rule_id": "TEST-CONF-002"}
        await db.create_rule(rule_a)
        await db.create_rule(rule_b)
        await db.create_edge("CONFLICTS_WITH", "TEST-CONF-001", "TEST-CONF-002")

        # Rebuild cache to include the new edge.
        await cache.build_from_db(db)

        conflicts = check_conflicts("TEST-CONF-001", cache)
        conflict_ids = [c["rule_id"] for c in conflicts]
        assert "TEST-CONF-002" in conflict_ids

    async def test_no_conflict_on_clean_rule(self, cache) -> None:
        from writ.authoring import check_conflicts

        conflicts = check_conflicts("NONEXISTENT-RULE-999", cache)
        assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# Graph write via writ add
# ---------------------------------------------------------------------------

class TestAuthoringGraphWrite:

    async def test_add_creates_rule_in_neo4j(self, db) -> None:
        data = _make_new_rule_data()
        data["rule_id"] = "TEST-ADD-001"
        await db.create_rule(data)
        fetched = await db.get_rule("TEST-ADD-001")
        assert fetched is not None
        assert fetched["rule_id"] == "TEST-ADD-001"
        assert fetched["domain"] == "Testing"

    async def test_add_with_edge_creates_relationship(self, db) -> None:
        data = _make_new_rule_data()
        data["rule_id"] = "TEST-ADD-002"
        await db.create_rule(data)
        await db.create_edge("SUPPLEMENTS", "TEST-ADD-002", "TEST-ADD-001")

        neighbors = await db.traverse_neighbors("TEST-ADD-002", hops=1)
        neighbor_ids = [n["rule_id"] for n in neighbors]
        assert "TEST-ADD-001" in neighbor_ids


# ---------------------------------------------------------------------------
# writ edit: idempotent update (INV-7)
# ---------------------------------------------------------------------------

class TestAuthoringEdit:

    async def test_edit_updates_existing_rule(self, db) -> None:
        # Create initial.
        data = _make_new_rule_data()
        data["rule_id"] = "TEST-EDIT-001"
        await db.create_rule(data)

        # Edit: change statement.
        data["statement"] = "Updated statement for edit test."
        await db.create_rule(data)

        fetched = await db.get_rule("TEST-EDIT-001")
        assert fetched["statement"] == "Updated statement for edit test."

    async def test_edit_is_idempotent(self, db) -> None:
        data = _make_new_rule_data()
        data["rule_id"] = "TEST-EDIT-002"
        await db.create_rule(data)

        # Edit with same data.
        await db.create_rule(data)
        fetched = await db.get_rule("TEST-EDIT-002")
        assert fetched["rule_id"] == "TEST-EDIT-002"

    async def test_edit_nonexistent_rule_id(self, db) -> None:
        fetched = await db.get_rule("NONEXISTENT-RULE-999")
        assert fetched is None
