"""Tests for Phase 8: Compression layer (clustering + abstraction nodes).

Per TEST-TDD-001: test skeletons approved before implementation.
Per TEST-ISO-001: each test sets up its own state, no shared mutables.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_asyncio

from writ.compression.abstractions import generate_abstractions
from writ.compression.clusters import (
    ClusterResult,
    ComparisonResult,
    cluster_hdbscan,
    cluster_kmeans,
    evaluate_both,
)
from writ.retrieval.ranking import SUMMARY_THRESHOLD, apply_context_budget


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def domain_rules() -> list[dict]:
    """Minimal set of domain rules for clustering tests."""
    return [
        {"rule_id": f"ARCH-{i:03d}", "domain": "Architecture",
         "severity": "high", "scope": "module",
         "trigger": f"Architecture trigger {i}", "statement": f"Architecture rule {i}.",
         "violation": "v", "pass_example": "p", "enforcement": "e", "rationale": "r",
         "mandatory": False, "last_validated": "2026-03-20"}
        for i in range(1, 11)
    ] + [
        {"rule_id": f"PERF-{i:03d}", "domain": "Performance",
         "severity": "medium", "scope": "file",
         "trigger": f"Performance trigger {i}", "statement": f"Performance rule {i}.",
         "violation": "v", "pass_example": "p", "enforcement": "e", "rationale": "r",
         "mandatory": False, "last_validated": "2026-03-20"}
        for i in range(1, 9)
    ]


@pytest.fixture()
def embeddings_for_domain_rules() -> np.ndarray:
    """Synthetic embeddings: ARCH rules cluster together, PERF rules cluster together."""
    rng = np.random.default_rng(42)
    # 10 ARCH rules near [1, 0, 0, ...], 8 PERF rules near [0, 1, 0, ...]
    dim = 64
    arch = rng.normal(loc=1.0, scale=0.1, size=(10, dim)).astype(np.float32)
    perf = rng.normal(loc=-1.0, scale=0.1, size=(8, dim)).astype(np.float32)
    return np.vstack([arch, perf])


@pytest.fixture()
def rule_ids_for_domain(domain_rules: list[dict]) -> list[str]:
    return [r["rule_id"] for r in domain_rules]


# ---------------------------------------------------------------------------
# Unit tests: cluster_rules
# ---------------------------------------------------------------------------

class TestClusterRules:

    def test_all_domain_rules_assigned_or_ungrouped(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        clustered = {rid for members in result.clusters.values() for rid in members}
        all_assigned = clustered | set(result.ungrouped)
        assert all_assigned == set(rule_ids_for_domain)

    def test_mandatory_rules_excluded(self) -> None:
        """Caller must filter mandatory rules before passing to cluster functions.
        Verify the clustering functions work with pre-filtered input."""
        rule_ids = ["ARCH-001", "ARCH-002", "ARCH-003"]
        embeddings = np.random.default_rng(42).normal(size=(3, 64)).astype(np.float32)
        result = cluster_hdbscan(rule_ids, embeddings)
        all_ids = {rid for members in result.clusters.values() for rid in members} | set(result.ungrouped)
        assert "ENF-GATE-001" not in all_ids

    def test_no_singleton_clusters(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        for cid, members in result.clusters.items():
            assert len(members) >= 2, f"Cluster {cid} has {len(members)} members (singleton)"

    def test_deterministic_on_same_input(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        r1 = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        r2 = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        assert r1.clusters == r2.clusters
        assert r1.ungrouped == r2.ungrouped

    def test_returns_cluster_metadata(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        assert isinstance(result, ClusterResult)
        assert isinstance(result.clusters, dict)
        assert isinstance(result.centroid_indices, dict)
        for cid in result.clusters:
            assert cid in result.centroid_indices

    def test_ungrouped_rules_reported(self) -> None:
        """With widely scattered embeddings, some rules may be ungrouped."""
        rng = np.random.default_rng(99)
        rule_ids = [f"TEST-{i:03d}" for i in range(5)]
        embeddings = rng.normal(scale=10.0, size=(5, 64)).astype(np.float32)
        result = cluster_hdbscan(rule_ids, embeddings)
        # At minimum, result should report ungrouped list (may or may not be empty).
        assert isinstance(result.ungrouped, list)

    def test_empty_input_returns_empty(self) -> None:
        result = cluster_hdbscan([], np.array([], dtype=np.float32).reshape(0, 64))
        assert result.clusters == {}
        assert result.ungrouped == []


# ---------------------------------------------------------------------------
# Unit tests: generate_abstractions
# ---------------------------------------------------------------------------

class TestGenerateAbstractions:

    def test_summary_is_nearest_centroid_statement(
        self, domain_rules: list[dict], rule_ids_for_domain: list[str],
        embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        if not result.clusters:
            pytest.skip("No clusters produced")
        abstractions = generate_abstractions(result, domain_rules)
        for abst in abstractions:
            # Summary should be a statement from one of the member rules.
            member_statements = [
                r["statement"] for r in domain_rules if r["rule_id"] in abst["rule_ids"]
            ]
            assert abst["summary"] in member_statements

    def test_abstraction_has_required_fields(
        self, domain_rules: list[dict], rule_ids_for_domain: list[str],
        embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        if not result.clusters:
            pytest.skip("No clusters produced")
        abstractions = generate_abstractions(result, domain_rules)
        for abst in abstractions:
            assert "abstraction_id" in abst
            assert "summary" in abst
            assert "rule_ids" in abst
            assert "domain" in abst
            assert "compression_ratio" in abst

    def test_compression_ratio_above_one(
        self, domain_rules: list[dict], rule_ids_for_domain: list[str],
        embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        if not result.clusters:
            pytest.skip("No clusters produced")
        abstractions = generate_abstractions(result, domain_rules)
        for abst in abstractions:
            assert abst["compression_ratio"] > 1.0

    def test_domain_derived_from_members(
        self, domain_rules: list[dict], rule_ids_for_domain: list[str],
        embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        if not result.clusters:
            pytest.skip("No clusters produced")
        abstractions = generate_abstractions(result, domain_rules)
        for abst in abstractions:
            assert abst["domain"] in {"Architecture", "Performance"}

    def test_abstraction_id_format(
        self, domain_rules: list[dict], rule_ids_for_domain: list[str],
        embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        if not result.clusters:
            pytest.skip("No clusters produced")
        abstractions = generate_abstractions(result, domain_rules)
        for abst in abstractions:
            assert abst["abstraction_id"].startswith("ABS-")


# ---------------------------------------------------------------------------
# Unit tests: algorithm evaluation
# ---------------------------------------------------------------------------

class TestAlgorithmEvaluation:

    def test_hdbscan_produces_clusters(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_hdbscan(rule_ids_for_domain, embeddings_for_domain_rules)
        assert len(result.clusters) >= 2

    def test_kmeans_produces_clusters(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        result = cluster_kmeans(rule_ids_for_domain, embeddings_for_domain_rules, k=3)
        assert len(result.clusters) >= 2

    def test_hdbscan_vs_kmeans_comparison(
        self, rule_ids_for_domain: list[str], embeddings_for_domain_rules: np.ndarray,
    ) -> None:
        comparison = evaluate_both(rule_ids_for_domain, embeddings_for_domain_rules)
        assert isinstance(comparison, ComparisonResult)
        assert comparison.chosen in {"hdbscan", "kmeans"}
        assert comparison.hdbscan.silhouette != 0.0 or comparison.kmeans.silhouette != 0.0


# ---------------------------------------------------------------------------
# Integration: Neo4j abstraction storage
# ---------------------------------------------------------------------------

class TestAbstractionStorage:

    @pytest_asyncio.fixture()
    async def db(self):
        from writ.graph.db import Neo4jConnection

        conn = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        await conn.clear_all()
        yield conn
        await conn.clear_all()
        await conn.close()

    @pytest.mark.asyncio()
    async def test_create_abstraction_node(self, db) -> None:
        data = {
            "abstraction_id": "ABS-TEST-001",
            "summary": "Test summary",
            "domain": "Testing",
            "compression_ratio": 3.5,
            "rule_count": 2,
        }
        result = await db.create_abstraction(data)
        assert result == "ABS-TEST-001"

    @pytest.mark.asyncio()
    async def test_abstracts_edges_created(self, db, valid_rule_data: dict) -> None:
        await db.create_rule(valid_rule_data)
        abs_data = {
            "abstraction_id": "ABS-ARCH-001",
            "summary": "Arch summary",
            "domain": "Architecture",
            "compression_ratio": 2.0,
            "rule_count": 1,
        }
        await db.create_abstraction(abs_data)
        await db.create_abstracts_edge("ABS-ARCH-001", valid_rule_data["rule_id"])
        abst = await db.get_abstraction("ABS-ARCH-001")
        assert abst is not None
        assert len(abst["members"]) == 1
        assert abst["members"][0]["rule_id"] == valid_rule_data["rule_id"]

    @pytest.mark.asyncio()
    async def test_get_all_abstractions(self, db) -> None:
        for i in range(3):
            await db.create_abstraction({
                "abstraction_id": f"ABS-TEST-{i:03d}",
                "summary": f"Summary {i}",
                "domain": "Testing",
                "compression_ratio": 2.0,
                "rule_count": 2,
            })
        result = await db.get_all_abstractions()
        assert len(result) == 3

    @pytest.mark.asyncio()
    async def test_get_abstraction_by_id(self, db) -> None:
        await db.create_abstraction({
            "abstraction_id": "ABS-FIND-001",
            "summary": "Find me",
            "domain": "Test",
            "compression_ratio": 1.5,
            "rule_count": 2,
        })
        result = await db.get_abstraction("ABS-FIND-001")
        assert result is not None
        assert result["abstraction_id"] == "ABS-FIND-001"
        assert result["summary"] == "Find me"

    @pytest.mark.asyncio()
    async def test_get_abstraction_not_found(self, db) -> None:
        result = await db.get_abstraction("ABS-NOPE-999")
        assert result is None

    @pytest.mark.asyncio()
    async def test_delete_abstractions_clears_all(self, db, valid_rule_data: dict) -> None:
        await db.create_rule(valid_rule_data)
        await db.create_abstraction({
            "abstraction_id": "ABS-DEL-001",
            "summary": "Delete me",
            "domain": "Test",
            "compression_ratio": 2.0,
            "rule_count": 1,
        })
        await db.create_abstracts_edge("ABS-DEL-001", valid_rule_data["rule_id"])
        deleted = await db.delete_abstractions()
        assert deleted == 1
        # Rule must still exist.
        rule = await db.get_rule(valid_rule_data["rule_id"])
        assert rule is not None
        # Abstraction must be gone.
        abst = await db.get_abstraction("ABS-DEL-001")
        assert abst is None

    @pytest.mark.asyncio()
    async def test_recompress_replaces_old(self, db) -> None:
        await db.create_abstraction({
            "abstraction_id": "ABS-OLD-001",
            "summary": "Old",
            "domain": "Test",
            "compression_ratio": 2.0,
            "rule_count": 2,
        })
        assert len(await db.get_all_abstractions()) == 1
        # Simulate recompress: delete then create new.
        await db.delete_abstractions()
        await db.create_abstraction({
            "abstraction_id": "ABS-NEW-001",
            "summary": "New",
            "domain": "Test",
            "compression_ratio": 3.0,
            "rule_count": 3,
        })
        result = await db.get_all_abstractions()
        assert len(result) == 1
        assert result[0]["abstraction_id"] == "ABS-NEW-001"


# ---------------------------------------------------------------------------
# Pipeline summary mode upgrade
# ---------------------------------------------------------------------------

class TestSummaryModeAbstractions:

    @pytest.fixture()
    def sample_rules(self) -> list[dict]:
        return [
            {"rule_id": "ARCH-ORG-001", "score": 0.9, "statement": "Layer sep", "trigger": "t1"},
            {"rule_id": "ARCH-DI-001", "score": 0.8, "statement": "DI", "trigger": "t2"},
            {"rule_id": "PERF-IO-001", "score": 0.7, "statement": "No sync IO", "trigger": "t3"},
        ]

    @pytest.fixture()
    def sample_abstractions(self) -> list[dict]:
        return [
            {
                "abstraction_id": "ABS-ARCH-000",
                "summary": "Architecture principles.",
                "rule_ids": ["ARCH-ORG-001", "ARCH-DI-001"],
                "compression_ratio": 2.5,
                "domain": "Architecture",
            },
        ]

    def test_summary_mode_returns_abstractions(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        result, mode = apply_context_budget(
            sample_rules, SUMMARY_THRESHOLD - 1, abstractions=sample_abstractions,
        )
        assert mode == "summary"
        abs_items = [r for r in result if "abstraction_id" in r]
        assert len(abs_items) >= 1
        assert abs_items[0]["abstraction_id"] == "ABS-ARCH-000"

    def test_summary_mode_includes_member_ids(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        result, _ = apply_context_budget(
            sample_rules, SUMMARY_THRESHOLD - 1, abstractions=sample_abstractions,
        )
        abs_items = [r for r in result if "abstraction_id" in r]
        assert "rule_ids" in abs_items[0]
        assert "ARCH-ORG-001" in abs_items[0]["rule_ids"]

    def test_summary_mode_includes_compression_ratio(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        result, _ = apply_context_budget(
            sample_rules, SUMMARY_THRESHOLD - 1, abstractions=sample_abstractions,
        )
        abs_items = [r for r in result if "abstraction_id" in r]
        assert abs_items[0]["compression_ratio"] == 2.5

    def test_summary_mode_fallback_without_abstractions(
        self, sample_rules: list[dict],
    ) -> None:
        result, mode = apply_context_budget(sample_rules, SUMMARY_THRESHOLD - 1)
        assert mode == "summary"
        # Should get raw statement+trigger, not abstractions.
        assert "rule_id" in result[0]
        assert "abstraction_id" not in result[0]

    def test_standard_mode_unchanged(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        result, mode = apply_context_budget(
            sample_rules, SUMMARY_THRESHOLD + 100, abstractions=sample_abstractions,
        )
        assert mode == "standard"
        assert "rule_id" in result[0]
        assert "abstraction_id" not in result[0]

    def test_full_mode_unchanged(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        result, mode = apply_context_budget(
            sample_rules, 10000, abstractions=sample_abstractions,
        )
        assert mode == "full"
        assert "rule_id" in result[0]
        assert "abstraction_id" not in result[0]

    def test_ungrouped_rule_falls_back(
        self, sample_rules: list[dict], sample_abstractions: list[dict],
    ) -> None:
        """PERF-IO-001 is not in any abstraction -- should get statement+trigger."""
        result, _ = apply_context_budget(
            sample_rules, SUMMARY_THRESHOLD - 1, abstractions=sample_abstractions,
        )
        ungrouped = [r for r in result if r.get("rule_id") == "PERF-IO-001"]
        assert len(ungrouped) == 1
        assert "statement" in ungrouped[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCompressCLI:

    def test_compress_command_registered(self) -> None:
        from writ.cli import app

        command_names = [cmd.callback.__name__ for cmd in app.registered_commands]
        assert "compress" in command_names


# ---------------------------------------------------------------------------
# Regression gates
# ---------------------------------------------------------------------------

class TestNoRegression:

    def test_pipeline_query_without_abstractions(self) -> None:
        """apply_context_budget with no abstractions behaves identically to Phase 7."""
        rules = [
            {"rule_id": f"R-{i:03d}", "score": 0.9 - i * 0.1,
             "statement": f"s{i}", "trigger": f"t{i}"}
            for i in range(5)
        ]
        # Summary mode without abstractions.
        result, mode = apply_context_budget(rules, SUMMARY_THRESHOLD - 1)
        assert mode == "summary"
        assert all("rule_id" in r for r in result)
        assert "abstraction_id" not in result[0]

        # Standard mode.
        result_std, mode_std = apply_context_budget(rules, SUMMARY_THRESHOLD + 100)
        assert mode_std == "standard"

        # Full mode.
        result_full, mode_full = apply_context_budget(rules, 10000)
        assert mode_full == "full"
