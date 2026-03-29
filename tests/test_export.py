"""Tests for Phase 7: Markdown export from graph (round-trip fidelity).

Per TEST-TDD-001: test skeletons approved before implementation.
Per TEST-ISO-001: each test sets up its own state, no shared mutables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from writ.export import (
    GRAPH_ONLY_FIELDS,
    SECTION_HEADERS,
    SECTION_ORDER,
    _build_file_content,
    check_export_staleness,
    export_rules_to_markdown,
    group_rules_by_file,
    read_export_timestamp,
    rule_to_markdown,
    write_export_timestamp,
)
from writ.graph.ingest import (
    parse_rules_from_file,
    validate_parsed_rule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def rule_with_code_blocks() -> dict:
    """A rule with fenced code blocks in violation and pass_example."""
    return {
        "rule_id": "TEST-CODE-001",
        "domain": "Testing",
        "severity": "high",
        "scope": "file",
        "trigger": "When writing a test.",
        "statement": "Tests must be isolated.",
        "violation": "```python\nshared_state = []\ndef test_a():\n    shared_state.append(1)\n```",
        "pass_example": "```python\ndef test_a():\n    state = []\n    state.append(1)\n    assert state == [1]\n```",
        "enforcement": "Code review.",
        "rationale": "Shared state causes flaky tests.",
        "last_validated": "2026-03-20",
    }


@pytest.fixture()
def bible_dir_with_rules(tmp_path: Path) -> Path:
    """Create a minimal bible directory with one rule file for structure mapping."""
    arch_dir = tmp_path / "bible" / "architecture"
    arch_dir.mkdir(parents=True)
    md = arch_dir / "principles.md"
    md.write_text(
        "<!-- RULE START: ARCH-ORG-001 -->\n"
        "## Rule ARCH-ORG-001\n"
        "<!-- RULE END: ARCH-ORG-001 -->\n",
        encoding="utf-8",
    )
    enf_dir = tmp_path / "bible" / "enforcement"
    enf_dir.mkdir(parents=True)
    enf_md = enf_dir / "reasoning-discipline.md"
    enf_md.write_text(
        "<!-- RULE START: ENF-GATE-001 -->\n"
        "## Rule ENF-GATE-001\n"
        "<!-- RULE END: ENF-GATE-001 -->\n",
        encoding="utf-8",
    )
    return tmp_path / "bible"


# ---------------------------------------------------------------------------
# Unit tests: rule_to_markdown
# ---------------------------------------------------------------------------

class TestRuleToMarkdown:

    def test_contains_rule_start_end_markers(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        assert f"<!-- RULE START: {valid_rule_data['rule_id']} -->" in md
        assert f"<!-- RULE END: {valid_rule_data['rule_id']} -->" in md

    def test_metadata_bold_format(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        assert "**Domain**: Architecture" in md
        assert "**Severity**: Critical" in md
        assert "**Scope**: Module" in md

    def test_all_section_headers_present(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        for header in SECTION_HEADERS.values():
            assert header in md

    def test_section_content_matches_fields(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        for field in SECTION_ORDER:
            assert valid_rule_data[field] in md

    def test_severity_title_cased(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        assert "**Severity**: Critical" in md
        assert "**Severity**: critical" not in md

    def test_scope_title_cased(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        assert "**Scope**: Module" in md
        assert "**Scope**: module" not in md

    def test_pass_header_is_pass_not_pass_example(self, valid_rule_data: dict) -> None:
        md = rule_to_markdown(valid_rule_data)
        assert "### Pass" in md
        assert "### Pass_example" not in md
        assert "### pass_example" not in md

    def test_multiline_content_preserved(self, rule_with_code_blocks: dict) -> None:
        md = rule_to_markdown(rule_with_code_blocks)
        assert "```python" in md
        assert "shared_state = []" in md

    def test_graph_only_fields_not_in_markdown(self, valid_enf_rule_data: dict) -> None:
        md = rule_to_markdown(valid_enf_rule_data)
        # None of the graph-only field names should appear as metadata lines.
        for field in GRAPH_ONLY_FIELDS:
            assert f"**{field.title()}**:" not in md
            assert f"**{field}**:" not in md


# ---------------------------------------------------------------------------
# Unit tests: group_rules_by_file
# ---------------------------------------------------------------------------

class TestGroupRulesByFile:

    def test_rules_mapped_to_existing_file(
        self, valid_rule_data: dict, bible_dir_with_rules: Path
    ) -> None:
        groups = group_rules_by_file([valid_rule_data], bible_dir_with_rules)
        # ARCH-ORG-001 should map to architecture/principles.md
        paths = list(groups.keys())
        assert any("architecture" in str(p) for p in paths)

    def test_unknown_domain_gets_derived_file(self, bible_dir_with_rules: Path) -> None:
        rule = {
            "rule_id": "NEW-DOM-001",
            "domain": "New Domain",
            "severity": "low",
            "scope": "file",
            "trigger": "t",
            "statement": "s",
            "violation": "v",
            "pass_example": "p",
            "enforcement": "e",
            "rationale": "r",
            "last_validated": "2026-03-20",
        }
        groups = group_rules_by_file([rule], bible_dir_with_rules)
        paths = list(groups.keys())
        assert any("new-domain" in str(p) for p in paths)

    def test_preserves_directory_structure(
        self,
        valid_rule_data: dict,
        valid_enf_rule_data: dict,
        bible_dir_with_rules: Path,
    ) -> None:
        groups = group_rules_by_file(
            [valid_rule_data, valid_enf_rule_data], bible_dir_with_rules
        )
        dir_names = {p.parts[0] for p in groups.keys()}
        assert "architecture" in dir_names
        assert "enforcement" in dir_names


# ---------------------------------------------------------------------------
# Unit tests: staleness detection
# ---------------------------------------------------------------------------

class TestStalenessDetection:

    def test_fresh_export_not_stale(self, tmp_path: Path) -> None:
        write_export_timestamp(tmp_path)
        # Graph write time in the past -> export is fresh.
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert check_export_staleness(tmp_path, past) is False

    def test_stale_after_later_graph_write(self, tmp_path: Path) -> None:
        write_export_timestamp(tmp_path)
        # Graph write time in the future -> export is stale.
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert check_export_staleness(tmp_path, future) is True

    def test_no_export_timestamp_is_stale(self, tmp_path: Path) -> None:
        graph_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert check_export_staleness(tmp_path, graph_time) is True

    def test_no_graph_write_is_not_stale(self, tmp_path: Path) -> None:
        write_export_timestamp(tmp_path)
        assert check_export_staleness(tmp_path, None) is False

    def test_timestamp_round_trips(self, tmp_path: Path) -> None:
        write_export_timestamp(tmp_path)
        ts = read_export_timestamp(tmp_path)
        assert ts is not None
        assert isinstance(ts, datetime)


# ---------------------------------------------------------------------------
# Integration: export + re-ingest round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Round-trip fidelity: export -> ingest -> compare fields."""

    INGEST_VISIBLE_FIELDS = (
        "rule_id", "domain", "severity", "scope",
        "trigger", "statement", "violation", "pass_example",
        "enforcement", "rationale",
    )

    def _write_and_parse(self, rules: list[dict], tmp_path: Path) -> list[dict]:
        """Helper: serialize rules to markdown, then parse back via ingest."""
        groups = group_rules_by_file(rules, tmp_path)
        for rel_path, grouped in groups.items():
            target = tmp_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_build_file_content(grouped), encoding="utf-8")

        # Re-ingest all written files.
        parsed: list[dict] = []
        for md_file in sorted(tmp_path.rglob("*.md")):
            parsed.extend(parse_rules_from_file(md_file))
        return parsed

    def test_single_rule_round_trip(self, valid_rule_data: dict, tmp_path: Path) -> None:
        parsed = self._write_and_parse([valid_rule_data], tmp_path)
        assert len(parsed) == 1
        for field in self.INGEST_VISIBLE_FIELDS:
            original = str(valid_rule_data[field]).lower()
            reparsed = str(parsed[0].get(field, "")).lower()
            assert reparsed == original, f"Field '{field}' mismatch: {reparsed!r} != {original!r}"

    def test_multi_rule_round_trip(
        self, valid_rule_data: dict, valid_enf_rule_data: dict, tmp_path: Path
    ) -> None:
        rules = [valid_rule_data, valid_enf_rule_data]
        parsed = self._write_and_parse(rules, tmp_path)
        assert len(parsed) == len(rules)
        original_ids = {r["rule_id"] for r in rules}
        parsed_ids = {r["rule_id"] for r in parsed}
        assert parsed_ids == original_ids

    def test_round_trip_cross_references_detected(self, tmp_path: Path) -> None:
        rule_a = {
            "rule_id": "ARCH-REF-001",
            "domain": "Architecture",
            "severity": "high",
            "scope": "module",
            "trigger": "When creating a class.",
            "statement": "Must follow ARCH-DI-001 and PERF-IO-001.",
            "violation": "Does not follow ARCH-DI-001.",
            "pass_example": "Follows ARCH-DI-001.",
            "enforcement": "Code review.",
            "rationale": "See PERF-IO-001 for details.",
            "last_validated": "2026-03-20",
        }
        parsed = self._write_and_parse([rule_a], tmp_path)
        refs = parsed[0].get("_cross_references", [])
        assert "ARCH-DI-001" in refs
        assert "PERF-IO-001" in refs

    def test_round_trip_code_blocks_preserved(
        self, rule_with_code_blocks: dict, tmp_path: Path
    ) -> None:
        parsed = self._write_and_parse([rule_with_code_blocks], tmp_path)
        assert len(parsed) == 1
        assert "shared_state = []" in parsed[0]["violation"]
        assert "state.append(1)" in parsed[0]["pass_example"]

    def test_double_round_trip_stable(self, valid_rule_data: dict, tmp_path: Path) -> None:
        """export -> ingest -> export -> ingest: second ingest must match first."""
        # First round trip.
        parsed_1 = self._write_and_parse([valid_rule_data], tmp_path)
        # Clean the non-ingest-visible fields to simulate graph state.
        clean_1 = {k: v for k, v in parsed_1[0].items() if not k.startswith("_")}

        # Second round trip from parsed data.
        tmp_path_2 = tmp_path / "round2"
        tmp_path_2.mkdir()
        parsed_2 = self._write_and_parse([clean_1], tmp_path_2)

        for field in self.INGEST_VISIBLE_FIELDS:
            v1 = str(parsed_1[0].get(field, "")).lower()
            v2 = str(parsed_2[0].get(field, "")).lower()
            assert v1 == v2, f"Double round-trip mismatch on '{field}': {v1!r} != {v2!r}"

    def test_all_fixture_rules_round_trip(
        self,
        valid_rule_data: dict,
        valid_enf_rule_data: dict,
        minimal_rule_data: dict,
        compound_id_rule_data: dict,
        enf_gate_final_data: dict,
        tmp_path: Path,
    ) -> None:
        """Every conftest fixture must survive round-trip."""
        all_rules = [
            valid_rule_data,
            valid_enf_rule_data,
            minimal_rule_data,
            compound_id_rule_data,
            enf_gate_final_data,
        ]
        parsed = self._write_and_parse(all_rules, tmp_path)
        assert len(parsed) == len(all_rules)
        for original in all_rules:
            match = next((p for p in parsed if p["rule_id"] == original["rule_id"]), None)
            assert match is not None, f"Missing rule after round-trip: {original['rule_id']}"
            for field in self.INGEST_VISIBLE_FIELDS:
                orig_val = str(original[field]).lower()
                parsed_val = str(match.get(field, "")).lower()
                assert parsed_val == orig_val, (
                    f"{original['rule_id']}.{field}: {parsed_val!r} != {orig_val!r}"
                )

    def test_round_tripped_rules_pass_schema_validation(
        self, valid_rule_data: dict, tmp_path: Path
    ) -> None:
        """Re-ingested rules must pass Pydantic validation."""
        parsed = self._write_and_parse([valid_rule_data], tmp_path)
        rule = validate_parsed_rule(parsed[0])
        assert rule.rule_id == valid_rule_data["rule_id"]


# ---------------------------------------------------------------------------
# Integration: Neo4j export (requires running Neo4j)
# ---------------------------------------------------------------------------

class TestExportWithNeo4j:
    """Tests that require a live Neo4j instance."""

    @pytest_asyncio.fixture()
    async def db(self):
        """Provide a Neo4j connection, clear before and after."""
        from writ.graph.db import Neo4jConnection

        conn = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    @pytest.mark.asyncio()
    async def test_export_creates_files(
        self, db, valid_rule_data: dict, tmp_path: Path
    ) -> None:
        await db.create_rule(valid_rule_data)
        result = await export_rules_to_markdown(db, tmp_path)
        assert result["rules_exported"] == 1
        assert result["files_written"] >= 1
        md_files = list(tmp_path.rglob("*.md"))
        assert len(md_files) >= 1

    @pytest.mark.asyncio()
    async def test_export_empty_graph(self, db, tmp_path: Path) -> None:
        result = await export_rules_to_markdown(db, tmp_path)
        assert result["rules_exported"] == 0
        assert result["files_written"] == 0

    @pytest.mark.asyncio()
    async def test_export_count_matches_graph(
        self, db, valid_rule_data: dict, valid_enf_rule_data: dict, tmp_path: Path
    ) -> None:
        await db.create_rule(valid_rule_data)
        await db.create_rule(valid_enf_rule_data)
        result = await export_rules_to_markdown(db, tmp_path)
        count = await db.count_rules()
        assert result["rules_exported"] == count

    @pytest.mark.asyncio()
    async def test_export_idempotent(
        self, db, valid_rule_data: dict, tmp_path: Path
    ) -> None:
        await db.create_rule(valid_rule_data)
        await export_rules_to_markdown(db, tmp_path)
        content_1 = {
            p.relative_to(tmp_path): p.read_text()
            for p in tmp_path.rglob("*.md")
        }
        await export_rules_to_markdown(db, tmp_path)
        content_2 = {
            p.relative_to(tmp_path): p.read_text()
            for p in tmp_path.rglob("*.md")
        }
        assert content_1 == content_2

    @pytest.mark.asyncio()
    async def test_export_writes_timestamp(
        self, db, valid_rule_data: dict, tmp_path: Path
    ) -> None:
        await db.create_rule(valid_rule_data)
        await export_rules_to_markdown(db, tmp_path)
        ts = read_export_timestamp(tmp_path)
        assert ts is not None

    @pytest.mark.asyncio()
    async def test_full_round_trip_through_neo4j(
        self, db, valid_rule_data: dict, tmp_path: Path
    ) -> None:
        """Write to Neo4j -> export -> re-ingest from files -> compare."""
        await db.create_rule(valid_rule_data)
        await export_rules_to_markdown(db, tmp_path)

        # Re-ingest from exported files.
        parsed: list[dict] = []
        for md_file in sorted(tmp_path.rglob("*.md")):
            parsed.extend(parse_rules_from_file(md_file))
        assert len(parsed) == 1
        assert parsed[0]["rule_id"] == valid_rule_data["rule_id"]
        assert parsed[0]["domain"] == valid_rule_data["domain"]


# ---------------------------------------------------------------------------
# CLI command registration
# ---------------------------------------------------------------------------

class TestExportCLI:

    def test_export_command_registered(self) -> None:
        from writ.cli import app

        command_names = [cmd.callback.__name__ for cmd in app.registered_commands]
        assert "export" in command_names
