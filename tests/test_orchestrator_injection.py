"""Hook-level proofs for the two gaps plan dfacff61 closes (plan.md ##
Analysis "What is broken"; capabilities.md items 4-14).

GAP 1: an orchestrator master's per-prompt turn never reaches /prompt-bundle
at all. writ-rag-inject.sh's `IS_ORCHESTRATOR` branch hand-rolls only the
methodology companion and exits before the always-on floor, and its citation
record, ever render, so a master receives zero mandatory rules per turn and a
planner worker citing an always-on rule gets reported as hallucinated.
TestOrchestratorFullFloor and TestPhaseAValidatorAcceptsTheFloor pin the fix.

GAP 2: an orchestrator's post-write hook exits before ever building a query,
on the strength of a comment ("orchestrator writes are metadata-only: no
RAG") that the write gate itself does not enforce (`grep -c is_orchestrator
writ/session/gates.py` returns 0). TestPostWriteQueryCount pins the fix
alongside the two negative controls plan.md names explicitly: a .md write
(already stopped by the extension map, a DIFFERENT mechanism) and a
non-orchestrator .py write (the unmodified baseline).

TestOrchestratorPromptGating covers the two remaining capabilities that are
NOT about the gap itself: a prompt too short to query must still not send a
bundle request, and a dead daemon must degrade the master exactly like every
other session rather than blocking the turn.

Every class below runs the REAL hook script as a subprocess against a real
listener (a stub, or the suite's own throwaway daemon), never the bash source
read as a string. A change that keeps the string "is_orchestrator" somewhere
in the file but removes its effect on control flow must still be caught
here, which a grep-for-a-string test cannot do.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from tests._daemon import _port, expected_cache_dir  # noqa: E402
from tests._stub_daemon import StubDaemon  # noqa: E402
from writ.shared.logging import read_streams, resolve_project  # noqa: E402

# autouse: pins cwd to a sandbox so a stray `mode set` cannot delete THIS
# repo's own gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401,E402

RAG_INJECT_HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-rag-inject.sh"
POSTTOOL_HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-posttool-rag.sh"

MIN_QUERY_LENGTH = 10  # hooks/scripts/writ-rag-inject.sh:28

# read_streams/resolve_project is the P1 per-project router, keyed off
# WRIT_LOG_ROOT (always set per-test by conftest's autouse _isolate_friction_log
# fixture). WRIT_FRICTION_LOG, a separate legacy single-file var, is also set
# per-test by that same fixture unless a module opts out via this marker, and
# some code on this path still prefers it when set, which would route events
# away from the per-project stream this file reads. Mirrors
# tests/test_methodology_companion_orchestrator.py, which reads the exact
# same events.
pytestmark = pytest.mark.no_friction_isolation


# --------------------------------------------------------------------------- #
# Daemon lifecycle: this module owns its own daemon rather than depending on
# one already answering on the test port. conftest deliberately does NOT
# start a daemon itself (daemon-dependent tests are meant to skip when none
# answers); bringing one up mid-suite would change the world for every later
# test. Mirrors tests/test_phase3b_approval_rewrap.py::_start_own_daemon /
# _stop_own_daemon and tests/test_methodology_companion_orchestrator.py's
# _ensure_aligned_daemon, aligned to the SAME expected_cache_dir() the rest of
# the suite already uses (so _read_cache in this process sees the same files
# the daemon-side cmd_update writes), rather than a private per-file cache dir.
# --------------------------------------------------------------------------- #


def _start_own_daemon() -> bool:
    ensure = SKILL_DIR / "scripts" / "ensure-server.sh"
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
    subprocess.run(["pkill", "-f", f"writ serve --port {_port()}"], capture_output=True)


@pytest.fixture(scope="module", autouse=True)
def corpus():
    """THIS FILE SELF-HEALS THE CORPUS; it does not assume a predecessor module
    left one behind.

    The suite's preflight rebuilds the isolated graph ONCE, at session start, so
    any module that wipes it and does not restore poisons every later module that
    needs a populated always-on channel. Measured 2026-09-01: after
    `pytest tests/test_ingest.py`, the isolated instance (bolt://localhost:7688,
    NOT production on 7687) holds 0 rules, 0 injection-eligible rules and 0
    ForbiddenResponse nodes, and this file's hook runs then emit only the
    orchestrator status line: no floor, and no server-unavailable line either,
    because the daemon is up and simply has nothing to return.

    `tests/_corpus.ensure_corpus()` is the established convention for this (seven
    other modules already use it, including
    tests/test_orchestrator_playbook_node.py, which is exactly why that file
    stayed green in full-suite order while this one did not). autouse + module
    scope so it covers EVERY class here, not only the ones that take a daemon.
    """
    from tests._corpus import ensure_corpus

    ensure_corpus()


@pytest.fixture(scope="module")
def own_daemon(corpus):
    # `corpus` is requested EXPLICITLY, not left to autouse ordering: the daemon
    # builds its retrieval indexes at startup, so a heal that ran after it came up
    # would leave the daemon warm on an empty graph.
    if not _start_own_daemon():
        pytest.skip("could not start a daemon on the test port (Neo4j down?)")
    yield
    _stop_own_daemon()


# --------------------------------------------------------------------------- #
# Session-cache seeding + cleanup, shared by every class below.
# --------------------------------------------------------------------------- #


def _seed_cache(session_id: str, cache_dir: str | None = None, **overrides) -> Path:
    cache_dir_path = Path(cache_dir) if cache_dir else Path(expected_cache_dir())
    cache_dir_path.mkdir(parents=True, exist_ok=True)
    base = {
        "session_id": session_id, "mode": "work", "is_orchestrator": True,
        "is_subagent": False, "current_phase": "implementation",
        "loaded_rule_ids": [], "loaded_rule_ids_by_phase": {},
        "remaining_budget": 8000, "context_percent": 0, "queries": 0,
        "pretool_queried_files": [], "files_written": [], "loaded_rules": [],
        "always_on_rule_ids": [], "always_on_tokens_used": 0,
        "pending_violations": [], "escalation": {"needed": False},
        "invalidation_history": {}, "failed_writes": [],
    }
    base.update(overrides)
    path = cache_dir_path / f"writ-session-{session_id}.json"
    path.write_text(json.dumps(base))
    return path


def _cleanup_session(session_id: str, cache_dir: str | None = None) -> None:
    cache_dir_path = Path(cache_dir) if cache_dir else Path(expected_cache_dir())
    for suffix in ("", ".lock"):
        try:
            (cache_dir_path / f"writ-session-{session_id}.json{suffix}").unlink()
        except OSError:
            pass


def _drain_event_buffer(session_id: str, cwd: Path) -> None:
    """Do what a real turn's Stop hook does before reading the router's streams.

    The shared /prompt-bundle path BUFFERS its friction rows
    (`writ_friction_buffer_append`, bin/lib/common.sh) rather than paying a measured
    35.3ms interpreter start per prompt to append them, and `writ_event_buffer_flush`
    releases them at turn end. Reading a stream straight after one hook run therefore
    measures the buffer, not the router. Run from `cwd` so the drain resolves the same
    project scope the hook did.
    """
    subprocess.run(
        [sys.executable, str(SKILL_DIR / "bin" / "lib" / "writ-flush-events.py"), session_id],
        capture_output=True, text=True, cwd=str(cwd), timeout=60, check=False,
    )


def _run_rag_inject(session_id: str, prompt: str, cwd: Path,
                     env: dict | None = None) -> subprocess.CompletedProcess:
    envelope = json.dumps({"session_id": session_id, "prompt": prompt})
    return subprocess.run(
        ["bash", str(RAG_INJECT_HOOK)], input=envelope, capture_output=True,
        text=True, cwd=str(cwd), timeout=20, env=env,
    )


# --------------------------------------------------------------------------- #
# Capabilities 4-7: the full always-on floor, capability 6: the companion
# survives, capability 7: the ranked channel logs a suppression rather than a
# zero-rule query.
# --------------------------------------------------------------------------- #
class TestOrchestratorFullFloor:
    """A work-mode, is_orchestrator=true hook run must reach the SAME
    /prompt-bundle path every other session reaches, with the ranked channel
    suppressed, not the pre-fix hand-rolled companion-only call.

    MUTATION (shared across this whole class): restoring the branch's own
    `exit 0` (the pre-fix shape, right after the hand-rolled companion call)
    turns every test here red, because the hook never reaches the shared
    bundle call at all.
    """

    PROMPT = ("Please continue implementing the retrieval budget fix: confirm "
              "the always-on floor renders and the ranked channel stays off.")

    def _run(self, tmp_path):
        sid = f"orch-floor-{uuid.uuid4().hex[:8]}"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        (project_root / ".git").mkdir()
        _seed_cache(sid, mode="work", is_orchestrator=True)
        result = _run_rag_inject(sid, self.PROMPT, project_root)
        return result, sid, project_root

    def test_always_active_rules_block_reaches_stdout(self, own_daemon, tmp_path):
        result, sid, _ = self._run(tmp_path)
        try:
            assert result.returncode == 0, result.stderr[:800]
            assert "=== ALWAYS-ACTIVE RULES ===" in result.stdout, result.stdout[:1500]
        finally:
            _cleanup_session(sid)

    def test_cache_records_always_on_rule_ids_and_tokens(self, own_daemon, tmp_path):
        result, sid, _ = self._run(tmp_path)
        try:
            assert result.returncode == 0, result.stderr[:800]
            from writ.session.cache import _read_cache
            cache = _read_cache(sid)
            tokens = cache.get("always_on_tokens_used")
            # bool is an int in Python: a stray True must not satisfy ">0" by
            # accident, so the type is checked as well as the value (HARD
            # CONSTRAINT 5). MUTATION: dropping --add-always-on-rules from the
            # paired update (query.py) leaves always_on_rule_ids empty even
            # though always_on_tokens_used would still be non-zero, so both
            # fields are asserted independently rather than one implying the
            # other.
            assert isinstance(tokens, int) and not isinstance(tokens, bool), cache
            assert tokens > 0, cache
            assert cache.get("always_on_rule_ids"), (
                f"always_on_rule_ids is empty; the master's floor was never recorded: {cache}"
            )
        finally:
            _cleanup_session(sid)

    def test_methodology_companion_block_still_present(self, own_daemon, tmp_path):
        """MUTATION (plan.md verification table, 'companion still emitted'):
        moving the new orchestrator exit ABOVE the methodology-block emit
        turns this red while leaving the always-on tests above green."""
        result, sid, _ = self._run(tmp_path)
        try:
            assert result.returncode == 0, result.stderr[:800]
            assert "[Writ: methodology companion]" in result.stdout, result.stdout[:1500]
        finally:
            _cleanup_session(sid)

    def test_suppressed_ranked_channel_is_logged_not_a_zero_rule_query(
        self, own_daemon, tmp_path
    ):
        """MUTATION (plan.md verification table, 'suppression row'): emitting
        a zero-rule rag_query for 'broad' instead of the suppression row
        turns this red."""
        result, sid, project_root = self._run(tmp_path)
        try:
            assert result.returncode == 0, result.stderr[:800]
            _drain_event_buffer(sid, project_root)
            events = read_streams(resolve_project(str(project_root)), ["metrics"])
            always_on = [e for e in events if e.get("event") == "always_on_inject"]
            suppressed = [e for e in events if e.get("event") == "rag_channel_suppressed"]
            zero_rule_broad = [
                e for e in events
                if e.get("event") == "rag_query" and e.get("query_source") == "broad"
            ]
            assert len(always_on) == 1, events
            assert len(suppressed) == 1, events
            assert suppressed[0].get("channel") == "broad", suppressed
            assert zero_rule_broad == [], (
                "the ranked channel's suppression was logged as a zero-rule "
                f"rag_query instead of a suppression: {zero_rule_broad}"
            )
        finally:
            _cleanup_session(sid)


# --------------------------------------------------------------------------- #
# Capability 9: the fifth consequence of Decision 1: filling the master's
# always_on_rule_ids fills the field _validate_phase_a reads.
# --------------------------------------------------------------------------- #
class TestPhaseAValidatorAcceptsTheFloor:
    """The gate must accept a citation of a rule the master's OWN hook run
    just injected, and must still catch a fabricated one in the same plan.

    Complements tests/test_always_on_citations.py's TestEndToEnd, which
    proves the SAME contract via a direct /prompt-bundle call. This class
    proves it via the actual HOOK an orchestrator master runs, which is the
    thing gap 1 breaks: today, always_on_rule_ids never gets populated for a
    master because the orchestrator branch never reaches /prompt-bundle.

    MUTATION: reverting the always-on delivery (this class's own setup
    failing to populate always_on_rule_ids) turns the first assertion red.
    The fabricated-id half is the anti-vacuity companion: it proves the
    validator was actually LIVE rather than abstaining because "nothing was
    captured" (`_validate_citations` returns no hallucinations when the
    available set is empty).
    """

    FABRICATED = "TOTALLY-MADE-UP-ORCH-999"

    @staticmethod
    def _plan(cited: str) -> str:
        return (
            "# Plan: something\n\n"
            "## Files\n\n"
            "- `writ/example.py` (modify) -- because the thing needs doing.\n\n"
            "## Analysis\n\n"
            "The what and the why, with contracts and integration points.\n\n"
            "## Rules Applied\n\n"
            f"- [{cited}] why it applies here.\n\n"
            "## Capabilities\n\n"
            "- [ ] the behavior is testable\n"
        )

    def test_accepts_a_real_always_on_id_and_rejects_a_fabricated_one(
        self, own_daemon, tmp_path
    ):
        sid = f"orch-cite-{uuid.uuid4().hex[:8]}"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        (project_root / ".git").mkdir()
        (project_root / "pyproject.toml").write_text("[project]\nname='x'\n")
        _seed_cache(sid, mode="work", is_orchestrator=True)
        try:
            result = _run_rag_inject(
                sid,
                "Continue implementing the budget fix and confirm the always-on floor.",
                project_root,
            )
            assert result.returncode == 0, result.stderr[:800]

            from writ.session.cache import _read_cache
            from writ.session.approval_workflow import _validate_phase_a

            always_on_ids = _read_cache(sid).get("always_on_rule_ids") or []
            assert always_on_ids, "the hook run recorded no always_on_rule_ids at all"

            (project_root / "plan.md").write_text(self._plan(always_on_ids[0]))
            assert _validate_phase_a(str(project_root), sid) is None

            (project_root / "plan.md").write_text(self._plan(self.FABRICATED))
            err = _validate_phase_a(str(project_root), sid)
            assert err is not None and "hallucinated" in err and self.FABRICATED in err, err
        finally:
            _cleanup_session(sid)


# --------------------------------------------------------------------------- #
# A minimal loopback recorder for POST /prompt-bundle ONLY, used to prove a
# NEGATIVE: a short orchestrator prompt never sends a bundle request at all.
# Deliberately NOT added to tests/_stub_daemon.py, which the plan scopes to
# the post-write /query route: this needs a listener that WOULD answer if
# reached, which is what distinguishes "never sent" from "sent and refused"
# (a closed port, used below for the no-daemon capability, cannot make that
# distinction).
# --------------------------------------------------------------------------- #
class _BundleRequestRecorder:
    def __init__(self, response: dict) -> None:
        self._response = json.dumps(response).encode("utf-8")
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_BundleRequestRecorder":
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_a):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                recorder.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(recorder._response)))
                self.end_headers()
                self.wfile.write(recorder._response)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def saw(self, path: str) -> bool:
        return path in self.requests


_BUNDLE_STUB_RESPONSE = {
    "always_on_block": "", "rules_text": "", "methodology_block": "",
    "nudge": "", "error": False,
    "broad_meta": {"suppressed": True}, "ao_meta": None, "method_meta": None,
}


# --------------------------------------------------------------------------- #
# Capabilities 13-14: the bundle request itself is gated exactly like the
# non-orchestrator path (short prompts never send it), and a dead daemon
# degrades a master the same way it degrades everyone else.
#
# No live daemon needed for this class: it targets a dedicated loopback
# recorder or a closed port, never the real /prompt-bundle handler.
# --------------------------------------------------------------------------- #
class TestOrchestratorPromptGating:
    LONG_PROMPT = "Please continue implementing the retrieval budget fix end to end."
    SHORT_PROMPT = "hi"  # 2 chars, well under MIN_QUERY_LENGTH (10)

    def test_short_prompt_sends_no_bundle_request(self, tmp_path):
        """MUTATION: sending the bundle request unconditionally from the
        orchestrator branch (i.e. never gating it on prompt length the way
        the non-orchestrator path's step 2 does) turns this red: the
        recorder would show a hit for a 2-character prompt."""
        assert len(self.SHORT_PROMPT) < MIN_QUERY_LENGTH
        sid = f"orch-short-{uuid.uuid4().hex[:8]}"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _seed_cache(sid, mode="work", is_orchestrator=True)
        try:
            with _BundleRequestRecorder(_BUNDLE_STUB_RESPONSE) as stub:
                env = {**os.environ, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port)}
                result = _run_rag_inject(sid, self.SHORT_PROMPT, project_root, env=env)
                saw_bundle = stub.saw("/prompt-bundle")
            assert result.returncode == 0, result.stderr[:800]
            assert "[Writ: mode=" in result.stdout, result.stdout[:500]
            assert not saw_bundle, (
                "a prompt shorter than MIN_QUERY_LENGTH sent a bundle request anyway"
            )
        finally:
            _cleanup_session(sid)

    def test_long_prompt_positive_control_does_send_a_bundle_request(self, tmp_path):
        """Anti-vacuity companion to the test above: the SAME recorder, given
        a normal-length prompt, must show a hit. Without this, the
        short-prompt test could read green against a recorder that never
        receives ANY connection (a harness bug), not against a real length
        gate. Also the capability that is actually RED pre-fix: today the
        orchestrator branch never calls /prompt-bundle regardless of length."""
        sid = f"orch-long-{uuid.uuid4().hex[:8]}"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _seed_cache(sid, mode="work", is_orchestrator=True)
        try:
            with _BundleRequestRecorder(_BUNDLE_STUB_RESPONSE) as stub:
                env = {**os.environ, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub.port)}
                result = _run_rag_inject(sid, self.LONG_PROMPT, project_root, env=env)
                saw_bundle = stub.saw("/prompt-bundle")
            assert result.returncode == 0, result.stderr[:800]
            assert saw_bundle, (
                f"the orchestrator branch never reached /prompt-bundle at all "
                f"(recorded requests: {stub.requests})"
            )
        finally:
            _cleanup_session(sid)

    def test_no_daemon_listening_exits_zero_with_server_unavailable_line(self, tmp_path):
        """MUTATION: this line does not exist anywhere in the PRE-FIX
        orchestrator branch at all (it silently drops the companion and exits
        with only the status line), so this is red until the branch actually
        flows into the shared /prompt-bundle section that already prints it
        (writ-rag-inject.sh, "failed: empty /prompt-bundle response")."""
        sid = f"orch-down-{uuid.uuid4().hex[:8]}"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        _seed_cache(sid, mode="work", is_orchestrator=True)
        # The suite's own convention for "deliberately unreachable" (see e.g.
        # tests/test_cwd_changed.py, tests/test_enforce_violations.py):
        # nothing ever listens on this port in the test environment.
        env = {**os.environ, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": "19999"}
        try:
            result = _run_rag_inject(sid, self.LONG_PROMPT, project_root, env=env)
            assert result.returncode == 0, result.stderr[:800]
            assert "[Writ: server unavailable, proceeding without rules]" in result.stdout, (
                result.stdout[:800]
            )
        finally:
            _cleanup_session(sid)


# --------------------------------------------------------------------------- #
# Capabilities 10-12: the post-write query gate.
#
# No live daemon: a StubDaemon answering /query (tests/_stub_daemon.py) is
# enough. Every other call the hook makes (session read, should-skip, format,
# update) 404s against the stub and falls back to the LOCAL subprocess CLI,
# which reads the cache file this module seeds directly: exactly the
# resilience the daemon-down design already relies on elsewhere.
# --------------------------------------------------------------------------- #
_CANNED_RULE = {
    "rule_id": "TEST-CANNED-001", "severity": "high", "authority": "human",
    "domain": "test", "score": 0.9,
    "trigger": "when writing this test", "statement": "do the canned thing",
}
_QUERY_RESPONSE = {"mode": "standard", "rules": [_CANNED_RULE]}


def _seed_local_cache(cache_dir: Path, session_id: str, **overrides) -> None:
    base = {
        "session_id": session_id, "mode": "work", "is_orchestrator": False,
        "is_subagent": False, "current_phase": "implementation",
        "loaded_rule_ids": [], "loaded_rule_ids_by_phase": {},
        "remaining_budget": 8000, "context_percent": 0, "queries": 0,
        "pretool_queried_files": [], "files_written": [], "loaded_rules": [],
        "always_on_rule_ids": [], "always_on_tokens_used": 0,
    }
    base.update(overrides)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(base))


def _run_posttool(session_id: str, file_path: Path, content: str, cache_dir: Path,
                   stub_port: int) -> subprocess.CompletedProcess:
    envelope = json.dumps({
        "session_id": session_id, "tool_name": "Write",
        "hook_event_name": "PostToolUse",
        "tool_input": {"file_path": str(file_path), "content": content},
    })
    env = {
        **os.environ, "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(stub_port),
    }
    return subprocess.run(
        ["bash", str(POSTTOOL_HOOK)], input=envelope, capture_output=True,
        text=True, cwd=str(cache_dir), timeout=20, env=env,
    )


class TestPostWriteQueryCount:
    """writ-posttool-rag.sh's is_orchestrator early exit (the comment
    'orchestrator writes are metadata-only: no RAG') stops an orchestrator's
    .py write from ever building a query, which is a claim the write gate
    itself does not make (`grep -c is_orchestrator writ/session/gates.py`
    returns 0; the metadata-only restriction in gates.py belongs to the
    no-mode state, a different condition).

    MUTATION (plan.md verification table, 'one query on a .py write'):
    restoring the early exit turns test_orchestrator_py_write_... red.
    """

    PY_CONTENT = "class Example:\n    def run_query(self):\n        return True\n"

    def test_orchestrator_py_write_drives_exactly_one_query(self, tmp_path):
        sid = f"post-orch-py-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        _seed_local_cache(cache_dir, sid, is_orchestrator=True)
        target = tmp_path / "module.py"
        with StubDaemon(query=_QUERY_RESPONSE) as stub:
            result = _run_posttool(sid, target, self.PY_CONTENT, cache_dir, stub.port)
        assert result.returncode == 0, result.stderr[:800]
        matches = stub.matching("POST", "/query")
        assert len(matches) == 1, (
            f"expected exactly one POST /query, saw: {stub.describe()}"
        )
        # The canned rule must actually reach stdout as an additionalContext
        # envelope, not just be fetched and discarded (capabilities.md:
        # "the canned rule reaches stdout as an additionalContext envelope").
        ac_lines = [ln for ln in result.stdout.splitlines() if "additionalContext" in ln]
        assert ac_lines, f"no additionalContext envelope on stdout: {result.stdout!r}"
        envelope = json.loads(ac_lines[-1])
        assert envelope["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert "TEST-CANNED-001" in envelope["hookSpecificOutput"]["additionalContext"]

    def test_orchestrator_md_write_drives_zero_queries(self, tmp_path):
        """Already true today by a DIFFERENT mechanism (the extension map at
        writ-posttool-rag.sh:87-98 has no .md entry), and must stay true once
        the is_orchestrator exit above is deleted: this proves the two
        mechanisms are independent, not that the fix landed.

        MUTATION (plan.md verification table, 'zero queries on a .md write'):
        removing the EXTENSION-MAP guard turns this red, not restoring the
        is_orchestrator exit this class otherwise targets.
        """
        sid = f"post-orch-md-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        _seed_local_cache(cache_dir, sid, is_orchestrator=True)
        target = tmp_path / "plan.md"
        with StubDaemon(query=_QUERY_RESPONSE) as stub:
            result = _run_posttool(sid, target, "# Plan\n\nsome text\n", cache_dir, stub.port)
        assert result.returncode == 0, result.stderr[:800]
        assert stub.matching("POST", "/query") == [], stub.describe()

    def test_non_orchestrator_py_write_drives_exactly_one_query_unchanged(self, tmp_path):
        """Anti-vacuity control named explicitly in plan.md: a
        non-orchestrator session writing the same .py file is the baseline
        this change must never touch. Without this, a future change that
        disabled the write-path query for EVERYONE would only be caught by
        the orchestrator test above looking green for the wrong reason (a
        broken StubDaemon route, say) rather than by an actual regression
        signal."""
        sid = f"post-nonorch-py-{uuid.uuid4().hex[:8]}"
        cache_dir = tmp_path / "cache"
        _seed_local_cache(cache_dir, sid, is_orchestrator=False)
        target = tmp_path / "module.py"
        with StubDaemon(query=_QUERY_RESPONSE) as stub:
            result = _run_posttool(sid, target, self.PY_CONTENT, cache_dir, stub.port)
        assert result.returncode == 0, result.stderr[:800]
        assert len(stub.matching("POST", "/query")) == 1, stub.describe()
