"""Structural pre-filter for candidate rules.

Consolidates five checks into a single gate function that produces a binary
accept/reject. This is a structural filter, not qualitative judgment.
The actual judgment -- "is this rule correct?" -- remains human responsibility.

Per ARCH-ORG-001: gate logic here, authoring helpers in authoring.py.
Per ARCH-DI-001: pipeline injected, not imported globally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from writ.graph.schema import Rule

if TYPE_CHECKING:
    from writ.retrieval.pipeline import RetrievalPipeline

# Per ARCH-CONST-001: named constants for gate thresholds.
# Defaults from EVOLUTION_PLAN.md; overridable via writ.toml.
NOVELTY_THRESHOLD = 0.85
REDUNDANCY_THRESHOLD = 0.95

# Handbook Section 2.1: words that disqualify a rule from being enforceable.
VAGUE_DISQUALIFIERS = (
    r"\bconsider\b",
    r"\bbe aware\b",
    r"\bwhere appropriate\b",
    r"\bwhen possible\b",
    r"\bif necessary\b",
    r"\bas needed\b",
    r"\btry to\b",
    r"\bshould generally\b",
    r"\bmay want to\b",
    r"\bkeep in mind\b",
)

# Pre-compiled for performance in hot path.
_VAGUE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in VAGUE_DISQUALIFIERS]


@dataclass
class GateResult:
    """Result of structural_gate(). Immutable after creation."""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    similar_rules: list[str] = field(default_factory=list)


def structural_gate(
    candidate: dict,
    pipeline: RetrievalPipeline,
    *,
    novelty_threshold: float = NOVELTY_THRESHOLD,
    redundancy_threshold: float = REDUNDANCY_THRESHOLD,
) -> GateResult:
    """Screen a candidate rule against structural quality checks.

    Returns accept/reject with reasons. Does NOT evaluate correctness.

    Checks (in order):
    1. Schema validation (Pydantic)
    2. Specificity (vague language disqualifiers)
    3. Redundancy (cosine > redundancy_threshold with existing rule)
    4. Novelty (cosine > novelty_threshold with existing rule)
    5. Conflict (CONFLICTS_WITH edge with existing rule)
    """
    reasons: list[str] = []
    similar_rules: list[str] = []

    # 1. Schema validation.
    schema_reasons = _check_schema(candidate)
    reasons.extend(schema_reasons)

    # 1b. Mechanical-enforcement-path policy (Phase 2, plan Section 2.1).
    # Mandatory rules must cite a mechanical enforcement path. Rules without
    # one must be advisory so the advisory-vs-mandatory distinction remains
    # honest. Applies universally (existing mandatory rules audited via
    # Section 17 retroactive-audit protocol).
    mechanical_reasons = _check_mechanical_enforcement(candidate)
    reasons.extend(mechanical_reasons)

    # 2. Specificity -- vague language in trigger or statement.
    specificity_reasons = _check_specificity(candidate)
    reasons.extend(specificity_reasons)

    # 3 & 4. Redundancy and novelty via vector similarity.
    sim_reasons, sim_rules = _check_similarity(
        candidate, pipeline, novelty_threshold, redundancy_threshold
    )
    reasons.extend(sim_reasons)
    similar_rules.extend(sim_rules)

    # 5. Conflict check via graph traversal.
    conflict_reasons = _check_conflicts(candidate, pipeline)
    reasons.extend(conflict_reasons)

    return GateResult(
        accepted=len(reasons) == 0,
        reasons=reasons,
        similar_rules=similar_rules,
    )


def _check_schema(candidate: dict) -> list[str]:
    """Validate candidate against Pydantic Rule model."""
    clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
    try:
        Rule(**clean)
    except Exception as e:
        return [f"Schema validation failed: {e}"]
    return []


def _check_mechanical_enforcement(candidate: dict) -> list[str]:
    """Reject mandatory rules without a non-empty mechanical_enforcement_path.

    Plan Section 2.1 policy: rules with `mandatory: true` must cite a
    mechanical enforcement path (hook file + matcher + deny condition).
    Rules that can't be mechanically enforced must be declared advisory
    (`mandatory: false`, severity may remain high/critical). This keeps the
    advisory-vs-mandatory distinction honest. Retroactive audit of the
    existing 35 mandatory rules lives in docs/mandatory-rule-audit.md.
    """
    mandatory = candidate.get("mandatory", False)
    if not mandatory:
        return []
    path = candidate.get("mechanical_enforcement_path")
    if path is None or not str(path).strip():
        rid = candidate.get("rule_id", "<unknown>")
        return [
            f"Mechanical-enforcement policy (plan Section 2.1): rule '{rid}' "
            f"is mandatory but has no mechanical_enforcement_path. Either "
            f"name a hook + matcher + deny condition, or demote to "
            f"mandatory=false (advisory)."
        ]
    return []


def _check_specificity(candidate: dict) -> list[str]:
    """Check trigger and statement for vague language disqualifiers."""
    trigger = candidate.get("trigger", "")
    statement = candidate.get("statement", "")
    text = f"{trigger} {statement}"

    found: list[str] = []
    for pattern in _VAGUE_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append(match.group(0))

    if found:
        return [f"Specificity: vague language detected: {', '.join(found)}"]
    return []


def _check_similarity(
    candidate: dict,
    pipeline: RetrievalPipeline,
    novelty_threshold: float,
    redundancy_threshold: float,
) -> tuple[list[str], list[str]]:
    """Check redundancy (> redundancy_threshold) and novelty (> novelty_threshold)."""
    reasons: list[str] = []
    similar_rules: list[str] = []

    query_text = f"{candidate.get('trigger', '')} {candidate.get('statement', '')}"
    query_vector = pipeline._model.encode(query_text).tolist()
    results = pipeline._vector.search(query_vector, k=10)

    # Exclude self if already in graph.
    candidate_id = candidate.get("rule_id", "")
    results = [r for r in results if r.rule_id != candidate_id]

    for r in results:
        if r.score >= redundancy_threshold:
            reasons.append(
                f"Redundancy: cosine {r.score:.4f} with {r.rule_id} "
                f"(threshold: {redundancy_threshold})"
            )
            similar_rules.append(r.rule_id)
        elif r.score >= novelty_threshold:
            reasons.append(
                f"Novelty: cosine {r.score:.4f} with {r.rule_id} "
                f"(threshold: {novelty_threshold})"
            )
            similar_rules.append(r.rule_id)

    return reasons, similar_rules


def _check_conflicts(candidate: dict, pipeline: RetrievalPipeline) -> list[str]:
    """Check if candidate conflicts with existing rules via graph edges.

    Only flags conflicts where the existing rule has stronger evidence.
    New candidates without graph presence cannot have CONFLICTS_WITH edges,
    so this check is relevant only for candidates already partially ingested
    or for re-validation of existing rules.
    """
    candidate_id = candidate.get("rule_id", "")
    if not candidate_id:
        return []

    # Check if the rule exists in graph metadata (already ingested).
    if candidate_id not in pipeline._metadata:
        return []

    # Use pipeline's adjacency cache if available.
    neighbors = pipeline._cache.get_neighbors(candidate_id)
    conflicts: list[str] = []
    for n in neighbors:
        if n["edge_type"] == "CONFLICTS_WITH":
            conflicts.append(
                f"Conflict: CONFLICTS_WITH edge to {n['rule_id']}"
            )

    return conflicts


async def propose_rule(
    candidate: dict,
    pipeline: RetrievalPipeline,
    db: object,
    *,
    origin_db_path: object | None = None,
    task_description: str = "",
    query_that_triggered: str | None = None,
    novelty_threshold: float = NOVELTY_THRESHOLD,
    redundancy_threshold: float = REDUNDANCY_THRESHOLD,
) -> dict:
    """End-to-end AI rule proposal: gate -> ingest -> origin context.

    Returns dict with accepted, rule_id, authority, confidence, reasons.
    """
    # Enforce authority and confidence ceiling.
    candidate["authority"] = "ai-provisional"
    candidate["confidence"] = "speculative"

    # Run structural gate.
    gate_result = structural_gate(
        candidate, pipeline,
        novelty_threshold=novelty_threshold,
        redundancy_threshold=redundancy_threshold,
    )

    if not gate_result.accepted:
        return {
            "accepted": False,
            "rule_id": candidate.get("rule_id", ""),
            "reasons": gate_result.reasons,
            "similar_rules": gate_result.similar_rules,
        }

    # Ingest into graph.
    clean = {k: v for k, v in candidate.items() if not k.startswith("_")}
    await db.create_rule(clean)

    # Write origin context.
    if origin_db_path is not None:
        from writ.origin_context import OriginContextStore

        store = OriginContextStore(origin_db_path)
        consulted = candidate.get("_consulted_rules", [])
        store.write(
            candidate["rule_id"],
            task_description,
            query_that_triggered,
            consulted,
        )
        store.close()

    return {
        "accepted": True,
        "rule_id": candidate["rule_id"],
        "authority": "ai-provisional",
        "confidence": "speculative",
        "reasons": [],
    }
