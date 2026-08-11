"""Writ HTTP API -- Pydantic request models (PY-PYDANTIC-001).

All 23 request-body models for the FastAPI service live here, moved verbatim
from the pre-split writ/server.py. This module imports NOTHING from writ.server,
so it cannot participate in an import cycle; writ/server/__init__.py re-exports
these names so `from writ.server import QueryRequest` keeps working.
"""

# writ-auth-scan: internal-service

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    """Request body for /query endpoint."""

    query: str
    # Optional, for correlating a retrieval_result row with the session that caused it.
    # Defaults to empty so every existing caller (four hooks post to /query today, none
    # of them sending it) keeps working unchanged; a row without it still carries the
    # quality signal, it just cannot be grouped by session.
    session_id: str = ""
    domain: str | None = None
    scope: str | None = None
    budget_tokens: int | None = None
    exclude_rule_ids: list[str] | None = None
    prefer_rule_ids: list[str] | None = None
    node_types: list[str] | None = None
    retrieval_mode: str = "semantic"
    # The caller's project NAME, already resolved. Empty/None means no project was
    # resolved, which now scopes retrieval to doctrine only (never search-all).
    project: str | None = None
    # The caller's project ROOT, unresolved. This is what a hook actually holds
    # (detect_project_root is pure bash); the daemon resolves root to name because
    # resolution needs the :Project registry it already has open, and doing it
    # client-side would cost a Neo4j round trip plus a python start per prompt per
    # channel. Empty by default so every existing caller keeps working unchanged.
    project_root: str = ""


class CompanionRequest(BaseModel):
    """Request body for /methodology-companion (CHANNEL 2: methodology by state)."""

    mode: str | None = None
    prompt: str = ""
    action: str | None = None
    # Provisional cap; D3 makes this a separately MEASURED methodology budget,
    # sized once floors are authored in 1.7 (the floor set is the dominant cost).
    budget_tokens: int = 5000
    exclude_rule_ids: list[str] | None = None
    # Accepted so every retrieval channel carries the same field and a hook does not
    # have to know which endpoint is project-aware. Every node this route can return
    # is a retrievable METHODOLOGY label, which node_scope.py classifies as doctrine
    # and therefore delivers to every project, so there is nothing here to scope out.
    # The field exists so that stops being an accident: the day this route can return
    # a record-typed node, the caller's root is already on the wire.
    project_root: str = ""


class PromptBundleRequest(BaseModel):
    """Request body for /prompt-bundle (#8: the three per-prompt channels in one call)."""

    session_id: str
    mode: str = ""
    prompt: str = ""          # keyword-extracted prompt: query text + companion prompt + always-on context
    effort: str = ""
    always_on_filter: bool = True
    # The per-prompt hot path. Forwarded to the internal channel-1 QueryRequest,
    # which dropped the project entirely before this cycle: a fix that stopped at
    # /query would have tested green and left the route that runs on every prompt
    # unscoped.
    project_root: str = ""


class ProposeRequest(BaseModel):
    """Request body for /propose endpoint."""

    rule_id: str
    domain: str
    severity: str
    scope: str
    trigger: str
    statement: str
    violation: str
    pass_example: str
    enforcement: str
    rationale: str
    last_validated: str
    task_description: str = ""
    query_that_triggered: str | None = None


class FeedbackRequest(BaseModel):
    """Request body for /feedback endpoint."""

    rule_id: str
    signal: str


class ConflictsRequest(BaseModel):
    """Request body for /conflicts endpoint."""

    rule_ids: list[str]


class CommitCaptureRequest(BaseModel):
    """Request body for /commit/capture (decision-memory Phase 1d)."""

    project_root: str
    commit_hash: str
    subject: str = ""
    author: str = ""
    branch: str = ""
    files: list[dict] = Field(default_factory=list)
    session_id: str = ""


class GitHooksAutoInstallRequest(BaseModel):
    """Request body for /git-hooks/auto-install (decision-memory Phase 1d)."""

    project_root: str


class RecallRequest(BaseModel):
    """Request body for /recall (decision-memory Phase 2)."""

    project_root: str
    branch: str = ""
    budget: int = 20000
    full: bool = False


class MemoryRecordRequest(BaseModel):
    """Request body for /memory-record (auto-memory graph mirror).

    The parsed memory FILE, as built by bin/lib/memory_capture.py: the mirror hook
    and `writ memory backfill` both send this shape. Every field but `path` and
    `name` is optional so a sparse memory (no description, no links) still mirrors;
    `project` is optional because the route derives it from `path` when absent.
    """

    path: str
    name: str
    project: str = ""
    description: str = ""
    type: str = ""
    body: str = ""
    links: list[str] = Field(default_factory=list)
    session_id: str = ""
    updated_at: str = ""
    status: str = "live"
    # Accepted for symmetry with the other capture routes (the caller's repo root);
    # the memory scope is the encoded project dir in `path`, not the git project.
    project_root: str = ""


# ---------------------------------------------------------------------------
# Session route Pydantic models (PY-PYDANTIC-001)
# ---------------------------------------------------------------------------


class SessionUpdateRequest(BaseModel):
    """Request body for POST /session/{session_id}/update."""

    model_config = {"strict": True}

    key: str
    value: str


class SessionModeSetRequest(BaseModel):
    """Request body for POST /session/{session_id}/mode."""

    model_config = {"strict": True}

    mode: str
    orchestrator: bool = False


class SessionCanWriteRequest(BaseModel):
    """Request body for POST /session/{session_id}/can-write."""

    tool_input: dict[str, Any] = Field(default_factory=dict)
    skill_dir: str = ""


class SessionFormatRequest(BaseModel):
    """Request body for POST /session/format."""

    query_response: dict[str, Any]


class SessionAutoFeedbackRequest(BaseModel):
    """Request body for POST /session/{session_id}/auto-feedback."""

    feedback: str = ""


class SessionAddViolationRequest(BaseModel):
    """Request body for POST /session/{session_id}/add-pending-violation."""

    rule_id: str
    detail: str = ""
    file: str = ""
    line: int | None = None


class SessionInvalidateGateRequest(BaseModel):
    """Request body for POST /session/{session_id}/invalidate-gate.

    Mirrors the `writ-session.py invalidate-gate` CLI: gate + rule + file are
    required to record an invalidation cycle; the rest are optional context.
    """

    gate: str = ""
    rule_id: str = ""
    file: str = ""
    evidence: str = ""
    trace: str = ""
    plan_hash: str = ""
    project_root: str = ""


class ContextPercentRequest(BaseModel):
    """Request body for POST /session/{session_id}/context-percent."""

    model_config = {"strict": True}

    context_percent: int


class PreWriteCheckRequest(BaseModel):
    """Request body for POST /pre-write-check."""

    session_id: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    skill_dir: str = ""
    file_path: str = ""
    prefer_rule_ids: list[str] | None = None


class SessionAdvancePhaseRequest(BaseModel):
    """Request body for POST /session/{session_id}/advance-phase.

    Defaults mirror the prior `(body or {}).get(key, default)` reads so an
    omitted key keeps today's 200 business response (API-BREAKING-001); the
    type layer only turns a present-but-wrong-typed value into a clean 422.
    """

    confirmation_source: str = "explicit"
    token: str = ""
    project_root: str = ""
    # The caller's working directory, used to resolve the project root when
    # project_root is empty (a cwd with no repo marker used to resolve to nothing,
    # which refused every advance). The daemon cannot substitute its own cwd: that is
    # Writ's install dir. Optional, so existing callers keep today's behavior.
    cwd: str = ""


class SessionPromoteCandidateRequest(BaseModel):
    """Request body for POST /session/{session_id}/promote-candidate."""

    candidate_id: str | None = None
    token: str = ""
    edited_fields: dict | None = None


class SessionActivePlaybookRequest(BaseModel):
    """Request body for POST /session/{session_id}/active-playbook."""

    playbook_id: str | None = None
    phase_id: str | None = None
    total_steps: int | None = None

    @field_validator("total_steps", mode="before")
    @classmethod
    def _reject_bool_total_steps(cls, v: Any) -> Any:
        # Pydantic v2 lax mode coerces JSON true->1 (bool subclasses int).
        # Reject bool explicitly; pass None and real ints/strings through so
        # lax int coercion of "4"->4 still works.
        if isinstance(v, bool):
            raise ValueError("total_steps must be an integer, not a boolean")
        return v


class SessionVerificationEvidenceRequest(BaseModel):
    """Request body for POST /session/{session_id}/verification-evidence."""

    todo_id: str | None = None
    command: str = ""
    output_excerpt: str = ""
    exit_code: int = 0

    @field_validator("exit_code", mode="before")
    @classmethod
    def _reject_bool_exit_code(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("exit_code must be an integer, not a boolean")
        return v


class SessionReviewFindingsRequest(BaseModel):
    """Request body for POST /session/{session_id}/review-findings.

    `message` is the reviewer's final text verbatim, not a pre-parsed verdict:
    parsing lives in bin/lib/review_findings.py so the HTTP path and the
    SubagentStop hook cannot disagree about what a verdict is.
    """

    message: str = ""
    agent_id: str = ""


class SessionQualityJudgmentRequest(BaseModel):
    """Request body for POST /session/{session_id}/quality-judgment."""

    artifact_path: str | None = None
    score: int = 0
    failing_section: str | None = None
    rationale: str = ""
    overridden: bool = False
    rubric: str | None = None
    latency_ms: int | float | None = None

    @field_validator("score", mode="before")
    @classmethod
    def _reject_bool_score(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("score must be an integer, not a boolean")
        return v


__all__ = [
    "QueryRequest",
    "CompanionRequest",
    "PromptBundleRequest",
    "ProposeRequest",
    "FeedbackRequest",
    "ConflictsRequest",
    "CommitCaptureRequest",
    "GitHooksAutoInstallRequest",
    "RecallRequest",
    "SessionUpdateRequest",
    "SessionModeSetRequest",
    "SessionCanWriteRequest",
    "SessionFormatRequest",
    "SessionAutoFeedbackRequest",
    "SessionAddViolationRequest",
    "SessionInvalidateGateRequest",
    "ContextPercentRequest",
    "PreWriteCheckRequest",
    "SessionAdvancePhaseRequest",
    "SessionPromoteCandidateRequest",
    "SessionActivePlaybookRequest",
    "SessionVerificationEvidenceRequest",
    "SessionQualityJudgmentRequest",
]
