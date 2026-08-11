"""Regression: the SubagentStart hook must inject rules into every sub-agent.

Root cause (fixed 2026-06-18, commits 6d4e0a5 + 522541b): the hook built its RAG
query from `prompt`/`description`/`message`, but CC 2.1.181's SubagentStart payload
carries only `agent_type` (no task field). So the query never ran and ZERO rules
reached any sub-agent. A second bug leaked a stray status-JSON to stdout that would
shadow the real hookSpecificOutput once the first was fixed.

These tests pin the fix with a stub daemon (no real daemon / Neo4j needed):
  - no `task` in the payload still yields a non-empty additionalContext with rules
    (via the agent_type role-descriptive fallback), and the fallback query is
    actually sent to /query;
  - the `subagent_rules_injected` observability event fires with the rule ids and
    query_source (`agent_type` for the fallback, `task` when the payload carries one);
  - the hook emits exactly ONE JSON object on stdout (no stray line).

Per TEST-REGRESSION-001: these are RED before the fix, GREEN after.

--- Isolation cycle v2 (plan.md, Part 3), 2026-08-11 ---

TestSubagentQueryCarriesProjectRoot (appended below) pins capability:
"writ-subagent-start.sh's inline /query request carries the project root, so a
dispatched sub-agent is scoped to the dispatching project." The hook's inline
/query body (lines 210-217) currently sends exactly {query, budget_tokens,
exclude_rule_ids} -- no project field at all. It must gain `project_root`,
computed the same way writ-rag-inject.sh already does (`detect_project_root
"$(pwd -P)"`), from the DISPATCHING project's cwd (the hook runs in the parent
Claude Code process's cwd, not the sub-agent's).

RED today: the inline /query body has no project_root key and the hook never
calls detect_project_root.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.fixtures.net import free_port as _free_port

HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-subagent-start.sh"
    )
)


# Rule ids the stub daemon returns from /query; the hook must surface these.
_STUB_RULE_IDS = ["TEST-INJ-001", "TEST-INJ-002", "TEST-INJ-003"]


class _RulesDaemonHandler(BaseHTTPRequestHandler):
    """Stub daemon that is HEALTHY (so the hook runs its rules query) and returns a
    fixed rule set from /query. Records every query body so a test can assert the
    fallback actually sends a non-empty query when the payload has no task.

    received_bodies (added for the Part 3 project-root tests) records the FULL
    parsed /query request dict, not just the `query` text, so a test can inspect
    fields the original regression tests never needed to look at."""

    received_queries: list[str] = []
    received_bodies: list[dict] = []

    def log_message(self, *args):  # silence stderr noise
        pass

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode()
        elif self.path.startswith("/session/") and self.path.endswith("/mode"):
            body = json.dumps({"mode": "work"}).encode()
        elif self.path.startswith("/session/"):
            body = json.dumps(
                {"mode": "work", "current_phase": "planning", "gates_approved": []}
            ).encode()
        else:
            self.send_error(404)
            return
        self._ok(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/query":
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            _RulesDaemonHandler.received_bodies.append(parsed)
            try:
                _RulesDaemonHandler.received_queries.append(json.loads(raw).get("query", ""))
            except Exception:
                _RulesDaemonHandler.received_queries.append("<unparseable>")
            body = json.dumps(
                {
                    "rules": [
                        {
                            "rule_id": rid,
                            "severity": "high",
                            "authority": "human",
                            "domain": "Testing",
                            "score": 0.9,
                            "trigger": "t",
                            "statement": "s",
                        }
                        for rid in _STUB_RULE_IDS
                    ],
                    "mode": "standard",
                    "total_candidates": len(_STUB_RULE_IDS),
                    "latency_ms": 1,
                }
            ).encode()
            self._ok(body)
        else:
            self.send_error(404)

    def _ok(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def rules_daemon():
    """A healthy stub daemon on a free port that serves a fixed rule set."""
    _RulesDaemonHandler.received_queries = []
    _RulesDaemonHandler.received_bodies = []
    port = _free_port()
    srv = HTTPServer(("localhost", port), _RulesDaemonHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()


def _seed_parent(cache_dir, parent_id):
    path = os.path.join(str(cache_dir), f"writ-session-{parent_id}.json")
    with open(path, "w") as f:
        json.dump({"mode": "work", "current_phase": "planning", "gates_approved": []}, f)


def _run_start_hook(*, cache_dir, port, agent_id, agent_type, parent_id, task=None, cwd=None):
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_HOST"] = "localhost"
    env["WRIT_PORT"] = str(port)
    env["WRIT_FRICTION_LOG"] = os.path.join(str(cache_dir), "friction.log")
    envelope = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": parent_id,
        "hook_event_name": "SubagentStart",
    }
    if task is not None:
        envelope["task"] = task
    return subprocess.run(
        ["bash", HOOK],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=20,
    )


def _friction_events(cache_dir, event):
    path = os.path.join(str(cache_dir), "friction.log")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("event") == event:
                out.append(e)
    return out


def _stdout_json_objects(stdout: str) -> list[dict]:
    return [json.loads(ln) for ln in stdout.splitlines() if ln.strip()]


class TestSubagentRuleInjection:
    def test_rules_injected_without_task(self, tmp_path, rules_daemon):
        """No task in the payload (the CC 2.1.181 case) still injects rules into the
        sub-agent via additionalContext."""
        _seed_parent(tmp_path, "p1")
        r = _run_start_hook(
            cache_dir=tmp_path, port=rules_daemon,
            agent_id="a1", agent_type="writ-explorer", parent_id="p1",
        )
        assert r.returncode == 0, r.stderr
        objs = _stdout_json_objects(r.stdout)
        hso = next((o["hookSpecificOutput"] for o in objs if "hookSpecificOutput" in o), None)
        assert hso is not None, f"no hookSpecificOutput emitted: {r.stdout!r}"
        ctx = hso.get("additionalContext", "")
        assert ctx, "additionalContext is empty -- no rules reached the sub-agent"
        assert any(rid in ctx for rid in _STUB_RULE_IDS), (
            f"expected stub rule ids in additionalContext, got: {ctx[:200]!r}"
        )

    def test_fallback_query_is_nonempty(self, tmp_path, rules_daemon):
        """Without a task, the hook must still send a NON-empty (role-descriptive)
        query -- the empty-query path was the bug."""
        _seed_parent(tmp_path, "p2")
        _run_start_hook(
            cache_dir=tmp_path, port=rules_daemon,
            agent_id="a2", agent_type="writ-explorer", parent_id="p2",
        )
        assert _RulesDaemonHandler.received_queries, "daemon received no /query"
        assert all(q.strip() for q in _RulesDaemonHandler.received_queries), (
            f"a query was empty: {_RulesDaemonHandler.received_queries!r}"
        )

    def test_rules_injected_event_fires_with_fallback_source(self, tmp_path, rules_daemon):
        """The subagent_rules_injected observability event fires with the rule ids and
        query_source='agent_type' when there is no task."""
        _seed_parent(tmp_path, "p3")
        _run_start_hook(
            cache_dir=tmp_path, port=rules_daemon,
            agent_id="a3", agent_type="writ-explorer", parent_id="p3",
        )
        events = _friction_events(tmp_path, "subagent_rules_injected")
        assert len(events) == 1, f"expected one event, got {events}"
        e = events[0]
        assert e["rule_count"] == len(_STUB_RULE_IDS)
        assert e["rule_ids"] == _STUB_RULE_IDS
        assert e["query_source"] == "agent_type"
        assert e["agent_type"] == "writ-explorer"

    def test_query_source_is_task_when_task_present(self, tmp_path, rules_daemon):
        """When the payload carries a task (newer CC), query_source is 'task' and the
        task text is what gets queried."""
        _seed_parent(tmp_path, "p4")
        _run_start_hook(
            cache_dir=tmp_path, port=rules_daemon,
            agent_id="a4", agent_type="writ-implementer", parent_id="p4",
            task="fix the SQL injection in the auth login handler",
        )
        events = _friction_events(tmp_path, "subagent_rules_injected")
        assert len(events) == 1 and events[0]["query_source"] == "task", events
        assert any("SQL injection" in q for q in _RulesDaemonHandler.received_queries), (
            f"task text not used as query: {_RulesDaemonHandler.received_queries!r}"
        )

    def test_stdout_is_single_json_object(self, tmp_path, rules_daemon):
        """Regression for the stray status-JSON: stdout must be exactly one JSON object
        (the hookSpecificOutput), or CC reads the wrong line and drops the rules."""
        _seed_parent(tmp_path, "p5")
        r = _run_start_hook(
            cache_dir=tmp_path, port=rules_daemon,
            agent_id="a5", agent_type="writ-explorer", parent_id="p5",
        )
        objs = _stdout_json_objects(r.stdout)
        assert len(objs) == 1, f"expected 1 stdout JSON object, got {len(objs)}: {r.stdout!r}"
        assert "hookSpecificOutput" in objs[0]


class TestSubagentQueryCarriesProjectRoot:
    """Capability: writ-subagent-start.sh's inline /query request carries the
    project root, so a dispatched sub-agent is scoped to the dispatching project.

    The hook is run with `cwd` set to a directory that carries a marker
    (`.git`), matching detect_project_root's own walk-up-for-a-marker contract
    (bin/lib/common.sh:702) -- an unmarked tmp_path would resolve to itself,
    which is a valid root but would not distinguish "computed a root" from
    "always echoes an empty default".
    """

    def test_query_body_carries_the_dispatching_projects_root(self, tmp_path, rules_daemon):
        project_dir = tmp_path / "dispatching-project"
        (project_dir / ".git").mkdir(parents=True)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_parent(cache_dir, "p-root-1")

        _run_start_hook(
            cache_dir=cache_dir, port=rules_daemon,
            agent_id="a-root-1", agent_type="writ-explorer", parent_id="p-root-1",
            task="review the payment client for missing error handling",
            cwd=str(project_dir),
        )

        assert _RulesDaemonHandler.received_bodies, "daemon received no /query body"
        bodies = _RulesDaemonHandler.received_bodies
        assert any(b.get("project_root") == str(project_dir) for b in bodies), (
            f"no /query body carried the dispatching project's root "
            f"({project_dir!s}); got bodies={bodies!r}"
        )

    def test_a_different_dispatching_project_sends_a_different_root(self, tmp_path, rules_daemon):
        """Anti-vacuity: a hardcoded or empty project_root would pass the test
        above by accident if the stub happened to match. Two distinct marked
        directories must produce two distinct roots."""
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        (project_a / ".git").mkdir(parents=True)
        (project_b / ".git").mkdir(parents=True)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        _seed_parent(cache_dir, "p-root-a")
        _run_start_hook(
            cache_dir=cache_dir, port=rules_daemon,
            agent_id="a-root-a", agent_type="writ-explorer", parent_id="p-root-a",
            task="explore the codebase", cwd=str(project_a),
        )
        _seed_parent(cache_dir, "p-root-b")
        _run_start_hook(
            cache_dir=cache_dir, port=rules_daemon,
            agent_id="a-root-b", agent_type="writ-explorer", parent_id="p-root-b",
            task="explore the codebase", cwd=str(project_b),
        )

        roots_seen = {b.get("project_root") for b in _RulesDaemonHandler.received_bodies}
        assert str(project_a) in roots_seen
        assert str(project_b) in roots_seen
        assert str(project_a) != str(project_b)

    def test_query_still_carries_the_original_fields(self, tmp_path, rules_daemon):
        """The project_root addition must not displace the fields the original
        regression (6d4e0a5 + 522541b) pins."""
        project_dir = tmp_path / "dispatching-project"
        (project_dir / ".git").mkdir(parents=True)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _seed_parent(cache_dir, "p-root-3")

        _run_start_hook(
            cache_dir=cache_dir, port=rules_daemon,
            agent_id="a-root-3", agent_type="writ-explorer", parent_id="p-root-3",
            task="audit the auth login handler for injection risk",
            cwd=str(project_dir),
        )

        body = _RulesDaemonHandler.received_bodies[-1]
        assert body.get("query"), "the query text field must still be present and non-empty"
        assert "budget_tokens" in body
        assert "exclude_rule_ids" in body
