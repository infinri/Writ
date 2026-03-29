"""Tests for Phase 9: Agentic retrieval loop (session tracker + endpoint extensions).

Per TEST-TDD-001: test skeletons approved before implementation.
Per TEST-ISO-001: each test sets up its own state, no shared mutables.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.retrieval.ranking import apply_context_budget
from writ.retrieval.session import (
    APPROX_TOKENS_PER_RULE_STANDARD,
    DEFAULT_SESSION_BUDGET,
    SessionTracker,
)


# ---------------------------------------------------------------------------
# Unit tests: SessionTracker
# ---------------------------------------------------------------------------

class TestSessionTracker:

    def test_initial_state_empty(self) -> None:
        t = SessionTracker()
        assert t.loaded_rule_ids == []
        assert t.remaining_budget == DEFAULT_SESSION_BUDGET

    def test_next_query_includes_loaded_rule_ids(self) -> None:
        t = SessionTracker()
        t.load_results({
            "rules": [{"rule_id": "ARCH-ORG-001", "score": 0.9}],
            "mode": "standard",
        })
        payload = t.next_query("some query")
        assert "ARCH-ORG-001" in payload["loaded_rule_ids"]

    def test_load_results_adds_rule_ids(self) -> None:
        t = SessionTracker()
        t.load_results({
            "rules": [
                {"rule_id": "ARCH-ORG-001"},
                {"rule_id": "PERF-IO-001"},
            ],
            "mode": "full",
        })
        assert set(t.loaded_rule_ids) == {"ARCH-ORG-001", "PERF-IO-001"}

    def test_load_results_decrements_budget(self) -> None:
        t = SessionTracker(initial_budget=5000)
        t.load_results({
            "rules": [{"rule_id": "R-001"}, {"rule_id": "R-002"}],
            "mode": "standard",
        })
        expected = 5000 - 2 * APPROX_TOKENS_PER_RULE_STANDARD
        assert t.remaining_budget == expected

    def test_no_duplicate_rule_ids_across_loads(self) -> None:
        t = SessionTracker()
        t.load_results({"rules": [{"rule_id": "R-001"}], "mode": "standard"})
        t.load_results({"rules": [{"rule_id": "R-001"}], "mode": "standard"})
        assert t.loaded_rule_ids == ["R-001"]

    def test_next_query_passes_budget(self) -> None:
        t = SessionTracker(initial_budget=3000)
        payload = t.next_query("test query")
        assert payload["budget_tokens"] == 3000

    def test_reset_clears_state(self) -> None:
        t = SessionTracker(initial_budget=5000)
        t.load_results({"rules": [{"rule_id": "R-001"}], "mode": "full"})
        t.reset()
        assert t.loaded_rule_ids == []
        assert t.remaining_budget == 5000

    def test_loaded_rule_ids_property_sorted(self) -> None:
        t = SessionTracker()
        t.load_results({
            "rules": [{"rule_id": "Z-001"}, {"rule_id": "A-001"}],
            "mode": "standard",
        })
        assert t.loaded_rule_ids == ["A-001", "Z-001"]

    def test_load_results_handles_abstraction_members(self) -> None:
        """When summary mode returns abstractions with rule_ids, those are loaded too."""
        t = SessionTracker()
        t.load_results({
            "rules": [
                {"abstraction_id": "ABS-ARCH-000", "rule_ids": ["ARCH-ORG-001", "ARCH-DI-001"]},
            ],
            "mode": "summary",
        })
        assert "ARCH-ORG-001" in t.loaded_rule_ids
        assert "ARCH-DI-001" in t.loaded_rule_ids

    def test_next_query_includes_domain(self) -> None:
        t = SessionTracker()
        payload = t.next_query("test", domain="Architecture")
        assert payload["domain"] == "Architecture"

    def test_budget_floors_at_zero(self) -> None:
        t = SessionTracker(initial_budget=100)
        t.load_results({
            "rules": [{"rule_id": f"R-{i:03d}"} for i in range(20)],
            "mode": "full",
        })
        assert t.remaining_budget == 0


# ---------------------------------------------------------------------------
# Pipeline: loaded_rule_ids exclusion
# ---------------------------------------------------------------------------

class TestLoadedRuleIdsExclusion:
    """Tests pipeline.query() with loaded_rule_ids parameter."""

    @pytest_asyncio.fixture()
    async def pipeline(self):
        """Build a pipeline with migrated rules."""
        from pathlib import Path

        from writ.graph.db import Neo4jConnection
        from writ.graph.ingest import discover_rule_files, parse_rules_from_file, validate_parsed_rule
        from writ.retrieval.pipeline import build_pipeline

        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        count = await db.count_rules()
        if count == 0:
            bible = Path("bible/")
            for f in discover_rule_files(bible):
                for rd in parse_rules_from_file(f):
                    validate_parsed_rule(rd)
                    clean = {k: v for k, v in rd.items() if not k.startswith("_")}
                    await db.create_rule(clean)
        try:
            p = await build_pipeline(db)
            yield p
        finally:
            await db.close()

    @pytest.mark.asyncio()
    async def test_loaded_rule_ids_excluded(self, pipeline) -> None:
        result_all = pipeline.query(query_text="layer separation architecture")
        if not result_all["rules"]:
            pytest.skip("No results")
        first_id = result_all["rules"][0]["rule_id"]
        result_excluded = pipeline.query(
            query_text="layer separation architecture",
            loaded_rule_ids=[first_id],
        )
        returned_ids = {r["rule_id"] for r in result_excluded["rules"]}
        assert first_id not in returned_ids

    @pytest.mark.asyncio()
    async def test_exclude_and_loaded_union(self, pipeline) -> None:
        result = pipeline.query(
            query_text="architecture",
            exclude_rule_ids=["ARCH-ORG-001"],
            loaded_rule_ids=["ARCH-DI-001"],
        )
        returned_ids = {r["rule_id"] for r in result["rules"]}
        assert "ARCH-ORG-001" not in returned_ids
        assert "ARCH-DI-001" not in returned_ids

    @pytest.mark.asyncio()
    async def test_loaded_empty_no_effect(self, pipeline) -> None:
        r1 = pipeline.query(query_text="architecture")
        r2 = pipeline.query(query_text="architecture", loaded_rule_ids=[])
        ids1 = {r["rule_id"] for r in r1["rules"]}
        ids2 = {r["rule_id"] for r in r2["rules"]}
        assert ids1 == ids2

    @pytest.mark.asyncio()
    async def test_backward_compatible(self, pipeline) -> None:
        result = pipeline.query(query_text="architecture")
        assert "rules" in result
        assert "mode" in result
        assert "latency_ms" in result


# ---------------------------------------------------------------------------
# /rule/{rule_id} abstraction membership
# ---------------------------------------------------------------------------

class TestRuleAbstractionMembership:

    @pytest_asyncio.fixture()
    async def db(self):
        from writ.graph.db import Neo4jConnection

        conn = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    @pytest.mark.asyncio()
    async def test_rule_with_abstraction_returns_membership(self, db) -> None:
        await db.create_rule({
            "rule_id": "ARCH-ORG-001", "domain": "Architecture", "severity": "high",
            "scope": "module", "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-20",
        })
        await db.create_rule({
            "rule_id": "ARCH-DI-001", "domain": "Architecture", "severity": "high",
            "scope": "module", "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-20",
        })
        await db.create_abstraction({
            "abstraction_id": "ABS-ARCH-001", "summary": "Arch rules",
            "domain": "Architecture", "compression_ratio": 2.0, "rule_count": 2,
        })
        await db.create_abstracts_edge("ABS-ARCH-001", "ARCH-ORG-001")
        await db.create_abstracts_edge("ABS-ARCH-001", "ARCH-DI-001")

        result = await db.get_rule_abstraction("ARCH-ORG-001")
        assert result is not None
        assert result["abstraction_id"] == "ABS-ARCH-001"
        assert "ARCH-DI-001" in result["sibling_rule_ids"]

    @pytest.mark.asyncio()
    async def test_rule_without_abstraction_returns_none(self, db) -> None:
        await db.create_rule({
            "rule_id": "PERF-IO-001", "domain": "Performance", "severity": "high",
            "scope": "module", "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-20",
        })
        result = await db.get_rule_abstraction("PERF-IO-001")
        assert result is None

    @pytest.mark.asyncio()
    async def test_sibling_ids_exclude_self(self, db) -> None:
        await db.create_rule({
            "rule_id": "TEST-A-001", "domain": "Test", "severity": "low",
            "scope": "file", "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-20",
        })
        await db.create_rule({
            "rule_id": "TEST-B-001", "domain": "Test", "severity": "low",
            "scope": "file", "trigger": "t", "statement": "s", "violation": "v",
            "pass_example": "p", "enforcement": "e", "rationale": "r",
            "last_validated": "2026-03-20",
        })
        await db.create_abstraction({
            "abstraction_id": "ABS-TEST-001", "summary": "Test",
            "domain": "Test", "compression_ratio": 2.0, "rule_count": 2,
        })
        await db.create_abstracts_edge("ABS-TEST-001", "TEST-A-001")
        await db.create_abstracts_edge("ABS-TEST-001", "TEST-B-001")

        result = await db.get_rule_abstraction("TEST-A-001")
        assert result is not None
        assert "TEST-A-001" not in result["sibling_rule_ids"]
        assert "TEST-B-001" in result["sibling_rule_ids"]


# ---------------------------------------------------------------------------
# Multi-query session simulation
# ---------------------------------------------------------------------------

class TestMultiQuerySession:

    @pytest_asyncio.fixture()
    async def pipeline(self):
        from pathlib import Path

        from writ.graph.db import Neo4jConnection
        from writ.graph.ingest import discover_rule_files, parse_rules_from_file, validate_parsed_rule
        from writ.retrieval.pipeline import build_pipeline

        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        # Ensure rules are migrated (previous tests may have cleared DB).
        count = await db.count_rules()
        if count == 0:
            bible = Path("bible/")
            for f in discover_rule_files(bible):
                for rd in parse_rules_from_file(f):
                    validate_parsed_rule(rd)
                    clean = {k: v for k, v in rd.items() if not k.startswith("_")}
                    await db.create_rule(clean)
        try:
            p = await build_pipeline(db)
            yield p
        finally:
            await db.close()

    @pytest.mark.asyncio()
    async def test_3_query_no_duplicates(self, pipeline) -> None:
        tracker = SessionTracker(initial_budget=10000)
        all_rule_ids: list[str] = []

        for query_text in [
            "architecture layer separation",
            "performance optimization async",
            "testing isolation mocks",
        ]:
            payload = tracker.next_query(query_text)
            result = pipeline.query(
                query_text=payload["query"],
                budget_tokens=payload["budget_tokens"],
                loaded_rule_ids=payload["loaded_rule_ids"],
            )
            tracker.load_results(result)
            for rule in result["rules"]:
                if "rule_id" in rule:
                    all_rule_ids.append(rule["rule_id"])

        assert len(all_rule_ids) == len(set(all_rule_ids)), "Duplicate rules across queries"

    @pytest.mark.asyncio()
    async def test_3_query_broader_coverage(self, pipeline) -> None:
        single_result = pipeline.query(query_text="architecture", budget_tokens=10000)
        single_ids = {r["rule_id"] for r in single_result["rules"]}

        tracker = SessionTracker(initial_budget=10000)
        multi_ids: set[str] = set()
        for q in ["architecture", "performance", "testing"]:
            payload = tracker.next_query(q)
            result = pipeline.query(
                query_text=payload["query"],
                budget_tokens=payload["budget_tokens"],
                loaded_rule_ids=payload["loaded_rule_ids"],
            )
            tracker.load_results(result)
            for rule in result["rules"]:
                if "rule_id" in rule:
                    multi_ids.add(rule["rule_id"])

        assert len(multi_ids) >= len(single_ids)

    @pytest.mark.asyncio()
    async def test_budget_exhaustion(self, pipeline) -> None:
        tracker = SessionTracker(initial_budget=5000)
        payload = tracker.next_query("architecture")
        result = pipeline.query(
            query_text=payload["query"],
            budget_tokens=payload["budget_tokens"],
            loaded_rule_ids=payload["loaded_rule_ids"],
        )
        tracker.load_results(result)
        # After loading results, remaining should be lower than initial.
        assert tracker.remaining_budget < 5000


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------

class TestNoRegression:

    def test_existing_query_response_shape(self) -> None:
        """apply_context_budget returns same structure as Phase 8."""
        rules = [
            {"rule_id": f"R-{i:03d}", "score": 0.9 - i * 0.1,
             "statement": f"s{i}", "trigger": f"t{i}"}
            for i in range(5)
        ]
        result, mode = apply_context_budget(rules, 5000)
        assert mode == "standard"
        assert "rule_id" in result[0]
        assert "statement" in result[0]
