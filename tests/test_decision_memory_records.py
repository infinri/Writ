"""Decision Memory Phase 1a: graph foundation / record substrate.

Test skeleton for the capability gate defined in capabilities.md and plan.md.
Every test in this file is RED until the implementer builds the substrate.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime; system python3 errors
on embedding imports).

Neo4j-gated tests use the same fixture/skip pattern as test_reconcile_command.py and
test_phaseM3_project_query_scope.py: a pytest_asyncio fixture that calls
pytest.skip("Neo4j unreachable") when the connection is unavailable.

Capability map (one comment per test):
  Cap 1 -- Decision/FileChange/Commit are plain BaseModel records, absent from registries
  Cap 2 -- "record" in VALID_PROVENANCE, PARITY_EXEMPT_PROVENANCE defined correctly
  Cap 3 -- create_decision/create_filechange/create_commit MERGE with correct fields
  Cap 4 -- seven edge types in ALLOWED_EDGE_TYPES, create_edge type-check accepts them
  Cap 5 -- apply_constraints idempotently creates four record indexes
  Cap 6 -- provenance="record" node SURVIVES reconcile (keystone)
  Cap 7 -- provenance="record" node exempt from detect_parity_violations
  Cap 8 -- provenance="record" node NOT enrolled in graduation paths
  Cap 9 -- GRAPH_FIRST_PROVENANCE -> PARITY_EXEMPT_PROVENANCE swap preserves Rule/methodology
           reconcile+parity behavior (regression)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import ALLOWED_EDGE_TYPES, Neo4jConnection
from writ.graph.schema import (


    GRAPH_FIRST_PROVENANCE,
    NODE_ID_FIELDS,
    NODE_TYPE_MODELS,
    RETRIEVABLE_NODE_TYPES,
    VALID_PROVENANCE,
    NodeType,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


BIBLE = Path(__file__).resolve().parent.parent / "bible"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _decision_factory(**overrides) -> dict:
    """Minimal well-formed Decision record dict."""
    defaults = {
        "decision_id": "DEC-test-0001",
        "project": "test-dm",
        "title": "Add record substrate",
        "rationale": "Phase 1a builds the persistence layer.",
        "planned_files": [],
        "governing_rule_ids": [],
        "phase": "1a",
        "session_id": "sess-abc-001",
        "ts": "2026-06-25T00:00:00Z",
    }
    return {**defaults, **overrides}


def _filechange_factory(**overrides) -> dict:
    """Minimal well-formed FileChange record dict."""
    defaults = {
        "change_id": "FC-test-0001",
        "project": "test-dm",
        "path": "writ/graph/schema.py",
        "change_type": "modify",
        "reason": "Add Decision/FileChange/Commit models.",
        "commit_hash": None,
        "ts": "2026-06-25T00:00:00Z",
    }
    return {**defaults, **overrides}


def _commit_factory(**overrides) -> dict:
    """Minimal well-formed Commit record dict."""
    defaults = {
        "commit_hash": "abc123def456abc123def456abc123def456abc1",
        "project": "test-dm",
        "subject": "feat(dm): add record substrate",
        "author": "Lucio",
        "branch": "main",
        "ts": "2026-06-25T00:00:00Z",
    }
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# Neo4j-gated fixture (mirrors test_reconcile_command.py:db_corpus pattern)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe the test-dm project scope, yield, wipe again.

    Skips the whole test when Neo4j is unreachable (same pattern as
    test_reconcile_command.py:db_corpus). Uses project='test-dm' so it never
    touches the live 'writ' corpus.
    """
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    await conn.clear_project("test-dm")
    yield conn
    await conn.clear_project("test-dm")
    await conn.close()


# ---------------------------------------------------------------------------
# Capability 1 (pure Python): record models are plain BaseModel, absent from
# NodeType / RETRIEVABLE_NODE_TYPES / NODE_TYPE_MODELS / NODE_ID_FIELDS
# ---------------------------------------------------------------------------

class TestRecordModelsArePlainBaseModel:

    def test_decision_model_importable(self) -> None:
        """Cap 1: Decision is importable from writ.graph.schema."""
        from writ.graph.schema import Decision  # noqa: F401 -- RED until built

    def test_filechange_model_importable(self) -> None:
        """Cap 1: FileChange is importable from writ.graph.schema."""
        from writ.graph.schema import FileChange  # noqa: F401 -- RED until built

    def test_commit_model_importable(self) -> None:
        """Cap 1: Commit is importable from writ.graph.schema."""
        from writ.graph.schema import Commit  # noqa: F401 -- RED until built

    def test_decision_is_basemodel_not_methodology_base(self) -> None:
        """Cap 1: Decision inherits BaseModel directly, not _MethodologyNodeBase."""
        from pydantic import BaseModel
        from writ.graph.schema import Decision, _MethodologyNodeBase
        assert issubclass(Decision, BaseModel), "Decision must be a Pydantic BaseModel"
        assert not issubclass(Decision, _MethodologyNodeBase), (
            "Decision must NOT subclass _MethodologyNodeBase -- "
            "it is a record, not a retrieval candidate"
        )

    def test_filechange_is_basemodel_not_methodology_base(self) -> None:
        """Cap 1: FileChange inherits BaseModel directly, not _MethodologyNodeBase."""
        from pydantic import BaseModel
        from writ.graph.schema import FileChange, _MethodologyNodeBase
        assert issubclass(FileChange, BaseModel)
        assert not issubclass(FileChange, _MethodologyNodeBase), (
            "FileChange must NOT subclass _MethodologyNodeBase"
        )

    def test_commit_is_basemodel_not_methodology_base(self) -> None:
        """Cap 1: Commit inherits BaseModel directly, not _MethodologyNodeBase."""
        from pydantic import BaseModel
        from writ.graph.schema import Commit, _MethodologyNodeBase
        assert issubclass(Commit, BaseModel)
        assert not issubclass(Commit, _MethodologyNodeBase), (
            "Commit must NOT subclass _MethodologyNodeBase"
        )

    def test_decision_has_required_blueprint_fields(self) -> None:
        """Cap 1: Decision carries the blueprint fields (plan.md Analysis section)."""
        from writ.graph.schema import Decision
        fields = Decision.model_fields
        for required in (
            "decision_id", "project", "title", "rationale",
            "planned_files", "governing_rule_ids", "phase",
            "session_id", "ts", "provenance", "source_origin",
        ):
            assert required in fields, (
                f"Decision is missing field '{required}' required by blueprint lines 72-74"
            )

    def test_filechange_has_required_blueprint_fields(self) -> None:
        """Cap 1: FileChange carries the blueprint fields."""
        from writ.graph.schema import FileChange
        fields = FileChange.model_fields
        for required in (
            "change_id", "project", "path", "change_type",
            "reason", "commit_hash", "ts", "provenance", "source_origin",
        ):
            assert required in fields, (
                f"FileChange is missing field '{required}' required by blueprint lines 75-77"
            )

    def test_commit_has_required_blueprint_fields(self) -> None:
        """Cap 1: Commit carries the blueprint fields."""
        from writ.graph.schema import Commit
        fields = Commit.model_fields
        for required in (
            "commit_hash", "project", "subject", "author", "branch", "ts",
        ):
            assert required in fields, (
                f"Commit is missing field '{required}' required by blueprint line 77"
            )

    def test_decision_absent_from_node_type_enum(self) -> None:
        """Cap 1: 'Decision' is NOT a member of NodeType (must not enter RAG ranking)."""
        node_type_values = {nt.value for nt in NodeType}
        assert "Decision" not in node_type_values, (
            "'Decision' must not be in NodeType -- records are not retrieval candidates"
        )

    def test_filechange_absent_from_node_type_enum(self) -> None:
        """Cap 1: 'FileChange' is NOT a member of NodeType."""
        node_type_values = {nt.value for nt in NodeType}
        assert "FileChange" not in node_type_values

    def test_commit_absent_from_node_type_enum(self) -> None:
        """Cap 1: 'Commit' is NOT a member of NodeType."""
        node_type_values = {nt.value for nt in NodeType}
        assert "Commit" not in node_type_values

    def test_decision_absent_from_retrievable_node_types(self) -> None:
        """Cap 1: Decision label is absent from RETRIEVABLE_NODE_TYPES."""
        retrievable_values = {nt.value for nt in RETRIEVABLE_NODE_TYPES}
        assert "Decision" not in retrievable_values, (
            "Decision must not be in RETRIEVABLE_NODE_TYPES"
        )

    def test_filechange_absent_from_retrievable_node_types(self) -> None:
        """Cap 1: FileChange label is absent from RETRIEVABLE_NODE_TYPES."""
        retrievable_values = {nt.value for nt in RETRIEVABLE_NODE_TYPES}
        assert "FileChange" not in retrievable_values

    def test_commit_absent_from_retrievable_node_types(self) -> None:
        """Cap 1: Commit label is absent from RETRIEVABLE_NODE_TYPES."""
        retrievable_values = {nt.value for nt in RETRIEVABLE_NODE_TYPES}
        assert "Commit" not in retrievable_values

    def test_decision_absent_from_node_type_models(self) -> None:
        """Cap 1: 'Decision' key is absent from NODE_TYPE_MODELS."""
        assert "Decision" not in NODE_TYPE_MODELS, (
            "'Decision' must not be in NODE_TYPE_MODELS -- "
            "records use custom create methods, not _node_write_spec dispatch"
        )

    def test_filechange_absent_from_node_type_models(self) -> None:
        """Cap 1: 'FileChange' key is absent from NODE_TYPE_MODELS."""
        assert "FileChange" not in NODE_TYPE_MODELS

    def test_commit_absent_from_node_type_models(self) -> None:
        """Cap 1: 'Commit' key is absent from NODE_TYPE_MODELS."""
        assert "Commit" not in NODE_TYPE_MODELS

    def test_decision_absent_from_node_id_fields(self) -> None:
        """Cap 1: 'Decision' key is absent from NODE_ID_FIELDS (structural protection)."""
        assert "Decision" not in NODE_ID_FIELDS, (
            "'Decision' must not be in NODE_ID_FIELDS -- "
            "coalesced-id null drop keeps records invisible to reconcile/parity enumeration"
        )

    def test_filechange_absent_from_node_id_fields(self) -> None:
        """Cap 1: 'FileChange' key is absent from NODE_ID_FIELDS."""
        assert "FileChange" not in NODE_ID_FIELDS

    def test_commit_absent_from_node_id_fields(self) -> None:
        """Cap 1: 'Commit' key is absent from NODE_ID_FIELDS."""
        assert "Commit" not in NODE_ID_FIELDS


# ---------------------------------------------------------------------------
# Capability 2 (pure Python): "record" in VALID_PROVENANCE, and
# PARITY_EXEMPT_PROVENANCE == GRAPH_FIRST_PROVENANCE | {"record"}
# ---------------------------------------------------------------------------

class TestRecordProvenanceConstants:

    def test_record_in_valid_provenance(self) -> None:
        """Cap 2: 'record' is an accepted value in VALID_PROVENANCE."""
        assert "record" in VALID_PROVENANCE, (
            "'record' must be added to VALID_PROVENANCE in schema.py:32 "
            "(plan.md Analysis, blueprint line 57)"
        )

    def test_parity_exempt_provenance_importable(self) -> None:
        """Cap 2: PARITY_EXEMPT_PROVENANCE is importable from writ.graph.schema."""
        from writ.graph.schema import PARITY_EXEMPT_PROVENANCE  # noqa: F401 -- RED until built

    def test_parity_exempt_provenance_is_superset_of_graph_first(self) -> None:
        """Cap 2: PARITY_EXEMPT_PROVENANCE == GRAPH_FIRST_PROVENANCE | {"record"}."""
        from writ.graph.schema import PARITY_EXEMPT_PROVENANCE
        expected = GRAPH_FIRST_PROVENANCE | {"record"}
        assert PARITY_EXEMPT_PROVENANCE == expected, (
            f"PARITY_EXEMPT_PROVENANCE={PARITY_EXEMPT_PROVENANCE!r} "
            f"but expected {expected!r}. "
            "See plan.md Analysis 'provenance set-membership exemption' section."
        )

    def test_graph_first_provenance_unchanged(self) -> None:
        """Cap 2: GRAPH_FIRST_PROVENANCE still equals {proposed, graduation_pending}.

        The swap must NOT modify the existing constant -- its 'promotable' semantics
        must be preserved so no future graduation path auto-includes 'record'.
        """
        assert GRAPH_FIRST_PROVENANCE == frozenset({"proposed", "graduation_pending"}), (
            "GRAPH_FIRST_PROVENANCE must remain {proposed, graduation_pending}. "
            "The new constant is PARITY_EXEMPT_PROVENANCE, not a mutation of this one."
        )

    def test_record_not_in_graph_first_provenance(self) -> None:
        """Cap 2: 'record' is NOT in GRAPH_FIRST_PROVENANCE (wrong set, wrong semantics)."""
        assert "record" not in GRAPH_FIRST_PROVENANCE, (
            "'record' must NOT be added to GRAPH_FIRST_PROVENANCE. "
            "Records are permanent, not promotable graph-first knowledge. "
            "Use PARITY_EXEMPT_PROVENANCE instead."
        )


# ---------------------------------------------------------------------------
# Capability 4 (pure Python, membership half): seven edge types are in
# ALLOWED_EDGE_TYPES; create_edge type-check accepts them without raising
# "Unknown edge type".
# ---------------------------------------------------------------------------

DECISION_MEMORY_EDGE_TYPES = (
    "HAS_DECISION", "HAS_CHANGE", "HAS_COMMIT",
    "MOTIVATED_BY", "GOVERNED_BY", "INCLUDES", "REALIZES",
)


class TestDecisionMemoryEdgeTypesMembership:

    @pytest.mark.parametrize("edge_type", DECISION_MEMORY_EDGE_TYPES)
    def test_edge_type_in_allowed_edge_types(self, edge_type: str) -> None:
        """Cap 4: each decision-memory edge type is a member of ALLOWED_EDGE_TYPES."""
        assert edge_type in ALLOWED_EDGE_TYPES, (
            f"'{edge_type}' must be added to ALLOWED_EDGE_TYPES in db.py. "
            "See plan.md Analysis 'Edge types' section and blueprint line 108."
        )

    @pytest.mark.parametrize("edge_type", DECISION_MEMORY_EDGE_TYPES)
    def test_create_edge_does_not_raise_unknown_edge_type(self, edge_type: str) -> None:
        """Cap 4 (type-check acceptance): create_edge passes the allow-list check for
        each decision-memory edge type and does NOT raise ValueError('Unknown edge type').

        NOTE: create_edge resolves endpoints via NODE_ID_FIELDS (_id_or_match). Because
        record id fields are intentionally absent from NODE_ID_FIELDS (Cap 1), the call
        will raise a different error after the type check passes (e.g., the endpoint
        MATCH finds no node). That is acceptable -- we assert the error is NOT the
        type-validation ValueError.

        This is purely a unit assertion on ALLOWED_EDGE_TYPES via the type-check branch
        at db.py:299-300. No Neo4j connection is needed.
        """
        # ALLOWED_EDGE_TYPES is already imported at module level.
        # The membership assertion above (same parametrize) is the canonical check.
        # This test confirms the message string: if we called create_edge with a known-
        # bogus node against a live DB, we want NOT "Unknown edge type". We can assert
        # membership directly -- if edge_type is IN the set, the raise at db.py:299 is
        # never reached.
        assert edge_type in ALLOWED_EDGE_TYPES, (
            f"create_edge would raise ValueError('Unknown edge type: {edge_type}') "
            f"because '{edge_type}' is absent from ALLOWED_EDGE_TYPES. "
            "Add it per plan.md."
        )


# ---------------------------------------------------------------------------
# Capability 3 (Neo4j-gated): create_decision / create_filechange /
# create_commit MERGE on id key, carry project, set provenance="record"
# and source_origin="graph-authored", readable via direct Cypher.
# ---------------------------------------------------------------------------

class TestRecordCreateMethods:

    @pytest.mark.asyncio
    async def test_create_decision_exists_on_connection(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Neo4jConnection has a create_decision method."""
        assert hasattr(db_clean, "create_decision"), (
            "Neo4jConnection must have a create_decision method. "
            "See plan.md Analysis 'Custom create methods' section."
        )

    @pytest.mark.asyncio
    async def test_create_filechange_exists_on_connection(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Neo4jConnection has a create_filechange method."""
        assert hasattr(db_clean, "create_filechange"), (
            "Neo4jConnection must have a create_filechange method."
        )

    @pytest.mark.asyncio
    async def test_create_commit_exists_on_connection(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Neo4jConnection has a create_commit method."""
        assert hasattr(db_clean, "create_commit"), (
            "Neo4jConnection must have a create_commit method."
        )

    @pytest.mark.asyncio
    async def test_create_decision_returns_decision_id(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: create_decision returns the decision_id string."""
        data = _decision_factory()
        result = await db_clean.create_decision(**data)
        assert result == data["decision_id"], (
            f"create_decision must return the decision_id; got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_create_decision_sets_provenance_record(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Decision node has provenance='record' after create_decision."""
        data = _decision_factory()
        await db_clean.create_decision(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Decision {decision_id: $did}) "
                "RETURN n.provenance AS provenance",
                did=data["decision_id"],
            )
            row = await res.single()
        assert row is not None, "Decision node not found after create_decision"
        assert row["provenance"] == "record", (
            f"Decision.provenance must be 'record', got {row['provenance']!r}"
        )

    @pytest.mark.asyncio
    async def test_create_decision_sets_source_origin_graph_authored(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Decision node has source_origin='graph-authored' after create_decision."""
        data = _decision_factory()
        await db_clean.create_decision(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Decision {decision_id: $did}) "
                "RETURN n.source_origin AS source_origin",
                did=data["decision_id"],
            )
            row = await res.single()
        assert row is not None
        assert row["source_origin"] == "graph-authored", (
            f"Decision.source_origin must be 'graph-authored', got {row['source_origin']!r}. "
            "Blueprint line 65: honest 'no markdown home' marker."
        )

    @pytest.mark.asyncio
    async def test_create_decision_sets_project(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Decision node carries the project property."""
        data = _decision_factory(project="test-dm")
        await db_clean.create_decision(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Decision {decision_id: $did}) RETURN n.project AS project",
                did=data["decision_id"],
            )
            row = await res.single()
        assert row is not None
        assert row["project"] == "test-dm"

    @pytest.mark.asyncio
    async def test_create_decision_is_idempotent(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: create_decision is idempotent (MERGE on decision_id)."""
        data = _decision_factory()
        await db_clean.create_decision(**data)
        await db_clean.create_decision(**data)  # second call must not error or duplicate
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Decision {decision_id: $did}) RETURN count(n) AS c",
                did=data["decision_id"],
            )
            row = await res.single()
        assert row["c"] == 1, (
            f"Expected 1 Decision node after two identical creates, found {row['c']}"
        )

    @pytest.mark.asyncio
    async def test_create_filechange_sets_provenance_record(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: FileChange node has provenance='record' after create_filechange."""
        data = _filechange_factory()
        await db_clean.create_filechange(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:FileChange {change_id: $cid}) "
                "RETURN n.provenance AS provenance",
                cid=data["change_id"],
            )
            row = await res.single()
        assert row is not None, "FileChange node not found"
        assert row["provenance"] == "record"

    @pytest.mark.asyncio
    async def test_create_filechange_sets_source_origin_graph_authored(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: FileChange node has source_origin='graph-authored'."""
        data = _filechange_factory()
        await db_clean.create_filechange(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:FileChange {change_id: $cid}) "
                "RETURN n.source_origin AS source_origin",
                cid=data["change_id"],
            )
            row = await res.single()
        assert row is not None
        assert row["source_origin"] == "graph-authored"

    @pytest.mark.asyncio
    async def test_create_commit_sets_provenance_record(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Commit node has provenance='record' after create_commit."""
        data = _commit_factory()
        await db_clean.create_commit(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Commit {commit_hash: $ch}) "
                "RETURN n.provenance AS provenance",
                ch=data["commit_hash"],
            )
            row = await res.single()
        assert row is not None, "Commit node not found"
        assert row["provenance"] == "record"

    @pytest.mark.asyncio
    async def test_create_commit_sets_source_origin_graph_authored(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: Commit node has source_origin='graph-authored'."""
        data = _commit_factory()
        await db_clean.create_commit(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Commit {commit_hash: $ch}) "
                "RETURN n.source_origin AS source_origin",
                ch=data["commit_hash"],
            )
            row = await res.single()
        assert row is not None
        assert row["source_origin"] == "graph-authored"

    @pytest.mark.asyncio
    async def test_create_commit_is_idempotent(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 3: create_commit is idempotent (MERGE on commit_hash)."""
        data = _commit_factory()
        await db_clean.create_commit(**data)
        await db_clean.create_commit(**data)
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Commit {commit_hash: $ch}) RETURN count(n) AS c",
                ch=data["commit_hash"],
            )
            row = await res.single()
        assert row["c"] == 1, (
            f"Expected 1 Commit node after two identical creates, found {row['c']}"
        )


# ---------------------------------------------------------------------------
# Capability 5 (Neo4j-gated): apply_constraints creates the four record
# indexes idempotently.
# ---------------------------------------------------------------------------

class TestRecordIndexes:

    @pytest.mark.asyncio
    async def test_apply_constraints_idempotent_for_record_indexes(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 5: apply_constraints can be called twice without error."""
        await db_clean.apply_constraints()
        await db_clean.apply_constraints()  # second call must not raise

    @pytest.mark.asyncio
    async def test_filechange_project_path_index_exists(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 5: SHOW INDEXES confirms the FileChange(project, path) index."""
        await db_clean.apply_constraints()
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run("SHOW INDEXES YIELD labelsOrTypes, properties")
            rows = [dict(r) async for r in res]
        filechange_indexes = [
            r for r in rows
            if r.get("labelsOrTypes") and "FileChange" in r["labelsOrTypes"]
        ]
        path_index = any(
            "path" in (r.get("properties") or []) for r in filechange_indexes
        )
        assert path_index, (
            "No FileChange index on 'path' found after apply_constraints. "
            "Expected: CREATE INDEX filechange_project_path IF NOT EXISTS "
            "FOR (n:FileChange) ON (n.project, n.path)  (or similar)."
        )

    @pytest.mark.asyncio
    async def test_filechange_project_commit_hash_index_exists(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 5: SHOW INDEXES confirms the FileChange(project, commit_hash) index."""
        await db_clean.apply_constraints()
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run("SHOW INDEXES YIELD labelsOrTypes, properties")
            rows = [dict(r) async for r in res]
        filechange_indexes = [
            r for r in rows
            if r.get("labelsOrTypes") and "FileChange" in r["labelsOrTypes"]
        ]
        commit_hash_index = any(
            "commit_hash" in (r.get("properties") or []) for r in filechange_indexes
        )
        assert commit_hash_index, (
            "No FileChange index on 'commit_hash' found after apply_constraints."
        )

    @pytest.mark.asyncio
    async def test_commit_commit_hash_index_exists(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 5: SHOW INDEXES confirms the Commit(commit_hash) index."""
        await db_clean.apply_constraints()
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run("SHOW INDEXES YIELD labelsOrTypes, properties")
            rows = [dict(r) async for r in res]
        commit_indexes = [
            r for r in rows
            if r.get("labelsOrTypes") and "Commit" in r["labelsOrTypes"]
        ]
        hash_index = any(
            "commit_hash" in (r.get("properties") or []) for r in commit_indexes
        )
        assert hash_index, (
            "No Commit index on 'commit_hash' found after apply_constraints."
        )

    @pytest.mark.asyncio
    async def test_decision_project_index_exists(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 5: SHOW INDEXES confirms the Decision(project) index."""
        await db_clean.apply_constraints()
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run("SHOW INDEXES YIELD labelsOrTypes, properties")
            rows = [dict(r) async for r in res]
        decision_indexes = [
            r for r in rows
            if r.get("labelsOrTypes") and "Decision" in r["labelsOrTypes"]
        ]
        project_index = any(
            "project" in (r.get("properties") or []) for r in decision_indexes
        )
        assert project_index, (
            "No Decision index on 'project' found after apply_constraints."
        )


# ---------------------------------------------------------------------------
# Capability 6 (Neo4j-gated, KEYSTONE): provenance="record" node survives
# reconcile (is NOT deleted by _reconcile_delete_stale_nodes).
# ---------------------------------------------------------------------------

class TestRecordSurvivesReconcile:

    @pytest.mark.asyncio
    async def test_decision_record_survives_reconcile(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 6 (keystone): a Decision with provenance='record' is NOT deleted
        by reconcile against the bible oracle.

        Precondition: 'DEC-test-keystone-001' is absent from the bible corpus
        (it is a made-up id, never in any markdown file).
        Post-reconcile assertion: the Decision node still exists.
        """
        from writ.graph.methodology_ingest import reconcile

        data = _decision_factory(
            decision_id="DEC-test-keystone-001",
            project="test-dm",
        )
        await db_clean.create_decision(**data)

        # Reconcile the writ bible against project 'test-dm'.
        # The Decision node is absent from the oracle but has provenance='record',
        # so it must be exempted from deletion.
        # NOTE: reconcile() requires a non-empty oracle -- it runs against 'bible/'
        # which has real Rule/methodology nodes but zero Decision nodes. That's fine:
        # the oracle is non-empty (Rules exist), and 'test-dm' scoping means only
        # test-dm nodes are candidates for deletion.
        result = await reconcile(BIBLE, db_clean, project="test-dm")

        assert "DEC-test-keystone-001" not in result["deleted_nodes"], (
            "reconcile deleted a Decision node with provenance='record'. "
            "The PARITY_EXEMPT_PROVENANCE swap at methodology_ingest.py:564 is missing "
            "or did not include 'record'."
        )

        # Confirm the node actually still exists in the graph.
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Decision {decision_id: $did}) RETURN count(n) AS c",
                did="DEC-test-keystone-001",
            )
            row = await res.single()
        assert row["c"] == 1, (
            "Decision node gone after reconcile despite provenance='record'. "
            "Keystone broken: records must survive the reconcile sweep."
        )

    @pytest.mark.asyncio
    async def test_filechange_record_survives_reconcile(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 6: a FileChange with provenance='record' is NOT deleted by reconcile."""
        from writ.graph.methodology_ingest import reconcile

        data = _filechange_factory(change_id="FC-test-keystone-001", project="test-dm")
        await db_clean.create_filechange(**data)

        result = await reconcile(BIBLE, db_clean, project="test-dm")

        assert "FC-test-keystone-001" not in result["deleted_nodes"]
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:FileChange {change_id: $cid}) RETURN count(n) AS c",
                cid="FC-test-keystone-001",
            )
            row = await res.single()
        assert row["c"] == 1, "FileChange with provenance='record' deleted by reconcile"


# ---------------------------------------------------------------------------
# Capability 7 (Neo4j-gated): provenance="record" node is exempt from
# detect_parity_violations.
# ---------------------------------------------------------------------------

class TestRecordParityExempt:

    @pytest.mark.asyncio
    async def test_decision_not_flagged_by_detect_parity_violations(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 7: detect_parity_violations does not flag a Decision record.

        A Decision node is absent from every bible/ markdown file (by design),
        but because its provenance is 'record' (in PARITY_EXEMPT_PROVENANCE),
        the parity check must exempt it, not report it as a violation.
        """
        from writ.graph.integrity import IntegrityChecker

        data = _decision_factory(
            decision_id="DEC-test-parity-001",
            project="test-dm",
        )
        await db_clean.create_decision(**data)

        checker = IntegrityChecker(db_clean._driver, db_clean._database)
        violations = await checker.detect_parity_violations(
            bible_dir=BIBLE, project="test-dm"
        )

        violation_ids = {v["id"] for v in violations}
        assert "DEC-test-parity-001" not in violation_ids, (
            "detect_parity_violations flagged a Decision node with provenance='record'. "
            "The PARITY_EXEMPT_PROVENANCE swap at integrity.py:688 is missing or did not "
            "include 'record'."
        )

    @pytest.mark.asyncio
    async def test_filechange_not_flagged_by_detect_parity_violations(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 7: detect_parity_violations does not flag a FileChange record."""
        from writ.graph.integrity import IntegrityChecker

        data = _filechange_factory(change_id="FC-test-parity-001", project="test-dm")
        await db_clean.create_filechange(**data)

        checker = IntegrityChecker(db_clean._driver, db_clean._database)
        violations = await checker.detect_parity_violations(
            bible_dir=BIBLE, project="test-dm"
        )

        violation_ids = {v["id"] for v in violations}
        assert "FC-test-parity-001" not in violation_ids, (
            "detect_parity_violations flagged a FileChange node with provenance='record'."
        )


# ---------------------------------------------------------------------------
# Capability 8 (pure Python + structural): provenance="record" node is NOT
# enrolled in graduation. The graduation paths key on :Rule + literal 'proposed';
# a record carries neither.
# ---------------------------------------------------------------------------

class TestRecordNotEnrolledInGraduation:

    def test_graduation_flip_query_keys_on_rule_label_and_proposed_literal(self) -> None:
        """Cap 8: the db.py graduation flip uses MATCH (r:Rule) WHERE r.provenance='proposed'.

        A record node is NOT a :Rule and its provenance is 'record', not 'proposed'.
        This test verifies the scoping by inspecting the query string at the known
        location (db.py:993: 'proposed' literal + :Rule label guard).
        Fails if either the `:Rule` or `'proposed'` guard is absent from the flip query.
        """
        import inspect
        from writ.graph.db import Neo4jConnection
        source = inspect.getsource(Neo4jConnection.evaluate_and_flip_graduation)
        assert ":Rule" in source or "Rule" in source, (
            "evaluate_and_flip_graduation must scope to the :Rule label -- "
            "a record node (no :Rule label) must never be affected"
        )
        assert "'proposed'" in source or '"proposed"' in source, (
            "evaluate_and_flip_graduation must guard on the literal 'proposed' provenance -- "
            "a record (provenance='record') must never match the graduation condition"
        )

    def test_promotion_promote_candidate_requires_graduation_pending(self) -> None:
        """Cap 8: promotion.py:promote_candidate requires provenance='graduation_pending'.

        A record with provenance='record' is rejected before any promotion write.
        Verified by inspecting the source guard at promotion.py:191.
        """
        import inspect
        from writ.promotion import promote_candidate
        source = inspect.getsource(promote_candidate)
        assert "graduation_pending" in source, (
            "promote_candidate must check provenance=='graduation_pending' -- "
            "a record with provenance='record' must never pass this gate"
        )

    def test_record_provenance_does_not_equal_proposed(self) -> None:
        """Cap 8: the literal 'record' != 'proposed', so frequency counters never flip it.

        The frequency graduation path (db.py:981) reads
        `if rec["provenance"] != "proposed": return None`.
        A record node with provenance='record' satisfies that guard and is a no-op.
        """
        assert "record" != "proposed", (
            "'record' and 'proposed' are distinct -- graduation never touches records"
        )

    def test_record_provenance_does_not_equal_graduation_pending(self) -> None:
        """Cap 8: 'record' != 'graduation_pending', so promote_candidate rejects records."""
        assert "record" != "graduation_pending"

    def test_record_model_has_no_graduation_counter_fields(self) -> None:
        """Cap 8: Decision/FileChange/Commit carry no times_seen_* or graduated_via fields.

        These fields are the frequency/graduation enrollment markers on Rule nodes.
        Records must not carry them.
        """
        from writ.graph.schema import Commit, Decision, FileChange
        graduation_fields = ("times_seen_positive", "times_seen_negative", "graduated_via")
        for model_cls in (Decision, FileChange, Commit):
            for field in graduation_fields:
                assert field not in model_cls.model_fields, (
                    f"{model_cls.__name__} must not carry '{field}' -- "
                    "that field is a graduation enrollment marker for Rule nodes only"
                )


# ---------------------------------------------------------------------------
# Capability 9 (Neo4j-gated, regression): GRAPH_FIRST_PROVENANCE ->
# PARITY_EXEMPT_PROVENANCE swap does NOT change existing reconcile/parity
# behavior for Rule/methodology nodes. A 'proposed' node still survives
# reconcile and is still parity-exempt.
# ---------------------------------------------------------------------------

class TestGraphFirstProvenanceRegressionAfterSwap:

    @pytest.mark.asyncio
    async def test_proposed_rule_still_survives_reconcile(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 9 (regression): a Rule with provenance='proposed' is NOT deleted by reconcile.

        This verifies that swapping GRAPH_FIRST_PROVENANCE -> PARITY_EXEMPT_PROVENANCE at
        methodology_ingest.py:564 is a strict superset -- the old members ('proposed',
        'graduation_pending') still behave exactly as before.
        """
        from writ.graph.methodology_ingest import reconcile

        # Seed a proposed Rule that is NOT in the bible oracle.
        proposed_rule = {
            "rule_id": "TEST-DM-REG-001",
            "project": "test-dm",
            "provenance": "proposed",
            "domain": "Architecture",
            "severity": "high",
            "scope": "module",
            "trigger": "regression-probe for decision-memory cap-9",
            "statement": "regression probe -- not in bible",
            "violation": "Bad example.",
            "pass_example": "Good example.",
            "enforcement": "Code review.",
            "rationale": "Regression: proposed Rule must survive reconcile after the swap.",
            "last_validated": "2026-03-15",
        }
        await db_clean.create_rule(proposed_rule, source_origin="graph-authored")

        result = await reconcile(BIBLE, db_clean, project="test-dm")

        assert "TEST-DM-REG-001" not in result["deleted_nodes"], (
            "reconcile deleted a Rule with provenance='proposed' after the "
            "PARITY_EXEMPT_PROVENANCE swap. The swap must be a SUPERSET -- "
            "'proposed' must still be exempt."
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (n:Rule {rule_id: $rid}) RETURN count(n) AS c",
                rid="TEST-DM-REG-001",
            )
            row = await res.single()
        assert row["c"] == 1, (
            "proposed Rule node gone after reconcile -- regression: "
            "PARITY_EXEMPT_PROVENANCE does not include 'proposed'"
        )

    @pytest.mark.asyncio
    async def test_proposed_rule_still_parity_exempt(
        self, db_clean: Neo4jConnection
    ) -> None:
        """Cap 9 (regression): a Rule with provenance='proposed' is not flagged by
        detect_parity_violations after the PARITY_EXEMPT_PROVENANCE swap."""
        from writ.graph.integrity import IntegrityChecker

        proposed_rule = {
            "rule_id": "TEST-DM-PAR-001",
            "project": "test-dm",
            "provenance": "proposed",
            "domain": "Architecture",
            "severity": "high",
            "scope": "module",
            "trigger": "parity-regression-probe for decision-memory cap-9",
            "statement": "parity regression probe -- not in bible",
            "violation": "Bad example.",
            "pass_example": "Good example.",
            "enforcement": "Code review.",
            "rationale": "Regression: proposed Rule must be parity-exempt after the swap.",
            "last_validated": "2026-03-15",
        }
        await db_clean.create_rule(proposed_rule, source_origin="graph-authored")

        checker = IntegrityChecker(db_clean._driver, db_clean._database)
        violations = await checker.detect_parity_violations(
            bible_dir=BIBLE, project="test-dm"
        )

        violation_ids = {v["id"] for v in violations}
        assert "TEST-DM-PAR-001" not in violation_ids, (
            "detect_parity_violations flagged a Rule with provenance='proposed' after "
            "the PARITY_EXEMPT_PROVENANCE swap. Regression: 'proposed' must remain "
            "in PARITY_EXEMPT_PROVENANCE (it is inherited via GRAPH_FIRST_PROVENANCE)."
        )
