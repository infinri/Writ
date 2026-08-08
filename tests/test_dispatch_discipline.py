"""Fix B (Phase 3): the dispatch-discipline Task hook steers generic dispatches.

hooks/scripts/writ-dispatch-discipline.sh is a PreToolUse(Task) hook. In governed modes
(work / investigate / unset), a generic dispatch (subagent_type general-purpose / Explore /
claude / empty) with no escape marker is REWRITTEN via updatedInput to the Writ role that
matches the prompt -- so the model proceeds with the governed role directly, no deny/retry
(SKL-PROC-DISPATCH-001). A prompt that maps to no specific role falls back to deny+ask.
Named (writ-*) roles, escape-hatched prompts, and non-governed modes pass through untouched
-- the hook never blocks a dispatch spuriously (ERR-GRACEFUL-001).

Per TEST-REGRESSION-001: these assert the new behavior; they fail against the absent hook
and pass once it is wired.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "hooks",
        "scripts",
        "writ-dispatch-discipline.sh",
    )
)
HELPER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
)
FLUSH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-flush-events.py")
)


def _hook_env(cache_dir):
    """A throwaway cache dir and log destination, so no test reads or writes live state."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = os.path.join(str(cache_dir), "friction.log")
    env["WRIT_LOG_ROOT"] = os.path.join(str(cache_dir), "logs")
    return env


def _seed_mode(cache_dir, sid, mode):
    """Set the master session's mode via the file-direct CLI (how production sets it)."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    subprocess.run(
        [sys.executable, HELPER, "mode", "set", mode, sid],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _drain(cache_dir, sid):
    """Release `sid`'s buffered rows into the log.

    TWO SINKS, ONE COLLECTOR. log_gate_decision BUFFERS an "allow" (that is the volume)
    and emits anything else synchronously, so a test that read only one sink would see
    only one branch of the very thing under test. Draining puts both on the one log file
    this test owns.
    """
    subprocess.run([sys.executable, FLUSH, sid], env=_hook_env(cache_dir),
                   capture_output=True, text=True, timeout=60)


def _logged_gate_rows(cache_dir):
    """Every `dispatch-discipline` gate_decision on this test's log, any session."""
    log = _hook_env(cache_dir)["WRIT_FRICTION_LOG"]
    if not os.path.exists(log):
        return []
    with open(log) as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [r for r in rows
            if r.get("event") == "gate_decision" and r.get("gate") == "dispatch-discipline"]


def _audit_rows(cache_dir, sid):
    """The `dispatch-discipline` gate_decisions recorded FOR `sid`, drained first."""
    _drain(cache_dir, sid)
    return [r for r in _logged_gate_rows(cache_dir) if r.get("session") == sid]


def _run_hook(cache_dir, master_sid, *, subagent_type, prompt):
    env = _hook_env(cache_dir)
    envelope = {
        "session_id": master_sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
    }
    res = subprocess.run(
        ["bash", HOOK],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert res.returncode == 0, res.stderr  # denial is via stdout JSON, never exit code
    return res.stdout


def _decision(stdout):
    """Parse the hook's stdout; return (permissionDecision, reason) or (None, '')."""
    out = stdout.strip()
    if not out:
        return None, ""
    payload = json.loads(out)
    hso = payload.get("hookSpecificOutput", {})
    return hso.get("permissionDecision"), hso.get("permissionDecisionReason", "")


def _rewrite_target(stdout):
    """The subagent_type the hook rewrote the dispatch to via updatedInput (or None)."""
    out = stdout.strip()
    if not out:
        return None
    hso = json.loads(out).get("hookSpecificOutput", {})
    return (hso.get("updatedInput") or {}).get("subagent_type")


class TestDispatchDiscipline:
    def test_work_generic_explore_routes_to_explorer(self, tmp_path):
        _seed_mode(tmp_path, "m1", "work")
        out = _run_hook(
            tmp_path, "m1",
            subagent_type="general-purpose",
            prompt="explore the codebase structure and find where auth is handled",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-explorer"

    def test_work_named_role_allowed(self, tmp_path):
        """A named writ-* role is the correct dispatch -> pass through (no deny)."""
        _seed_mode(tmp_path, "m2", "work")
        out = _run_hook(
            tmp_path, "m2",
            subagent_type="writ-explorer",
            prompt="explore the codebase structure",
        )
        decision, _ = _decision(out)
        assert decision is None

    def test_escape_hatch_allows_generic(self, tmp_path):
        """An explicit [general-purpose] marker overrides the discipline."""
        _seed_mode(tmp_path, "m3", "work")
        out = _run_hook(
            tmp_path, "m3",
            subagent_type="general-purpose",
            prompt="[general-purpose] do this odd one-off task with no matching role",
        )
        decision, _ = _decision(out)
        assert decision is None

    def test_conversation_mode_not_enforced(self, tmp_path):
        """Conversation is not governed work -> pass through (a quick lookup mid-chat is fine)."""
        _seed_mode(tmp_path, "m4", "conversation")
        out = _run_hook(
            tmp_path, "m4",
            subagent_type="general-purpose",
            prompt="explore the codebase structure",
        )
        decision, _ = _decision(out)
        assert decision is None

    def test_investigate_mode_enforced(self, tmp_path):
        """Investigate (audit/explore) IS governed dispatch -> a generic audit dispatch is
        steered to writ-explorer. This is the exact scenario that slipped through before:
        an audit request must use the named role, not the built-in Explore/general-purpose."""
        _seed_mode(tmp_path, "m4b", "investigate")
        out = _run_hook(
            tmp_path, "m4b",
            subagent_type="general-purpose",
            prompt="audit the codebase for security issues and find where input is validated",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-explorer"

    def test_work_generic_research_routes_to_explorer(self, tmp_path):
        """'research ...' must route to writ-explorer (investigation engine)."""
        _seed_mode(tmp_path, "m4c", "work")
        out = _run_hook(
            tmp_path, "m4c",
            subagent_type="general-purpose",
            prompt="research how the session cache is keyed and what TTL is applied",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-explorer"

    def test_work_generic_implement_routes_to_implementer(self, tmp_path):
        """'implement the approved plan' routes to writ-implementer, not writ-planner."""
        _seed_mode(tmp_path, "m5", "work")
        out = _run_hook(
            tmp_path, "m5",
            subagent_type="general-purpose",
            prompt="implement the approved plan in the orders module",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-implementer"

    def test_work_builtin_explore_type_routes_to_explorer(self, tmp_path):
        """The built-in 'Explore' subagent_type is generic and is rewritten too."""
        _seed_mode(tmp_path, "m6", "work")
        out = _run_hook(
            tmp_path, "m6",
            subagent_type="Explore",
            prompt="investigate how the session cache is keyed",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-explorer"

    def test_empty_subagent_type_in_work_routes_to_planner(self, tmp_path):
        """An empty subagent_type (defaults to generic) is rewritten in work mode."""
        _seed_mode(tmp_path, "m7", "work")
        out = _run_hook(
            tmp_path, "m7",
            subagent_type="",
            prompt="plan the implementation of the new export endpoint",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-planner"

    def test_work_generic_ambiguous_denied(self, tmp_path):
        """A generic dispatch whose prompt maps to no specific role is DENIED (we ask rather
        than force a possibly-wrong role) -- the deny fallback the rewrite path preserves."""
        _seed_mode(tmp_path, "m8", "work")
        out = _run_hook(
            tmp_path, "m8",
            subagent_type="general-purpose",
            prompt="handle this one-off miscellaneous chore",
        )
        decision, reason = _decision(out)
        assert decision == "deny"
        assert "did not map to a specific Writ role" in reason

    # --- Binding extended to UNSET mode (2026-06-18) ----------------------------
    # Real engineering work routinely runs with no mode set; that ungoverned gap let
    # general-purpose agents through (observed in a real client project: most dispatches
    # ran mode=None). Unset mode is now governed like work/investigate.

    def test_unset_mode_generic_routes_to_role(self, tmp_path):
        """No mode set (the real-client-project case): a generic dispatch is now REWRITTEN
        to the matching writ-* role (was the ungoverned leak)."""
        # NB: no _seed_mode -> the session has no mode -> `mode get` returns "".
        out = _run_hook(
            tmp_path, "u1",
            subagent_type="general-purpose",
            prompt="explore the codebase and find where auth is handled",
        )
        assert _decision(out)[0] == "allow"
        assert _rewrite_target(out) == "writ-explorer"

    def test_unset_mode_named_role_allowed(self, tmp_path):
        """A named writ-* role is correct even with no mode set -> pass through."""
        out = _run_hook(
            tmp_path, "u2",
            subagent_type="writ-explorer",
            prompt="explore the codebase",
        )
        assert _decision(out)[0] is None

    def test_unset_mode_escape_hatch_allowed(self, tmp_path):
        """The [general-purpose] hatch still overrides when no mode is set."""
        out = _run_hook(
            tmp_path, "u3",
            subagent_type="general-purpose",
            prompt="[general-purpose] a genuine one-off with no matching role",
        )
        assert _decision(out)[0] is None

    def test_debug_mode_not_enforced(self, tmp_path):
        """debug is a deliberately-chosen non-build mode (own flow, Explore agents) ->
        stays ungoverned."""
        _seed_mode(tmp_path, "d1", "debug")
        out = _run_hook(
            tmp_path, "d1",
            subagent_type="general-purpose",
            prompt="explore the failing code path",
        )
        assert _decision(out)[0] is None

    def test_review_mode_not_enforced(self, tmp_path):
        """review (read-only) stays ungoverned."""
        _seed_mode(tmp_path, "r1", "review")
        out = _run_hook(
            tmp_path, "r1",
            subagent_type="general-purpose",
            prompt="review the diff for correctness",
        )
        assert _decision(out)[0] is None


class TestTheAuditRowMatchesTheDecisionEmitted:
    """The governance record must say what the hook actually did.

    THE DEFECT, reproduced live 2026-08-08. The hook logged `deny` whenever it emitted
    ANYTHING, inferring the label from "did we intervene" rather than from the decision
    in the JSON. Both branches emit, and the dominant branch is the REROUTE:
    permissionDecision=allow plus updatedInput, which is this hook's whole purpose. So
    every successful reroute was filed in the audit stream -- a governance record kept
    for 365 days -- as a denial, and the record was lying about the single mechanism
    that closes the general-purpose sub-agent gap.

    WHY BOTH DIRECTIONS ARE ASSERTED. A hook hard-coded to "allow" would pass an
    allow-only test just as the broken hook passed a deny-only reading of its own
    behavior. The label is only trustworthy if it TRACKS the decision, so each test
    compares the audit row against the permissionDecision that same run emitted, and
    `test_the_two_branches_are_recorded_differently` pins that the two do not collapse.
    """

    EXPLORE_PROMPT = "explore the codebase structure and find where auth is handled"
    AMBIGUOUS_PROMPT = "handle this one-off miscellaneous chore"

    def _emit_and_record(self, cache_dir, sid, prompt):
        """Run one dispatch; return (hook stdout, the single audit row it produced)."""
        out = _run_hook(cache_dir, sid, subagent_type="general-purpose", prompt=prompt)
        rows = _audit_rows(cache_dir, sid)
        assert len(rows) == 1, (
            f"expected exactly one dispatch-discipline audit row for {sid}, got {rows}"
        )
        return out, rows[0]

    def test_a_reroute_is_recorded_as_the_allow_it_emitted(self, tmp_path):
        """The row that was wrong: a governed reroute, recorded as a denial."""
        _seed_mode(tmp_path, "audit-allow", "work")
        out, row = self._emit_and_record(tmp_path, "audit-allow", self.EXPLORE_PROMPT)

        emitted, _ = _decision(out)
        assert emitted == "allow"
        assert _rewrite_target(out) == "writ-explorer", (
            "this case must be the reroute branch, or the assertion below proves nothing"
        )
        assert row["decision"] == emitted, (
            f"the dispatch was allowed and rerouted to writ-explorer, but the audit "
            f"stream recorded decision={row['decision']!r}"
        )

    def test_a_genuine_denial_is_still_recorded_as_deny(self, tmp_path):
        """The other direction, and the reason the fix is not just s/deny/allow/: an
        ambiguous dispatch really is denied and must still read as one."""
        _seed_mode(tmp_path, "audit-deny", "work")
        out, row = self._emit_and_record(tmp_path, "audit-deny", self.AMBIGUOUS_PROMPT)

        emitted, reason = _decision(out)
        assert emitted == "deny"
        assert "did not map to a specific Writ role" in reason
        assert row["decision"] == emitted, (
            f"the dispatch was denied but the audit stream recorded "
            f"decision={row['decision']!r}"
        )

    def test_two_dispatches_are_recorded_as_the_two_things_they_were(self, tmp_path):
        """Anti-vacuity for both tests above, read from the UNFILTERED log.

        A hook hard-coded to either label satisfies one of the two tests above, and each
        of those reads only its own session's rows. This one runs both dispatches and
        compares the whole audit picture, so it fails if the labels collapse onto one
        value AND if a row is filed under a session that did not dispatch it.
        """
        _seed_mode(tmp_path, "audit-a", "work")
        _seed_mode(tmp_path, "audit-b", "work")
        _run_hook(tmp_path, "audit-a", subagent_type="general-purpose",
                  prompt=self.EXPLORE_PROMPT)
        _run_hook(tmp_path, "audit-b", subagent_type="general-purpose",
                  prompt=self.AMBIGUOUS_PROMPT)
        _drain(tmp_path, "audit-a")
        _drain(tmp_path, "audit-b")

        recorded = {(r["session"], r["decision"]) for r in _logged_gate_rows(tmp_path)}
        assert recorded == {("audit-a", "allow"), ("audit-b", "deny")}, (
            f"the audit stream cannot distinguish a reroute from a refusal, or filed a "
            f"row under the wrong session: {recorded}"
        )

    def test_a_pass_through_is_recorded_as_allow(self, tmp_path):
        """The third branch, unchanged by the fix and asserted so it stays that way: a
        dispatch the hook never touches emits nothing and is an allow."""
        _seed_mode(tmp_path, "audit-passthrough", "work")
        out = _run_hook(tmp_path, "audit-passthrough",
                        subagent_type="writ-explorer", prompt=self.EXPLORE_PROMPT)
        assert _decision(out)[0] is None
        rows = _audit_rows(tmp_path, "audit-passthrough")
        assert [r["decision"] for r in rows] == ["allow"], rows
        assert rows[0]["reason"] == "dispatch not intercepted"

    def test_the_recorded_reason_is_the_decision_that_was_emitted(self, tmp_path):
        """The label and the evidence behind it must come from the same emission.

        The reason field carries the hook's own stdout, so an audit reader can check the
        label against the payload it claims to describe rather than trusting it.
        """
        _seed_mode(tmp_path, "audit-reason", "work")
        out, row = self._emit_and_record(tmp_path, "audit-reason", self.EXPLORE_PROMPT)
        assert json.loads(row["reason"]) == json.loads(out)


class TestTheAuditRowNamesTheAgentTypeDispatched:
    """The `target` field must carry the agent type the dispatch ASKED FOR.

    Every other gate names its subject in `target` -- the file it validated, the path it
    refused, the host it questioned -- which is what makes a row answerable without the
    payload beside it. This hook wrote `${AGENT_TYPE:-}`, a variable it never assigned, so
    all three branches filed an empty target and the audit stream could not say WHICH
    dispatch it had rerouted, refused, or waved through.

    The requested type lives in tool_input, which the hook reads only inside its embedded
    python, so this is not a rename: the value has to reach the shell. HOOK_AGENT_TYPE from
    load_hook_env is the type of the agent DOING the dispatching, which on the main session
    is empty and inside a sub-agent is confidently wrong.
    """

    EXPLORE_PROMPT = "explore the codebase structure and find where auth is handled"
    AMBIGUOUS_PROMPT = "handle this one-off miscellaneous chore"

    def test_a_reroute_names_the_generic_type_it_replaced(self, tmp_path):
        _seed_mode(tmp_path, "target-allow", "work")
        out = _run_hook(tmp_path, "target-allow", subagent_type="general-purpose",
                        prompt=self.EXPLORE_PROMPT)
        assert _rewrite_target(out) == "writ-explorer", "must be the reroute branch"

        rows = _audit_rows(tmp_path, "target-allow")
        assert [r["target"] for r in rows] == ["general-purpose"], (
            f"a reroute must record the type that was dispatched, got {rows}"
        )

    def test_a_denial_names_the_type_it_refused(self, tmp_path):
        _seed_mode(tmp_path, "target-deny", "work")
        out = _run_hook(tmp_path, "target-deny", subagent_type="claude",
                        prompt=self.AMBIGUOUS_PROMPT)
        assert _decision(out)[0] == "deny", "must be the ambiguous branch"

        rows = _audit_rows(tmp_path, "target-deny")
        assert [r["target"] for r in rows] == ["claude"], (
            f"a refusal must record the type that was refused, got {rows}"
        )

    def test_a_pass_through_names_the_governed_role_it_allowed(self, tmp_path):
        """The branch that emits nothing still knows what it let past."""
        _seed_mode(tmp_path, "target-passthrough", "work")
        out = _run_hook(tmp_path, "target-passthrough", subagent_type="writ-implementer",
                        prompt=self.EXPLORE_PROMPT)
        assert _decision(out)[0] is None, "must be the untouched branch"

        rows = _audit_rows(tmp_path, "target-passthrough")
        assert [r["target"] for r in rows] == ["writ-implementer"], (
            f"a pass-through must record what passed through, got {rows}"
        )

    def test_the_target_tracks_the_dispatch_rather_than_being_a_constant(self, tmp_path):
        """Anti-vacuity. Each test above pins ONE type, so a hook that hard-coded any
        single string would satisfy one of them exactly as the empty variable satisfied a
        reader who only checked that the field existed. Three dispatches of three
        different types must produce three different targets.
        """
        for sid, agent_type in (("target-x", "general-purpose"),
                                ("target-y", "Explore"),
                                ("target-z", "writ-reviewer")):
            _seed_mode(tmp_path, sid, "work")
            _run_hook(tmp_path, sid, subagent_type=agent_type, prompt=self.EXPLORE_PROMPT)
            _drain(tmp_path, sid)

        recorded = {(r["session"], r["target"]) for r in _logged_gate_rows(tmp_path)}
        assert recorded == {("target-x", "general-purpose"),
                            ("target-y", "Explore"),
                            ("target-z", "writ-reviewer")}, (
            f"the audit target does not track the dispatched type: {recorded}"
        )
