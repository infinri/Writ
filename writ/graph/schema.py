"""Writ graph schema -- Pydantic models for all node and edge types."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# Per ARCH-CONST-001: named constants for validation patterns.
# Matches: ARCH-ORG-001, FW-M2-RT-003, ENF-GATE-007, DB-SQL-001, SEC-UNI-001
RULE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)+(-\d{3}|(-[A-Z][A-Z0-9]*))$")

# Phase 1c: scope values are format-validated, not membership-validated.
# Any lowercase string matching this pattern is a valid scope.
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

STALENESS_WINDOW_DEFAULT = 365
EVIDENCE_DEFAULT = "doc:original-bible"
REDUNDANCY_SIMILARITY_THRESHOLD = 0.95

# Phase 3a: valid authority values for Rule nodes.
VALID_AUTHORITIES = ("human", "ai-provisional", "ai-promoted")

# Phase 6.1: the provenance lineage of a node -- the 4-state refinement of 0.10's
# binary source_origin (ingest | graph-authored). hand-authored/graduated are the
# ingest (markdown-home) states; proposed/graduation_pending are the graph-first
# (no-home-yet) states. Default hand-authored: the bible corpus at rest. proposed is
# set by /propose + cli add; graduation_pending by the frequency crossing (6.3a);
# graduated by the human promotion gate's export-to-source (6.3c).
VALID_PROVENANCE = ("hand-authored", "proposed", "graduation_pending", "graduated", "record")
PROVENANCE_DEFAULT = "hand-authored"
# The graph-first (no-markdown-home-yet) states: a node in one of these is transient
# and PROMOTABLE; the canonical states {hand-authored, graduated} must have a
# source home. This is the promotable axis; do NOT add 'record' here -- records are
# permanent, never promoted, so a future promotion sweep gated on this set must never
# auto-include them.
GRAPH_FIRST_PROVENANCE = frozenset({"proposed", "graduation_pending"})
# Decision-memory Phase 1a: the set the reconcile-deletion + parity exemptions key on.
# A union of the promotable graph-first states and the permanent 'record' state, so a
# 'record' node survives reconcile and is parity-exempt WITHOUT inheriting any future
# promotion behavior gated on GRAPH_FIRST_PROVENANCE.
PARITY_EXEMPT_PROVENANCE = GRAPH_FIRST_PROVENANCE | {"record"}

# Phase 6.3c: HOW a graduated node became canon (authorship axis, orthogonal to the
# provenance lineage axis). human-approve-asis = the human promoted the candidate text
# verbatim; human-edit = the human edited it at the gate before promotion. None for
# non-graduated nodes. Provenance records earned-by-evidence; graduated_via records who
# wrote the words -- a human-edited graduated node STAYS graduated (lineage preserved).
VALID_GRADUATED_VIA = ("human-approve-asis", "human-edit")

# Phase 3.5: the closed domain vocabulary -- the 16 top-level bible/ rule dirs
# plus `routing` (the methodology Category tree's domain). A node carrying a
# domain outside this set is a taxonomy drift (e.g. the pre-normalization
# `AI Enforcement`, `PHP / Error Handling`, casing dup `Architecture`).
# Membership is checked by integrity.detect_domain_enum_invariant against the
# full corpus (NOT at the Pydantic boundary -- test fixtures use placeholder
# domains like "Test"). `languages` intentionally collapses PHP+Python+SQL-idiom
# rules (maintainer decision 2026-06-14).
VALID_DOMAINS: frozenset[str] = frozenset({
    "api-design", "architecture", "code-quality", "communication", "database",
    "documentation", "enforcement", "frameworks", "languages", "meta-authoring",
    "performance", "process", "research", "scaling", "security", "testing",
    "routing",
})

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


class RouteValue(str, Enum):
    """Routing modes a Category can declare. A node's BELONGS_TO category routes
    determine how it surfaces (Phase 0 §Chosen Approach #1)."""

    SEMANTIC = "semantic"
    SCOPED = "scoped"
    STATE = "state"
    ACTION = "action"
    ALWAYS_ON = "always_on"
    PULL = "pull"
    RIDE_ALONG = "ride_along"


VALID_ROUTES: frozenset[str] = frozenset(rv.value for rv in RouteValue)

# The routes whose delivery mechanism is actually IMPLEMENTED -- the route analog
# of integrity's KNOWN_ACTIONS. A Category may DECLARE any VALID_ROUTES value; a
# declared route outside this set reaches nobody, so
# detect_route_implementation_closure fails `writ validate` on it.
#
# HAND-LISTED, NOT DERIVED, and the direction matters. Deriving it as
# `VALID_ROUTES - _UNWIRED_ROUTES` reads tidier and keeps the two sets in step
# with the enum for free, but it makes a route added to RouteValue WIRED BY
# DEFAULT: the new value lands in WIRED_ROUTES with no implementation behind it
# and the closure check stays green, which is precisely how `ride_along` survived
# for months. The explicit list inverts the default to UNWIRED, so a category
# declaring a brand-new route trips the check immediately. The cost is one edit
# here when a route genuinely gains an implementation, and that edit IS the
# conscious decision the derived form only appeared to force. Do not "simplify"
# this back to a subtraction.
#
# Each entry names the mechanism that serves it, verified by reading the code:
#   semantic   -- RulesRetrievalPipeline admits a candidate to the ranked pool
#                 only when its route list contains this value
#                 (writ/retrieval/pipeline.py:517). The ONLY route whose STRING a
#                 delivery branch reads.
#   pull       -- MethodologyTriggerIndex pull channel, keyed on the member's own
#                 trigger_keywords (writ/retrieval/trigger_index.py:172).
#   action     -- the same index's push channel, keyed on the member's
#                 action_triggers against KNOWN_ACTIONS (trigger_index push).
#   state      -- the mode floor, keyed on the member's floor_modes against
#                 EXPECTED_FLOORS (writ/graph/integrity/_common.py).
#   always_on  -- the injection channel, INJECTION_RULE_WHERE
#                 (writ/graph/predicates.py:16) selecting mandatory/always_on
#                 members every turn.
# For the four non-semantic entries the route value is metadata: the channel keys
# off the MEMBER's property, not off the category's route string. They are listed
# because the mechanism exists and demonstrably delivers those categories'
# members, which is what "wired" has to mean for a check about undeliverable
# nodes. Their per-route accuracy is policed elsewhere (pull by
# detect_trigger_keyword_invariant's pull_orphans, action by
# detect_push_reachability's empty_action_routes, state by
# detect_floor_completeness).
#
# DELIBERATELY ABSENT:
#   ride_along -- no mechanism anywhere. It exists as a RouteValue member, a seed
#                 in scripts/migrate_phase0_categories.py, and a test asserting a
#                 Category may declare it. CAT-DISC-001 declared it as its ONLY
#                 route, leaving 26 members with no channel able to select them
#                 (14 AntiPatterns verified unreachable by every live path in all
#                 five modes) while every existing invariant stayed green, because
#                 each was scoped to the route it was written for.
#   scoped     -- also implemented nowhere, found while verifying this list. No
#                 code reads the value and no language/framework/file-type scoping
#                 mechanism exists to read it; its four declaring categories
#                 (CAT-CODE-FW-MAGENTO-001, CAT-CODE-LANG-{PHP,PYTHON,SQL}-001)
#                 co-declare `semantic`, so unlike ride_along it strands no member
#                 -- it is an inert adjective on an otherwise wired category.
# Retiring either enum value is deferred: deleting it from VALID_ROUTES would make
# any surviving corpus file carrying it unparseable at ingest. Both stay legal to
# declare, and declaring either now fails loud.
WIRED_ROUTES: frozenset[str] = frozenset({
    RouteValue.SEMANTIC.value,
    RouteValue.PULL.value,
    RouteValue.ACTION.value,
    RouteValue.STATE.value,
    RouteValue.ALWAYS_ON.value,
})

# Anti-drift guard, the one real benefit the derived form had: a typo in the list
# above cannot invent a route the schema does not know. SUBSET, never equality --
# the two sets are MEANT to differ while a route is unimplemented, and asserting
# equality would re-wire every unwired route by definition. A bare `assert` would
# vanish under `python -O`, so this raises.
if not WIRED_ROUTES <= VALID_ROUTES:
    raise ValueError(
        f"WIRED_ROUTES contains route(s) absent from VALID_ROUTES: "
        f"{sorted(WIRED_ROUTES - VALID_ROUTES)}; every wired route must be a "
        f"RouteValue member (valid: {sorted(VALID_ROUTES)})"
    )


class NodeType(str, Enum):
    """All node types in the graph. Retrievable subset per plan Section 2.3."""

    RULE = "Rule"
    ABSTRACTION = "Abstraction"
    CATEGORY = "Category"
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


def _validate_node_id(field_name: str, expected_prefix: str | None = None):
    """Factory for per-type node_id validators using the shared RULE_ID_PATTERN.

    If expected_prefix is provided (e.g., "SKL-"), the validator also rejects
    IDs that match the format but use the wrong type prefix. Each node type
    has a fixed prefix: SKL- Skill, PBK- Playbook, TEC- Technique,
    ANT- AntiPattern, FRB- ForbiddenResponse, PHA- Phase, RAT- Rationalization,
    PSC- PressureScenario, EXM- WorkedExample, ROL- SubagentRole, CAT- Category.
    """

    def _validator(cls, v: str) -> str:
        if not v:
            raise ValueError(f"{field_name} must not be empty")
        if not RULE_ID_PATTERN.match(v):
            raise ValueError(
                f"{field_name} '{v}' does not match required format (e.g., SKL-PROC-BRAIN-001)"
            )
        if expected_prefix and not v.startswith(expected_prefix):
            raise ValueError(
                f"{field_name} '{v}' must start with '{expected_prefix}' "
                f"for this node type"
            )
        return v

    return _validator


def _validate_domain_value(cls, v: str) -> str:
    if not v or not v.strip():
        raise ValueError("domain must not be empty")
    return v


def _validate_scope_value(cls, v: str) -> str:
    if not SCOPE_PATTERN.match(v):
        raise ValueError(
            f"scope '{v}' must be lowercase, start with a letter, and match [a-z][a-z0-9_-]*"
        )
    return v


def _validate_authority_value(cls, v: str) -> str:
    if v not in VALID_AUTHORITIES:
        raise ValueError(f"authority '{v}' must be one of: {', '.join(VALID_AUTHORITIES)}")
    return v


def _validate_provenance_value(cls, v: str) -> str:
    """Reusable provenance validator shared by Rule, _MethodologyNodeBase, and the
    standalone node models (Abstraction, Category)."""
    if v not in VALID_PROVENANCE:
        raise ValueError(f"provenance '{v}' must be one of: {', '.join(VALID_PROVENANCE)}")
    return v


def _validate_graduated_via_value(cls, v: str | None) -> str | None:
    if v is not None and v not in VALID_GRADUATED_VIA:
        raise ValueError(
            f"graduated_via '{v}' must be one of: {', '.join(VALID_GRADUATED_VIA)} (or unset)"
        )
    return v


def _validate_non_empty_text_value(cls, v: str) -> str:
    if not v or not v.strip():
        raise ValueError("field must not be empty or whitespace-only")
    return v


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
    # Methodology absorption schema additions (signed off 2026-04-21).
    # All default to not-set so pre-absorption rules remain valid without migration.
    rationalization_counters: list[dict[str, str]] = Field(default_factory=list)
    red_flag_thoughts: list[str] = Field(default_factory=list)
    always_on: bool = False
    # Always-on applicability routing (WRIT-BLUEPRINT 3.5): applicability_scope =
    # the injection points where this rule applies (universal / prompt / write /
    # bash / stop); trigger_keywords = whole-word context keywords matched at those
    # points. An empty scope is treated as universal at injection time (fail-open;
    # a rule is never silently dropped for lacking routing data).
    applicability_scope: list[str] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=list)
    mechanical_enforcement_path: str | None = None
    body: str = ""
    source_attribution: str | None = None
    source_commit: str | None = None
    provenance: str = PROVENANCE_DEFAULT
    graduated_via: str | None = None

    @field_validator("rule_id")
    @classmethod
    def validate_rule_id(cls, v: str) -> str:
        if not v:
            raise ValueError("rule_id must not be empty")
        if not RULE_ID_PATTERN.match(v):
            raise ValueError(
                f"rule_id '{v}' does not match required format "
                "(e.g., ARCH-ORG-001, FW-M2-RT-003, ENF-GATE-007)"
            )
        return v

    _validate_non_empty_text = field_validator(
        "trigger", "statement", "violation", "pass_example", "enforcement", "rationale"
    )(_validate_non_empty_text_value)
    _validate_domain = field_validator("domain")(_validate_domain_value)
    _validate_scope = field_validator("scope")(_validate_scope_value)
    _validate_authority = field_validator("authority")(_validate_authority_value)
    _validate_provenance = field_validator("provenance")(_validate_provenance_value)
    _validate_graduated_via = field_validator("graduated_via")(_validate_graduated_via_value)


class Abstraction(BaseModel):
    abstraction_id: str
    summary: str
    rule_ids: list[str]
    domain: str
    compression_ratio: float
    provenance: str = PROVENANCE_DEFAULT

    _validate_abstraction_id = field_validator("abstraction_id")(
        _validate_node_id("abstraction_id")
    )
    _validate_provenance = field_validator("provenance")(_validate_provenance_value)


class Domain(BaseModel):
    name: str
    rule_count: int
    last_updated: datetime


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


class Abstracts(BaseModel):
    abstraction_id: str
    rule_ids: list[str]


# --- Methodology node types (signed off 2026-04-21) ---
# Ingest parser populates these from <!-- NODE START type=X id=Y --> markers
# in markdown fixtures. Neo4j migration creates a label per node_type and a
# relationship type per new edge class.


def _normalize_tags(v: list[str]) -> list[str]:
    """Deterministic tag canonicalization: lowercase, deduplicate, sort.

    Prevents BM25 index inconsistency ("TDD" vs "tdd" as distinct terms).
    Applied at the Pydantic boundary which is the ingest boundary for
    fixtures and API payloads.
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
    evidence: str = "peer-reviewed"
    times_seen_positive: int = 0
    times_seen_negative: int = 0
    last_seen: str | None = None
    source_attribution: str | None = None
    source_commit: str | None = None
    provenance: str = PROVENANCE_DEFAULT
    graduated_via: str | None = None
    body: str = ""
    # 1.6 routing-as-data (mirrors Category.routes): node-declared routing for the
    # methodology-trigger index. floor_modes = modes this node floors (universal =
    # all modes, folding in the old always_on per D4); action_triggers = actions
    # that push it (1.8); trigger_keywords = curated WHEN-clause keywords for pull
    # (Invariant B, enforced once authored). Empty defaults keep this additive.
    floor_modes: list[str] = Field(default_factory=list)
    action_triggers: list[str] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=list)

    _validate_non_empty_text = field_validator("trigger", "statement", "rationale")(
        _validate_non_empty_text_value
    )
    _validate_domain = field_validator("domain")(_validate_domain_value)
    _validate_scope = field_validator("scope")(_validate_scope_value)
    _validate_authority = field_validator("authority")(_validate_authority_value)
    _validate_provenance = field_validator("provenance")(_validate_provenance_value)
    _validate_graduated_via = field_validator("graduated_via")(_validate_graduated_via_value)

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


# Public alias for downstream consumers (ingest parser, dashboard, tests).
# The underscore-prefixed `_MethodologyNodeBase` remains the canonical
# implementation symbol; `MethodologyNode` is the documented public name.
MethodologyNode = _MethodologyNodeBase


# --- Retrievable node types (Stage 1-3 ranking participants) ---


class Skill(_RetrievableBase):
    skill_id: str

    _validate_skill_id = field_validator("skill_id")(_validate_node_id("skill_id", "SKL-"))


class Playbook(_RetrievableBase):
    playbook_id: str
    phase_ids: list[str]
    preconditions: list[str] = Field(default_factory=list)
    dispatched_roles: list[str] = Field(default_factory=list)

    _validate_playbook_id = field_validator("playbook_id")(_validate_node_id("playbook_id", "PBK-"))


class Technique(_RetrievableBase):
    technique_id: str

    _validate_technique_id = field_validator("technique_id")(_validate_node_id("technique_id", "TEC-"))


class AntiPattern(_RetrievableBase):
    antipattern_id: str
    counter_nodes: list[str]
    named_in: str | None = None

    _validate_antipattern_id = field_validator("antipattern_id")(_validate_node_id("antipattern_id", "ANT-"))


class ForbiddenResponse(_RetrievableBase):
    forbidden_id: str
    forbidden_phrases: list[str]
    what_to_say_instead: str
    always_on: bool = True

    _validate_forbidden_id = field_validator("forbidden_id")(_validate_node_id("forbidden_id", "FRB-"))

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

    _validate_phase_id = field_validator("phase_id")(_validate_node_id("phase_id", "PHA-"))


class Rationalization(_NonRetrievableBase):
    rationalization_id: str
    thought: str
    counter: str
    attached_to: str

    _validate_rationalization_id = field_validator("rationalization_id")(_validate_node_id("rationalization_id", "RAT-"))


class PressureScenario(_NonRetrievableBase):
    scenario_id: str
    prompt: str
    expected_compliance: str
    failure_patterns: list[str]
    rule_under_test: str
    difficulty: str

    _validate_scenario_id = field_validator("scenario_id")(_validate_node_id("scenario_id", "PSC-"))


class WorkedExample(_NonRetrievableBase):
    example_id: str
    title: str
    before: str
    applied_skill: str
    result: str
    linked_skill: str

    _validate_example_id = field_validator("example_id")(_validate_node_id("example_id", "EXM-"))


class SubagentRole(_NonRetrievableBase):
    role_id: str
    name: str
    prompt_template: str
    dispatched_by: list[str] = Field(default_factory=list)
    model_preference: str | None = None
    tools: str | None = None
    description: str | None = None

    _validate_role_id = field_validator("role_id")(_validate_node_id("role_id", "ROL-"))


# --- Category node (Phase 0: data-driven routing) ---
# A Category groups nodes (via BELONGS_TO edges) and declares how its members
# surface through `routes`. Not retrievable: categories never enter ranking;
# they are routing metadata only.


class Category(BaseModel):
    category_id: str
    name: str
    routes: list[str] = Field(default_factory=list)
    parent: str | None = None
    description: str = ""
    provenance: str = PROVENANCE_DEFAULT

    _validate_category_id = field_validator("category_id")(
        _validate_node_id("category_id", "CAT-")
    )
    _validate_provenance = field_validator("provenance")(_validate_provenance_value)

    @field_validator("routes")
    @classmethod
    def _validate_routes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("routes must be non-empty")
        deduped: list[str] = []
        for route in v:
            if route not in VALID_ROUTES:
                raise ValueError(
                    f"route '{route}' is not a valid route "
                    f"(expected one of {sorted(VALID_ROUTES)})"
                )
            if route not in deduped:
                deduped.append(route)
        return deduped


# --- Decision-memory record models (Phase 1a) ---
# Records, NOT retrieval candidates: plain BaseModel subclasses with custom db.py
# create methods. They are deliberately absent from NodeType, RETRIEVABLE_NODE_TYPES,
# NODE_TYPE_MODELS, and NODE_ID_FIELDS so they never enter the RAG pipeline, ingest's
# per-type dispatch, or the coalesced-id reconcile/parity enumeration. Every record
# node carries provenance='record' + source_origin='graph-authored', stamped on the
# node by the create methods (db.py) so all three kinds are reconcile/parity-exempt.
# Decision and FileChange also declare the two as model fields (per the blueprint);
# Commit does not. The create-time stamp is the single source of truth either way, so
# the node is correct regardless of which model carries the field.


class Decision(BaseModel):
    decision_id: str
    project: str
    title: str
    rationale: str
    planned_files: list[dict] = Field(default_factory=list)
    governing_rule_ids: list[str] = Field(default_factory=list)
    phase: str
    session_id: str
    ts: str
    provenance: str = "record"
    source_origin: str = "graph-authored"


class FileChange(BaseModel):
    change_id: str
    project: str
    path: str
    change_type: str
    reason: str
    queried_rule_ids: list[str] = Field(default_factory=list)
    # Commit-time snapshot of the governing Decision's governing_rule_ids. Named
    # `cited` on FileChange vs `governing` on Decision intentionally: it is the
    # frozen citation, not a live join, so the PR comment renders identically
    # after the claim resolves.
    cited_rule_ids: list[str] = Field(default_factory=list)
    commit_hash: str | None = None
    ts: str
    provenance: str = "record"
    source_origin: str = "graph-authored"


class Commit(BaseModel):
    commit_hash: str
    project: str
    subject: str
    author: str
    branch: str
    ts: str


# --- New edge types per plan Section 3.1 ---
# Directed edges. Each extends _DirectedEdge. Neo4j relationship type matches the class name
# uppercased-with-underscores (e.g. PressureTests -> PRESSURE_TESTS). Direction is the design
# contract; INC-2 normalized the corpus to it. The "source-types -> target-types" below are
# the COMPLETE valid endpoint sets (graph traversal is undirected, but the model is directed).


class Teaches(_DirectedEdge):
    """{Skill, Playbook, Technique} -> {Rule, Skill, Playbook, Technique}: source teaches the
    target's discipline (the instructional node points at what it teaches)."""


class Counters(_DirectedEdge):
    """{AntiPattern, Rationalization} -> {Skill, Playbook, Rule}: the anti-pattern/rationalization
    is countered by the target (the anti-pattern points at the discipline that counters it)."""


class Demonstrates(_DirectedEdge):
    """{WorkedExample, ForbiddenResponse, Technique, SubagentRole, Skill} ->
    {Skill, Rule, Playbook, Technique, PressureScenario}: source demonstrates the target's
    discipline (the example/role/technique points at what it illustrates)."""


class Dispatches(_DirectedEdge):
    """{Playbook, Skill} -> {SubagentRole, Technique, Playbook, Skill}: the dispatcher invokes
    the target as a sub-invocation (the orchestrating node points at what it dispatches)."""


class Gates(_DirectedEdge):
    """Rule -> {Skill, Playbook}: the rule mechanically enforces the target's discipline
    (the enforcement rule points at the playbook/skill it gates)."""


class PressureTests(_DirectedEdge):
    """PressureScenario → Rule/Skill/Playbook: 'scenario tests compliance with target'."""


class Contains(_DirectedEdge):
    """Playbook → Phase: 'phase is a structural member of playbook'."""


class AttachedTo(_DirectedEdge):
    """Rationalization → Skill/Playbook/Rule: 'rationalization attached to parent'."""


class BelongsTo(_DirectedEdge):
    """{any node} → Category: 'node belongs to category' (Neo4j BELONGS_TO).
    Carries the literal relationship label for downstream graph writers."""

    edge_type: str = "BELONGS_TO"


# --- Canonical node-type registry (POL-3 / C6: single source) -------------------------------
# node_type -> Pydantic model and node_type -> primary-key field name. ingest.py and db.py
# import/derive from these instead of redefining the maps (adding a node type is now one edit).
NODE_TYPE_MODELS: dict[str, type[BaseModel]] = {
    "Rule": Rule,
    "Abstraction": Abstraction,
    "Category": Category,
    "Skill": Skill,
    "Playbook": Playbook,
    "Technique": Technique,
    "AntiPattern": AntiPattern,
    "ForbiddenResponse": ForbiddenResponse,
    "Phase": Phase,
    "Rationalization": Rationalization,
    "PressureScenario": PressureScenario,
    "WorkedExample": WorkedExample,
    "SubagentRole": SubagentRole,
}

NODE_ID_FIELDS: dict[str, str] = {
    "Rule": "rule_id",
    "Abstraction": "abstraction_id",
    "Category": "category_id",
    "Skill": "skill_id",
    "Playbook": "playbook_id",
    "Technique": "technique_id",
    "AntiPattern": "antipattern_id",
    "ForbiddenResponse": "forbidden_id",
    "Phase": "phase_id",
    "Rationalization": "rationalization_id",
    "PressureScenario": "scenario_id",
    "WorkedExample": "example_id",
    "SubagentRole": "role_id",
}

# Round-trip contract: the Markdown section headers export WRITES and ingest
# READS must match exactly, or an export/import cycle silently loses fields.
# Single source so the map cannot drift across the two modules (was duplicated
# byte-for-byte in export.py and graph/ingest.py).
SECTION_HEADERS: dict[str, str] = {
    "trigger": "### Trigger",
    "statement": "### Statement",
    "violation": "### Violation",
    "pass_example": "### Pass",
    "enforcement": "### Enforcement",
    "rationale": "### Rationale",
}

# The methodology node types (every type except the coding-rule `Rule`).
METHODOLOGY_NODE_TYPES: frozenset[str] = frozenset(NODE_ID_FIELDS) - {"Rule"}

# 0.10 (prop completion): props reconcile may clear when ABSENT from source --
# the property analog of the stale-edge prune. Derived from every node model's
# fields MINUS the props the daemon writes at runtime (frequency/observation) and
# the per-type id fields. The split is the safety story: ONLY names in this
# allowlist are ever removed, so a frequency counter -- or any non-model prop such
# as an embedding or a future runtime field -- is NEVER touched. authority and
# confidence are MANAGED (source-of-truth-at-rest is absolute; they are universally
# source-declared, and Phase 5 graduation exports promotions to source) -- only the
# pure-runtime props below are exempt.
RUNTIME_EXEMPT_PROPS: frozenset[str] = frozenset(
    {"times_seen_positive", "times_seen_negative", "last_seen", "source_origin",
     # 6.1: provenance is graph-side lineage, absent from the hand-authored .md
     # corpus (export omits the default). Like source_origin, reconcile/prop-parity
     # must never clear or flag it -- the value is the floor for the 6.4 exemption.
     "provenance"}
    | set(NODE_ID_FIELDS.values())
)
MANAGED_PROP_NAMES: frozenset[str] = (
    frozenset(
        field
        for model in NODE_TYPE_MODELS.values()
        for field in model.model_fields
    )
    - RUNTIME_EXEMPT_PROPS
)
