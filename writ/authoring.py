"""Authoring helpers for writ add / writ edit.

Functions for relationship suggestion, redundancy detection, and conflict
path checking. Used by CLI commands; no CLI dependency here.

Per ARCH-ORG-001: domain logic separated from CLI dispatch layer.
Per ARCH-DI-001: pipeline and cache injected, not imported globally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

from writ.graph.schema import REDUNDANCY_SIMILARITY_THRESHOLD as REDUNDANCY_THRESHOLD

SUGGESTION_LIMIT = 5


class RuleIdCollisionError(Exception):
    """Raised when a rule_id already exists in the graph.

    Neo4j's MERGE would silently update an existing node, so the authoring
    pipeline needs an explicit pre-check. The existing rule payload is
    attached so callers can surface a useful diff to the user.
    """

    def __init__(self, rule_id: str, existing: dict) -> None:
        super().__init__(f"rule_id already exists in graph: {rule_id}")
        self.rule_id = rule_id
        self.existing = existing


async def check_id_collision(
    rule_id: str,
    db: Neo4jConnection,
) -> None:
    """Fail fast if `rule_id` already exists in Neo4j.

    Runs `MATCH (r:Rule {rule_id: $id}) RETURN r`. Raises
    `RuleIdCollisionError` on a hit. Call before schema validation in the
    `writ add` gate so authors cannot clobber an existing rule via MERGE.
    """
    existing = await db.get_rule(rule_id)
    if existing is not None:
        raise RuleIdCollisionError(rule_id, existing)


def suggest_relationships(
    rule_data: dict,
    pipeline: RetrievalPipeline,
) -> list[dict]:
    """Run the new rule's trigger+statement through the retrieval pipeline.

    Returns top-5 similar rules as relationship candidates.
    Excludes the rule itself if it already exists in the graph.
    """
    query_text = f"{rule_data.get('trigger', '')} {rule_data.get('statement', '')}"
    rule_id = rule_data.get("rule_id", "")
    exclude = [rule_id] if rule_id else []

    result = pipeline.query(query_text, exclude_rule_ids=exclude)
    suggestions = []
    for rule in result["rules"][:SUGGESTION_LIMIT]:
        suggestions.append({
            "rule_id": rule["rule_id"],
            "score": rule["score"],
            "statement": rule.get("statement", ""),
        })
    return suggestions


def check_redundancy(
    rule_data: dict,
    pipeline: RetrievalPipeline,
    threshold: float = REDUNDANCY_THRESHOLD,
) -> list[dict]:
    """Check if a new rule's text is near-duplicate of existing rules.

    Uses the pipeline's embedding model and vector store to compute
    cosine similarity. Returns candidates exceeding the threshold.

    Per INV-5: threshold is cosine similarity (0.95 default), independent
    of the RRF ranking score.
    """
    query_text = f"{rule_data.get('trigger', '')} {rule_data.get('statement', '')}"
    query_vector = pipeline._model.encode(query_text).tolist()

    # Search with higher k to find close matches.
    results = pipeline._vector.search(query_vector, k=10)
    flagged = []
    for r in results:
        if r.score >= threshold:
            meta = pipeline._metadata.get(r.rule_id, {})
            flagged.append({
                "rule_id": r.rule_id,
                "similarity": round(r.score, 4),
                "statement": meta.get("statement", ""),
            })
    return flagged


def check_conflicts(
    rule_id: str,
    cache: AdjacencyCache,
) -> list[dict]:
    """Check if any neighbors have a CONFLICTS_WITH relationship.

    Searches 1-hop neighbors for CONFLICTS_WITH edges.
    Returns list of conflicting rule_ids with edge info.
    """
    neighbors = cache.get_neighbors(rule_id)
    conflicts = []
    for n in neighbors:
        if n["edge_type"] == "CONFLICTS_WITH":
            conflicts.append({
                "rule_id": n["rule_id"],
                "edge_type": n["edge_type"],
                "direction": n["direction"],
            })
    return conflicts
