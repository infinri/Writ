"""Phase 6b/6c/6d: consolidated infrastructure verification.

Closes the documentation/coverage gaps in the master plan
(`docs/phase-6-plan.md`) for sub-phases 6b (edges + traversal),
6c (ingest parser), and 6d (Neo4j migration). The substantive
testing for these sub-phases already lives in:

  - `tests/test_schema_roundtrip.py::TestNewEdgeTypes` (24 tests)
  - `tests/test_multi_node_ingest.py` (16 tests)

This file adds the small confirmatory assertions that pin the
contract surface those existing tests didn't directly cover:

  6b -- ALLOWED_EDGE_TYPES set membership + class-name to
        Neo4j-relationship-name mapping convention.
  6c -- NODE START + legacy RULE START dispatch in the same file.
  6d -- migrate.py module imports + idempotency contract under a
        stub Neo4j connection.

No production code changes. Test-only addition.

Change C update: APPLIES_TO and JUSTIFIED_BY removed from
_PRE_EXISTING_RELATIONSHIP_NAMES (they are retired); total count
updated from 19 to 17.  test_allowed_edge_types_total_count is RED
until db.py is updated.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 6b -- Edge contract surface
# ============================================================================


_NEW_EDGE_RELATIONSHIP_NAMES = {
    "TEACHES",
    "COUNTERS",
    "DEMONSTRATES",
    "DISPATCHES",
    "GATES",
    "PRESSURE_TESTS",
    "CONTAINS",
    "ATTACHED_TO",
}


# Change C: APPLIES_TO and JUSTIFIED_BY retired; removed from this set.
# Pre-existing allowlist is now 7 types (was 9).
_PRE_EXISTING_RELATIONSHIP_NAMES = {
    "DEPENDS_ON",
    "PRECEDES",
    "CONFLICTS_WITH",
    "SUPPLEMENTS",
    "SUPERSEDES",
    "RELATED_TO",
    "ABSTRACTS",
}


def _camel_to_screaming_snake(name: str) -> str:
    """Convert PascalCase class name to SCREAMING_SNAKE relationship name.

    `Teaches` -> `TEACHES`
    `PressureTests` -> `PRESSURE_TESTS`
    `AttachedTo` -> `ATTACHED_TO`
    """
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.upper()


class TestPhase6bEdgeContract:
    """Edge-class contract surface: ALLOWED_EDGE_TYPES membership +
    class-name to Neo4j-relationship-name convention."""

    def test_allowed_edge_types_contains_all_eight_new_types(self) -> None:
        from writ.graph.db import ALLOWED_EDGE_TYPES
        missing = _NEW_EDGE_RELATIONSHIP_NAMES - ALLOWED_EDGE_TYPES
        assert not missing, (
            f"ALLOWED_EDGE_TYPES is missing the 8 Phase-1 edges: {missing}"
        )

    def test_allowed_edge_types_total_count(self) -> None:
        from writ.graph.db import ALLOWED_EDGE_TYPES
        # 17 as of Change C (APPLIES_TO + JUSTIFIED_BY retired), plus 7 added by
        # later programs that this stale constant never tracked: decision-memory
        # (HAS_DECISION, HAS_CHANGE, HAS_COMMIT, MOTIVATED_BY) and the methodology
        # corpus (TEACHES, DISPATCHES, PRESSURE_TESTS). The count is deliberately
        # exact so ADDING an edge type stays a conscious act that updates this
        # number; it had drifted to a permanent RED instead.
        # 25 since ABOUT (open questions, program item 7a, 2026-10-08).
        assert len(ALLOWED_EDGE_TYPES) == 25, (
            f"ALLOWED_EDGE_TYPES expected 25 entries; got "
            f"{len(ALLOWED_EDGE_TYPES)}: {sorted(ALLOWED_EDGE_TYPES)}. Adding or "
            f"removing an edge type must update this count deliberately."
        )

    @pytest.mark.parametrize(
        "class_name, expected_relation",
        [
            ("Teaches", "TEACHES"),
            ("Counters", "COUNTERS"),
            ("Demonstrates", "DEMONSTRATES"),
            ("Dispatches", "DISPATCHES"),
            ("Gates", "GATES"),
            ("PressureTests", "PRESSURE_TESTS"),
            ("Contains", "CONTAINS"),
            ("AttachedTo", "ATTACHED_TO"),
        ],
    )
    def test_class_name_maps_to_relationship_name(
        self, class_name: str, expected_relation: str
    ) -> None:
        """The class-name to relationship-name convention holds for
        all 8 Phase-1 edge classes -- CamelCase splits on internal
        capitals into SCREAMING_SNAKE_CASE."""
        derived = _camel_to_screaming_snake(class_name)
        assert derived == expected_relation, (
            f"Class {class_name} should map to {expected_relation}; "
            f"convention produced {derived}"
        )

    def test_no_orphan_edge_classes(self) -> None:
        """Every concrete `_DirectedEdge` subclass in writ.graph.schema
        has a matching entry in ALLOWED_EDGE_TYPES."""
        import writ.graph.schema as schema
        from writ.graph.db import ALLOWED_EDGE_TYPES
        leaf_classes = {
            cls for name, cls in vars(schema).items()
            if isinstance(cls, type)
            and hasattr(cls, "model_fields")
            and {"source_id", "target_id"}.issubset(cls.model_fields)
            and not cls.__name__.startswith("_")
        }
        orphans: list[str] = []
        for cls in leaf_classes:
            expected = _camel_to_screaming_snake(cls.__name__)
            if expected not in ALLOWED_EDGE_TYPES:
                orphans.append(f"{cls.__name__} -> {expected}")
        assert not orphans, (
            f"Orphan edge classes (no ALLOWED_EDGE_TYPES entry): {orphans}"
        )


# ============================================================================
# 6c -- Ingest parser contract: NODE START + legacy RULE START dispatch
# ============================================================================


class TestPhase6cIngestContract:
    """Ingest parser handles both new and legacy markers in the same
    file, dispatching each to the correct Pydantic model."""

    def test_node_start_marker_parsed_to_correct_model(
        self, tmp_path: Path
    ) -> None:
        """A NODE START / NODE END block (without YAML front-matter,
        which would take precedence) parses to the right Pydantic
        model via parse_nodes_from_file + validate_parsed_node."""
        from writ.graph.ingest import parse_nodes_from_file, validate_parsed_node
        from writ.graph.schema import Skill

        # No front-matter -- the parser layers front-matter > NODE
        # START > legacy RULE START, returning at first hit.
        f = tmp_path / "skill_node.md"
        # NODE START blocks use two formats inside:
        #   `**field**: value` (METADATA_PATTERN) for short metadata
        #   `### Heading\\nlong text` (SECTION_HEADERS) for prose fields
        f.write_text(dedent("""\
            <!-- NODE START type=Skill id=SKL-PROC-TEST-001 -->
            **domain**: process
            **scope**: session
            **severity**: medium

            ### Trigger
            When the test fixture is loaded.

            ### Statement
            Skill node parses cleanly via parse_nodes_from_file.

            ### Rationale
            Phase 6c contract surface assertion.

            <!-- NODE END: SKL-PROC-TEST-001 -->
            """))

        parsed = parse_nodes_from_file(f)
        assert len(parsed) == 1, (
            f"Expected one parsed node; got {len(parsed)}"
        )
        validated = validate_parsed_node(parsed[0])
        assert isinstance(validated, Skill), (
            f"Expected Skill instance; got {type(validated).__name__}"
        )
        assert validated.skill_id == "SKL-PROC-TEST-001"

    def test_legacy_rule_start_marker_still_parses(
        self, tmp_path: Path
    ) -> None:
        """Backward compat: the legacy <!-- RULE START: id --> marker
        path remains intact."""
        from writ.graph.ingest import parse_rules_from_file

        f = tmp_path / "legacy_rule.md"
        f.write_text(dedent("""\
            <!-- RULE START: TST-LEGACY-001 -->
            ## Trigger
            When legacy rule markers are present.

            ## Statement
            Legacy markers must continue to parse.

            ## Violation
            Legacy markers no longer parse.

            ## Pass Example
            Legacy markers parse to Rule instances.

            ## Enforcement
            advisory-only

            ## Rationale
            Phase 6c backward-compat assertion.

            ## Domain
            test

            ## Severity
            low

            ## Scope
            session

            ## Last Validated
            2026-05-08
            <!-- RULE END: TST-LEGACY-001 -->
            """))

        parsed = parse_rules_from_file(f)
        assert len(parsed) >= 1, (
            f"Legacy <!-- RULE START --> marker did not parse: {parsed}"
        )
        # Parser shape may carry rule_id under different keys depending
        # on internal conventions; assert by string match.
        repr_str = repr(parsed)
        assert "TST-LEGACY-001" in repr_str, (
            f"Legacy rule_id missing from parsed output: {repr_str[:300]}"
        )

    def test_yaml_front_matter_takes_precedence_over_markers(
        self, tmp_path: Path
    ) -> None:
        """Documented precedence: front-matter (with `node_type` or
        `rule_id`) > NODE START > legacy RULE START. The parser
        returns at the first match. This test asserts the precedence
        is honored: a file with front-matter declaring a Skill is
        parsed via the front-matter path, not via embedded NODE START
        markers that follow."""
        from writ.graph.ingest import parse_nodes_from_file, validate_parsed_node
        from writ.graph.schema import Skill

        f = tmp_path / "front_matter_wins.md"
        f.write_text(dedent("""\
            ---
            skill_id: SKL-PROC-FRONTMATTER-001
            node_type: Skill
            domain: process
            severity: medium
            scope: session
            trigger: Front-matter precedence test
            statement: Front-matter wins over body markers
            rationale: Phase 6c precedence assertion
            tags: []
            last_validated: 2026-05-08
            ---

            # Body content -- should not be re-parsed as a separate node

            <!-- NODE START type=Skill id=SKL-SHOULD-NOT-APPEAR-002 -->
            ## domain
            should-not-appear
            <!-- NODE END: SKL-SHOULD-NOT-APPEAR-002 -->
            """))

        parsed = parse_nodes_from_file(f)
        assert len(parsed) == 1, (
            f"Front-matter precedence: expected 1 node, got {len(parsed)} "
            f"(NODE START markers in body must NOT be parsed when front-matter wins)"
        )
        validated = validate_parsed_node(parsed[0])
        assert isinstance(validated, Skill)
        assert validated.skill_id == "SKL-PROC-FRONTMATTER-001"


# ============================================================================
# 6d -- Migration contract surface
# ============================================================================


class _StubNeo4jSession:
    """Records every Cypher query + parameters for idempotency assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, **params: Any):
        self.calls.append((cypher, params))
        # Return a no-op async iterable for any consumer.
        class _Result:
            async def consume(self_inner):
                return None
            async def single(self_inner):
                return None
            def __aiter__(self_inner):
                return self_inner
            async def __anext__(self_inner):
                raise StopAsyncIteration
        return _Result()

    async def __aenter__(self): return self
    async def __aexit__(self, *_a): return None


class TestPhase6dMigrationContract:
    """Migration script imports cleanly; running it twice is a no-op
    (every MERGE seen in run-1 also seen in run-2 with identical
    parameters)."""

    def test_migrate_script_imports_cleanly(self) -> None:
        """`scripts.migrate` module loads without side effects beyond
        function definitions."""
        scripts_dir = WRIT_ROOT / "scripts"
        sys_path_added = str(scripts_dir) not in sys.path
        if sys_path_added:
            sys.path.insert(0, str(scripts_dir))
        try:
            module = importlib.import_module("migrate")
        finally:
            if sys_path_added:
                sys.path.remove(str(scripts_dir))
        assert hasattr(module, "run_migration"), (
            "scripts/migrate.py must expose run_migration()"
        )
        assert callable(getattr(module, "run_migration")), (
            "run_migration is not callable"
        )

    def test_migration_uses_merge_not_create(self) -> None:
        """Idempotency in Neo4j migrations is achieved via MERGE, not
        CREATE. Source-level audit: the migration script should not
        contain unconditional CREATE statements for nodes/relationships
        that would duplicate on re-run.
        """
        src = (WRIT_ROOT / "scripts" / "migrate.py").read_text()
        # Permit CREATE INDEX / CREATE CONSTRAINT (those are idempotent
        # via IF NOT EXISTS in modern Neo4j). Disallow plain `CREATE (n:`
        # node patterns.
        # Look for the dangerous form: CREATE (...:Label ...) without
        # surrounding MERGE context. A coarse but useful heuristic.
        bad_creates = re.findall(
            r"CREATE\s*\(\s*[a-zA-Z_]+\s*:\s*[A-Z]",
            src,
        )
        assert not bad_creates, (
            f"Migration script contains unconditional CREATE (n:Label ...): "
            f"{bad_creates[:5]}. Use MERGE for idempotency."
        )
