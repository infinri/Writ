"""Phase 3: Authority property, ranking preference, proximity seeding, proposal workflow.

Per TEST-ISO-001: each test owns its data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from pydantic import ValidationError

from writ.gate import propose_rule
from writ.graph.ingest import parse_rules_from_file
from writ.graph.schema import Rule
from writ.origin_context import OriginContextStore
from writ.retrieval.ranking import apply_authority_preference, filter_proximity_seeds


# --- Helpers ---


def _make_candidate(**overrides: str) -> dict:
    base = {
        "rule_id": "TEST-AUTH-001",
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


def _make_mock_pipeline(search_results=None, metadata=None, neighbors=None):
    pipeline = MagicMock()
    pipeline._model.encode.return_value = np.zeros(384, dtype=np.float32)
    pipeline._vector.search.return_value = search_results or []
    pipeline._metadata = metadata or {}
    pipeline._cache.get_neighbors.return_value = neighbors or []
    return pipeline


# --- 3a: Authority Property ---


class TestAuthorityProperty:
    """Authority field on Rule model."""

    def test_default_authority_is_human(self, valid_rule_data: dict) -> None:
        rule = Rule(**valid_rule_data)
        assert rule.authority == "human"

    def test_ai_provisional_accepted(self, valid_rule_data: dict) -> None:
        valid_rule_data["authority"] = "ai-provisional"
        rule = Rule(**valid_rule_data)
        assert rule.authority == "ai-provisional"

    def test_ai_promoted_accepted(self, valid_rule_data: dict) -> None:
        valid_rule_data["authority"] = "ai-promoted"
        rule = Rule(**valid_rule_data)
        assert rule.authority == "ai-promoted"

    def test_invalid_authority_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["authority"] = "auto-approved"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_ingest_sets_human_authority(self) -> None:
        from textwrap import dedent
        from pathlib import Path as P
        import tempfile

        md = dedent("""\
            <!-- RULE START: ARCH-TEST-001 -->
            ## Rule ARCH-TEST-001: Test Rule

            **Domain**: Testing
            **Severity**: High
            **Scope**: file

            ### Trigger
            When testing.

            ### Statement
            Must test.

            ### Violation (bad)
            Bad code.

            ### Pass (good)
            Good code.

            ### Enforcement
            Code review.

            ### Rationale
            Because testing.
            <!-- RULE END: ARCH-TEST-001 -->
        """)
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write(md)
            f.flush()
            rules = parse_rules_from_file(P(f.name))
        assert rules[0]["authority"] == "human"


# --- 3b: Hard Preference Rule ---


class TestAuthorityPreference:
    """Hard preference: human outranks ai-provisional within threshold."""

    def test_human_swapped_above_ai_provisional(self) -> None:
        rules = [
            {"rule_id": "AI-001", "score": 0.82, "authority": "ai-provisional"},
            {"rule_id": "HUMAN-001", "score": 0.80, "authority": "human"},
        ]
        result = apply_authority_preference(rules, threshold=0.05)
        assert result[0]["rule_id"] == "HUMAN-001"

    def test_no_swap_when_gap_exceeds_threshold(self) -> None:
        rules = [
            {"rule_id": "AI-001", "score": 0.90, "authority": "ai-provisional"},
            {"rule_id": "HUMAN-001", "score": 0.70, "authority": "human"},
        ]
        result = apply_authority_preference(rules, threshold=0.05)
        assert result[0]["rule_id"] == "AI-001"

    def test_no_swap_between_two_humans(self) -> None:
        rules = [
            {"rule_id": "H1", "score": 0.82, "authority": "human"},
            {"rule_id": "H2", "score": 0.80, "authority": "human"},
        ]
        result = apply_authority_preference(rules, threshold=0.05)
        assert result[0]["rule_id"] == "H1"

    def test_no_swap_between_two_ai_provisional(self) -> None:
        rules = [
            {"rule_id": "AI-1", "score": 0.82, "authority": "ai-provisional"},
            {"rule_id": "AI-2", "score": 0.80, "authority": "ai-provisional"},
        ]
        result = apply_authority_preference(rules, threshold=0.05)
        assert result[0]["rule_id"] == "AI-1"

    def test_ai_promoted_treated_like_human(self) -> None:
        rules = [
            {"rule_id": "AI-P", "score": 0.82, "authority": "ai-provisional"},
            {"rule_id": "PROM", "score": 0.80, "authority": "ai-promoted"},
        ]
        result = apply_authority_preference(rules, threshold=0.05)
        assert result[0]["rule_id"] == "PROM"

    def test_threshold_zero_disables_preference(self) -> None:
        rules = [
            {"rule_id": "AI-001", "score": 0.82, "authority": "ai-provisional"},
            {"rule_id": "HUMAN-001", "score": 0.80, "authority": "human"},
        ]
        result = apply_authority_preference(rules, threshold=0.0)
        assert result[0]["rule_id"] == "AI-001"

    def test_all_human_corpus_unchanged(self) -> None:
        rules = [
            {"rule_id": f"H-{i}", "score": 1.0 - i * 0.1, "authority": "human"}
            for i in range(5)
        ]
        original_order = [r["rule_id"] for r in rules]
        result = apply_authority_preference(rules, threshold=0.05)
        assert [r["rule_id"] for r in result] == original_order


# --- 3c: Proximity Seeding ---


class TestProximitySeeding:
    """AI-provisional excluded from top-3 graph proximity seeding."""

    def test_ai_provisional_excluded_from_top3(self) -> None:
        first_pass = [
            ("AI-001", 0.95, "ai-provisional"),
            ("HUMAN-001", 0.90, "human"),
            ("HUMAN-002", 0.85, "human"),
            ("HUMAN-003", 0.80, "human"),
        ]
        top3 = filter_proximity_seeds(first_pass)
        assert "AI-001" not in top3
        assert len(top3) == 3
        assert top3 == ["HUMAN-001", "HUMAN-002", "HUMAN-003"]

    def test_fewer_than_3_human_no_backfill(self) -> None:
        first_pass = [
            ("AI-001", 0.95, "ai-provisional"),
            ("AI-002", 0.90, "ai-provisional"),
            ("HUMAN-001", 0.85, "human"),
        ]
        top3 = filter_proximity_seeds(first_pass)
        assert top3 == ["HUMAN-001"]

    def test_all_human_unchanged(self) -> None:
        first_pass = [
            ("H1", 0.95, "human"),
            ("H2", 0.90, "human"),
            ("H3", 0.85, "human"),
        ]
        top3 = filter_proximity_seeds(first_pass)
        assert top3 == ["H1", "H2", "H3"]

    def test_ai_promoted_included(self) -> None:
        first_pass = [
            ("PROM-001", 0.95, "ai-promoted"),
            ("H1", 0.90, "human"),
            ("H2", 0.85, "human"),
        ]
        top3 = filter_proximity_seeds(first_pass)
        assert "PROM-001" in top3


# --- 3d: Proposal Workflow ---


class TestProposalWorkflow:
    """End-to-end AI rule proposal."""

    def test_valid_proposal_accepted(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.30)]
        )
        mock_db = AsyncMock()
        candidate = _make_candidate()
        result = asyncio.get_event_loop().run_until_complete(
            propose_rule(candidate, mock, mock_db)
        )
        assert result["accepted"] is True
        assert result["authority"] == "ai-provisional"
        assert result["confidence"] == "speculative"

    def test_proposal_enforces_ai_provisional(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.30)]
        )
        mock_db = AsyncMock()
        candidate = _make_candidate()
        candidate["authority"] = "human"
        candidate["confidence"] = "battle-tested"
        result = asyncio.get_event_loop().run_until_complete(
            propose_rule(candidate, mock, mock_db)
        )
        assert result["accepted"] is True
        assert result["authority"] == "ai-provisional"
        assert result["confidence"] == "speculative"

    def test_gate_rejection_prevents_ingestion(self) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.30)]
        )
        mock_db = AsyncMock()
        candidate = _make_candidate(statement="Consider doing this")
        result = asyncio.get_event_loop().run_until_complete(
            propose_rule(candidate, mock, mock_db)
        )
        assert result["accepted"] is False
        assert len(result["reasons"]) > 0
        mock_db.create_rule.assert_not_called()

    def test_origin_context_written_on_accept(self, tmp_path: Path) -> None:
        mock = _make_mock_pipeline(
            search_results=[FakeScoredResult("EXISTING-001", 0.30)]
        )
        mock_db = AsyncMock()
        candidate = _make_candidate()
        asyncio.get_event_loop().run_until_complete(
            propose_rule(
                candidate, mock, mock_db,
                origin_db_path=tmp_path / "ctx.db",
                task_description="testing proposal",
            )
        )
        store = OriginContextStore(tmp_path / "ctx.db")
        ctx = store.get(candidate["rule_id"])
        store.close()
        assert ctx is not None
        assert ctx["task_description"] == "testing proposal"
