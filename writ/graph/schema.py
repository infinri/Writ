"""Writ graph schema -- Pydantic models for all node and edge types."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, field_validator

# Per ARCH-CONST-001: named constants for validation patterns.
# Matches: ARCH-ORG-001, FW-M2-RT-003, ENF-GATE-FINAL, DB-SQL-001, SEC-UNI-001
RULE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)+(-\d{3}|(-[A-Z][A-Z0-9]*))$")

# Phase 1c: scope values are format-validated, not membership-validated.
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

STALENESS_WINDOW_DEFAULT = 365
EVIDENCE_DEFAULT = "doc:original-bible"

# Phase 1d: documented enforcement field conventions for rule authors.
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
    evidence: str = EVIDENCE_DEFAULT
    staleness_window: int = STALENESS_WINDOW_DEFAULT
    last_validated: date

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
