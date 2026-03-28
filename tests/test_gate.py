"""Phase 2: Structural pre-filter tests.

Tests gate function checks: schema, specificity, novelty, redundancy, conflict.
Uses mock pipeline to isolate gate logic from retrieval infrastructure.
Per TEST-ISO-001: each test owns its data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from unittest.mock import MagicMock

import numpy as np

from writ.gate import (
    GateResult,
    _check_schema,
    _check_specificity,
    structural_gate,
)


# --- Helpers ---


def _make_candidate(**overrides: str) -> dict:
    """Minimal valid candidate rule dict."""
    base = {
        "rule_id": "TEST-GATE-001",
        "domain": "Testing",
        "severity": "high",
        "scope": "file",
        "trigger": "When writing a function that exceeds 30 lines.",
        "statement": "Functions must not exceed 30 lines of logic.",
        "violation": "Function body is 45 lines.",
        "pass_example": "Function decomposed into sub-functions.",
        "enforcement": "Code review.",
        "rationale": "Long functions resist testing and reuse.",
        "last_validated": date.today().isoformat(),
    }
    base.update(overrides)
    return base


@dataclass
class FakeScoredResult:
    rule_id: str
    score: float


def _make_mock_pipeline(
    search_results: list[FakeScoredResult] | None = None,
    metadata: dict | None = None,
    neighbors: list[dict] | None = None,
) -> MagicMock:
    """Build a mock pipeline with controllable vector search and adjacency."""
    pipeline = MagicMock()

    # Mock embedding model -- returns a zero vector.
    pipeline._model.encode.return_value = np.zeros(384, dtype=np.float32)

    # Mock vector store search.
    pipeline._vector.search.return_value = search_results or []

    # Mock metadata dict.
    pipeline._metadata = metadata or {}

    # Mock adjacency cache.
    pipeline._cache.get_neighbors.return_value = neighbors or []

    return pipeline


# --- GateResult ---


class TestGateResult:
    """GateResult dataclass behaves correctly."""

    def test_accepted_result(self) -> None:
        result = GateResult(accepted=True, reasons=[], similar_rules=[])
        assert result.accepted is True
        assert result.reasons == []

    def test_rejected_result(self) -> None:
        result = GateResult(accepted=False, reasons=["too vague"], similar_rules=[])
        assert result.accepted is False
        assert "too vague" in result.reasons


# --- Schema Check ---


class TestSchemaCheck:
    """Gate rejects candidates that fail Pydantic validation."""

    def test_missing_required_field_rejected(self) -> None:
        candidate = _make_candidate(trigger="")
        reasons = _check_schema(candidate)
        assert len(reasons) > 0
        assert any("validation" in r.lower() or "schema" in r.lower() for r in reasons)

    def test_valid_candidate_passes_schema(self) -> None:
        candidate = _make_candidate()
        reasons = _check_schema(candidate)
        assert reasons == []


# --- Specificity Check ---


class TestSpecificityCheck:
    """Gate rejects candidates with vague language disqualifiers."""

    def test_consider_rejected(self) -> None:
        candidate = _make_candidate(statement="Consider using a service layer")
        reasons = _check_specificity(candidate)
        assert len(reasons) > 0
        assert any("vague" in r.lower() or "specificity" in r.lower() for r in reasons)

    def test_be_aware_rejected(self) -> None:
        candidate = _make_candidate(trigger="Be aware of plugin execution order")
        reasons = _check_specificity(candidate)
        assert len(reasons) > 0

    def test_where_appropriate_rejected(self) -> None:
        candidate = _make_candidate(statement="Where appropriate, centralize logic")
        reasons = _check_specificity(candidate)
        assert len(reasons) > 0

    def test_try_to_rejected(self) -> None:
        candidate = _make_candidate(statement="Try to avoid global state")
        reasons = _check_specificity(candidate)
        assert len(reasons) > 0

    def test_specific_statement_passes(self) -> None:
        candidate = _make_candidate(
            statement="Controllers must not contain SQL queries."
        )
        reasons = _check_specificity(candidate)
        assert reasons == []

    def test_consider_in_code_block_still_detected(self) -> None:
        candidate = _make_candidate(
            statement="Consider refactoring when complexity increases."
        )
        reasons = _check_specificity(candidate)
        assert len(reasons) > 0


# --- Novelty Check ---


class TestNoveltyCheck:
    """Gate rejects candidates too similar to existing rules."""

    def test_high_similarity_rejected(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.90)]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert result.accepted is False
        assert any("novelty" in r.lower() for r in result.reasons)
        assert "EXISTING-001" in result.similar_rules

    def test_novel_candidate_passes(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.50)]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert not any("novelty" in r.lower() for r in result.reasons)


# --- Redundancy Check ---


class TestRedundancyCheck:
    """Gate rejects near-duplicates."""

    def test_duplicate_rejected(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.97)]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert result.accepted is False
        assert any("redundan" in r.lower() for r in result.reasons)

    def test_non_duplicate_passes(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.50)]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert not any("redundan" in r.lower() for r in result.reasons)


# --- Conflict Check ---


class TestConflictCheck:
    """Gate rejects candidates with CONFLICTS_WITH edges."""

    def test_conflict_detected(self) -> None:
        mock = _make_mock_pipeline(
            metadata={"TEST-GATE-001": {"rule_id": "TEST-GATE-001"}},
            neighbors=[{
                "rule_id": "CONFLICT-001",
                "edge_type": "CONFLICTS_WITH",
                "direction": "outgoing",
            }],
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert result.accepted is False
        assert any("conflict" in r.lower() for r in result.reasons)

    def test_no_conflict_passes(self) -> None:
        mock = _make_mock_pipeline(
            metadata={"TEST-GATE-001": {"rule_id": "TEST-GATE-001"}},
            neighbors=[{
                "rule_id": "RELATED-001",
                "edge_type": "RELATED_TO",
                "direction": "outgoing",
            }],
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert not any("conflict" in r.lower() for r in result.reasons)

    def test_new_rule_no_graph_presence_skips_conflict(self) -> None:
        mock = _make_mock_pipeline(metadata={})
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert not any("conflict" in r.lower() for r in result.reasons)


# --- Integration ---


class TestGateIntegration:
    """Gate consolidates all five checks into a single pass/fail."""

    def test_all_checks_pass(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.30)]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert result.accepted is True
        assert result.reasons == []

    def test_multiple_failures_reported(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.92)]
        )
        candidate = _make_candidate(statement="Consider using this approach")
        result = structural_gate(candidate, mock)
        assert result.accepted is False
        assert len(result.reasons) >= 2

    def test_self_exclusion(self) -> None:
        """Candidate's own rule_id is excluded from similarity results."""
        mock = _make_mock_pipeline(
            search_results=[
                FakeScoredResult("TEST-GATE-001", 1.0),
                FakeScoredResult("OTHER-001", 0.30),
            ]
        )
        candidate = _make_candidate()
        result = structural_gate(candidate, mock)
        assert "TEST-GATE-001" not in result.similar_rules
        assert result.accepted is True

    def test_custom_thresholds(self) -> None:
        """Custom thresholds override defaults."""
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.80)]
        )
        candidate = _make_candidate()
        # Default novelty is 0.85; lowering to 0.75 should flag 0.80.
        result = structural_gate(candidate, mock, novelty_threshold=0.75)
        assert any("novelty" in r.lower() for r in result.reasons)
