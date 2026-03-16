"""Phase 3: Ingestion and migration tests.

Tests Markdown parsing, schema validation of parsed rules, and migration integration.
Parser tests are pure unit tests (TEST-ISO-001). Migration tests require Neo4j.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import pytest_asyncio

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import (
    discover_rule_files,
    parse_rules_from_file,
    validate_parsed_rule,
)

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"

SAMPLE_RULE = dedent("""\
    # Test Document

    <!-- RULE START: ARCH-ORG-001 -->
    ## Rule ARCH-ORG-001: Code Organization

    **Domain**: Architecture
    **Severity**: High
    **Scope**: module

    ### Trigger
    When creating a class that contains logic from a different layer.

    ### Statement
    Each class must belong to exactly one architectural layer.

    ### Violation (bad)
    Controller contains SQL query.

    ### Pass (good)
    Controller delegates to service.

    ### Enforcement
    Per-slice findings table. See also ARCH-DI-001.

    ### Rationale
    Mixed layers create untestable classes.
    <!-- RULE END: ARCH-ORG-001 -->
""")

SAMPLE_TWO_RULES = dedent("""\
    # Multi-rule file

    <!-- RULE START: DB-SQL-001 -->
    ## Rule DB-SQL-001: Named Binds

    **Domain**: Database
    **Severity**: High
    **Scope**: file

    ### Trigger
    When writing raw SQL with parameter placeholders.

    ### Statement
    Use named bind keys instead of positional placeholders.

    ### Violation (bad)
    Positional ? placeholders.

    ### Pass (good)
    Named :customerId binds.

    ### Enforcement
    Code review.

    ### Rationale
    Named binds are self-documenting.
    <!-- RULE END: DB-SQL-001 -->

    ---

    <!-- RULE START: DB-SQL-002 -->
    ## Rule DB-SQL-002: Minimal Fragmentation

    **Domain**: Database
    **Severity**: Medium
    **Scope**: file

    ### Trigger
    When SQL is constructed via string concatenation.

    ### Statement
    Define SQL in a single heredoc or multi-line string.

    ### Violation (bad)
    Concatenated SQL fragments.

    ### Pass (good)
    Single heredoc SQL.

    ### Enforcement
    Code review.

    ### Rationale
    Fragmented SQL is harder to debug.
    <!-- RULE END: DB-SQL-002 -->
""")

SAMPLE_ENF_RULE = dedent("""\
    <!-- RULE START: ENF-GATE-001 -->
    ## Rule ENF-GATE-001: Phase A Gate

    **Domain**: AI Enforcement
    **Severity**: Critical
    **Scope**: session

    ### Trigger
    When AI completes Phase A.

    ### Statement
    Phase A must be approved before Phase B.

    ### Violation (bad)
    AI proceeds without approval.

    ### Pass (good)
    AI halts and waits.

    ### Enforcement
    Gate file check.

    ### Rationale
    Human review catches errors.
    <!-- RULE END: ENF-GATE-001 -->
""")

NO_MARKERS = dedent("""\
    # Core Principles

    ## DRY
    Before writing new code, search for similar implementations.

    ## SOLID
    SRP, OCP, LSP, ISP, DIP.
""")


@pytest.fixture()
def tmp_rule_file(tmp_path: Path) -> Path:
    f = tmp_path / "test_rule.md"
    f.write_text(SAMPLE_RULE)
    return f


@pytest.fixture()
def tmp_multi_file(tmp_path: Path) -> Path:
    f = tmp_path / "multi_rules.md"
    f.write_text(SAMPLE_TWO_RULES)
    return f


@pytest.fixture()
def tmp_enf_file(tmp_path: Path) -> Path:
    f = tmp_path / "enf_rule.md"
    f.write_text(SAMPLE_ENF_RULE)
    return f


@pytest.fixture()
def tmp_no_markers(tmp_path: Path) -> Path:
    f = tmp_path / "no_markers.md"
    f.write_text(NO_MARKERS)
    return f


@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


class TestMarkdownParser:
    """Markdown rule block extraction."""

    def test_parse_single_rule(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        assert len(rules) == 1
        assert rules[0]["rule_id"] == "ARCH-ORG-001"

    def test_parse_multiple_rules_from_one_file(self, tmp_multi_file: Path) -> None:
        rules = parse_rules_from_file(tmp_multi_file)
        assert len(rules) == 2
        ids = [r["rule_id"] for r in rules]
        assert "DB-SQL-001" in ids
        assert "DB-SQL-002" in ids

    def test_skip_file_without_markers(self, tmp_no_markers: Path) -> None:
        rules = parse_rules_from_file(tmp_no_markers)
        assert len(rules) == 0

    def test_metadata_extraction(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        rule = rules[0]
        assert rule["domain"] == "Architecture"
        assert rule["severity"] == "high"
        assert rule["scope"] == "module"

    def test_section_extraction(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        rule = rules[0]
        assert "logic from a different layer" in rule["trigger"]
        assert "one architectural layer" in rule["statement"]
        assert "SQL query" in rule["violation"]
        assert "delegates to service" in rule["pass_example"]
        assert "Per-slice" in rule["enforcement"]
        assert "untestable" in rule["rationale"]

    def test_graph_only_defaults_applied(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        rule = rules[0]
        assert rule["confidence"] == "production-validated"
        assert rule["evidence"] == "doc:original-bible"
        assert rule["staleness_window"] == 365
        assert "last_validated" in rule

    def test_mandatory_derived_from_enf_prefix(self, tmp_enf_file: Path) -> None:
        rules = parse_rules_from_file(tmp_enf_file)
        assert rules[0]["mandatory"] is True

    def test_non_enf_not_mandatory(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        assert rules[0]["mandatory"] is False

    def test_cross_references_detected(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        refs = rules[0].get("_cross_references", [])
        assert "ARCH-DI-001" in refs

    def test_validated_rule_passes_schema(self, tmp_rule_file: Path) -> None:
        rules = parse_rules_from_file(tmp_rule_file)
        validated = validate_parsed_rule(rules[0])
        assert validated.rule_id == "ARCH-ORG-001"


class TestMigrationIntegration:
    """Full migration against live Neo4j."""

    @pytest.mark.asyncio
    async def test_migrate_real_bible(self, db: Neo4jConnection) -> None:
        bible_dir = Path("bible/")
        if not bible_dir.exists():
            pytest.skip("bible/ directory not found")

        files = discover_rule_files(bible_dir)
        total_rules = 0
        for f in files:
            parsed = parse_rules_from_file(f)
            for rule_data in parsed:
                validated = validate_parsed_rule(rule_data)
                clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                await db.create_rule(clean)
                total_rules += 1

        count = await db.count_rules()
        assert count == total_rules
        assert count > 0
        print(f"\nMigrated {count} rules from {len(files)} files")

    @pytest.mark.asyncio
    async def test_idempotent(self, db: Neo4jConnection) -> None:
        bible_dir = Path("bible/")
        if not bible_dir.exists():
            pytest.skip("bible/ directory not found")

        files = discover_rule_files(bible_dir)
        for _ in range(2):
            for f in files:
                for rule_data in parse_rules_from_file(f):
                    clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                    await db.create_rule(clean)

        # Count once -- should match single-pass count.
        all_rules = []
        for f in files:
            all_rules.extend(parse_rules_from_file(f))

        count = await db.count_rules()
        assert count == len(all_rules)

    @pytest.mark.asyncio
    async def test_skeleton_edges_created(self, db: Neo4jConnection) -> None:
        bible_dir = Path("bible/")
        if not bible_dir.exists():
            pytest.skip("bible/ directory not found")

        files = discover_rule_files(bible_dir)
        all_rules: list[dict] = []
        for f in files:
            all_rules.extend(parse_rules_from_file(f))

        rule_ids = {r["rule_id"] for r in all_rules}

        for rule_data in all_rules:
            clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
            await db.create_rule(clean)

        edge_count = 0
        for rule_data in all_rules:
            for ref_id in rule_data.get("_cross_references", []):
                if ref_id in rule_ids:
                    await db.create_edge("RELATED_TO", rule_data["rule_id"], ref_id)
                    edge_count += 1

        assert edge_count > 0
        print(f"\nCreated {edge_count} RELATED_TO edges")
