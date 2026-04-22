"""Phase 2 rule-authoring policy: mandatory rules must cite mechanical path."""
from __future__ import annotations

from datetime import date

import pytest

from writ.gate import _check_mechanical_enforcement


def _rule(**overrides) -> dict:
    base = {
        "rule_id": "ENF-TEST-001",
        "domain": "test",
        "severity": "high",
        "scope": "file",
        "trigger": "When X happens",
        "statement": "Must do Y",
        "violation": "Did not do Y",
        "pass_example": "Did Y",
        "enforcement": "static check",
        "rationale": "Because reasons",
        "mandatory": False,
        "last_validated": date(2026, 4, 21),
    }
    base.update(overrides)
    return base


class TestMechanicalEnforcementPolicy:
    def test_advisory_rule_passes(self) -> None:
        reasons = _check_mechanical_enforcement(_rule(mandatory=False))
        assert reasons == []

    def test_mandatory_with_path_passes(self) -> None:
        r = _rule(mandatory=True, mechanical_enforcement_path=".claude/hooks/foo.sh")
        assert _check_mechanical_enforcement(r) == []

    def test_mandatory_without_path_rejected(self) -> None:
        r = _rule(mandatory=True)  # mechanical_enforcement_path missing
        reasons = _check_mechanical_enforcement(r)
        assert len(reasons) == 1
        assert "mechanical_enforcement_path" in reasons[0]

    def test_mandatory_with_empty_path_rejected(self) -> None:
        r = _rule(mandatory=True, mechanical_enforcement_path="")
        assert len(_check_mechanical_enforcement(r)) == 1

    def test_mandatory_with_whitespace_path_rejected(self) -> None:
        r = _rule(mandatory=True, mechanical_enforcement_path="   ")
        assert len(_check_mechanical_enforcement(r)) == 1

    def test_mandatory_with_none_path_rejected(self) -> None:
        r = _rule(mandatory=True, mechanical_enforcement_path=None)
        assert len(_check_mechanical_enforcement(r)) == 1
