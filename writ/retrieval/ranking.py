"""Reciprocal Rank Fusion of BM25 + vector scores, weighted by severity, confidence, and graph proximity.

score = (w1 * bm25_norm) + (w2 * vector_norm) + (w3 * severity_weight) + (w4 * confidence_weight) + (w5 * graph_proximity)

Weights are configurable via writ.toml. Constraint: w1 + w2 + w3 + w4 + w5 = 1.0.
Tuned values: 0.198 / 0.594 / 0.099 / 0.099 / 0.01. Phase 5 ratios (2:6:1:1) scaled by 0.99, graph proximity added in Phase 6.

Context budget modes (Phase 5 degraded -- abstractions are Phase 8):
- Summary (< 2K tokens): statement + trigger only
- Standard (2K-8K tokens): top-5 full rules, omit rationale
- Full (> 8K tokens): top-10 full rules with rationale and relationship context
"""

from __future__ import annotations

from dataclasses import dataclass

# Per ARCH-CONST-001: named constants for defaults.
# Phase 5 ratios (2:6:1:1) scaled by 0.99 to make room for w_graph.
# Graph proximity uses discrete values (0.0/0.5/1.0), so even w_graph=0.01
# creates meaningful rank shifts (12/83 queries affected) without MRR@5 regression.
DEFAULT_W_BM25 = 0.198
DEFAULT_W_VECTOR = 0.594
DEFAULT_W_SEVERITY = 0.099
DEFAULT_W_CONFIDENCE = 0.099
DEFAULT_W_GRAPH = 0.01

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
    w_graph: float = DEFAULT_W_GRAPH

    def validate(self) -> None:
        total = self.w_bm25 + self.w_vector + self.w_severity + self.w_confidence + self.w_graph
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Weights must sum to 1.0, got {total}")

    def first_pass_weights(self) -> tuple[float, float, float, float]:
        """Return w1-w4 renormalized to sum 1.0 for first-pass ranking (INV-4).

        Preserves the ratio between w1-w4 regardless of w_graph value.
        """
        total = self.w_bm25 + self.w_vector + self.w_severity + self.w_confidence
        if total < 0.001:
            return (0.25, 0.25, 0.25, 0.25)
        return (
            self.w_bm25 / total,
            self.w_vector / total,
            self.w_severity / total,
            self.w_confidence / total,
        )


def compute_confidence_weight(
    static_confidence: str,
    times_positive: int,
    times_negative: int,
    threshold: int = 50,
    ratio_min: float = 0.75,
) -> float:
    """Return confidence weight, using empirical ratio when graduated."""
    from writ.frequency import evaluate_graduation

    grad = evaluate_graduation(times_positive, times_negative, threshold, ratio_min)
    if grad.graduated:
        return grad.ratio
    return CONFIDENCE_WEIGHTS.get(static_confidence, 0.8)


def compute_score(
    bm25_norm: float,
    vector_norm: float,
    severity: str,
    confidence: str,
    graph_proximity: float = 0.0,
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
        + weights.w_graph * graph_proximity
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


def apply_authority_preference(
    scored_rules: list[dict],
    threshold: float,
) -> list[dict]:
    """Hard preference: human/ai-promoted outranks ai-provisional within threshold.

    For each adjacent pair, if an ai-provisional rule ranks above a
    human/ai-promoted rule and the score gap is within threshold, swap them.
    Threshold of 0.0 disables the preference (no swaps).
    """
    if threshold <= 0.0:
        return scored_rules

    result = list(scored_rules)
    changed = True
    while changed:
        changed = False
        for i in range(len(result) - 1):
            upper = result[i]
            lower = result[i + 1]
            gap = upper.get("score", 0.0) - lower.get("score", 0.0)
            if gap > threshold:
                continue
            upper_auth = upper.get("authority", "human")
            lower_auth = lower.get("authority", "human")
            if upper_auth == "ai-provisional" and lower_auth != "ai-provisional":
                result[i], result[i + 1] = result[i + 1], result[i]
                changed = True
    return result


def filter_proximity_seeds(
    first_pass_scores: list[tuple[str, float, str]],
    top_n: int = 3,
) -> list[str]:
    """Select top-N rule IDs for graph proximity seeding, excluding ai-provisional.

    first_pass_scores: list of (rule_id, score, authority) tuples, sorted by score desc.
    Returns up to top_n rule IDs. No backfill with ai-provisional.
    """
    seeds: list[str] = []
    for rid, _score, authority in first_pass_scores:
        if authority != "ai-provisional":
            seeds.append(rid)
            if len(seeds) >= top_n:
                break
    return seeds


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
