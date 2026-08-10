"""Shared session-state pytest fixtures and can-write caller (Wave-5 Cycle 5.3c).

Consolidates the `session_id` and `project_root` fixtures plus the can-write
envelope/stdin/parse dance formerly duplicated in `test_mode_infrastructure.py`
and `test_phase3_centralization.py`. These are imported EXPLICITLY into each
consuming test module (`from tests.fixtures.session_state import session_id,
project_root`), never registered in a root conftest, so they cannot shadow the
many other files that define their own divergent `session_id`/`project_root`
fixtures.

`call_can_write` takes `writ_session` and `skill_dir` as arguments rather than
recomputing them, because each consumer loads `writ-session.py` via its own
`importlib` spec and computes `SKILL_DIR` relative to its own `__file__`.
"""

from __future__ import annotations

import io
import json

import pytest


@pytest.fixture()
def session_id(tmp_path, monkeypatch, request):
    """Provide a session ID and redirect cache to tmp_path.

    The returned name is cosmetic (never asserted; it is only used as a
    cache-file key), so it defaults to a shared literal. A consuming file may
    override it via indirect parametrization (`request.param`) if needed.
    """
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    return getattr(request, "param", "test-session")


@pytest.fixture()
def project_root(tmp_path):
    """Create a minimal project root with .git marker and gates dir."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def sandbox_cwd(tmp_path, monkeypatch):
    """Move the process cwd to a throwaway project for every test in the importing module.

    `mode set` / `mode init` stamp cache["project_root"] from the process cwd, and clearing
    gate state DELETES <project_root>/.claude/gates/*.approved. So a test that calls
    cmd_mode in-process, or spawns `writ-session.py mode set` or the UserPromptSubmit hook
    without pinning cwd, inherits pytest's own cwd (the real repo root) and destroys this
    repo's approval artifacts as a side effect of running the suite. A sentinel probe
    found 26 modules doing exactly that. Subprocesses inherit the chdir, so this one
    fixture covers the in-process and the spawned shapes alike; a subprocess that passes an
    explicit `cwd=` must drop it to be covered.

    autouse, but imported per module rather than registered in a root conftest, for the
    reason this file's header gives: an autouse chdir in a conftest would silently reroute
    EVERY test in the suite, including the many that resolve paths against the repo root.

    The sandbox carries a .git marker so a project-root walk stops inside it instead of
    climbing back out to a real project, and a .claude/gates directory so the cleanup has a
    real (empty) directory to act on rather than a missing-path no-op.
    """
    sandbox = tmp_path / "cwd-sandbox"
    (sandbox / ".claude" / "gates").mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(sandbox)
    return sandbox


def call_can_write(writ_session, session_id, file_path, monkeypatch, capsys, skill_dir=None):
    """Call cmd_can_write with a synthetic tool envelope and return the JSON result."""
    capsys.readouterr()  # clear any prior output
    envelope = json.dumps({"tool_input": {"file_path": file_path}})
    monkeypatch.setattr("sys.stdin", io.StringIO(envelope))
    writ_session.cmd_can_write(session_id, skill_dir)
    out = capsys.readouterr().out.strip()
    return json.loads(out)
