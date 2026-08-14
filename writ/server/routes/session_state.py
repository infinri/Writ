# writ-auth-scan: internal-service
"""Session-cache CRUD routes -- thin HTTP wrappers around writ-session.py.

24 routes: the /session/{session_id}/... CRUD family plus the no-id
/session/format and /session/{id}/can-write. Also the _mode_set_kwarg helper.

Per PY-ASYNC-001: async def with asyncio.to_thread() for file I/O.
Per PERF-IO-001: no sync I/O blocking the event loop.

writ_session, log_friction_event and _run_cmd_format_locked are read via live
`server.<attr>` access inside handler bodies only (the monkeypatch seam).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter

import writ.server as server
from writ.server.models import (
    ContextPercentRequest,
    SessionActivePlaybookRequest,
    SessionAddViolationRequest,
    SessionAutoFeedbackRequest,
    SessionCanWriteRequest,
    SessionFormatRequest,
    SessionInvalidateGateRequest,
    SessionModeSetRequest,
    SessionQualityJudgmentRequest,
    SessionReviewFindingsRequest,
    SessionUpdateRequest,
    SessionVerificationEvidenceRequest,
)
from writ.session.locators import plan_md_hash
from writ.session.mode_engine import VALID_MODES, _next_pending_gate
from writ.shared.logging import emit

router = APIRouter()


@router.get("/session/{session_id}")
async def session_read(session_id: str) -> dict[str, Any]:
    """Read the full session cache."""
    data = await asyncio.to_thread(server.writ_session._read_cache, session_id)
    data["session_id"] = session_id
    return data


@router.post("/session/{session_id}/update")
async def session_update(session_id: str, request: SessionUpdateRequest) -> dict[str, Any]:
    """Update a single key in the session cache."""

    def _do_update() -> None:
        with server.writ_session.mutate_cache(session_id) as cache:
            cache[request.key] = request.value

    await asyncio.to_thread(_do_update)
    return {"ok": True}


@router.get("/session/{session_id}/should-skip")
async def session_should_skip(session_id: str) -> dict[str, Any]:
    """Check whether RAG queries should be skipped for this session.

    Delegates to cmd_should_skip (the single policy source, including the
    is_subagent never-skip exemption the old inline check missed). `known`
    tells the caller whether this daemon actually has a cache for the
    session: false means the boolean is a default, not an answer (divergent
    cache dir / stale daemon), and hooks should fall back to the local read.
    """

    def _check() -> tuple[bool, bool]:
        # _read_cache returns a defaults scaffold for unknown sessions, so file
        # existence, not dict truthiness, is the recognition signal.
        known = os.path.exists(server.writ_session._cache_path(session_id))
        return server.writ_session.cmd_should_skip(session_id), known

    result, known = await asyncio.to_thread(_check)
    return {"should_skip": result, "known": known}


@router.get("/session/{session_id}/prompt-state")
async def session_prompt_state(session_id: str) -> dict[str, Any]:
    """Everything the RAG hook asks about a session, in one call and one cache read.

    The hook used to ask three separate questions (should-skip, the full cache read, and
    check-escalation). Each cost a python interpreter start (9.5ms floor) plus an HTTP
    round trip plus its OWN read of the same cache file, to answer questions that are all
    functions of that one file. Measured 2026-08-07: 41.7ms of interpreter across the
    three, against ~10ms for a single round trip.

    THE FIELDS ARE THE EXISTING RESPONSES VERBATIM, deliberately. `should_skip`, `known`
    and `escalation` keep the exact names and semantics of /should-skip and
    /check-escalation, and `cache` is exactly what GET /session/{id} returns. Reshaping
    them here would make the aggregate and the individual routes disagree, and the
    individual routes stay in place for callers that want one answer.

    Consistency is a real gain on top of the latency: three separate reads can straddle a
    concurrent mutation and hand the hook a skip decision from one moment and an
    escalation flag from another. One read cannot.
    """

    def _collect() -> dict[str, Any]:
        # _read_cache returns a defaults scaffold for unknown sessions, so file existence,
        # not dict truthiness, is the recognition signal (see session_should_skip).
        known = os.path.exists(server.writ_session._cache_path(session_id))
        cache = server.writ_session._read_cache(session_id)
        cache["session_id"] = session_id
        esc = cache.get("escalation", {})
        return {
            # cmd_should_skip is the single policy source (it carries the is_subagent
            # never-skip exemption), so this calls it rather than re-deriving the rule.
            "should_skip": server.writ_session.cmd_should_skip(session_id),
            "known": known,
            "escalation": bool(esc.get("needed", False)) if isinstance(esc, dict) else False,
            "cache": cache,
        }

    return await asyncio.to_thread(_collect)


@router.get("/session/{session_id}/mode")
async def session_mode_get(session_id: str) -> dict[str, Any]:
    """Get the current mode for the session."""

    def _get() -> str:
        cache = server.writ_session._read_cache(session_id)
        return cache.get("mode", "") or ""

    mode = await asyncio.to_thread(_get)
    return {"mode": mode}


def _mode_set_kwarg(session_id: str, mode: str, orchestrator: bool) -> None:
    """Wrapper that passes orchestrator as a keyword arg.

    The mock writ_session._mode_set in tests has signature
    (session_id, mode, **kwargs). The real _mode_set in writ-session.py
    has signature (session_id, mode, is_orchestrator=False). Calling with
    a keyword arg satisfies both shapes.
    """
    server.writ_session._mode_set(session_id, mode, is_orchestrator=orchestrator)


@router.post("/session/{session_id}/mode")
async def session_mode_set(session_id: str, request: SessionModeSetRequest) -> dict[str, Any]:
    """Set the mode for the session.

    Routes through writ_session._mode_set so canonicalization (lowercase) and
    friction-log emission match the CLI path byte-for-byte. Invalid modes are
    rejected with HTTP 400 before reaching the cache.
    """
    from fastapi import HTTPException
    mode = (request.mode or "").lower()
    if mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {request.mode!r}. Must be one of: {', '.join(sorted(VALID_MODES))}.",
        )
    await asyncio.to_thread(_mode_set_kwarg, session_id, mode, request.orchestrator)
    return {"ok": True, "mode": mode}


@router.post("/session/{session_id}/can-write")
async def session_can_write(session_id: str, request: SessionCanWriteRequest | None = None) -> dict[str, Any]:
    """Check whether a file write is allowed.

    Runs the REAL gate (_can_write_check) -- the same logic /pre-write-check and the
    CLI use. The route previously returned True unconditionally in work mode, ignoring
    the test-skeleton / phase-a gates, which made it a silent write bypass (audit #2).
    """
    req = request or SessionCanWriteRequest()
    envelope = {"tool_input": req.tool_input}
    result = await asyncio.to_thread(
        server.writ_session._can_write_check, session_id, envelope, req.skill_dir
    )
    return {"can_write": result["can_write"], "reason": result.get("reason")}


@router.get("/session/{session_id}/current-phase")
async def session_current_phase(session_id: str) -> dict[str, Any]:
    """The phase, the mode, the next pending gate and the plan fingerprint.

    THE FOUR FIELDS ARE THE ONES THIS ROUTE'S CALLER ALREADY PARSES.
    hooks/scripts/auto-approve-gate.sh reads `.phase`, `.mode`, `.next_gate` and
    `.plan_hash` out of this reply, and the route used to return `{"phase": ...}`
    alone, so three of the four read empty and the hook fell back to inferring the
    gate from the phase (planning -> phase-a, testing -> test-skeletons,
    implementation -> nothing). A gate re-armed DURING implementation -- which is
    exactly what editing plan.md does, by design -- was therefore invisible to the
    only mechanism that can clear it: every write denied and the user's "approved"
    answered with "No approval gate was advanced". The CLI's cmd_current_phase
    (writ/session/approval_workflow.py) has reported all four all along; an endpoint
    returning a strict subset of what its only caller parses is the defect.

    `next_gate` is computed by mode_engine._next_pending_gate, the SAME function
    /advance-phase enforces with, so the approval path and the enforcement path
    cannot hold two derivations of one fact again. Do not re-derive it here: the
    plan-hash re-arm it performs is the binding of an approval to the plan it was
    granted against, and reporting a gate on any other basis would either hide a
    re-arm or manufacture a gate nobody is waiting on.

    null is a real answer for both nullable fields, not an error: no gate is pending
    (or the mode has no gates at all), and there is no plan.md under the session's
    project root. Neither computation raises on those inputs -- _next_pending_gate
    returns None for any non-work mode and plan_md_hash returns None for an empty
    root or an absent plan -- which matters because a hook calls this on every
    approval-shaped user prompt, so a 500 here would break the turn itself.
    """

    def _get() -> dict[str, Any]:
        # ONE cache read, and both derivations inside the SAME to_thread: each does
        # file I/O (_next_pending_gate fingerprints plan.md through plan_md_hash), so
        # computing either outside this thread would put a disk read on the event
        # loop (PY-ASYNC-001 / PERF-IO-001). One read also means the gate and the
        # fingerprint describe one moment, which is what the token binding needs.
        cache = server.writ_session._read_cache(session_id)
        return {
            "phase": cache.get("current_phase", "planning") or "planning",
            # "" rather than null for an unset mode, matching this file's own
            # /session/{id}/mode route; the hook coalesces either to empty anyway.
            "mode": cache.get("mode", "") or "",
            "next_gate": _next_pending_gate(cache),
            "plan_hash": plan_md_hash(cache.get("project_root")),
        }

    return await asyncio.to_thread(_get)


@router.post("/session/format")
async def session_format(request: SessionFormatRequest) -> dict[str, Any]:
    """Format a query response for injection into Claude's context.

    Returns {"text": "<formatted>", "meta": {"rule_ids": [...], "tokens": N}}.
    Replaces the subprocess fallthrough on the hook hot path; common.sh now
    routes "format" through this endpoint, keeping the subprocess as fallback
    only when the server is unreachable.
    """

    def _format() -> dict[str, Any]:
        from writ.retrieval.prompt_bundle import split_format

        # Two formatting backends, picked at runtime:
        # 1. If writ_session.cmd_format accepts a query_response arg and
        #    returns a string (test mock shape), use the return value.
        # 2. Otherwise call the production cmd_format() via _run_cmd_format_locked
        #    (the shared, lock-serialized stdin/stdout swap) and parse the
        #    WRIT_META: tail line.
        raw = None
        try:
            candidate = server.writ_session.cmd_format(query_response=request.query_response)  # type: ignore[call-arg]
        except TypeError:
            candidate = None
        if isinstance(candidate, str):
            raw = candidate
        else:
            raw = server._run_cmd_format_locked(request.query_response)

        raw = raw or ""
        text, _m = split_format(raw)
        meta: dict[str, Any] = {"rule_ids": _m["rule_ids"], "tokens": _m["cost"]}
        return {"text": text, "meta": meta}

    result = await asyncio.to_thread(_format)
    return result


@router.get("/session/{session_id}/coverage")
async def session_coverage(session_id: str) -> dict[str, Any]:
    """Get rule coverage for the session."""

    def _get() -> float:
        cache = server.writ_session._read_cache(session_id)
        loaded = cache.get("loaded_rule_ids", [])
        queries = cache.get("queries", 0)
        if queries == 0:
            return 0.0
        return len(loaded) / max(queries, 1)

    coverage = await asyncio.to_thread(_get)
    return {"coverage": coverage}


@router.get("/session/{session_id}/check-escalation")
async def session_check_escalation(session_id: str) -> dict[str, Any]:
    """Check whether escalation is needed."""

    def _check() -> bool:
        cache = server.writ_session._read_cache(session_id)
        esc = cache.get("escalation", {})
        return bool(esc.get("needed", False))

    result = await asyncio.to_thread(_check)
    return {"escalation": result}


@router.post("/session/{session_id}/auto-feedback")
async def session_auto_feedback(session_id: str, request: SessionAutoFeedbackRequest) -> dict[str, Any]:
    """Trigger auto-feedback correlation for the session.

    Runs the real correlation (cmd_auto_feedback): map loaded rules to written-file
    outcomes and POST per-rule positive/negative signals to /feedback. Previously a
    no-op stub, so the daemon-up session-end feedback loop never ran (audit #2).
    """
    await asyncio.to_thread(server.writ_session.cmd_auto_feedback, session_id)
    return {"ok": True}


@router.post("/session/{session_id}/clear-pending-violations")
async def session_clear_pending_violations(session_id: str) -> dict[str, Any]:
    """Clear pending violations for the session."""

    def _clear() -> None:
        with server.writ_session.mutate_cache(session_id) as cache:
            cache["pending_violations"] = []

    await asyncio.to_thread(_clear)
    return {"ok": True}


@router.post("/session/{session_id}/add-pending-violation")
async def session_add_pending_violation(
    session_id: str, request: SessionAddViolationRequest,
) -> dict[str, Any]:
    """Add a pending violation to the session."""

    def _add() -> None:
        with server.writ_session.mutate_cache(session_id) as cache:
            violations = cache.get("pending_violations", [])
            violations.append({
                "rule_id": request.rule_id,
                "detail": request.detail,
                "file": request.file,
                "line": request.line,
            })
            cache["pending_violations"] = violations

    await asyncio.to_thread(_add)
    return {"ok": True}


@router.post("/session/{session_id}/invalidate-gate")
async def session_invalidate_gate(
    session_id: str, request: SessionInvalidateGateRequest | None = None
) -> dict[str, Any]:
    """Invalidate a gate: record the cycle, delete the .approved file, check escalation.

    Wires the route to the real cmd_invalidate_gate (the same logic the
    `writ-session.py invalidate-gate` CLI runs) instead of the prior
    `setdefault` no-op, which recorded nothing and never escalated.
    """
    req = request or SessionInvalidateGateRequest()
    if not req.gate or not req.rule_id or not req.file:
        return {"ok": False, "error": "Required: gate, rule_id, file"}

    args = [req.gate, "--rule", req.rule_id, "--file", req.file]
    if req.evidence:
        args += ["--evidence", req.evidence]
    if req.trace:
        args += ["--trace", req.trace]
    if req.plan_hash:
        args += ["--plan-hash", req.plan_hash]
    if req.project_root:
        args += ["--project-root", req.project_root]

    try:
        await asyncio.to_thread(server.writ_session.cmd_invalidate_gate, session_id, args)
    except SystemExit as exc:
        # cmd_invalidate_gate exits 1 on bad args, 2 on cache error.
        return {"ok": False, "error": f"invalidate-gate failed (exit {exc.code})"}
    return {"ok": True}


@router.get("/session/{session_id}/pending-violations")
async def session_pending_violations(session_id: str) -> dict[str, Any]:
    """Get pending violations for the session."""

    def _get() -> list:
        cache = server.writ_session._read_cache(session_id)
        return cache.get("pending_violations", [])

    violations = await asyncio.to_thread(_get)
    return {"violations": violations}


@router.post("/session/{session_id}/context-percent")
async def session_context_percent(
    session_id: str, request: ContextPercentRequest,
) -> dict[str, Any]:
    """Set context_percent for the session.

    Called by the statusLine command, which sources the harness-native
    context_window.used_percentage. The update is a read-modify-write under
    to_thread so it is serialized against other cache writers.
    """

    def _set() -> dict[str, Any]:
        with server.writ_session.mutate_cache(session_id) as cache:
            cache["context_percent"] = int(request.context_percent)
            context_percent = cache["context_percent"]
        return {
            "ok": True,
            "context_percent": context_percent,
        }

    return await asyncio.to_thread(_set)


@router.post("/session/{session_id}/clear-rules-for-compaction")
async def session_clear_rules_for_compaction(session_id: str) -> dict[str, Any]:
    """Clear loaded_rules from cache before compaction (PreCompact)."""

    def _clear() -> dict[str, Any]:
        import io
        import contextlib
        import json as json_mod

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.writ_session.cmd_clear_rules_for_compaction(session_id)
        return json_mod.loads(buf.getvalue().strip())

    result = await asyncio.to_thread(_clear)
    return result


@router.post("/session/{session_id}/reset-after-compaction")
async def session_reset_after_compaction(session_id: str) -> dict[str, Any]:
    """Reset budget and clear phase exclusion list after compaction (PostCompact)."""

    def _reset() -> dict[str, Any]:
        import io
        import contextlib
        import json as json_mod

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.writ_session.cmd_reset_after_compaction(session_id)
        return json_mod.loads(buf.getvalue().strip())

    result = await asyncio.to_thread(_reset)
    return result


# --- Phase 1: session endpoints for playbook/verification/quality state (deliverable 6) ---


@router.get("/session/{session_id}/active-playbook")
async def session_active_playbook_get(session_id: str) -> dict[str, Any]:
    """Read the session's active playbook + phase + history."""

    def _read() -> dict[str, Any]:
        cache = server.writ_session._read_cache(session_id)
        return {
            "active_playbook": cache.get("active_playbook"),
            "active_phase": cache.get("active_phase"),
            "playbook_phase_history": cache.get("playbook_phase_history", []),
        }

    return await asyncio.to_thread(_read)


@router.post("/session/{session_id}/active-playbook")
async def session_active_playbook_set(
    session_id: str, request: SessionActivePlaybookRequest
) -> dict[str, Any]:
    """Set active playbook and phase. body: {playbook_id, phase_id, total_steps?}.

    Appends the prior (playbook, phase) pair to history for audit trail.
    Also emits a `playbook_step_complete` friction event so the Phase 5
    `--playbook-compliance` analyzer can score in-order vs skip-step
    sessions.
    """

    def _set() -> dict[str, Any]:
        with server.writ_session.mutate_cache(session_id) as cache:
            prev = (cache.get("active_playbook"), cache.get("active_phase"))
            history = list(cache.get("playbook_phase_history", []))
            if prev[0] is not None:
                history.append({"playbook": prev[0], "phase": prev[1],
                                "ts": datetime.now().isoformat()})
                cache["playbook_phase_history"] = history
            cache["active_playbook"] = request.playbook_id
            cache["active_phase"] = request.phase_id
            active_playbook = cache["active_playbook"]
            active_phase = cache["active_phase"]
        return {
            "ok": True,
            "active_playbook": active_playbook,
            "active_phase": active_phase,
            "_history_at_advance": history,
            "_prev_ts": history[-1]["ts"] if history else None,
            "_total_steps": request.total_steps,
        }

    result = await asyncio.to_thread(_set)

    # Phase 5 instrumentation: emit playbook_step_complete event.
    pb = result.get("active_playbook")
    step = result.get("active_phase")
    if pb and step:
        history = result.get("_history_at_advance") or []
        step_index = len(history)
        prev_ts = result.get("_prev_ts")
        elapsed_ms: int | None = None
        if prev_ts:
            try:
                prev_dt = datetime.fromisoformat(prev_ts)
                elapsed_ms = max(0, int((datetime.now() - prev_dt).total_seconds() * 1000))
            except ValueError:
                elapsed_ms = None
        total = result.get("_total_steps")
        server.log_friction_event(
            session_id=session_id,
            mode=None,
            event="playbook_step_complete",
            playbook_id=pb,
            step_id=step,
            step_index=step_index,
            total_steps=total,
            elapsed_ms_since_prev_step=elapsed_ms,
        )

    return {
        "ok": True,
        "active_playbook": result["active_playbook"],
        "active_phase": result["active_phase"],
    }


@router.post("/session/{session_id}/verification-evidence")
async def session_verification_evidence_set(
    session_id: str, request: SessionVerificationEvidenceRequest
) -> dict[str, Any]:
    """Record verification evidence for a completion claim.

    body: {todo_id: str, command: str, output_excerpt: str, exit_code: int}
    Gate 5 Tier 1 reads this to unblock TodoWrite completion claims.
    """

    def _set() -> dict[str, Any]:
        # Body-only validation runs BEFORE the lock so a rejected request never
        # rewrites the cache.
        todo_id = request.todo_id
        if not todo_id:
            return {"ok": False, "error": "todo_id required"}
        with server.writ_session.mutate_cache(session_id) as cache:
            evidence = dict(cache.get("verification_evidence") or {})
            evidence[todo_id] = {
                "command": request.command,
                "output_excerpt": request.output_excerpt,
                "exit_code": request.exit_code,
                "recorded_at": datetime.now().isoformat(),
            }
            cache["verification_evidence"] = evidence
        # Audit item F: this lived ONLY in the session cache, so the proof behind a
        # completion claim died with the cache -- and a claim whose evidence has evaporated
        # is indistinguishable from one that never had any. Mirrored to the durable audit
        # stream (365-day retention) AFTER the cache write succeeds, so a rejected or
        # failed write never leaves a record of evidence that was not stored.
        emit(
            "audit", "verification_evidence", session_id, None,
            todo_id=todo_id,
            command=request.command,
            exit_code=request.exit_code,
            output_excerpt=request.output_excerpt,
        )
        return {"ok": True, "todo_id": todo_id}

    return await asyncio.to_thread(_set)


@router.get("/session/{session_id}/verification-evidence")
async def session_verification_evidence_get(session_id: str, todo_id: str | None = None) -> dict[str, Any]:
    """Read verification evidence. Pass ?todo_id=X for a single entry, omit for all."""

    def _read() -> dict[str, Any]:
        cache = server.writ_session._read_cache(session_id)
        evidence = cache.get("verification_evidence") or {}
        if todo_id:
            return {"todo_id": todo_id, "evidence": evidence.get(todo_id)}
        return {"evidence": evidence}

    return await asyncio.to_thread(_read)


_REVIEW_FINDINGS_LIB = None


def _review_findings_lib():
    """The shared parser from bin/lib (not a package: no __init__.py).

    Deliberately the SAME module the SubagentStop hook and the Bash gate use, so
    the HTTP surface cannot develop its own idea of what counts as blocking.

    Loaded once and cached, matching how the server holds writ-session.py: re-exec'ing
    the file per request would put a synchronous disk read and module exec inside an
    async handler.
    """
    global _REVIEW_FINDINGS_LIB
    if _REVIEW_FINDINGS_LIB is None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[3] / "bin" / "lib" / "review_findings.py"
        spec = importlib.util.spec_from_file_location("writ_review_findings", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REVIEW_FINDINGS_LIB = module
    return _REVIEW_FINDINGS_LIB


@router.post("/session/{session_id}/review-findings")
async def session_review_findings_set(
    session_id: str, request: SessionReviewFindingsRequest
) -> dict[str, Any]:
    """Record a reviewer verdict for the session. The latest one wins.

    body: {message: str (the reviewer's final text, verbatim), agent_id: str}

    The message is stored parsed, not raw: `bin/lib/review_findings.py` extracts
    the verdict from prose-plus-fenced-JSON, and a message it cannot parse is
    recorded as unparseable and treated as blocking. Fixing the findings and
    re-running the reviewer records a clean verdict, which lifts the block.
    """
    lib = _review_findings_lib()

    def _set() -> dict[str, Any]:
        state = lib.record(session_id, request.message, request.agent_id)
        return {
            "ok": True,
            "blocking": lib.is_blocking(state["verdict"]),
            "verdict": state["verdict"],
        }

    return await asyncio.to_thread(_set)


@router.get("/session/{session_id}/review-findings")
async def session_review_findings_get(session_id: str) -> dict[str, Any]:
    """The latest recorded reviewer verdict and whether it blocks a commit."""
    lib = _review_findings_lib()

    def _read() -> dict[str, Any]:
        state = lib.read_state(session_id)
        verdict = (state or {}).get("verdict")
        return {
            "verdict": verdict,
            "blocking": lib.is_blocking(verdict),
            "reason": lib.describe(verdict) if lib.is_blocking(verdict) else "",
            "recorded_at": (state or {}).get("recorded_at", ""),
            "agent_id": (state or {}).get("agent_id", ""),
        }

    return await asyncio.to_thread(_read)


@router.post("/session/{session_id}/quality-judgment")
async def session_quality_judgment_set(
    session_id: str, request: SessionQualityJudgmentRequest
) -> dict[str, Any]:
    """Record a Gate 5 Tier 2 (self-scored) quality score for an artifact.

    body: {artifact_path: str, score: int (0-5), failing_section: str|None,
           rationale: str, overridden: bool, rubric: str|None}

    Also emits a `quality_judgment` friction event so the Phase 5
    `--quality-judge-false-positives` analyzer can compute per-rubric
    override rates.
    """
    import time as _time
    start_perf = _time.perf_counter()

    def _set() -> dict[str, Any]:
        # Body-only validation runs BEFORE the lock so a rejected request never
        # rewrites the cache.
        path = request.artifact_path
        if not path:
            return {"ok": False, "error": "artifact_path required"}
        score = request.score
        with server.writ_session.mutate_cache(session_id) as cache:
            judgments = dict(cache.get("quality_judgment_state") or {})
            judgments[path] = {
                "score": score,
                "failing_section": request.failing_section,
                "rationale": request.rationale,
                "overridden": request.overridden,
                "rubric": request.rubric,
                "recorded_at": datetime.now().isoformat(),
            }
            cache["quality_judgment_state"] = judgments
            if request.overridden:
                cache["quality_override_count"] = int(cache.get("quality_override_count", 0)) + 1
            override_count = cache.get("quality_override_count", 0)
            mode = cache.get("mode")
        return {
            "ok": True, "artifact_path": path, "score": score,
            "override_count": override_count,
            "_mode": mode,
        }

    result = await asyncio.to_thread(_set)
    if result.get("ok"):
        score = request.score
        decision = "pass" if score >= 3 else "fail"
        path = request.artifact_path or ""
        judgment_id = hashlib.md5(
            f"{path}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]
        # latency_ms records the time the judgment took to produce. Callers
        # whose judge ran out-of-process (Haiku, etc.) should pass their
        # measured value via body["latency_ms"]. When absent we fall back
        # to the recording-side latency (server-side cache write only --
        # not inference time, but a non-zero placeholder useful for
        # detecting endpoint-write regressions).
        body_latency = request.latency_ms
        if isinstance(body_latency, (int, float)) and body_latency >= 0:
            latency_ms = int(body_latency)
        else:
            latency_ms = max(0, int((_time.perf_counter() - start_perf) * 1000))
        server.log_friction_event(
            session_id=session_id,
            mode=result.get("_mode"),
            event="quality_judgment",
            judgment_id=judgment_id,
            rubric=request.rubric or "default",
            decision=decision,
            override=request.overridden,
            latency_ms=latency_ms,
            score=score,
            failing_section=request.failing_section,
        )
    # Strip private fields before returning to caller.
    return {k: v for k, v in result.items() if not k.startswith("_")}


@router.get("/session/{session_id}/quality-judgment")
async def session_quality_judgment_get(session_id: str) -> dict[str, Any]:
    """Read all quality judgments plus the override count for the session."""

    def _read() -> dict[str, Any]:
        cache = server.writ_session._read_cache(session_id)
        return {
            "judgments": cache.get("quality_judgment_state") or {},
            "override_count": cache.get("quality_override_count", 0),
        }

    return await asyncio.to_thread(_read)
