"""Tests for the Wave-3 dedup: hooks/scripts/writ-read-rag.sh:164-187 and
hooks/scripts/writ-posttool-rag.sh:197-220 currently inline byte-identical
"build the /query request + POST it" bodies (only the budget variable name
and REQUEST/RESPONSE casing differ). The planned fix moves this into a single
`rag_query()` bash function in bin/lib/common.sh (already sourced by both
hooks); the hooks then call `rag_query "$QUERY" "$PRETOOL_BUDGET"/"$POSTTOOL_BUDGET"
"$LOADED_RULE_IDS"` instead of inlining the python3 -c + curl pair.

RED now:
  - TestStaticByteIdentity        -- common.sh has no rag_query() function yet.
  - TestBehavioralParityVsHeadInline -- same (helper undefined -> empty stdout,
    mismatches the frozen HEAD-inline reference).
  - TestSetEDoesNotAbort          -- same (undefined command aborts a set -e
    script before it reaches its own `exit 7`).
  - TestHooksAdoptRagQuery        -- neither hook has been repointed yet.

GREEN always (pre-existing regression guards this cycle must not disturb):
  - TestUntouchedTailInvariants.

Run: .venv/bin/python -m pytest tests/test_rag_query_helper.py -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.net import free_port as _free_port

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = REPO_ROOT / "bin" / "lib" / "common.sh"
HOOKS_DIR = REPO_ROOT / "hooks" / "scripts"
READ_RAG_HOOK = HOOKS_DIR / "writ-read-rag.sh"
POSTTOOL_RAG_HOOK = HOOKS_DIR / "writ-posttool-rag.sh"


# ── Frozen HEAD reference (do NOT read live from the hooks -- they change
# after the implementation lands; hardcode the byte-for-byte HEAD behavior,
# same discipline as tests/test_phase_scoped_rules.py's HEAD_BODY). Mirrors
# hooks/scripts/writ-read-rag.sh:165-183 exactly, with QUERY/BUDGET/EXCLUDE
# taken from positional args ($1/$2/$3) instead of $QUERY/$PRETOOL_BUDGET/
# $LOADED_RULE_IDS, and WRIT_URL read from the environment (as both hooks do).
HEAD_BUILD_POST = r'''
QUERY="$1"
BUDGET="$2"
EXCLUDE="$3"
REQUEST=$(python3 -c "
import json, sys
print(json.dumps({
    'query': sys.argv[1],
    'budget_tokens': int(sys.argv[2]),
    'exclude_rule_ids': json.loads(sys.argv[3]),
    'top_k': 3,
}))
" "$QUERY" "$BUDGET" "$EXCLUDE" 2>/dev/null)

if [ -z "$REQUEST" ]; then
    exit 0
fi

RESPONSE=$(curl -s --connect-timeout 0.3 --max-time 1 \
    -X POST "$WRIT_URL" \
    -H "Content-Type: application/json" \
    -d "$REQUEST" 2>/dev/null) || true

printf '%s' "$RESPONSE"
'''


def _extract_bash_function(source: str, name: str) -> str | None:
    """Extract `name() { ... }` (including the braces) from bash source text
    via brace counting (handles the nested `{...}` of the embedded python
    dict literal). Returns None if the function is not defined."""
    marker = f"{name}() {{"
    idx = source.find(marker)
    if idx == -1:
        return None
    brace_idx = idx + len(marker) - 1  # index of the opening "{"
    depth = 0
    i = brace_idx
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[idx : i + 1]
        i += 1
    return None


def _closed_port() -> int:
    """A port that is guaranteed free-but-unlistened-on right now, so a
    connection to it is refused quickly (models "server is down")."""
    return _free_port()


def _make_stub_handler_class():
    """Builds a fresh BaseHTTPRequestHandler subclass with its own isolated
    `received` list (TEST-ISOLATE-001 -- no cross-test shared mutable state).
    Answers POST /query with a fixed rule body; records the parsed request."""
    received: list = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence stderr noise
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                parsed = {"_raw": raw.decode("utf-8", errors="replace")}
            received.append(parsed)
            body = json.dumps(
                {"rules": [{"rule_id": "X-1", "score": 0.9, "statement": "s"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self.send_error(404)

    return Handler, received


@pytest.fixture()
def stub_server():
    """An isolated local stub /query HTTP server (thread, ephemeral port),
    modeled on tests/test_read_rag_investigate_gate.py's _QueryStub. Skips
    gracefully if the environment cannot bind a local socket."""
    handler_cls, received = _make_stub_handler_class()
    port = _free_port()
    try:
        srv = HTTPServer(("127.0.0.1", port), handler_cls)
    except OSError:
        pytest.skip("could not bind a local stub /query server")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/query", requests=received)
    finally:
        srv.shutdown()
        srv.server_close()


def _run_rag_query_helper(query: str, budget: str, exclude: str, writ_url: str):
    """Runs `source common.sh; WRIT_URL=<writ_url>; RESPONSE=$(rag_query ...);
    printf '%s' "$RESPONSE"`. The command-substitution + `printf '%s'` mirrors
    exactly how both hooks consume rag_query (`RESPONSE=$(rag_query ...)`), so
    trailing-newline stripping matches the real call site and the parity vs
    _run_head_build_post is apples-to-apples (not raw-stdout vs stripped).
    Args are passed via argv (never spliced into the script text) so malformed
    exclude strings (brackets/quotes) can never break the script."""
    script = 'source "$1"; WRIT_URL="$2"; RESPONSE=$(rag_query "$3" "$4" "$5"); printf "%s" "$RESPONSE"'
    return subprocess.run(
        ["bash", "-c", script, "bash", str(COMMON_SH), writ_url, query, budget, exclude],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _run_head_build_post(query: str, budget: str, exclude: str, writ_url: str):
    """Runs the frozen HEAD_BUILD_POST reference with the same three inputs,
    WRIT_URL supplied via env exactly as the hooks set it."""
    env = os.environ.copy()
    env["WRIT_URL"] = writ_url
    return subprocess.run(
        ["bash", "-c", HEAD_BUILD_POST, "bash", query, budget, exclude],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


# -- 1. Source-level proof: the moved body is byte-identical to HEAD --------


class TestStaticByteIdentity:
    """No subprocess execution -- pure text extraction from bin/lib/common.sh.
    RED until rag_query() exists there with the exact HEAD request-build +
    curl invocation moved in verbatim."""

    def _rag_query_body(self) -> str:
        source = COMMON_SH.read_text()
        body = _extract_bash_function(source, "rag_query")
        if body is None:
            pytest.fail(
                "bin/lib/common.sh has no rag_query() function yet "
                "(RED: the Wave-3 rag_query dedup helper has not been added)."
            )
        return body

    def test_rag_query_function_exists_in_common_sh(self):
        assert self._rag_query_body() is not None

    def test_request_build_matches_head_inline_json_fields(self):
        body = self._rag_query_body()
        assert "'budget_tokens': int(sys.argv[2])," in body
        assert "'exclude_rule_ids': json.loads(sys.argv[3])," in body
        assert "'top_k': 3," in body

    def test_post_invocation_keeps_the_head_inline_parameters(self):
        """The POST moved from a raw curl to the curl-first writ_http_post wrapper (curl
        is an optional accelerator now, so a curl-less machine must still retrieve rules).
        The parameters it carried are unchanged: same URL, same JSON body variable, same
        0.3s connect / 1s total budget, same fail-open redirect."""
        body = self._rag_query_body()
        assert not re.search(r"curl\s+-", body), (
            "rag_query must POST through writ_http_post, not raw curl"
        )
        assert "WRIT_HTTP_CONNECT_TIMEOUT=0.3" in body and "WRIT_HTTP_TIMEOUT=1" in body, (
            "the connect/total budgets the inline curl carried must be preserved"
        )
        assert 'writ_http_post "$WRIT_URL"' in body
        # Lowercase local `$request`, unlike the hooks' `$REQUEST`.
        assert '"$request"' in body
        assert "2>/dev/null || true" in body

    def test_empty_request_guard_present(self):
        body = self._rag_query_body()
        assert '[ -z "$request" ] && return 0' in body


# -- 2. Behavioral parity vs. the frozen HEAD inline snippet -----------------


class TestBehavioralParityVsHeadInline:
    """Stub-server-backed differential test: rag_query() must produce
    byte-identical stdout to the HEAD inline build+POST for every case."""

    @pytest.fixture(autouse=True)
    def _require_curl(self):
        if shutil.which("curl") is None:
            pytest.skip("curl is not available in this environment")

    def test_normal_request_returns_identical_stub_body(self, stub_server):
        helper_out = _run_rag_query_helper("test", "1500", "[]", stub_server.url)
        head_out = _run_head_build_post("test", "1500", "[]", stub_server.url)
        assert json.loads(head_out.stdout) == {
            "rules": [{"rule_id": "X-1", "score": 0.9, "statement": "s"}]
        }, "sanity: the frozen HEAD reference must actually reach the stub"
        assert helper_out.stdout == head_out.stdout

    def test_exclude_rule_ids_passthrough_and_identical_stdout(self, stub_server):
        helper_out = _run_rag_query_helper("test", "1500", '["A","B"]', stub_server.url)
        assert len(stub_server.requests) == 1, (
            "rag_query did not reach the stub server with the expected request"
        )
        received = stub_server.requests[-1]
        assert received.get("exclude_rule_ids") == ["A", "B"]
        assert received.get("budget_tokens") == 1500
        assert received.get("top_k") == 3

        head_out = _run_head_build_post("test", "1500", '["A","B"]', stub_server.url)
        assert helper_out.stdout == head_out.stdout

    def test_malformed_exclude_yields_empty_request_and_no_server_hit(self, stub_server):
        before = len(stub_server.requests)
        helper_out = _run_rag_query_helper("test", "1500", "not json{", stub_server.url)
        assert helper_out.stdout == "", (
            "rag_query must return empty stdout when the request body fails to build"
        )
        assert len(stub_server.requests) == before, (
            "rag_query must NOT POST to the server when json.loads(exclude) fails"
        )

        head_out = _run_head_build_post("test", "1500", "not json{", stub_server.url)
        assert head_out.stdout == ""
        assert helper_out.stdout == head_out.stdout

    def test_server_down_both_return_empty_stdout(self):
        closed_url = f"http://127.0.0.1:{_closed_port()}/query"
        helper_out = _run_rag_query_helper("test", "1500", "[]", closed_url)
        head_out = _run_head_build_post("test", "1500", "[]", closed_url)
        assert helper_out.stdout == ""
        assert head_out.stdout == ""
        assert helper_out.stdout == head_out.stdout


# -- 3. set -e safety ---------------------------------------------------------


class TestSetEDoesNotAbort:
    """rag_query must never abort a caller running under `set -e`, even when
    its own request-build fails (malformed exclude). The two hooks both run
    under `set -euo pipefail` at the top of the file."""

    def test_rag_query_does_not_abort_set_e_caller(self):
        script = (
            'set -euo pipefail; source "$1"; '
            'WRIT_URL="http://127.0.0.1:1/query"; '
            'RESPONSE=$(rag_query "q" "1500" "not json{"); '
            'echo "AFTER=[$RESPONSE]"; '
            "exit 7"
        )
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(COMMON_SH)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 7, (
            f"script did not reach its own `exit 7` (rag_query aborted the "
            f"set -e caller); returncode={result.returncode!r} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "AFTER=[]" in result.stdout


# -- 4. Hook adoption source guard --------------------------------------------


class TestHooksAdoptRagQuery:
    """Reads hook source text directly. RED until each hook is repointed at
    the new rag_query() helper and its own inline copy is removed."""

    def test_read_rag_invokes_rag_query_helper(self):
        content = READ_RAG_HOOK.read_text()
        assert 'rag_query "$QUERY" "$PRETOOL_BUDGET" "$LOADED_RULE_IDS"' in content

    def test_posttool_rag_invokes_rag_query_helper(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert 'rag_query "$QUERY" "$POSTTOOL_BUDGET" "$LOADED_RULE_IDS"' in content

    def test_read_rag_no_longer_inlines_request_build(self):
        content = READ_RAG_HOOK.read_text()
        assert content.count("'budget_tokens': int(sys.argv[2])") == 0, (
            "writ-read-rag.sh still inlines the request-build python body; "
            "it must call rag_query() instead."
        )

    def test_posttool_rag_no_longer_inlines_request_build(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert content.count("'budget_tokens': int(sys.argv[2])") == 0, (
            "writ-posttool-rag.sh still inlines the request-build python body; "
            "it must call rag_query() instead."
        )

    def test_read_rag_no_longer_inlines_curl_d_request(self):
        content = READ_RAG_HOOK.read_text()
        assert content.count('-d "$REQUEST"') == 0, (
            "writ-read-rag.sh still inlines its own curl POST; "
            "it must call rag_query() instead."
        )

    def test_posttool_rag_no_longer_inlines_curl_d_request(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert content.count('-d "$REQUEST"') == 0, (
            "writ-posttool-rag.sh still inlines its own curl POST; "
            "it must call rag_query() instead."
        )

    def test_read_rag_preserves_empty_response_bail(self):
        content = READ_RAG_HOOK.read_text()
        assert 'if [ -z "$RESPONSE" ]; then' in content

    def test_posttool_rag_preserves_empty_response_bail(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert 'if [ -z "$RESPONSE" ]; then' in content


# -- 5. Pre-existing invariants this cycle must not disturb ------------------


class TestUntouchedTailInvariants:
    """Guards the three named regression risks. Must PASS at HEAD (before any
    change) and keep passing after the rag_query adoption -- these are NOT
    the RED target of this cycle."""

    def test_read_rag_hook_log_sink_redirect_count(self):
        content = READ_RAG_HOOK.read_text()
        assert content.count('2>>"$WRIT_HOOK_LOG_SINK"') == 1

    def test_posttool_rag_hook_log_sink_redirect_count(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert content.count('2>>"$WRIT_HOOK_LOG_SINK"') == 2

    def test_read_rag_retains_injected_context_strings(self):
        content = READ_RAG_HOOK.read_text()
        assert "file-context rules for" in content
        assert "PreToolUse" in content
        assert "file-read" in content

    def test_posttool_rag_retains_injected_context_strings(self):
        content = POSTTOOL_RAG_HOOK.read_text()
        assert "post-write rules for" in content
        assert "PostToolUse" in content
        assert "file-write-post" in content

    def test_posttool_rag_keeps_hook_timer_end_read_rag_does_not(self):
        posttool_content = POSTTOOL_RAG_HOOK.read_text()
        read_content = READ_RAG_HOOK.read_text()
        assert "hook_timer_end" in posttool_content
        assert "hook_timer_end" not in read_content
