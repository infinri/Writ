"""Decision capture at approve (Phase 1c, deliverable 4).

At the moment a human approves the planning gate, snapshot the approved plan.md
into one Decision node in Neo4j, wire it to its Project and to the rules it cited,
and record each planned file as an OPEN claim for Phase 1d to resolve at commit.
Isolated from approval_workflow.py (CLI, no _db) and server.py (route) so the
fail-open capture logic is testable in one place.
"""

import logging
from datetime import datetime, timezone

from writ.session.harvester import _decision_id
from writ.session.locators import _find_plan_md
from writ.session.plan_harvest import harvest_plan
from writ.session.registration import ensure_project_registered

logger = logging.getLogger(__name__)


async def _resolve_project_name(db, project_root: str, cwd: str | None, runner=None) -> str | None:
    """Resolve the project name to scope the Decision under, register-before-capture.

    The derived name comes from ensure_project_registered (git identity -> :Project).
    When git identity cannot resolve (the cwd is in no git repo),
    ensure_project_registered returns None and so does this: a cwd in no git repo
    captures NOTHING. A record is NEVER scoped under a path-derived fallback, which
    restores the blueprint SCOPE-KEY COLLISION GUARD.
    """
    return await ensure_project_registered(db, cwd or project_root, runner=runner)


async def capture_decision_at_approve(
    db, project_root: str, session_id: str, phase: str, *, cwd: str | None = None,
    runner=None,
) -> str | None:
    """Snapshot the approved plan.md into a Decision and wire it into the graph.

    Returns the decision_id, or None when there is nothing to capture (no plan.md,
    or an explicit cwd that genuinely exists but is in no git repo). Callers are
    responsible for the fail-open guard.
    """
    plan_path = _find_plan_md(project_root, session_id)
    if not plan_path:
        return None
    with open(plan_path) as f:
        plan_text = f.read()

    harvested = harvest_plan(plan_text)
    rationale = harvested["rationale"]
    files = harvested["files"]
    cited_rules = harvested["cited_rules"]

    name = await _resolve_project_name(db, project_root, cwd, runner=runner)
    if name is None:
        return None

    # Content-hash id (Phase 3a): so this superseded live path, if re-enabled,
    # MERGEs the SAME Decision the post-commit path creates for the same plan.
    decision_id = _decision_id(name, plan_text)
    planned_files = [
        {"path": f["path"], "reason": f["reason"], "resolved": False}
        for f in files
    ]
    title = rationale.splitlines()[0] if rationale.strip() else plan_path

    await db.create_decision(
        decision_id=decision_id,
        project=name,
        title=title,
        rationale=rationale,
        planned_files=planned_files,
        governing_rule_ids=cited_rules,
        phase=phase,
        session_id=session_id,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    await db.wire_has_decision(name, decision_id, name)

    for rule_id in cited_rules:
        if await db.get_rule(rule_id) is not None:
            await db.wire_governed_by(decision_id, rule_id, name)

    return decision_id
