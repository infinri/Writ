"""Phase 1 deliverable 2: multi-node ingest parser tests.

Covers the extended ingest that handles:
- Legacy <!-- RULE START: id --> ... <!-- RULE END: id --> (back-compat)
- New <!-- NODE START type=X id=Y --> ... <!-- NODE END --> markers
- YAML front-matter (one node per file, used by Phase 0 synthetic corpus)
- Edge markers (front-matter edges: list and/or inline <!-- EDGE: ... --> comments)
- Type-dispatched validation against the 11 Pydantic models

Integration test: parse every file in tests/fixtures/synthetic_methodology/ and
validate each. All 60 Phase-0 fixtures must round-trip.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from writ.graph.ingest import (
    NODE_TYPE_MODELS,
    parse_edges_from_file,
    parse_nodes_from_file,
    parse_rules_from_file,
    validate_parsed_node,
    validate_parsed_rule,
)
from writ.graph.schema import (
    AntiPattern,
    Phase,
    Playbook,
    Rule,
    Skill,
)

SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "synthetic_methodology"


# --- Front-matter parsing ------------------------------------------------------


class TestFrontMatterParsing:
    def test_parse_skill_from_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "SKL-X-001.md").write_text(
            "---\n"
            "skill_id: SKL-PROC-BRAIN-X01\n"
            "node_type: Skill\n"
            "domain: process\n"
            "severity: high\n"
            "scope: session\n"
            "trigger: When starting a feature\n"
            "statement: Present approaches\n"
            "rationale: Prevents premature code\n"
            "last_validated: 2026-04-21\n"
            "---\n"
            "# Body\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "SKL-X-001.md")
        assert len(nodes) == 1
        n = nodes[0]
        assert n["node_type"] == "Skill"
        assert n["skill_id"] == "SKL-PROC-BRAIN-X01"

    def test_parse_rule_from_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "r.md").write_text(
            "---\n"
            "rule_id: ENF-PROC-X-001\n"
            "domain: process\n"
            "severity: critical\n"
            "scope: session\n"
            "trigger: Gate fires\n"
            "statement: Must do X\n"
            "violation: did not do X\n"
            "pass_example: did X\n"
            "enforcement: hook\n"
            "rationale: reasons\n"
            "last_validated: 2026-04-21\n"
            "---\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "r.md")
        assert nodes[0]["node_type"] == "Rule"
        assert nodes[0]["rule_id"] == "ENF-PROC-X-001"

    def test_frontmatter_extracts_body(self, tmp_path: Path) -> None:
        (tmp_path / "n.md").write_text(
            "---\nskill_id: SKL-X-Y-001\nnode_type: Skill\ndomain: process\nseverity: high\n"
            "scope: session\ntrigger: T\nstatement: S\nrationale: R\nlast_validated: 2026-04-21\n"
            "---\n# Body content here\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "n.md")
        assert "Body content here" in nodes[0].get("body", "")


# --- NODE START marker parsing -------------------------------------------------


class TestNodeMarkerParsing:
    def test_parse_single_node_marker(self, tmp_path: Path) -> None:
        (tmp_path / "n.md").write_text(
            "# header\n\n"
            "<!-- NODE START type=Skill id=SKL-PROC-X-001 -->\n"
            "**Domain**: process\n"
            "**Severity**: high\n"
            "**Scope**: session\n\n"
            "### Trigger\nWhen X happens\n\n"
            "### Statement\nDo Y\n\n"
            "### Rationale\nBecause Z\n\n"
            "<!-- NODE END: SKL-PROC-X-001 -->\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "n.md")
        assert len(nodes) == 1
        assert nodes[0]["node_type"] == "Skill"
        assert nodes[0]["skill_id"] == "SKL-PROC-X-001"

    def test_parse_multiple_node_markers(self, tmp_path: Path) -> None:
        (tmp_path / "n.md").write_text(
            "<!-- NODE START type=Skill id=SKL-X-X-001 -->\n"
            "**Domain**: process\n**Severity**: high\n**Scope**: session\n\n"
            "### Trigger\nA\n### Statement\nB\n### Rationale\nC\n\n"
            "<!-- NODE END: SKL-X-X-001 -->\n\n"
            "<!-- NODE START type=Technique id=TEC-X-X-001 -->\n"
            "**Domain**: process\n**Severity**: medium\n**Scope**: task\n\n"
            "### Trigger\nD\n### Statement\nE\n### Rationale\nF\n\n"
            "<!-- NODE END: TEC-X-X-001 -->\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "n.md")
        assert len(nodes) == 2
        assert {n["node_type"] for n in nodes} == {"Skill", "Technique"}


# --- Legacy RULE START back-compat ---------------------------------------------


class TestLegacyRuleStart:
    def test_rule_start_still_parses(self, tmp_path: Path) -> None:
        (tmp_path / "r.md").write_text(
            "<!-- RULE START: ARCH-X-001 -->\n"
            "## Rule ARCH-X-001: legacy style\n\n"
            "**Domain**: architecture\n**Severity**: high\n**Scope**: module\n\n"
            "### Trigger\nA\n### Statement\nB\n### Violation\nC\n"
            "### Pass\nD\n### Enforcement\nE\n### Rationale\nF\n\n"
            "<!-- RULE END: ARCH-X-001 -->\n"
        )
        rules = parse_rules_from_file(tmp_path / "r.md")
        assert len(rules) == 1
        assert rules[0]["rule_id"] == "ARCH-X-001"

    def test_rule_start_routes_through_nodes_parser(self, tmp_path: Path) -> None:
        (tmp_path / "r.md").write_text(
            "<!-- RULE START: ARCH-X-002 -->\n"
            "**Domain**: architecture\n**Severity**: high\n**Scope**: file\n\n"
            "### Trigger\nA\n### Statement\nB\n### Violation\nC\n"
            "### Pass\nD\n### Enforcement\nE\n### Rationale\nF\n\n"
            "<!-- RULE END: ARCH-X-002 -->\n"
        )
        nodes = parse_nodes_from_file(tmp_path / "r.md")
        assert len(nodes) == 1
        assert nodes[0]["node_type"] == "Rule"
        assert nodes[0]["rule_id"] == "ARCH-X-002"


# --- Edge parsing --------------------------------------------------------------


class TestEdgeParsing:
    def test_edges_from_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "n.md").write_text(
            "---\nskill_id: SKL-X-X-001\nnode_type: Skill\ndomain: process\n"
            "severity: high\nscope: session\ntrigger: T\nstatement: S\n"
            "rationale: R\nlast_validated: 2026-04-21\n"
            "edges:\n"
            "  - { target: PBK-Y-Y-001, type: TEACHES }\n"
            "  - { target: ENF-Z-Z-001, type: GATES }\n"
            "---\n"
        )
        edges = parse_edges_from_file(tmp_path / "n.md")
        assert len(edges) == 2
        types = {e["type"] for e in edges}
        assert types == {"TEACHES", "GATES"}

    def test_edges_carry_source(self, tmp_path: Path) -> None:
        (tmp_path / "n.md").write_text(
            "---\nskill_id: SKL-X-X-001\nnode_type: Skill\ndomain: process\n"
            "severity: high\nscope: session\ntrigger: T\nstatement: S\n"
            "rationale: R\nlast_validated: 2026-04-21\n"
            "edges:\n  - { target: PBK-Y-Y-001, type: TEACHES }\n---\n"
        )
        edges = parse_edges_from_file(tmp_path / "n.md")
        assert edges[0]["source"] == "SKL-X-X-001"
        assert edges[0]["target"] == "PBK-Y-Y-001"


# --- Type dispatch -------------------------------------------------------------


class TestNodeTypeDispatch:
    def test_node_type_models_covers_all_types(self) -> None:
        expected = {
            "Rule", "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
            "Phase", "Rationalization", "PressureScenario", "WorkedExample", "SubagentRole",
        }
        assert expected <= NODE_TYPE_MODELS.keys()

    def test_validate_parsed_node_dispatches_by_type(self) -> None:
        skill_dict = {
            "skill_id": "SKL-PROC-X-001",
            "node_type": "Skill",
            "domain": "process",
            "severity": "high",
            "scope": "session",
            "trigger": "t",
            "statement": "s",
            "rationale": "r",
            "last_validated": "2026-04-21",
        }
        result = validate_parsed_node(skill_dict)
        assert isinstance(result, Skill)

    def test_validate_parsed_node_rejects_unknown_type(self) -> None:
        with pytest.raises(ValueError):
            validate_parsed_node({"node_type": "Nonsense", "foo": "bar"})

    def test_validate_parsed_rule_back_compat(self, valid_rule_data: dict) -> None:
        # Legacy callers must continue to work.
        result = validate_parsed_rule(valid_rule_data)
        assert isinstance(result, Rule)


# --- Integration: synthetic corpus round-trip ---------------------------------


class TestSyntheticCorpusRoundTrip:
    """Every synthetic_methodology fixture must parse AND validate through its Pydantic model."""

    def test_every_fixture_parses(self) -> None:
        files = sorted(SYNTHETIC_DIR.glob("*.md"))
        assert len(files) >= 50, f"Expected >=50 fixtures, found {len(files)}"
        parse_errors = []
        for f in files:
            try:
                nodes = parse_nodes_from_file(f)
                assert len(nodes) >= 1, f"{f.name} parsed to zero nodes"
            except Exception as e:
                parse_errors.append(f"{f.name}: {e}")
        assert not parse_errors, f"Parse errors: {parse_errors}"

    def test_every_fixture_validates(self) -> None:
        files = sorted(SYNTHETIC_DIR.glob("*.md"))
        validation_errors = []
        for f in files:
            try:
                for node in parse_nodes_from_file(f):
                    validate_parsed_node(node)
            except (ValueError, ValidationError) as e:
                validation_errors.append(f"{f.name}: {type(e).__name__}: {str(e)[:200]}")
        assert not validation_errors, f"Validation errors:\n" + "\n".join(validation_errors)

    def test_every_fixture_has_edges_extracted(self) -> None:
        """Files with edges: in front-matter should surface them via parse_edges_from_file."""
        files = sorted(SYNTHETIC_DIR.glob("*.md"))
        edge_count = 0
        for f in files:
            edges = parse_edges_from_file(f)
            edge_count += len(edges)
        # 60-node corpus with ~3 edges per retrievable node on average → expect >50 total.
        assert edge_count >= 50, f"Edge extraction surfaced only {edge_count} edges across {len(files)} files"
