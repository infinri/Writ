"""Triggers each declared bash refusal as a real subprocess and asserts:

1. TRIGGERED. A real subprocess produced the refusal.
2. SHAPE. The emitted mechanism is the one declared (exit code, or a stdout
   `hookSpecificOutput.permissionDecision` of deny/ask), and the reason text is
   non-empty and matches at least one declared action marker.
3. RECORD. A durable row exists, in the stream that row belongs to, carrying the
   reason -- read via `read_streams`, never a single collapsed file.

Also pins the harness's own isolation properties (env-builder constraints, the
daemon-unreachable arm, the real capture log staying untouched) and the two
`validate-rules.sh` exit-2 sites, which are not uniform with the rest (one needs a
live `/analyze` stub) so they get dedicated tests rather than the generic loop.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tests.firedrill._census import (
    ACTION_MARKERS,
    by_id,
    generic_refusals,
    matches_action_marker,
)
from tests.firedrill._harness import (
    build_env,
    closed_port,
    make_isolation,
    read_cache,
    real_blackbox_snapshot,
    run_hook,
    stub_analyze_server,
    write_cache,
)

# A shrinking matrix should fail loudly, not silently reduce coverage (repo
# convention: tests/test_role_write_scope.py pins its own cross-product size).
assert len(generic_refusals()) == 24
assert len(ACTION_MARKERS) == 13


class TestHarnessEnvBuilder:
    """Capability: the harness env builder never points WRIT_CACHE_DIR, WRIT_LOG_ROOT
    or HOME outside the test's tmp dir, and always removes WRIT_FRICTION_LOG."""

    def test_cache_log_and_home_stay_inside_the_tmp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_FRICTION_LOG", "/should/never/survive.jsonl")
        iso = make_isolation(tmp_path)
        env = build_env(iso)
        tmp_resolved = tmp_path.resolve()
        for key in ("WRIT_CACHE_DIR", "WRIT_LOG_ROOT", "HOME"):
            value = Path(env[key]).resolve()
            assert value == tmp_resolved or tmp_resolved in value.parents, (
                f"{key}={value} escapes the test's tmp dir {tmp_resolved}"
            )

    def test_writ_friction_log_is_always_removed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_FRICTION_LOG", "/should/never/survive.jsonl")
        iso = make_isolation(tmp_path)
        env = build_env(iso)
        assert "WRIT_FRICTION_LOG" not in env

    def test_no_autostart_and_a_closed_port_are_set(self, tmp_path):
        iso = make_isolation(tmp_path)
        env = build_env(iso)
        assert env.get("WRIT_NO_AUTOSTART") == "1"
        port = int(env["WRIT_PORT"])
        with pytest.raises(OSError):
            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()


class TestRealCaptureLogNeverTouched:
    """Capability (trap 3): the real ~/.claude/writ-blackbox.jsonl is never written
    to by a drill run, because HOME is pinned to a tmp dir in the child env."""

    def test_a_representative_hook_run_does_not_touch_the_real_capture_log(
        self, tmp_path
    ) -> None:
        before = real_blackbox_snapshot()
        entry = by_id("state-write-gate")
        iso = make_isolation(tmp_path, session_id="capture-log-guard")
        setup = entry.setup(iso)
        run_hook(entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env"))
        after = real_blackbox_snapshot()
        assert after == before, (
            "a drill hook run appended to the developer's REAL blackbox capture log "
            f"(before={before}, after={after}); HOME isolation is broken"
        )


class TestEachDeclaredBashRefusal:
    """The generic loop: every census entry with `generic=True` is triggered,
    shape-checked and record-checked the same way."""

    @pytest.mark.parametrize(
        "entry", generic_refusals(), ids=[e.id for e in generic_refusals()]
    )
    def test_triggered_shape_and_record(self, tmp_path, entry) -> None:
        iso = make_isolation(tmp_path, session_id=f"firedrill-{entry.id}")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env"))

        if entry.mechanism == "permissionDecisionReason":
            decision = result.permission_decision()
            assert decision == entry.permission_decision, (
                f"{entry.id}: expected permissionDecision={entry.permission_decision!r}, "
                f"got {decision!r}; stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            reason = result.permission_reason()
        elif entry.mechanism in ("exit2_stderr", "exit_nonzero_stderr"):
            assert result.returncode == entry.exit_code, (
                f"{entry.id}: expected exit {entry.exit_code}, got {result.returncode}; "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            reason = result.stderr
        else:
            pytest.fail(f"{entry.id}: unknown declared mechanism {entry.mechanism!r}")

        # SHAPE, reason half: non-empty first (the precondition that makes the marker
        # comparison meaningful), then the marker match itself.
        assert reason and reason.strip(), (
            f"{entry.id}: refusal reason is empty -- a refusal that names no way out "
            "is a deadlock, not a control"
        )
        if entry.check_action_marker:
            assert matches_action_marker(reason), (
                f"{entry.id}: reason matches none of ACTION_MARKERS: {reason!r}"
            )

        # RECORD: assert the stream is non-empty BEFORE filtering it, so a pass here
        # is never "an empty list equals an empty list" in disguise.
        if entry.shape == "gate_decision":
            audit = result.audit()
            assert audit, (
                f"{entry.id}: audit stream is empty; the hook produced no durable "
                f"record at all (streams read: {result.all_streams()!r})"
            )
            rows = [r for r in audit if r.get("event") == "gate_decision"]
            if entry.gate_name:
                rows = [r for r in rows if r.get("gate") == entry.gate_name]
            assert rows, (
                f"{entry.id}: no gate_decision row (gate={entry.gate_name!r}) in "
                f"audit: {audit!r}"
            )
            last = rows[-1]
            assert last.get("decision") in ("deny", "ask"), last
            assert last.get("reason"), f"{entry.id}: gate_decision row has an empty reason: {last!r}"
        elif entry.shape == "friction_custom":
            audit = result.audit()
            assert audit, (
                f"{entry.id}: audit stream is empty; expected a {entry.custom_event!r} "
                f"row (streams read: {result.all_streams()!r})"
            )
            rows = [r for r in audit if r.get("event") == entry.custom_event]
            assert rows, f"{entry.id}: no {entry.custom_event!r} row in audit: {audit!r}"
        else:
            pytest.fail(f"{entry.id}: unknown declared shape {entry.shape!r} for the generic loop")


class TestEnforceViolationsCarriesRuleIds:
    """D1, the specific half beyond the generic shape check: the recorded reason
    carries the pending violation rule ids, not just SOME non-empty text."""

    def test_the_gate_decision_reason_names_the_violated_rule_id(self, tmp_path) -> None:
        entry = by_id("enforce-violations")
        iso = make_isolation(tmp_path, session_id="enforce-violations-rule-ids")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso)

        assert result.returncode == 2, result.stderr
        assert "ENF-TEST-999" in result.stderr, (
            f"stderr must name the violated rule id: {result.stderr!r}"
        )

        audit = result.audit()
        assert audit, "audit stream is empty; enforce-violations.sh recorded nothing"
        rows = [
            r for r in audit
            if r.get("event") == "gate_decision" and r.get("decision") == "deny"
        ]
        assert rows, f"no gate_decision deny row: {audit!r}"
        assert any("ENF-TEST-999" in (r.get("reason") or "") for r in rows), (
            f"no gate_decision row's reason carries the violated rule id: {rows!r}"
        )


class TestBashWriteCredentialGateDecisionNamesThePath:
    """D4, the specific half beyond the generic shape check: the recorded
    gate_decision row NAMES THE CREDENTIAL PATH, alongside emit_deny's own output,
    not just some non-empty reason."""

    def test_the_gate_decision_row_names_the_credential_path(self, tmp_path) -> None:
        entry = by_id("bash-write-credential")
        iso = make_isolation(tmp_path, session_id="bash-write-credential-names-path")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso)

        assert result.permission_decision() == "deny", (
            f"expected emit_deny's own deny; stdout={result.stdout!r}"
        )
        credential_path = str(iso.project_root / ".env")

        audit = result.audit()
        assert audit, "audit stream is empty; writ-bash-write-gate.sh recorded nothing"
        rows = [
            r for r in audit
            if r.get("event") == "gate_decision" and r.get("decision") == "deny"
        ]
        assert rows, f"no gate_decision deny row: {audit!r}"
        assert any(credential_path in (r.get("reason") or "") for r in rows), (
            f"no gate_decision row names the credential path {credential_path!r}: {rows!r}"
        )


class TestPinnedExitCodesForAdvisoryStopHooks:
    """D3: the exit codes of these three Stop hooks stay 1 on their refusal paths.
    Flipping to 2 would start blocking the user's real turns without their consent;
    this is the trip-wire that makes a future change to 2 visible."""

    @pytest.mark.parametrize(
        "refusal_id",
        ["run-pending-tests", "verify-before-claim", "comms-output-gate"],
    )
    def test_exit_code_is_pinned_at_one(self, tmp_path, refusal_id) -> None:
        entry = by_id(refusal_id)
        iso = make_isolation(tmp_path, session_id=f"pin-{refusal_id}")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env"))
        assert result.returncode == 1, (
            f"{refusal_id}: exit code drifted off its pinned value of 1 "
            f"(got {result.returncode}); if this is deliberate it needs the user's "
            f"explicit consent (plan.md Analysis / Decision 5, D3)"
        )


class TestAllFourRecordShapesAppearAcrossTheDrill:
    """D8: gate_decision, write_attempt, a custom-event friction-append pipe, and a
    session-cache invalidation_history write are each exercised somewhere in the
    drill. `write_attempt` is not a bash-census shape (it comes from gates.py's own
    deny/allow telemetry, exercised directly in test_python_refusals.py); it is
    named here as a literal, not invented to make the assertion pass."""

    def test_required_shapes_are_a_subset_of_what_the_drill_declares(self) -> None:
        from tests.firedrill._census import REFUSALS

        bash_shapes = {r.shape for r in REFUSALS}
        all_shapes = bash_shapes | {"write_attempt"}
        required = {"gate_decision", "write_attempt", "friction_custom", "invalidation_history"}
        assert required <= all_shapes, f"missing record shapes: {required - all_shapes}"


class TestValidateRulesBothSites:
    """D2: `validate-rules.sh` has two bare `exit 2` sites with no stderr message at
    all today. Both are triggered as real subprocesses; neither goes through the
    generic loop because their trigger paths are not uniform with the rest."""

    def test_top_of_file_sentinel_exit_2_before_any_processing(self, tmp_path) -> None:
        entry = by_id("validate-rules-site-a")
        iso = make_isolation(tmp_path, session_id="validate-rules-site-a")
        setup = entry.setup(iso)
        result = run_hook(
            entry.script, setup["envelope"], iso, extra_env=setup.get("extra_env")
        )
        assert result.returncode == 2, (
            f"expected the top-of-file sentinel to exit 2; got {result.returncode}, "
            f"stderr={result.stderr!r}"
        )
        assert result.stderr.strip(), (
            "the top-of-file sentinel exit 2 must carry a non-empty stderr message "
            "naming the gate that was invalidated and the action (D2); today it is "
            "a bare `exit 2` with no echo and no stderr at all"
        )
        # The sentinel is consumed (rm -f) on the way out, exactly once, so a
        # re-invocation with the same session does NOT exit 2 again.
        assert not setup["sentinel_path"].exists(), (
            "the sentinel file must be removed after it is read"
        )

    def test_tail_end_sentinel_exit_2_after_a_routed_invalidation(self, tmp_path) -> None:
        iso = make_isolation(tmp_path, session_id="validate-rules-site-b")
        sub_dir = iso.project_root / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "plan.md").write_text("## Files\n- `src/thing.py`\n")

        target = iso.project_root / "src" / "thing.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('hello')\n")

        write_cache(iso, {
            "mode": "work",
            "loaded_rules": [{"rule_id": "ENF-TEST-999"}],
            "files_written": [str(target)],
            "analysis_results": {str(target): "pass"},
        })

        response = {
            "verdict": "fail",
            "summary": "one confirmed violation (firedrill)",
            "findings": [
                {"rule_id": "ENF-TEST-999", "status": "violated", "evidence": "firedrill"}
            ],
        }
        sys_tmp = iso.tmp_path / "sysTmp"
        sys_tmp.mkdir(parents=True, exist_ok=True)

        with stub_analyze_server(response) as port:
            result = run_hook(
                "validate-rules.sh",
                {
                    "session_id": iso.session_id,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(target)},
                },
                iso,
                extra_env={"WRIT_PORT": port, "TMPDIR": str(sys_tmp)},
                cwd=iso.project_root,
            )

        assert result.returncode == 2, (
            f"expected the tail-end sentinel to exit 2; got {result.returncode}, "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # NOT asserted here: "stderr must name the action" (D2's actual fix). Measured
        # directly -- this path's stderr is ALREADY non-empty before the fix
        # ('[Writ rule compliance] <summary>', printed by the pre-existing findings
        # report a few lines earlier), so a bare non-empty check here would pass for
        # the wrong reason regardless of whether the bare `exit 2` site itself ever
        # gains a message. Site A isolates that exact fix (nothing else in its path
        # writes to stderr); this site's job is the OTHER thing only it can prove --
        # the fourth record shape below, produced through the live routing logic.

        # The fourth record shape (D8): a session-cache invalidation_history write,
        # made by the REAL `invalidate-gate` CLI this run shelled out to.
        cache_after = read_cache(iso)
        assert cache_after is not None, "the session cache vanished after the run"
        history = (cache_after.get("invalidation_history") or {}).get("phase-a", [])
        assert history, (
            "invalidate-gate did not append a phase-a invalidation_history record: "
            f"{cache_after!r}"
        )
        assert any(rec.get("rule_id") == "ENF-TEST-999" for rec in history), history


_IRREVERSIBLE_NEGATIVE_CASES = [
    pytest.param("git push origin main --force-with-lease", id="force-with-lease"),
    pytest.param("git branch -d feature-branch", id="branch-lowercase-d"),
]
assert len(_IRREVERSIBLE_NEGATIVE_CASES) == 2


class TestIrreversibleGitNegativeControls:
    """A drill that only proves denials would not notice the day the gate starts
    refusing a safe spelling too. writ-bash-write-gate.sh's own `_irreversible_
    reason` deliberately allows the reversible forms: `--soft`, `--force-with-
    lease`, `git clean -n`, and `git branch -d` (lowercase). This pins the two
    that share a command-text prefix with a denied pattern above, so a regex
    that widens to catch the flag it should exclude is caught here, not missed."""

    @pytest.mark.parametrize("cmd", _IRREVERSIBLE_NEGATIVE_CASES)
    def test_the_reversible_form_is_not_refused(self, tmp_path, cmd) -> None:
        iso = make_isolation(tmp_path, session_id=f"irrev-negative-{hash(cmd) & 0xffff}")
        envelope = {
            "session_id": iso.session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }
        result = run_hook("writ-bash-write-gate.sh", envelope, iso)
        assert result.permission_decision() is None, (
            f"the reversible form {cmd!r} must not be refused: stdout={result.stdout!r}"
        )
