"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (4c): one composite daemon call
(POST /subagent/start-context) for writ-subagent-start.sh, replacing /health, /query,
/session/format and GET /subagent-role on the healthy path, with the current multi-call
path kept as the fallback.

RED at HEAD: `POST /subagent/start-context` does not exist (every route test gets a 404
through the real ASGI app rather than the documented 200); `writ.session.subagent_start_
context` does not exist (`fetch_start_context` skeleton-fails); `role_scope.py`'s GET is
hardcoded to `http://localhost:8765/subagent-role/` and ignores `WRIT_SESSION_BASE`; the
hook does not call the composite at all, so every count below is measured against HEAD's
UNCHANGED multi-call behaviour.

ISOLATION (read this before adding a case). Two real, live things exist on the machine
this file runs on: a Writ daemon on localhost:8765, and a unix socket at
~/.cache/writ/run/writ.sock connected to it. `role_scope.fetch_declared_scope`'s hardcoded
URL means ANY subprocess that resolves a sub-agent role on the `subagent_start` cache
path will, at HEAD, try to reach that real daemon regardless of WRIT_HOST/WRIT_PORT. Every
hook-subprocess test in this file therefore sets `http_proxy`/`HTTP_PROXY` to its own stub
(and `no_proxy`/`NO_PROXY` to "") IN ADDITION to WRIT_HOST/WRIT_PORT/WRIT_SOCKET: both
`curl` and Python's `urllib` honour an http proxy for a `localhost`-addressed request, so
the hardcoded call lands on the stub instead of the real daemon regardless of which
production file (fixed or not) makes it. Verified directly (scratch probe, 2026-09-25):
an unmodified `urllib.request.urlopen("http://localhost:8765/...")` under this env lands
on a local stub and never reaches the real daemon. In-process calls to
`role_scope.fetch_declared_scope` additionally monkeypatch `urllib.request.urlopen`
directly (a cached global opener can otherwise ignore a late `http_proxy` env change),
so NOTHING in this file can answer from -- or write to -- the operator's real daemon,
its socket, or its session caches.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fixtures.net import free_port
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401 -- autouse, protects real gate state

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-subagent-start.sh"


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


# --------------------------------------------------------------------------------- #
# 1. POST /subagent/start-context -- the route, mocked pipeline/formatter/db.
# --------------------------------------------------------------------------------- #

CANNED_RULES = [
    {"rule_id": "SEC-CTX-001", "severity": "high", "authority": "human",
     "domain": "security", "score": 0.9, "trigger": "t", "statement": "s"},
]
CANNED_QUERY_RESPONSE = {
    "rules": CANNED_RULES, "mode": "standard", "total_candidates": 1, "latency_ms": 1,
}
CANNED_FORMATTED = "SEC-CTX-001: s\nWRIT_META:" + json.dumps(
    {"rule_ids": ["SEC-CTX-001"], "cost": 42}
)


@pytest.fixture()
def asgi_client():
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    import writ.server as server

    transport = ASGITransport(app=server.app)

    async def _make():
        return AsyncClient(transport=transport, base_url="http://test")

    return server, _make


@pytest.mark.asyncio
async def test_returns_the_same_text_and_rule_ids_session_format_produces(asgi_client) -> None:
    from unittest.mock import AsyncMock, MagicMock

    server, make = asgi_client
    ac = await make()
    orig_pipeline, orig_db, orig_writ_session = server._pipeline, server._db, server.writ_session
    fake_pipeline = MagicMock()
    fake_pipeline.query = MagicMock(return_value=dict(CANNED_QUERY_RESPONSE))
    fake_writ_session = MagicMock()
    fake_writ_session.cmd_format = MagicMock(return_value=CANNED_FORMATTED)
    server._pipeline = fake_pipeline
    server._db = None
    server.writ_session = fake_writ_session
    try:
        reference = await ac.post("/session/format", json={"query_response": CANNED_QUERY_RESPONSE})
        assert reference.status_code == 200, reference.text
        ref_body = reference.json()

        composite = await ac.post("/subagent/start-context", json={
            "query": "explore the codebase", "budget_tokens": 500,
            "project_root": "", "role": "",
        })
    finally:
        server._pipeline, server._db, server.writ_session = orig_pipeline, orig_db, orig_writ_session
        await ac.aclose()

    assert composite.status_code == 200, composite.text
    body = composite.json()
    assert body["text"] == ref_body["text"], (body, ref_body)
    assert body["rule_ids"] == ref_body["meta"]["rule_ids"]
    assert body["query_rule_ids"] == ["SEC-CTX-001"]
    assert body["rule_count"] == 1
    assert body["retrieval"] == "ok"


@pytest.mark.asyncio
async def test_empty_role_makes_no_role_lookup(asgi_client) -> None:
    from unittest.mock import AsyncMock, MagicMock

    server, make = asgi_client
    ac = await make()
    orig_pipeline, orig_db, orig_writ_session = server._pipeline, server._db, server.writ_session
    fake_pipeline = MagicMock()
    fake_pipeline.query = MagicMock(return_value=dict(CANNED_QUERY_RESPONSE))
    fake_db = AsyncMock()
    fake_writ_session = MagicMock()
    fake_writ_session.cmd_format = MagicMock(return_value=CANNED_FORMATTED)
    server._pipeline, server._db, server.writ_session = fake_pipeline, fake_db, fake_writ_session
    try:
        resp = await ac.post("/subagent/start-context", json={
            "query": "x", "budget_tokens": 500, "project_root": "", "role": "",
        })
    finally:
        server._pipeline, server._db, server.writ_session = orig_pipeline, orig_db, orig_writ_session
        await ac.aclose()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role_lookup"] == "skipped"
    assert body["write_scope"] is None
    fake_db.get_subagent_role.assert_not_called()


@pytest.mark.asyncio
async def test_write_scope_null_and_empty_list_stay_distinct(asgi_client) -> None:
    from unittest.mock import AsyncMock, MagicMock

    server, make = asgi_client
    orig_pipeline, orig_db, orig_writ_session = server._pipeline, server._db, server.writ_session
    fake_pipeline = MagicMock()
    fake_pipeline.query = MagicMock(return_value=dict(CANNED_QUERY_RESPONSE))
    fake_writ_session = MagicMock()
    fake_writ_session.cmd_format = MagicMock(return_value=CANNED_FORMATTED)
    server._pipeline, server.writ_session = fake_pipeline, fake_writ_session
    try:
        for declared, expect in ((None, None), ([], [])):
            fake_db = AsyncMock()
            fake_db.get_subagent_role.return_value = {
                "role_id": "R", "name": "writ-explorer", "prompt_template": "",
                "model_preference": "sonnet", "dispatched_by": [], "write_scope": declared,
            }
            server._db = fake_db
            ac = await make()
            try:
                resp = await ac.post("/subagent/start-context", json={
                    "query": "x", "budget_tokens": 500, "project_root": "", "role": "writ-explorer",
                })
            finally:
                await ac.aclose()
            assert resp.status_code == 200, resp.text
            assert resp.json()["write_scope"] == expect
            assert resp.json()["role_lookup"] == "ok"
    finally:
        server._pipeline, server._db, server.writ_session = orig_pipeline, orig_db, orig_writ_session


@pytest.mark.asyncio
@pytest.mark.parametrize("break_pipeline,break_db,expect_retrieval,expect_role_lookup", [
    ("absent", "absent", "unavailable", "unavailable"),
    ("raises", "absent", "error", "unavailable"),
])
async def test_each_part_fails_open_independently(
    asgi_client, break_pipeline, break_db, expect_retrieval, expect_role_lookup
) -> None:
    from unittest.mock import MagicMock

    server, make = asgi_client
    orig_pipeline, orig_db, orig_writ_session = server._pipeline, server._db, server.writ_session
    if break_pipeline == "absent":
        server._pipeline = None
    else:
        boom = MagicMock()
        boom.query = MagicMock(side_effect=RuntimeError("pipeline exploded"))
        server._pipeline = boom
    server._db = None if break_db == "absent" else server._db
    server.writ_session = MagicMock()
    ac = await make()
    try:
        resp = await ac.post("/subagent/start-context", json={
            "query": "x", "budget_tokens": 500, "project_root": "", "role": "writ-explorer",
        })
    finally:
        server._pipeline, server._db, server.writ_session = orig_pipeline, orig_db, orig_writ_session
        await ac.aclose()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieval"] == expect_retrieval
    assert body["role_lookup"] == expect_role_lookup
    assert body["write_scope"] is None
    for key in ("retrieval", "text", "rule_ids", "query_rule_ids", "rule_count",
                "role_lookup", "write_scope"):
        assert key in body, f"missing key {key!r} on a degraded response: {body}"


# --------------------------------------------------------------------------------- #
# 2. `fetch_start_context` -- the client helper.
# --------------------------------------------------------------------------------- #


class _ContextStubHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"retrieval": "ok", "text": "hi", "rule_ids": ["SEC-CTX-001"], ' \
           b'"query_rule_ids": ["SEC-CTX-001"], "rule_count": 1, "role_lookup": "skipped", ' \
           b'"write_scope": null}'

    def log_message(self, *a) -> None:  # noqa: N802
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length) if length else None
        cls = type(self)
        self.send_response(cls.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cls.body)))
        self.end_headers()
        self.wfile.write(cls.body)


@pytest.fixture()
def context_stub():
    _ContextStubHandler.status = 200
    _ContextStubHandler.body = b'{"retrieval": "ok", "text": "hi", "rule_ids": [], ' \
        b'"query_rule_ids": [], "rule_count": 0, "role_lookup": "skipped", "write_scope": null}'
    server = HTTPServer(("127.0.0.1", 0), _ContextStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _fetch_start_context_module():
    try:
        from writ.session import subagent_start_context
    except ImportError as exc:
        pytest.fail(f"skeleton: writ/session/subagent_start_context.py does not exist yet ({exc})")
    return subagent_start_context


class TestFetchStartContext:
    def test_returns_the_parsed_answer_on_a_well_formed_200(
        self, context_stub, tmp_path, monkeypatch
    ) -> None:
        mod = _fetch_start_context_module()
        monkeypatch.setenv("WRIT_SOCKET", str(tmp_path / "no.sock"))
        monkeypatch.setenv("WRIT_SESSION_BASE", f"http://127.0.0.1:{context_stub}")
        result = mod.fetch_start_context("explore", "", "writ-explorer", budget_tokens=500)
        assert result is not None
        assert result["retrieval"] == "ok"

    def test_returns_none_on_404(self, tmp_path, monkeypatch) -> None:
        _ContextStubHandler.status = 404
        _ContextStubHandler.body = b"not found"
        server = HTTPServer(("127.0.0.1", 0), _ContextStubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            mod = _fetch_start_context_module()
            monkeypatch.setenv("WRIT_SOCKET", str(tmp_path / "no.sock"))
            monkeypatch.setenv("WRIT_SESSION_BASE", f"http://127.0.0.1:{server.server_address[1]}")
            assert mod.fetch_start_context("x", "", "writ-explorer") is None
        finally:
            server.shutdown()

    def test_returns_none_on_status_zero(self, tmp_path, monkeypatch) -> None:
        mod = _fetch_start_context_module()
        import socket

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        monkeypatch.setenv("WRIT_SOCKET", str(tmp_path / "no.sock"))
        monkeypatch.setenv("WRIT_SESSION_BASE", f"http://127.0.0.1:{port}")
        assert mod.fetch_start_context("x", "", "writ-explorer", timeout=0.5) is None

    def test_returns_none_on_a_malformed_body(self, tmp_path, monkeypatch) -> None:
        _ContextStubHandler.status = 200
        _ContextStubHandler.body = b"not json at all"
        server = HTTPServer(("127.0.0.1", 0), _ContextStubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            mod = _fetch_start_context_module()
            monkeypatch.setenv("WRIT_SOCKET", str(tmp_path / "no.sock"))
            monkeypatch.setenv("WRIT_SESSION_BASE", f"http://127.0.0.1:{server.server_address[1]}")
            assert mod.fetch_start_context("x", "", "writ-explorer") is None
        finally:
            server.shutdown()


# --------------------------------------------------------------------------------- #
# 3. `fetch_declared_scope` honours WRIT_SESSION_BASE.
# --------------------------------------------------------------------------------- #


class TestFetchDeclaredScopeHonoursSessionBase:
    """RED at HEAD: `role_scope._ROLE_URL` is hardcoded to `http://localhost:8765/...`
    and never reads WRIT_SESSION_BASE. `urllib.request.urlopen` is monkeypatched
    directly (not merely the env var) so this in-process test can never reach the
    real daemon even while the code under test still ignores the env override."""

    def test_the_get_goes_to_writ_session_base(self, monkeypatch) -> None:
        from writ.session import role_scope

        captured: dict = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"write_scope": ["plan.md"]}).encode()

        def _fake_urlopen(url, timeout=None):
            captured["url"] = url
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setenv("WRIT_SESSION_BASE", "http://127.0.0.1:59999")
        scope = role_scope.fetch_declared_scope("writ-explorer")
        assert captured.get("url", "").startswith("http://127.0.0.1:59999/"), (
            f"fetch_declared_scope must build its URL from WRIT_SESSION_BASE when set; "
            f"got {captured}"
        )
        assert scope == ["plan.md"]


# --------------------------------------------------------------------------------- #
# 4. The hook itself, through a PATH shim + a real stub daemon.
# --------------------------------------------------------------------------------- #

_CALL_MARKER = "@@WRIT_CTX_EXEC_BUDGET_CALL@@"


def _shim_dir(tmp_path):
    shim = tmp_path / "path-shim"
    shim.mkdir()
    for name in ("python3", "jq", "curl"):
        real = shutil.which(name)
        assert real, f"no {name} on PATH to build the exec-count shim from"
        wrapper = shim / name
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "{_CALL_MARKER}\\n%s\\n" "$*" >> "${{WRIT_EXEC_COUNTER_{name.upper()}:?}}"\n'
            f'exec "{real}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return shim


def _exec_count(counter: Path) -> int:
    return sum(1 for ln in counter.read_text().splitlines() if ln == _CALL_MARKER)


def _bare_path(raw_path: str) -> str:
    """self.path as an origin-form path, whether the request arrived origin-form
    ("/health") or absolute-form via an HTTP proxy ("http://host:port/health")."""
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        return urllib.parse.urlsplit(raw_path).path
    return raw_path


class _CompositeStubHandler(BaseHTTPRequestHandler):
    """Serves the composite route plus the full legacy fallback surface (/health,
    /query, /session/format, GET /subagent-role/<name>), and records every request's
    bare path. `serve_composite=False` makes it 404 the composite only, for the
    fallback capability."""

    hits: list[str] = []
    serve_composite = True
    # False reproduces tests/test_subagent_start_exec_budget.py's own healthy-stub
    # shape (health + query only): the daemon 404s /session/format, so the hook's
    # local-python formatting fallback runs, which IS today's injection-path exec
    # budget (python3 11, jq 6, curl 3) that capability pins.
    serve_format = True

    def log_message(self, *a) -> None:  # noqa: N802
        pass

    def _record(self) -> str:
        path = _bare_path(self.path)
        type(self).hits.append(path)
        return path

    def _ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self._record()
        if path == "/health":
            self._ok(b'{"status":"ok"}')
        elif path.startswith("/subagent-role/"):
            self._ok(json.dumps({"write_scope": ["plan.md"]}).encode())
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        path = self._record()
        if path == "/subagent/start-context" and type(self).serve_composite:
            self._ok(json.dumps({
                "retrieval": "ok", "text": "SEC-CTX-001: s",
                "rule_ids": ["SEC-CTX-001"], "query_rule_ids": ["SEC-CTX-001"],
                "rule_count": 1, "role_lookup": "ok", "write_scope": ["plan.md"],
            }).encode())
        elif path == "/subagent/start-context":
            self.send_error(404)
        elif path == "/query":
            self._ok(json.dumps({
                "rules": [{"rule_id": "SEC-CTX-001", "severity": "high",
                           "authority": "human", "domain": "security", "score": 0.9,
                           "trigger": "t", "statement": "s"}],
                "mode": "standard", "total_candidates": 1, "latency_ms": 1,
            }).encode())
        elif path == "/session/format" and not type(self).serve_format:
            self.send_error(404)
        elif path == "/session/format":
            self._ok(("SEC-CTX-001: s\nWRIT_META:" + json.dumps(
                {"rule_ids": ["SEC-CTX-001"], "cost": 1}
            )).encode())
        else:
            self.send_error(404)


@pytest.fixture()
def composite_stub():
    _CompositeStubHandler.hits = []
    _CompositeStubHandler.serve_composite = True
    _CompositeStubHandler.serve_format = True
    server = HTTPServer(("127.0.0.1", 0), _CompositeStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _seed_parent(cache_dir: Path, session_id: str, **overrides) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = {"mode": "work", "current_phase": "planning", "gates_approved": []}
    state.update(overrides)
    (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(state))


PARENT = "ctx-parent-1"
AGENT = "ctx-agent-1"


def _run_hook(cache_dir: Path, port: int, *, agent_id: str = AGENT, parent_id: str = PARENT,
              agent_type: str = "writ-explorer", shim: Path | None = None,
              exec_counters: dict[str, Path] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = str(cache_dir / "friction.log")
    env["WRIT_HOST"] = "127.0.0.1"
    env["WRIT_PORT"] = str(port)
    env["WRIT_SOCKET"] = str(cache_dir / "no-writ.sock")  # guaranteed absent
    # Belt-and-suspenders isolation: intercept ANY hardcoded localhost:8765 request
    # (role_scope.py's known bug at HEAD) via an HTTP proxy pointed at THIS SAME
    # stub, so it can never reach the operator's real daemon. See module docstring.
    env["http_proxy"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
    env["no_proxy"] = env["NO_PROXY"] = ""
    if shim is not None:
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    if exec_counters is not None:
        for name, path in exec_counters.items():
            path.write_text("")
            env[f"WRIT_EXEC_COUNTER_{name}"] = str(path)
    envelope = {
        "agent_id": agent_id, "agent_type": agent_type, "session_id": parent_id,
        "hook_event_name": "SubagentStart", "task": "explore and understand the module",
    }
    return subprocess.run(
        ["bash", str(HOOK)], input=json.dumps(envelope), capture_output=True, text=True,
        env=env, timeout=20,
    )


def _child_cache(cache_dir: Path, agent_id: str = AGENT) -> dict:
    return json.loads((cache_dir / f"writ-session-{agent_id}.json").read_text())


def _friction_events(cache_dir: Path, event: str) -> list[dict]:
    path = cache_dir / "friction.log"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("event") == event:
            out.append(row)
    return out


class TestCompositePathMakesOneDaemonRequest:
    """RED at HEAD: HEAD's hook never calls POST /subagent/start-context, so this
    always shows 4 legacy requests and 0 composite requests until 4c lands."""

    def test_exactly_one_request_the_composite(self, tmp_path, composite_stub) -> None:
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        result = _run_hook(cache_dir, composite_stub)
        assert result.returncode == 0, result.stderr
        assert _CompositeStubHandler.hits == ["/subagent/start-context"], (
            f"expected exactly one composite request and nothing else: {_CompositeStubHandler.hits}"
        )


class TestExecBudgetOnTheCompositePath:
    """Pinned literals, MEASURED against this exact harness at HEAD, 2026-09-25 (a
    real subprocess run, the PATH-shim marker-line count, the full-featured stub
    that also answers /session/format and GET /subagent-role so it can serve
    either the current multi-call path or the future single-composite-call path
    unchanged -- "the same stub" the plan's capability text asks for):
    python3=9, jq=5, curl=3. HEAD's unmodified hook takes the full multi-call path
    regardless of what the stub serves (it never calls the composite yet). The
    plan's stated deltas are python3 -2, jq -1, curl -3 (curl to 0) once 4c lands."""

    _HEAD_PYTHON3 = 9
    _HEAD_JQ = 5
    _HEAD_CURL = 3

    def _measure(self, tmp_path, composite_stub) -> dict[str, int]:
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq",
                    "CURL": tmp_path / "c_curl"}
        result = _run_hook(cache_dir, composite_stub, shim=shim, exec_counters=counters)
        assert result.returncode == 0, result.stderr
        return {name: _exec_count(path) for name, path in counters.items()}

    def test_python3_is_head_minus_2(self, tmp_path, composite_stub) -> None:
        counts = self._measure(tmp_path, composite_stub)
        assert counts["PYTHON3"] == self._HEAD_PYTHON3 - 2, counts

    def test_jq_is_head_minus_1(self, tmp_path, composite_stub) -> None:
        counts = self._measure(tmp_path, composite_stub)
        assert counts["JQ"] == self._HEAD_JQ - 1, counts

    def test_curl_is_head_minus_3(self, tmp_path, composite_stub) -> None:
        counts = self._measure(tmp_path, composite_stub)
        assert counts["CURL"] == self._HEAD_CURL - 3, counts


class TestCompositePathThreadsRoleWriteScopeAndInjectedIds:
    def test_child_cache_carries_role_write_scope_and_injected_ids(
        self, tmp_path, composite_stub
    ) -> None:
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        result = _run_hook(cache_dir, composite_stub)
        assert result.returncode == 0, result.stderr
        child = _child_cache(cache_dir)
        assert child.get("role_write_scope") == ["plan.md"]
        assert child.get("loaded_rule_ids") == ["SEC-CTX-001"]
        rows = _friction_events(cache_dir, "subagent_rules_injected")
        assert len(rows) == 1
        assert rows[0].get("rule_ids") == ["SEC-CTX-001"]


class TestFallbackWhenComposite404s:
    """The daemon predates the route: /health, /query and GET /subagent-role are
    each requested once, and the injection-path exec budget holds (pinned in
    tests/test_subagent_start_exec_budget.py::TestInjectionPathExecBudget)."""

    def test_each_legacy_call_is_made_exactly_once(self, tmp_path, composite_stub) -> None:
        _CompositeStubHandler.serve_composite = False
        _CompositeStubHandler.serve_format = False
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        result = _run_hook(cache_dir, composite_stub)
        assert result.returncode == 0, result.stderr
        counts = {p: _CompositeStubHandler.hits.count(p) for p in set(_CompositeStubHandler.hits)}
        assert counts.get("/health") == 1, counts
        assert counts.get("/query") == 1, counts
        assert len([p for p in _CompositeStubHandler.hits if p.startswith("/subagent-role/")]) == 1, counts

    def test_injection_path_exec_budget_holds(self, tmp_path, composite_stub) -> None:
        """Pinned to tests/test_subagent_start_exec_budget.py::TestInjectionPathExec
        Budget's OWN currently-green after-4a numbers (python3 11, jq 6, curl 3):
        this scenario is byte-for-byte that file's "healthy stub with rules" shape
        (health + query only, /session/format 404s so the local-format fallback
        runs), reproduced here because 4c's fallback must still land on it."""
        _CompositeStubHandler.serve_composite = False
        _CompositeStubHandler.serve_format = False
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq",
                    "CURL": tmp_path / "c_curl"}
        result = _run_hook(cache_dir, composite_stub, shim=shim, exec_counters=counters)
        assert result.returncode == 0, result.stderr
        counts = {name: _exec_count(path) for name, path in counters.items()}
        assert counts["PYTHON3"] == 11, counts
        assert counts["JQ"] == 6, counts
        assert counts["CURL"] == 3, counts


class TestDaemonDownExecBudgetHolds:
    """Pinned to tests/test_subagent_start_exec_budget.py::TestDaemonDownExecBudget's
    own currently-green after-4a numbers (python3 6, jq 4, curl 1), reproduced here
    (MEASURED against this harness, 2026-09-25) because 4c must not raise them: the
    composite attempt and, on failure, the one fallback fetch both run inside the
    seed process that already exists."""

    def test_daemon_down_python3_jq_curl_unchanged(self, tmp_path) -> None:
        import socket

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, PARENT)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq",
                    "CURL": tmp_path / "c_curl"}
        result = _run_hook(cache_dir, port, shim=shim, exec_counters=counters)
        assert result.returncode == 0, result.stderr
        counts = {name: _exec_count(path) for name, path in counters.items()}
        assert counts["PYTHON3"] == 6, counts
        assert counts["JQ"] == 4, counts
        assert counts["CURL"] == 1, counts


# --------------------------------------------------------------------------------- #
# 7. The composite is a body-carrying read, served over TCP under WRIT_TCP_READONLY.
# --------------------------------------------------------------------------------- #


class TestCompositeIsServedUnderTcpReadonly:
    """With WRIT_TCP_READONLY=1 the daemon refuses every TCP POST it does not name as
    a body-carrying read. /subagent/start-context only reads (query_rules, the
    formatter, get_subagent_role), so refusing it would silently push every TCP hook
    onto the multi-call fallback. It must be served like /query, while a
    state-changing POST is still refused."""

    @staticmethod
    def _tcp_post(path: str) -> dict:
        return {"type": "http", "client": ("127.0.0.1", 5555), "method": "POST", "path": path}

    def test_composite_is_served_and_writes_still_refused(self, monkeypatch):
        import writ.server.transport as transport

        monkeypatch.setenv("WRIT_TCP_READONLY", "1")
        assert "/subagent/start-context" in transport.TCP_READONLY_POST_ALLOWLIST
        assert transport.tcp_refusal(self._tcp_post("/subagent/start-context")) is None
        assert transport.tcp_refusal(self._tcp_post("/query")) is None
        assert transport.tcp_refusal(self._tcp_post("/feedback")) is not None
        assert transport.tcp_refusal(self._tcp_post("/feedback/batch")) is not None
