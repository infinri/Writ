"""`/health` must name the file this daemon's rows actually land in (cycle S).

MEASURED 2026-09-04, before this cycle. `/health` reported
`"friction_log": "workflow-friction.log"`, a bare cwd-relative literal, because
`writ/server/routes/query.py` asked `writ/analysis/friction.py::resolve_log_path`, whose
third fallback is that literal, and `WRIT_FRICTION_LOG` is NOT set in the daemon's
environment (`systemctl --user show-environment` has zero matches; the only setter under
`hooks/` or `bin/` is `bin/lib/common.sh`, for dev and test use). The repo-root
`workflow-friction.log` is 23,814,070 bytes and its newest row is dated 2026-07-01. The
rows are elsewhere: `var/logs/github.com/infinri/Writ/audit.jsonl` held 780
`write_attempt` rows and `friction.jsonl` held 0 of them, because `write_attempt` is
AUDIT-classified by design (`STREAM_MAP`, `writ/shared/logging.py:57`). Reading the
reported path, finding no write rows, and concluding that write telemetry was dead is a
misdiagnosis that actually happened.

WHY NO TEST CAUGHT IT, also measured. `tests/conftest.py::_isolate_friction_log` is
autouse and sets `WRIT_FRICTION_LOG` for every test, and `emit` collapses EVERY stream
into that one file when the variable is set (`writ/shared/logging.py:674-680`), so the
resolver and the router cannot disagree under the suite's own conditions.
`tests/_daemon.py` then restarts the daemon whenever `/health` disagrees with the
variable it always sets, forcing agreement a second way. The production condition is the
variable ABSENT, and nothing exercised it.

This module uses the repo's existing escape hatch for exactly that problem,
`no_friction_isolation` (registered in `pyproject.toml`, honored at
`tests/conftest.py:110-115`), which deletes the variable and keeps the `WRIT_LOG_ROOT`
tmp_path sandbox. No new mechanism. A reader who removes the marker turns every test in
this module back into a collapse-mode test that passes for the wrong reason: the
classification split (`write_attempt` -> audit, `approval_pattern_match` -> friction) is
never exercised once `WRIT_FRICTION_LOG` is set, because `emit` takes its early-return
collapse branch before consulting `STREAM_MAP` at all.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from writ.analysis.friction import resolve_log_path
from writ.shared.logging import (
    UNRESOLVED_PROJECT,
    emit,
    emit_destination,
    request_project_scope,
    resolve_project,
    stream_path,
)

REPO = Path(__file__).resolve().parent.parent

pytestmark = [pytest.mark.no_friction_isolation]

try:
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    pytestmark.append(pytest.mark.skip(reason="httpx not installed"))


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sessions(path: Path) -> list[str]:
    return [row.get("session") for row in _rows(path)]


async def _health() -> dict:
    """The real /health payload through the ASGI app, with the graph deliberately absent.

    `_db` is patched to None so the branch is deterministic (another module in the same
    pytest process may have connected the module-level handle), and because the
    not_ready branch is the one an operator hits while asking a struggling daemon where
    its rows are.
    """
    from writ.server import app

    with patch("writ.server.writ_session", MagicMock()), patch("writ.server._db", None):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/health")
    return response.json()


class TestHealthNamesTheFileTheRowsLandIn:
    """`WRIT_FRICTION_LOG` unset, which is the production condition.

    The assertion is on the RENDERED artifact: a row is emitted and then looked for in
    the file `/health` named, so a field pointing at a plausible but unwritten path
    fails. Comparing the field against `emit_destination` instead would be green while
    both were wrong.
    """

    @pytest.mark.asyncio
    async def test_audit_log_holds_a_write_attempt_row_emitted_now(self) -> None:
        body = await _health()
        assert "audit_log" in body, f"/health reports no audit_log; keys={sorted(body)}"
        emit(None, "write_attempt", "sid-health-audit", "work", result="allow")

        assert "sid-health-audit" in _sessions(Path(body["audit_log"])), (
            f"the row is absent from the file /health named ({body['audit_log']}); "
            f"emit put it in {emit_destination('audit')}"
        )
        assert "sid-health-audit" not in _sessions(Path(body["friction_log"])), (
            "a gate-decision row must not be in the file reported as the friction log"
        )

    @pytest.mark.asyncio
    async def test_friction_log_holds_a_friction_row_emitted_now(self) -> None:
        body = await _health()
        assert "friction_log" in body, f"keys={sorted(body)}"
        emit(None, "approval_pattern_match", "sid-health-friction", "work",
             outcome="advanced")

        assert "sid-health-friction" in _sessions(Path(body["friction_log"])), (
            f"the row is absent from the file /health named ({body['friction_log']})"
        )
        assert "sid-health-friction" not in _sessions(Path(body["audit_log"])), (
            "a friction row must not be in the file reported as the audit log"
        )

    @pytest.mark.asyncio
    async def test_it_no_longer_answers_the_friction_log_resolver(self) -> None:
        """The defect itself, pinned by value and by shape.

        With the variable unset, `resolve_log_path()` answers the relative literal
        `workflow-friction.log`. A relative path is also unusable to the reader, since
        it means "relative to whatever cwd this daemon happens to have".
        """
        body = await _health()
        stale = str(resolve_log_path())
        for field in ("audit_log", "friction_log"):
            assert body[field] != stale, (
                f"{field} is still the friction-log resolver's answer ({stale})"
            )
            assert Path(body[field]).is_absolute(), (field, body[field])
        assert Path(body["audit_log"]).name == "audit.jsonl", body["audit_log"]
        assert Path(body["friction_log"]).name == "friction.jsonl", body["friction_log"]
        assert body["audit_log"] != body["friction_log"], (
            "with the collapse off the two streams are two files; one value for both "
            "would send the operator to the wrong one"
        )


class TestTheFieldsMeanTheSameThingWhenTheVariableIsSet:
    """The other half of the contract: the field must not silently mean something else
    in the dev and test condition.

    It means the same thing in both, "where a row of this stream lands", and under the
    collapse that is one file for every stream. This is also the alignment contract two
    readers already depend on: `tests/_daemon.py::ensure_daemon_aligned` and
    `scripts/lib/writ-server-lib.sh` compare `/health`'s `friction_log` against their
    own `WRIT_FRICTION_LOG` and restart the daemon when the two differ. With the
    variable set the reported value IS that variable, unchanged by this cycle.
    """

    @pytest.mark.asyncio
    async def test_both_fields_are_the_pinned_collapse_file(
        self, tmp_path, monkeypatch,
    ) -> None:
        collapsed = tmp_path / "pinned-collapse.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(collapsed))

        body = await _health()
        assert body["friction_log"] == str(collapsed), body["friction_log"]
        assert body["audit_log"] == str(collapsed), body["audit_log"]

    @pytest.mark.asyncio
    async def test_the_reported_collapse_file_is_where_a_row_really_goes(
        self, tmp_path, monkeypatch,
    ) -> None:
        collapsed = tmp_path / "pinned-collapse.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(collapsed))

        body = await _health()
        emit(None, "write_attempt", "sid-collapsed", "work", result="allow")
        assert "sid-collapsed" in _sessions(Path(body["audit_log"]))


class TestBothReturnBranchesAnswerTheSameWay:
    """A two-branch payload is where the second copy grows, and a reporter fixed for one
    caller and left wrong for another is this cycle's whole subject, so the keys come
    from one helper and that is asserted rather than trusted.
    """

    @pytest.mark.asyncio
    async def test_the_not_ready_branch_carries_both_fields(
        self, tmp_path, monkeypatch,
    ) -> None:
        """It carried neither before.

        Two readers compare `friction_log` against their own `WRIT_FRICTION_LOG`, so a
        missing key read as None, which never equals a set variable: a not_ready daemon
        was PERMANENTLY diverged and was restarted on every probe. It now answers the
        pinned file, so the comparison is about divergence again.
        """
        collapsed = tmp_path / "pinned-collapse.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(collapsed))

        body = await _health()
        assert body["status"] == "not_ready", body["status"]
        assert body["friction_log"] == str(collapsed)
        assert body["audit_log"] == str(collapsed)

    def test_neither_key_is_spelled_inside_the_route_body(self) -> None:
        import writ.server.routes.query as qroute

        helper = inspect.getsource(qroute._log_destinations)
        route = inspect.getsource(qroute.health)
        for key in ('"audit_log"', '"friction_log"'):
            assert key in helper, f"{key} must be produced by _log_destinations"
            assert key not in route, (
                f"{key} is spelled inside the /health body; both return branches must "
                "spread the one helper, or a later cycle fixes only one of them"
            )
        assert route.count("_log_destinations()") >= 2, (
            "both /health return branches must spread the destination helper"
        )


class TestDaemonAlignmentNoLongerSeesNoneOnNotReady:
    """The other live reader of `friction_log`, exercised directly rather than reasoned
    about: `tests/_daemon.py::ensure_daemon_aligned` computes
    `health.get("friction_log") == expected_friction`. Before this cycle the not_ready
    branch omitted the key entirely, so that `.get()` always answered None, which never
    equals a set `WRIT_FRICTION_LOG`: a not_ready daemon read as PERMANENTLY diverged and
    was restarted on every probe, regardless of whether it was actually pinned correctly.

    This drives the real not_ready route (through the same `_health()` helper the rest of
    this module uses) and the real `tests/_daemon.py` accessor functions, feeding the
    constructed payload in through `_daemon_health` rather than requiring a second live
    daemon, so the property is proved by executing the actual comparison, not by
    re-typing it here.
    """

    @pytest.mark.asyncio
    async def test_daemon_friction_log_reads_the_pinned_value_off_a_not_ready_payload(
        self, tmp_path, monkeypatch,
    ) -> None:
        from tests import _daemon

        pinned = tmp_path / "pinned-collapse.jsonl"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(pinned))

        body = await _health()
        assert body["status"] == "not_ready", body["status"]

        # Feed the real not_ready payload into the real accessor rather than requiring a
        # live daemon on the loopback port; this proves the SAME comparison
        # ensure_daemon_aligned makes, executed on the actual route output.
        monkeypatch.setattr(_daemon, "_daemon_health", lambda: body)

        seen = _daemon.daemon_friction_log()
        assert seen is not None, (
            "tests/_daemon.py::daemon_friction_log() still reads a not_ready payload as "
            "None; ensure_daemon_aligned's friction_aligned check can never be True "
            "against a set WRIT_FRICTION_LOG, so a not_ready daemon is restarted on "
            "every probe regardless of whether it is actually pinned correctly"
        )
        assert seen == _daemon.expected_friction_log() == str(pinned), (
            seen, str(pinned),
        )


class TestClassificationUnderThisRepoIdentity:
    """The split `/health`'s two fields rest on, asserted for a DERIVED project identity.

    `tests/test_logging_router.py:73-79` already disables the collapse, but it writes
    under `WRIT_LOG_PROJECT="proj-a"`, a name it invents, so it cannot catch a wrong
    destination for a repo whose scope comes from git. Here the scope is the identity
    `resolve_project` derives for the repo under test, and every assertion has an
    absence half, so reclassifying an event reddens the test instead of moving a row
    that a presence-only assertion would still accept.
    """

    def test_write_attempt_lands_in_audit_and_not_in_friction(self) -> None:
        project = resolve_project(str(REPO))
        with request_project_scope(str(REPO)):
            emit(None, "write_attempt", "sid-classify-audit", "work", result="allow")

        assert _sessions(stream_path(project, "audit")) == ["sid-classify-audit"]
        assert _rows(stream_path(project, "friction")) == [], (
            "a gate decision in the friction stream is the misclassification /health "
            "would then report honestly and uselessly"
        )

    def test_approval_pattern_match_lands_in_friction_and_not_in_audit(self) -> None:
        project = resolve_project(str(REPO))
        with request_project_scope(str(REPO)):
            emit(None, "approval_pattern_match", "sid-classify-friction", "work",
                 outcome="advanced")

        assert _sessions(stream_path(project, "friction")) == ["sid-classify-friction"]
        assert _rows(stream_path(project, "audit")) == []

    def test_the_identity_is_derived_and_not_invented(self) -> None:
        assert not os.environ.get("WRIT_LOG_PROJECT"), (
            "an override would make this module's project name invented, which is the "
            "exact gap in the existing router test"
        )
        project = resolve_project(str(REPO))
        assert project != UNRESOLVED_PROJECT, (
            "the repo under test must resolve a git identity, or the two assertions "
            "above are about the unresolved bucket rather than a real project"
        )

    def test_emit_destination_agrees_with_where_the_row_landed(self) -> None:
        """Executed, not reasoned. The reporter's answer and the writer's file are the
        same file, which is the property `/health` now rests on."""
        with request_project_scope(str(REPO)):
            emit(None, "write_attempt", "sid-agree", "work", result="allow")
            reported = emit_destination("audit")

        assert _sessions(reported) == ["sid-agree"]
