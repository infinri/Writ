"""Writ graph schema -- Pydantic models for all node and edge types."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# Per ARCH-CONST-001: named constants for validation patterns.
# Matches: ARCH-ORG-001, FW-M2-RT-003, ENF-GATE-FINAL, DB-SQL-001, SEC-UNI-001
RULE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)+(-\d{3}|(-[A-Z][A-Z0-9]*))$")

# Phase 1c: scope values are format-validated, not membership-validated.
# Any lowercase string matching this pattern is a valid scope.
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

STALENESS_WINDOW_DEFAULT = 365
EVIDENCE_DEFAULT = "doc:original-bible"
REDUNDANCY_SIMILARITY_THRESHOLD = 0.95

# Phase 3a: valid authority values for Rule nodes.
VALID_AUTHORITIES = ("human", "ai-provisional", "ai-promoted")

# Phase 1d: documented enforcement field conventions for rule authors.
# Not enforced in code -- exists for discoverability.
ENFORCEMENT_CONVENTIONS = (
    "human-review",
    "judgment-gate",
    "training-feedback",
    "audit-log",
    "advisory-only",
)


# --- Enums ---


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(str, Enum):
    BATTLE_TESTED = "battle-tested"
    PRODUCTION_VALIDATED = "production-validated"
    PEER_REVIEWED = "peer-reviewed"
    SPECULATIVE = "speculative"


class EvidenceType(str, Enum):
    INCIDENT = "incident"
    PR = "pr"
    DOC = "doc"
    ADR = "adr"


class NodeType(str, Enum):
    """All node types in the graph. Retrievable subset per plan Section 2.3."""

    RULE = "Rule"
    ABSTRACTION = "Abstraction"
    # Retrievable (participate in Stage 1-3 ranking)
    SKILL = "Skill"
    PLAYBOOK = "Playbook"
    TECHNIQUE = "Technique"
    ANTIPATTERN = "AntiPattern"
    FORBIDDEN_RESPONSE = "ForbiddenResponse"
    # Non-retrievable (bundle-expansion / template-only; Stage 4 surfacing only)
    PHASE = "Phase"
    RATIONALIZATION = "Rationalization"
    PRESSURE_SCENARIO = "PressureScenario"
    WORKED_EXAMPLE = "WorkedExample"
    SUBAGENT_ROLE = "SubagentRole"


RETRIEVABLE_NODE_TYPES = frozenset({
    NodeType.RULE,
    NodeType.ABSTRACTION,
    NodeType.SKILL,
    NodeType.PLAYBOOK,
    NodeType.TECHNIQUE,
    NodeType.ANTIPATTERN,
    NodeType.FORBIDDEN_RESPONSE,
})


# --- Node Models ---


class Rule(BaseModel):
    """A single enforceable rule in the knowledge graph.

    Per PY-PYDANTIC-001: validates all fields at the data boundary.
    """

    rule_id: str
    domain: str
    severity: Severity
    scope: str
    trigger: str
    statement: str
    violation: str
    pass_example: str
    enforcement: str
    rationale: str
    mandatory: bool = False
    confidence: Confidence = Confidence.PRODUCTION_VALIDATED
    authority: str = "human"
    times_seen_positive: int = 0
    times_seen_negative: int = 0
    last_seen: str | None = None
    evidence: str = EVIDENCE_DEFAULT
    staleness_window: int = STALENESS_WINDOW_DEFAULT
    last_validated: date
    # Phase 1 additions per plan Section 6.1 and docs/phase-0-schema-proposal.md.
    # All default to not-set so existing 80 rules remain valid without migration.
    rationalization_counters: list[dict[str, str]] = Field(default_factory=list)
    red_flag_thoughts: list[str] = Field(default_factory=list)
    always_on: bool = False
    mechanical_enforcement_path: str | None = None
    body: str = ""
    source_attribution: str | None = None
    source_commit: str | None = None

    @field_validator("rule_id")
    @classmethod
    def validate_rule_id(cls, v: str) -> str:
        if not v:
            raise ValueError("rule_id must not be empty")
        if not RULE_ID_PATTERN.match(v):
            raise ValueError(
                f"rule_id '{v}' does not match required format "
                "(e.g., ARCH-ORG-001, FW-M2-RT-003, ENF-GATE-FINAL)"
            )
        return v

    @field_validator("trigger", "statement", "violation", "pass_example", "enforcement", "rationale")
    @classmethod
    def validate_non_empty_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be empty or whitespace-only")
        return v

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("domain must not be empty")
        return v

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        if not SCOPE_PATTERN.match(v):
            raise ValueError(
                f"scope '{v}' must be lowercase, start with a letter, "
                "and match [a-z][a-z0-9_-]*"
            )
        return v

    @field_validator("authority")
    @classmethod
    def validate_authority(cls, v: str) -> str:
        if v not in VALID_AUTHORITIES:
            raise ValueError(
                f"authority '{v}' must be one of: {', '.join(VALID_AUTHORITIES)}"
            )
        return v


class Abstraction(BaseModel):
    abstraction_id: str
    summary: str
    rule_ids: list[str]
    domain: str
    compression_ratio: float


class Domain(BaseModel):
    name: str
    rule_count: int
    last_updated: datetime


class Evidence(BaseModel):
    evidence_id: str
    type: EvidenceType
    reference: str
    date: date


class Tag(BaseModel):
    name: str
    rule_count: int


# --- Edge Models ---


class _DirectedEdge(BaseModel):
    """Base for directed edges. Per ARCH-DRY-001: shared validation in one place."""

    source_id: str
    target_id: str

    @field_validator("source_id", "target_id")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("edge endpoint must not be empty")
        return v


class DependsOn(_DirectedEdge):
    pass


class Precedes(_DirectedEdge):
    pass


class ConflictsWith(_DirectedEdge):
    pass


class Supplements(_DirectedEdge):
    pass


class Supersedes(_DirectedEdge):
    pass


class RelatedTo(_DirectedEdge):
    pass


class AppliesTo(BaseModel):
    rule_id: str
    target_name: str
    target_type: str


class Abstracts(BaseModel):
    abstraction_id: str
    rule_ids: list[str]


class JustifiedBy(BaseModel):
    rule_id: str
    evidence_id: str


# --- Phase 1: Methodology node types per plan Section 6.1 ---
# Schema design signed off in docs/phase-0-schema-proposal.md. Ingest parser
# (Phase 1 deliverable 2) populates these from <!-- NODE START type=X id=Y -->
# markers in markdown fixtures. Neo4j migration (deliverable 3) creates a label
# per node_type and a relationship type per new edge class.


def _normalize_tags(v: list[str]) -> list[str]:
    """Deterministic tag canonicalization: lowercase, deduplicate, sort.

    Per docs/phase-0-schema-proposal.md resolved-decision 6: prevents BM25 index
    inconsistency ("TDD" vs "tdd" as distinct terms). Applied at the Pydantic
    boundary which is the ingest boundary for fixtures and API payloads.
    """
    return sorted({t.lower() for t in v})


class _MethodologyNodeBase(BaseModel):
    """Shared fields for every new node type. Per-type id field and type-specific
    fields are declared on subclasses.

    Retrievable subclasses override `severity` to required (non-optional);
    non-retrievable subclasses leave it as `Severity | None = None`.
    """

    domain: str
    scope: str
    trigger: str
    statement: str
    rationale: str
    tags: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.PRODUCTION_VALIDATED
    authority: str = "human"
    last_validated: date
    staleness_window: int = STALENESS_WINDOW_DEFAULT
    evidence: str = "doc:superpowers"
    times_seen_positive: int = 0
    times_seen_negative: int = 0
    last_seen: str | None = None
    source_attribution: str | None = None
    source_commit: str | None = None
    body: str = ""

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("domain must not be empty")
        return v

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, v: str) -> str:
        if not SCOPE_PATTERN.match(v):
            raise ValueError(
                f"scope '{v}' must be lowercase, start with a letter, and match [a-z][a-z0-9_-]*"
            )
        return v

    @field_validator("trigger", "statement", "rationale")
    @classmethod
    def _validate_non_empty_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be empty or whitespace-only")
        return v

    @field_validator("authority")
    @classmethod
    def _validate_authority(cls, v: str) -> str:
        if v not in VALID_AUTHORITIES:
            raise ValueError(f"authority '{v}' must be one of: {', '.join(VALID_AUTHORITIES)}")
        return v

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        return _normalize_tags(v)


class _RetrievableBase(_MethodologyNodeBase):
    """Retrievable types require severity (feeds ranking weight w_severity)."""

    severity: Severity


class _NonRetrievableBase(_MethodologyNodeBase):
    """Non-retrievable types never enter ranking; severity is optional metadata."""

    severity: Severity | None = None


def _validate_node_id(field_name: str):
    """Factory for per-type node_id validators using the shared RULE_ID_PATTERN."""

    def _validator(cls, v: str) -> str:
        if not v:
            raise ValueError(f"{field_name} must not be empty")
        if not RULE_ID_PATTERN.match(v):
            raise ValueError(
                f"{field_name} '{v}' does not match required format (e.g., SKL-PROC-BRAIN-001)"
            )
        return v

    return _validator


# --- Retrievable node types (Stage 1-3 ranking participants) ---


class Skill(_RetrievableBase):
    skill_id: str

    _validate_skill_id = field_validator("skill_id")(_validate_node_id("skill_id"))


class Playbook(_RetrievableBase):
    playbook_id: str
    phase_ids: list[str]
    preconditions: list[str] = Field(default_factory=list)
    dispatched_roles: list[str] = Field(default_factory=list)

    _validate_playbook_id = field_validator("playbook_id")(_validate_node_id("playbook_id"))


class Technique(_RetrievableBase):
    technique_id: str

    _validate_technique_id = field_validator("technique_id")(_validate_node_id("technique_id"))


class AntiPattern(_RetrievableBase):
    antipattern_id: str
    counter_nodes: list[str]
    named_in: str | None = None

    _validate_antipattern_id = field_validator("antipattern_id")(_validate_node_id("antipattern_id"))


class ForbiddenResponse(_RetrievableBase):
    forbidden_id: str
    forbidden_phrases: list[str]
    what_to_say_instead: str
    always_on: bool = True

    _validate_forbidden_id = field_validator("forbidden_id")(_validate_node_id("forbidden_id"))

    @field_validator("what_to_say_instead")
    @classmethod
    def _validate_what_to_say(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("what_to_say_instead must not be empty")
        return v


# --- Non-retrievable node types (Stage 4 bundle expansion only) ---


class Phase(_NonRetrievableBase):
    phase_id: str
    position: int
    name: str
    description: str
    parent_playbook_id: str

    _validate_phase_id = field_validator("phase_id")(_validate_node_id("phase_id"))


class Rationalization(_NonRetrievableBase):
    rationalization_id: str
    thought: str
    counter: str
    attached_to: str

    _validate_rationalization_id = field_validator("rationalization_id")(_validate_node_id("rationalization_id"))


class PressureScenario(_NonRetrievableBase):
    scenario_id: str
    prompt: str
    expected_compliance: str
    failure_patterns: list[str]
    rule_under_test: str
    difficulty: str

    _validate_scenario_id = field_validator("scenario_id")(_validate_node_id("scenario_id"))


class WorkedExample(_NonRetrievableBase):
    example_id: str
    title: str
    before: str
    applied_skill: str
    result: str
    linked_skill: str

    _validate_example_id = field_validator("example_id")(_validate_node_id("example_id"))


class SubagentRole(_NonRetrievableBase):
    role_id: str
    name: str
    prompt_template: str
    dispatched_by: list[str] = Field(default_factory=list)
    model_preference: str | None = None

    _validate_role_id = field_validator("role_id")(_validate_node_id("role_id"))


# --- New edge types per plan Section 3.1 ---
# 8 new directed edges (6 new + PRECEDES and CONTAINS-family already implied by
# existing _DirectedEdge pattern). Each extends _DirectedEdge for shared endpoint
# validation. Neo4j relationship type matches class name uppercased-with-underscores
# (e.g. PressureTests → PRESSURE_TESTS).


class Teaches(_DirectedEdge):
    """Skill/Playbook → Rule/Technique: 'teaches the enforcement target'."""


class Counters(_DirectedEdge):
    """AntiPattern/Rationalization → Skill/Playbook/Rule: 'countered by the target'."""


class Demonstrates(_DirectedEdge):
    """WorkedExample/ForbiddenResponse → Skill/Rule: 'demonstrates the target's discipline'."""


class Dispatches(_DirectedEdge):
    """Playbook/Skill → SubagentRole/Technique: 'target is dispatched as sub-invocation'."""


class Gates(_DirectedEdge):
    """Rule → Skill/Playbook: 'mechanical enforcement of the target's discipline'."""


class PressureTests(_DirectedEdge):
    """PressureScenario → Rule/Skill/Playbook: 'scenario tests compliance with target'."""


class Contains(_DirectedEdge):
    """Playbook → Phase: 'phase is a structural member of playbook'."""


class AttachedTo(_DirectedEdge):
    """Rationalization → Skill/Playbook/Rule: 'rationalization attached to parent'."""
