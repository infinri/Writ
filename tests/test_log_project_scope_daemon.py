"""Daemon-emitted rows must be filed under the project the REQUEST is about.

`emit()` resolves its log scope with `resolve_project()` and no argument, which derives
the project from `os.getcwd()`. That is right for hook-side callers (a hook is a
subprocess in the user's project) and wrong for the daemon, whose cwd is pinned to the
skill directory by `WorkingDirectory` in the systemd unit. So every event emitted while
serving an HTTP request is attributed to Writ regardless of whose write was gated.

MEASURED before this cycle: 33 of the 51 `write_attempt` rows in Writ's own audit log
carried file paths under `/home/lucio.saldivar/workspaces/ai-stack/`. The owning project's
audit log showed zero write-gate rows, so looking there to ask "is the gate running here"
answered a confident no.

RED until `request_project_scope` exists in writ.shared.logging: the import below fails,
which is the intended red state per ENF-PROC-TDD-001 (skeletons approved before
implementation).

`no_friction_isolation` is REQUIRED here, not incidental. conftest's autouse fixture sets
WRIT_FRICTION_LOG for every test, which collapses all streams into one file and would make
every per-project path assertion in this module vacuous. The same fixture still sandboxes
WRIT_LOG_ROOT into tmp_path for opted-out tests, which is what makes the path assertions
safe to run at all.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from writ.shared.logging import (
    emit,
    request_project_scope,
    resolve_project,
    stream_path,
)

pytestmark = pytest.mark.no_friction_isolation


def _git_repo(root: Path, remote: str) -> Path:
    """A real repo with an origin, so derive_project_identity yields a stable host/org/repo.

    A repo without a remote resolves to its abspath, which is machine-specific and would
    make the expected path depend on tmp_path. The remote makes the identity deterministic.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=30)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True, timeout=30)
    return root


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestEmitHonorsTheRequestScope:
    def test_emit_files_under_scoped_project_when_override_set(self, tmp_path) -> None:
        """The whole point: the row lands under the request's project, not the cwd's."""
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        with request_project_scope(str(proj)):
            emit("audit", "write_attempt", "sid-1", "work",
                 file_path=str(proj / "src/x.py"), result="allow")

        rows = _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))
        assert len(rows) == 1, "the scoped project's audit stream must hold exactly this row"
        assert rows[0]["event"] == "write_attempt"
        assert rows[0]["file_path"] == str(proj / "src/x.py")

    def test_emit_does_not_write_to_the_cwd_derived_project_when_scoped(self, tmp_path) -> None:
        """Misattribution is the defect, so absence from the WRONG project is the assertion."""
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        cwd_project = resolve_project()
        with request_project_scope(str(proj)):
            emit("audit", "write_attempt", "sid-2", "work", result="allow")

        assert _read_jsonl(stream_path(cwd_project, "audit")) == [], (
            f"no row may land under the cwd-derived project {cwd_project!r} while scoped"
        )

    def test_emit_path_is_unchanged_when_override_unset(self, tmp_path) -> None:
        """Hook-side callers and every existing test must see byte-identical behavior."""
        emit("audit", "write_attempt", "sid-3", "work", result="allow")

        rows = _read_jsonl(stream_path(resolve_project(), "audit"))
        assert len(rows) == 1
        assert rows[0]["session"] == "sid-3"

    def test_scope_does_not_leak_to_the_next_request(self, tmp_path) -> None:
        """A request without a scope must not inherit the previous request's project."""
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        with request_project_scope(str(proj)):
            emit("audit", "write_attempt", "scoped", "work", result="allow")
        emit("audit", "write_attempt", "unscoped", "work", result="allow")

        scoped = _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))
        after = _read_jsonl(stream_path(resolve_project(), "audit"))
        assert [r["session"] for r in scoped] == ["scoped"]
        assert [r["session"] for r in after] == ["unscoped"]

    def test_scope_is_restored_when_the_body_raises(self, tmp_path) -> None:
        """An exception mid-request must not strand the scope for the next one."""
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        with pytest.raises(RuntimeError):
            with request_project_scope(str(proj)):
                raise RuntimeError("boom")
        emit("audit", "write_attempt", "after-raise", "work", result="allow")

        rows = _read_jsonl(stream_path(resolve_project(), "audit"))
        assert [r["session"] for r in rows] == ["after-raise"]


class TestScopeCrossesTheThreadBoundary:
    def test_scope_propagates_into_asyncio_to_thread(self, tmp_path) -> None:
        """The property the whole design rests on.

        /pre-write-check evaluates the gate inside asyncio.to_thread (gate.py:519), so the
        row is emitted on a worker thread. asyncio.to_thread copies the caller's context,
        which is why nothing has to be threaded through gates.py. Pinned, not assumed.
        """
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")

        async def _run() -> None:
            with request_project_scope(str(proj)):
                await asyncio.to_thread(
                    emit, "audit", "write_attempt", "threaded", "work", result="allow",
                )

        asyncio.run(_run())

        rows = _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))
        assert [r["session"] for r in rows] == ["threaded"], (
            "an event emitted under to_thread must inherit the request's project scope"
        )


class TestRouteScopeDoesNotEscape:
    """`set_request_project_scope` never restores, so its safety IS the context copy.

    Both daemon shapes rely on that copy: `asyncio.to_thread` runs its target via
    `copy_context().run(...)`, and an async handler runs as a Task, which copies the
    context at creation. The setter's docstring makes that claim in both directions, so
    both are checked here instead of trusted.
    """

    def test_a_scope_set_inside_to_thread_does_not_reach_the_caller(self, tmp_path) -> None:
        from writ.shared.logging import set_request_project_scope

        proj = _git_repo(tmp_path / "worker", "git@github.com:acme/worker.git")

        async def _run() -> None:
            await asyncio.to_thread(set_request_project_scope, str(proj))
            # Back on the event loop: the worker's scope must not have followed us here.
            emit("audit", "write_attempt", "after-thread", "work", result="allow")

        asyncio.run(_run())

        assert [r["session"] for r in _read_jsonl(stream_path(resolve_project(), "audit"))] == [
            "after-thread",
        ], "a scope set inside to_thread leaked back onto the calling context"

    def test_a_scope_set_inside_one_task_does_not_reach_another(self, tmp_path) -> None:
        from writ.shared.logging import set_request_project_scope

        proj = _git_repo(tmp_path / "task-a", "git@github.com:acme/task-a.git")

        async def _first() -> None:
            set_request_project_scope(str(proj))
            emit("audit", "write_attempt", "task-a", "work", result="allow")

        async def _second() -> None:
            emit("audit", "write_attempt", "task-b", "work", result="allow")

        async def _run() -> None:
            await asyncio.create_task(_first())
            await asyncio.create_task(_second())

        asyncio.run(_run())

        assert [r["session"] for r in _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))] == [
            "task-a",
        ]
        assert [r["session"] for r in _read_jsonl(stream_path(resolve_project(), "audit"))] == [
            "task-b",
        ], "one request's scope leaked into the next request's task"


class TestFallbackAndBoundaries:
    @pytest.mark.parametrize("override", ["", "   ", None])
    def test_blank_override_falls_back_to_cwd_resolution(self, override, tmp_path) -> None:
        """A blank scope is 'no scope', not a new junk directory named after nothing."""
        with request_project_scope(override):
            emit("audit", "write_attempt", "blank", "work", result="allow")

        rows = _read_jsonl(stream_path(resolve_project(), "audit"))
        assert [r["session"] for r in rows] == ["blank"]

    def test_non_repo_override_falls_back_rather_than_inventing_a_scope(self, tmp_path) -> None:
        """A path that is not a git repo raises NotInRepoError inside resolve_project.

        The row must still be written: an unattributed row is a visible gap, a dropped one
        is lost evidence (the contract documented on UNRESOLVED_PROJECT).
        """
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        with request_project_scope(str(plain)):
            emit("audit", "write_attempt", "non-repo", "work", result="allow")

        written = [p for p in (tmp_path / "logs").rglob("audit.jsonl")]
        assert written, "the row must be written somewhere, never dropped"
        assert any(r["session"] == "non-repo" for p in written for r in _read_jsonl(p))

    def test_traversal_override_cannot_escape_the_log_root(self, tmp_path) -> None:
        """SEC-INJ-PATH-001: the override feeds resolve_project, which sanitizes segments."""
        root = Path(os.environ["WRIT_LOG_ROOT"]).resolve()
        with request_project_scope("../../../../etc/writ-escape"):
            emit("audit", "write_attempt", "traversal", "work", result="allow")

        for path in root.rglob("audit.jsonl"):
            assert root in path.resolve().parents, f"{path} escaped the log root {root}"
        assert not Path("/etc/writ-escape").exists()

    def test_writ_friction_log_still_wins_over_the_override(self, tmp_path, monkeypatch) -> None:
        """The single-file test-isolation collapse is checked before any project routing."""
        single = tmp_path / "collapsed.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(single))
        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")

        with request_project_scope(str(proj)):
            emit("audit", "write_attempt", "collapsed", "work", result="allow")

        assert [r["session"] for r in _read_jsonl(single)] == ["collapsed"]
        assert _read_jsonl(stream_path(resolve_project(str(proj)), "audit")) == []


class TestPreWriteCheckRouteAttribution:
    """End to end through the route, which is where the reported symptom lives.

    The real _can_write_check runs (that is the emitter under test); only the session
    cache is substituted, because the cache is what carries project_root in production.
    WRIT_CACHE_DIR is redirected because the deny path calls mutate_cache, which writes.
    """

    @staticmethod
    def _cache(project_root: str, *, approved: list[str]) -> dict:
        return {
            "mode": "work",
            "project_root": project_root,
            "current_phase": "planning" if not approved else "implementation",
            "gates_approved": approved,
            "gates_approved_plan": {},
            "denial_counts": {},
            "is_subagent": False,
            "remaining_budget": 1500,
        }

    def test_allowed_write_files_write_attempt_under_the_caller_project(
        self, tmp_path, monkeypatch,
    ) -> None:
        import writ.server as server
        from writ.server import PreWriteCheckRequest, pre_write_check

        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(
            server.writ_session, "_read_cache",
            lambda sid: self._cache(str(proj), approved=["phase-a", "test-skeletons"]),
        )
        monkeypatch.setattr(server, "_pipeline", None)

        target = str(proj / "src" / "handler.py")
        result = asyncio.run(pre_write_check(PreWriteCheckRequest(
            session_id="s-allow", tool_input={"file_path": target},
            file_path=target, skill_dir=str(tmp_path / "skill"),
        )))

        assert result["decision"] == "allow"
        rows = _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))
        assert any(r["event"] == "write_attempt" and r["result"] == "allow" for r in rows), (
            f"the caller project's audit stream must hold the write_attempt, got {rows!r}"
        )

    def test_denied_write_files_both_rows_under_the_caller_project(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The deny path is a separate branch from allow, and it is the one the symptom hid."""
        import writ.server as server
        from writ.server import PreWriteCheckRequest, pre_write_check

        proj = _git_repo(tmp_path / "caller", "git@github.com:acme/caller.git")
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(
            server.writ_session, "_read_cache",
            lambda sid: self._cache(str(proj), approved=[]),
        )

        target = str(proj / "src" / "handler.py")
        result = asyncio.run(pre_write_check(PreWriteCheckRequest(
            session_id="s-deny", tool_input={"file_path": target},
            file_path=target, skill_dir=str(tmp_path / "skill"),
        )))

        assert result["decision"] == "deny"
        events = [r["event"] for r in _read_jsonl(stream_path(resolve_project(str(proj)), "audit"))]
        assert "write_attempt" in events
        assert "gate_denial" in events

    def test_a_request_without_a_project_root_still_writes_its_row(
        self, tmp_path, monkeypatch,
    ) -> None:
        """No project_root in the cache is the pre-existing case; it must not drop the row."""
        import writ.server as server
        from writ.server import PreWriteCheckRequest, pre_write_check

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        cache = self._cache("", approved=["phase-a", "test-skeletons"])
        monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: cache)
        monkeypatch.setattr(server, "_pipeline", None)

        target = str(tmp_path / "loose" / "x.py")
        asyncio.run(pre_write_check(PreWriteCheckRequest(
            session_id="s-noroot", tool_input={"file_path": target},
            file_path=target, skill_dir=str(tmp_path / "skill"),
        )))

        written = list((tmp_path / "logs").rglob("audit.jsonl"))
        assert any(
            r["event"] == "write_attempt"
            for p in written for r in _read_jsonl(p)
        ), "an unscoped request must still record its write_attempt somewhere"
