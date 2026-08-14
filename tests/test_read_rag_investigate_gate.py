"""Fix: writ-read-rag.sh must fire in investigate mode (audit/explore is read-heavy).

The PreToolUse Read RAG hook was gated to review/debug ONLY (writ-read-rag.sh:34);
`investigate` -- the unified audit/explore/research mode added after this hook -- fell
through, so a read-only agent (writ-explorer / the reviewers) running in investigate mode
received ZERO per-read RAG across its whole run. This reproduces the gate end-to-end with a
stub /query daemon and asserts investigate now injects, while conversation still skips and
review still works (regression guard).

Per TEST-REGRESSION-001: RED before adding investigate to the gate, GREEN after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.fixtures.net import free_port as _free_port

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-read-rag.sh")
)
HELPER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
)


class _QueryStub(BaseHTTPRequestHandler):
    """Answers POST /query with one high-score rule; 404s everything else so the hook's
    mode-get / should-skip / format calls fall back to the file-direct helper."""

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        if self.path == "/query":
            length = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(length)
            body = json.dumps({
                "rules": [{
                    "rule_id": "TEST-RULE-001",
                    "trigger": "when reading a service class",
                    "statement": "do the governed thing",
                    "violation": "",
                    "pass_example": "",
                    "enforcement": "",
                    "domain": "sec",
                    "severity": "high",
                    "score": 0.95,
                }],
                "meta": {"rule_ids": ["TEST-RULE-001"], "tokens": 120},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):  # noqa: N802
        self.send_error(404)


@pytest.fixture()
def query_stub():
    port = _free_port()
    srv = HTTPServer(("localhost", port), _QueryStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()


@pytest.fixture()
def src_file(tmp_path):
    f = tmp_path / "Service.php"
    f.write_text("<?php\nclass Service { public function run(): void {} }\n")
    return str(f)


def _seed_mode(cache_dir, sid, mode):
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    subprocess.run(
        [sys.executable, HELPER, "mode", "set", mode, sid],
        env=env, check=True, capture_output=True, text=True,
    )


def _run_hook(cache_dir, port, sid, file_path):
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_PORT"] = str(port)
    env["WRIT_HOST"] = "localhost"
    env["WRIT_FRICTION_LOG"] = os.path.join(str(cache_dir), "friction.log")
    envelope = {
        "agent_id": sid,
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
    }
    return subprocess.run(
        ["bash", HOOK], input=json.dumps(envelope),
        capture_output=True, text=True, env=env, timeout=20,
    )


class TestReadRagInvestigateGate:
    def test_investigate_mode_injects(self, tmp_path, query_stub, src_file):
        """Investigate (audit/explore) must now pass the gate and inject file-context rules."""
        _seed_mode(tmp_path, "inv-1", "investigate")
        r = _run_hook(tmp_path, query_stub, "inv-1", src_file)
        assert r.returncode == 0, r.stderr
        assert "file-context rules" in r.stdout

    def test_review_mode_still_injects(self, tmp_path, query_stub, src_file):
        """Regression guard: review mode keeps working."""
        _seed_mode(tmp_path, "rev-1", "review")
        r = _run_hook(tmp_path, query_stub, "rev-1", src_file)
        assert r.returncode == 0, r.stderr
        assert "file-context rules" in r.stdout

    def test_conversation_mode_skips(self, tmp_path, query_stub, src_file):
        """Conversation is not a governed read mode -> still gated off."""
        _seed_mode(tmp_path, "conv-1", "conversation")
        r = _run_hook(tmp_path, query_stub, "conv-1", src_file)
        assert r.returncode == 0, r.stderr
        assert "file-context rules" not in r.stdout
