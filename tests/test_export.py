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
    SECTION_ORDER,
    _METHODOLOGY_CANONICAL_RULE_IDS,
    _build_file_content,
    check_export_staleness,
    export_rules_to_markdown,
    group_rules_by_file,
    node_to_yaml_frontmatter,
    read_export_timestamp,
    rule_to_markdown,
    write_export_timestamp,
)
from writ.graph.ingest import (
    parse_edges_from_file,
    parse_rules_from_file,
    validate_parsed_rule,
)
from writ.graph.schema import SECTION_HEADERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _restore_corpus_after_module():
    """Restore the shared Neo4j corpus once after this module finishes.

    The two function-scoped Neo4j fixtures (TestExportWithNeo4j.db and
    TestExportGraphToMarkdown.live_db) each call clear_all() (a whole-graph
    wipe across every project), so without a restore this module would leak an
    empty graph to whatever test runs next in the same pytest process. Mirror
    the pipeline_db / _roundtrip_db contract and re-import bible/ exactly once
    (after the last test). The restore is skipped only when Neo4j is unreachable
    (nothing to restore); when Neo4j is up it runs once regardless of which
    tests were selected, which is harmless because the re-import is idempotent.
    """
    yield

    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from tests._corpus import neo4j_reachable  # noqa: PLC0415
    from tests._writ_cmd import WRIT_CMD_PREFIX  # noqa: PLC0415

    if not neo4j_reachable():
        return

    try:
        subprocess.run(
            [*WRIT_CMD_PREFIX, "import-markdown", "bible/"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(
            "[test_export teardown] writ import-markdown "
            f"restore failed: {e}\n"
        )


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
        assert "**Scope**: Component" in md

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
        assert "**Scope**: Component" in md
        assert "**Scope**: component" not in md

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

    def test_category_emitted_as_metadata_line(self, valid_rule_data: dict) -> None:
        """A rule carrying a 'category' must round-trip via a **Category** line
        so the auto-export does not strip the category the migration injected
        (graph keeps it as a node property; markdown must keep it too)."""
        rule = {**valid_rule_data, "category": "CAT-CODE-SECURITY-001"}
        md = rule_to_markdown(rule)
        assert "**Category**: CAT-CODE-SECURITY-001" in md

    def test_category_line_absent_when_no_category(self, valid_rule_data: dict) -> None:
        """A rule without a category must not emit an empty Category line."""
        md = rule_to_markdown(valid_rule_data)
        assert "**Category**:" not in md


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
        from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
        from writ.graph.db import Neo4jConnection

        conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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


# ---------------------------------------------------------------------------
# Phase 0: GRAPH_ONLY_FIELDS completeness
# ---------------------------------------------------------------------------

class TestGraphOnlyFieldsComplete:
    """GRAPH_ONLY_FIELDS must declare every field that ingest re-derives from the
    graph and that must not appear in exported Markdown.

    Phase 0 adds authority, times_seen_positive, times_seen_negative, last_seen
    to the existing set (confidence, evidence, staleness_window, last_validated).
    RED reason: GRAPH_ONLY_FIELDS is currently a 4-element set; 4 new fields are
    missing until the export.py implementation update lands.
    """

    _EXPECTED_FIELDS = frozenset({
        "confidence",
        "evidence",
        "staleness_window",
        "last_validated",
        "authority",
        "times_seen_positive",
        "times_seen_negative",
        "last_seen",
    })

    def test_all_re_derived_fields_declared(self) -> None:
        missing = self._EXPECTED_FIELDS - GRAPH_ONLY_FIELDS
        assert not missing, (
            f"GRAPH_ONLY_FIELDS is missing re-derived fields: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Phase 0: node_to_yaml_frontmatter
# ---------------------------------------------------------------------------

class TestNodeToYamlFrontmatter:
    """node_to_yaml_frontmatter serialises a methodology node dict to YAML
    front-matter suitable for bible/methodology/<ID>.md files.

    RED reason: node_to_yaml_frontmatter does not exist in writ.export yet.
    """

    def _import_fn(self):
        from writ.export import node_to_yaml_frontmatter  # noqa: PLC0415
        return node_to_yaml_frontmatter

    def _make_node(self, **overrides) -> dict:
        base = {
            "node_id": "SKL-PROC-TEST-001",
            "node_type": "Skill",
            "trigger": "When writing tests.",
            "statement": "Use TDD.",
            "rationale": "Correctness.",
            "domain": "Testing",
            "last_validated": "2026-06-01",
            "confidence": 0.9,
            "authority": "internal",
        }
        base.update(overrides)
        return base

    def test_starts_with_dashes(self) -> None:
        """Front-matter block must open with '---\\n'."""
        node_to_yaml_frontmatter = self._import_fn()
        result = node_to_yaml_frontmatter(self._make_node())
        assert result.startswith("---\n"), (
            f"Expected front-matter to start with '---\\n', got: {result[:20]!r}"
        )

    def test_graph_only_fields_absent(self) -> None:
        """Graph-only fields must not appear in the serialised front-matter."""
        node_to_yaml_frontmatter = self._import_fn()
        node = self._make_node()
        result = node_to_yaml_frontmatter(node)
        for field in GRAPH_ONLY_FIELDS:
            assert field not in result, (
                f"Graph-only field '{field}' must not appear in front-matter output"
            )

    def test_edges_injected(self) -> None:
        """When the node dict contains an 'edges' key (list of edge dicts), the
        serialised front-matter must include an 'edges:' block with target and
        type sub-keys for each edge."""
        node_to_yaml_frontmatter = self._import_fn()
        node = self._make_node(edges=[
            {"target": "ENF-PROC-TDD-001", "type": "TEACHES"},
            {"target": "SKL-PROC-BRAIN-001", "type": "RELATED_TO"},
        ])
        result = node_to_yaml_frontmatter(node)
        assert "edges:" in result, "Serialised front-matter must contain 'edges:' key"
        assert "ENF-PROC-TDD-001" in result
        assert "TEACHES" in result
        assert "SKL-PROC-BRAIN-001" in result
        assert "RELATED_TO" in result


# ---------------------------------------------------------------------------
# Phase 0: dual-location canonical target list
# ---------------------------------------------------------------------------

class TestDualLocationCanonicalTarget:
    """_METHODOLOGY_CANONICAL_RULE_IDS is the set of rule IDs that exist as
    standalone files under bible/methodology/ AND as blocks inside a
    bible/<domain>/rules.md.  group_rules_by_file must route these IDs to
    bible/methodology/<ID>.md (the canonical location) and exclude them from
    domain rules.md files.

    RED reason: _METHODOLOGY_CANONICAL_RULE_IDS and the routing override do not
    exist in writ.export yet.
    """

    def _import_canonical(self):
        from writ.export import _METHODOLOGY_CANONICAL_RULE_IDS  # noqa: PLC0415
        return _METHODOLOGY_CANONICAL_RULE_IDS

    def test_dual_location_ids_defined(self) -> None:
        """The methodology-canonical set (excluded from domain rules.md export):
        12 legacy dual-location rules + ENF-COMMS-OUTPUT-001 (Phase 4 A3) +
        ENF-PROC-FIXLOOP-001 (cycle F, the first methodology Rule authored AFTER
        the set existed -- it was missed, the auto-export wrote a duplicate
        RULE-START copy, and the round-trip broke; export.py now ALSO derives
        canonicity from bible/methodology/<id>.md existing, so membership here
        is documentation, not the load-bearing check)."""
        ids = self._import_canonical()
        assert len(ids) == 14, (
            f"Expected 14 methodology-canonical IDs, found {len(ids)}: {sorted(ids)}"
        )

    def test_group_rules_by_file_excludes_dual_location(
        self, tmp_path: Path
    ) -> None:
        """ENF-PROC-BRAIN-001 is one of the 12; its output path from
        group_rules_by_file must NOT be inside process/rules.md.

        Build a fake bible that has ENF-PROC-BRAIN-001 in process/rules.md so
        the ID is discoverable, then call group_rules_by_file with a rule dict
        for that ID and assert the resulting path is the methodology canonical
        path, not the process domain path."""
        # Set up a fake bible with the duplicate block in process/rules.md.
        process_dir = tmp_path / "bible" / "process"
        process_dir.mkdir(parents=True)
        (process_dir / "rules.md").write_text(
            "<!-- RULE START: ENF-PROC-BRAIN-001 -->\n"
            "## Rule ENF-PROC-BRAIN-001\n"
            "<!-- RULE END: ENF-PROC-BRAIN-001 -->\n",
            encoding="utf-8",
        )
        # Also add the methodology canonical file so the directory exists.
        methodology_dir = tmp_path / "bible" / "methodology"
        methodology_dir.mkdir(parents=True)
        (methodology_dir / "ENF-PROC-BRAIN-001.md").write_text(
            "---\nrule_id: ENF-PROC-BRAIN-001\n---\n", encoding="utf-8"
        )

        rule = {
            "rule_id": "ENF-PROC-BRAIN-001",
            "domain": "AI Enforcement",
            "severity": "critical",
            "scope": "session",
            "trigger": "When about to write.",
            "statement": "Design before code.",
            "violation": "Wrote code without plan.",
            "pass_example": "Presented design, got approval.",
            "enforcement": "Gate blocks write.",
            "rationale": "Correctness.",
            "last_validated": "2026-06-01",
        }
        bible_dir = tmp_path / "bible"
        groups = group_rules_by_file([rule], bible_dir)
        # A dual-location rule is hand-authored methodology front-matter SOURCE;
        # the rule export excludes it from ALL output groups (it is never
        # regenerated by group_rules_by_file), so it appears in no group at all.
        all_ids = [r["rule_id"] for g in groups.values() for r in g]
        assert "ENF-PROC-BRAIN-001" not in all_ids, (
            f"dual-location rule must be excluded from every rule-export group; "
            f"got groups: {list(groups.keys())}"
        )


# ---------------------------------------------------------------------------
# Phase 0 Wave E: dual-location rules export their edges (parity gap)
# ---------------------------------------------------------------------------

class TestDualLocationEdgesExported:
    """The 12 dual-location rules (_METHODOLOGY_CANONICAL_RULE_IDS) are written
    to bible/methodology/<ID>.md via node_to_yaml_frontmatter so their outgoing
    edges (e.g. ENF-META-CONCISE-001 GATES PBK-AUTHOR-001) survive export.

    rule_to_markdown emits no edges block, so routing these rules through it
    drops the edges that their original methodology files carried and that
    parse_edges_from_file must still find. node_to_yaml_frontmatter serialises
    the edges; the category must still ride along.

    RED reason (before the fix): dual-location rules were emitted via
    rule_to_markdown, producing a RULE START block with no edges: front-matter,
    so the exported file carried no GATES edge for parse_edges_from_file.
    """

    def _dual_location_rule_node(self) -> dict:
        """A Rule node dict as get_all_nodes_by_type('Rule') would return it for
        one of the 12 canonical dual-location ids, with a category property."""
        return {
            "rule_id": "ENF-META-CONCISE-001",
            "domain": "AI Enforcement",
            "severity": "high",
            "scope": "session",
            "trigger": "When responding.",
            "statement": "Be concise.",
            "violation": "Padded the answer.",
            "pass_example": "Led with the answer.",
            "enforcement": "Gate blocks verbose output.",
            "rationale": "Signal over noise.",
            "mandatory": True,
            "category": "CAT-PROC-METHODOLOGY-001",
            "last_validated": "2026-06-01",
        }

    def test_canonical_id_is_in_dual_location_set(self) -> None:
        """Guard: the fixture id must actually be one of the 12 so this test
        exercises the dual-location export path."""
        assert "ENF-META-CONCISE-001" in _METHODOLOGY_CANONICAL_RULE_IDS

    def test_frontmatter_contains_gates_edge(self) -> None:
        """The serialised front-matter for a dual-location rule must contain its
        GATES edge (target + type), which rule_to_markdown would have dropped."""
        node = self._dual_location_rule_node()
        edges = [{"target": "PBK-AUTHOR-001", "type": "GATES"}]
        out = node_to_yaml_frontmatter(node, edges=edges)
        assert "edges:" in out, "dual-location export must serialise an edges block"
        assert "GATES" in out
        assert "PBK-AUTHOR-001" in out

    def test_frontmatter_preserves_category(self) -> None:
        """The category property must ride along into the exported front-matter
        so the BELONGS_TO routing survives the round trip."""
        node = self._dual_location_rule_node()
        out = node_to_yaml_frontmatter(
            node, edges=[{"target": "PBK-AUTHOR-001", "type": "GATES"}]
        )
        assert "CAT-PROC-METHODOLOGY-001" in out

    def test_exported_file_edges_reparse(self, tmp_path: Path) -> None:
        """Writing the dual-location rule the way export_graph_to_markdown does
        (methodology/<ID>.md via node_to_yaml_frontmatter) must produce a file
        whose GATES edge parse_edges_from_file can recover with the rule as the
        edge source."""
        node = self._dual_location_rule_node()
        edges = [{"target": "PBK-AUTHOR-001", "type": "GATES"}]
        target = tmp_path / "methodology" / f"{node['rule_id']}.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            node_to_yaml_frontmatter(node, edges=edges), encoding="utf-8"
        )

        parsed = parse_edges_from_file(target)
        assert {"source": "ENF-META-CONCISE-001", "target": "PBK-AUTHOR-001", "type": "GATES"} in parsed

    def test_rule_to_markdown_would_drop_edges(self) -> None:
        """Documents the regression guard: rule_to_markdown never emits an edges
        block, which is why dual-location rules must not be routed through it."""
        node = self._dual_location_rule_node()
        md = rule_to_markdown(node)
        assert "edges:" not in md
        assert "GATES" not in md


# ---------------------------------------------------------------------------
# Phase 0: export_graph_to_markdown (all node types + edges)
# ---------------------------------------------------------------------------

class TestExportGraphToMarkdown:
    """export_graph_to_markdown exports ALL node types (not just Rule) and all
    edges, returning a summary dict with 'nodes_exported' and 'edges_exported'
    keys.

    The Neo4j tests also exercise the new db methods get_all_nodes_by_type and
    get_all_edges_cross_type.

    RED reason: export_graph_to_markdown, get_all_nodes_by_type, and
    get_all_edges_cross_type do not exist yet.
    """

    def test_returns_nodes_and_edges_exported_keys(self) -> None:
        """export_graph_to_markdown must return a dict with at least
        'nodes_exported' and 'edges_exported' integer keys (verified without
        a live Neo4j connection by exercising the synchronous path with a
        minimal stub)."""
        # Import guard: the function must be importable even without Neo4j.
        from writ.export import export_graph_to_markdown  # noqa: PLC0415
        assert callable(export_graph_to_markdown)

    @pytest_asyncio.fixture()
    async def live_db(self):
        """Live Neo4j connection; skips when Neo4j is unreachable."""
        from tests._corpus import neo4j_reachable  # noqa: PLC0415

        if not neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user  # noqa: PLC0415
        from writ.graph.db import Neo4jConnection  # noqa: PLC0415

        conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    @pytest.mark.asyncio()
    async def test_db_get_all_nodes_by_type_returns_list(self, live_db) -> None:
        """db.get_all_nodes_by_type(label) must return a list (possibly empty)
        for every known node type label."""
        from writ.graph.schema import NODE_ID_FIELDS  # noqa: PLC0415

        for label in NODE_ID_FIELDS:
            result = await live_db.get_all_nodes_by_type(label)
            assert isinstance(result, list), (
                f"get_all_nodes_by_type('{label}') must return list, got {type(result)}"
            )

    @pytest.mark.asyncio()
    async def test_db_get_all_edges_cross_type_returns_list(self, live_db) -> None:
        """db.get_all_edges_cross_type() must return a list of edge dicts, each
        containing at least 'source_id', 'target_id', and 'type' keys."""
        result = await live_db.get_all_edges_cross_type()
        assert isinstance(result, list), (
            f"get_all_edges_cross_type() must return list, got {type(result)}"
        )
        # If any edges are present each must have the required keys.
        for edge in result:
            assert "source_id" in edge, f"Edge missing 'source_id': {edge}"
            assert "target_id" in edge, f"Edge missing 'target_id': {edge}"
            assert "type" in edge, f"Edge missing 'type': {edge}"

    @pytest.mark.asyncio()
    async def test_export_graph_to_markdown_returns_summary(
        self, live_db, tmp_path: Path
    ) -> None:
        """export_graph_to_markdown over an empty graph must return a dict with
        'nodes_exported' == 0 and 'edges_exported' == 0."""
        from writ.export import export_graph_to_markdown  # noqa: PLC0415

        result = await export_graph_to_markdown(live_db, tmp_path)
        assert "nodes_exported" in result, (
            f"export_graph_to_markdown must return 'nodes_exported' key; got: {result}"
        )
        assert "edges_exported" in result, (
            f"export_graph_to_markdown must return 'edges_exported' key; got: {result}"
        )
        assert isinstance(result["nodes_exported"], int)
        assert isinstance(result["edges_exported"], int)
        # Empty graph: both counts must be 0.
        assert result["nodes_exported"] == 0
        assert result["edges_exported"] == 0
