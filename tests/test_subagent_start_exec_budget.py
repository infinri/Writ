"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4a and the count side of 4b:
writ-subagent-start.sh must stop asking the daemon for a mode it just seeded, and must
stop paying two python3 starts to read the parent's cache once for PHASE_INFO and again
for PLAN_DIR_INFO.

4a(i): `CURRENT_MODE=$(_writ_session "mode get" "$AGENT_ID")` (line 234) costs one curl
round trip (and a jq parse of the answer) when the daemon answers, even though the seed
heredoc just above it (172-207) already read the child's own cache to seed it. The fix
prints the child's mode as SEED_STATUS's second line instead of asking the daemon again.

4a(ii): PHASE_INFO (391-407) and PLAN_DIR_INFO (421-436) are two separate
`python3 - <<'PY'` heredocs, each importing `writ.session.cache._read_cache` and
reading the SAME parent cache file. The fix merges them into one heredoc.

Per ENF-PROC-TDD-001 / the plan's own instruction (## Analysis, "Counts"): the
baselines below are measured on HEAD through a PATH shim (the pattern of
tests/test_read_records_examined.py::TestPython3ExecCountPerRead) and pinned as
literal constants -- the plan states only the deltas ("baseline minus N"), not the
baseline itself, and reproducing the derivation from prose would silently drift from
what the shim actually counts (log_friction_event, manual_test_grant.py and the
always-unconditional `_writ_session update` link call are easy to undercount by hand).

Measured on HEAD, 2026-09-25, against this harness (real hook subprocess, WRIT_CACHE_DIR
under tmp_path, a stub daemon on a free port -- never the operator's real daemon on
8765): healthy stub with rules python3=15 jq=4 curl=4; daemon down (closed port)
python3=9 jq=3 curl=2.

Per TEST-REGRESSION-001: the exec-budget and zero-mode-GET capabilities pin the
counts batch 3 reached (the hook before it paid an extra exec and an extra GET); the
seed-failure fallback and the merged-heredoc rendering capabilities are regression
guards (the old two-heredoc rendering was byte-identical to the merged one; the
oversized-role fault falls back to `mode get` because seeding never starts).
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fixtures.net import free_port as _free_port
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401 -- autouse, protects real gate state

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-subagent-start.sh"

_STUB_RULE_IDS = ["TEST-BUDGET-001", "TEST-BUDGET-002"]

# The stub's WRONG answer for GET /session/<agent>/mode, deliberately different from
# both the parent's mode ("work") and the child's own pre-seeded mode ("review"): if the
# hook is still asking the daemon for the mode instead of reading the child cache it just
# wrote, this value -- not "review" -- ends up on the subagent_start friction row.
_STUB_WRONG_MODE = "conversation"

PARENT = "budget-parent-1"
AGENT = "budget-agent-1"


class _BudgetStubHandler(BaseHTTPRequestHandler):
    """A healthy daemon for the injection-path measurement, and the one recorder every
    capability in this module needs: how many times GET /session/<agent>/mode was hit."""

    mode_get_hits: list[str] = []

    def log_message(self, *args):  # noqa: N802 (http.server API) -- silence stderr noise
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._ok(b'{"status": "ok"}')
        elif self.path.startswith("/session/") and self.path.endswith("/mode"):
            _BudgetStubHandler.mode_get_hits.append(self.path)
            self._ok(json.dumps({"mode": _STUB_WRONG_MODE}).encode())
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/query":
            body = json.dumps({
                "rules": [
                    {"rule_id": rid, "severity": "high", "authority": "human",
                     "domain": "Testing", "score": 0.9, "trigger": "t", "statement": "s"}
                    for rid in _STUB_RULE_IDS
                ],
                "mode": "standard", "total_candidates": len(_STUB_RULE_IDS), "latency_ms": 1,
            }).encode()
            self._ok(body)
        else:
            self.send_error(404)

    def _ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def healthy_stub():
    _BudgetStubHandler.mode_get_hits = []
    port = _free_port()
    srv = HTTPServer(("localhost", port), _BudgetStubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()


_CALL_MARKER = "@@WRIT_EXEC_BUDGET_CALL@@"


def _shim_dir(tmp_path):
    """A PATH shim for python3/jq/curl. Each wrapper appends ONE marker line (for an
    exec count immune to multi-line argv -- several of this hook's own python3 calls
    are multi-line heredoc scripts, and a plain `printf "%s\\n" "$*"` -- the naive
    extension of tests/test_read_records_examined.py::TestPython3ExecCountPerRead's
    single-line-argv `echo exec` marker -- would silently inflate the count by one
    line per embedded newline) followed by the call's full argv (for the substring
    filters some capabilities in this file need, e.g. "was /mode ever requested"),
    then execs the real binary."""
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
    """The number of actual invocations recorded by `_shim_dir`'s wrapper -- the
    count of marker lines, NOT the file's total line count (a multi-line python3 -c
    script inflates the latter)."""
    return sum(1 for ln in counter.read_text().splitlines() if ln == _CALL_MARKER)


def _seed_parent(cache_dir: Path, session_id: str = PARENT, **overrides) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = {"mode": "work", "current_phase": "planning", "gates_approved": []}
    state.update(overrides)
    (cache_dir / f"writ-session-{session_id}.json").write_text(json.dumps(state))


def _seed_lazy_child(cache_dir: Path, agent_id: str, mode: str) -> None:
    """A child cache that already declares itself seeded (`is_subagent`), with its OWN
    mode, distinct from the parent's -- the `seeding printed skipped` shape."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"writ-session-{agent_id}.json").write_text(json.dumps({
        "mode": mode, "is_subagent": True, "cache_source": "lazy_seed",
    }))


def _run_hook(cache_dir: Path, *, agent_id: str, parent_id: str, port: int | None,
              task: str = "investigate the module structure", agent_type: str = "writ-explorer",
              extra_env: dict | None = None,
              exec_counters: dict[str, Path] | None = None, shim: Path | None = None,
              cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = str(cache_dir / "friction.log")
    if port is not None:
        env["WRIT_HOST"] = "localhost"
        env["WRIT_PORT"] = str(port)
    if shim is not None:
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    if exec_counters is not None:
        for name, path in exec_counters.items():
            path.write_text("")
            env[f"WRIT_EXEC_COUNTER_{name}"] = str(path)
    if extra_env:
        env.update(extra_env)
    envelope = {
        "agent_id": agent_id, "agent_type": agent_type, "session_id": parent_id,
        "hook_event_name": "SubagentStart", "task": task,
    }
    return subprocess.run(
        ["bash", str(HOOK)], input=json.dumps(envelope), capture_output=True, text=True,
        env=env, cwd=str(cwd) if cwd else None, timeout=20,
    )


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


def _injected_context(result: subprocess.CompletedProcess) -> str:
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        return (doc.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return ""


class TestNoModeGetRoundTripWhenSeedingReported:
    """Capability: writ-subagent-start.sh makes 0 GET /session/<agent_id>/mode requests
    when seeding printed a status. Pins 0 requests; the hook before batch 3 made 1,
    because it always resolved `CURRENT_MODE` through `_writ_session "mode get"`, which
    hits the daemon whenever it is reachable."""

    def test_zero_mode_get_requests_against_a_healthy_daemon(self, tmp_path, healthy_stub):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir)
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=healthy_stub)
        assert result.returncode == 0, result.stderr
        assert _BudgetStubHandler.mode_get_hits == [], (
            f"expected 0 GET /session/<agent>/mode requests: {_BudgetStubHandler.mode_get_hits}"
        )


class TestFrictionRowModeMatchesTheChildCache:
    """Capability: the subagent_start friction row's mode equals the child cache's
    mode, including when the child was already seeded with a mode different from the
    parent's (seeding prints `skipped`). The daemon in this scenario answers mode-get
    with a THIRD, wrong value, so a hook that still asks the daemon records the wrong
    mode; a hook that reads the child cache it just seeded (or found already seeded)
    records the real one. Pins the child cache's mode even when a healthy-but-wrong
    daemon is reachable."""

    def test_freshly_seeded_child_records_the_inherited_mode(self, tmp_path, healthy_stub):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, mode="work")
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=healthy_stub)
        assert result.returncode == 0, result.stderr
        rows = _friction_events(cache_dir, "subagent_start")
        assert len(rows) == 1
        assert rows[0].get("mode") == "work"

    def test_already_seeded_child_with_a_divergent_mode_records_its_own(self, tmp_path, healthy_stub):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, mode="work")
        _seed_lazy_child(cache_dir, AGENT, mode="review")
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=healthy_stub)
        assert result.returncode == 0, result.stderr
        rows = _friction_events(cache_dir, "subagent_start")
        assert len(rows) == 1
        assert rows[0].get("mode") == "review", (
            "the already-seeded child's own mode must win over both the parent's mode "
            f"and the daemon's (wrong) answer: {rows[0]}"
        )


class TestSeedFailureStillFallsBackToModeGet:
    """Capability: when the seed exec prints nothing (the oversized-role fault
    tests/test_subagent_seed.py::TestSeedFailureIsVisible triggers it with), the hook
    falls back to `_writ_session "mode get"` exactly once and still writes the critical
    line and the subagent_seed_failed row. Regression guard: SEED_STATUS is already
    empty on this fault at HEAD (the seed heredoc's exec itself dies on the oversized
    argument), so the fallback already runs unconditionally; 4a must not touch this path.

    The oversized value must be `agent_type`, not `task`: only `agent_type` crosses
    an exec boundary before the seed heredoc runs (parsed_field -> AGENT_TYPE ->
    WRIT_SA_ROLE, an exported env var the seed heredoc's `python3 -` inherits, per
    writ-subagent-start.sh:174/94). `task` stays on the hook's own stdin the whole
    time and never touches MAX_ARG_STRLEN.
    """

    _MAX_ARG_STRLEN = 32 * os.sysconf("SC_PAGE_SIZE")
    _PAD_BYTES = _MAX_ARG_STRLEN + 4096

    @staticmethod
    def _platform_enforces_arg_limit(pad_bytes: int) -> bool:
        try:
            subprocess.run(["true", "x" * pad_bytes], capture_output=True, timeout=30)
        except OSError as exc:
            return exc.errno == errno.E2BIG
        return False

    def test_falls_back_to_mode_get_exactly_once_and_records_the_failure(self, tmp_path):
        if not self._platform_enforces_arg_limit(self._PAD_BYTES):
            pytest.skip(
                f"platform accepted a {self._PAD_BYTES}-byte argv[1] without E2BIG; "
                "the oversized-role fault cannot be reproduced here"
            )
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq", "CURL": tmp_path / "c_curl"}
        closed_port = _free_port()
        result = _run_hook(
            cache_dir, agent_id=AGENT, parent_id=PARENT, port=closed_port,
            shim=shim, exec_counters=counters,
            extra_env={"WRIT_PROJECTS_DIR": str(tmp_path / "no-projects")},
            agent_type="x" * self._PAD_BYTES,
        )
        assert result.returncode == 0, result.stderr
        assert "[WRIT CRITICAL] writ-subagent-start" in result.stderr, result.stderr
        failed_rows = _friction_events(cache_dir, "subagent_seed_failed")
        assert len(failed_rows) == 1, failed_rows
        curl_lines = [ln for ln in counters["CURL"].read_text().splitlines() if "/mode" in ln]
        assert len(curl_lines) == 1, (
            f"expected exactly one mode-get round trip on the seed-failure fallback: {curl_lines}"
        )


class TestInjectionPathExecBudget:
    """Capability: on the injection path (healthy stub with rules) the hook's python3
    exec count is the HEAD baseline minus 4, jq plus 2, curl minus 1.

    HEAD baseline (measured against this exact harness, 2026-09-25): python3=15, jq=4,
    curl=4. Pins the counts after the batch 3 reductions, which the hook before it did
    not produce."""

    _BASELINE_PYTHON3 = 15
    _BASELINE_JQ = 4
    _BASELINE_CURL = 4

    def _measure(self, tmp_path, healthy_stub):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq", "CURL": tmp_path / "c_curl"}
        result = _run_hook(
            cache_dir, agent_id=AGENT, parent_id=PARENT, port=healthy_stub,
            shim=shim, exec_counters=counters,
        )
        assert result.returncode == 0, result.stderr
        return {name: _exec_count(path) for name, path in counters.items()}

    def test_python3_is_baseline_minus_4(self, tmp_path, healthy_stub):
        counts = self._measure(tmp_path, healthy_stub)
        assert counts["PYTHON3"] == self._BASELINE_PYTHON3 - 4, counts

    def test_jq_is_baseline_plus_2(self, tmp_path, healthy_stub):
        counts = self._measure(tmp_path, healthy_stub)
        assert counts["JQ"] == self._BASELINE_JQ + 2, counts

    def test_curl_is_baseline_minus_1(self, tmp_path, healthy_stub):
        counts = self._measure(tmp_path, healthy_stub)
        assert counts["CURL"] == self._BASELINE_CURL - 1, counts


class TestDaemonDownExecBudget:
    """Capability: with the daemon down the hook's python3 exec count is the HEAD
    baseline minus 3, curl minus 1, jq plus 1.

    HEAD baseline (measured against this exact harness, 2026-09-25, a closed port):
    python3=9, jq=3, curl=2. SA_OUTPUT runs on this path too (it sits outside the
    daemon-health block and PHASE_INFO is never empty), so moving it to jq removes a
    third python3 and adds one jq on top of the mode-get fallback and the merged
    heredoc. Pins all three reduced counts."""

    _BASELINE_PYTHON3 = 9
    _BASELINE_JQ = 3
    _BASELINE_CURL = 2

    def _measure(self, tmp_path):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir)
        shim = _shim_dir(tmp_path)
        counters = {"PYTHON3": tmp_path / "c_python3", "JQ": tmp_path / "c_jq", "CURL": tmp_path / "c_curl"}
        closed_port = _free_port()
        result = _run_hook(
            cache_dir, agent_id=AGENT, parent_id=PARENT, port=closed_port,
            shim=shim, exec_counters=counters,
        )
        assert result.returncode == 0, result.stderr
        return {name: _exec_count(path) for name, path in counters.items()}

    def test_python3_is_baseline_minus_3(self, tmp_path):
        counts = self._measure(tmp_path)
        assert counts["PYTHON3"] == self._BASELINE_PYTHON3 - 3, counts

    def test_curl_is_baseline_minus_1(self, tmp_path):
        counts = self._measure(tmp_path)
        assert counts["CURL"] == self._BASELINE_CURL - 1, counts

    def test_jq_is_baseline_plus_1(self, tmp_path):
        counts = self._measure(tmp_path)
        assert counts["JQ"] == self._BASELINE_JQ + 1, counts


class TestMergedHeredocRendersTheSameAdditionalContext:
    """Capability: the merged PHASE_INFO/PLAN_DIR_INFO heredoc renders the same
    additionalContext prefix as HEAD (two separate heredocs joined with a newline):
    phase line plus plan-artifacts line for a parent with a project_root, phase line
    alone when plan_dir yields nothing, `{}` defaults for an empty parent session.

    Regression guard: this is byte-identical output on both sides of 4a(ii) by
    construction (two heredocs concatenated == one heredoc emitting the same two
    lines), so it is already GREEN at HEAD and must stay green after the merge."""

    def test_phase_and_plan_lines_for_a_parent_with_a_project_root(self, tmp_path):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, mode="work", current_phase="implementation",
                      gates_approved=["phase-a"], project_root=str(tmp_path))
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=None)
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert "[Writ sub-agent: mode=work, phase=implementation, gates=phase-a]" in context
        assert f"[Writ plan artifacts: write plan.md and capabilities.md to {tmp_path}/.claude/plans/{PARENT}/]" in context

    def test_phase_line_alone_when_plan_dir_yields_nothing(self, tmp_path):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, mode="work", current_phase="planning", gates_approved=[])
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=None)
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        # A trailing "\n" is real HEAD behavior, not test noise: SA_OUTPUT's python
        # (writ-subagent-start.sh:446-448) does `ctx = sys.argv[1]; if sys.argv[2]:
        # ctx = sys.argv[2] + '\n' + ctx`, and with ADDITIONAL_CONTEXT empty (no rules
        # injected here) that is `PHASE_INFO + '\n' + ""`.
        assert context == "[Writ sub-agent: mode=work, phase=planning, gates=none]\n"

    def test_empty_parent_session_renders_the_documented_defaults(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = {"agent_id": AGENT, "agent_type": "writ-explorer",
                    "hook_event_name": "SubagentStart"}
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(cache_dir)
        env["WRIT_FRICTION_LOG"] = str(cache_dir / "friction.log")
        env["WRIT_PORT"] = "59999"
        result = subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(envelope), capture_output=True, text=True,
            env=env, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert "mode=work, phase=planning, gates=none" in context


class TestPhaseLineFailureStillCarriesThePlanLine:
    """Capability: a parent cache whose phase line cannot be rendered (gates_approved
    holding a non-string element, so `','.join(gates)` raises inside the heredoc and
    bash's `|| echo "[Writ sub-agent: isolated session]"` takes over) yields the
    isolated-session text and STILL carries the plan-artifacts line when that one
    resolves independently. Regression guard: already GREEN at HEAD (two independent
    heredocs), and the merge in 4a(ii) must not let the phase-line exception abort the
    plan-line computation too."""

    def test_isolated_session_text_with_the_plan_line_still_attached(self, tmp_path):
        cache_dir = tmp_path / "cache"
        _seed_parent(cache_dir, mode="work", current_phase="implementation",
                     gates_approved=[123], project_root=str(tmp_path))
        result = _run_hook(cache_dir, agent_id=AGENT, parent_id=PARENT, port=None)
        assert result.returncode == 0, result.stderr
        context = _injected_context(result)
        assert context.startswith("[Writ sub-agent: isolated session]")
        assert f"[Writ plan artifacts: write plan.md and capabilities.md to {tmp_path}/.claude/plans/{PARENT}/]" in context
