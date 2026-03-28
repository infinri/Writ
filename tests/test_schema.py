"""Phase 1: Schema validation tests.

Tests that Pydantic models correctly accept valid rules and reject malformed rules.
No database required -- pure model validation (PY-PYDANTIC-001).
Each test is isolated with its own data (TEST-ISO-001).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from writ.graph.schema import (
    Confidence,
    DependsOn,
    Rule,
)

STALENESS_WINDOW_DEFAULT = 365
EVIDENCE_DEFAULT = "doc:original-bible"


class TestRuleValidation:
    """Valid rules parse correctly."""

    def test_valid_rule_parses(self, valid_rule_data: dict) -> None:
        rule = Rule(**valid_rule_data)
        assert rule.rule_id == "ARCH-ORG-001"
        assert rule.domain == "Architecture"

    def test_valid_enf_rule_mandatory_true(self, valid_enf_rule_data: dict) -> None:
        rule = Rule(**valid_enf_rule_data)
        assert rule.mandatory is True
        assert rule.rule_id == "ENF-GATE-001"

    def test_graph_only_defaults(self, minimal_rule_data: dict) -> None:
        rule = Rule(**minimal_rule_data)
        assert rule.mandatory is False
        assert rule.confidence == Confidence.PRODUCTION_VALIDATED
        assert rule.staleness_window == STALENESS_WINDOW_DEFAULT
        assert rule.evidence == EVIDENCE_DEFAULT


class TestRuleRejection:
    """Malformed rules are rejected with actionable errors."""

    REQUIRED_FIELDS = [
        "rule_id",
        "domain",
        "severity",
        "scope",
        "trigger",
        "statement",
        "violation",
        "pass_example",
        "enforcement",
        "rationale",
        "last_validated",
    ]

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_missing_required_field(self, valid_rule_data: dict, field: str) -> None:
        data = {k: v for k, v in valid_rule_data.items() if k != field}
        with pytest.raises(ValidationError) as exc_info:
            Rule(**data)
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert field in field_names

    def test_empty_trigger_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["trigger"] = ""
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_whitespace_only_statement_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["statement"] = "   "
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_empty_domain_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["domain"] = ""
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)


class TestEnumValidation:
    """Invalid enum values are rejected."""

    def test_invalid_severity(self, valid_rule_data: dict) -> None:
        valid_rule_data["severity"] = "urgent"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_invalid_scope_uppercase(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "MODULE"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_invalid_confidence(self, valid_rule_data: dict) -> None:
        valid_rule_data["confidence"] = "untested"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)


class TestRuleIdFormat:
    """rule_id format validation."""

    def test_valid_simple_id(self, valid_rule_data: dict) -> None:
        rule = Rule(**valid_rule_data)
        assert rule.rule_id == "ARCH-ORG-001"

    def test_valid_compound_id(self, compound_id_rule_data: dict) -> None:
        rule = Rule(**compound_id_rule_data)
        assert rule.rule_id == "FW-M2-RT-003"

    def test_valid_enf_gate_final(self, enf_gate_final_data: dict) -> None:
        rule = Rule(**enf_gate_final_data)
        assert rule.rule_id == "ENF-GATE-FINAL"

    def test_lowercase_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["rule_id"] = "arch-org-001"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_no_prefix_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["rule_id"] = "001"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_empty_string_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["rule_id"] = ""
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)


class TestScopeValidation:
    """Phase 1c: scope accepts any lowercase string matching [a-z][a-z0-9_-]*."""

    def test_existing_scope_file(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "file"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "file"

    def test_existing_scope_module(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "module"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "module"

    def test_new_domain_scope_policy(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "policy"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "policy"

    def test_hyphenated_scope(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "code-review"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "code-review"

    def test_scope_with_digits(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "tier2"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "tier2"

    def test_underscore_scope(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "code_review"
        rule = Rule(**valid_rule_data)
        assert rule.scope == "code_review"

    def test_uppercase_scope_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "MODULE"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_leading_digit_scope_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "123abc"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_empty_scope_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = ""
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)

    def test_space_in_scope_rejected(self, valid_rule_data: dict) -> None:
        valid_rule_data["scope"] = "code review"
        with pytest.raises(ValidationError):
            Rule(**valid_rule_data)


class TestMandatoryField:
    """Phase 1b: mandatory field schema validation."""

    def test_mandatory_default_false(self, valid_rule_data: dict) -> None:
        rule = Rule(**valid_rule_data)
        assert rule.mandatory is False

    def test_mandatory_explicit_true(self, valid_rule_data: dict) -> None:
        valid_rule_data["mandatory"] = True
        rule = Rule(**valid_rule_data)
        assert rule.mandatory is True

    def test_mandatory_explicit_false_on_enf(self, valid_enf_rule_data: dict) -> None:
        valid_enf_rule_data["mandatory"] = False
        rule = Rule(**valid_enf_rule_data)
        assert rule.mandatory is False


class TestEdgeModels:
    """Edge model validation."""

    def test_depends_on_valid(self) -> None:
        edge = DependsOn(source_id="ARCH-ORG-001", target_id="ARCH-DI-001")
        assert edge.source_id == "ARCH-ORG-001"
        assert edge.target_id == "ARCH-DI-001"

    def test_depends_on_empty_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DependsOn(source_id="", target_id="ARCH-DI-001")

    def test_depends_on_empty_target_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DependsOn(source_id="ARCH-ORG-001", target_id="")
