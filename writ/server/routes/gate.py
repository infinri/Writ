# writ-auth-scan: internal-service
"""Human-gate routes: phase advance, candidate promotion, pre-write check.

3 routes: /session/{id}/advance-phase, /session/{id}/promote-candidate,
/pre-write-check.

The gate-token functions (read/valid/claim/consume), writ_session,
log_friction_event, capture_decision_at_approve, _db, _pipeline, _BIBLE_DIR and
_run_cmd_format_locked are read via live `server.<attr>` access inside handler
bodies (the monkeypatch seam). The pure by-value helpers are imported from
their origin modules.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter

import writ.server as server
from writ.server.models import (
    PreWriteCheckRequest,
    SessionAdvancePhaseRequest,
    SessionPromoteCandidateRequest,
)
from writ.session.approval_workflow import (
    _BINDING_REFUSAL_REASONS,
    _GATE_VALIDATORS,
    apply_phase_advance,
)
from writ.session.locators import _find_plan_md, resolve_project_root
from writ.session.mode_engine import MODE_CONFIG, _next_pending_gate

router = APIRouter()


@router.post("/session/{session_id}/advance-phase")
async def session_advance_phase(
    session_id: str, request: SessionAdvancePhaseRequest | None = None
) -> dict[str, Any]:
    """Advance to the next workflow phase.

    Phase 3 addition: body.confirmation_source explicitly names how the user
    authorized the advance. Values: "tool" (/writ-approve or writ_approve MCP),
    "pattern" (string-match on "approved"), "explicit" (direct endpoint call).
    Recorded to session.phase_transitions for audit; emitted as friction-log
    event so Phase 5 can tally by source.

    G1 (concurrency): the gate token is CLAIMED atomically (claim_gate_token) at
    the point a real advance is pending, so two concurrent requests carrying the
    SAME valid token cannot both advance -- exactly one wins the claim and runs the
    advance + side effects; the loser returns a no-op. The claim REPLACES the old
    post-advance consume_gate_token (claiming IS consuming) and is the mutual-
    exclusion primitive. target_gate is computed from the single pre-claim cache
    read and NEVER recomputed after the claim (a fresh post-claim read would let
    one token advance the NEXT gate -- see project_advance_phase_token_race).
    """
    req = request or SessionAdvancePhaseRequest()
    source = req.confirmation_source
    if source not in ("tool", "pattern", "explicit"):
        return {"error": f"Invalid confirmation_source: {source}"}

    # (1) Non-destructive token presence/match check (fail-fast). auto-approve-gate.sh
    # writes /tmp/writ-gate-token-<sid> ONLY when the user's prompt matches an approval
    # pattern -- input the agent cannot forge -- so requiring the token is what makes the
    # human the approver. This check must NOT claim: a clearly-invalid token cannot be
    # allowed to consume the real one.
    token = req.token
    expected_token = await asyncio.to_thread(server.read_gate_token, session_id)
    if not server.gate_token_valid(token, expected_token):
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=None,
            event="agent_self_approval_blocked",
            had_token=bool(token),
            had_expected=bool(expected_token),
            confirmation_source=source,
        )
        return {
            "advanced": False,
            "error": (
                "Invalid or missing gate token. A phase advance requires the token the "
                "approval hook writes on genuine user approval; the agent cannot advance "
                "its own gate."
            ),
        }

    # Ordered resolution (explicit > marker-at-or-above-cwd > cwd). req.cwd comes from
    # the approval hook's payload; the daemon's own cwd is never a candidate.
    project_root, root_tier = resolve_project_root(explicit=req.project_root, start=req.cwd)
    # Read the session cache ONCE; the gate validation, the pending-gate decision,
    # and apply_phase_advance all derive from this single read.
    cache = await asyncio.to_thread(server.writ_session._read_cache, session_id)

    # (2) Terminal guard + pending-gate decision, from the SAME pre-claim read. This
    # runs BEFORE validation so a no-op advance (no gate pending) validates nothing and
    # spends nothing -- and so validation can dispatch on the TARGET gate.
    old_phase = cache.get("current_phase", "planning")
    if old_phase == "complete":
        return {
            "error": (
                "Session phase is `complete`. Reset via "
                "`writ-session.py mode set work <sid>` before advancing "
                "into a new task."
            ),
            "phase": "complete",
            "from": "complete",
            "confirmation_source": source,
        }

    mode = cache.get("mode")
    target_gate = _next_pending_gate(cache)
    if target_gate is None:
        # No pending gate / non-work mode: a no-op MUST NOT claim/consume the token
        # or run any side effect.
        return {
            "advanced": False,
            "reason": "No pending gate to advance",
            "phase": old_phase,
            "from": old_phase,
            "confirmation_source": source,
        }

    # (3) Validate the TARGET gate's artifact via the shared registry -- the same
    # gate->validator map the CLI dispatches. The route used to inline a phase-a-only
    # check, so `test-skeletons` advanced on this path (the one the approval hook and
    # /writ-approve both use) without ever checking that a test skeleton exists: the
    # second gate was a rubber stamp in production while its validator ran only in the
    # CLI. Dispatching the registry here makes both paths enforce the same artifacts.
    validator = _GATE_VALIDATORS.get(target_gate)
    if validator is not None:
        if not project_root:
            # Fail closed: without a root there is no artifact to judge. The token is
            # NOT spent -- an unresolvable root is an infrastructure failure, not a
            # rejected artifact, and spending here would burn the human's approval for
            # something they cannot fix by editing anything. (Spend-on-rejection below
            # still holds: a judged-and-failed artifact needs a fresh approval.)
            await asyncio.to_thread(
                server.log_friction_event,
                session_id=session_id,
                mode=mode,
                event="gate_root_unresolved",
                gate=target_gate,
                had_explicit_root=bool(req.project_root),
                had_cwd=bool(req.cwd),
            )
            return {
                "advanced": False,
                "error": (
                    f"Cannot validate the {target_gate} gate: no project root. The "
                    "caller sent neither a project_root nor a cwd to resolve one from. "
                    "Re-approve from the project directory, or pass project_root "
                    "explicitly. Your approval was NOT consumed."
                ),
                "gate": target_gate,
                "token_spent": False,
                "root_tier": root_tier,
            }
        gate_error = await asyncio.to_thread(validator, project_root, session_id)
        if gate_error:
            # A judged-and-failed artifact SPENDS the approval (no reuse: a changed
            # artifact needs a fresh approval). consume_gate_token (not the claim) is
            # correct here: concurrent rejections both remove + both reject.
            await asyncio.to_thread(server.consume_gate_token, session_id)
            return {
                "advanced": False,
                "error": gate_error,
                "gate": target_gate,
                "token_spent": True,
                "project_root": project_root,
                "root_tier": root_tier,
            }

    # (4) A real advance is pending -> atomically CLAIM the token. Exactly one
    # concurrent same-token caller wins; the loser returns a no-op and runs NO
    # side effects. The claim consumes the token (replaces the old post-advance
    # consume_gate_token).
    #
    # The claim also enforces what the approval authorizes: the gate named on line 2 of
    # the token file must be the gate being advanced, and the plan fingerprint on line 3
    # must still match. Both sides derive the fingerprint from the cache's project_root,
    # the same input cmd_current_phase hands the approval hook at mint time, so the mint
    # and the claim hash the same file by construction.
    #
    # STRICT, WITH NO FALLBACK. Every claim on this route goes through
    # claim_gate_token; a token file carrying no binding (the pre-cycle one-line format)
    # is REFUSED here exactly as the CLI refuses it. The `if binding is None: use the
    # bare mutex` branch that used to sit here read as a kindness to a session whose
    # token predated the upgrade, but its real effect was a fail-open branch on the
    # decision of whether a human approved an action: anything that could put a single
    # line into /tmp/writ-gate-token-<sid> got an UNBOUND claim through the route while
    # the same file was refused at the CLI. The production hook always mints three
    # lines, so nothing legitimate ever took that branch -- which is exactly why it
    # could be deleted rather than deprecated. _claim_token_mutex stays what it has
    # always been: the internal mutual-exclusion primitive claim_gate_token is built on,
    # never a route-level substitute for it.
    #
    # Both calls below read files (plan.md, the token file), so they go to a thread like
    # every other blocking call on this route.
    plan_hash = await asyncio.to_thread(server.plan_md_hash, cache.get("project_root")) or ""
    refusal = await asyncio.to_thread(
        server.gate_binding_refusal, session_id, gate=target_gate, plan_hash=plan_hash
    )
    if refusal:
        # Fail closed and say WHY: the previous behavior of every refusal on this
        # route was a message the user read as "no gate pending". The token is NOT
        # spent (nothing was claimed), so the approval it does authorize survives.
        # The reason text comes from the CLI's table so the two gate paths cannot
        # drift into telling the user two different things about the same refusal.
        binding = await asyncio.to_thread(server.read_gate_binding, session_id)
        bound_gate = binding[0] if binding else ""
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=mode,
            event=refusal,
            gate=target_gate,
            bound_gate=bound_gate,
        )
        return {
            "advanced": False,
            "error": (
                _BINDING_REFUSAL_REASONS[refusal].format(
                    bound=bound_gate, target=target_gate,
                )
                + " Your approval was NOT consumed."
            ),
            "gate": target_gate,
            "token_spent": False,
        }
    claimed = await asyncio.to_thread(
        server.claim_gate_token, session_id, token,
        gate=target_gate, plan_hash=plan_hash,
    )
    if not claimed:
        return {
            "advanced": False,
            "reason": (
                "Gate token already consumed by a concurrent approval; this "
                "duplicate advance is a no-op."
            ),
            "phase": old_phase,
            "from": old_phase,
            "confirmation_source": source,
        }

    # (5) Claim winner only: derive the target phase + artifacts from the pre-claim
    # read, apply the advance idempotently under mutate_cache, then run side effects
    # exactly once.
    new_phase = MODE_CONFIG[mode]["phase_after_gate"][target_gate]
    artifacts = []
    validated_path = ""
    # `project_root and` guards the empty-root case: _find_plan_md("") resolves its
    # relative candidates against the DAEMON's cwd (Writ's own install dir, which has a
    # plan.md), so an unguarded call would name Writ's plan as the approved artifact.
    if target_gate == "phase-a" and project_root:
        plan_path = _find_plan_md(project_root)
        if plan_path:
            validated_path = plan_path
            artifacts.append(os.path.relpath(plan_path, project_root))

    def _apply() -> None:
        with server.writ_session.mutate_cache(session_id) as locked_cache:
            apply_phase_advance(
                locked_cache, target_gate, old_phase, new_phase,
                trigger="user-approved", mode=mode,
                confirmation_source=source, artifacts_validated=artifacts,
            )

    await asyncio.to_thread(_apply)
    # project_root/root_tier/validated travel back so the approval hook can TELL the
    # user which project and which plan.md the approval just accepted. A root resolved
    # from a stray marker file above the work directory (a leftover pyproject.toml, say)
    # is otherwise invisible: the gate would stamp an unrelated plan and say only
    # "approved". Naming the artifact is what makes a wrong root noticeable.
    result = {
        "from": old_phase,
        "phase": new_phase,
        "confirmation_source": source,
        "project_root": project_root,
        "root_tier": root_tier,
        "validated": validated_path,
    }

    # Phase 5 telemetry: one friction event per phase advance. Routed through the
    # shared env-aware writer (honors WRIT_FRICTION_LOG), same as the
    # playbook_step_complete emit below.
    await asyncio.to_thread(
        server.log_friction_event,
        session_id=session_id,
        mode=None,
        event="phase_advance",
        from_phase=result["from"],
        to_phase=result["phase"],
        confirmation_source=source,
    )

    # Decision capture (Phase 1c, deliverable 4): on a planning-gate approval,
    # snapshot plan.md into a Decision. FAIL-OPEN: the advance is already durably
    # committed and the token claimed, so a capture failure (including _db down)
    # is caught + logged and never blocks the advance. _db is None skips capture.
    if server._db is not None and project_root and result["from"] == "planning":
        try:
            await server.capture_decision_at_approve(
                server._db, project_root, session_id, phase=result["from"]
            )
        except Exception as exc:
            await asyncio.to_thread(
                server.log_friction_event,
                session_id=session_id,
                mode=None,
                event="decision_capture_failed",
                error=str(exc),
            )

    # Phase 6i: emit a parallel playbook_step_complete event so
    # Phase 5's --playbook-compliance analyzer has signal. The SDD
    # playbook has three operational steps (planning, testing,
    # implementation); "complete" is the terminal state, not a step,
    # so it does not emit.
    _PHASE_ORDER = ["planning", "testing", "implementation"]
    try:
        step_index = _PHASE_ORDER.index(result["phase"])
    except ValueError:
        step_index = -1
    if step_index >= 0:
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=None,
            event="playbook_step_complete",
            playbook_id="PBK-PROC-SDD-001",
            step_id=result["phase"],
            step_index=step_index,
            total_steps=len(_PHASE_ORDER),
        )

    # Return `result` whole rather than re-projecting two keys: the re-projection dropped
    # `from` (which every other return path here includes) and would drop the
    # project_root/root_tier/validated fields the approval hook reports to the user.
    # Purely additive to the response contract; no existing key changes.
    return result


@router.post("/session/{session_id}/promote-candidate")
async def session_promote_candidate(
    session_id: str, request: SessionPromoteCandidateRequest | None = None
) -> dict[str, Any]:
    """6.3c: human-gated, edit-capable promotion of a graduation_pending candidate to canon.

    SECURITY: like /advance-phase, this REQUIRES the agent-unforgeable gate token that
    auto-approve-gate.sh writes only on a genuine user approval prompt. No token => no canon
    write (the candidate stays graduation_pending, no bible/ file). This is the one seam
    where Writ could otherwise write its own memory unsupervised, so the token gate is what
    keeps the human the approver. Body: {candidate_id, token, edited_fields?}. The human may
    approve as-is or supply edited_fields (edit-at-gate); the (edited) text re-runs the
    structural gate before export. Token is consumed on a successful promotion.
    """
    if server._db is None or server._pipeline is None:
        return {"promoted": False, "error": "Database/pipeline not connected."}
    req = request or SessionPromoteCandidateRequest()
    candidate_id = req.candidate_id
    if not candidate_id:
        return {"promoted": False, "error": "candidate_id is required."}

    token = req.token
    expected_token = await asyncio.to_thread(server.read_gate_token, session_id)
    if not server.gate_token_valid(token, expected_token):
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=None,
            event="agent_self_approval_blocked",
            event_target="promote_candidate",
            candidate_id=candidate_id,
            had_token=bool(token),
            had_expected=bool(expected_token),
        )
        return {
            "promoted": False,
            "error": (
                "Invalid or missing gate token. Promoting a candidate to canon requires "
                "the token the approval hook writes on genuine user approval; the agent "
                "cannot promote its own proposal."
            ),
        }

    # The one deliberate narrowing of cycle 1: this route used to accept the very same
    # token a phase advance accepts, so an "approved" typed at a plan.md could be spent
    # writing a decision-memory candidate into the canon. That is a cross-ACTION
    # confusion, not merely a cross-gate one. A token whose line 2 names a gate was
    # minted while that gate was pending and belongs to it; promotion requires a token
    # minted with no phase gate pending, which is the state this route is actually used
    # in. A pre-binding token (no line 2 at all) is accepted as before.
    binding = await asyncio.to_thread(server.read_gate_binding, session_id)
    if binding is not None and binding[0]:
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=None,
            event="candidate_promotion_gate_bound",
            candidate_id=candidate_id,
            bound_gate=binding[0],
        )
        return {
            "promoted": False,
            "error": (
                f"That approval is bound to the {binding[0]} gate, so it cannot promote a "
                "candidate to canon. Approve the promotion on its own turn, with no phase "
                "gate pending."
            ),
        }

    from writ.promotion import promote_candidate
    result = await promote_candidate(
        candidate_id, server._pipeline, server._db, server._BIBLE_DIR,
        edited_fields=req.edited_fields,
    )

    # Consume the token only on a successful canon write: one user approval authorizes
    # exactly one promotion. A rejected edit (gate failure) leaves the token for a retry.
    if result.get("promoted"):
        await asyncio.to_thread(server.consume_gate_token, session_id)
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=session_id,
            mode=None,
            event="candidate_promoted",
            candidate_id=candidate_id,
            graduated_via=result.get("graduated_via"),
        )
    return result


@router.post("/pre-write-check")
async def pre_write_check(request: PreWriteCheckRequest) -> dict[str, Any]:
    """Combined gate check + final-gate check + RAG query for Write/Edit.

    Returns {"decision": "allow"|"deny"|"ask", "reason": "...", "rag_rules": "...",
             "rag_meta": {"rule_ids": [...], "tokens": N}}.
    """

    def _check() -> dict[str, Any]:
        session_id = request.session_id
        envelope = {"tool_input": request.tool_input}
        skill_dir = request.skill_dir

        # A10: read the session cache ONCE for the whole request and reuse it for
        # the gate check, the denial-count escalation, and the RAG branch (was up
        # to 3 _read_cache calls of the same file per Write/Edit). _can_write_check
        # mutates this same dict in place on a gate denial (_log_gate_denial bumps
        # denial_counts and persists), so the post-check reads below see the fresh
        # count -- equivalent to the old re-read.
        cache = server.writ_session._read_cache(session_id)
        mode = cache.get("mode")

        # 1. Gate approval check
        gate_result = server.writ_session._can_write_check(session_id, envelope, skill_dir, cache=cache)
        if not gate_result["can_write"]:
            # Check denial count for escalation (cache reflects the just-applied bump)
            denial_counts = cache.get("denial_counts", {})
            max_count = max(denial_counts.values()) if denial_counts else 0
            decision = "ask" if max_count >= 2 else "deny"
            return {
                "decision": decision,
                "reason": gate_result["reason"],
                "rag_rules": "",
                "rag_meta": {"rule_ids": [], "tokens": 0},
                # A11: mode + denial count travel in the envelope so the dispatch
                # hook drops its separate `mode get` and denial-count daemon reads.
                "mode": mode,
                "max_denial_count": max_count,
            }

        # 2. RAG query (if pipeline available)
        file_path = request.file_path or request.tool_input.get("file_path", "")
        rag_rules = ""
        rag_meta: dict[str, Any] = {"rule_ids": [], "tokens": 0}
        if server._pipeline is not None and file_path:
            try:
                by_phase = cache.get("loaded_rule_ids_by_phase", {})
                current_phase = cache.get("current_phase", "")
                if by_phase and current_phase:
                    exclude_ids = by_phase.get(current_phase, [])
                else:
                    exclude_ids = cache.get("loaded_rule_ids", [])
                remaining_budget = cache.get("remaining_budget", server.writ_session.DEFAULT_SESSION_BUDGET)
                max_budget = min(remaining_budget, 1500)
                if max_budget >= 200:
                    # Build query from file path
                    import re
                    basename = os.path.basename(file_path)
                    name_no_ext = os.path.splitext(basename)[0]
                    ext = os.path.splitext(file_path)[1]
                    ext_map = {
                        '.py': 'python', '.php': 'php', '.js': 'javascript',
                        '.ts': 'typescript', '.go': 'go', '.rs': 'rust',
                    }
                    lang = ext_map.get(ext, '')
                    words = re.findall(r'[A-Z][a-z]+|[a-z]+', name_no_ext)
                    query_parts = [lang] + [w.lower() for w in words if len(w) > 3]
                    query_text = ' '.join(query_parts[:15])
                    if len(query_text) >= 5:
                        result = server._pipeline.query(
                            query_text=query_text,
                            budget_tokens=max_budget,
                            exclude_rule_ids=exclude_ids,
                            prefer_rule_ids=request.prefer_rule_ids,
                        )
                        rules = result.get("rules", [])
                        if rules:
                            from writ.retrieval.prompt_bundle import split_format
                            formatted = server._run_cmd_format_locked(result)
                            rag_rules, _m = split_format(formatted)
                            rag_meta = {"rule_ids": _m["rule_ids"], "tokens": _m["cost"]}
            except Exception as exc:
                # Fail-open (RAG is advisory) but no longer silent: match the
                # sibling fail-open branches in session_advance_phase, commit_capture,
                # recall, and git_hooks_auto_install. _check runs inside
                # asyncio.to_thread, so log synchronously (already off loop).
                server.log_friction_event(
                    session_id=session_id,
                    mode=mode,
                    event="pre_write_rag_failed",
                    error=str(exc),
                )

        return {
            "decision": "allow",
            "reason": None,
            "rag_rules": rag_rules,
            "rag_meta": rag_meta,
            # A11: mode for the dispatch hook's telemetry (no separate `mode get`).
            "mode": mode,
            "max_denial_count": max((cache.get("denial_counts") or {}).values(), default=0),
        }

    result = await asyncio.to_thread(_check)
    return result
