"""Authoring helpers for writ add / writ edit / writ review / writ propose.

Functions for rule-dict assembly, relationship suggestion, redundancy
detection, conflict path checking, the post-mutation conflict/export tail, and
the review authority state machine (promote / reject / downweight). Used by CLI
commands; no CLI dependency here (no typer, no echoes).

Public surface: `build_rule_dict`, `check_id_collision`, `suggest_relationships`,
`check_redundancy`, `check_conflicts`, `finalize_conflict_and_export`,
`assert_ai_provisional`, `promote`, `reject`, `downweight`, `record_trust`,
`record_approval`, `dispute`, `verify`, `RuleIdCollisionError`,
`IllegalAuthorityTransitionError`.

Per ARCH-ORG-001: domain logic separated from CLI dispatch layer.
Per ARCH-DI-001: pipeline and cache injected, not imported globally.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from writ.graph.db import Neo4jConnection
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache
    from writ.session.gate_token import GateIdentity

from writ.graph.db._common import _now_iso
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
    of the reciprocal-rank fusion ranking score.
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


def build_rule_dict(
    *,
    rule_id: str,
    domain,
    severity,
    scope,
    trigger,
    statement,
    violation,
    pass_example,
    enforcement,
    rationale,
) -> dict:
    """The canonical rule field dict assembled from authored inputs, stamped with
    today's last_validated. Single source of the field set shared by add + propose."""
    return {
        "rule_id": rule_id,
        "domain": domain,
        "severity": severity,
        "scope": scope,
        "trigger": trigger,
        "statement": statement,
        "violation": violation,
        "pass_example": pass_example,
        "enforcement": enforcement,
        "rationale": rationale,
        "last_validated": date.today().isoformat(),
    }


async def finalize_conflict_and_export(
    db: Neo4jConnection,
    cache: AdjacencyCache,
    rule_id: str,
    bible_dir: str = "bible/",
) -> dict:
    """After a rule mutation (add/edit): rebuild the adjacency cache, detect any
    CONFLICTS_WITH neighbors, then auto-export the corpus to the bible (Phase 7).

    De-echoed single source for the post-mutation tail shared by the add and
    edit commands. Rebuilds the caller-passed `cache` in place, then returns
    structured data so the CLI dispatch layer drives all echoes:
    `{"conflicts": [...], "rules_exported": int, "export_dir": bible_dir}`.
    """
    from pathlib import Path

    from writ.export import export_rules_to_markdown

    await cache.build_from_db(db)
    conflicts = check_conflicts(rule_id, cache)
    export_result = await export_rules_to_markdown(db, Path(bible_dir))
    return {
        "conflicts": conflicts,
        "rules_exported": export_result["rules_exported"],
        "export_dir": bible_dir,
    }


class IllegalAuthorityTransitionError(Exception):
    """Raised when a review action is attempted against a rule whose authority
    forbids it (only `ai-provisional` rules may be promoted or rejected).

    Mirrors `RuleIdCollisionError`: carries the context the CLI dispatch layer
    needs to surface an exact message and exit code (CLEAN-ERR-003).
    """

    def __init__(self, rule_id: str, current_authority, action: str) -> None:
        super().__init__(
            f"{rule_id} has authority '{current_authority}', cannot {action}"
        )
        self.rule_id = rule_id
        self.current_authority = current_authority
        self.action = action


def assert_ai_provisional(existing: dict, rule_id: str, action: str) -> None:
    """Single source of the promote/reject legality check (DRY-DUP-002)."""
    if existing.get("authority") != "ai-provisional":
        raise IllegalAuthorityTransitionError(
            rule_id, existing.get("authority"), action
        )


async def promote(db: Neo4jConnection, rule_id: str, existing: dict) -> None:
    """Promote an ai-provisional rule to ai-promoted / peer-reviewed.

    Guards on `existing['authority'] == 'ai-provisional'`; an illegal transition
    raises `IllegalAuthorityTransitionError` and touches neither db method. A
    legal transition updates BOTH authority and confidence as a unit.
    """
    assert_ai_provisional(existing, rule_id, "promote")
    await db.update_rule_authority(rule_id, "ai-promoted")
    await db.update_rule_confidence(rule_id, "peer-reviewed")


async def reject(db: Neo4jConnection, rule_id: str, existing: dict) -> None:
    """Reject (delete) an ai-provisional rule.

    Guards identically to `promote`; an illegal transition raises
    `IllegalAuthorityTransitionError` and never calls `delete_rule`.
    """
    assert_ai_provisional(existing, rule_id, "reject")
    await db.delete_rule(rule_id)


async def downweight(db: Neo4jConnection, rule_id: str) -> None:
    """Set a rule's confidence floor to speculative.

    Asymmetric by design: NO authority guard (unlike promote/reject), so it
    succeeds for any authority. Preserve exactly -- do not add a guard.
    """
    await db.update_rule_confidence(rule_id, "speculative")


async def record_trust(
    db: Neo4jConnection, *, kind: str, rule_id: str, rule_props: dict, via: str,
    session_id: str, identity: GateIdentity, note: str = "",
) -> str | None:
    """The one trust write: the Rule's summary props, then one TrustEvent carrying who
    approved. Rule props first, so a crash between the two never leaves history for a
    change that did not happen. Returns the event id, or None (no event) when the rule
    is gone."""
    project = await db.set_rule_trust_props(rule_id, rule_props)
    if project is None:
        return None
    return await db.create_trust_event(
        event_id=f"te-{uuid.uuid4().hex}", project=project, rule_id=rule_id, kind=kind,
        via=via, session_id=session_id, os_login=identity.os_login,
        git_name=identity.git_name, note=note,
    )


async def record_approval(
    db: Neo4jConnection, rule_id: str, *, via: str, session_id: str, identity: GateIdentity,
) -> str | None:
    """Record a promotion that already committed. An approval is also a verification."""
    return await record_trust(
        db, kind="approval", rule_id=rule_id,
        rule_props={
            "approved_at": _now_iso(), "approval_via": via,
            "last_verified": date.today().isoformat(),
        },
        via=via, session_id=session_id, identity=identity,
    )


async def dispute(
    db: Neo4jConnection, rule_id: str, *, session_id: str, identity: GateIdentity,
    note: str = "",
) -> str | None:
    """Mark a rule disputed. Never adjudicated, never cleared here, any authority."""
    return await record_trust(
        db, kind="dispute", rule_id=rule_id, rule_props={"disputed": True},
        via="review_dispute", session_id=session_id, identity=identity, note=note,
    )


async def verify(
    db: Neo4jConnection, rule_id: str, *, session_id: str, identity: GateIdentity,
    note: str = "",
) -> str | None:
    """Restart a rule's verify clock, any authority."""
    return await record_trust(
        db, kind="verify", rule_id=rule_id,
        rule_props={"last_verified": date.today().isoformat()},
        via="review_verify", session_id=session_id, identity=identity, note=note,
    )
