"""Reciprocal Rank Fusion of BM25 + vector scores, weighted by severity and confidence.

score = (w1 * bm25_norm) + (w2 * vector_norm) + (w3 * severity_weight) + (w4 * confidence_weight)

Weights are configurable via writ.toml. Constraint: w1 + w2 + w3 + w4 = 1.0.
Tuned values: 0.3 / 0.5 / 0.1 / 0.1. Shifted toward vector to reduce BM25 keyword noise.

Context budget modes (Phase 5 degraded -- abstractions are Phase 8):
- Summary (< 2K tokens): statement + trigger only
- Standard (2K-8K tokens): top-5 full rules, omit rationale
- Full (> 8K tokens): top-10 full rules with rationale and relationship context
"""

from __future__ import annotations

from dataclasses import dataclass

# Per ARCH-CONST-001: named constants for defaults.
DEFAULT_W_BM25 = 0.2
DEFAULT_W_VECTOR = 0.6
DEFAULT_W_SEVERITY = 0.1
DEFAULT_W_CONFIDENCE = 0.1

SUMMARY_THRESHOLD = 2000
STANDARD_THRESHOLD = 8000
SUMMARY_LIMIT = 10
STANDARD_LIMIT = 5
FULL_LIMIT = 10

SEVERITY_WEIGHTS = {
    "critical": 1.0,
    "high": 0.75,
    "medium": 0.5,
    "low": 0.25,
}

CONFIDENCE_WEIGHTS = {
    "battle-tested": 1.0,
    "production-validated": 0.8,
    "peer-reviewed": 0.6,
    "speculative": 0.3,
}


@dataclass
class RankingWeights:
    w_bm25: float = DEFAULT_W_BM25
    w_vector: float = DEFAULT_W_VECTOR
    w_severity: float = DEFAULT_W_SEVERITY
    w_confidence: float = DEFAULT_W_CONFIDENCE

    def validate(self) -> None:
        total = self.w_bm25 + self.w_vector + self.w_severity + self.w_confidence
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Weights must sum to 1.0, got {total}")


def compute_score(
    bm25_norm: float,
    vector_norm: float,
    severity: str,
    confidence: str,
    weights: RankingWeights | None = None,
) -> float:
    """Compute final ranking score for a single rule candidate."""
    if weights is None:
        weights = RankingWeights()

    sev_w = SEVERITY_WEIGHTS.get(severity, 0.5)
    conf_w = CONFIDENCE_WEIGHTS.get(confidence, 0.8)

    return (
        weights.w_bm25 * bm25_norm
        + weights.w_vector * vector_norm
        + weights.w_severity * sev_w
        + weights.w_confidence * conf_w
    )


def normalize_ranks(scores: list[float]) -> list[float]:
    """Normalize a list of scores to [0, 1] range via reciprocal rank fusion.

    Higher original score -> higher normalized score.
    """
    if not scores:
        return []
    # Sort indices by score descending, assign reciprocal rank.
    indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    normalized = [0.0] * len(scores)
    for rank, (orig_idx, _score) in enumerate(indexed):
        normalized[orig_idx] = 1.0 / (rank + 1)
    return normalized


def apply_context_budget(
    rules: list[dict],
    budget_tokens: int | None,
) -> tuple[list[dict], str]:
    """Apply context budget constraints to the result set.

    Returns (trimmed_rules, mode_name).
    """
    if budget_tokens is None:
        budget_tokens = STANDARD_THRESHOLD + 1

    if budget_tokens < SUMMARY_THRESHOLD:
        mode = "summary"
        limit = SUMMARY_LIMIT
        # Summary: statement + trigger only.
        trimmed = []
        for rule in rules[:limit]:
            trimmed.append({
                "rule_id": rule["rule_id"],
                "score": rule.get("score", 0.0),
                "statement": rule.get("statement", ""),
                "trigger": rule.get("trigger", ""),
            })
        return trimmed, mode

    elif budget_tokens <= STANDARD_THRESHOLD:
        mode = "standard"
        limit = STANDARD_LIMIT
        trimmed = []
        for rule in rules[:limit]:
            trimmed.append({
                "rule_id": rule["rule_id"],
                "score": rule.get("score", 0.0),
                "statement": rule.get("statement", ""),
                "trigger": rule.get("trigger", ""),
                "violation": rule.get("violation", ""),
                "pass_example": rule.get("pass_example", ""),
            })
        return trimmed, mode

    else:
        mode = "full"
        limit = FULL_LIMIT
        trimmed = []
        for rule in rules[:limit]:
            trimmed.append({
                "rule_id": rule["rule_id"],
                "score": rule.get("score", 0.0),
                "statement": rule.get("statement", ""),
                "trigger": rule.get("trigger", ""),
                "violation": rule.get("violation", ""),
                "pass_example": rule.get("pass_example", ""),
                "rationale": rule.get("rationale", ""),
                "relationships": rule.get("relationships", []),
            })
        return trimmed, mode
