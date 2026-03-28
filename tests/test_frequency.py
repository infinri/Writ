"""Phase 4: Frequency tracking, graduation, and feedback tests.

Per TEST-ISO-001: each test owns its data.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from writ.frequency import evaluate_graduation
from writ.graph.ingest import parse_rules_from_file
from writ.graph.schema import Rule
from writ.retrieval.ranking import compute_confidence_weight


# --- Graduation Logic ---


class TestGraduationLogic:
    """Phase 4c: empirical confidence graduation."""

    def test_below_threshold_returns_static(self) -> None:
        result = evaluate_graduation(30, 5, threshold=50, ratio_min=0.75)
        assert result.graduated is False
        assert result.flagged is False
        assert result.n == 35

    def test_at_threshold_high_ratio_graduates(self) -> None:
        result = evaluate_graduation(40, 10, threshold=50, ratio_min=0.75)
        assert result.graduated is True
        assert result.ratio == 0.8

    def test_at_threshold_low_ratio_flagged(self) -> None:
        result = evaluate_graduation(30, 20, threshold=50, ratio_min=0.75)
        assert result.graduated is False
        assert result.flagged is True
        assert result.ratio == 0.6

    def test_boundary_just_below_ratio(self) -> None:
        # 37/50 = 0.74 -- just below 0.75
        result = evaluate_graduation(37, 13, threshold=50, ratio_min=0.75)
        assert result.graduated is False
        assert result.flagged is True

    def test_boundary_just_above_ratio(self) -> None:
        # 38/50 = 0.76 -- above 0.75
        result = evaluate_graduation(38, 12, threshold=50, ratio_min=0.75)
        assert result.graduated is True

    def test_zero_observations(self) -> None:
        result = evaluate_graduation(0, 0, threshold=50, ratio_min=0.75)
        assert result.graduated is False
        assert result.flagged is False
        assert result.ratio == 0.0
        assert result.n == 0

    def test_all_positive(self) -> None:
        result = evaluate_graduation(50, 0, threshold=50, ratio_min=0.75)
        assert result.graduated is True
        assert result.ratio == 1.0

    def test_all_negative(self) -> None:
        result = evaluate_graduation(0, 50, threshold=50, ratio_min=0.75)
        assert result.graduated is False
        assert result.flagged is True
        assert result.ratio == 0.0


# --- Graduation in Ranking ---


class TestGraduationInRanking:
    """Graduation integrates into confidence weight at scoring time."""

    def test_graduated_rule_uses_empirical_confidence(self) -> None:
        weight = compute_confidence_weight("speculative", 54, 6, threshold=50, ratio_min=0.75)
        assert weight == pytest.approx(0.9, abs=0.01)

    def test_ungraduated_rule_uses_static_confidence(self) -> None:
        weight = compute_confidence_weight("speculative", 8, 2, threshold=50, ratio_min=0.75)
        assert weight == pytest.approx(0.3, abs=0.01)

    def test_human_rule_no_frequency_uses_static(self) -> None:
        weight = compute_confidence_weight("production-validated", 0, 0, threshold=50, ratio_min=0.75)
        assert weight == pytest.approx(0.8, abs=0.01)

    def test_graduated_battle_tested_uses_empirical(self) -> None:
        # Even battle-tested rules use empirical when graduated
        weight = compute_confidence_weight("battle-tested", 45, 5, threshold=50, ratio_min=0.75)
        assert weight == pytest.approx(0.9, abs=0.01)

    def test_flagged_rule_uses_static(self) -> None:
        # n=50 but ratio < 0.75 -> not graduated, uses static
        weight = compute_confidence_weight("peer-reviewed", 30, 20, threshold=50, ratio_min=0.75)
        assert weight == pytest.approx(0.6, abs=0.01)


# --- Frequency Properties ---


class TestFrequencyProperties:
    """Phase 4a: frequency fields on Rule model."""

    def test_default_frequency_values(self, valid_rule_data: dict) -> None:
        rule = Rule(**valid_rule_data)
        assert rule.times_seen_positive == 0
        assert rule.times_seen_negative == 0
        assert rule.last_seen is None

    def test_explicit_frequency_values(self, valid_rule_data: dict) -> None:
        valid_rule_data["times_seen_positive"] = 42
        valid_rule_data["times_seen_negative"] = 3
        valid_rule_data["last_seen"] = "2026-03-28T12:00:00"
        rule = Rule(**valid_rule_data)
        assert rule.times_seen_positive == 42
        assert rule.times_seen_negative == 3
        assert rule.last_seen == "2026-03-28T12:00:00"

    def test_ingest_sets_frequency_defaults(self) -> None:
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
            rules = parse_rules_from_file(Path(f.name))
        assert rules[0]["times_seen_positive"] == 0
        assert rules[0]["times_seen_negative"] == 0
        assert rules[0]["last_seen"] is None
