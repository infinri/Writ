"""The degradation contract: jq, curl and envsubst are optional accelerators, never
requirements (plan.md / capabilities.md items 10-17, 19-20; item 21 is operational-only
and verified by a real fresh-machine install, not here).

Every hook and helper this file drives must behave identically whether the fast-path
tool is present or forced absent (WRIT_NO_JQ / WRIT_NO_CURL) or genuinely missing from
PATH. Two named defects are the reason this file exists and are pinned directly:

  * writ-rag-inject.sh turned a healthy /prompt-bundle response into "query failed"
    when jq was missing (the `${BUNDLE_ERR:-1}` default-to-failed bug).
  * auto-approve-gate.sh lost gate approval entirely when curl was missing (no
    fallback on the /advance-phase POST).

All daemon interactions here hit a throwaway stdlib http.server on an ephemeral port,
never the real Writ daemon and never WRIT_PORT=8799 (the suite's own test daemon).
WRIT_CACHE_DIR is always pointed at a tmp_path subdirectory -- var/session is never
touched.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

SKILL_ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = SKILL_ROOT / "bin" / "lib" / "common.sh"
WRIT_INSTALL = SKILL_ROOT / "bin" / "lib" / "writ_install.py"
SESSION_HELPER = SKILL_ROOT / "bin" / "lib" / "writ-session.py"
SERVER_LIB = SKILL_ROOT / "scripts" / "lib" / "writ-server-lib.sh"
RAG_INJECT = SKILL_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh"
AUTO_APPROVE = SKILL_ROOT / "hooks" / "scripts" / "auto-approve-gate.sh"
BOOTSTRAP = SKILL_ROOT / "scripts" / "bootstrap.sh"
BOOTSTRAP_PLUGIN = SKILL_ROOT / "scripts" / "bootstrap-plugin.sh"
SESSION_START_BOOTSTRAP = SKILL_ROOT / "hooks" / "scripts" / "session-start-bootstrap.sh"

SCAN_ROOTS = [SKILL_ROOT / "scripts", SKILL_ROOT / "hooks", SKILL_ROOT / "bin"]

# Tools every hook/script in this suite may legitimately need that are NOT part of
# the jq/curl/envsubst question. jq, curl and envsubst are added per-test, never here.
CORE_TOOLS = [
    "bash", "python3", "mkdir", "dirname", "basename", "flock", "seq", "sleep",
    "nohup", "cat", "rm", "cp", "mv", "grep", "sed", "head", "tr", "ps", "date",
    "cut", "chmod", "wc",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _limited_path_bin(tmp_path: Path, tools: list[str]) -> Path:
    """A PATH containing only symlinks to the named tools -- the rest are absent,
    including any tool not listed (in particular jq/curl/envsubst unless asked for)."""
    fake_bin = tmp_path / "bin"
    # parents=True: callers pass a not-yet-created subdirectory (the curl-present vs
    # curl-absent parity case uses tmp_path/"with-curl" and tmp_path/"no-curl").
    fake_bin.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        found = shutil.which(tool)
        target = fake_bin / tool
        if found and not target.exists():
            target.symlink_to(found)
    return fake_bin


class _StubHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    requests: list = []

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self.requests.append({
            "method": self.command,
            "path": self.path,
            "body": body.decode("utf-8", "replace"),
        })
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
def stub_daemon(routes: dict):
    """routes: {path: (status_code, json_body)}. Yields (base_url, requests_list)."""
    handler = type("Handler", (_StubHandler,), {"routes": dict(routes), "requests": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}", handler.requests
    finally:
        server.shutdown()
        server.server_close()


def _host_port(base_url: str) -> tuple[str, str]:
    host_port = base_url.split("://", 1)[1]
    host, port = host_port.split(":")
    return host, port


def _extract_bash_function(text: str, name: str) -> str:
    """Body of a `name() { ... }` bash function defined at column 0 with its closing
    brace also at column 0 -- the style every function in this repo uses."""
    m = re.search(rf"^{re.escape(name)}\(\)\s*\{{(.*?)^\}}", text, re.M | re.S)
    return m.group(1) if m else ""


def _all_scripts() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_dir():
            out.extend(root.rglob("*.sh"))
            out.extend(root.rglob("*.py"))
    return out


def _seed_work_mode_planning(cache_dir: Path, session_id: str) -> None:
    """Real local session state (mode=work, current_phase=planning) via the actual
    writ-session.py CLI -- auto-approve-gate.sh's CURRENT_PHASE read is a direct
    local subprocess call, not a daemon round-trip, so this must be genuine state."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["python3", str(SESSION_HELPER), "mode", "set", "work", session_id],
        env={**os.environ, "WRIT_CACHE_DIR": str(cache_dir)},
        capture_output=True, text=True, timeout=15,
    )


# --------------------------------------------------------------------------- #
# Item 10: writ_http_get / writ_http_post curl-vs-WRIT_NO_CURL=1 equivalence
# --------------------------------------------------------------------------- #


def _writ_http_get(url: str, *, no_curl: bool = False, fail: bool = False, timeout: int = 15):
    env_prefix = "WRIT_NO_CURL=1 " if no_curl else ""
    flag = " --fail" if fail else ""
    script = (
        f"source {shlex.quote(str(COMMON_SH))}; "
        f"{env_prefix}writ_http_get {shlex.quote(url)}{flag}"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)


def _writ_http_post(url: str, body: str, *, no_curl: bool = False, fail: bool = False, timeout: int = 15):
    env_prefix = "WRIT_NO_CURL=1 " if no_curl else ""
    flag = " --fail" if fail else ""
    script = (
        f"source {shlex.quote(str(COMMON_SH))}; "
        f"{env_prefix}writ_http_post {shlex.quote(url)} {shlex.quote(body)}{flag}"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)


class TestHttpShimCurlVsNoCurlEquivalence:
    def test_get_200_body_identical_with_and_without_curl(self):
        with stub_daemon({"/ok": (200, {"msg": "hello"})}) as (base, _reqs):
            url = f"{base}/ok"
            with_curl = _writ_http_get(url)
            no_curl = _writ_http_get(url, no_curl=True)
        assert with_curl.returncode == 0
        assert no_curl.returncode == 0
        assert with_curl.stdout == no_curl.stdout
        assert json.loads(with_curl.stdout)["msg"] == "hello"

    def test_get_4xx_body_identical_without_fail_flag(self):
        with stub_daemon({"/bad": (404, {"error": "nope"})}) as (base, _reqs):
            url = f"{base}/bad"
            with_curl = _writ_http_get(url)
            no_curl = _writ_http_get(url, no_curl=True)
        assert with_curl.stdout == no_curl.stdout
        assert json.loads(with_curl.stdout)["error"] == "nope"

    def test_get_5xx_body_identical_without_fail_flag(self):
        with stub_daemon({"/bad": (500, {"error": "boom"})}) as (base, _reqs):
            url = f"{base}/bad"
            with_curl = _writ_http_get(url)
            no_curl = _writ_http_get(url, no_curl=True)
        assert with_curl.stdout == no_curl.stdout
        assert json.loads(with_curl.stdout)["error"] == "boom"

    def test_fail_mode_emits_nothing_and_exits_nonzero_on_4xx_get(self):
        with stub_daemon({"/bad": (404, {"error": "nope"})}) as (base, _reqs):
            url = f"{base}/bad"
            with_curl = _writ_http_get(url, fail=True)
            no_curl = _writ_http_get(url, fail=True, no_curl=True)
        assert with_curl.returncode != 0
        assert no_curl.returncode != 0
        assert with_curl.stdout == ""
        assert no_curl.stdout == ""

    def test_post_body_delivered_and_response_identical(self):
        with stub_daemon({"/echo": (200, {"ok": True})}) as (base, reqs):
            url = f"{base}/echo"
            body = json.dumps({"token": "abc123", "cwd": "/tmp/project"})
            with_curl = _writ_http_post(url, body)
            no_curl = _writ_http_post(url, body, no_curl=True)
        assert with_curl.stdout == no_curl.stdout
        assert len(reqs) == 2
        for req in reqs:
            assert json.loads(req["body"]) == {"token": "abc123", "cwd": "/tmp/project"}

    def test_post_fail_mode_emits_nothing_and_exits_nonzero_on_4xx(self):
        with stub_daemon({"/bad": (422, {"error": "invalid"})}) as (base, _reqs):
            url = f"{base}/bad"
            with_curl = _writ_http_post(url, "{}", fail=True)
            no_curl = _writ_http_post(url, "{}", fail=True, no_curl=True)
        assert with_curl.returncode != 0
        assert no_curl.returncode != 0
        assert with_curl.stdout == ""
        assert no_curl.stdout == ""


# --------------------------------------------------------------------------- #
# Items 11-12: jq absent -- writ-rag-inject.sh stays healthy
# --------------------------------------------------------------------------- #


def _rag_inject_routes(session_id: str) -> dict:
    return {
        f"/session/{session_id}": (200, {
            "mode": "conversation", "loaded_rule_ids": [], "remaining_budget": 8000,
            "is_orchestrator": False, "recall_briefed": False,
        }),
        f"/session/{session_id}/should-skip": (200, {"known": True, "should_skip": False}),
        f"/session/{session_id}/check-escalation": (200, {"needed": False}),
        "/recall": (200, {"briefing": "RECALL BRIEFING TEXT"}),
        "/prompt-bundle": (200, {
            # error=False is part of EVERY healthy response from the real endpoint
            # (writ/server/routes/query.py sets it in the base dict and flips it to True
            # only on a /query failure). Omitting it here made the stub falsier than the
            # server and let a "non-empty means failed" read of the field pass while
            # reporting every real healthy bundle as "query failed".
            "error": False,
            "always_on_block": "ALWAYS-ON BLOCK TEXT",
            "rules_text": "RULES BLOCK TEXT",
            "methodology_block": "METHODOLOGY BLOCK TEXT",
            "nudge": "",
            "broad_meta": {"cost": 0, "rule_ids": []},
            "ao_meta": {"tokens": 0, "count": 0, "rule_ids": []},
            "method_meta": {"cost": 0, "rule_ids": [], "query_source": "methodology"},
        }),
    }


def _run_rag_inject(tmp_path: Path, base_url: str, *, session_id: str,
                     prompt: str = "please implement the widget handler correctly",
                     env_overrides: dict | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    host, port = _host_port(base_url)
    stdin_payload = json.dumps({
        "session_id": session_id, "prompt": prompt, "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
    })
    env = {
        **os.environ,
        "WRIT_HOST": host, "WRIT_PORT": port,
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(RAG_INJECT)], input=stdin_payload, capture_output=True, text=True,
        env=env, cwd=str(tmp_path), timeout=timeout,
    )


class TestPromptBundleSurvivesJqAbsence:
    def test_healthy_bundle_yields_all_blocks_with_jq_forced_absent(self, tmp_path):
        session_id = "rag-jq-forced"
        with stub_daemon(_rag_inject_routes(session_id)) as (base, _reqs):
            r = _run_rag_inject(tmp_path, base, session_id=session_id, env_overrides={"WRIT_NO_JQ": "1"})
        assert r.returncode == 0
        assert "query failed" not in r.stdout.lower()
        assert "server unavailable" not in r.stdout.lower()
        assert "ALWAYS-ON BLOCK TEXT" in r.stdout
        assert "RULES BLOCK TEXT" in r.stdout
        assert "METHODOLOGY BLOCK TEXT" in r.stdout

    def test_healthy_bundle_yields_all_blocks_with_jq_absent_from_path(self, tmp_path):
        fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS + ["curl"])
        session_id = "rag-path-stripped"
        with stub_daemon(_rag_inject_routes(session_id)) as (base, _reqs):
            r = _run_rag_inject(
                tmp_path, base, session_id=session_id,
                env_overrides={"PATH": str(fake_bin)},
            )
        assert r.returncode == 0
        assert "query failed" not in r.stdout.lower()
        assert "ALWAYS-ON BLOCK TEXT" in r.stdout

    def test_recall_briefing_still_injected_with_jq_absent(self, tmp_path):
        session_id = "rag-recall"
        with stub_daemon(_rag_inject_routes(session_id)) as (base, reqs):
            r = _run_rag_inject(tmp_path, base, session_id=session_id, env_overrides={"WRIT_NO_JQ": "1"})
        assert r.returncode == 0
        assert "RECALL BRIEFING TEXT" in r.stdout
        assert any(req["path"].startswith("/recall") and req["method"] == "POST" for req in reqs)

    def test_genuine_bundle_error_true_still_reports_query_failed(self, tmp_path):
        # Review fix: the truthiness fix must keep its POSITIVE contract too -- a real
        # error:true from the daemon still surfaces as a failed query, with no rule
        # blocks emitted from the errored bundle.
        session_id = "rag-error-true"
        routes = _rag_inject_routes(session_id)
        status, bundle = routes["/prompt-bundle"]
        bundle = dict(bundle, error=True)
        routes["/prompt-bundle"] = (status, bundle)
        with stub_daemon(routes) as (base, _reqs):
            r = _run_rag_inject(tmp_path, base, session_id=session_id,
                                env_overrides={"WRIT_NO_JQ": "1"})
        assert r.returncode == 0
        assert "query failed" in r.stdout.lower()
        assert "ALWAYS-ON BLOCK TEXT" not in r.stdout

    def test_genuine_bundle_error_string_still_reports_query_failed(self, tmp_path):
        session_id = "rag-error-str"
        routes = _rag_inject_routes(session_id)
        status, bundle = routes["/prompt-bundle"]
        bundle = dict(bundle, error="neo4j unreachable")
        routes["/prompt-bundle"] = (status, bundle)
        with stub_daemon(routes) as (base, _reqs):
            r = _run_rag_inject(tmp_path, base, session_id=session_id,
                                env_overrides={"WRIT_NO_JQ": "1"})
        assert r.returncode == 0
        assert "query failed" in r.stdout.lower()
        assert "ALWAYS-ON BLOCK TEXT" not in r.stdout

    def test_bundle_error_default_no_longer_treats_healthy_as_failed(self):
        """Regression guard for the exact CRITICAL bug named in plan.md."""
        text = RAG_INJECT.read_text()
        assert "${BUNDLE_ERR:-1}" not in text, (
            "an absent/empty error field must never default to 'query failed'"
        )

    def test_prompt_bundle_post_no_longer_a_bare_curl_call(self):
        text = RAG_INJECT.read_text()
        assert "writ_http_post" in text, "the /prompt-bundle POST must go through writ_http_post"
        assert not re.search(r"curl\s+-s[^\n]*prompt-bundle", text), (
            "the /prompt-bundle POST must not be a raw curl call"
        )


# --------------------------------------------------------------------------- #
# Item 13: curl absent -- auto-approve-gate.sh still POSTs /advance-phase
# --------------------------------------------------------------------------- #


def _run_auto_approve_gate(tmp_path: Path, base_url: str, *, session_id: str,
                            prompt: str = "approved", curl_present: bool = False,
                            timeout: int = 20) -> subprocess.CompletedProcess:
    host, port = _host_port(base_url)
    tools = CORE_TOOLS + (["curl"] if curl_present else [])
    fake_bin = _limited_path_bin(tmp_path, tools)
    stdin_payload = json.dumps({"session_id": session_id, "prompt": prompt})
    env = {
        **os.environ,
        "PATH": str(fake_bin),
        "WRIT_HOST": host, "WRIT_PORT": port,
        "WRIT_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(AUTO_APPROVE)], input=stdin_payload, capture_output=True, text=True,
        env=env, cwd=str(tmp_path), timeout=timeout,
    )


class TestAdvancePhaseSurvivesCurlAbsence:
    def test_advance_phase_reaches_the_stub_server_with_curl_absent(self, tmp_path):
        session_id = "approve-no-curl"
        _seed_work_mode_planning(tmp_path / "cache", session_id)
        routes = {
            f"/session/{session_id}/advance-phase": (200, {
                "phase": "testing", "advanced": True, "token_spent": True,
            }),
        }
        with stub_daemon(routes) as (base, reqs):
            r = _run_auto_approve_gate(tmp_path, base, session_id=session_id, curl_present=False)
        assert r.returncode == 0
        posts = [x for x in reqs if x["method"] == "POST" and "advance-phase" in x["path"]]
        assert len(posts) == 1, "the /advance-phase POST must still be sent with curl absent"

    def test_advance_phase_payload_carries_token_and_cwd_with_curl_absent(self, tmp_path):
        session_id = "approve-payload"
        _seed_work_mode_planning(tmp_path / "cache", session_id)
        routes = {
            f"/session/{session_id}/advance-phase": (200, {
                "phase": "testing", "advanced": True, "token_spent": True,
            }),
        }
        with stub_daemon(routes) as (base, reqs):
            _run_auto_approve_gate(tmp_path, base, session_id=session_id, curl_present=False)
        posts = [x for x in reqs if x["method"] == "POST" and "advance-phase" in x["path"]]
        assert posts, "no /advance-phase POST was recorded"
        body = json.loads(posts[0]["body"])
        assert body.get("token"), "the single-use gate token must be in the payload"
        assert body.get("cwd") == str(tmp_path), "cwd must be sent so the server resolves the project root"

    def test_advance_confirmation_shown_to_the_user(self, tmp_path):
        session_id = "approve-confirm"
        _seed_work_mode_planning(tmp_path / "cache", session_id)
        routes = {
            f"/session/{session_id}/advance-phase": (200, {
                "phase": "testing", "advanced": True, "token_spent": True,
            }),
        }
        with stub_daemon(routes) as (base, _reqs):
            r = _run_auto_approve_gate(tmp_path, base, session_id=session_id, curl_present=False)
        assert "gate approved" in r.stdout.lower()
        assert "testing" in r.stdout

    def test_rejection_with_unspent_token_tells_the_user_not_to_reapprove(self, tmp_path):
        session_id = "approve-reject-unspent"
        _seed_work_mode_planning(tmp_path / "cache", session_id)
        routes = {
            f"/session/{session_id}/advance-phase": (200, {
                "error": "plan.md is missing a ## Files section", "token_spent": False,
            }),
        }
        with stub_daemon(routes) as (base, _reqs):
            r = _run_auto_approve_gate(tmp_path, base, session_id=session_id, curl_present=False)
        assert "REJECTED" in r.stdout
        assert "plan.md is missing a ## Files section" in r.stdout
        assert "not consumed" in r.stdout.lower()

    def test_rejection_with_spent_token_tells_the_user_to_reapprove(self, tmp_path):
        session_id = "approve-reject-spent"
        _seed_work_mode_planning(tmp_path / "cache", session_id)
        routes = {
            f"/session/{session_id}/advance-phase": (200, {
                "error": "plan rejected on format grounds", "token_spent": True,
            }),
        }
        with stub_daemon(routes) as (base, _reqs):
            r = _run_auto_approve_gate(tmp_path, base, session_id=session_id, curl_present=False)
        assert "REJECTED" in r.stdout
        assert "approve again" in r.stdout.lower()

    def test_behavior_identical_whether_curl_is_present_or_absent(self, tmp_path):
        routes_factory = lambda sid: {
            f"/session/{sid}/advance-phase": (200, {
                "phase": "implementation", "advanced": True, "token_spent": True,
            }),
        }
        sid_a, sid_b = "approve-parity-a", "approve-parity-b"
        with stub_daemon(routes_factory(sid_a)) as (base_a, _r1):
            _seed_work_mode_planning(tmp_path / "cache-a", sid_a)
            r_no_curl = _run_auto_approve_gate(
                tmp_path / "no-curl", base_a, session_id=sid_a, curl_present=False,
            )
        with stub_daemon(routes_factory(sid_b)) as (base_b, _r2):
            _seed_work_mode_planning(tmp_path / "cache-b", sid_b)
            r_with_curl = _run_auto_approve_gate(
                tmp_path / "with-curl", base_b, session_id=sid_b, curl_present=True,
            )
        assert r_no_curl.returncode == r_with_curl.returncode == 0
        assert ("gate approved" in r_no_curl.stdout.lower()) == ("gate approved" in r_with_curl.stdout.lower())


# --------------------------------------------------------------------------- #
# Item 14: writ_server_health with curl absent
# --------------------------------------------------------------------------- #


class TestWritServerHealthCurlAbsent:
    def test_reports_healthy_for_a_live_daemon_with_curl_absent(self, tmp_path):
        with stub_daemon({"/health": (200, {"status": "ok"})}) as (base, _reqs):
            host, port = _host_port(base)
            fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS)
            script = (
                f"export WRIT_HOST={shlex.quote(host)} WRIT_PORT={shlex.quote(port)}; "
                f"source {shlex.quote(str(SERVER_LIB))}; writ_server_health"
            )
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={**os.environ, "PATH": str(fake_bin)}, timeout=15,
            )
        assert r.returncode == 0, "writ_server_health must report healthy without curl"

    def test_reports_unhealthy_when_the_daemon_is_actually_down_curl_absent(self, tmp_path):
        fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS)
        script = (
            "export WRIT_HOST=127.0.0.1 WRIT_PORT=1; "
            f"source {shlex.quote(str(SERVER_LIB))}; writ_server_health"
        )
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env={**os.environ, "PATH": str(fake_bin)}, timeout=15,
        )
        assert r.returncode != 0

    def test_writ_server_health_no_longer_a_bare_curl_call(self):
        text = SERVER_LIB.read_text()
        body = _extract_bash_function(text, "writ_server_health")
        assert body, "writ_server_health must still be defined in writ-server-lib.sh"
        assert "curl" not in body, (
            "writ_server_health must route through the curl-or-fallback helper, not raw curl "
            "(a probe that is always false is not daemon-down-equivalent)"
        )

    def test_ensure_server_recognizes_the_healthy_daemon_and_does_not_relaunch(self, tmp_path):
        with stub_daemon({"/health": (200, {"status": "ok"})}) as (base, _reqs):
            host, port = _host_port(base)
            fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS)
            script = (
                f"export WRIT_HOST={shlex.quote(host)} WRIT_PORT={shlex.quote(port)} "
                f"WRIT_DIR={shlex.quote(str(SKILL_ROOT))} WRIT_SERVE_CMD=/bin/false; "
                f"source {shlex.quote(str(SERVER_LIB))}; writ_ensure_server"
            )
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={**os.environ, "PATH": str(fake_bin)}, timeout=15,
            )
        assert r.returncode == 0
        assert "already running" in (r.stdout + r.stderr).lower(), (
            "a healthy daemon (curl absent) must not trigger a second `writ serve` launch"
        )


# --------------------------------------------------------------------------- #
# Item 15: rag_query / writ_action_push with curl absent
# --------------------------------------------------------------------------- #


class TestRagQueryAndActionPushCurlAbsent:
    def test_rag_query_returns_the_daemons_response_without_curl(self, tmp_path):
        with stub_daemon({"/query": (200, {"rules": [{"rule_id": "X-1"}]})}) as (base, _reqs):
            fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS)
            script = (
                f"export WRIT_URL={shlex.quote(base + '/query')}; "
                f"source {shlex.quote(str(COMMON_SH))}; "
                'WRIT_NO_CURL=1 rag_query "implement a widget handler" 2000 "[]"'
            )
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={**os.environ, "PATH": str(fake_bin)}, timeout=15,
            )
        assert r.returncode == 0
        assert json.loads(r.stdout)["rules"][0]["rule_id"] == "X-1"

    def test_writ_action_push_returns_stub_daemon_text_without_curl(self, tmp_path):
        routes = {
            "/methodology-companion": (200, {
                "rules": [{"rule_id": "M-1", "channel": "push", "statement": "do the thing"}],
                "total_tokens": 10,
            }),
        }
        with stub_daemon(routes) as (base, reqs):
            host, port = _host_port(base)
            fake_bin = _limited_path_bin(tmp_path, CORE_TOOLS)
            script = (
                f"export WRIT_HOST={shlex.quote(host)} WRIT_PORT={shlex.quote(port)}; "
                f"source {shlex.quote(str(COMMON_SH))}; "
                'WRIT_NO_CURL=1 writ_action_push "sess-push-1" "review-feedback"'
            )
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={**os.environ, "PATH": str(fake_bin)}, timeout=15,
            )
        assert r.returncode == 0
        posts = [x for x in reqs if x["method"] == "POST" and "methodology-companion" in x["path"]]
        assert len(posts) == 1, "writ_action_push must still reach the daemon without curl"

    def test_rag_query_no_longer_calls_curl_directly(self):
        body = _extract_bash_function(COMMON_SH.read_text(), "rag_query")
        assert body, "rag_query must still be defined in common.sh"
        assert "curl" not in body, "rag_query must route through writ_http_post, not raw curl"

    def test_writ_action_push_no_longer_calls_curl_directly(self):
        body = _extract_bash_function(COMMON_SH.read_text(), "writ_action_push")
        assert body, "writ_action_push must still be defined in common.sh"
        assert "curl" not in body, "writ_action_push must route through writ_http_post, not raw curl"

    def test_writ_http_get_and_post_are_defined_and_use_curl_first(self):
        text = COMMON_SH.read_text()
        get_body = _extract_bash_function(text, "writ_http_get")
        post_body = _extract_bash_function(text, "writ_http_post")
        assert get_body, "writ_http_get must be defined in common.sh"
        assert post_body, "writ_http_post must be defined in common.sh"
        assert "curl" in get_body, "writ_http_get must still try curl first"
        assert "curl" in post_body, "writ_http_post must still try curl first"
        assert "WRIT_NO_CURL" in get_body or "WRIT_NO_CURL" in post_body, (
            "the forcing seam WRIT_NO_CURL must be honored by the wrapper"
        )


# --------------------------------------------------------------------------- #
# Item 16 (+ the fail-cleanly half of item 17): bootstrap --preflight
# --------------------------------------------------------------------------- #


class TestBootstrapPreflight:
    def _run_preflight(self, script: Path, tmp_path: Path, tools: list[str]) -> subprocess.CompletedProcess:
        fake_bin = _limited_path_bin(tmp_path, tools)
        env = {"HOME": str(tmp_path), "PATH": str(fake_bin)}
        return subprocess.run(
            ["bash", str(script), "--preflight"], env=env, capture_output=True, text=True, timeout=15,
        )

    def test_bootstrap_preflight_exits_zero_with_only_python_docker_git(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP, tmp_path, ["bash", "python3", "docker", "git"])
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    def test_bootstrap_plugin_preflight_exits_zero_with_only_python_docker_git(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP_PLUGIN, tmp_path, ["bash", "python3", "docker", "git"])
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    def test_bootstrap_preflight_names_jq_and_curl_as_optional(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP, tmp_path, ["bash", "python3", "docker", "git"])
        out = (r.stdout + r.stderr).lower()
        assert "jq" in out and "curl" in out
        assert "optional" in out or "accelerat" in out

    def test_bootstrap_plugin_preflight_names_jq_and_curl_as_optional(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP_PLUGIN, tmp_path, ["bash", "python3", "docker", "git"])
        out = (r.stdout + r.stderr).lower()
        assert "jq" in out and "curl" in out
        assert "optional" in out or "accelerat" in out

    def test_bootstrap_preflight_fails_cleanly_without_python3(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP, tmp_path, ["bash", "docker", "git"])
        assert r.returncode != 0
        assert "python" in (r.stdout + r.stderr).lower()

    def test_bootstrap_preflight_fails_cleanly_without_docker(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP, tmp_path, ["bash", "python3", "git"])
        assert r.returncode != 0
        assert "docker" in (r.stdout + r.stderr).lower()

    def test_bootstrap_plugin_preflight_fails_cleanly_without_python3(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP_PLUGIN, tmp_path, ["bash", "docker", "git"])
        assert r.returncode != 0
        assert "python" in (r.stdout + r.stderr).lower()

    def test_bootstrap_plugin_preflight_fails_cleanly_without_docker(self, tmp_path):
        r = self._run_preflight(BOOTSTRAP_PLUGIN, tmp_path, ["bash", "python3", "git"])
        assert r.returncode != 0
        assert "docker" in (r.stdout + r.stderr).lower()

    def test_bootstrap_preflight_never_calls_docker_info(self, tmp_path):
        """--preflight must stop before the daemon-reachability probe: a `docker`
        binary that exists but whose `info` subcommand fails must not fail preflight."""
        fake_bin = _limited_path_bin(tmp_path, ["bash", "python3", "git"])
        stub = fake_bin / "docker"
        stub.write_text("#!/usr/bin/env bash\nexit 1\n")
        stub.chmod(0o755)
        env = {"HOME": str(tmp_path), "PATH": str(fake_bin)}
        r = subprocess.run(
            ["bash", str(BOOTSTRAP), "--preflight"], env=env, capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, "preflight must not invoke `docker info`"


# --------------------------------------------------------------------------- #
# Item 19: session-start-bootstrap.sh prints a copy-pasteable absolute command
# --------------------------------------------------------------------------- #


class TestSessionStartBootstrapMessage:
    def _run(self, tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
        plugin_root = tmp_path / "plugin-root"
        plugin_root.mkdir()
        env = {
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        }
        r = subprocess.run(
            ["bash", str(SESSION_START_BOOTSTRAP)], input="{}",
            capture_output=True, text=True, env=env, timeout=15,
        )
        return r, plugin_root

    def test_prints_the_absolute_command_with_no_unexpanded_variables(self, tmp_path):
        r, plugin_root = self._run(tmp_path)
        combined = r.stdout + r.stderr
        assert "${" not in combined, f"an unexpanded variable leaked into the message: {combined!r}"
        expected = f"bash {plugin_root}/scripts/bootstrap-plugin.sh"
        assert expected in combined

    def test_the_command_line_itself_carries_no_writ_prefix(self, tmp_path):
        r, _plugin_root = self._run(tmp_path)
        combined = r.stdout + r.stderr
        command_lines = [ln for ln in combined.splitlines() if "bootstrap-plugin.sh" in ln]
        assert command_lines, f"no bootstrap command line found in output: {combined!r}"
        assert any(not ln.strip().startswith("[Writ]") for ln in command_lines), (
            "the copy-pasteable command must be on its own unprefixed line"
        )


# --------------------------------------------------------------------------- #
# Item 20: repo guard
# --------------------------------------------------------------------------- #

# The plan's own daemon-down-equivalent allowlist (left unchanged by this cycle),
# keyed by a substring marker per site rather than a line number so minor
# reformatting cannot silently defeat the guard. Add to this deliberately -- the
# whole point is that an undocumented new raw curl call fails this suite.
DAEMON_DOWN_EQUIVALENT_RAW_CURL: dict[str, tuple[str, ...]] = {
    "hooks/scripts/writ-rag-inject.sh": ("$WRIT_HEALTH_URL", "7474", "/recall", "$COMPANION_URL"),
    "hooks/scripts/writ-subagent-start.sh": ("/health", "/query"),
    "hooks/scripts/writ-pre-write-dispatch.sh": ("/always-on",),
    "hooks/scripts/writ-bash-write-gate.sh": ("can-write",),
    "hooks/scripts/writ-cwd-changed.sh": ("git-hooks/auto-install",),
    "hooks/scripts/writ-memory-capture.sh": ("$MEMORY_URL",),
    "hooks/scripts/validate-rules.sh": ("$ANALYZE_URL",),
}

# Lines that LOOK like a curl invocation but are not one: text Writ PRINTS for the model
# to run. writ-quality-judge.sh emits the /quality-judgment POST as copy-paste guidance
# inside a heredoc; the hook itself makes no HTTP call at all, so there is nothing to
# degrade when curl is absent. Keyed by marker like the allowlist above, so a genuine new
# call site in the same file still has to be justified.
NON_INVOCATION_CURL_TEXT: dict[str, tuple[str, ...]] = {
    "hooks/scripts/writ-quality-judge.sh": ("quality-judgment",),
}

# Files that OWN the curl-or-fallback wrapper implementation. Their raw curl calls are
# the sanctioned fast path itself (verified precisely in the *_no_longer_bare_curl /
# *_use_curl_first tests above), not scanned generically here.
HTTP_WRAPPER_HOMES = {"bin/lib/common.sh", "scripts/lib/writ-server-lib.sh"}

# curl in COMMAND position: the word followed by a flag, a URL, or a variable holding one.
# A bare-word mention (`optional_tool curl`, or "requests, axios, curl" inside a rule's
# trigger prose in a seed script) is not a call site and must not be reported, or the
# guard would force the tree to stop naming the tool it deliberately made optional.
_CURL_INVOCATION = re.compile(r"""(?<![\w.-])curl(?=\s+(?:-|["']?(?:http|\$)))""")


def _curl_statement(lines: list[str], index: int) -> str:
    """The line at `index` plus its backslash continuations, joined.

    Raw curl calls in this tree wrap, so the URL frequently sits on a later physical
    line than the word `curl`. Matching markers against the joined statement is what
    lets the allowlist key on the endpoint (`/recall`, `$MEMORY_URL`) rather than on
    incidental formatting.
    """
    parts = [lines[index]]
    while parts[-1].rstrip().endswith("\\") and index + 1 < len(lines):
        index += 1
        parts.append(lines[index])
    # Drop the continuation backslashes so the joined text reads as one shell
    # statement (`curl \` + `-s URL` must join to `curl -s URL`, or the
    # invocation regex cannot see through the split).
    return " ".join(part.strip().rstrip("\\").rstrip() for part in parts)


def _raw_curl_sites(paths: list[Path]) -> list[tuple[Path, int, str]]:
    hits = []
    for path in paths:
        try:
            rel = path.relative_to(SKILL_ROOT).as_posix()
        except ValueError:
            # A planted file outside the tree (the detector's own self-test).
            rel = path.as_posix()
        if rel in HTTP_WRAPPER_HOMES:
            continue
        lines = path.read_text().splitlines()
        consumed = -1
        for lineno, line in enumerate(lines, start=1):
            if lineno <= consumed:
                continue  # part of a continuation already scanned as one statement
            if line.strip().startswith("#"):
                continue
            # Match against the JOINED statement, not the physical line: a call split
            # as `curl \` + flags-on-the-next-line must not evade the guard (review
            # finding), and the allowlist markers key on the endpoint which frequently
            # sits on a later physical line.
            statement = _curl_statement(lines, lineno - 1)
            joined_span = 1
            probe = lineno - 1
            while lines[probe].rstrip().endswith("\\") and probe + 1 < len(lines):
                probe += 1
                joined_span += 1
            consumed = lineno - 1 + joined_span
            if _CURL_INVOCATION.search(statement):
                hits.append((path, lineno, statement))
    return hits


class TestRepoGuardNoHardToolRequirements:
    def test_no_script_hard_requires_jq(self):
        offenders = [
            str(p) for p in _all_scripts()
            if re.search(r"require_tool\s+jq\b", p.read_text())
            or re.search(r"jq is required", p.read_text(), re.I)
        ]
        assert offenders == [], f"jq is still a hard requirement in: {offenders}"

    def test_no_script_hard_requires_curl(self):
        offenders = [
            str(p) for p in _all_scripts()
            if re.search(r"require_tool\s+curl\b", p.read_text())
            or re.search(r"curl is required", p.read_text(), re.I)
        ]
        assert offenders == [], f"curl is still a hard requirement in: {offenders}"

    def test_no_script_hard_requires_envsubst(self):
        offenders = [
            str(p) for p in _all_scripts()
            if re.search(r"require_tool\s+envsubst\b", p.read_text())
            or re.search(r"envsubst is required", p.read_text(), re.I)
        ]
        assert offenders == [], f"envsubst is still a hard requirement in: {offenders}"

    def test_no_script_invokes_envsubst_at_all(self):
        offenders = [str(p) for p in _all_scripts() if re.search(r"\benvsubst\b", p.read_text())]
        assert offenders == [], f"envsubst is still invoked in: {offenders}"


class TestRepoGuardRawCurlAllowlist:
    def test_every_raw_curl_site_is_documented(self):
        hits = _raw_curl_sites(_all_scripts())
        undocumented = []
        for path, lineno, line in hits:
            rel = path.relative_to(SKILL_ROOT).as_posix()
            markers = (DAEMON_DOWN_EQUIVALENT_RAW_CURL.get(rel, ())
                       + NON_INVOCATION_CURL_TEXT.get(rel, ()))
            if not any(marker in line for marker in markers):
                undocumented.append(f"{rel}:{lineno}: {line.strip()}")
        assert undocumented == [], (
            "raw curl call site(s) outside the documented daemon-down-equivalent allowlist "
            "(add to DAEMON_DOWN_EQUIVALENT_RAW_CURL deliberately if genuinely equivalent):\n"
            + "\n".join(undocumented)
        )

    def test_the_allowlist_is_not_vacuous(self):
        """Proves the scan has teeth: at least one currently-allowlisted site is found."""
        hits = _raw_curl_sites(_all_scripts())
        rels = {p.relative_to(SKILL_ROOT).as_posix() for p, _lineno, _line in hits}
        assert rels & set(DAEMON_DOWN_EQUIVALENT_RAW_CURL), (
            "no scanned file matched an allowlisted path; the allowlist or the scan regex has drifted"
        )

    def test_a_planted_undocumented_raw_curl_call_would_be_caught(self, tmp_path):
        """Proves the detector itself has teeth, independent of the real tree's state."""
        planted = tmp_path / "planted.sh"
        planted.write_text('#!/usr/bin/env bash\ncurl -s "http://example.invalid/not-documented"\n')
        hits = _raw_curl_sites([planted])
        assert hits, "the scan regex failed to find an obvious raw curl call"

    def test_a_backslash_continued_raw_curl_call_would_be_caught(self, tmp_path):
        # Review fix: `curl \` with its flags on the NEXT physical line must not evade
        # the guard -- the invocation regex runs against the joined statement.
        planted = tmp_path / "planted_continued.sh"
        planted.write_text(
            '#!/usr/bin/env bash\ncurl \\\n  -s "http://example.invalid/split-across-lines"\n'
        )
        hits = _raw_curl_sites([planted])
        assert hits, "a backslash-continued raw curl call evaded the scan"


class TestHttpWrapperOwnership:
    def test_advance_phase_post_no_longer_a_bare_curl_call(self):
        text = AUTO_APPROVE.read_text()
        assert "writ_http_post" in text, "the /advance-phase POST must go through writ_http_post"
        assert not re.search(r"curl\s+-s[^\n]*advance-phase", text), (
            "the /advance-phase POST must not be a raw curl call"
        )
