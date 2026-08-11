"""Session-scoped gate artifacts (plan.md Part 2, isolation cycle, plan_hash 033bb1595c2c).

Approval artifacts are moving from `<project_root>/.claude/gates/<gate>.approved` (shared
by every session working in the same project) to
`<project_root>/.claude/gates/<session_id>/<gate>.approved`. Today's code writes and reads
ONLY the flat path (writ/session/approval_workflow.py:516-520, and every one of the six
non-test readers this cycle touches), so session B's `mode set` can delete session A's
approval and B's own gate check can read A's approval as its own -- capabilities.md's own
words for it: "the reported interference".

Per ENF-SYS-005: the isolation claim is about concurrent OS-level actors sharing one
filesystem, so it is proven with REAL subprocesses (writ-session.py, check-gates.sh,
inject-tier-workflow.sh, writ-session-end.sh, writ-rag-inject.sh) against REAL files under
a REAL temp project directory. Nothing here mocks the filesystem, a session cache, or a
subprocess boundary.

Capability numbers below are this file's own 1-based index into capabilities.md, counted
from the top of that file (Part 1 -- already shipped as commit 34f975f -- occupies items
1-8; Part 3's doctrine-leak items open at 25):
    9  approval writes the session-scoped path and nothing at the flat path
   10  an invalid session-id component is refused by the writer, reads as no-approval
   11  two sessions each approving phase-a in the SAME project produce two artifacts
   12  session B's `mode set` does not delete session A's artifacts (real processes)
   13  two simultaneous advances for A and B each land, no lost write, no cross-write
   14  check-gates.sh --session A/B disagree from the same project at the same moment
   15  the writ-rag-inject.sh work-mode reminder is session-scoped
   20  SessionEnd removes only the ending session's gate directory
   23  gate token paths are unchanged at /tmp/writ-gate-token-<sid>
   24  the installed permission-deny globs still refuse a write to a session-scoped path
Item 16 (_derive_phase) lives in test_validate_rules_helper.py; items 17-19 (mode-init
no-op/clears-own-artifacts, the legacy sweep, and the session-subdirectory symlink escape)
live in test_gate_artifact_cleanup.py -- both per plan.md's own per-file annotation, so the
flat-artifact fixtures those files already build stay next to the code that seeds them.
Items 21-22 (bash/python path parity, no added python interpreter spawn) live in
test_gate_dir_bash_python_parity.py. Two readers with no separately numbered capability
item (inject-tier-workflow.sh, friction-logger.sh) still get dedicated classes below, since
the brief names all six readers as in scope and "matters most" is not "matters only".

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import contextlib
import fnmatch
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

# autouse: pins THIS PROCESS's own cwd to a throwaway sandbox (tests/fixtures/session_state.py).
# Every subprocess spawned below passes its own explicit cwd= into a tmp_path project, which
# ESCAPES that fixture by design -- an explicit cwd= is what makes a call a REAL, independent
# process rather than a call that happens to run inside this test process. The fixture is
# defense in depth for the handful of in-process helper calls this file makes directly
# (write_bound_gate_token, _write_cache): if one of them ever fell back to os.getcwd(), it
# would land in the sandbox, never in this repo's own .claude/gates.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.fixtures.session_state import write_bound_gate_token

REPO = Path(__file__).resolve().parent.parent
WRIT_SESSION = REPO / "bin" / "lib" / "writ-session.py"
CHECK_GATES = REPO / "bin" / "check-gates.sh"
RAG_INJECT = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
SESSION_END = REPO / "hooks" / "scripts" / "writ-session-end.sh"
INJECT_TIER_WORKFLOW = REPO / "hooks" / "scripts" / "inject-tier-workflow.sh"
FRICTION_LOGGER = REPO / "hooks" / "scripts" / "friction-logger.sh"

# An unbound port. conftest.py already points WRIT_PORT at the suite's own dedicated test
# daemon (8799), which may or may not be alive for this narrow a run; overriding it here
# forces every _writ_session/curl round trip in the hooks below to fail fast and fall back
# to the local CLI/file read, so these tests never depend on -- and can never cross-talk
# with -- a real daemon (mirrors test_session_end.py's own WRIT_PORT="19999" pattern).
# NOT used by TestWorkModeReminderIsSessionScoped: that hook hard-exits at its
# /prompt-bundle gate on an empty response (see the stub daemon below), so it needs a
# daemon that actually answers rather than one guaranteed unreachable.
UNREACHABLE_PORT = "19999"

# Plan.md content minimal enough for _validate_phase_a: one annotated ## Files bullet, an
# ## Analysis paragraph, "No matching rules" (skips the citation-hallucination check, so no
# loaded_rule_ids need seeding), and one unchecked ## Capabilities box. Per TEST-FIXTURE-002:
# only what phase-a validation actually reads, nothing else.
MINIMAL_PLAN_MD = """\
## Files

- `src/thing.py` (create) -- the thing

## Analysis

Adds the thing.

## Rules Applied

No matching rules.

## Capabilities

- [ ] the thing works
"""


def _sid(label: str) -> str:
    """A fresh, collision-proof session id.

    Real gate tokens live at /tmp/writ-gate-token-<sid> (gate_token.py); a uuid4 suffix
    keeps every id minted here from ever matching a real session's -- including the one
    live token this repo's own approved cycle currently depends on.
    """
    return f"isotest-{label}-{uuid.uuid4().hex[:12]}"


def _env(cache_dir) -> dict:
    return {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_PORT": UNREACHABLE_PORT,
    }


def _flat_phase_a(project_root: Path) -> Path:
    return project_root / ".claude" / "gates" / "phase-a.approved"


def _session_phase_a(project_root: Path, session_id: str) -> Path:
    return project_root / ".claude" / "gates" / session_id / "phase-a.approved"


@pytest.fixture()
def two_sessions(tmp_path):
    """One real project root (a real plan.md included) and two fresh session ids that
    will both work inside it -- the shape of two Claude Code instances in one repo."""
    project_root = tmp_path / "shared-project"
    (project_root / ".git").mkdir(parents=True)
    (project_root / "plan.md").write_text(MINIMAL_PLAN_MD)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return project_root, cache_dir, _sid("a"), _sid("b")


def _seed_work_cache(cache_dir, session_id: str, project_root, **overrides) -> None:
    """Write a session cache directly via the real `_write_cache` (the same writer
    production code uses, not a hand-rolled dict on disk): mode=work, planning phase, no
    gate approved yet, project_root stamped as `_apply_mode_set` would stamp it.
    `recall_briefed=True` keeps the once-per-session recall branch in writ-rag-inject.sh
    from firing a network call this test does not care about.
    """
    from writ.session.cache import _write_cache

    cache = {
        "mode": "work",
        "current_phase": "planning",
        "gates_approved": [],
        "project_root": str(project_root),
        "loaded_rule_ids": [],
        "recall_briefed": True,
    }
    cache.update(overrides)
    os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
    _write_cache(session_id, cache)


def _mint_token(cache_dir, session_id: str) -> str:
    """The bound token a genuine `auto-approve-gate.sh` approval would mint, derived from
    the session's own already-seeded cache (tests/fixtures/session_state.py)."""
    os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
    return write_bound_gate_token(session_id)


def _advance(session_id, project_root, token, cache_dir, prompt: str = "approved"):
    return subprocess.run(
        [sys.executable, str(WRIT_SESSION), "advance-phase", session_id,
         "--project-root", str(project_root), "--token", token],
        input=prompt, capture_output=True, text=True, timeout=30,
        env=_env(cache_dir), cwd=str(project_root),
    )


def _mode_set(session_id, mode, project_root, cache_dir):
    return subprocess.run(
        [sys.executable, str(WRIT_SESSION), "mode", "set", mode, session_id],
        capture_output=True, text=True, timeout=30,
        env=_env(cache_dir), cwd=str(project_root),
    )


def _check_gates(session_id, project_root, cache_dir) -> dict:
    proc = subprocess.run(
        ["bash", str(CHECK_GATES), "--session", session_id, str(project_root)],
        capture_output=True, text=True, timeout=30, env=_env(cache_dir), cwd=str(project_root),
    )
    assert proc.returncode == 0, f"check-gates.sh aborted: {proc.stderr}"
    return json.loads(proc.stdout)


def _approve_phase_a(session_id, project_root, cache_dir) -> dict:
    """The full real-process path a genuine approval takes: seed the cache, mint the
    bound token auto-approve-gate.sh would mint, then a REAL `advance-phase` subprocess
    (never an in-process call standing in for one)."""
    _seed_work_cache(cache_dir, session_id, project_root)
    token = _mint_token(cache_dir, session_id)
    proc = _advance(session_id, project_root, token, cache_dir)
    assert proc.returncode == 0, f"advance-phase crashed: {proc.stderr}"
    result = json.loads(proc.stdout)
    assert result.get("advanced") is True, (
        f"the real approval this test's setup depends on did not go through: {result}"
    )
    return result


# A throwaway stdlib HTTP daemon stand-in, copied down (self-contained, not imported
# across test modules) from tests/test_no_tool_prereqs.py's own `_StubHandler`/
# `stub_daemon` -- the established shape in this suite for driving writ-rag-inject.sh
# past its daemon calls without touching a real Writ server. Only
# TestWorkModeReminderIsSessionScoped needs it: that hook hard-exits at its
# /prompt-bundle gate ("[Writ: server unavailable, proceeding without rules]", line
# ~500-503) when nothing answers, and the work-mode reminder this test targets sits far
# below that gate.
class _StubHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        route = self.routes.get(self.path.split("?", 1)[0])
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        status, payload = route
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, *_a):
        pass


@contextlib.contextmanager
def _stub_daemon(routes: dict):
    """routes: {path: (status_code, json_body)}. Yields (host, port)."""
    handler = type("Handler", (_StubHandler,), {"routes": dict(routes)})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", str(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Capability 9
# ---------------------------------------------------------------------------


class TestApprovalArtifactIsSessionScoped:
    """Capability 9: a real advance-phase process writes under the session's OWN
    subdirectory, and the flat path -- shared by every session in the repo -- gets
    nothing.

    RED reason: approval_workflow.py:516-520 today unconditionally writes
    `<project_root>/.claude/gates/<gate>.approved`, with no session component in the path
    at all. Both tests below fail against that code: the session-scoped file the first
    looks for was never created, and the flat file the second says must NOT exist is
    exactly the file line 518 just wrote.
    """

    def test_advance_writes_the_session_scoped_path(self, two_sessions):
        project_root, cache_dir, sid_a, _sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)

        artifact = _session_phase_a(project_root, sid_a)
        gates_dir = project_root / ".claude" / "gates"
        assert artifact.is_file(), (
            f"expected the approval at {artifact}, session-scoped under its own id; "
            f"gates dir holds: "
            f"{sorted(str(p.relative_to(gates_dir)) for p in gates_dir.rglob('*')) if gates_dir.exists() else '<missing>'}"
        )
        assert artifact.read_text().strip() == sid_a

    def test_advance_writes_nothing_at_the_flat_path(self, two_sessions):
        project_root, cache_dir, sid_a, _sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)

        assert not _flat_phase_a(project_root).exists(), (
            "the flat, session-less path still received the approval -- every other "
            "session working in this project would read it as its own"
        )


# ---------------------------------------------------------------------------
# Capability 10
# ---------------------------------------------------------------------------


class TestInvalidSessionIdComponent:
    """Capability 10: the session-id path component is validated as
    `^[A-Za-z0-9._-]{1,128}$` (plan.md ## Analysis, Part 2). A caller with a bad id gets
    no gate path at all, and no reader can raise trying to check one.

    RED reason: `writ.session.locators` has no `gate_dir`, `gate_artifact_path`, or
    `is_valid_session_component` today -- every test below fails on the first call
    (AttributeError/ImportError), which is the correct RED for a capability whose
    implementation does not exist yet. `gate_dir`/`gate_artifact_path` are the exact
    names plan.md's ## Files section gives for the new locators; the validator name is
    this test's own choice (plan.md names only its behavior, not a symbol) -- an
    implementer is free to rename it as long as both locators route through it.
    """

    GOOD_IDS = ["abc123", "a.b-c_d", "A" * 128, "sid.2026-08-10"]
    BAD_IDS = ["", "a/b", "../escape", "a" * 129, "with space", "semi;colon", "a\nb", ".."]

    @pytest.mark.parametrize("session_id", GOOD_IDS)
    def test_a_wellformed_id_is_accepted(self, session_id, tmp_path):
        from writ.session.locators import gate_dir, is_valid_session_component

        assert is_valid_session_component(session_id) is True
        path = gate_dir(str(tmp_path), session_id)
        assert path == os.path.join(str(tmp_path), ".claude", "gates", session_id)

    @pytest.mark.parametrize("session_id", BAD_IDS)
    def test_a_malformed_id_gets_no_path(self, session_id, tmp_path):
        """The writer's half of the refusal: a rejected id never becomes a path string,
        so nothing downstream can accidentally write through it."""
        from writ.session.locators import (
            gate_artifact_path,
            gate_dir,
            is_valid_session_component,
        )

        assert is_valid_session_component(session_id) is False, (
            f"{session_id!r} must fail the [A-Za-z0-9._-]{{1,128}} check"
        )
        assert gate_dir(str(tmp_path), session_id) == ""
        assert gate_artifact_path(str(tmp_path), session_id, "phase-a") == ""

    @pytest.mark.parametrize("session_id", BAD_IDS)
    def test_a_malformed_id_reads_as_no_approval_never_raises(self, session_id, tmp_path):
        """The reader's half: checking a bad id's approval answers False/absent, never
        throws. A hook that crashes on a malformed id fails OPEN under `set -e` -- the
        exception aborts the script before the gate check it was trying to run, which is
        the wrong direction for a human-oversight boundary."""
        from writ.session.locators import gate_artifact_path

        path = gate_artifact_path(str(tmp_path), session_id, "phase-a")
        assert path == "" or not os.path.exists(path)


# ---------------------------------------------------------------------------
# Capabilities 11, 12, 13, 14
# ---------------------------------------------------------------------------


class TestTwoSessionsSameProjectDoNotShareApprovals:
    """Capabilities 11, 12, 13, 14: the isolation claim itself, against real files.

    Every test below drives writ-session.py and check-gates.sh as SEPARATE OS processes
    (subprocess.run/Popen, never an in-process function call standing in for one), against
    ONE real project directory on disk, per ENF-SYS-005: this is a claim about concurrent
    actors and a filesystem, and a mock of either would prove nothing about it.
    """

    def test_a_approving_leaves_b_unapproved(self, two_sessions):
        """Capabilities 11 + 14.

        RED reason: approval_workflow.py writes ONE flat file no matter which session
        approves, so check-gates.sh --session B reads A's approval as its own -- both
        calls answer `phase-a: true` today, not `true` / `false`.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)

        status_a = _check_gates(sid_a, project_root, cache_dir)
        status_b = _check_gates(sid_b, project_root, cache_dir)

        assert status_a["gates"]["phase-a"] is True, status_a
        assert status_b["gates"]["phase-a"] is False, (
            f"session B read session A's approval as its own: {status_b}"
        )

    def test_mode_set_in_b_does_not_delete_a(self, two_sessions):
        """Capability 12, "the reported interference" in capabilities.md's own words.

        RED reason: `_clear_gate_artifacts` (mode_engine.py:197-237) globs
        `<project_root>/.claude/gates/*.approved` with no session filter, so B's real
        `mode set` subprocess deletes the one flat file regardless of whose approval it
        is. A's status flips from true to false.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)
        assert _check_gates(sid_a, project_root, cache_dir)["gates"]["phase-a"] is True

        _seed_work_cache(cache_dir, sid_b, project_root)
        proc = _mode_set(sid_b, "investigate", project_root, cache_dir)
        assert proc.returncode == 0, proc.stderr

        status_a = _check_gates(sid_a, project_root, cache_dir)
        assert status_a["gates"]["phase-a"] is True, (
            f"session B's mode set deleted session A's approval: {status_a}"
        )

    def test_simultaneous_advance_from_a_and_b_both_land(self, two_sessions):
        """Capability 13.

        RED reason: both processes target the SAME flat `phase-a.approved` file today, so
        the second writer's `open(path, "w")` truncates the first's write -- one whole
        session's approval is silently lost, not merely delayed.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        _seed_work_cache(cache_dir, sid_a, project_root)
        _seed_work_cache(cache_dir, sid_b, project_root)
        token_a = _mint_token(cache_dir, sid_a)
        token_b = _mint_token(cache_dir, sid_b)

        env = _env(cache_dir)
        proc_a = subprocess.Popen(
            [sys.executable, str(WRIT_SESSION), "advance-phase", sid_a,
             "--project-root", str(project_root), "--token", token_a],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(project_root),
        )
        proc_b = subprocess.Popen(
            [sys.executable, str(WRIT_SESSION), "advance-phase", sid_b,
             "--project-root", str(project_root), "--token", token_b],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(project_root),
        )
        out_a, err_a = proc_a.communicate(input="approved", timeout=30)
        out_b, err_b = proc_b.communicate(input="approved", timeout=30)
        assert proc_a.returncode == 0, err_a
        assert proc_b.returncode == 0, err_b
        result_a = json.loads(out_a)
        result_b = json.loads(out_b)
        assert result_a.get("advanced") is True, result_a
        assert result_b.get("advanced") is True, result_b

        gates_dir = project_root / ".claude" / "gates"
        artifact_a = _session_phase_a(project_root, sid_a)
        artifact_b = _session_phase_a(project_root, sid_b)
        listing = sorted(str(p.relative_to(gates_dir)) for p in gates_dir.rglob("*")) if gates_dir.exists() else []
        assert artifact_a.is_file() and artifact_a.read_text().strip() == sid_a, (
            f"session A's concurrent approval did not land at its own path: {listing}"
        )
        assert artifact_b.is_file() and artifact_b.read_text().strip() == sid_b, (
            f"session B's concurrent approval did not land at its own path: {listing}"
        )


# ---------------------------------------------------------------------------
# Capability 15
# ---------------------------------------------------------------------------


class TestWorkModeReminderIsSessionScoped:
    """Capability 15: writ-rag-inject.sh's work-mode reminder (lines ~680-696) reads
    `<project_root>/.claude/gates/phase-a.approved` straight off disk today, with no
    session component -- so a project-wide flat approval (from ANY session) silences the
    reminder for a session that has approved nothing itself.

    The hook hard-exits much earlier ("[Writ: server unavailable, proceeding without
    rules]", line ~500-503) when its /prompt-bundle call gets no response, so this class
    runs it against a real ephemeral HTTP daemon (see `_stub_daemon` above) instead of an
    unreachable port -- otherwise every test here would fail at the wrong gate and never
    exercise the work-mode reminder at all.
    """

    @staticmethod
    def _routes(session_id: str) -> dict:
        """The minimal healthy daemon response set needed to reach section 9b (the
        work-mode reminder), copied down from test_no_tool_prereqs.py's own
        `_rag_inject_routes` -- the shape this suite already established for a healthy
        /prompt-bundle response -- with mode="work" and recall_briefed=True for this
        test's scenario."""
        return {
            f"/session/{session_id}": (200, {
                "mode": "work", "loaded_rule_ids": [], "remaining_budget": 8000,
                "is_orchestrator": False, "recall_briefed": True,
            }),
            f"/session/{session_id}/should-skip": (200, {"known": True, "should_skip": False}),
            f"/session/{session_id}/check-escalation": (200, {"needed": False}),
            "/prompt-bundle": (200, {
                "error": False,
                "always_on_block": "", "rules_text": "", "methodology_block": "",
                "nudge": "",
                "broad_meta": {"cost": 0, "rule_ids": []},
                "ao_meta": {"tokens": 0, "count": 0, "rule_ids": []},
                "method_meta": {"cost": 0, "rule_ids": [], "query_source": "methodology"},
            }),
        }

    def _envelope(self, session_id: str) -> str:
        # A deliberately neutral prompt: it must classify to no mode hint at all
        # (writ_mode_hint.classify_mode_hint returns None for it), so the auto-route /
        # mid-session re-route block (out of scope here -- Part 6) never fires and
        # CURRENT_MODE comes only from the cache/stub this test seeded.
        return json.dumps({
            "session_id": session_id,
            "prompt": "just checking in, no changes needed today",
            "hook_event_name": "UserPromptSubmit",
        })

    def _run(self, session_id, project_root, cache_dir, host, port) -> subprocess.CompletedProcess:
        env = {**_env(cache_dir), "WRIT_HOST": host, "WRIT_PORT": port}
        return subprocess.run(
            ["bash", str(RAG_INJECT)], input=self._envelope(session_id),
            capture_output=True, text=True, timeout=30,
            env=env, cwd=str(project_root),
        )

    def test_reminder_still_fires_for_a_session_with_no_artifact_of_its_own(
        self, two_sessions
    ):
        """RED reason: a flat `phase-a.approved` -- exactly the shape today's real
        approval write leaves -- makes session B's disk check think B is PAST the plan
        gate, because it cannot tell A's approval from its own. B gets told
        "test-skeletons gate pending" (skipping the plan-gate warning it still needs)
        instead of "plan gate pending", which is arguably worse than silence: it reads
        as progress B never made. The reminder must name the plan gate for B regardless
        of what sid_a already did.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)  # writes today's flat file
        _seed_work_cache(cache_dir, sid_b, project_root, recall_briefed=True)

        with _stub_daemon(self._routes(sid_b)) as (host, port):
            proc = self._run(sid_b, project_root, cache_dir, host, port)
        assert proc.returncode == 0, proc.stderr
        assert "server unavailable" not in proc.stdout, (
            f"the stub daemon this test depends on was not actually reached; "
            f"stdout={proc.stdout!r}"
        )
        assert "plan gate pending" in proc.stdout, (
            f"session B saw no plan-gate reminder even though it approved nothing "
            f"itself; stdout={proc.stdout!r}"
        )


# ---------------------------------------------------------------------------
# Sixth and fifth readers with no separately numbered capability item
# ---------------------------------------------------------------------------


class TestInjectTierWorkflowChecksOwnSession:
    """One of the six non-test readers named in the brief: inject-tier-workflow.sh's
    already-approved short circuit (line ~122) checks
    `<project_root>/.claude/gates/phase-a.approved` today, with no session component,
    immediately after a `mode set work` Bash call succeeds.
    """

    def _envelope(self, session_id: str) -> str:
        return json.dumps({
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {
                "command": f"python3 bin/lib/writ-session.py mode set work {session_id}",
            },
            "tool_output": "set: work\n",
        })

    def _run(self, session_id, project_root, cache_dir) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(INJECT_TIER_WORKFLOW)], input=self._envelope(session_id),
            capture_output=True, text=True, timeout=30,
            env=_env(cache_dir), cwd=str(project_root),
        )

    def test_full_instructions_fire_for_a_session_with_no_artifact_of_its_own(
        self, two_sessions
    ):
        """RED reason: session A's real flat approval (today's actual write shape)
        satisfies the `-f .../phase-a.approved` check for session B too, so the hook
        exits silently instead of printing the workflow instructions B still needs after
        its own `mode set work`.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        _approve_phase_a(sid_a, project_root, cache_dir)

        proc = self._run(sid_b, project_root, cache_dir)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        ac_text = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "STOP. Do NOT write any code yet" in ac_text, (
            f"session B got no workflow instructions even though it has approved "
            f"nothing itself; stdout={proc.stdout!r}"
        )


class TestFrictionLoggerReadsOwnSessionGateDir:
    """The sixth non-test reader: friction-logger.sh's GATE_DIR (line ~105) is the flat
    `<project_root>/.claude/gates` today, so its gate_denied_then_approved event (Event 1)
    fires for ANY session once ANY session's `.approved` file exists on disk, keyed only
    by that session's OWN invalidation_history.
    """

    def test_event_does_not_fire_off_a_different_sessions_flat_approval(
        self, two_sessions, monkeypatch
    ):
        """RED reason: session B has an invalidation_history entry for phase-a (it was
        denied once) but never approved anything itself. Session A's real approval (the
        flat file today's code actually writes) satisfies the disk check anyway, so B's
        Stop hook logs "denied then approved" for an approval that was never B's.
        """
        project_root, cache_dir, sid_a, sid_b = two_sessions
        log_root = cache_dir.parent / "logs"
        log_project = "isotest-friction-project"
        monkeypatch.setenv("WRIT_LOG_ROOT", str(log_root))
        monkeypatch.setenv("WRIT_LOG_PROJECT", log_project)
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)

        _approve_phase_a(sid_a, project_root, cache_dir)

        from writ.session.cache import _write_cache

        os.environ["WRIT_CACHE_DIR"] = str(cache_dir)
        _write_cache(sid_b, {
            "mode": "work",
            "invalidation_history": {
                "phase-a": [{"ts": "2026-01-01T00:00:00Z", "reason": "test denial"}],
            },
            "phase_transitions": [],
            "phase_transitions_logged": 0,
        })

        env = {**_env(cache_dir), "WRIT_LOG_ROOT": str(log_root), "WRIT_LOG_PROJECT": log_project}
        env.pop("WRIT_FRICTION_LOG", None)
        payload = json.dumps({"stop_hook_active": False, "session_id": sid_b})
        proc = subprocess.run(
            ["bash", str(FRICTION_LOGGER)], input=payload, capture_output=True, text=True,
            env=env, cwd=str(project_root), timeout=30,
        )
        assert proc.returncode == 0, proc.stderr

        from writ.shared.logging import read_streams

        events = read_streams(log_project, ["audit"])
        matches = [
            e for e in events
            if e.get("event") == "gate_denied_then_approved" and e.get("session") == sid_b
        ]
        assert matches == [], (
            f"session B's Stop hook recorded a gate_denied_then_approved event off "
            f"session A's approval: {matches}"
        )


# ---------------------------------------------------------------------------
# Capability 20
# ---------------------------------------------------------------------------


class TestSessionEndRemovesOnlyItsOwnGateDirectory:
    """Capability 20: writ-session-end.sh's metrics loop reads
    `<project_root>/.claude/gates/*.approved` today (line ~59) with no session component,
    and nothing removes anything afterward -- there is no "this session's directory" to
    remove yet, because there is no session directory at all.

    RED reason: this test seeds the FUTURE session-scoped shape (Part 2 introduces it)
    directly and asserts the ending session's own directory is gone afterward. Today's
    hook has no code path that touches a session subdirectory, so it survives untouched
    and the core assertion fails.
    """

    def test_ending_session_directory_is_removed_sibling_is_not(self, two_sessions):
        project_root, cache_dir, sid_a, sid_b = two_sessions
        gates = project_root / ".claude" / "gates"
        (gates / sid_a).mkdir(parents=True)
        (gates / sid_a / "phase-a.approved").write_text(sid_a + "\n")
        (gates / sid_b).mkdir(parents=True)
        (gates / sid_b / "phase-a.approved").write_text(sid_b + "\n")
        _seed_work_cache(cache_dir, sid_a, project_root)

        payload = json.dumps({"session_id": sid_a})
        proc = subprocess.run(
            ["bash", str(SESSION_END)], input=payload, capture_output=True, text=True,
            timeout=30, env=_env(cache_dir), cwd=str(project_root),
        )
        assert proc.returncode == 0, proc.stderr

        listing = sorted(str(p.relative_to(gates)) for p in gates.rglob("*"))
        assert not (gates / sid_a).exists(), (
            f"session A's own gate directory survived its SessionEnd: {listing}"
        )
        assert (gates / sid_b / "phase-a.approved").is_file(), (
            f"SessionEnd for session A removed session B's sibling directory too: {listing}"
        )


# ---------------------------------------------------------------------------
# Capability 23
# ---------------------------------------------------------------------------


class TestGateTokenPathIsUnchanged:
    """Capability 23: only the .approved ARTIFACT moves under a session directory; the
    TOKEN (the thing that actually authorizes an advance) stays exactly where it is,
    already named by session id (gate_token.py's own module docstring calls the path
    "must match the bash writer byte-for-byte", and plan.md's own Analysis says tokens
    "need no path change").

    Both tests below are expected to PASS already -- this is a forward-guard pin, not a
    RED-before-implementation claim, because nothing in Part 2 touches gate_token.py.
    """

    def test_gate_token_path_is_flat_tmp_not_session_scoped(self):
        from writ.session.gate_token import gate_token_path

        sid = _sid("token")
        assert gate_token_path(sid) == f"/tmp/writ-gate-token-{sid}"

    def test_a_real_approval_mints_and_claims_exactly_one_token(self, two_sessions):
        """Anti-vacuity/integration companion: the token this test's own setup creates is
        gone after the successful advance it authorized (claiming consumes it -- see
        gate_token.claim_gate_token's docstring), and nothing stray survives it."""
        project_root, cache_dir, sid_a, _sid_b = two_sessions
        from writ.session.gate_token import gate_token_path

        token_path = gate_token_path(sid_a)
        assert not os.path.exists(token_path), f"a stray token already exists at {token_path}"
        try:
            _approve_phase_a(sid_a, project_root, cache_dir)
            assert not os.path.exists(token_path), (
                "the token file was not consumed by the successful advance it authorized"
            )
        finally:
            if os.path.exists(token_path):
                os.unlink(token_path)


# ---------------------------------------------------------------------------
# Capability 24
# ---------------------------------------------------------------------------


class TestPermissionDenyPatternsCoverSessionScopedPaths:
    """Capability 24: the agent must never write its own approval -- that is the whole
    premise of a human-oversight gate. writ_install.py's DENY globs use `Bash(...)`
    patterns where `*` spans `/` (plan.md ## Analysis, Part 2: "its deny patterns use
    globs where `*` spans `/` and matches the raw path, so the added directory level is
    already covered"), so plan.md itself says this file needs NO change for the new
    directory level.

    This is therefore a PINNING test, not a RED-before-implementation one: it is expected
    to pass already, and it must keep passing once the directory level is added, or a
    load-bearing accident this plan explicitly relies on stopped holding and the agent
    could self-approve a session-scoped gate.
    """

    @staticmethod
    def _bash_deny_patterns() -> list[str]:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "writ_install", str(REPO / "bin" / "lib" / "writ_install.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        patterns = []
        for entry in mod.DENY:
            m = re.fullmatch(r"Bash\((.*)\)", entry)
            if m:
                patterns.append(m.group(1))
        return patterns

    @pytest.mark.parametrize("command", [
        "touch /home/user/project/.claude/gates/isotest-abc123def456/phase-a.approved",
        "echo ok >.claude/gates/isotest-abc123def456/test-skeletons.approved",
        "printf ok > /home/user/project/.claude/gates/isotest-abc123def456/gate-final.approved",
    ])
    def test_a_session_scoped_approval_write_is_still_denied(self, command):
        patterns = self._bash_deny_patterns()
        assert patterns, "writ_install.DENY produced no Bash(...) patterns to check"
        assert any(fnmatch.fnmatchcase(command, p) for p in patterns), (
            f"no DENY pattern matches {command!r}; patterns={patterns!r}"
        )

    def test_the_deny_list_still_names_the_gate_boundary(self):
        """Anti-vacuity: guards against the parametrized test above passing only because
        DENY became empty or lost its gate-scoped entries entirely."""
        patterns = self._bash_deny_patterns()
        assert any("gates" in p for p in patterns), (
            f"no remaining DENY pattern even mentions gates: {patterns!r}"
        )
