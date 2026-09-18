"""Reciprocal-rank normalization of BM25 + vector scores, then weighted linear fusion by severity, confidence, and graph proximity.

Note: this is reciprocal-rank (1/(rank+1)) + weighted sum, NOT classical Reciprocal
Rank Fusion (which uses 1/(k+rank) with a constant k~60). See normalize_ranks.

score = (w1 * bm25_norm) + (w2 * vector_norm) + (w3 * severity_weight) + (w4 * confidence_weight) + (w5 * graph_proximity)

Weights are configurable via writ.toml. Constraint: w1 + w2 + w3 + w4 + w5 = 1.0.
Tuned values: 0.19 / 0.57 / 0.095 / 0.095 / 0.05. Phase 5 ratios (2:6:1:1) rebalanced to leave room for w_graph; graph proximity added in Phase 6, and its weight raised from 0.01 to the measured optimum on 2026-09-18 (benchmarks/NEO4J-ABLATION-2026-09-18.md).

Context budget modes (Phase 5 degraded -- abstractions are Phase 8):
- Summary (< 2K tokens): statement + trigger only
- Standard (2K-8K tokens): top-5 full rules, omit rationale
- Full (> 8K tokens): top-10 full rules with rationale and relationship context
"""

from __future__ import annotations

from dataclasses import dataclass

# Per ARCH-CONST-001: named constants for defaults.
# Phase 5 ratios (2:6:1:1), rebalanced to leave room for w_graph: the four are
# scaled by (1 - w_graph) / 0.99 so all five sum to 1.0, which is the same
# rebalance scripts/sweep_ranking.py applies at every sweep point. Change w_graph
# and these four move with it, or the shipped vector is a point nobody measured;
# tests/test_graph_contribution_invariants.py derives that and fails by name.
#
# w_graph = 0.05 is the do-no-harm winner of the 2026-09-18 ablation over the
# 193-query gold set: the only arm that improves MRR@5 (0.6124 -> 0.6273),
# hit-rate@5 (0.8083 -> 0.8135) and nDCG@10 (0.7327 -> 0.7367) together while
# losing zero queries (4 wins, 0 losses, paired sign test p = 0.1250). The prior
# 0.01 was never a measured optimum; its comment justified it with 12/83 queries,
# a gold set two sizes ago. Ablating the term outright costs one query of 193 and
# nothing on MRR@5, so this weight buys little either way: what the measurement
# retired is the claim that the graph had been SHOWN not to contribute.
# Full method, arms table and caveats: benchmarks/NEO4J-ABLATION-2026-09-18.md.
DEFAULT_W_BM25 = 0.19
DEFAULT_W_VECTOR = 0.57
DEFAULT_W_SEVERITY = 0.095
DEFAULT_W_CONFIDENCE = 0.095
DEFAULT_W_GRAPH = 0.05

# Phase 1 addition: literal retrieval mode for exact-phrase / rationalization
# queries where BM25 carries the distinguishing signal. Used when caller passes
# retrieval_mode="literal". Coding-rule default (retrieval_mode="semantic") is
# unchanged above. Verified on ground_truth_proc.json: methodology MRR 0.80,
# hit rate 1.00, bundle completeness 0.88 (2-hop).
LITERAL_W_BM25 = 0.396
LITERAL_W_VECTOR = 0.396
LITERAL_W_SEVERITY = 0.099
LITERAL_W_CONFIDENCE = 0.099
LITERAL_W_GRAPH = 0.01

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

    @classmethod
    def literal(cls) -> "RankingWeights":
        """Weights tuned for exact-phrase / rationalization queries.

        Used when a call passes retrieval_mode='literal'. Equal-weight BM25 and
        vector so forbidden-phrase matches, rationalization keywords, and named
        anti-pattern terms surface alongside semantic matches.
        """
        return cls(
            w_bm25=LITERAL_W_BM25,
            w_vector=LITERAL_W_VECTOR,
            w_severity=LITERAL_W_SEVERITY,
            w_confidence=LITERAL_W_CONFIDENCE,
            w_graph=LITERAL_W_GRAPH,
        )

    def validate(self) -> None:
        total = (
            self.w_bm25 + self.w_vector + self.w_severity
            + self.w_confidence + self.w_graph
        )
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
    """Return confidence weight, using empirical ratio when graduated.

    If the rule has sufficient frequency data (n >= threshold) and a passing
    ratio (>= ratio_min), the empirical ratio replaces the static confidence.
    Otherwise, the static enum value is used.
    """
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
    times_seen_positive: int = 0,
    times_seen_negative: int = 0,
) -> float:
    """Compute final ranking score for a single rule candidate.

    Confidence weight uses compute_confidence_weight: when a rule has
    sufficient frequency data (positive + negative >= 50) and a passing
    positive ratio (>= 0.75), the empirical ratio replaces the static
    confidence-tier weight. With default counters (0, 0) the behavior
    reduces to the static enum lookup, preserving prior semantics.
    """
    if weights is None:
        weights = RankingWeights()

    sev_w = SEVERITY_WEIGHTS.get(severity, 0.5)
    conf_w = compute_confidence_weight(confidence, times_seen_positive, times_seen_negative)

    return (
        weights.w_bm25 * bm25_norm
        + weights.w_vector * vector_norm
        + weights.w_severity * sev_w
        + weights.w_confidence * conf_w
        + weights.w_graph * graph_proximity
    )


def normalize_ranks(scores: list[float]) -> list[float]:
    """Normalize a list of scores via reciprocal rank: 1 / (rank + 1) where rank is descending by score.

    Output is in (0, 1] with the top-scoring item at 1.0 and lower-ranked
    items decaying as 1/2, 1/3, .... This is plain reciprocal rank, not
    classical Reciprocal Rank Fusion (which would use 1/(k+rank) with a
    constant k, typically 60). Weighted linear fusion of the resulting
    normalized ranks happens in compute_score().
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
    # Bubble pass: swap adjacent pairs where preference applies.
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


# The fields the injected header renders (writ/session/budget_tracking.py:cmd_format).
# Every projection in this module carries them, so the header shows real values.
_HEADER_FIELDS = ("severity", "authority")


def _carry_header_fields(entry: dict, rule: dict) -> None:
    """Copy the header fields onto a projected entry, only when the source declares
    them. Never defaulted here: the formatter owns how absence looks, and a second
    default in this module is what let these fields go missing."""
    for f in _HEADER_FIELDS:
        if f in rule:
            entry[f] = rule[f]


def _project_rules(
    rules: list[dict], limit: int, str_fields: list[str], include_relationships: bool = False
) -> list[dict]:
    """Project the top-`limit` rules to a budget mode's field set. Base fields
    (rule_id/node_type/score) come first, then the header fields
    (severity/authority, copied only when the source declares them), then
    `str_fields` (default ''), then relationships (default []) only in full
    mode. Single source for the per-mode projection shared by
    summary/standard/full. Key order is a stability convention, not a wire
    contract: every consumer reads by key."""
    out: list[dict] = []
    for rule in rules[:limit]:
        d = {
            "rule_id": rule["rule_id"],
            "node_type": rule.get("node_type", "Rule"),
            "score": rule.get("score", 0.0),
        }
        _carry_header_fields(d, rule)
        for f in str_fields:
            d[f] = rule.get(f, "")
        if include_relationships:
            d["relationships"] = rule.get("relationships", [])
        out.append(d)
    return out


def apply_context_budget(
    rules: list[dict],
    budget_tokens: int | None,
    abstractions: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """Apply context budget constraints to the result set.

    When abstractions are available and budget < 2K, returns abstraction summaries
    instead of raw statement+trigger (Phase 8 upgrade).

    Returns (trimmed_rules, mode_name).
    """
    if budget_tokens is None:
        budget_tokens = STANDARD_THRESHOLD + 1

    if budget_tokens < SUMMARY_THRESHOLD:
        if abstractions:
            return _summary_with_abstractions(rules, abstractions), "summary"
        # Fallback: statement + trigger only (pre-Phase 8 behavior).
        mode = "summary"
        trimmed = _project_rules(rules, SUMMARY_LIMIT, ["statement", "trigger"])
        return trimmed, mode

    elif budget_tokens <= STANDARD_THRESHOLD:
        mode = "standard"
        trimmed = _project_rules(
            rules, STANDARD_LIMIT, ["statement", "trigger", "violation", "pass_example"]
        )
        return trimmed, mode

    else:
        mode = "full"
        trimmed = _project_rules(
            rules,
            FULL_LIMIT,
            ["statement", "trigger", "violation", "pass_example", "rationale"],
            include_relationships=True,
        )
        return trimmed, mode


def _summary_with_abstractions(
    rules: list[dict],
    abstractions: list[dict],
) -> list[dict]:
    """Return abstraction summaries that cover the top-ranked rules.

    For each top rule, find the abstraction that contains it. Deduplicate
    so each abstraction appears at most once. Ungrouped rules fall back
    to statement+trigger.
    """
    # Build rule_id -> abstraction lookup.
    rid_to_abs: dict[str, dict] = {}
    for abst in abstractions:
        for rid in abst.get("rule_ids", abst.get("member_ids", [])):
            rid_to_abs[rid] = abst

    seen_abs: set[str] = set()
    result: list[dict] = []

    for rule in rules[:SUMMARY_LIMIT]:
        rid = rule["rule_id"]
        abst = rid_to_abs.get(rid)
        if abst and abst["abstraction_id"] not in seen_abs:
            seen_abs.add(abst["abstraction_id"])
            result.append({
                "abstraction_id": abst["abstraction_id"],
                "summary": abst.get("summary", ""),
                "rule_ids": abst.get("rule_ids", abst.get("member_ids", [])),
                "compression_ratio": abst.get("compression_ratio", 1.0),
                "domain": abst.get("domain", ""),
            })
        elif not abst:
            # Ungrouped rule: fall back to statement+trigger.
            entry = {
                "rule_id": rid,
                "score": rule.get("score", 0.0),
            }
            _carry_header_fields(entry, rule)
            entry["statement"] = rule.get("statement", "")
            entry["trigger"] = rule.get("trigger", "")
            result.append(entry)

    return result
