"""Phase 1: Schema roundtrip tests for methodology node types and edges.

Covers the 10 new node models, 8 new edge classes, and Rule extensions per
plan Section 6.1 and docs/phase-0-schema-proposal.md. Pure Pydantic model
validation, no database (PY-PYDANTIC-001). Each test isolated (TEST-ISO-001).
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from writ.graph.schema import (
    # existing (unchanged)
    Confidence,
    Rule,
    Severity,
    # new retrievable node types
    AntiPattern,
    ForbiddenResponse,
    Playbook,
    Skill,
    Technique,
    # new non-retrievable node types
    Phase,
    PressureScenario,
    Rationalization,
    SubagentRole,
    WorkedExample,
    # new edge types
    AttachedTo,
    Contains,
    Counters,
    Demonstrates,
    Dispatches,
    Gates,
    PressureTests,
    Teaches,
    # node type enum
    NodeType,
)


# --- Common fixtures -----------------------------------------------------------


@pytest.fixture
def skill_data() -> dict:
    return {
        "skill_id": "SKL-PROC-BRAIN-001",
        "domain": "process",
        "severity": "high",
        "scope": "session",
        "trigger": "When starting a new feature",
        "statement": "Present 2-3 approaches",
        "rationale": "Canonical failure mode is premature implementation",
        "last_validated": date(2026, 4, 21),
    }


@pytest.fixture
def playbook_data() -> dict:
    return {
        "playbook_id": "PBK-PROC-BRAIN-001",
        "domain": "process",
        "severity": "high",
        "scope": "session",
        "trigger": "Brainstorm invoked",
        "statement": "Nine-phase process",
        "rationale": "Ordered sequence prevents phase skipping",
        "last_validated": date(2026, 4, 21),
        "phase_ids": ["PHA-BRAIN-001", "PHA-BRAIN-002"],
    }


@pytest.fixture
def antipattern_data() -> dict:
    return {
        "antipattern_id": "ANT-PROC-TDD-001",
        "domain": "process",
        "severity": "high",
        "scope": "task",
        "trigger": "Test passes on first run",
        "statement": "Test passes immediately is an anti-pattern",
        "rationale": "Watch-it-fail is the evidence the test works",
        "last_validated": date(2026, 4, 21),
        "counter_nodes": ["PBK-PROC-TDD-001"],
    }


@pytest.fixture
def forbidden_data() -> dict:
    return {
        "forbidden_id": "FRB-COMMS-001",
        "domain": "communication",
        "severity": "high",
        "scope": "session",
        "trigger": "Responding to review feedback",
        "statement": "Performative-agreement phrases forbidden",
        "rationale": "Skipping verification is the failure mode",
        "last_validated": date(2026, 4, 21),
        "forbidden_phrases": ["You're absolutely right", "Great point"],
        "what_to_say_instead": "Verify against the codebase, then respond",
    }


@pytest.fixture
def phase_data() -> dict:
    return {
        "phase_id": "PHA-BRAIN-001",
        "domain": "process",
        "scope": "session",
        "trigger": "First phase of brainstorm",
        "statement": "Understand intent",
        "rationale": "Intent ambiguity is cheapest to fix first",
        "last_validated": date(2026, 4, 21),
        "position": 1,
        "name": "Understand intent",
        "description": "Restate the user's goal and confirm",
        "parent_playbook_id": "PBK-PROC-BRAIN-001",
    }


@pytest.fixture
def rationalization_data() -> dict:
    return {
        "rationalization_id": "RAT-BRAIN-001",
        "domain": "process",
        "scope": "session",
        "trigger": "Agent thinks task is simple",
        "statement": "Too-simple rationalization",
        "rationale": "Canonical skip trigger",
        "last_validated": date(2026, 4, 21),
        "thought": "This task is obviously simple.",
        "counter": "Every project goes through brainstorm.",
        "attached_to": "ENF-PROC-BRAIN-001",
    }


@pytest.fixture
def scenario_data() -> dict:
    return {
        "scenario_id": "PSC-BRAIN-001",
        "domain": "process",
        "scope": "session",
        "trigger": "Pressure test for BRAIN rule",
        "statement": "One-line refactor temptation",
        "rationale": "Tests 'too simple' rationalization",
        "last_validated": date(2026, 4, 21),
        "prompt": "Quick fix: change X from 30 to 60",
        "expected_compliance": "Agent presents options and waits",
        "failure_patterns": ["Agent writes without asking"],
        "rule_under_test": "ENF-PROC-BRAIN-001",
        "difficulty": "easy",
    }


@pytest.fixture
def example_data() -> dict:
    return {
        "example_id": "EXM-TDD-001",
        "domain": "process",
        "scope": "task",
        "trigger": "User asks for TDD example",
        "statement": "Empty-body bug walk-through",
        "rationale": "Concrete commands anchor abstract methodology",
        "last_validated": date(2026, 4, 21),
        "title": "TDD on empty-body bug",
        "before": "src/api.py crashes on empty body",
        "applied_skill": "PBK-PROC-TDD-001",
        "result": "Test first, fix second, both pass",
        "linked_skill": "PBK-PROC-TDD-001",
    }


@pytest.fixture
def role_data() -> dict:
    return {
        "role_id": "ROL-CODE-REVIEWER-001",
        "domain": "process",
        "scope": "task",
        "trigger": "Code review dispatch",
        "statement": "Code reviewer subagent template",
        "rationale": "Fresh context for independent judgment",
        "last_validated": date(2026, 4, 21),
        "name": "writ-code-reviewer",
        "prompt_template": "You are a code reviewer...",
    }


# --- Rule extensions -----------------------------------------------------------


class TestRuleExtensions:
    """New fields on Rule per plan Section 6.1; all default to not-set so existing rules still validate."""

    def test_existing_rule_data_still_validates(self, valid_rule_data: dict) -> None:
        r = Rule(**valid_rule_data)
        assert r.always_on is False
        assert r.mechanical_enforcement_path is None
        assert r.rationalization_counters == []
        assert r.red_flag_thoughts == []
        assert r.body == ""
        assert r.source_attribution is None
        assert r.source_commit is None

    def test_rationalization_counters_accepts_list_of_dicts(self, valid_rule_data: dict) -> None:
        valid_rule_data["rationalization_counters"] = [
            {"thought": "Too simple", "counter": "Every project goes through process"},
        ]
        r = Rule(**valid_rule_data)
        assert len(r.rationalization_counters) == 1
        assert r.rationalization_counters[0]["thought"] == "Too simple"

    def test_red_flag_thoughts_list_of_strings(self, valid_rule_data: dict) -> None:
        valid_rule_data["red_flag_thoughts"] = ["Just this once", "Emergency"]
        r = Rule(**valid_rule_data)
        assert r.red_flag_thoughts == ["Just this once", "Emergency"]

    def test_always_on_sets_true(self, valid_rule_data: dict) -> None:
        valid_rule_data["always_on"] = True
        r = Rule(**valid_rule_data)
        assert r.always_on is True

    def test_mechanical_enforcement_path_string(self, valid_rule_data: dict) -> None:
        valid_rule_data["mechanical_enforcement_path"] = ".claude/hooks/foo.sh"
        r = Rule(**valid_rule_data)
        assert r.mechanical_enforcement_path == ".claude/hooks/foo.sh"

    def test_body_optional_string(self, valid_rule_data: dict) -> None:
        valid_rule_data["body"] = "Additional teaching content"
        r = Rule(**valid_rule_data)
        assert r.body == "Additional teaching content"

    def test_source_attribution_format(self, valid_rule_data: dict) -> None:
        valid_rule_data["source_attribution"] = "superpowers@5.0.7"
        r = Rule(**valid_rule_data)
        assert r.source_attribution == "superpowers@5.0.7"

    def test_source_commit_reserved(self, valid_rule_data: dict) -> None:
        valid_rule_data["source_commit"] = "b557648"
        r = Rule(**valid_rule_data)
        assert r.source_commit == "b557648"


# --- Retrievable node types ----------------------------------------------------


class TestSkillValidation:
    def test_valid_skill_parses(self, skill_data: dict) -> None:
        s = Skill(**skill_data)
        assert s.skill_id == "SKL-PROC-BRAIN-001"
        assert s.severity == Severity.HIGH

    def test_skill_missing_statement_rejected(self, skill_data: dict) -> None:
        del skill_data["statement"]
        with pytest.raises(ValidationError):
            Skill(**skill_data)

    def test_skill_missing_trigger_rejected(self, skill_data: dict) -> None:
        del skill_data["trigger"]
        with pytest.raises(ValidationError):
            Skill(**skill_data)

    def test_skill_missing_rationale_rejected(self, skill_data: dict) -> None:
        del skill_data["rationale"]
        with pytest.raises(ValidationError):
            Skill(**skill_data)

    def test_skill_id_pattern_enforced(self, skill_data: dict) -> None:
        skill_data["skill_id"] = "invalid id"
        with pytest.raises(ValidationError):
            Skill(**skill_data)

    def test_skill_body_defaults_empty(self, skill_data: dict) -> None:
        s = Skill(**skill_data)
        assert s.body == ""

    def test_skill_source_attribution_optional(self, skill_data: dict) -> None:
        s = Skill(**skill_data)
        assert s.source_attribution is None

    def test_skill_roundtrip(self, skill_data: dict) -> None:
        s = Skill(**skill_data)
        dumped = s.model_dump()
        s2 = Skill(**dumped)
        assert s == s2


class TestPlaybookValidation:
    def test_valid_playbook_parses(self, playbook_data: dict) -> None:
        p = Playbook(**playbook_data)
        assert p.playbook_id == "PBK-PROC-BRAIN-001"
        assert p.phase_ids == ["PHA-BRAIN-001", "PHA-BRAIN-002"]

    def test_playbook_requires_phase_ids(self, playbook_data: dict) -> None:
        del playbook_data["phase_ids"]
        with pytest.raises(ValidationError):
            Playbook(**playbook_data)

    def test_playbook_dispatched_roles_default_empty(self, playbook_data: dict) -> None:
        p = Playbook(**playbook_data)
        assert p.dispatched_roles == []

    def test_playbook_preconditions_default_empty(self, playbook_data: dict) -> None:
        p = Playbook(**playbook_data)
        assert p.preconditions == []

    def test_playbook_roundtrip(self, playbook_data: dict) -> None:
        p = Playbook(**playbook_data)
        assert Playbook(**p.model_dump()) == p


class TestTechniqueValidation:
    def test_valid_technique_parses(self, skill_data: dict) -> None:
        # Technique reuses the common-base fields.
        skill_data["technique_id"] = skill_data.pop("skill_id").replace("SKL-", "TEC-")
        t = Technique(**skill_data)
        assert t.technique_id.startswith("TEC-")

    def test_technique_roundtrip(self, skill_data: dict) -> None:
        skill_data["technique_id"] = skill_data.pop("skill_id").replace("SKL-", "TEC-")
        t = Technique(**skill_data)
        assert Technique(**t.model_dump()) == t


class TestAntiPatternValidation:
    def test_valid_antipattern_parses(self, antipattern_data: dict) -> None:
        a = AntiPattern(**antipattern_data)
        assert a.counter_nodes == ["PBK-PROC-TDD-001"]

    def test_antipattern_requires_counter_nodes(self, antipattern_data: dict) -> None:
        del antipattern_data["counter_nodes"]
        with pytest.raises(ValidationError):
            AntiPattern(**antipattern_data)

    def test_antipattern_named_in_optional(self, antipattern_data: dict) -> None:
        a = AntiPattern(**antipattern_data)
        assert a.named_in is None

    def test_antipattern_roundtrip(self, antipattern_data: dict) -> None:
        a = AntiPattern(**antipattern_data)
        assert AntiPattern(**a.model_dump()) == a


class TestForbiddenResponseValidation:
    def test_valid_forbidden_parses(self, forbidden_data: dict) -> None:
        f = ForbiddenResponse(**forbidden_data)
        assert len(f.forbidden_phrases) == 2
        assert f.always_on is True  # default for this type

    def test_forbidden_requires_phrases(self, forbidden_data: dict) -> None:
        del forbidden_data["forbidden_phrases"]
        with pytest.raises(ValidationError):
            ForbiddenResponse(**forbidden_data)

    def test_forbidden_requires_what_to_say_instead(self, forbidden_data: dict) -> None:
        del forbidden_data["what_to_say_instead"]
        with pytest.raises(ValidationError):
            ForbiddenResponse(**forbidden_data)

    def test_forbidden_roundtrip(self, forbidden_data: dict) -> None:
        f = ForbiddenResponse(**forbidden_data)
        assert ForbiddenResponse(**f.model_dump()) == f


# --- Non-retrievable node types ------------------------------------------------


class TestPhaseValidation:
    def test_valid_phase_parses(self, phase_data: dict) -> None:
        p = Phase(**phase_data)
        assert p.position == 1
        assert p.parent_playbook_id == "PBK-PROC-BRAIN-001"

    def test_phase_id_three_digit_suffix_required(self, phase_data: dict) -> None:
        phase_data["phase_id"] = "PHA-BRAIN-01"  # two digits — invalid per pattern
        with pytest.raises(ValidationError):
            Phase(**phase_data)

    def test_phase_severity_optional(self, phase_data: dict) -> None:
        p = Phase(**phase_data)
        assert p.severity is None

    def test_phase_requires_parent_playbook(self, phase_data: dict) -> None:
        del phase_data["parent_playbook_id"]
        with pytest.raises(ValidationError):
            Phase(**phase_data)

    def test_phase_roundtrip(self, phase_data: dict) -> None:
        p = Phase(**phase_data)
        assert Phase(**p.model_dump()) == p


class TestRationalizationValidation:
    def test_valid_rationalization_parses(self, rationalization_data: dict) -> None:
        r = Rationalization(**rationalization_data)
        assert r.thought == "This task is obviously simple."
        assert r.attached_to == "ENF-PROC-BRAIN-001"

    def test_rationalization_requires_thought_and_counter(self, rationalization_data: dict) -> None:
        del rationalization_data["thought"]
        with pytest.raises(ValidationError):
            Rationalization(**rationalization_data)

    def test_rationalization_requires_attached_to(self, rationalization_data: dict) -> None:
        del rationalization_data["attached_to"]
        with pytest.raises(ValidationError):
            Rationalization(**rationalization_data)

    def test_rationalization_severity_optional(self, rationalization_data: dict) -> None:
        r = Rationalization(**rationalization_data)
        assert r.severity is None

    def test_rationalization_roundtrip(self, rationalization_data: dict) -> None:
        r = Rationalization(**rationalization_data)
        assert Rationalization(**r.model_dump()) == r


class TestPressureScenarioValidation:
    def test_valid_scenario_parses(self, scenario_data: dict) -> None:
        s = PressureScenario(**scenario_data)
        assert s.difficulty == "easy"
        assert s.rule_under_test == "ENF-PROC-BRAIN-001"

    def test_scenario_requires_failure_patterns(self, scenario_data: dict) -> None:
        del scenario_data["failure_patterns"]
        with pytest.raises(ValidationError):
            PressureScenario(**scenario_data)

    def test_scenario_requires_rule_under_test(self, scenario_data: dict) -> None:
        del scenario_data["rule_under_test"]
        with pytest.raises(ValidationError):
            PressureScenario(**scenario_data)

    def test_scenario_roundtrip(self, scenario_data: dict) -> None:
        s = PressureScenario(**scenario_data)
        assert PressureScenario(**s.model_dump()) == s


class TestWorkedExampleValidation:
    def test_valid_example_parses(self, example_data: dict) -> None:
        e = WorkedExample(**example_data)
        assert e.linked_skill == "PBK-PROC-TDD-001"

    def test_example_requires_linked_skill(self, example_data: dict) -> None:
        del example_data["linked_skill"]
        with pytest.raises(ValidationError):
            WorkedExample(**example_data)

    def test_example_roundtrip(self, example_data: dict) -> None:
        e = WorkedExample(**example_data)
        assert WorkedExample(**e.model_dump()) == e


class TestSubagentRoleValidation:
    def test_valid_role_parses(self, role_data: dict) -> None:
        r = SubagentRole(**role_data)
        assert r.name == "writ-code-reviewer"
        assert r.prompt_template.startswith("You are")

    def test_role_requires_prompt_template(self, role_data: dict) -> None:
        del role_data["prompt_template"]
        with pytest.raises(ValidationError):
            SubagentRole(**role_data)

    def test_role_dispatched_by_defaults_empty(self, role_data: dict) -> None:
        r = SubagentRole(**role_data)
        assert r.dispatched_by == []

    def test_role_model_preference_optional(self, role_data: dict) -> None:
        r = SubagentRole(**role_data)
        assert r.model_preference is None

    def test_role_roundtrip(self, role_data: dict) -> None:
        r = SubagentRole(**role_data)
        assert SubagentRole(**r.model_dump()) == r


# --- Edge types ----------------------------------------------------------------


class TestNewEdgeTypes:
    """All 8 new edge types extend _DirectedEdge; carry source_id + target_id; reject empty endpoints."""

    @pytest.mark.parametrize(
        "edge_cls",
        [Teaches, Counters, Demonstrates, Dispatches, Gates, PressureTests, Contains, AttachedTo],
    )
    def test_valid_edge_parses(self, edge_cls) -> None:
        e = edge_cls(source_id="SKL-PROC-BRAIN-001", target_id="PBK-PROC-BRAIN-001")
        assert e.source_id == "SKL-PROC-BRAIN-001"
        assert e.target_id == "PBK-PROC-BRAIN-001"

    @pytest.mark.parametrize(
        "edge_cls",
        [Teaches, Counters, Demonstrates, Dispatches, Gates, PressureTests, Contains, AttachedTo],
    )
    def test_edge_rejects_empty_source(self, edge_cls) -> None:
        with pytest.raises(ValidationError):
            edge_cls(source_id="", target_id="PBK-PROC-BRAIN-001")

    @pytest.mark.parametrize(
        "edge_cls",
        [Teaches, Counters, Demonstrates, Dispatches, Gates, PressureTests, Contains, AttachedTo],
    )
    def test_edge_rejects_empty_target(self, edge_cls) -> None:
        with pytest.raises(ValidationError):
            edge_cls(source_id="SKL-PROC-BRAIN-001", target_id="")


# --- NodeType enum -------------------------------------------------------------


class TestNodeTypeEnum:
    def test_all_planned_types_present(self) -> None:
        values = {nt.value for nt in NodeType}
        expected = {
            "Rule", "Abstraction",
            "Skill", "Playbook", "Technique", "AntiPattern", "ForbiddenResponse",
            "Phase", "Rationalization", "PressureScenario", "WorkedExample", "SubagentRole",
        }
        assert expected <= values


# --- Source attribution on all new node types ---------------------------------


class TestSourceAttributionUniformity:
    """All new node types carry source_attribution: str | None and source_commit: str | None."""

    @pytest.mark.parametrize(
        "model_cls,fixture_name",
        [
            (Skill, "skill_data"),
            (Playbook, "playbook_data"),
            (AntiPattern, "antipattern_data"),
            (ForbiddenResponse, "forbidden_data"),
            (Phase, "phase_data"),
            (Rationalization, "rationalization_data"),
            (PressureScenario, "scenario_data"),
            (WorkedExample, "example_data"),
            (SubagentRole, "role_data"),
        ],
    )
    def test_source_fields_default_none(self, model_cls, fixture_name, request) -> None:
        data = request.getfixturevalue(fixture_name)
        instance = model_cls(**data)
        assert instance.source_attribution is None
        assert instance.source_commit is None

    @pytest.mark.parametrize(
        "model_cls,fixture_name",
        [
            (Skill, "skill_data"),
            (Playbook, "playbook_data"),
            (AntiPattern, "antipattern_data"),
            (ForbiddenResponse, "forbidden_data"),
        ],
    )
    def test_source_fields_populate(self, model_cls, fixture_name, request) -> None:
        data = request.getfixturevalue(fixture_name)
        data["source_attribution"] = "superpowers@5.0.7"
        data["source_commit"] = "b557648"
        instance = model_cls(**data)
        assert instance.source_attribution == "superpowers@5.0.7"
        assert instance.source_commit == "b557648"
