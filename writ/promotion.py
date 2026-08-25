"""Phase 6.3b/6.3c: the human-gated graduation loop.

A graduation_pending candidate becomes canon ONLY through an informed, edit-capable,
token-gated human promotion (the North Star: oversight relocated to the graduation
moment, not removed).

- build_promotion_review_artifact (6.3b), served by POST /session/{id}/promotion-review,
  surfaces the candidate's CONTENT + canon-fit so
  the gate is INFORMED, not a rubber-stamp.
- promote_candidate (6.3c) performs the gated, edit-capable canon write: re-lint the
  (edited) text, stamp provenance=graduated + graduated_via, export to bible/ source.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from writ.export import node_to_yaml_frontmatter
from writ.gate import structural_gate
from writ.graph.db import _GRAPH_ID_COALESCE
from writ.retrieval.pipeline import RetrievalPipeline

HIGH_SIMILARITY_DEFAULT = 0.7
NEAREST_K_DEFAULT = 5


def _normalize_statement(s: str) -> str:
    """Lowercase + collapse internal whitespace, so a whitespace/case-only edit
    is NOT treated as a wording change."""
    return " ".join((s or "").lower().split())


async def _statement_for(rule_id: str, pipeline: RetrievalPipeline, db: object) -> str:
    """Prefer the hot-path metadata statement; fall back to a graph read so a neighbor
    always carries its text (the artifact must show content, not just an id)."""
    meta = getattr(pipeline, "_metadata", {}).get(rule_id, {})
    stmt = meta.get("statement") if isinstance(meta, dict) else None
    if not stmt:
        node = await db.get_rule(rule_id)
        stmt = (node or {}).get("statement", "")
    return stmt or ""


async def build_promotion_review_artifact(
    candidate_id: str,
    pipeline: RetrievalPipeline,
    db: object,
    *,
    k: int = NEAREST_K_DEFAULT,
    high_similarity: float = HIGH_SIMILARITY_DEFAULT,
) -> dict:
    """6.3b: the REVIEW ARTIFACT for a graduation_pending candidate.

    Surfaces the candidate's statement/trigger/both-examples/severity/scope AND its
    canon-fit: nearest rules by embedding similarity + conflict candidates (the union of
    CONFLICTS_WITH targets, same-category members, and high-similarity neighbors). This is
    what lets the human be the APPROVER of canon, not a veto switch reacting to an id.
    """
    node = await db.get_rule(candidate_id)
    if node is None:
        raise ValueError(f"promotion candidate not found: {candidate_id}")

    text = f"{node.get('trigger', '')} {node.get('statement', '')}"
    qvec = pipeline._model.encode(text)
    qvec = qvec.tolist() if hasattr(qvec, "tolist") else qvec
    # k+1 because the candidate itself may rank top against its own text.
    raw = pipeline._vector.search(qvec, k + 1)

    nearest: list[dict] = []
    for r in raw:
        if r.rule_id == candidate_id:
            continue
        nearest.append({
            "id": r.rule_id,
            "score": round(float(r.score), 4),
            "statement": await _statement_for(r.rule_id, pipeline, db),
        })
        if len(nearest) >= k:
            break

    # conflict candidates = union of three signals, deduped by id with accumulated reasons.
    reasons: dict[str, set[str]] = {}
    for n in pipeline._cache.get_neighbors(candidate_id):
        if n.get("edge_type") == "CONFLICTS_WITH":
            reasons.setdefault(n["rule_id"], set()).add("CONFLICTS_WITH")
    category = node.get("category")
    if category:
        same_cat = await db.get_nodes_by_category(
            category, node.get("project", "writ"), exclude_id=candidate_id
        )
        for other in same_cat:
            reasons.setdefault(other["id"], set()).add("same-category")
    for nb in nearest:
        if nb["score"] >= high_similarity:
            reasons.setdefault(nb["id"], set()).add("high-similarity")

    conflict_candidates = [
        {
            "id": rid,
            "reasons": sorted(rs),
            "statement": await _statement_for(rid, pipeline, db),
        }
        for rid, rs in reasons.items()
    ]

    return {
        "candidate_id": candidate_id,
        "statement": node.get("statement", ""),
        "trigger": node.get("trigger", ""),
        "violation": node.get("violation", ""),
        "pass_example": node.get("pass_example", ""),
        "severity": node.get("severity", ""),
        "scope": node.get("scope", ""),
        "nearest_similar": nearest,
        "conflict_candidates": conflict_candidates,
    }


async def _node_outgoing_edges(db: object, node_id: str, project: str = "writ") -> list[dict]:
    """Declared outgoing edges of a node as [{target, type}], EXCLUDING BELONGS_TO
    (the category edge is re-derived from the `category` field on re-ingest -- emitting
    it would double-declare it)."""
    a_id = _GRAPH_ID_COALESCE.format(v="a")
    b_id = _GRAPH_ID_COALESCE.format(v="b")
    query = (
        f"MATCH (a)-[r]->(b) WHERE {a_id} = $id AND coalesce(a.project, 'writ') = $project "
        f"AND type(r) <> 'BELONGS_TO' RETURN type(r) AS type, {b_id} AS target"
    )
    async with db._driver.session(database=db._database) as session:
        result = await session.run(query, id=node_id, project=project)
        return [
            {"target": r["target"], "type": r["type"]}
            async for r in result if r["target"] is not None
        ]


async def export_node_to_source(db: object, node_id: str, output_dir: Path) -> Path:
    """6.3c: write ONE graduated Rule to its bible/ source file as rich front-matter,
    losslessly (all source-visible fields + declared edges; provenance/graduated_via
    included). The home is <output_dir>/methodology/<id>.md -- the same dual-location form
    ENF-COMMS-OUTPUT-001 uses, so re-ingest round-trips. Strips the environmental
    project/label so the source file matches the hand-authored corpus convention.

    Rule-only: a graduation candidate is always a Rule (the only graph-first authored
    type -- /propose and cli add create Rules; there is no graph-first methodology path)."""
    node = await db.get_rule(node_id)
    if node is None:
        raise ValueError(f"cannot export missing node: {node_id}")
    # Capture project BEFORE stripping it (M.2 edge queries are project-scoped).
    project = node.get("project", "writ") or "writ"
    node = {k: v for k, v in node.items() if k not in ("label", "project")}
    edges = await _node_outgoing_edges(db, node_id, project)
    md = node_to_yaml_frontmatter(node, edges=edges or None, node_type="Rule")
    target = Path(output_dir) / "methodology" / f"{node_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(md, encoding="utf-8")
    return target


async def promote_candidate(
    candidate_id: str,
    pipeline: RetrievalPipeline,
    db: object,
    output_dir: Path,
    *,
    edited_fields: dict | None = None,
) -> dict:
    """6.3c: the human GATES and AUTHORS the canon write (edit-at-gate, not approve-only).

    The candidate MUST be a graduation_pending Rule (the only graph-first authored type --
    there is no graph-first methodology path, so no methodology node can reach this state).
    The human either approves as-is (edited_fields is None -> graduated_via=
    'human-approve-asis') or edits the wording/examples before promotion (edited_fields ->
    graduated_via='human-edit'). The (edited) text re-runs the structural gate
    (schema/specificity/redundancy-excluding-self/conflict); a rejected edit does NOT write
    canon. On acceptance the node is stamped provenance=graduated + graduated_via and
    exported to bible/ source.

    NOTE: this is the business logic -- the agent-unforgeable token gate lives on the
    server route (POST /session/{sid}/promote-candidate), which is what makes the human
    the approver. Calling promote_candidate directly is the gated path's continuation
    AFTER the token check.

    RESOLVED (blueprint 6.3c): a human-edit that changes the normalized statement
    (lowercase + collapsed-whitespace) now resets the observation counts to 0 -- the
    claim's wording changed, so the prior evidence must be re-earned. Approve-as-is and a
    human-edit that leaves the statement normalized-equal preserve the existing counters.
    graduated_via still annotates HOW the text was authored.
    """
    node = await db.get_rule(candidate_id)
    if node is None:
        return {"promoted": False, "error": f"candidate not found: {candidate_id}"}
    if node.get("provenance") != "graduation_pending":
        return {
            "promoted": False,
            "error": (
                f"{candidate_id} is provenance={node.get('provenance')!r}; only a "
                f"graduation_pending candidate can be promoted"
            ),
        }

    edited = {k: v for k, v in node.items() if k not in ("label",)}
    graduated_via = "human-approve-asis"
    if edited_fields:
        edited.update(edited_fields)
        graduated_via = "human-edit"

    # Re-lint the (edited) text. structural_gate excludes the candidate from its own
    # redundancy check (gate.py), so an in-graph candidate does not self-reject; a
    # genuinely broken edit (empty/vague text, new duplicate) IS rejected -> no canon write.
    # Per the server.py:283 / PERF-IO convention: structural_gate runs a blocking ONNX
    # encode + vector search, so it is offloaded off the event loop via asyncio.to_thread
    # (same as the propose_rule call site in gate.py); it stays synchronous.
    gate_result = await asyncio.to_thread(structural_gate, edited, pipeline)
    if not gate_result.accepted:
        return {"promoted": False, "reasons": gate_result.reasons}

    edited["provenance"] = "graduated"
    edited["graduated_via"] = graduated_via
    # The authority comes from the promotion EVENT, not the original proposal.
    edited["authority"] = "ai-promoted"
    # 6.3c: re-earn evidence when the claim's wording changed -- a statement-changing
    # human-edit zeroes the observation counts; whitespace/case-only edits preserve them.
    if graduated_via == "human-edit":
        # Reachable only when edited_fields is truthy (graduated_via set above).
        new_stmt = edited_fields.get("statement", node.get("statement"))
        if _normalize_statement(new_stmt) != _normalize_statement(node.get("statement")):
            edited["times_seen_positive"] = 0
            edited["times_seen_negative"] = 0
    # source_origin -> ingest: the node now HAS a markdown home (closing the loop), so
    # reconcile no longer exempts it; provenance=graduated (in `edited`) is preserved.
    await db.create_rule(edited, source_origin="ingest")
    path = await export_node_to_source(db, candidate_id, output_dir)
    return {
        "promoted": True,
        "graduated_via": graduated_via,
        "source_file": str(path),
    }
