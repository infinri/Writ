"""Writ analysis module -- code compliance checking.

Provides the /analyze endpoint's core logic: pattern extraction,
confidence-scored scanning, LLM escalation, and calibration instrumentation.
"""

from __future__ import annotations

from pydantic import BaseModel


class AnalyzeRequest(BaseModel):
    """Request body for POST /analyze."""

    code: str
    file_path: str
    phase: str  # "planning" | "code_generation" | "testing" | "review"
    context: str


class Finding(BaseModel):
    """Unified finding from pattern matching or LLM analysis."""

    rule_id: str
    source: str  # "pattern" | "llm"
    status: str  # "violated" | "pass" | "uncertain"
    line: int | None = None
    confidence: str | None = None  # "high" | "medium" | "low" | None (null for llm)
    evidence: str = ""
    suggestion: str = ""


class AnalyzeResponse(BaseModel):
    """Response body for POST /analyze."""

    verdict: str  # "pass" | "fail" | "warn"
    findings: list[Finding] = []
    rules_checked: list[str] = []
    analysis_method: str = "pattern"  # "pattern" | "llm" | "hybrid" | "calibration"
    retrieval_scores: dict[str, float] = {}
    summary: str = ""
