"""Phase 3b (superseded behavior): auto-approve-gate.sh advances the gate itself.

History: the original Phase 3b design had the hook emit a `/writ-approve`
steer directive on an approval phrase and NEVER advance -- the assistant had
to echo a token back through the slash command. That dance was circular and
unworkable, so it was superseded.

Current behavior (plan Section 8.1, revised): the hook is trusted infra. When
the user types an approval AND the session is in Work mode at the planning or
testing phase (the two phases that await a human approval gate), the hook
writes the single-use gate token and advances the phase itself via the server's
/advance-phase endpoint, printing:

    [Writ: <phase> gate approved -> <next>] ...

This is NOT agent self-approval: the user typed the approval, the trusted hook
executes it, the agent never handles the token. Outside that precondition (no
pending gate, wrong mode/phase, or server unreachable) the hook prints a
fallback:

    [Writ: approval pattern detected]
    No approval gate was advanced ...

The hook never emits `/writ-approve` anymore.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests._daemon import _port, expected_cache_dir

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOK = WRIT_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"
SESSION_HELPER = WRIT_ROOT / "bin" / "lib" / "writ-session.py"
PYTHON = WRIT_ROOT / ".venv" / "bin" / "python"


def _start_own_daemon() -> bool:
    """Start a daemon on the TEST port, aligned to the test cache dir.

    Two earlier attempts at this were wrong, both caught by review:

      1. Popping WRIT_CACHE_DIR to reach the interactive daemon. It worked only because
         the hook's curl hardcoded :8765 and so bypassed the suite's WRIT_PORT isolation,
         and it wrote genuine phase_advance rows into the real audit log -- fabricated
         approvals in the stream that exists to record real ones.
      2. Calling _daemon.start_test_daemon() from inside a test. conftest deliberately
         does NOT start a daemon (it only realigns one that already exists) because
         daemon-dependent tests are meant to skip when none answers, "same as CI". Bringing
         one up mid-suite changed the world for every later test and could stop/restart the
         daemon other tests were mid-request against.

    So this owns the lifecycle: start here, stop in the fixture teardown, by exact port,
    never touching the interactive singleton. Mirrors
    test_methodology_companion_orchestrator._ensure_aligned_daemon.
    """
    ensure = WRIT_ROOT / "scripts" / "ensure-server.sh"
    if not ensure.exists():
        return False
    env = {
        **os.environ,
        "WRIT_PORT": _port(),
        "WRIT_HOST": "localhost",
        "WRIT_CACHE_DIR": expected_cache_dir(),
        "WRIT_REALIGN_CACHE": "1",
        "WRIT_NO_AUTOSTART": "",  # this explicit start IS the daemon
    }
    subprocess.run(["bash", str(ensure)], capture_output=True, text=True,
                   env=env, timeout=40, check=False)
    health = f"http://localhost:{_port()}/health"
    for _ in range(40):
        try:
            with urllib.request.urlopen(health, timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def _stop_own_daemon() -> None:
    """Stop only the daemon this module started, matched by its exact port."""
    subprocess.run(["pkill", "-f", f"writ serve --port {_port()}"], capture_output=True)


def _cleanup_session(session_id: str) -> None:
    session_dir = Path(expected_cache_dir())
    for path in (
        session_dir / f"writ-session-{session_id}.json",
        # mutate_cache leaves a sibling .lock file; removing only the .json left the
        # session dir accumulating lock residue on every test run.
        session_dir / f"writ-session-{session_id}.json.lock",
        # gate_token_path hardcodes /tmp so the bash writer and python reader agree.
        Path("/tmp") / f"writ-gate-token-{session_id}",
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _run_hook(
    prompt: str,
    session_id: str = "phase3b-test",
    cwd: str = str(WRIT_ROOT),
    env: dict | None = None,
) -> tuple[str, int]:
    # cwd drives the hook's PROJECT_ROOT walk (auto-approve-gate.sh derives it
    # from os.getcwd() and feeds it to the server's plan.md validator), so an
    # isolated cwd makes the advance test hermetic. Defaults to WRIT_ROOT to
    # preserve behavior for every other caller.
    stdin = json.dumps({"session_id": session_id, "prompt": prompt})
    proc = subprocess.run(
        [str(HOOK)],
        input=stdin, capture_output=True, text=True,
        cwd=cwd, env=env,
    )
    return proc.stdout, proc.returncode


def _setup_work_planning_session(session_id: str, env: dict | None = None) -> bool:
    """Put a session into Work mode at the planning phase. Returns True on success."""
    interp = str(PYTHON) if PYTHON.exists() else "python3"
    # NO cwd=WRIT_ROOT here, unlike the reads below. `mode set` stamps the process cwd as
    # the session's project_root and then deletes that project's .claude/gates/*.approved,
    # so pinning it to the repo root made this helper wipe THIS repo's approval artifacts
    # on every run. Inheriting the sandbox_cwd fixture's cwd keeps the damage in tmp_path.
    proc = subprocess.run(
        [interp, str(SESSION_HELPER), "mode", "set", "work", session_id],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        return False
    phase = subprocess.run(
        [interp, str(SESSION_HELPER), "current-phase", session_id],
        capture_output=True, text=True, cwd=str(WRIT_ROOT), env=env,
    )
    try:
        data = json.loads(phase.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return data.get("mode") == "work" and data.get("phase") == "planning"


@pytest.fixture(scope="module")
def own_daemon():
    """A daemon this module starts and stops, for the end-to-end advance cases.

    Module-scoped so one start/stop covers all three tests instead of thrashing the
    port, and torn down so the rest of the suite sees the same world it would without
    this file (conftest's contract: no daemon unless a test brings its own).
    """
    if not _start_own_daemon():
        pytest.skip("could not start a daemon on the test port (Neo4j down?)")
    yield
    _stop_own_daemon()


class TestApprovalFallbackWhenNoGatePending:
    """The default session has no pending Work-mode gate, so an approval phrase
    produces the 'No approval gate was advanced' fallback -- NOT the old
    /writ-approve directive and NOT a phase advance."""

    @pytest.mark.parametrize("prompt", ["approved", "lgtm", "proceed"])
    def test_no_pending_gate_emits_fallback(self, prompt: str) -> None:
        # Unique session per param so a stray prior advance cannot pollute it;
        # the default unclassified/null-mode session always hits the fallback.
        stdout, code = _run_hook(prompt, session_id=f"phase3b-fallback-{prompt}")
        assert code == 0
        assert "[Writ: approval pattern detected]" in stdout
        assert "No approval gate was advanced" in stdout

    @pytest.mark.parametrize("prompt", ["approved", "lgtm", "proceed"])
    def test_fallback_does_not_emit_writ_approve(self, prompt: str) -> None:
        # The superseded design steered to /writ-approve. The current hook never does.
        stdout, code = _run_hook(prompt, session_id=f"phase3b-noapprove-{prompt}")
        assert code == 0
        assert "/writ-approve" not in stdout

    @pytest.mark.parametrize("prompt", ["approved", "lgtm", "proceed"])
    def test_fallback_does_not_advance(self, prompt: str) -> None:
        # The fallback path is a no-op advance: it must NOT print the advance line.
        stdout, code = _run_hook(prompt, session_id=f"phase3b-noadv-{prompt}")
        assert code == 0
        assert "gate approved ->" not in stdout


class TestApprovalAdvancesWhenGatePending:
    """In Work mode at the planning phase, the trusted hook advances the gate
    itself on a user approval and prints the advance confirmation line."""

    def test_work_planning_approval_advances(self, tmp_path: Path, own_daemon) -> None:
        session_id = f"phase3b-advance-planning-{uuid.uuid4().hex[:8]}"
        if not _setup_work_planning_session(session_id):
            pytest.skip("could not establish a Work-mode planning session")
        # Hermetic project: a .git marker makes tmp_path itself the PROJECT_ROOT
        # (auto-approve-gate.sh checks the cwd first), and a gate-valid plan.md
        # lets the phase-a validator pass regardless of the ambient repo's plan.md
        # (whose [x] boxes vary with prior work cycles).
        (tmp_path / ".git").mkdir()
        (tmp_path / "plan.md").write_text(
            "## Files\n"
            "- `src/example.py` (create) -- isolated fixture file\n\n"
            "## Analysis\n"
            "Isolated fixture plan to exercise the phase-a approval gate.\n\n"
            "## Rules Applied\n"
            "No matching rules\n\n"
            "## Capabilities\n"
            "- [ ] does the thing\n"
        )
        try:
            stdout, code = _run_hook(
                "approved", session_id=session_id, cwd=str(tmp_path)
            )
        finally:
            _cleanup_session(session_id)
        assert code == 0
        # No skip-on-refusal: the daemon is up (checked above) and the session was
        # seeded in the daemon's own cache, so a refusal here is a real failure. The
        # old skip swallowed exactly that.
        assert "[Writ: planning gate approved -> testing]" in stdout, stdout
        assert "no agent self-approval" in stdout
        assert "/writ-approve" not in stdout


class TestApprovalWorksInAnUnmarkedDirectory:
    """The same advance, from a directory with NO repo marker file.

    Claude Code runs anywhere. The hook's marker walk (composer.json,
    package.json, Cargo.toml, go.mod, pyproject.toml, .git) returned "" for such a
    directory, the hook posted an empty project_root, and the route refused the advance
    AND spent the approval token -- so Writ's gate could not be used at all outside a
    conventionally-marked repo. The hook now also sends its cwd and the route resolves
    the root from it.

    Deliberately no `.git` here: that is the whole point of the test.
    """

    def _unmarked_project(self, tmp_path: Path) -> Path:
        work = tmp_path / "just" / "a" / "folder"
        work.mkdir(parents=True)
        (work / "plan.md").write_text(
            "## Files\n"
            "- `src/example.py` (create) -- isolated fixture file\n\n"
            "## Analysis\n"
            "Fixture plan in a directory with no repo marker.\n\n"
            "## Rules Applied\n"
            "No matching rules\n\n"
            "## Capabilities\n"
            "- [ ] does the thing\n"
        )
        for marker in ("composer.json", "package.json", "Cargo.toml", "go.mod",
                       "pyproject.toml", ".git"):
            assert not (work / marker).exists()
        return work

    def test_unmarked_cwd_advances(self, tmp_path: Path, own_daemon) -> None:
        session_id = f"phase3b-unmarked-{uuid.uuid4().hex[:8]}"
        if not _setup_work_planning_session(session_id):
            pytest.skip("could not establish a Work-mode planning session")
        work = self._unmarked_project(tmp_path)
        try:
            stdout, code = _run_hook("approved", session_id=session_id, cwd=str(work))
        finally:
            _cleanup_session(session_id)
        assert code == 0
        assert "[Writ: planning gate approved -> testing]" in stdout, stdout

    def test_advance_names_the_validated_plan(self, tmp_path: Path, own_daemon) -> None:
        """The confirmation must say WHICH plan.md was accepted.

        An unqualified "approved" hid the project root, so a root resolved from a stray
        marker file above the work directory could stamp an unrelated plan silently.
        """
        session_id = f"phase3b-named-{uuid.uuid4().hex[:8]}"
        if not _setup_work_planning_session(session_id):
            pytest.skip("could not establish a Work-mode planning session")
        work = self._unmarked_project(tmp_path)
        try:
            stdout, code = _run_hook("approved", session_id=session_id, cwd=str(work))
        finally:
            _cleanup_session(session_id)
        assert code == 0
        assert str(work / "plan.md") in stdout, (
            "the advance confirmation must name the validated plan; got: " + stdout
        )


class TestNonApprovalNoDirective:
    """Prompts that aren't approvals get neither directive nor advance."""

    @pytest.mark.parametrize("prompt", [
        "refactor the database module",
        "how do I fix this bug?",
        "where does this function go in the architecture?",
    ])
    def test_no_directive_on_non_approval(self, prompt: str) -> None:
        stdout, code = _run_hook(prompt)
        assert code == 0
        assert "approval pattern detected" not in stdout
        assert "gate approved ->" not in stdout
        assert "/writ-approve" not in stdout


class TestHookExecutableAndValid:
    def test_hook_exists_and_executable(self) -> None:
        import os
        assert HOOK.exists()
        assert os.access(HOOK, os.X_OK)

    def test_hook_syntax(self) -> None:
        proc = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


class TestNoLegacySilentAdvanceCall:
    """The hook advances via the server's token-guarded /advance-phase endpoint,
    never via a direct `_writ_session advance-phase` shell call.

    The user's approval is the authorization; the hook writes the single-use
    gate token and POSTs it, so the agent never handles the token. The legacy
    in-process `_writ_session advance-phase --token` shell path is gone.
    """

    def test_hook_does_not_call_advance_phase_helper(self) -> None:
        content = HOOK.read_text()
        pattern = re.compile(r"_writ_session\s+advance-phase", re.IGNORECASE)
        assert not pattern.search(content), (
            "auto-approve-gate.sh must NOT call the advance-phase shell helper directly. "
            "It advances through the token-guarded server endpoint instead."
        )
