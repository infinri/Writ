"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 5a and 5b: session-start-bootstrap.sh
must stop paying a python carry-forward exec on a brand-new (`startup`) session, must
probe Neo4j with a half-second timeout instead of two seconds, and must stop discarding
the rest of the script's stderr after the Neo4j probe's fd-3 close.

5a(i): step 5's guard (170-173) runs `${VENV_DIR}/bin/python3 "${SESSION_HELPER}"
carry-forward-mode ...` whenever SID and CWD are non-empty, even on a `startup` payload,
where `rotation.carry_forward_mode` (writ/session/rotation.py:82-84) immediately no-ops
on its OWN `source == "startup"` check. The fix adds `[ "${SOURCE}" != "startup" ]` to
the bash guard, so the exec is skipped rather than started-and-thrown-away.

5a(ii): line 110's Neo4j probe becomes `timeout 0.5` (from `timeout 2`); behavior against
a real listening socket or a closed port is unchanged (only wall-clock differs, which
this file never asserts).

5b: lines 121-122, `exec 3<&- 2>/dev/null || true` / `exec 3>&- 2>/dev/null || true`,
use `exec` with no command, which makes EVERY redirection on that line permanent for the
rest of the script -- fd 2 stays /dev/null from step 3 onward, so writ_ensure_server's
"[Writ] Server already running" (and every other diagnostic it prints) never reaches
Claude Code. The fix scopes the redirect to a command group, `{ exec 3>&-; } 2>/dev/null
|| true`, so only the group's own stderr (irrelevant here -- the close cannot fail in a
way that prints anything) is silenced, and fd 2 is restored once the group ends.

Per ENF-PROC-TDD-001 / the plan's own instruction (## Analysis, "Counts"): the venv
python3 exec baseline is measured on HEAD through a PATH-independent shim (the venv's
OWN bin/python3, the pattern of tests/test_read_records_examined.py::
TestPython3ExecCountPerRead) and pinned as a literal constant.

Measured on HEAD, 2026-09-25, against this harness (real hook subprocess, CLAUDE_PLUGIN_ROOT
pointed at this repo so `import writ.*` resolves for real, a fake venv whose bin/python3
counts its own invocations, a real listening TCP socket standing in for Neo4j, tmp_path
HOME/XDG_STATE_HOME/WRIT_CACHE_DIR): a `startup` payload pays 3 venv python3 execs (the
venv-repoint probe, the payload parse, and carry-forward-mode, which then no-ops).

Per TEST-REGRESSION-001: the startup-skips-the-exec and half-second-timeout-literal and
stderr-reaches-the-caller capabilities are RED at HEAD (today always execs carry-forward
on startup, the probe reads `timeout 2`, and writ_ensure_server's stderr is swallowed);
the rotated-source-still-carries-forward-once and the Neo4j-probe's pass/fail behavior
against a real socket are regression guards, already GREEN at HEAD, and must stay green.

Per TEST-ISOLATE-002: HOME and XDG_STATE_HOME are redirected under tmp_path, WRIT_CACHE_DIR
is redirected under tmp_path, WRIT_NEO4J_HOST/PORT point at a throwaway socket this file
owns (never the real bolt port 7687), and WRIT_NO_AUTOSTART=1 keeps every test that is not
specifically about writ_ensure_server from ever touching port 8765.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.net import free_port as _free_port

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "session-start-bootstrap.sh"


_CALL_MARKER = "@@WRIT_VENV_PY_CALL@@"


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    """<venv>/bin/python3 appends ONE marker line plus its full argv to `counter`,
    then either exits 1 immediately (on writ_venv_repoint's own `-I` probe, so the
    probe's import "fails" fast and repoint never falls into its pip-install retry
    loop -- counted as exactly one exec either way) or execs the real interpreter
    (every other call: the payload parse and writ-session.py carry-forward-mode both
    need a working python to prove anything about how many times they ran).

    The marker line is load-bearing, not decoration: the payload parse (step 5) is a
    multi-line `-c` script, so `$*` itself contains embedded newlines, and counting
    raw FILE LINES (as opposed to marker occurrences) silently inflates one exec into
    a dozen "calls"."""
    real_python3 = shutil.which("python3")
    assert real_python3, "no python3 on PATH to build the fake venv from"
    venv = tmp_path / "fake-venv"
    (venv / "bin").mkdir(parents=True)
    counter = tmp_path / "venv-python3-execs.txt"
    counter.write_text("")
    wrapper = venv / "bin" / "python3"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "{_CALL_MARKER}\\n%s\\n" "$*" >> {shlex.quote(str(counter))}\n'
        'if [ "$1" = "-I" ]; then exit 1; fi\n'
        f'exec "{real_python3}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return venv, counter


@pytest.fixture()
def neo4j_listener():
    """A real listening TCP socket standing in for Neo4j. `/dev/tcp/HOST/PORT`'s connect
    completes against the kernel's own accept backlog; no explicit accept() is needed."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    port = s.getsockname()[1]
    try:
        yield port
    finally:
        s.close()


def _run_hook(
    tmp_path: Path, venv: Path, payload: dict, neo4j_port: int, *,
    autostart: bool = False, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg")
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env["WRIT_VENV"] = str(venv)
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    env["WRIT_NEO4J_HOST"] = "127.0.0.1"
    env["WRIT_NEO4J_PORT"] = str(neo4j_port)
    if autostart:
        env.pop("WRIT_NO_AUTOSTART", None)
    else:
        env["WRIT_NO_AUTOSTART"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30,
    )


def _venv_python3_calls(counter: Path) -> list[str]:
    """One entry per actual python3 exec -- split on the wrapper's call marker, NOT
    on newlines in the counter file, because the payload parse (step 5) is a
    multi-line `-c` script whose own embedded newlines would otherwise inflate one
    exec into many "calls". Each entry holds that call's full (possibly multi-line)
    argv, so a substring check like `"carry-forward-mode" in call` still works."""
    calls: list[str] = []
    current: list[str] = []
    for ln in counter.read_text().splitlines():
        if ln == _CALL_MARKER:
            if current:
                calls.append("\n".join(current))
            current = []
        else:
            current.append(ln)
    if current:
        calls.append("\n".join(current))
    return calls


class TestStartupPayloadSkipsCarryForward:
    """Capability: session-start-bootstrap.sh with a `startup` payload does not exec
    writ-session.py carry-forward-mode. Venv python execs 2 (3 at HEAD: the venv-repoint
    probe, the payload parse, and carry-forward-mode, which today always starts and then
    no-ops inside rotation.carry_forward_mode's own source=="startup" guard)."""

    _BASELINE_VENV_PYTHON3 = 3  # measured on HEAD, 2026-09-25, against this harness

    def _measure(self, tmp_path, neo4j_listener):
        venv, counter = _fake_venv(tmp_path)
        payload = {"session_id": "boot-startup-1", "cwd": str(tmp_path), "source": "startup"}
        result = _run_hook(tmp_path, venv, payload, neo4j_listener)
        assert result.returncode == 0, result.stderr
        return _venv_python3_calls(counter)

    def test_venv_python3_execs_is_baseline_minus_1(self, tmp_path, neo4j_listener):
        calls = self._measure(tmp_path, neo4j_listener)
        assert len(calls) == self._BASELINE_VENV_PYTHON3 - 1, calls

    def test_no_carry_forward_mode_exec_on_startup(self, tmp_path, neo4j_listener):
        calls = self._measure(tmp_path, neo4j_listener)
        carry = [c for c in calls if "carry-forward-mode" in c]
        assert carry == [], f"expected 0 carry-forward-mode execs on a startup payload: {carry}"


class TestRotatedSourcesStillCarryForwardOnce:
    """Capability: session-start-bootstrap.sh with resume, clear or compact payloads
    still execs carry-forward-mode exactly once. Regression guard: already GREEN at
    HEAD (today's unconditional exec IS exactly one exec), and 5a(i)'s new
    `source == "startup"` guard must leave every other source value untouched."""

    @pytest.mark.parametrize("source", ["resume", "clear", "compact"])
    def test_carry_forward_mode_execs_exactly_once(self, tmp_path, neo4j_listener, source):
        venv, counter = _fake_venv(tmp_path)
        payload = {"session_id": f"boot-{source}-1", "cwd": str(tmp_path), "source": source}
        result = _run_hook(tmp_path, venv, payload, neo4j_listener)
        assert result.returncode == 0, result.stderr
        calls = _venv_python3_calls(counter)
        carry = [c for c in calls if "carry-forward-mode" in c]
        assert len(carry) == 1, f"expected exactly one carry-forward-mode exec: {calls}"


class TestNeo4jProbe:
    """Capability: the Neo4j probe is `timeout 0.5` over NEO4J_HOST/NEO4J_PORT, succeeds
    against a real listening socket named through WRIT_NEO4J_HOST/WRIT_NEO4J_PORT (no
    "Neo4j not reachable" on stderr), and prints that block and exits 0 against a closed
    port. Only the timeout-literal assertion is RED at HEAD (today reads `timeout 2`);
    the pass/fail behavior against a real socket is a regression guard, already GREEN
    (both timeout values comfortably clear a local accept/refuse), and this file makes
    no wall-clock timing assertion that could tell them apart."""

    def test_source_uses_a_half_second_timeout(self):
        text = HOOK.read_text()
        assert 'timeout 0.5 bash -c "exec 3<>/dev/tcp/${NEO4J_HOST}/${NEO4J_PORT}"' in text, (
            "the Neo4j probe must read `timeout 0.5`, not the HEAD `timeout 2`"
        )

    def test_succeeds_against_a_real_listening_socket(self, tmp_path, neo4j_listener):
        venv, _counter = _fake_venv(tmp_path)
        payload = {"session_id": "boot-neo4j-up-1", "cwd": str(tmp_path), "source": "startup"}
        result = _run_hook(tmp_path, venv, payload, neo4j_listener)
        assert result.returncode == 0, result.stderr
        assert "Neo4j not reachable" not in result.stderr

    def test_prints_the_block_and_exits_zero_against_a_closed_port(self, tmp_path):
        closed_port = _free_port()  # bound then released; nothing listens on it
        venv, _counter = _fake_venv(tmp_path)
        payload = {"session_id": "boot-neo4j-down-1", "cwd": str(tmp_path), "source": "startup"}
        result = _run_hook(tmp_path, venv, payload, closed_port)
        assert result.returncode == 0, result.stderr
        assert f"Neo4j not reachable at 127.0.0.1:{closed_port}" in result.stderr


class TestServerAlreadyRunningMessageReachesStderr:
    """Capability: session-start-bootstrap.sh with WRIT_HEALTH_CMD=true and autostart
    enabled shows `[Writ] Server already running` on stderr. RED at HEAD: `exec 3<&-
    2>/dev/null || true` (the Neo4j probe's fd-3 close, step 3) makes fd 2 stay
    /dev/null for the rest of the script -- including through step 4 -- so
    writ_ensure_server's own stderr, this line among it, never reaches Claude Code.
    Injection matches tests/test_install_decoupling.py:428-431's WRIT_HEALTH_CMD=true
    pattern, so nothing here ever binds or connects to the real daemon port."""

    def test_shows_server_already_running_on_stderr(self, tmp_path, neo4j_listener):
        venv, _counter = _fake_venv(tmp_path)
        payload = {"session_id": "boot-autostart-1", "cwd": str(tmp_path), "source": "startup"}
        result = _run_hook(
            tmp_path, venv, payload, neo4j_listener, autostart=True,
            extra_env={"WRIT_HEALTH_CMD": "true"},
        )
        assert result.returncode == 0, result.stderr
        assert "[Writ] Server already running" in result.stderr
