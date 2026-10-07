# writ-auth-scan: internal-service
"""Retrieval + rule-metadata routes for the Writ session daemon.

14 routes: /query, /methodology-companion, /prompt-bundle, /analyze,
/rule/{rule_id}, /propose, /feedback, /feedback/batch, /conflicts, /health,
/always-on, /subagent-role/{name}, /subagent/start-context, /retrieval/reload. Plus the
/health helpers (_health_status, _count_categories, _route_distribution, _log_destinations,
_retrieval_status) and the _ALWAYS_ON_PROCESS_MODES const.

Mutable/monkeypatched daemon state (_db, _pipeline, _trigger_index, _llm_client,
_instrumentation, _startup_time, writ_session, _run_cmd_format_locked) is read
via live `server.<attr>` access inside handler bodies only (the monkeypatch
seam). By-value pure functions/constants are imported from their origin modules.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any

from fastapi import APIRouter

import writ.server as server
from writ.analysis import AnalyzeRequest, AnalyzeResponse
from writ.analysis.analyzer import run_analysis
from writ.graph.db import Neo4jConnection
from writ.graph.predicates import INJECTION_RULE_WHERE
from writ.server.models import (
    CompanionRequest,
    ConflictsRequest,
    FeedbackBatchRequest,
    FeedbackRequest,
    ProposeRequest,
    PromptBundleRequest,
    QueryRequest,
    RecallRequest,
    SubagentStartContextRequest,
)
from writ.server.routes.session_state import _format_query_response
from writ.shared.logging import emit, emit_destination, emit_exception
from writ.shared.tokens import cost_for, estimate_tokens
from writ.shared.trust import is_verify_stale

router = APIRouter()


async def resolve_caller_project(project_root: str) -> str:
    """Resolve a caller-supplied project ROOT to a project NAME, or "" on failure.

    The hooks send a root because they already hold one (detect_project_root is pure
    bash); the registry that maps root to name lives in the daemon, which is already
    being called. Resolution reads an in-process cached registry, so the steady-state
    cost is zero Neo4j round trips per request (see ProjectStoreMixin._cached_projects).

    NEVER raises and never returns an error to the caller. Retrieval is fail-open end
    to end (rag_query runs with a 0.3s connect timeout and `|| true`), and an
    unregistered project root is a NORMAL condition on any project that has not been
    registered yet. The safe degradation is doctrine-only retrieval, which an empty
    answer produces; a raise here would blank a turn's rules or surface a hook error
    for something routine. The degradation is recorded on the retrieval_result row
    instead, so it is visible rather than silent.
    """
    if not project_root or server._db is None:
        return ""
    try:
        return await server._db.resolve_project_for_cwd(project_root) or ""
    except Exception as exc:
        emit_exception("server.query.resolve_project", exc, "", None, project_root=project_root)
        return ""


@router.post("/query")
async def query_rules(request: QueryRequest) -> dict[str, Any]:
    """Ranked list of matching domain rules. Mandatory rules excluded."""
    if server._pipeline is None:
        return {"error": "Pipeline not initialized. Run writ serve."}
    # An already-resolved `project` wins over `project_root`, so an internal caller
    # (prompt_bundle's channel 1) that has resolved once does not resolve again.
    project = request.project or await resolve_caller_project(request.project_root)
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
            project=project or None,
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
            project=project or "",
        )
        raise
    _emit_retrieval_result(request, result, project)
    return result


def _emit_retrieval_result(
    request: QueryRequest, result: dict[str, Any], project: str = "",
) -> None:
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
            project=project or request.project or "",
            # The RAW root the caller sent, recorded even (especially) when it did
            # not resolve. A scope degradation is otherwise invisible: an operator
            # could see project="" on the row and not know WHICH root is
            # unregistered, so the fix (register that project) would have no target.
            project_root=request.project_root or "",
            had_error=bool(result.get("error")),
        )
    except Exception:  # noqa: BLE001 - telemetry must not break retrieval
        pass


@router.post("/methodology-companion")
async def methodology_companion(request: CompanionRequest) -> dict[str, Any]:
    """Methodology by workflow-state (floor u push u pull): CHANNEL 2 (1.5).

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


# The channels a request names when it omits `sections`: the three /prompt-bundle always
# returned. Recall is opt-in, so a legacy caller never spends the once-per-session briefing.
_LEGACY_PROMPT_SECTIONS = ("ranked", "always_on", "methodology")


@router.post("/prompt-bundle")
async def prompt_bundle(request: PromptBundleRequest) -> dict[str, Any]:
    """The per-prompt injection sections, each rendered under its own character ceiling.

    `sections` names which of always_on / ranked / methodology / recall to build. Each
    UserPromptSubmit hook asks for exactly one, so no hook prints another's text and none
    crosses the host's per-hook cap (docs/adr/ADR-prompt-injection-split.md). Omitted, it
    is the legacy trio in one call. Every section reads the ONE cache snapshot taken below,
    which is what lets four parallel hooks agree: no section depends on a write another
    section makes in the same turn.

    Awaits the existing /query, /always-on, /methodology-companion handlers in-process and
    applies the same cache updates the bash hook used to; friction rows stay client-side
    (the per-section meta is returned for the hook to log under the project-local path).
    """
    import json as _json
    from writ.retrieval.injection_ceiling import (
        NUDGE_TEXT, clamp_lines, collapse_floor, fit_methodology, fit_ranked,
        ranked_char_limit, render_always_on_section, section_char_limit,
    )
    from writ.retrieval.prompt_bundle import compute_nudge, extract_rule_objects, tag_overlap
    from writ.session.budget_tracking import should_skip_cache
    from writ.session.injection_state import (
        injection_epoch, marked_this_epoch, retrieval_exclude_ids, shown_ids,
    )
    from writ.shared.tokens import PROMPT_SECTION_TOKENS

    if server._pipeline is None:
        return {"error": "Pipeline not initialized. Run writ serve."}

    sections = set(request.sections) if request.sections is not None else set(_LEGACY_PROMPT_SECTIONS)
    sid = request.session_id
    mode = request.mode or ""
    prompt = request.prompt or ""

    cache = await asyncio.to_thread(server.writ_session._read_cache, sid)
    # Shown since the last compaction, scoped to the phase in work mode (program item 1c).
    exclude_ids = list(set(retrieval_exclude_ids(cache)))
    remaining_budget = cache.get("remaining_budget", 8000)
    prefer_ids = cache.get("last_injected_rule_ids", []) or []
    epoch = injection_epoch(cache)

    out: dict[str, Any] = {
        "always_on_block": "", "rules_text": "", "methodology_block": "", "recall_block": "",
        "nudge": "", "nudge_text": "", "error": False, "skipped": False,
        "broad_meta": None, "ao_meta": None, "method_meta": None,
    }
    # Explicit sections only: a legacy caller keeps its old no-skip behavior.
    if request.sections is not None and should_skip_cache(cache):
        out["skipped"] = True
        return out

    # --- Channel 1 retrieval (ranked). An error still aborts before any always-on read.
    # include_ranked=False (an orchestrator master) skips the RETRIEVAL, not just the
    # render, so the Neo4j read is never paid for.
    run_ranked = "ranked" in sections and request.include_ranked
    qresp: dict[str, Any] = {}
    if run_ranked:
        # No domain= here (program item 1b). The CwdChanged hook used to tag the session
        # with a project LANGUAGE and this passed it as an exact domain filter, but no rule
        # domain is a language (writ/graph/schema.py VALID_DOMAINS), so the filter dropped
        # every ranked candidate. An explicit domain from another /query caller still filters.
        qresp = await query_rules(QueryRequest(
            query=prompt,
            budget_tokens=min(remaining_budget, PROMPT_SECTION_TOKENS["ranked"]),
            exclude_rule_ids=exclude_ids,
            prefer_rule_ids=(prefer_ids or None),
            session_id=sid,
            project_root=request.project_root,
        ))
        if "error" in qresp:
            out["error"] = True
            return out

    # --- Channel 2 data: needed to emit always-on, and to tag the ranked overlap. The
    # ranked request computes it itself (read-only) so it never depends on the always-on
    # hook having run in the same turn.
    ao_json: dict[str, Any] = {}
    ao = None
    if "always_on" in sections or run_ranked:
        aoresp = await always_on_bundle(
            mode=(mode or "universal"),
            at=("prompt" if request.always_on_filter else None),
            context=prompt,
        )
        ao_json = aoresp if isinstance(aoresp, dict) else {}
        ao = render_always_on_section(
            ao_json, shown_ids(cache, "always_on"), section_char_limit("always_on"),
        )

    # --- Channel 1 render.
    if "ranked" in sections:
        if run_ranked:
            out["nudge"] = compute_nudge(qresp)
            out["nudge_text"] = NUDGE_TEXT.get(out["nudge"], "")
            # tag_overlap COPIES, so `qresp` still carries every field for the
            # --add-rule-objects cache the compliance-matching path reads.
            render_payload = dict(qresp)
            render_payload["rules"] = tag_overlap(qresp.get("rules") or [], ao.rule_ids if ao else [])
            text, meta, _used = await asyncio.to_thread(
                fit_ranked, render_payload, server._run_cmd_format_locked,
                ranked_char_limit(request.reserve_chars, out["nudge_text"]),
            )
            out["rules_text"] = text
            rule_ids = meta.get("rule_ids", []) or []
            cost = meta.get("cost", 0) or 0
            rendered = set(rule_ids)
            await asyncio.to_thread(server.writ_session.cmd_update, sid, [
                "--add-rules", _json.dumps(rule_ids),
                "--cost", str(cost),
                "--inc-queries",
                "--set-last-injected-rule-ids", _json.dumps(rule_ids),
                "--add-rule-objects", _json.dumps(
                    [o for o in extract_rule_objects(qresp) if o["rule_id"] in rendered]
                ),
            ])
            out["broad_meta"] = {"rule_ids": rule_ids, "cost": cost}
        else:
            # A SENTINEL, never an empty result: a zero-rule broad_meta is the abstention
            # signal, so recording a configuration choice that way would corrupt every
            # census that counts retrievals by source.
            out["broad_meta"] = {"suppressed": True}

    # --- Channel 2: always-on (emit).
    if "always_on" in sections and ao is not None:
        out["always_on_block"] = ao.text
        if ao.text and ao.tokens > 0:
            # Citations record every id present in the block, full or pointer; the
            # collapse record only the ones rendered in full.
            updates = [
                "--add-always-on-tokens", str(ao.tokens),
                "--add-always-on-rules", _json.dumps(ao.rule_ids),
            ]
            if ao.full_ids:
                updates += ["--mark-shown", "always_on", epoch, _json.dumps(ao.full_ids)]
            await asyncio.to_thread(server.writ_session.cmd_update, sid, updates)
            out["ao_meta"] = {
                "tokens": ao.tokens, "count": len(ao.rule_ids),
                "rule_ids": ao.rule_ids,
            }

    # --- Channel 3: methodology companion. The floor still bypasses the session exclude
    # and the rule budget; what changes is that an already-shown floor rule renders as a
    # pointer and the whole block fits its section limit.
    qsource = {
        "work": "methodology", "debug": "debug-playbook",
        "investigate": "investigation-doctrine",
        "conversation": "methodology-conversation", "review": "methodology-review",
    }.get(mode, "")
    if "methodology" in sections and qsource:
        floor_ids = server._trigger_index.floor_ids(mode) if server._trigger_index else set()
        cresp = await methodology_companion(CompanionRequest(
            mode=mode, prompt=prompt,
            exclude_rule_ids=[i for i in exclude_ids if i not in floor_ids],
            budget_tokens=PROMPT_SECTION_TOKENS["methodology"] if remaining_budget > 600 else 0,
            project_root=request.project_root,
        ))
        if "error" not in cresp:
            payload = dict(cresp)
            payload["rules"] = collapse_floor(cresp.get("rules") or [], shown_ids(cache, "floor"))
            ctext, cmeta, used = await asyncio.to_thread(
                fit_methodology, payload, server._run_cmd_format_locked,
                section_char_limit("methodology"),
            )
            out["methodology_block"] = ctext
            crule_ids = cmeta.get("rule_ids", []) or []
            used_rules = used.get("rules") or []
            ccost = cost_for([r for r in used_rules if r.get("channel") != "floor"], "summary")
            floor_full = [
                r["rule_id"] for r in used_rules
                if r.get("channel") == "floor" and not r.get("pointer_only") and r.get("rule_id")
            ]
            updates: list[str] = []
            if crule_ids:
                updates += ["--add-rules", _json.dumps(crule_ids), "--cost", str(ccost), "--inc-queries"]
            if floor_full:
                updates += ["--mark-shown", "floor", epoch, _json.dumps(floor_full)]
            if updates:
                await asyncio.to_thread(server.writ_session.cmd_update, sid, updates)
            out["method_meta"] = {"rule_ids": crule_ids, "cost": ccost, "query_source": qsource}

    # --- Recall: master only (the hook does not ask from a sub-agent). The first prompt of
    # an epoch briefs in full and marks the epoch even with nothing shown; a later prompt
    # shows only matched cards this epoch has not shown, and writes only when it shows one.
    if "recall" in sections:
        from writ.server.routes import decision_memory
        briefed = marked_this_epoch(cache, "recall")
        seen = shown_ids(cache, "recall")
        briefing = ""
        cards: list = []
        try:
            rresp = await decision_memory.recall(RecallRequest(
                project_root=request.project_root or "", budget=PROMPT_SECTION_TOKENS["recall"],
                prompt=prompt, exclude_ids=sorted(seen), matched_only=briefed,
            ))
            if isinstance(rresp, dict):
                briefing = str(rresp.get("briefing") or "")
                cards = rresp.get("cards") or []
        except Exception as exc:
            emit_exception("server.prompt_bundle.recall", exc, sid, None)
        out["recall_block"] = clamp_lines(briefing, section_char_limit("recall"))
        shown = [c["id"] for c in cards if c.get("head") and c["head"] in out["recall_block"]]
        if shown or not briefed:
            await asyncio.to_thread(server.writ_session.cmd_update, sid, [
                "--mark-shown", "recall", epoch, _json.dumps(shown),
            ])

    return out


@router.post("/analyze")
async def analyze_code(request: AnalyzeRequest) -> AnalyzeResponse | dict[str, Any]:
    """Analyze code against retrieved rules. Returns structured compliance verdict."""
    if server._pipeline is None or server._llm_client is None or server._instrumentation is None:
        return {"error": "Pipeline not initialized. Run writ serve."}
    # The project of the FILE UNDER ANALYSIS, not of whoever called: /analyze is the
    # one retrieval route whose subject is a path, so the scope is derived here rather
    # than added to AnalyzeRequest as a field a caller could forget or misreport. The
    # file's directory is what the registry matches (a repo_root prefix), never the
    # file itself.
    project = await resolve_caller_project(os.path.dirname(request.file_path or ""))
    return await run_analysis(
        code=request.code,
        file_path=request.file_path,
        phase=request.phase,
        context=request.context,
        pipeline=server._pipeline,
        llm_client=server._llm_client,
        instrumentation=server._instrumentation,
        project=project or None,
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


def _refresh_ranking_inputs(rows: list[dict]) -> None:
    """Copy the post-write feedback state the graph write returned into the served pipeline.

    Program item 2: feedback used to reach the graph only, so learned confidence ranked on
    the values loaded at startup until a restart. The rows come from the write itself
    (rule_store state_out), so this costs no extra graph read. Never raises: the write has
    committed, so a failure here must not change the route's answer; the served counts
    stay stale until the next reload, and the exception row says so.
    """
    pipeline = server._pipeline
    if pipeline is None or not rows:
        return
    try:
        pipeline.apply_feedback(rows)
        if server._reloader is not None:
            # A rebuild already reading the graph may have read it before this write;
            # queue one more so the generation it swaps in cannot carry older counts.
            server._reloader.request_soon()
    except Exception as exc:
        emit_exception(
            "server.feedback.refresh", exc, "", None,
            rule_ids=[str(r.get("rule_id")) for r in rows][:20],
        )


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
    state: dict[str, Any] = {}
    graduation_pending = await server._db.evaluate_and_flip_graduation(
        request.rule_id, state_out=state,
    )
    _refresh_ranking_inputs([state] if state else [])
    return {
        "rule_id": request.rule_id, "signal": request.signal, "recorded": True,
        "graduation_pending": graduation_pending == "graduation_pending",
    }


@router.post("/feedback/batch")
async def record_feedback_batch(request: FeedbackBatchRequest) -> dict[str, Any]:
    """Record a batch of feedback signals in one db transaction (SessionEnd).

    The batched form of /feedback: every signal is applied atomically, and graduation
    is evaluated for the touched rules inside the same transaction. /feedback stays for
    single-signal callers and old clients.
    """
    if not request.signals:
        return {"recorded": [], "not_found": [], "graduation_pending": [], "applied": 0}
    if server._db is None:
        return {"error": "Database not connected."}
    state: list[dict] = []
    result = await server._db.apply_feedback_batch(
        [(item.rule_id, item.signal) for item in request.signals],
        batch_id=request.batch_id,
        state_out=state,
    )
    # Empty on a replay: the stored answer is returned and nothing new committed.
    _refresh_ranking_inputs(state)
    return result


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


def _log_destinations() -> dict[str, str]:
    """The files THIS daemon's rows actually land in, keyed as /health reports them.

    ONE copy, spread into BOTH /health returns. A two-branch payload is exactly where
    the next copy would drift, and a reporter that was fixed for one caller and left
    wrong for another is the defect this cycle exists to close.

    Both values come from `writ.shared.logging.emit_destination`, the same function
    `emit` uses to choose a file, so the report cannot drift from the write. With
    `WRIT_FRICTION_LOG` set the two values are EQUAL and equal to that variable, which
    is the truth (every stream collapses into it) and is also the alignment value
    `tests/_daemon.py` and `scripts/lib/writ-server-lib.sh` compare against.

    `audit_log` is a SECOND field rather than a rename because `write_attempt`,
    `gate_decision` and every other gate verdict are AUDIT-classified by design
    (STREAM_MAP), so "the friction log" is the wrong place to send the reader who is
    looking for a write decision. That reader is why this exists: with the variable
    unset, this endpoint used to publish `resolve_log_path()`'s bare cwd-relative
    `workflow-friction.log`, a file this repo last wrote to on 2026-07-01, while all
    780 `write_attempt` rows sat in `audit.jsonl`.
    """
    return {
        "audit_log": str(emit_destination("audit")),
        "friction_log": str(emit_destination("friction")),
    }


def _retrieval_status() -> dict[str, Any]:
    """Which retrieval generation this daemon serves and how its last reload went.

    Reported on BOTH /health branches, for the reason tcp_readonly is: "did my reload
    land" has an answer even on a daemon that is not ready.
    """
    handle = server._retrieval
    reloader = server._reloader
    last = reloader.last_outcome if reloader is not None else None
    return {
        "generation": handle.generation if handle is not None else None,
        "built_at": handle.built_at if handle is not None else None,
        "reloading": reloader.in_progress if reloader is not None else False,
        "last_reload": last.as_dict() if last is not None else None,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    """Service status, rule count, index state, last ingestion timestamp.

    Includes `cache_dir` (FIX-2): the session-cache directory this daemon resolved.
    ensure-server.sh compares it against the caller's expected dir to detect and realign
    a server-cache desync (a daemon started under a divergent TMPDIR).

    Includes `audit_log` and `friction_log` (cycle S): the files a row of each stream
    would actually land in, from `writ.shared.logging.emit_destination`. `audit_log` is
    where every gate decision goes. `friction_log` is unchanged for its existing
    readers, which compare it against their own `WRIT_FRICTION_LOG`, and is no longer
    the friction-log RESOLVER's answer, which with that variable unset is a bare
    cwd-relative `workflow-friction.log` that nothing writes to.

    Includes `retrieval` (program item 2): the served generation, when it was built,
    whether a reload is running, and the last reload's outcome.
    """
    from writ.server.transport import tcp_readonly_enabled

    cache_dir = getattr(server.writ_session, "CACHE_DIR", None) if server.writ_session else None
    if server._db is None:
        # tcp_readonly is reported on THIS branch too. The enforcement state does not
        # depend on the graph being up, and a doctor asking a not-ready daemon still
        # needs a true answer about which transports can write to it.
        return {
            "status": "not_ready",
            "error": "Database not connected.",
            "cache_dir": cache_dir,
            "tcp_readonly": tcp_readonly_enabled(),
            "retrieval": _retrieval_status(),
            # The log destinations are reported on THIS branch too, for the same reason
            # tcp_readonly is: where this daemon's rows land does not depend on the
            # graph being up, and "where are the gate decisions" is exactly the question
            # asked of a daemon that is not answering. It also stops the two alignment
            # readers seeing a not_ready daemon as PERMANENTLY diverged: they compare
            # /health's friction_log against their own WRIT_FRICTION_LOG, and a missing
            # key read as None, which never equals a set variable.
            **_log_destinations(),
        }

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
        # Where this daemon's rows actually land, one field per stream an operator asks
        # about. See _log_destinations: both come from the router's own destination
        # function, so the report cannot drift from the write, and `friction_log` keeps
        # its name because two readers compare it against their own WRIT_FRICTION_LOG.
        **_log_destinations(),
        # Whether THIS daemon bounds its TCP port to reads plus the named POST reads.
        # Reported for the same reason cache_dir is: the caller cannot know it. The
        # flag lives in the service's environment (a systemd drop-in), so `writ
        # doctor` read its OWN environment and printed "TCP still serves every route"
        # at a daemon that was returning 403 to every TCP write.
        "tcp_readonly": tcp_readonly_enabled(),
        "retrieval": _retrieval_status(),
    }
    if status == "degraded":
        payload["warning"] = (
            "index is warm but Neo4j reports 0 rules -- the daemon's graph DB and retrieval "
            "index disagree (DB/index split). Re-ingest, or restart against the correct Neo4j."
        )
    return payload


@router.post("/retrieval/reload")
async def retrieval_reload() -> dict[str, Any]:
    """Rebuild retrieval from the graph and swap it in when the content changed (unix socket only).

    Coalesced with any reload already running; the answer comes from a rebuild that read
    the graph after this request arrived. A failed rebuild keeps the previous generation
    serving and answers status "failed" with an `error`. TCP is refused before this
    handler runs (writ/server/transport.py SOCKET_ONLY_PATHS).
    """
    if server._reloader is None:
        return {"status": "failed", "error": "Retrieval not initialized. Run writ serve."}
    outcome = await server._reloader.request()
    return outcome.as_dict()


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
               r.trigger_keywords AS trigger_keywords,
               r.deliberate AS deliberate, r.last_verified AS last_verified,
               r.verify_interval_days AS verify_interval_days
        ORDER BY r.severity DESC, r.rule_id
    """
    async with server._db._driver.session(database=server._db._database) as session:
        result = await session.run(query)
        rows = [record.data() async for record in result]
    today = date.today()
    for r in rows:
        r["stale"] = is_verify_stale(r.pop("last_verified"), r.pop("verify_interval_days"), today)

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
            "stale": bool(r.get("stale")),
            "deliberate": bool(r.get("deliberate")),
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
    """Return a SubagentRole node's canonical prompt template and its declared `write_scope` from the graph (`write_scope` is null when the role declares none and `[]` when it declares it writes nothing; the two are not coalesced). Read once per dispatch by the sub-agent seeder.

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
        "effort_preference": rec["effort_preference"],
        "dispatched_by": rec["dispatched_by"] or [],
        # NOT `or []` like dispatched_by above: absence and emptiness are different
        # answers here. None means the role declares no write scope, which the write gate
        # reads as "keep today's decision"; [] means the role declares it writes nothing,
        # which refuses every path. Coalescing would turn every undeclared role into a
        # role that may write nothing, i.e. deny every sub-agent write in the population.
        "write_scope": rec["write_scope"],
    }


@router.post("/subagent/start-context")
async def subagent_start_context(request: SubagentStartContextRequest) -> dict[str, Any]:
    """Everything writ-subagent-start.sh needs from the daemon, in one request.

    Replaces /health, /query, /session/format and GET /subagent-role/{role} on the
    hook's healthy path: the retrieval (through the query_rules handler, so project
    resolution and the retrieval_result row are identical), the formatted text and
    injected ids (through the same formatter /session/format uses), the raw query's
    rule ids for the rules-injected row, and the role's declared write scope, with
    null and [] kept distinct as /subagent-role does.

    The parent's mode, phase and gates are deliberately NOT returned: the hook reads
    the parent cache file-direct because the daemon's view can diverge and answer
    mode=None. Each part fails open on its own and every key is always present, so
    the hook can tell "retrieval down" from "role lookup down".
    """
    body: dict[str, Any] = {
        "retrieval": "unavailable", "text": "", "rule_ids": [], "query_rule_ids": [],
        "rule_count": 0, "role_lookup": "skipped", "write_scope": None,
    }
    if server._pipeline is not None:
        try:
            result = await query_rules(QueryRequest(
                query=request.query[:500], budget_tokens=request.budget_tokens,
                exclude_rule_ids=[], project_root=request.project_root,
            ))
            if result.get("error"):
                body["retrieval"] = "error"
            else:
                rules = result.get("rules")
                rules = rules if isinstance(rules, list) else []
                formatted = await asyncio.to_thread(_format_query_response, result)
                body.update({
                    "retrieval": "ok", "text": formatted["text"],
                    "rule_ids": formatted["meta"]["rule_ids"],
                    "query_rule_ids": [
                        r.get("rule_id", "") if isinstance(r, dict) else "" for r in rules
                    ],
                    "rule_count": len(rules),
                })
        except Exception as exc:
            emit_exception("server.subagent_start_context.retrieval", exc, "", None)
            body["retrieval"] = "error"

    if request.role:
        if server._db is None:
            body["role_lookup"] = "unavailable"
        else:
            try:
                rec = await server._db.get_subagent_role(request.role)
            except Exception as exc:
                emit_exception("server.subagent_start_context.role", exc, "", None)
                rec = None
                body["role_lookup"] = "unavailable"
            else:
                if rec is None:
                    body["role_lookup"] = "not_found"
                else:
                    body["role_lookup"] = "ok"
                    scope = rec.get("write_scope")
                    body["write_scope"] = list(scope) if isinstance(scope, list) else None
    return body
