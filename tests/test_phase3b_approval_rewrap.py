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
import uuid
from pathlib import Path

import pytest

from tests._hook_runner import hook_env, instrumented_daemon, verify_seeded_mode

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.fixtures.session_state import write_evidence_transcript

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOK = WRIT_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"
SESSION_HELPER = WRIT_ROOT / "bin" / "lib" / "writ-session.py"
PYTHON = WRIT_ROOT / ".venv" / "bin" / "python"

# The gate-token session namespace this module mints under. Module level is
# required, not stylistic: tests/_gate_token_leak.py reads this off
# `request.module` and sweeps only `/tmp/writ-gate-token-<prefix>*`, so a value
# declared anywhere else is invisible to the sweeper. Every session id this
# module mints under is `phase3b-*`, and several of them append a fresh
# uuid suffix, so each run adds a file rather than rewriting one in place.
GATE_TOKEN_SESSION_PREFIX = "phase3b-"


def _cleanup_session(session_id: str, cache_dir: str) -> None:
    session_dir = Path(cache_dir)
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
    transcript_path: str | None = None,
) -> tuple[str, int]:
    # cwd drives the hook's PROJECT_ROOT walk (auto-approve-gate.sh derives it
    # from os.getcwd() and feeds it to the server's plan.md validator), so an
    # isolated cwd makes the advance test hermetic. Defaults to WRIT_ROOT to
    # preserve behavior for every other caller.
    #
    # transcript_path is OMITTED (None) by default: only the no-pending-gate
    # fallback tests below need it (the approval-integrity cycle's evidence gate
    # applies to the exact tier regardless of whether a gate is pending; see
    # auto-approve-gate.sh's own comment on why the mint happens even then).
    payload = {"session_id": session_id, "prompt": prompt}
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    stdin = json.dumps(payload)
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
def own_daemon(tmp_path_factory):
    """A daemon this module starts and stops, for the end-to-end advance cases.

    `instrumented_daemon` (tests/_hook_runner.py) owns the start (an
    OS-assigned free port, a throwaway log root/socket/cache dir under this
    module's own tmp dir), the access-log positive control and the asserted
    stop verdict. Module-scoped so one start/stop covers all three tests
    instead of thrashing the port, and torn down so the rest of the suite
    sees the same world it would without this file (conftest's contract: no
    daemon unless a test brings its own).

    NO CORPUS PRECONDITION: `auto-approve-gate.sh` retrieves nothing, so
    `isolated_hook_daemon`'s injection-rule population would fail this module
    on a predicate unrelated to its subject.
    """
    with instrumented_daemon(tmp_path_factory.mktemp("phase3b-rewrap")) as daemon:
        yield daemon


class TestNarrowedVocabularyIsSilent:
    """A phrase that no longer mints produces no approval output at all.

    `lgtm` and `proceed` used to reach the fallback directive here, which is how they
    revealed themselves as approvals. As of 2026-08-23 the exact tier is the single word
    `approved`, so these are ordinary prose: no directive, no telemetry row, no advance.
    Kept as fixtures so re-widening the vocabulary fails a named test.
    """

    @pytest.mark.parametrize("prompt", ["lgtm", "proceed", "ok", "yes", "y", "continue"])
    def test_former_phrase_emits_no_approval_output(self, prompt: str) -> None:
        stdout, code = _run_hook(prompt, session_id=f"phase3b-silent-{prompt}")
        assert code == 0
        assert "[Writ: approval pattern detected]" not in stdout, prompt
        assert "No approval gate was advanced" not in stdout, prompt


class TestApprovalFallbackWhenNoGatePending:
    """The default session has no pending Work-mode gate, so an approval phrase
    produces the no-gate-pending fallback: NOT the old /writ-approve directive and
    NOT a phase advance."""

    @pytest.mark.parametrize("prompt", ["approved", "approved.", "approved!"])
    def test_no_pending_gate_emits_fallback(self, prompt: str, tmp_path) -> None:
        # Unique session per param so a stray prior advance cannot pollute it;
        # the default unclassified/null-mode session always hits the fallback.
        #
        # ASSERTS THE INTENT, NOT THE SENTENCE. This pinned the exact marker
        # "[Writ: approval pattern detected]" and the exact phrase "No approval gate was
        # advanced". The replan cycle split the one conflated no-gate/unreachable branch
        # into four named ones and gave each marker a suffix saying what happened, so the
        # marker is now "[Writ: approval pattern detected, nothing was advanced]" and the
        # body says nothing was pending. The property this test exists for is unchanged:
        # the fallback fires, and it says nothing advanced. Pinning the prefix and the
        # meaning keeps that property without re-breaking on the next wording pass.
        #
        # This is now reached only when evidence exists (approval-integrity cycle,
        # defect 2): without it, an exact `approved` gets the ask directive instead
        # of this fallback, so the fixture states the precondition explicitly.
        transcript_path = write_evidence_transcript(tmp_path)
        stdout, code = _run_hook(
            prompt, session_id=f"phase3b-fallback-{prompt}", transcript_path=transcript_path,
        )
        assert code == 0
        assert "[Writ: approval pattern detected" in stdout
        assert "nothing was advanced" in stdout
        assert "nothing to advance" in stdout

    @pytest.mark.parametrize("prompt", ["approved"])
    def test_fallback_does_not_emit_writ_approve(self, prompt: str, tmp_path) -> None:
        # The superseded design steered to /writ-approve. The current hook never does.
        transcript_path = write_evidence_transcript(tmp_path)
        stdout, code = _run_hook(
            prompt, session_id=f"phase3b-noapprove-{prompt}", transcript_path=transcript_path,
        )
        assert code == 0
        assert "/writ-approve" not in stdout

    @pytest.mark.parametrize("prompt", ["approved"])
    def test_fallback_does_not_advance(self, prompt: str, tmp_path) -> None:
        # The fallback path is a no-op advance: it must NOT print the advance line.
        transcript_path = write_evidence_transcript(tmp_path)
        stdout, code = _run_hook(
            prompt, session_id=f"phase3b-noadv-{prompt}", transcript_path=transcript_path,
        )
        assert code == 0
        assert "gate approved ->" not in stdout


class TestApprovalAdvancesWhenGatePending:
    """In Work mode at the planning phase, the trusted hook advances the gate
    itself on a user approval and prints the advance confirmation line."""

    def test_work_planning_approval_advances(self, tmp_path: Path, own_daemon) -> None:
        session_id = f"phase3b-advance-planning-{uuid.uuid4().hex[:8]}"
        env = hook_env(own_daemon)
        if not _setup_work_planning_session(session_id, env=env):
            pytest.skip("could not establish a Work-mode planning session")
        verify_seeded_mode(own_daemon, session_id, "work")
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
                "approved", session_id=session_id, cwd=str(tmp_path), env=env,
                transcript_path=write_evidence_transcript(tmp_path),
            )
        finally:
            _cleanup_session(session_id, own_daemon["health"]["cache_dir"])
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
        env = hook_env(own_daemon)
        if not _setup_work_planning_session(session_id, env=env):
            pytest.skip("could not establish a Work-mode planning session")
        verify_seeded_mode(own_daemon, session_id, "work")
        work = self._unmarked_project(tmp_path)
        try:
            stdout, code = _run_hook(
                "approved", session_id=session_id, cwd=str(work), env=env,
                transcript_path=write_evidence_transcript(tmp_path),
            )
        finally:
            _cleanup_session(session_id, own_daemon["health"]["cache_dir"])
        assert code == 0
        assert "[Writ: planning gate approved -> testing]" in stdout, stdout

    def test_advance_names_the_validated_plan(self, tmp_path: Path, own_daemon) -> None:
        """The confirmation must say WHICH plan.md was accepted.

        An unqualified "approved" hid the project root, so a root resolved from a stray
        marker file above the work directory could stamp an unrelated plan silently.
        """
        session_id = f"phase3b-named-{uuid.uuid4().hex[:8]}"
        env = hook_env(own_daemon)
        if not _setup_work_planning_session(session_id, env=env):
            pytest.skip("could not establish a Work-mode planning session")
        verify_seeded_mode(own_daemon, session_id, "work")
        work = self._unmarked_project(tmp_path)
        try:
            stdout, code = _run_hook(
                "approved", session_id=session_id, cwd=str(work), env=env,
                transcript_path=write_evidence_transcript(tmp_path),
            )
        finally:
            _cleanup_session(session_id, own_daemon["health"]["cache_dir"])
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
