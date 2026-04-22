"""Shared test fixtures for Writ test suite."""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session, exitstatus):
    """Re-migrate rules after test suite completes so CLI queries work immediately."""
    import asyncio
    from pathlib import Path

    try:
        from writ.config import get_neo4j_uri, get_neo4j_user, get_neo4j_password
        from writ.graph.db import Neo4jConnection
        from writ.graph.ingest import (
            NODE_ID_FIELDS,
            discover_rule_files,
            parse_edges_from_file,
            parse_nodes_from_file,
            parse_rules_from_file,
            validate_parsed_node,
            validate_parsed_rule,
        )
    except (ImportError, ModuleNotFoundError):
        return  # neo4j driver or other deps not installed; skip re-migration.

    async def _remigrate():
        bible = Path("bible/")
        methodology = Path("tests/fixtures/synthetic_methodology")
        if not bible.exists():
            return
        try:
            db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
            count = await db.count_rules()
            if count == 0:
                # Re-ingest the core coding-rule corpus from bible/.
                for f in discover_rule_files(bible):
                    for rd in parse_rules_from_file(f):
                        try:
                            validate_parsed_rule(rd)
                            clean = {k: v for k, v in rd.items() if not k.startswith("_")}
                            await db.create_rule(clean)
                        except ValueError:
                            pass
                # Re-ingest the Phase 1 methodology corpus so methodology
                # retrieval doesn't break after graph wipes. Nodes that
                # already exist MERGE cleanly.
                if methodology.exists():
                    edges_to_create = []
                    for f in sorted(methodology.glob("*.md")):
                        try:
                            for node in parse_nodes_from_file(f):
                                try:
                                    validate_parsed_node(node)
                                except ValueError:
                                    continue
                                node_type = node.get("node_type", "Rule")
                                clean = {
                                    k: v for k, v in node.items()
                                    if k != "node_type" and not k.startswith("_") and k != "edges"
                                }
                                if node_type == "Rule":
                                    await db.create_rule(clean)
                                else:
                                    await db.create_methodology_node(node_type, clean)
                            edges_to_create.extend(parse_edges_from_file(f))
                        except Exception:
                            continue
                    for e in edges_to_create:
                        try:
                            await db.create_edge(e["type"], e["source"], e["target"])
                        except Exception:
                            pass
            await db.close()
        except Exception:
            pass  # Neo4j may not be running; silently skip.

    try:
        asyncio.run(_remigrate())
    except Exception:
        pass


@pytest.fixture()
def valid_rule_data() -> dict:
    """A well-formed rule with all required fields."""
    return {
        "rule_id": "ARCH-ORG-001",
        "domain": "Architecture",
        "severity": "critical",
        "scope": "module",
        "trigger": "When creating a class that contains logic from a different layer.",
        "statement": "Each class must belong to exactly one architectural layer.",
        "violation": "Controller contains SQL query.",
        "pass_example": "Controller delegates to service, service delegates to repository.",
        "enforcement": "Per-slice findings table must verify layer separation.",
        "rationale": "Mixed layers create untestable, unreusable, fragile classes.",
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def valid_enf_rule_data() -> dict:
    """An ENF-* rule with mandatory=true."""
    return {
        "rule_id": "ENF-GATE-001",
        "domain": "AI Enforcement",
        "severity": "critical",
        "scope": "session",
        "trigger": "When the AI completes Phase A analysis.",
        "statement": "Phase A output must be approved before Phase B begins.",
        "violation": "AI proceeds to Phase B without human approval of Phase A.",
        "pass_example": "AI halts after Phase A and waits for approval.",
        "enforcement": "Gate file must exist before Phase B output is generated.",
        "rationale": "Human review catches incorrect call-path declarations.",
        "mandatory": True,
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def minimal_rule_data() -> dict:
    """Rule with only required fields -- graph-only fields use defaults."""
    return {
        "rule_id": "TEST-TDD-001",
        "domain": "Testing",
        "severity": "high",
        "scope": "slice",
        "trigger": "When generating implementation code for a new class.",
        "statement": "Test skeletons must exist before the implementation they test.",
        "violation": "Implementation written first, tests added after.",
        "pass_example": "Test skeleton written and approved before implementation.",
        "enforcement": "ENF-GATE-007 test-first gate.",
        "rationale": "Tests written after implementation confirm what was built, not what should be built.",
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def compound_id_rule_data(valid_rule_data: dict) -> dict:
    """Rule with a multi-segment ID like FW-M2-RT-003."""
    return {**valid_rule_data, "rule_id": "FW-M2-RT-003"}


@pytest.fixture()
def enf_gate_final_data(valid_rule_data: dict) -> dict:
    """Rule with non-numeric suffix: ENF-GATE-FINAL."""
    return {**valid_rule_data, "rule_id": "ENF-GATE-FINAL"}
