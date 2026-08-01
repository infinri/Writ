# writ-auth-scan: internal-service
"""Retrieval + rule-metadata routes for the Writ session daemon.

11 routes: /query, /methodology-companion, /prompt-bundle, /analyze,
/rule/{rule_id}, /propose, /feedback, /conflicts, /health, /always-on,
/subagent-role/{name}. Plus the /health helpers (_health_status,
_count_categories, _route_distribution) and the _ALWAYS_ON_PROCESS_MODES const.

Mutable/monkeypatched daemon state (_db, _pipeline, _trigger_index, _llm_client,
_instrumentation, _startup_time, writ_session, _run_cmd_format_locked) is read
via live `server.<attr>` access inside handler bodies only (the monkeypatch
seam). By-value pure functions/constants are imported from their origin modules.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

import writ.server as server
from writ.analysis import AnalyzeRequest, AnalyzeResponse
from writ.analysis.analyzer import run_analysis
from writ.analysis.friction import resolve_log_path
from writ.graph.db import Neo4jConnection
from writ.graph.predicates import INJECTION_RULE_WHERE
from writ.server.models import (
    CompanionRequest,
    ConflictsRequest,
    FeedbackRequest,
    ProposeRequest,
    PromptBundleRequest,
    QueryRequest,
)
from writ.shared.logging import emit, emit_exception
from writ.shared.tokens import estimate_tokens

router = APIRouter()


@router.post("/query")
async def query_rules(request: QueryRequest) -> dict[str, Any]:
    """Ranked list of matching domain rules. Mandatory rules excluded."""
    if server._pipeline is None:
        return {"error": "Pipeline not initialized. Run writ serve."}
    # Per PERF-IO-001: the pipeline query is CPU/IO-bound and synchronous; run it
    # off the event loop so concurrent hook requests are not serialized behind it.
    try:
        result = await asyncio.to_thread(
            server._pipeline.query,
            query_text=request.query,
            domain=request.domain,
            budget_tokens=request.budget_tokens,
            exclude_rule_ids=request.exclude_rule_ids,
            prefer_rule_ids=request.prefer_rule_ids,
            node_types=request.node_types,
            retrieval_mode=request.retrieval_mode,
            project=request.project,
        )
    except Exception as exc:
        # Audit item F: nothing distinguished "graph unreachable" from "no rules
        # matched", yet the fixes are unrelated (start Neo4j vs author a rule vs lower
        # the abstention threshold). A raising pipeline is almost always the graph:
        # Neo4jConnection errors surface here. Record it, then re-raise so the response
        # is byte-identical to before -- hooks fail open on a 500 and changing that shape
        # on the hottest route is a separate decision from making the failure visible.
        emit_exception(
            "server.query", exc, request.session_id or "", None,
            retrieval_mode=request.retrieval_mode,
            domain=request.domain or "",
            project=request.project or "",
        )
        raise
    _emit_retrieval_result(request, result)
    return result


def _emit_retrieval_result(request: QueryRequest, result: dict[str, Any]) -> None:
    """Record the quality of one retrieval on the metrics stream. Never raises.

    Audit item F: the S4 abstention gate returned an empty rule set with no event, so
    "Writ injected nothing" was indistinguishable from "nothing matched" from
    "the graph was unreachable" -- the three have completely different fixes.

    Emitted for EVERY query, not only the abstentions, because a rate needs a
    denominator: with abstentions alone an analyzer cannot tell one abstention in three
    queries from one in three hundred. `rule_count == 0` covers the empty-result case the
    audit also lists.

    Emitted HERE, at the daemon call site, rather than inside pipeline.query: the pipeline
    is a library used by authoring and benchmark paths that should stay silent, and the
    abstention threshold itself is opted into per call site for the same reason (see
    RULE_INJECTION_ABSTENTION_THRESHOLD). `writ query`, a human-run diagnostic, likewise
    does not emit.
    """
    try:
        rules = result.get("rules")
        emit(
            "metrics",
            "retrieval_result",
            request.session_id or "",
            None,
            mode=result.get("mode") or "",
            rule_count=len(rules) if isinstance(rules, list) else 0,
            total_candidates=result.get("total_candidates"),
            # Present only on the abstention path: the top raw cosine that failed the
            # threshold. It is the number to look at when tuning the threshold.
            abstain_signal=result.get("abstain_signal"),
            latency_ms=result.get("latency_ms"),
            retrieval_mode=request.retrieval_mode,
            domain=request.domain or "",
            project=request.project or "",
            had_error=bool(result.get("error")),
        )
    except Exception:  # noqa: BLE001 - telemetry must not break retrieval
        pass


@router.post("/methodology-companion")
async def methodology_companion(request: CompanionRequest) -> dict[str, Any]:
    """Methodology by workflow-state (floor u push u pull) -- CHANNEL 2 (1.5).

    DETERMINISTIC, not semantic: the trigger index matches mode floors, action
    pushes, and curated trigger_keywords (no embeddings). The response reuses
    /query's shape so the shared `cmd_format` renders it (summary form, no second
    formatter). BUILT-NOT-WIRED per D5: the hook keeps the legacy /query path
    until the 1.7 cutover (when floors are authored), so this introduces no
    behavior change yet.
    """
    if server._trigger_index is None:
        return {"error": "Trigger index not initialized. Run writ serve."}
    matched = server._trigger_index.match(
        mode=request.mode,
        prompt=request.prompt,
        action=request.action,
        budget_tokens=request.budget_tokens,
        exclude_ids=request.exclude_rule_ids,
    )
    rules = [
        {
            "rule_id": n["id"],
            "node_type": n["node_type"],
            "trigger": n.get("trigger", ""),
            "statement": n.get("statement", ""),
            "severity": n.get("severity") or "?",
            "authority": "human",
            "domain": n.get("domain") or "?",
            "score": 1.0,  # deterministic match, not a ranked score
            "channel": n["channel"],
            "relationships": [],
        }
        for n in matched["nodes"]
    ]
    return {
        "rules": rules,
        "mode": "summary",
        "total_candidates": len(rules),
        "latency_ms": 0,
        "total_tokens": matched["total_tokens"],
        "over_budget": matched["over_budget"],
    }


@router.post("/prompt-bundle")
async def prompt_bundle(request: PromptBundleRequest) -> dict[str, Any]:
    """#8: the three per-prompt injection channels in ONE warm call.

    Awaits the existing /query, /always-on, /methodology-companion handlers
    in-process, renders each via the shared prompt_bundle helpers, applies the
    SAME cache updates + friction events the bash hook did, and returns the
    rendered pieces SEPARATELY so the hook interleaves them with its bash-side mode
    reminders (preserving byte-identical output order). Retrieval already required
    the daemon, so this adds no new degraded-mode risk: daemon-down -> the hook
    prints 'server unavailable', exactly as before. Moves ~16 cold python3 spawns
    off the hot path (measured ~646ms -> target ~300ms per prompt).
    """
    import json as _json
    from writ.retrieval.prompt_bundle import (
        always_on_rule_ids, compute_nudge, extract_rule_objects, render_always_on,
        split_format,
    )

    if server._pipeline is None:
        return {"error": "Pipeline not initialized. Run writ serve."}

    sid = request.session_id
    mode = request.mode or ""
    prompt = request.prompt or ""
    effort = request.effort or ""

    cache = await asyncio.to_thread(server.writ_session._read_cache, sid)
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    current_phase = cache.get("current_phase", "")
    if by_phase and current_phase:
        exclude_ids = list(set(by_phase.get(current_phase, [])))
    else:
        exclude_ids = list(set(cache.get("loaded_rule_ids", [])))
    remaining_budget = cache.get("remaining_budget", 8000)
    prefer_ids = cache.get("last_injected_rule_ids", []) or []
    detected_domain = cache.get("detected_domain", "") or ""

    # Cache updates run server-side (the session cache is tempdir/session-id-keyed,
    # so its location is cwd-independent). Friction logging stays CLIENT-SIDE in the
    # hook: _resolve_log_path is cwd-relative when WRIT_FRICTION_LOG is unset, so
    # logging here (daemon cwd) would relocate a real project's rag_query telemetry
    # away from project/workflow-friction.log. The per-channel meta is returned for
    # the hook to log with the project-local path + the same format as before.
    out: dict[str, Any] = {
        "always_on_block": "", "rules_text": "", "methodology_block": "",
        "nudge": "", "error": False,
        "broad_meta": None, "ao_meta": None, "method_meta": None,
    }

    # --- Channel 1: broad /query ---
    qresp = await query_rules(QueryRequest(
        query=prompt,
        budget_tokens=remaining_budget,
        exclude_rule_ids=exclude_ids,
        prefer_rule_ids=(prefer_ids or None),
        domain=(detected_domain if detected_domain and detected_domain != "universal" else None),
        # This is the hot per-prompt retrieval and the session is right here, so its
        # retrieval_result row is session-correlated even though the four hooks that POST
        # /query directly do not send one yet.
        session_id=sid,
    ))
    if "error" in qresp:
        # Match the legacy hook: a /query error aborted the whole injection (the
        # always-on + methodology channels ran AFTER it), so return early.
        out["error"] = True
        return out
    else:
        out["nudge"] = compute_nudge(qresp)
        text, meta = split_format(await asyncio.to_thread(server._run_cmd_format_locked, qresp))
        out["rules_text"] = text
        rule_ids = meta.get("rule_ids", []) or []
        cost = meta.get("cost", 0) or 0
        await asyncio.to_thread(server.writ_session.cmd_update, sid, [
            "--add-rules", _json.dumps(rule_ids),
            "--cost", str(cost),
            "--inc-queries",
            "--set-last-injected-rule-ids", _json.dumps(rule_ids),
            "--add-rule-objects", _json.dumps(extract_rule_objects(qresp)),
        ])
        out["broad_meta"] = {"rule_ids": rule_ids, "cost": cost}

    # --- Channel 2: always-on ---
    aoresp = await always_on_bundle(
        mode=(mode or "universal"),
        at=("prompt" if request.always_on_filter else None),
        context=prompt,
    )
    ao_json = aoresp if isinstance(aoresp, dict) else {}
    block, ao_tokens, ao_count = render_always_on(ao_json)
    out["always_on_block"] = block
    if block and ao_tokens > 0:
        # Record the IDs, not just the token count. This channel injects rules into the
        # prompt; recording only tokens left _validate_phase_a validating citations
        # against a set with no always-on rule in it, so the gate reported the agent's
        # correct citations as hallucinated and spent the user's approval token.
        ao_ids = always_on_rule_ids(ao_json)
        await asyncio.to_thread(server.writ_session.cmd_update, sid, [
            "--add-always-on-tokens", str(ao_tokens),
            "--add-always-on-rules", _json.dumps(ao_ids),
        ])
        out["ao_meta"] = {"tokens": ao_tokens, "count": ao_count, "rule_ids": ao_ids}

    # --- Channel 3: methodology companion ---
    qsource = {
        "work": "methodology", "debug": "debug-playbook",
        "investigate": "investigation-doctrine",
        "conversation": "methodology-conversation", "review": "methodology-review",
    }.get(mode, "")
    if qsource and remaining_budget > 600:
        cresp = await methodology_companion(CompanionRequest(
            mode=mode, prompt=prompt, exclude_rule_ids=exclude_ids, budget_tokens=2000,
        ))
        if "error" not in cresp:
            ctext, cmeta = split_format(await asyncio.to_thread(server._run_cmd_format_locked, cresp))
            if ctext:
                out["methodology_block"] = "[Writ: methodology companion]\n" + ctext
            crule_ids = cmeta.get("rule_ids", []) or []
            ccost = cmeta.get("cost", 0) or 0
            if crule_ids:
                await asyncio.to_thread(server.writ_session.cmd_update, sid, [
                    "--add-rules", _json.dumps(crule_ids),
                    "--cost", str(ccost), "--inc-queries",
                ])
            out["method_meta"] = {"rule_ids": crule_ids, "cost": ccost, "query_source": qsource}

    return out


@router.post("/analyze")
async def analyze_code(request: AnalyzeRequest) -> AnalyzeResponse | dict[str, Any]:
    """Analyze code against retrieved rules. Returns structured compliance verdict."""
    if server._pipeline is None or server._llm_client is None or server._instrumentation is None:
        return {"error": "Pipeline not initialized. Run writ serve."}
    return await run_analysis(
        code=request.code,
        file_path=request.file_path,
        phase=request.phase,
        context=request.context,
        pipeline=server._pipeline,
        llm_client=server._llm_client,
        instrumentation=server._instrumentation,
    )


@router.get("/rule/{rule_id}")
async def get_rule(rule_id: str, include_graph: bool = False) -> dict[str, Any]:
    """Full rule node. Optionally includes 1-hop graph context."""
    if server._db is None:
        return {"error": "Database not connected."}
    rule = await server._db.get_rule(rule_id)
    if rule is None:
        return {"error": f"Rule {rule_id} not found."}
    response: dict[str, Any] = {"rule": rule}
    if include_graph:
        neighbors = await server._db.traverse_neighbors(rule_id, hops=1)
        response["graph_context"] = neighbors
    return response


@router.post("/propose")
async def propose_rule_endpoint(request: ProposeRequest) -> dict[str, Any]:
    """Propose an AI-generated rule. Runs structural gate, ingests if accepted."""
    if server._pipeline is None or server._db is None:
        return {"error": "Pipeline not initialized. Run writ serve."}

    from writ.gate import propose_rule
    from writ.origin_context import DEFAULT_DB_PATH

    candidate = {
        "rule_id": request.rule_id,
        "domain": request.domain,
        "severity": request.severity,
        "scope": request.scope,
        "trigger": request.trigger,
        "statement": request.statement,
        "violation": request.violation,
        "pass_example": request.pass_example,
        "enforcement": request.enforcement,
        "rationale": request.rationale,
        "last_validated": request.last_validated,
    }

    result = await propose_rule(
        candidate,
        server._pipeline,
        server._db,
        origin_db_path=DEFAULT_DB_PATH,
        task_description=request.task_description,
        query_that_triggered=request.query_that_triggered,
    )
    return result


@router.post("/feedback")
async def record_feedback(request: FeedbackRequest) -> dict[str, Any]:
    """Record positive or negative feedback for a rule."""
    if server._db is None:
        return {"error": "Database not connected."}
    if request.signal not in ("positive", "negative"):
        return {"error": f"Invalid signal: {request.signal}. Must be 'positive' or 'negative'."}

    if request.signal == "positive":
        found = await server._db.increment_positive(request.rule_id)
    else:
        found = await server._db.increment_negative(request.rule_id)

    if not found:
        return {"error": f"Rule {request.rule_id} not found."}

    # 6.3a: a positive signal may push a PROPOSED rule across the graduation
    # threshold -> flip it to graduation_pending (a CANDIDATE for the human gate).
    # Statistical crossing only -- no authority promotion, no bible/ write.
    graduation_pending = await server._db.evaluate_and_flip_graduation(request.rule_id)
    return {
        "rule_id": request.rule_id, "signal": request.signal, "recorded": True,
        "graduation_pending": graduation_pending == "graduation_pending",
    }


@router.post("/conflicts")
async def check_conflicts(request: ConflictsRequest) -> dict[str, Any]:
    """CONFLICTS_WITH edges between provided rules."""
    if server._db is None:
        return {"error": "Database not connected."}
    query = """
        MATCH (a:Rule)-[:CONFLICTS_WITH]-(b:Rule)
        WHERE a.rule_id IN $ids AND b.rule_id IN $ids
        AND a.rule_id < b.rule_id
        RETURN a.rule_id AS rule_a, b.rule_id AS rule_b
    """
    async with server._db._driver.session(database=server._db._database) as session:
        result = await session.run(query, ids=request.rule_ids)
        conflicts = [record.data() async for record in result]
    return {"conflicts": conflicts}


def _health_status(rule_count: int, index_warm: bool) -> str:
    """FIX-3: 'degraded' when the retrieval index is warm (queries work) but Neo4j reports
    zero rules -- a DB/index split (the audit's rule_count:0 pathology). Such a daemon must
    not read as a bland 'healthy'. Otherwise 'healthy'."""
    if index_warm and rule_count == 0:
        return "degraded"
    return "healthy"


async def _count_categories(db: Neo4jConnection) -> int:
    """Read-only count of Category nodes. Returns 0 on any error or empty corpus."""
    query = "MATCH (c:Category) RETURN count(c) AS category_count"
    try:
        async with db._driver.session(database=db._database) as session:
            result = await session.run(query)
            record = await result.single()
        if record is None:
            return 0
        return int(record.get("category_count") or 0)
    except Exception as exc:
        # A 0 from a graph failure reads downstream exactly like a genuinely empty
        # corpus, so the caller cannot tell "no categories" from "no database".
        from writ.shared.logging import emit_exception

        emit_exception("server.query.count_categories", exc)
        return 0


async def _route_distribution(db: Neo4jConnection) -> dict[str, int]:
    """Read-only {route: count} census across Category.routes.

    Categories carry a non-empty `routes` list; UNWIND fans them out so each
    route is tallied once per category that lists it. Returns {} on any error
    or when no categories exist.
    """
    query = """
        MATCH (c:Category)
        UNWIND c.routes AS route
        RETURN route AS route, count(*) AS count
        ORDER BY route
    """
    try:
        async with db._driver.session(database=db._database) as session:
            result = await session.run(query)
            rows = [record.data() async for record in result]
    except Exception as exc:
        # Same ambiguity as _count_categories: an empty distribution from a failure
        # is indistinguishable from a corpus with no routed categories.
        from writ.shared.logging import emit_exception

        emit_exception("server.query.route_distribution", exc)
        return {}
    distribution: dict[str, int] = {}
    for row in rows:
        route = row.get("route")
        if route is None:
            continue
        distribution[str(route)] = int(row.get("count") or 0)
    return distribution


@router.get("/health")
async def health() -> dict[str, Any]:
    """Service status, rule count, index state, last ingestion timestamp.

    Includes `cache_dir` (FIX-2): the session-cache directory this daemon resolved.
    ensure-server.sh compares it against the caller's expected dir to detect and realign
    a server-cache desync (a daemon started under a divergent TMPDIR).
    """
    cache_dir = getattr(server.writ_session, "CACHE_DIR", None) if server.writ_session else None
    if server._db is None:
        return {"status": "not_ready", "error": "Database not connected.", "cache_dir": cache_dir}

    rule_count = await server._db.count_rules()

    # Count mandatory rules.
    query = "MATCH (r:Rule) WHERE r.mandatory = true RETURN count(r) AS count"
    async with server._db._driver.session(database=server._db._database) as session:
        result = await session.run(query)
        record = await result.single()
        mandatory_count = record["count"]

    # Category census (Phase 0). Both queries are read-only and defensive: a
    # corpus with zero Category nodes returns 0 / {} rather than erroring.
    category_count = await _count_categories(server._db)
    route_distribution = await _route_distribution(server._db)

    index_warm = server._pipeline is not None
    status = _health_status(rule_count, index_warm)
    payload: dict[str, Any] = {
        "status": status,
        "rule_count": rule_count,
        "mandatory_count": mandatory_count,
        "category_count": category_count,
        "route_distribution": route_distribution,
        "index_state": "warm" if index_warm else "cold",
        "startup_time": server._startup_time.isoformat() if server._startup_time else None,
        "cache_dir": cache_dir,
        # The friction-log path this daemon writes to. The test suite aligns on
        # this (like cache_dir) so daemon-emitted events don't pollute the repo log.
        "friction_log": str(resolve_log_path()),
    }
    if status == "degraded":
        payload["warning"] = (
            "index is warm but Neo4j reports 0 rules -- the daemon's graph DB and retrieval "
            "index disagree (DB/index split). Re-ingest, or restart against the correct Neo4j."
        )
    return payload


# --- Phase 2: always-on rule bundle (plan Section 3.4) -----------------------

# Modes in which process-domain always-on rules (build/debug doctrine) are
# included. Process-domain rules apply when the agent reasons about producing
# OR diagnosing code, so they belong in BOTH "work" and "debug". They are
# excluded from conversation/review/universal, where the agent is neither
# building nor diagnosing.
_ALWAYS_ON_PROCESS_MODES = {"work", "debug"}


@router.get("/always-on")
async def always_on_bundle(
    mode: str | None = None, at: str | None = None, context: str = ""
) -> dict[str, Any]:
    """Return rules flagged always_on=true for injection into every session.

    Query params:
    - mode: optional session mode (work, debug, review, conversation). When
      provided, scopes the bundle to rules appropriate for that mode. When
      omitted, returns the universal bundle (all always-on rules).
    - at: optional injection point (prompt, write, bash, stop). When provided,
      applies APPLICABILITY-scoped filtering (WRIT-BLUEPRINT 3.5): only rules
      whose applicability_scope matches `at` and whose trigger_keywords match
      `context` are returned. When omitted, the full mode-scoped bundle is
      returned (back-compatible blanket behavior). No ranking, no budget drop.
    - context: the text matched against trigger_keywords for `at` (the prompt at
      `prompt`, file path + content at `write`, the command at `bash`).

    Response:
    - rules: list of dicts with rule_id, trigger, statement, severity, scope,
      rendered in SUMMARY form (short). Full content is available via /query
      or bundle expansion per plan Section 3.4 conditional-render-depth policy.
    - total_tokens: estimated token count for budget-audit purposes.
    - cap: 5000 per plan Section 0.4 decision 3.
    """
    if server._db is None:
        return {"error": "Database not connected."}

    # 3.6a: UNION injection -- a rule reaches the agent if it is a mandatory
    # obligation OR flagged always-on (WRIT-BLUEPRINT 3.5). Single-source
    # predicate (writ/graph/predicates.py) shared with the writ-validate
    # stranded-mandatory invariant so the two cannot drift. `mandatory` is
    # returned so the mode-strip below can exempt mandatory rules.
    query = f"""
        MATCH (r:Rule)
        WHERE {INJECTION_RULE_WHERE}
        RETURN r.rule_id AS rule_id, r.trigger AS trigger, r.statement AS statement,
               r.severity AS severity, r.scope AS scope, r.domain AS domain,
               r.mandatory AS mandatory,
               r.applicability_scope AS applicability_scope,
               r.trigger_keywords AS trigger_keywords
        ORDER BY r.severity DESC, r.rule_id
    """
    async with server._db._driver.session(database=server._db._database) as session:
        result = await session.run(query)
        rows = [record.data() async for record in result]

    # FRB-COMMS-* ForbiddenResponse nodes are also always-on.
    frb_query = """
        MATCH (n:ForbiddenResponse)
        RETURN n.forbidden_id AS rule_id, n.trigger AS trigger,
               n.statement AS statement, n.severity AS severity,
               n.scope AS scope, n.domain AS domain
        ORDER BY n.forbidden_id
    """
    async with server._db._driver.session(database=server._db._database) as session:
        result = await session.run(frb_query)
        frb_rows = [record.data() async for record in result]

    # 1.7 CUTOVER: block-3 (always_on Skill/Playbook methodology) DELETED. Per D1
    # clean separation, /always-on is now RULES + ForbiddenResponse only (CHANNEL
    # 1); ALL methodology -- including the universal floor (folded into
    # floor_modes=all-modes) -- is owned by /methodology-companion (CHANNEL 2).
    # This removes the split where a node's floor membership was delivered by two
    # endpoints; the universal floor still injects every turn, now via the
    # companion's floor channel.
    combined = rows + frb_rows

    # Mode scoping for process-domain rules (build/debug doctrine).
    # Process-domain always-on rules apply when the agent is producing OR
    # diagnosing code, so they are included in both "work" and "debug"
    # (_ALWAYS_ON_PROCESS_MODES). They are excluded from conversation/review/
    # universal. This is the carve-out the previous comment described but the
    # code never implemented: before, every non-"work" mode (including debug)
    # stripped domain==process, which silently dropped ENF-PROC-DEBUG-001 from
    # debug mode -- the one mode that doctrine is authored for.
    # 3.6a: mandatory rules are EXEMPT from the process-domain strip -- a
    # mandatory rule must reach the agent in every mode (blanket-inject, the
    # measured-under-cap state), else the 5 process-domain mandatory rules
    # (ENF-PROC-BRAIN/PLAN/TDD/VERIFY/WORKTREE) silently re-strand per-mode. The
    # strip still removes non-mandatory always-on process rules
    # (e.g. ENF-PROC-DEBUG-001) outside work/debug. (Methodology SKL-PROC nodes
    # left /always-on at the 1.7 cutover; their mode-scoping is now floor_modes.)
    if mode and mode.lower() not in _ALWAYS_ON_PROCESS_MODES:
        combined = [
            r for r in combined
            if r.get("mandatory") is True
            or (r.get("domain") or "").lower() not in ("process",)
        ]

    # Applicability-scoped filtering (WRIT-BLUEPRINT 3.5). Only when `at` is given;
    # otherwise the full mode-scoped bundle is returned (back-compatible). FRB-COMMS
    # ForbiddenResponse rows carry no routing fields -> default them to universal
    # (response discipline applies to every turn). A Rule with no scope fails open to
    # universal inside the filter, so nothing silently disappears pre-migration.
    if at:
        from writ.retrieval.always_on_filter import select_always_on
        for r in combined:
            if not r.get("applicability_scope") and r.get("rule_id", "").startswith("FRB-"):
                r["applicability_scope"] = ["universal"]
        combined = select_always_on(combined, at, context)

    # Summary-form render: trigger + statement only (plan Section 3.4).
    summary_bundle = []
    total_tokens = 0
    for r in combined:
        trigger = (r.get("trigger") or "").strip()
        statement = (r.get("statement") or "").strip()
        est = estimate_tokens(trigger, statement)
        summary_bundle.append({
            "rule_id": r["rule_id"],
            "trigger": trigger,
            "statement": statement,
            "severity": r.get("severity"),
            "est_tokens": est,
        })
        total_tokens += est

    return {
        "rules": summary_bundle,
        "total_tokens": total_tokens,
        "cap": 5000,  # plan Section 0.4 decision 3
        "mode_scope": mode or "universal",
        "injection_point": at or "all",
        "render_mode": "summary",
    }


@router.get("/subagent-role/{name}")
async def subagent_role_get(name: str) -> dict[str, Any]:
    """Return a SubagentRole node's canonical prompt template from the graph.

    Phase 3 Section 8 deliverable 2: graph is canonical for subagent prompts;
    .claude/agents/*.md files are exported from the graph. This endpoint
    exposes the canonical text for CLI and test consumers.
    """
    if server._db is None:
        return {"error": "Database not connected."}
    rec = await server._db.get_subagent_role(name)
    if rec is None:
        return {"error": f"SubagentRole '{name}' not found."}
    return {
        "role_id": rec["role_id"],
        "name": rec["name"],
        "prompt_template": rec["prompt_template"],
        "model_preference": rec["model_preference"],
        "dispatched_by": rec["dispatched_by"] or [],
    }
