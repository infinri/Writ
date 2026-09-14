"""tests/test_replan_reopen_planning.py

Cycle: a human-authorized way out of the plan-frozen deadlock (plan.md, session
dfacff61-23d5-474e-846c-2e2f0f0ea482).

THE LIVE INCIDENT THIS FIXES: session 534310ec, entries 1184-1200. mode=work,
current_phase=implementation, gates_approved=["phase-a","test-skeletons"]; the agent
needed to amend plan.md, [ENF-GATE-PLAN] refused the edit and told the agent to "ask
the user", the user typed "approved" twice, and hooks/scripts/auto-approve-gate.sh
answered with exactly this text:

    "No approval gate was advanced (no gate is pending in this phase/mode, or the Writ
    server is unreachable). If you intended to advance a Work-mode plan/test gate,
    ensure the server is up; otherwise this approval needs no gate action."

That closing clause -- "otherwise this approval needs no gate action" -- is the
misleading string this cycle deletes: in the deadlocked state an action WAS needed
(re-opening planning) and none existed, so the sentence told the user something false.
The fix gives the human one deliberate phrase, `replan approved`, that re-opens
planning (clears both gates, restores plan.md, keeps every source write blocked until
phase-a and test-skeletons are approved again against the revised plan), and splits
the "no gate pending" message from the "server unreachable" message so the two causes
this incident conflated are never reported as one.

MUST-NOT-REGRESS, PASSING TODAY, BEFORE ANY LINE OF THIS CYCLE LANDS:
  * TestBareApprovedAdvancesPendingGateMustNotRegress -- the bare word `approved`
    mints a token bound to a pending gate and advances it exactly as it does on the
    code checked in today (cycle 1's mint/claim/advance mechanism, untouched here).
  * TestBareApprovedWithNoGatePendingMustNotRegress::
    test_approved_with_no_gate_pending_changes_no_state -- with no gate pending and
    the replan phrase not used, `approved` changes no phase, no gates_approved, and no
    on-disk `*.approved` artifact. (Its sibling test in the same class, which pins
    that the emitted directive NAMES the replan phrase, is new behavior and starts RED
    -- the two are kept in one class because capabilities.md states them as one item,
    but only the state-invariant half is guaranteed to pass before implementation.)

EVERY OTHER TEST IN THIS FILE STARTS RED: `mode_engine.reopen_planning`,
`approval_workflow.cmd_reopen_planning`, `gate_token.REPLAN_GATE`, the `"replan"` tier
on `approval_match.classify`, the rewritten [ENF-GATE-PLAN] message, the three
`_replan_hint` call sites plus the split unreachable-daemon branch in
auto-approve-gate.sh, and the `reopen-planning` entry in writ-bash-write-gate.sh's
STATE_MATCH list do not exist yet on the code this file is checked in against.

NOT STATED VERBATIM IN plan.md, committed to here as the narrowest reading of its
Analysis and Files sections (same disclosure convention as
tests/test_gate_token_binding.py's module docstring):
  * `mode_engine.reopen_planning(session_id: str) -> None` -- no caller-supplied
    `trigger` kwarg is assumed; the plan's `trigger` pass-through is described as
    living on `_apply_mode_set`/`_mode_set`, which `reopen_planning` calls internally.
  * `approval_workflow.cmd_reopen_planning(session_id: str, token: str = "") -> None`,
    printing one JSON object to stdout via the same `_emit_json` convention as
    `cmd_advance_phase`. This file does not pin the exact success-flag key name (the
    plan states the CLI's shape only by analogy to `cmd_advance_phase`); it asserts on
    the fields plan.md's "Audit record" section states explicitly instead (from_phase,
    to_phase, gates_cleared, confirmation_source, reason) wherever the key name itself
    is not given.
  * the bash `replan)` case arm mints via the shared `write_gate_token_file` helper
    with the literal gate argument `"replan"`.

Per ENF-SYS-005 (## Rules Applied, this cycle's plan.md): the token claim is exercised
through the real `os.rename`-based claim path (`gate_token.claim_gate_token` /
`approval_workflow.cmd_reopen_planning`) with token-file presence or absence asserted
ON DISK, never through a patched claim function; the cache mutation goes through the
real flock-serialized `writ.session.cache.mutate_cache` (via `mode_engine.reopen_planning`
and `_mode_set`) against a `tmp_path`-backed `WRIT_CACHE_DIR`, never a mocked cache.

Per TEST-ISOLATE-003: every test gets its own `WRIT_CACHE_DIR` (via monkeypatch or the
shared `session_id` fixture), its own `tmp_path`-rooted project, and -- for anything
that mints a gate token, which `gate_token_path` hardcodes under `/tmp` regardless of
`WRIT_CACHE_DIR` -- its own uuid-suffixed session id plus the same `_mint_cleanup` /
autouse leak-guard convention `tests/test_gate_token_binding.py` established. The
`sandbox_cwd` fixture (imported, autouse) pins the process cwd to a disposable sandbox
for the whole file, because `mode_engine._apply_mode_set` (which `reopen_planning`
reuses) stamps `cache["project_root"] = os.getcwd()` and deletes
`<project_root>/.claude/gates/*.approved` -- exactly the operation this cycle adds --
so a test that let that resolve to the real repo would delete this repo's own gate
artifacts. No test in this file reads or writes the developer's real
`~/.cache/writ`, this repo's own `.claude/gates`, or a real `/tmp` gate-token path
outside its own uuid-suffixed session.

Per TEST-TDD-001 / SKL-PROC-WRIT-FAILURE-001: skeletons approved before implementation.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

# ruff: noqa: F811 -- sandbox_cwd is imported for its autouse side effect (pins cwd to
# a disposable sandbox for every test in this module); write_bound_gate_token and
# call_can_write are the shared session-state helpers this file is told to reuse
# rather than reinvent.
from tests.fixtures.session_state import (  # noqa: F401
    call_can_write,
    sandbox_cwd,
    write_bound_gate_token,
    write_evidence_transcript,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
AUTO_APPROVE_GATE_SH = SKILL_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"
BASH_WRITE_GATE_SH = SKILL_ROOT / "hooks" / "scripts" / "writ-bash-write-gate.sh"

# bin/lib/approval_match.py is a standalone stdlib-only module (no writ-package
# import, by its own docstring), loaded by path exactly as the hook loads it.
sys.path.insert(0, str(SKILL_ROOT / "bin" / "lib"))
import approval_match  # noqa: E402
from approval_match import classify, is_approval  # noqa: E402

from writ.session import approval_workflow  # noqa: E402
from writ.session import gate_token  # noqa: E402
from writ.session import gates as gates_module  # noqa: E402
from writ.session import mode_engine  # noqa: E402
from writ.session.cache import _read_cache, _write_cache  # noqa: E402
from writ.session.locators import gate_artifact_path, gate_dir, plan_md_hash  # noqa: E402
from writ.shared.logging import stream_path  # noqa: E402

# The session-cache facade, loaded exactly as tests/test_mode_infrastructure.py loads
# it (writ-session.py is not a package member), for call_can_write's writ_session arg.
_HELPER_SPEC = importlib.util.spec_from_file_location(
    "writ_session", str(SKILL_ROOT / "bin" / "lib" / "writ-session.py")
)
writ_session = importlib.util.module_from_spec(_HELPER_SPEC)
_HELPER_SPEC.loader.exec_module(writ_session)


def _require(module, *names) -> None:
    """Fail loudly (never skip) when this cycle's production symbols are absent."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(
            f"{getattr(module, '__name__', module)} is missing {missing}: this "
            "cycle's production code has not been implemented yet."
        )


def _sid(label: str) -> str:
    """A synthetic session id, unique per call, safe as a path component and as the
    `/tmp/writ-gate-token-<sid>` suffix (gate_token_path hardcodes /tmp regardless of
    WRIT_CACHE_DIR, per this module's docstring)."""
    return f"replan-{label}-{uuid.uuid4().hex[:8]}"


# A plan.md shape already proven to pass approval_workflow._validate_phase_a (the same
# shape tests/test_gates_fail_open.py and tests/test_gate_token_binding.py use).
_PLAN_BODY = (
    "# Plan: a fixture\n\n"
    "## Files\n\n"
    "- `src/thing.py` (modify) -- the fixture's only file\n\n"
    "## Analysis\n\n"
    "Fixture plan.\n\n"
    "## Rules Applied\n\n"
    "No matching rules.\n\n"
    "## Capabilities\n\n"
    "- [ ] the fixture behaves\n"
)

_REVISED_PLAN_BODY = _PLAN_BODY.replace(
    "the fixture behaves", "the fixture behaves, revised after a re-open"
)

_TEST_SKELETON_BODY = "def test_the_fixture_behaves():\n    assert True\n"


def _write_session_plan(project_root: Path, session_id: str, body: str = _PLAN_BODY) -> Path:
    """The session-scoped plan.md that plan_md_hash / _find_plan_md resolve first
    (mirrors tests/test_gates_fail_open.py's `_write_plan`)."""
    directory = project_root / ".claude" / "plans" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.md"
    path.write_text(body)
    return path


def _seed_cache(sid: str, **fields) -> dict:
    cache = _read_cache(sid)
    cache.update(fields)
    _write_cache(sid, cache)
    return cache


def _seed_deadlocked_cache(project_root: Path, sid: str, is_orchestrator: bool = False) -> str:
    """mode=work, phase=implementation, both gates approved against the CURRENT plan --
    the ordinary post-cycle state the replan phrase exists to escape. Returns the plan
    fingerprint."""
    _write_session_plan(project_root, sid, _PLAN_BODY)
    h = plan_md_hash(str(project_root), sid)
    _seed_cache(
        sid,
        mode="work", current_phase="implementation",
        gates_approved=["phase-a", "test-skeletons"],
        gates_approved_plan={"phase-a": h, "test-skeletons": h},
        project_root=str(project_root),
        denial_counts={}, is_orchestrator=is_orchestrator,
    )
    return h


def _seed_drift_rearmed_implementation_cache(project_root: Path, sid: str) -> None:
    """mode=work, phase=implementation, both gates approved -- but bound to a STALE
    fingerprint, so `_next_pending_gate` re-arms phase-a even though the phase already
    advanced past it. This is the plan's own "plan-drift re-arm" case: a gate IS
    pending during implementation."""
    _write_session_plan(project_root, sid, _PLAN_BODY)
    _seed_cache(
        sid,
        mode="work", current_phase="implementation",
        gates_approved=["phase-a", "test-skeletons"],
        gates_approved_plan={"phase-a": "stale-hash-value", "test-skeletons": "stale-hash-value"},
        project_root=str(project_root),
        denial_counts={},
    )


@contextlib.contextmanager
def _mint_cleanup(sid: str):
    """Guarantees /tmp/writ-gate-token-<sid> is removed after the block, even when the
    block raises (mirrors tests/test_gate_token_binding.py's helper of the same name)."""
    try:
        yield
    finally:
        try:
            os.remove(gate_token.gate_token_path(sid))
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leaked_gate_tokens():
    """Safety net for every test in this file, sharing the one decision
    tests/test_gate_token_binding.py's identical fixture uses: a forgotten
    `_mint_cleanup` still leaves a stray approval-shaped file loudly failing this test,
    but only the files whose session id could NOT belong to a live session are deleted.
    The rest come back as `left_alone`, untouched and reported, because this suite
    cannot tell one from an approval a human typed in another window."""
    from tests._gate_token_leak import (
        confined_leak_sweep,
        live_snapshot,
        warn_about_left_alone,
    )

    before = live_snapshot()
    yield
    removed, left_alone = confined_leak_sweep(before)
    warn_about_left_alone(left_alone)
    assert not removed, f"test leaked gate token file(s) (now removed): {sorted(removed)}"


def _call_json(fn, *args) -> dict:
    """Capture one CLI-style function's stdout JSON (mirrors
    tests/test_gate_token_binding.py's helper of the same name)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return json.loads(buf.getvalue().strip())


def _call_stdin_safe_json(monkeypatch, fn, *args) -> dict:
    """`_call_json`, with stdin stubbed first: cmd_advance_phase unconditionally reads
    stdin (hooks may pipe a prompt), which pytest's capture replaces with an object
    that raises OSError on read(). cmd_reopen_planning may or may not do the same; this
    is harmless either way."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return _call_json(fn, *args)


def _read_stream_rows(project: str, stream: str) -> list[dict]:
    path = stream_path(project, stream)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CannedAdvanceHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the daemon's /session/<id>/advance-phase route: always answers
    with this class's `body` (a canned JSON response), regardless of what it is asked.
    Used to drive auto-approve-gate.sh's response-classification branches (noop vs.
    advanced vs. rejected) deterministically, without a live Neo4j-backed daemon."""

    body = b'{"advanced": false, "reason": "No gates for this mode"}'

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        payload = self.__class__.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args, **_kwargs):  # silence BaseHTTPRequestHandler's stderr
        pass


@contextlib.contextmanager
def _mock_advance_daemon(response_body: bytes):
    """A real local HTTP server standing in for the Writ daemon's advance-phase route
    -- a genuine socket accepting a genuine POST, not a patched python function -- so
    the hook's own curl/urllib client code runs unmodified. Yields the port."""
    handler_cls = type("_Handler", (_CannedAdvanceHandler,), {"body": response_body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run_auto_approve_hook(payload: dict, cwd: Path, extra_env: dict | None = None) -> str:
    """Run the real UserPromptSubmit hook with a synthetic envelope; return stdout."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(AUTO_APPROVE_GATE_SH)],
        input=json.dumps(payload), cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout


def _run_bash_write_gate(cmd: str, sid: str, cwd: Path) -> dict | None:
    """Run the real Bash PreToolUse gate; return hookSpecificOutput on a deny, else
    None (mirrors tests/test_bash_write_gate.py's `_run_hook`)."""
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash", "tool_input": {"command": cmd}})
    proc = subprocess.run(
        ["bash", str(BASH_WRITE_GATE_SH)], input=envelope, cwd=str(cwd),
        capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out).get("hookSpecificOutput", {})


# =============================================================================
# Capability 1: classify("replan approved") == "replan"; is_approval stays False
# =============================================================================


class TestReplanPhraseClassification:
    """`classify()` gains a "replan" tier, checked ahead of "embedded" (the phrase
    contains the word "approved" and would otherwise classify there)."""

    @pytest.mark.parametrize("prompt", [
        "replan approved",
        "replan approved.",
        "replan approved!",
        "replan approved,",
        "  replan approved  ",
        "REPLAN APPROVED",
        "Replan Approved.",
        "\treplan approved\n",
    ], ids=[
        "bare", "trailing-period", "trailing-bang", "trailing-comma",
        "surrounding-whitespace", "uppercase", "titlecase-with-period", "tabs-and-newline",
    ])
    def test_classify_recognizes_replan_approved_variants(self, prompt: str) -> None:
        assert classify(prompt) == "replan", (
            f"classify({prompt!r}) must be 'replan'; got {classify(prompt)!r}"
        )
        # is_approval's accept set is untouched by this cycle: it stays exactly the
        # bare word "approved", so a token must never be minted straight off this
        # phrase through the OLD exact-tier path.
        assert is_approval(prompt) is False, (
            f"is_approval({prompt!r}) must stay False; the replan phrase must not "
            "also satisfy the ordinary exact-approval predicate"
        )


# =============================================================================
# Capability 2: the near-miss set never classifies as "replan" (PROPERTY, parametrized)
# =============================================================================


class TestNearMissPromptsNeverTriggerReplan:
    _NEAR_MISSES = [
        "do not replan",
        "replan not approved",
        "should we replan approved?",
        "replan approved because the schema changed",
    ]

    @pytest.mark.parametrize("prompt", _NEAR_MISSES)
    def test_near_miss_prompt_does_not_classify_as_replan(self, prompt: str) -> None:
        assert classify(prompt) != "replan", (
            f"classify({prompt!r}) must never be 'replan'; a near-miss prompt must "
            "not be able to reset a session's phase and gates"
        )

    def test_the_qualified_near_miss_still_reaches_the_embedded_tier(self) -> None:
        """'replan approved because the schema changed' contains the bare word
        'approved' and is not the whole-prompt phrase, so it must fall through to the
        existing embedded tier (ask, never advance) rather than to 'none' -- recall
        for a genuine-sounding approval must not regress just because 'replan' now
        exists as a word."""
        assert classify("replan approved because the schema changed") == "embedded"


# =============================================================================
# Capability 3 & 4: the two MUST-NOT-REGRESS cases for the bare word `approved`
# =============================================================================


class TestBareApprovedAdvancesPendingGateMustNotRegress:
    """MUST PASS TODAY: this exercises cycle 1's existing mint/claim/advance
    mechanism directly (approval_match.is_approval + gate_token + cmd_advance_phase),
    none of which this cycle touches."""

    def test_approved_with_phase_a_pending_mints_and_advances_exactly_as_today(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("mustnotregress-advance")

        with _mint_cleanup(sid):
            _write_session_plan(project_root, sid)
            plan_hash = plan_md_hash(str(project_root), sid)
            _seed_cache(
                sid, mode="work", current_phase="planning",
                gates_approved=[], gates_approved_plan={},
                project_root=str(project_root),
            )

            assert is_approval("approved") is True, (
                "precondition: the bare word must classify as an exact approval"
            )

            token = gate_token.mint_gate_token(sid, gate="phase-a", plan_hash=plan_hash)
            assert os.path.exists(gate_token.gate_token_path(sid)), (
                "precondition: the mint must actually leave a token file to claim"
            )

            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_advance_phase, sid, str(project_root), token,
            )

            assert result["advanced"] is True, f"today's mint+advance path must still succeed: {result}"
            assert result["gate"] == "phase-a"
            assert result["phase"] == "testing"

            updated = _read_cache(sid)
            assert updated["gates_approved"] == ["phase-a"]
            assert not os.path.exists(gate_token.gate_token_path(sid)), (
                "a claimed token must not remain on disk"
            )


class TestBareApprovedWithNoGatePendingMustNotRegress:
    def test_approved_with_no_gate_pending_changes_no_state(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        """MUST PASS TODAY: no gate pending (both approved, no drift), the bare word
        `approved` mints a no-gate-bound token and cmd_advance_phase's own
        'All gates already approved' no-op path leaves phase, gates_approved and the
        on-disk *.approved artifacts byte-for-byte unchanged."""
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("mustnotregress-noop")

        with _mint_cleanup(sid):
            _seed_deadlocked_cache(project_root, sid)

            # Precondition: real on-disk approval artifacts exist before the call, so
            # "unchanged" is a comparison against something, not None-equals-None.
            gd = gate_dir(str(project_root), sid)
            os.makedirs(gd, exist_ok=True)
            phase_a_artifact = gate_artifact_path(str(project_root), sid, "phase-a")
            skeletons_artifact = gate_artifact_path(str(project_root), sid, "test-skeletons")
            for artifact in (phase_a_artifact, skeletons_artifact):
                with open(artifact, "w") as f:
                    f.write(sid + "\n")
            before_cache = _read_cache(sid)
            before_bytes = {p: Path(p).read_bytes() for p in (phase_a_artifact, skeletons_artifact)}

            token = gate_token.mint_gate_token(sid, gate="", plan_hash="")

            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_advance_phase, sid, str(project_root), token,
            )
            assert result["advanced"] is False, f"nothing was pending; must not advance: {result}"

            after_cache = _read_cache(sid)
            assert after_cache["current_phase"] == before_cache["current_phase"] == "implementation"
            assert after_cache["gates_approved"] == before_cache["gates_approved"]
            for path, contents in before_bytes.items():
                assert Path(path).read_bytes() == contents, f"{path} was mutated by a no-op advance"

    def test_approved_with_no_gate_pending_directive_names_the_replan_phrase(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        """NEW behavior, starts RED: today's directive says only "this approval needs
        no gate action"; after this cycle it must additionally name `replan approved`
        so the user is not left guessing (this is the string the live incident's
        transcript shows the user never received)."""
        project_root = sandbox_cwd
        sid = _sid("directive-names-phrase")
        # The seeding below runs in THIS process while the hook reads the dir named in
        # extra_env, and conftest.py points this process at a session-wide mkdtemp. Without
        # this the hook sees an unknown session, fails closed, and the assertion below
        # would be testing the unknown-state branch rather than the no-gate-pending one.
        # Set per-test rather than per-class: the sibling must-not-regress test in this
        # class manages its own cache dir and must not be disturbed.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
        _seed_deadlocked_cache(project_root, sid)

        # The bare word is an EXACT-tier approval, and the exact tier mints a token
        # only WITH evidence that an approval was asked for (approval-integrity
        # cycle, defect 2). Even with no gate pending, see auto-approve-gate.sh's
        # own comment on why the mint still happens in that state. Wrapped in
        # _mint_cleanup so this real hook-minted token cannot leak into /tmp.
        transcript_path = write_evidence_transcript(tmp_path)
        with _mint_cleanup(sid):
            stdout = _run_auto_approve_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": transcript_path},
                cwd=project_root,
                extra_env={"WRIT_CACHE_DIR": str(tmp_path / "cache")},
            )
        assert "replan approved" in stdout, (
            f"the no-gate-pending directive must name the replan phrase; got:\n{stdout}"
        )


# =============================================================================
# Capability 5: reopen_planning's state transformation
# =============================================================================


class TestReopenPlanningStateTransition:
    def test_reopen_planning_clears_gates_returns_to_planning_and_preserves_orchestrator(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(mode_engine, "reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("state-transition")

        _seed_deadlocked_cache(project_root, sid, is_orchestrator=True)

        gd = gate_dir(str(project_root), sid)
        os.makedirs(gd, exist_ok=True)
        artifacts = [
            gate_artifact_path(str(project_root), sid, "phase-a"),
            gate_artifact_path(str(project_root), sid, "test-skeletons"),
        ]
        for artifact in artifacts:
            with open(artifact, "w") as f:
                f.write(sid + "\n")
        # Precondition: the artifacts genuinely exist before the reset, so "no
        # remaining artifacts" below is a real disappearance, not a vacuous absence.
        assert all(os.path.exists(a) for a in artifacts), "fixture setup failed"

        mode_engine.reopen_planning(sid)

        after = _read_cache(sid)
        assert after["current_phase"] == "planning", after
        assert after["gates_approved"] == [], after
        assert after["gates_approved_plan"] == {}, after
        assert after["is_orchestrator"] is True, (
            "an orchestrator session must stay suppressed after a re-open"
        )
        for artifact in artifacts:
            assert not os.path.exists(artifact), f"{artifact} must be removed by a re-open"


# =============================================================================
# Capability 6: STRICTNESS PROPERTY -- a re-open is stricter, never looser
# =============================================================================


class TestStrictnessPropertyAfterReopen:
    _ORDINARY_SOURCE_RELPATHS = [
        "src/thing.py", "app/module.js", "lib/foo.go", "pkg/bar.rs",
    ]

    def _reopen(self, tmp_path, monkeypatch, project_root: Path, sid: str) -> dict:
        _require(mode_engine, "reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        _seed_deadlocked_cache(project_root, sid)
        mode_engine.reopen_planning(sid)
        return _read_cache(sid)

    @pytest.mark.parametrize("relpath", _ORDINARY_SOURCE_RELPATHS)
    def test_ordinary_source_write_denied_immediately_after_reopen(
        self, tmp_path, monkeypatch, sandbox_cwd, relpath: str,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("strict-" + relpath.replace("/", "-"))
        cache_after = self._reopen(tmp_path, monkeypatch, project_root, sid)

        envelope = {"tool_input": {"file_path": str(project_root / relpath)}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is False, (
            f"a write to {relpath} must be denied immediately after a re-open: {result}"
        )
        assert "[ENF-GATE-PLAN]" in (result["reason"] or "")

    def test_write_becomes_allowed_once_both_gates_are_reapproved_against_the_revised_plan(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("strict-reapprove")
        self._reopen(tmp_path, monkeypatch, project_root, sid)

        # The revised plan the human wrote after the re-open, plus a test skeleton so
        # _validate_test_skeletons has something to find.
        _write_session_plan(project_root, sid, _REVISED_PLAN_BODY)
        (project_root / "tests").mkdir(exist_ok=True)
        (project_root / "tests" / "test_fixture.py").write_text(_TEST_SKELETON_BODY)

        for _ in ("phase-a", "test-skeletons"):
            token = write_bound_gate_token(sid)
            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_advance_phase, sid, str(project_root), token,
            )
            assert result["advanced"] is True, f"re-approval against the revised plan must succeed: {result}"

        cache_after = _read_cache(sid)
        envelope = {"tool_input": {"file_path": str(project_root / "src" / "thing.py")}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is True, (
            f"a source write must be allowed once both gates are reapproved: {result}"
        )


# =============================================================================
# Capability 7: plan.md / capabilities.md / exclusions writability around a re-open
# =============================================================================


class TestWritabilityAfterReopen:
    _EXCLUDED_RELPATHS = ["tests/test_thing.py", "migrations/0001_init.py", ".claude/notes.md"]

    def _reopen(self, tmp_path, monkeypatch, project_root: Path, sid: str) -> dict:
        _require(mode_engine, "reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        _seed_deadlocked_cache(project_root, sid)
        mode_engine.reopen_planning(sid)
        return _read_cache(sid)

    def test_plan_md_is_writable_immediately_after_reopen(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("writable-plan")
        cache_after = self._reopen(tmp_path, monkeypatch, project_root, sid)

        envelope = {"tool_input": {"file_path": str(project_root / "plan.md")}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is True, result

    def test_capabilities_md_stays_writable_after_reopen(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("writable-caps")
        cache_after = self._reopen(tmp_path, monkeypatch, project_root, sid)

        envelope = {"tool_input": {"file_path": str(project_root / "capabilities.md")}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is True, result

    @pytest.mark.parametrize("relpath", _EXCLUDED_RELPATHS)
    def test_excluded_paths_stay_writable_after_reopen(
        self, tmp_path, monkeypatch, sandbox_cwd, relpath: str,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("writable-excl-" + relpath.replace("/", "-"))
        cache_after = self._reopen(tmp_path, monkeypatch, project_root, sid)

        envelope = {"tool_input": {"file_path": str(project_root / relpath)}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is True, result

    def test_plan_md_is_refused_again_with_the_new_message_once_implementation_resumes(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("refused-again")
        self._reopen(tmp_path, monkeypatch, project_root, sid)

        _write_session_plan(project_root, sid, _REVISED_PLAN_BODY)
        (project_root / "tests").mkdir(exist_ok=True)
        (project_root / "tests" / "test_fixture.py").write_text(_TEST_SKELETON_BODY)
        for _ in ("phase-a", "test-skeletons"):
            token = write_bound_gate_token(sid)
            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_advance_phase, sid, str(project_root), token,
            )
            assert result["advanced"] is True, result

        cache_after = _read_cache(sid)
        assert cache_after["current_phase"] == "implementation"
        envelope = {"tool_input": {"file_path": str(project_root / "plan.md")}}
        result = gates_module._can_write_check(sid, envelope, str(SKILL_ROOT), cache_after)
        assert result["can_write"] is False, result
        reason = result["reason"] or ""
        assert "[ENF-GATE-PLAN]" in reason
        assert "replan approved" in reason


# =============================================================================
# Capability 8: the phrase is a no-op (state-unchanged) outside its firing window
# =============================================================================


class TestReplanGuardedByStateOutsideItsFiringWindow:
    """`cmd_reopen_planning` re-checks mode/phase/pending-gate authoritatively from
    the cache -- the guard is not bash-only -- so every one of these is exercised
    directly against the command, bypassing the hook."""

    def _mint_replan_token(self, sid: str, project_root: Path) -> str:
        _require(gate_token, "REPLAN_GATE")
        return write_bound_gate_token(sid, gate=gate_token.REPLAN_GATE)

    @pytest.mark.parametrize("mode,phase,drift", [
        pytest.param("debug", "implementation", False, id="mode-not-work"),
        pytest.param("work", "planning", False, id="phase-planning"),
        pytest.param("work", "testing", False, id="phase-testing"),
        pytest.param("work", "complete", False, id="phase-complete"),
        pytest.param("work", "implementation", True, id="gate-pending-drift-rearm"),
    ])
    def test_reopen_refused_and_state_unchanged(
        self, tmp_path, monkeypatch, sandbox_cwd, mode: str, phase: str, drift: bool,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("guarded-" + mode + "-" + phase + ("-drift" if drift else ""))

        if drift:
            _seed_drift_rearmed_implementation_cache(project_root, sid)
        else:
            _write_session_plan(project_root, sid)
            _seed_cache(
                sid, mode=mode, current_phase=phase,
                gates_approved=["phase-a", "test-skeletons"] if phase != "planning" else [],
                gates_approved_plan={}, project_root=str(project_root), denial_counts={},
            )

        with _mint_cleanup(sid):
            before = _read_cache(sid)
            token = self._mint_replan_token(sid, project_root)

            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_reopen_planning, sid, token,
            )

            after = _read_cache(sid)
            assert after["current_phase"] == before["current_phase"], (
                f"phase must not change on a refused re-open: {result}"
            )
            assert after["gates_approved"] == before["gates_approved"], (
                f"gates_approved must not change on a refused re-open: {result}"
            )
            # The refusal must be legible: SOME string field in the JSON must be
            # non-empty, so the hook has something to relay ("names what is actually
            # pending" per plan.md).
            reason_like = [v for v in result.values() if isinstance(v, str) and v]
            assert reason_like, f"a refusal must say why: {result}"


# =============================================================================
# Capability 9: FAIL-CLOSED on an unparseable/unknown state
# =============================================================================


class TestFailClosedOnUnknownState:
    @pytest.fixture(autouse=True)
    def _seed_where_the_hook_will_look(self, tmp_path, monkeypatch):
        """Same reason as TestHookNoGateBranchesEmitTheReplanHint's fixture: the seeding
        happens here, the reading happens in the subprocess, and they must agree on the
        cache dir. It matters MOST in this class, because without it the positive control
        below fails closed for the same reason the fail-closed test passes, and the pair
        stops discriminating anything.
        """
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)

    def test_known_good_state_reaches_the_hook_and_actually_reopens(
        self, tmp_path, sandbox_cwd,
    ) -> None:
        """Positive control for the fail-closed test below: proves the replan tier
        genuinely reaches the hook's reset arm and performs a real reset end-to-end
        (through the real CLI subprocess the hook spawns), so the fail-closed test's
        "nothing happened" is meaningfully different from "the tier never fires"."""
        project_root = sandbox_cwd
        sid = _sid("known-good")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        _seed_deadlocked_cache(project_root, sid)

        with _mint_cleanup(sid):
            _run_auto_approve_hook(
                {"session_id": sid, "prompt": "replan approved"},
                cwd=project_root, extra_env=cache_env,
            )

            after = _read_cache(sid)
            assert after["current_phase"] == "planning", (
                "a known-good work/implementation/no-gate-pending session must "
                f"actually be reset by the real hook; cache after={after}"
            )
            assert after["gates_approved"] == []

    def test_unseen_session_fails_closed_through_the_hook_and_reopens_nothing(
        self, tmp_path, sandbox_cwd,
    ) -> None:
        """An unknown session (no cache file has ever been written for it) reads back
        mode=None from cmd_current_phase, which is an UNPARSEABLE/UNKNOWN state per
        plan.md's fail-closed rule -- not "mode is not work" (a KNOWN, different
        mode), but no known mode at all. No reset may happen, and the hook must not
        report one it cannot confirm."""
        project_root = sandbox_cwd
        sid = _sid("unseen")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        # Deliberately: no _seed_cache call. This session id has no cache file.

        stdout = _run_auto_approve_hook(
            {"session_id": sid, "prompt": "replan approved"},
            cwd=project_root, extra_env=cache_env,
        )

        assert "planning" not in stdout.lower() or "reopen" not in stdout.lower(), (
            f"an unknown state must not be reported as a successful re-open: {stdout!r}"
        )
        after = _read_cache(sid)
        assert after.get("mode") is None, (
            "an unknown session's mode must still read as None; a reset must not "
            "have run against it"
        )
        assert not os.path.exists(gate_token.gate_token_path(sid)), (
            "a fail-closed unknown state must leave no token on disk"
        )


# =============================================================================
# Capability 10: a replan-bound token authorizes nothing else
# =============================================================================


class TestReplanTokenIsUnusableElsewhere:
    def test_phase_advance_refuses_a_replan_bound_token_as_gate_mismatch(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(gate_token, "REPLAN_GATE")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("mismatch-advance")

        with _mint_cleanup(sid):
            _write_session_plan(project_root, sid)
            plan_hash = plan_md_hash(str(project_root), sid)
            _seed_cache(
                sid, mode="work", current_phase="planning",
                gates_approved=[], gates_approved_plan={}, project_root=str(project_root),
            )
            token = gate_token.mint_gate_token(sid, gate=gate_token.REPLAN_GATE, plan_hash=plan_hash)

            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_advance_phase, sid, str(project_root), token,
            )
            assert result["advanced"] is False, (
                f"a replan-bound token must never advance a phase gate: {result}"
            )
            # Non-destructive: gate_binding_refusal runs BEFORE the claim.
            assert os.path.exists(gate_token.gate_token_path(sid)), (
                "a gate-mismatch refusal must not consume the token"
            )

    @pytest.mark.asyncio
    async def test_promote_candidate_refuses_a_replan_bound_token_for_a_non_empty_gate_line(
        self, monkeypatch,
    ) -> None:
        _require(gate_token, "REPLAN_GATE")
        import writ.server as server_module
        from writ.server import SessionPromoteCandidateRequest
        from writ.server.routes.gate import session_promote_candidate

        monkeypatch.setattr(server_module, "_db", object())
        monkeypatch.setattr(server_module, "_pipeline", object())

        sid = _sid("mismatch-promote")
        with _mint_cleanup(sid):
            token = gate_token.mint_gate_token(sid, gate=gate_token.REPLAN_GATE, plan_hash="abc123def456")

            result = await session_promote_candidate(
                sid, SessionPromoteCandidateRequest(candidate_id="cand-1", token=token),
            )
            assert result.get("promoted") is False
            assert gate_token.REPLAN_GATE in (result.get("error") or "")
            assert os.path.exists(gate_token.gate_token_path(sid)), (
                "the promote-candidate route's gate-bound refusal must not consume the token"
            )

    def test_bash_mint_literal_for_the_replan_gate_matches_the_python_constant(self) -> None:
        """TEXT-based, not functional, and that is deliberate: a SUCCESSFUL reopen
        mints and claims the token inside the same hook process before printing
        anything (capability 11), so there is nothing left on disk afterward for a
        subprocess-and-inspect test to compare. The only way to confirm the bash
        literal is identical to gate_token.REPLAN_GATE is to read the source line
        that writes it."""
        _require(gate_token, "REPLAN_GATE")
        hook_text = AUTO_APPROVE_GATE_SH.read_text()
        match = re.search(
            r'replan\).*?write_gate_token_file\s+"[^"]*"\s+"[^"]*"\s+"([^"]+)"',
            hook_text, re.DOTALL,
        )
        assert match, (
            "no `replan)` case arm minting a token via write_gate_token_file with a "
            "literal gate argument was found in auto-approve-gate.sh"
        )
        assert match.group(1) == gate_token.REPLAN_GATE, (
            f"the bash mint's literal gate argument ({match.group(1)!r}) must equal "
            f"the python REPLAN_GATE constant ({gate_token.REPLAN_GATE!r})"
        )


# =============================================================================
# Capability 11: token lifecycle by outcome
# =============================================================================


class TestReplanTokenLifecycle:
    def _seed_reopenable(self, project_root: Path, sid: str) -> None:
        _seed_deadlocked_cache(project_root, sid)

    def test_successful_reopen_consumes_the_token_leaving_no_file(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        _require(gate_token, "REPLAN_GATE")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("lifecycle-success")
        self._seed_reopenable(project_root, sid)

        with _mint_cleanup(sid):
            token = write_bound_gate_token(sid, gate=gate_token.REPLAN_GATE)
            assert os.path.exists(gate_token.gate_token_path(sid)), "precondition: mint must land on disk"

            _call_stdin_safe_json(monkeypatch, approval_workflow.cmd_reopen_planning, sid, token)

            assert _read_cache(sid)["current_phase"] == "planning", "the reopen must actually have run"
            assert not os.path.exists(gate_token.gate_token_path(sid)), (
                "a successful re-open must leave no token file"
            )

    def test_a_guard_refusal_after_a_replan_bound_mint_still_consumes_the_token(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        """The command's OWN guard (not bash's) refuses here: phase is `planning`,
        where plan.md is already writable and phase-a is pending, so a reset would
        achieve nothing. A replan-bound token authorizes only the thing just refused,
        so it must be consumed explicitly rather than left spendable."""
        _require(approval_workflow, "cmd_reopen_planning")
        _require(gate_token, "REPLAN_GATE")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("lifecycle-guard-refusal")
        _write_session_plan(project_root, sid)
        _seed_cache(
            sid, mode="work", current_phase="planning",
            gates_approved=[], gates_approved_plan={}, project_root=str(project_root),
        )

        with _mint_cleanup(sid):
            token = write_bound_gate_token(sid, gate=gate_token.REPLAN_GATE)
            assert os.path.exists(gate_token.gate_token_path(sid)), "precondition: mint must land on disk"

            result = _call_stdin_safe_json(monkeypatch, approval_workflow.cmd_reopen_planning, sid, token)

            assert _read_cache(sid)["current_phase"] == "planning", (
                f"a guard refusal must not itself change state: {result}"
            )
            assert not os.path.exists(gate_token.gate_token_path(sid)), (
                "a guard refusal after a replan-bound mint must still consume the token"
            )

    def test_an_invalid_token_leaves_an_existing_token_file_untouched_and_logs_self_approval_blocked(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("lifecycle-invalid-token")
        self._seed_reopenable(project_root, sid)

        events: list[str] = []
        monkeypatch.setattr(
            approval_workflow, "_log_friction_event",
            lambda session_id, mode, event, **extra: events.append(event),
        )

        with _mint_cleanup(sid):
            real_token = write_bound_gate_token(sid, gate=gate_token.REPLAN_GATE)
            before_bytes = Path(gate_token.gate_token_path(sid)).read_bytes()

            bogus_token = "not-the-real-token"
            result = _call_stdin_safe_json(
                monkeypatch, approval_workflow.cmd_reopen_planning, sid, bogus_token,
            )

            assert _read_cache(sid)["current_phase"] == "implementation", (
                f"an invalid token must not perform any reset: {result}"
            )
            assert Path(gate_token.gate_token_path(sid)).read_bytes() == before_bytes, (
                "an invalid-token call must leave the existing (unrelated, still-valid) "
                "token file byte-for-byte untouched"
            )
            assert "agent_self_approval_blocked" in events, (
                f"an invalid token must log agent_self_approval_blocked; got {events}"
            )
            # real_token is still on disk (asserted above); silence unused-var lint.
            assert real_token

    def test_a_token_bound_to_a_real_phase_gate_is_refused_without_being_claimed(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        project_root = sandbox_cwd
        sid = _sid("lifecycle-real-gate-bound")
        self._seed_reopenable(project_root, sid)

        with _mint_cleanup(sid):
            # A token bound to a REAL phase gate (as a genuine phase approval would
            # mint), not to "replan" -- the user's actual pending-gate approval.
            token = gate_token.mint_gate_token(sid, gate="phase-a", plan_hash="whatever-plan-hash")

            result = _call_stdin_safe_json(monkeypatch, approval_workflow.cmd_reopen_planning, sid, token)

            assert _read_cache(sid)["current_phase"] == "implementation", (
                f"a phase-gate-bound token must never trigger a reset: {result}"
            )
            assert os.path.exists(gate_token.gate_token_path(sid)), (
                "a token bound to a real phase gate must be refused WITHOUT being "
                "claimed -- it may still authorize the user's genuine phase approval"
            )


# =============================================================================
# Capability 12: the audit trail
# =============================================================================


class TestAuditTrailForReopen:
    @pytest.fixture(autouse=True)
    def _let_the_typed_streams_separate(self, monkeypatch):
        """Undo conftest's WRIT_FRICTION_LOG for this class only.

        `logging.emit` collapses EVERY typed stream into that one file when the variable is
        set (documented at writ/shared/logging.py:19, for back-compat with the suite's
        isolation fixture). These two tests assert that a governance row lands on the AUDIT
        stream specifically, which is unobservable while the collapse is active: registering
        the events as audit cannot help, and bypassing the collapse would break
        tests/test_friction_isolation.py. The delenv is the convention the rest of the suite
        uses for exactly this.
        """
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)

    def test_successful_reopen_writes_one_plan_reopened_audit_row_and_a_phase_transition(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        _require(gate_token, "REPLAN_GATE")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "replan-audit-project")
        project_root = sandbox_cwd
        sid = _sid("audit-success")
        _seed_deadlocked_cache(project_root, sid)

        with _mint_cleanup(sid):
            token = write_bound_gate_token(sid, gate=gate_token.REPLAN_GATE)
            _call_stdin_safe_json(monkeypatch, approval_workflow.cmd_reopen_planning, sid, token)

        rows = _read_stream_rows("replan-audit-project", "audit")
        matches = [r for r in rows if r.get("event") == "plan_reopened"]
        assert len(matches) == 1, f"expected exactly one plan_reopened audit row, got {rows}"
        row = matches[0]
        assert row.get("from_phase") == "implementation", row
        assert row.get("to_phase") == "planning", row
        assert row.get("gates_cleared") == ["phase-a", "test-skeletons"], row
        assert row.get("confirmation_source") == "pattern", row
        assert "replan approved" in row.values(), (
            f"the matched phrase must appear somewhere in the audit row: {row}"
        )

        transitions = _read_cache(sid).get("phase_transitions", [])
        assert transitions, "a successful reopen must append a phase_transitions record"
        assert transitions[-1]["trigger"] == "user-replan", transitions[-1]

    def test_a_refusal_writes_plan_reopen_refused_with_a_named_reason(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        _require(approval_workflow, "cmd_reopen_planning")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "replan-audit-refusal-project")
        project_root = sandbox_cwd
        sid = _sid("audit-refused")
        # mode != work: an unambiguous, cheaply-constructed refusal case.
        _write_session_plan(project_root, sid)
        _seed_cache(
            sid, mode="debug", current_phase="implementation",
            gates_approved=[], gates_approved_plan={}, project_root=str(project_root),
        )

        with _mint_cleanup(sid):
            token = gate_token.mint_gate_token(sid, gate="replan", plan_hash="")
            _call_stdin_safe_json(monkeypatch, approval_workflow.cmd_reopen_planning, sid, token)

        rows = _read_stream_rows("replan-audit-refusal-project", "audit")
        matches = [r for r in rows if r.get("event") == "plan_reopen_refused"]
        assert len(matches) == 1, f"expected exactly one plan_reopen_refused audit row, got {rows}"
        reason = matches[0].get("reason")
        named_set = {
            "not_work_mode", "not_implementation", "gate_pending", "state_unknown",
            "token_invalid", "token_not_replan", "token_claimed",
        }
        assert reason in named_set, f"reason {reason!r} must be one of {named_set}"


# =============================================================================
# Capability 13: the rewritten [ENF-GATE-PLAN] implementation-phase message
# =============================================================================


class TestImplementationPhaseRefusalMessage:
    def test_the_enf_gate_plan_message_names_the_phrase_and_drops_the_invalidate_advice(self) -> None:
        result = gates_module._check_special_files("plan.md", "work", "implementation")
        assert result is not None and result["can_write"] is False, result
        reason = result["reason"] or ""
        assert "[ENF-GATE-PLAN]" in reason
        assert "replan approved" in reason, (
            f"the refusal must name the phrase verbatim: {reason!r}"
        )
        assert "invalidate" not in reason.lower(), (
            f"the refusal must not name invalidate-gate as an escape: {reason!r}"
        )
        assert "only the user" in reason.lower() or "only you" in reason.lower(), (
            f"the refusal must say only the user can use the phrase: {reason!r}"
        )
        assert "clear" in reason.lower(), (
            f"the refusal must say the phrase clears both gates: {reason!r}"
        )


# =============================================================================
# Capability 14: the three no-gate hook branches, the separate unreachable branch,
# and the deleted misleading sentence
# =============================================================================


class TestHookNoGateBranchesEmitTheReplanHint:
    @pytest.fixture(autouse=True)
    def _seed_where_the_hook_will_look(self, tmp_path, monkeypatch):
        """Point the PYTEST process at the same cache dir the hook subprocess gets.

        Every test here hands the subprocess `extra_env={"WRIT_CACHE_DIR": tmp_path/"cache"}`
        but seeds the session from this process, and conftest.py points this process at a
        session-wide mkdtemp instead. Without this line the hook reads an unknown session
        and correctly fails closed, so the seeded work/implementation state could never
        reach the branch under test and every assertion here would be vacuous.
        """
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)

    def test_embedded_no_gate_branch_names_the_replan_phrase(
        self, tmp_path, sandbox_cwd,
    ) -> None:
        project_root = sandbox_cwd
        sid = _sid("hint-embedded")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        _seed_deadlocked_cache(project_root, sid)  # no gate pending, mode=work, phase=implementation

        stdout = _run_auto_approve_hook(
            {"session_id": sid, "prompt": "ok remember we fixed the findings, approved"},
            cwd=project_root, extra_env=cache_env,
        )
        assert "replan approved" in stdout, (
            f"the embedded no-gate branch must name the replan phrase: {stdout!r}"
        )

    def test_server_reported_noop_branch_names_the_replan_phrase(
        self, tmp_path, monkeypatch, sandbox_cwd,
    ) -> None:
        """GATE_PENDING is true LOCALLY (a plan-drift re-arm during implementation --
        see plan.md's Analysis), but the server's own advance-phase check disagrees
        and reports nothing to advance. That authoritative "nothing pending" answer is
        what the noop branch's hint fires on."""
        project_root = sandbox_cwd
        sid = _sid("hint-noop")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        _seed_drift_rearmed_implementation_cache(project_root, sid)
        transcript_path = write_evidence_transcript(tmp_path)

        with _mint_cleanup(sid):
            with _mock_advance_daemon(b'{"advanced": false, "reason": "No gates for this mode"}') as port:
                stdout = _run_auto_approve_hook(
                    {"session_id": sid, "prompt": "approved", "transcript_path": transcript_path},
                    cwd=project_root,
                    extra_env={**cache_env, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(port)},
                )
        assert "replan approved" in stdout, (
            f"the server-reported-noop branch must name the replan phrase: {stdout!r}"
        )

    def test_not_attempted_branch_names_the_replan_phrase(
        self, tmp_path, sandbox_cwd,
    ) -> None:
        """No gate pending locally (both approved, no drift) -> the exact tier never
        even attempts an HTTP call. This must be distinguishable from a genuine
        daemon-unreachable case (the next test) and must still carry the hint."""
        project_root = sandbox_cwd
        sid = _sid("hint-not-attempted")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        _seed_deadlocked_cache(project_root, sid)
        transcript_path = write_evidence_transcript(tmp_path)

        with _mint_cleanup(sid):
            stdout = _run_auto_approve_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": transcript_path},
                cwd=project_root, extra_env=cache_env,
            )
        assert "replan approved" in stdout, (
            f"the not-attempted branch must name the replan phrase: {stdout!r}"
        )

    def test_daemon_unreachable_branch_is_its_own_message_naming_the_restart_command(
        self, tmp_path, sandbox_cwd,
    ) -> None:
        """GATE_PENDING true (a drift re-arm during implementation), but the daemon
        genuinely does not answer -- distinct from the noop case above, which gets a
        real response saying nothing is pending. This must be its OWN branch naming
        the daemon restart, not the replan hint."""
        project_root = sandbox_cwd
        sid = _sid("hint-unreachable")
        cache_env = {"WRIT_CACHE_DIR": str(tmp_path / "cache")}
        os.makedirs(tmp_path / "cache", exist_ok=True)
        _seed_drift_rearmed_implementation_cache(project_root, sid)
        closed_port = _unused_port()
        transcript_path = write_evidence_transcript(tmp_path)

        with _mint_cleanup(sid):
            stdout = _run_auto_approve_hook(
                {"session_id": sid, "prompt": "approved", "transcript_path": transcript_path},
                cwd=project_root,
                extra_env={**cache_env, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(closed_port)},
            )
        assert "systemctl --user restart writ-server" in stdout, (
            f"the daemon-unreachable branch must name the restart command: {stdout!r}"
        )
        assert "replan approved" not in stdout, (
            "the daemon-unreachable branch is a distinct message and must not also "
            f"claim a replan hint applies: {stdout!r}"
        )

    def test_the_old_misleading_sentence_appears_nowhere_in_the_hook_source(self) -> None:
        """TEXT-based, and genuinely the only way: proving a string is ABSENT across
        an entire file cannot be established by running finitely many scenarios
        (there could always be one more branch that still prints it), so this is a
        direct read of the file plan.md says the sentence is deleted from."""
        hook_text = AUTO_APPROVE_GATE_SH.read_text()
        assert "otherwise this approval needs no gate action" not in hook_text


# =============================================================================
# Capability 15: the Bash write gate refuses `reopen-planning`, but not this test file
# =============================================================================


class TestBashWriteGateCoversReopenPlanning:
    def test_a_bash_command_naming_reopen_planning_is_denied(self, sandbox_cwd) -> None:
        sid = _sid("bashgate-deny")
        cmd = (
            f'python3 bin/lib/writ-session.py reopen-planning {sid} --token deadbeef'
        )
        output = _run_bash_write_gate(cmd, sid, sandbox_cwd)
        assert output is not None, "a command naming reopen-planning must be denied, not silently allowed"
        assert output.get("permissionDecision") == "deny"
        assert "[ENF-GATE-STATE]" in (output.get("permissionDecisionReason") or "")

    @pytest.mark.parametrize("prompt", ["replan approved", "approved"])
    def test_a_bash_command_invoking_the_minting_hook_is_denied(
        self, sandbox_cwd, prompt,
    ) -> None:
        """The hole the reviewer found, and the reason the subcommand pattern was not enough.

        Guarding `reopen-planning` closed the front door: an agent that never types that
        word can still run the approval hook itself with a forged stdin payload, and the
        hook mints a token and claims it in the same process. Reproduced before this guard
        existed: the phase went to planning and both gates cleared with no human turn, while
        the hook's own reply said "no agent self-approval".

        PARAMETRIZED OVER BOTH ARMS on purpose. The guard is on the INVOCATION, not on the
        replan arm, so it must also close the pre-existing exact-tier `approved` advance
        minted by the same script. That is what makes this a property of the hook rather
        than a patch for one phrase, and it is why the question "was the old path forgeable
        too?" no longer needs answering: either way it is unreachable from Bash now.
        """
        sid = _sid("bashgate-hook")
        payload = json.dumps({"session_id": sid, "prompt": prompt})
        cmd = f"echo '{payload}' | bash hooks/scripts/auto-approve-gate.sh"
        output = _run_bash_write_gate(cmd, sid, sandbox_cwd)
        assert output is not None, (
            "invoking the token-minting approval hook from Bash must be denied; "
            "this is the confirmed self-approval bypass"
        )
        assert output.get("permissionDecision") == "deny"
        assert "[ENF-GATE-STATE]" in (output.get("permissionDecisionReason") or "")

    def test_the_underscored_test_file_name_stays_allowed(self, sandbox_cwd) -> None:
        """The whole reason this skeleton's filename is underscored, not hyphenated:
        `tests/test_replan_reopen_planning.py` must never itself match the hyphenated
        `reopen-planning` STATE_MATCH pattern, so a pytest command naming this very
        file stays runnable after the pattern is added."""
        sid = _sid("bashgate-allow")
        cmd = "pytest tests/test_replan_reopen_planning.py -q"
        output = _run_bash_write_gate(cmd, sid, sandbox_cwd)
        assert output is None, (
            f"a pytest command naming this underscored test file must not be denied: {output}"
        )
