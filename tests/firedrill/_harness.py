"""The one hook runner every drill module shares (plan.md Decision 2).

Builds the isolated child env, feeds a census envelope on stdin, captures exit
code plus stdout plus stderr, and reads records back through
`writ.shared.logging.read_streams`. Reuses the pattern already proven in
`tests/test_stop_hook_friction_logger.py`: `subprocess.run(["bash", HOOK],
input=<envelope JSON>, env=<built env>, cwd=<tmp project>)`.

HARNESS TRAPS (see the dispatch brief for the incident each one closes):

1. The subprocess does NOT share this process's state. Whatever a test seeds must be
   written to a FILE inside the tmp `WRIT_CACHE_DIR` this module builds and hands the
   child in its env -- never monkeypatched into this process alone.
2. `WRIT_FRICTION_LOG` collapses every typed stream into one file. `build_env` always
   removes it from the child env, and callers read records back through
   `writ.shared.logging.read_streams` (which also unions rotated `archive/*.jsonl.gz`
   generations), never by grepping a single log file.
3. `HOME` is pinned to a tmp dir in the child env. Blackbox capture is live via the
   sentinel `~/.claude/writ-blackbox.on`, and both `blackbox_log` and `load_hook_env`
   in `bin/lib/common.sh` test for that file under `$HOME`. `assert_real_capture_log_
   untouched` below is the standing check that this drill never appends to the
   developer's real capture log.
4. `WRIT_PORT` is pointed at a closed port and `WRIT_NO_AUTOSTART=1` is set, so every
   gate that consults the daemon takes its local-fallback arm -- the daemon-unreachable
   outage case, which is also the arm most likely to lose a record.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HOOKS_DIR = REPO / "hooks" / "scripts"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from writ.shared.logging import read_streams  # noqa: E402

DEFAULT_SESSION_ID = "firedrill-session"


def closed_port() -> int:
    """An ephemeral TCP port with nothing bound to it.

    Binds to port 0 (OS-assigned, guaranteed free at bind time), reads the assigned
    port back, then closes the socket so nothing is listening there. Small race
    window between close and use is the accepted cost every test in this repo that
    needs "a port nothing answers on" already pays; there is no portable way to
    reserve a closed port outright.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@dataclass
class Isolation:
    """Everything a hook invocation needs isolated into one tmp tree.

    Every path here is derived from the test's own `tmp_path` fixture, never from
    `Path.home()` or a fixed skill-relative default -- that is the property capability
    "the harness env builder never points WRIT_CACHE_DIR, WRIT_LOG_ROOT or HOME at a
    path outside the test's tmp dir" pins.
    """

    tmp_path: Path
    cache_dir: Path
    log_root: Path
    fake_home: Path
    project_root: Path
    log_project: str
    session_id: str


def make_isolation(
    tmp_path: Path,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    log_project: str | None = None,
) -> Isolation:
    """Build the tmp cache/log/home/project tree for one drill case.

    `project_root` gets a `.git` marker so `detect_project_root` (bash) and
    `derive_project_identity` (python) both resolve it, and no `.gitignore` so the
    worktree-safety drill case's "no .gitignore exists" branch is reachable without
    extra setup.
    """
    cache_dir = tmp_path / "cache"
    log_root = tmp_path / "logs"
    fake_home = tmp_path / "home"
    project_root = tmp_path / "proj"
    for d in (cache_dir, log_root, fake_home, project_root):
        d.mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (project_root / ".git").mkdir(parents=True, exist_ok=True)
    return Isolation(
        tmp_path=tmp_path,
        cache_dir=cache_dir,
        log_root=log_root,
        fake_home=fake_home,
        project_root=project_root,
        log_project=log_project or f"firedrill-{session_id}",
        session_id=session_id,
    )


def build_env(iso: Isolation, *, extra: dict | None = None) -> dict:
    """The isolated child env (plan.md Decision 2): six pinned variables, nothing else.

    Starts from a copy of this process's own environment (so PATH, HOME's ordinary
    siblings, etc. still resolve `bash`/`python3`/`pytest`), then pins:
      WRIT_CACHE_DIR   -> iso.cache_dir      (session state; never the real skill install)
      WRIT_LOG_ROOT    -> iso.log_root       (typed streams; never the real skill install)
      WRIT_LOG_PROJECT -> iso.log_project    (deterministic scope, independent of git identity)
      WRIT_FRICTION_LOG -> removed           (the single-file collapse would make every
                                               stream-routing assertion in this drill vacuous)
      WRIT_NO_AUTOSTART -> "1"               (no hook may spawn a daemon)
      WRIT_PORT        -> a closed port      (every daemon-consulting gate takes its
                                               local-fallback arm -- the outage case)
      HOME             -> iso.fake_home      (the real ~/.claude/writ-blackbox.jsonl must
                                               never see this drill's traffic)
    Also prepends `<repo>/.venv/bin` to PATH. This is not one of the six pinned
    variables -- it is a fix for a fact about THIS machine, not a drill requirement:
    `~/.local/bin/pytest`'s shebang is the bare system `python3`, which resolves its
    own `import pytest` through the user-site path under `$HOME`. Pinning HOME (trap
    3, required) breaks that resolution for any hook that shells out to `pytest` on
    PATH (writ-run-pending-tests.sh); `.venv/bin/pytest` is a self-contained
    virtualenv script that needs no HOME-based site lookup, so putting it first on
    PATH fixes the one hook that needs a real test runner without weakening HOME's
    isolation for every other hook.
    `extra` may add or override additional variables (e.g. TMPDIR for a script that
    resolves its own sentinel path under `${TMPDIR:-/tmp}`, or a gate-specific feature
    toggle) -- never one of the six pinned above; a caller that tried would be
    overridden by the values already applied here from the base copy, since `extra`
    is applied AFTER.
    """
    env = dict(os.environ)
    env["WRIT_CACHE_DIR"] = str(iso.cache_dir)
    env["WRIT_LOG_ROOT"] = str(iso.log_root)
    env["WRIT_LOG_PROJECT"] = iso.log_project
    env.pop("WRIT_FRICTION_LOG", None)
    env["WRIT_NO_AUTOSTART"] = "1"
    env["WRIT_PORT"] = str(closed_port())
    env["HOME"] = str(iso.fake_home)
    venv_bin = REPO / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    if extra:
        for k, v in extra.items():
            env[k] = str(v)
    return env


def write_cache(iso: Isolation, cache: dict, *, session_id: str | None = None) -> Path:
    """Seed one session's cache FILE inside `iso.cache_dir` (trap 1: never in-process)."""
    sid = session_id or iso.session_id
    path = iso.cache_dir / f"writ-session-{sid}.json"
    path.write_text(json.dumps(cache))
    return path


def read_cache(iso: Isolation, *, session_id: str | None = None) -> dict | None:
    sid = session_id or iso.session_id
    path = iso.cache_dir / f"writ-session-{sid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


@dataclass
class HookResult:
    returncode: int
    stdout: str
    stderr: str
    iso: Isolation

    def stdout_json(self) -> dict | None:
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None

    def permission_decision(self) -> str | None:
        """`hookSpecificOutput.permissionDecision` from stdout, or None."""
        doc = self.stdout_json()
        if not doc:
            return None
        return (doc.get("hookSpecificOutput") or {}).get("permissionDecision")

    def permission_reason(self) -> str:
        """`permissionDecisionReason`, falling back to `additionalContext`, else ''."""
        doc = self.stdout_json()
        if not doc:
            return ""
        hso = doc.get("hookSpecificOutput") or {}
        return hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""

    def audit(self) -> list[dict]:
        return read_streams(self.iso.log_project, ["audit"])

    def friction(self) -> list[dict]:
        return read_streams(self.iso.log_project, ["friction"])

    def metrics(self) -> list[dict]:
        return read_streams(self.iso.log_project, ["metrics"])

    def all_streams(self) -> list[dict]:
        return read_streams(self.iso.log_project, ["audit", "friction", "metrics", "errors"])


def run_hook(
    script: str,
    envelope: dict,
    iso: Isolation,
    *,
    extra_env: dict | None = None,
    cwd: Path | None = None,
    timeout: int = 45,
) -> HookResult:
    """Trigger `hooks/scripts/<script>` as a real subprocess with `envelope` on stdin."""
    hook_path = HOOKS_DIR / script
    assert hook_path.is_file(), f"no such hook script: {hook_path}"
    env = build_env(iso, extra=extra_env)
    proc = subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or iso.project_root),
        timeout=timeout,
    )
    return HookResult(proc.returncode, proc.stdout, proc.stderr, iso)


@contextmanager
def stub_analyze_server(response: dict):
    """A loopback HTTP server that answers every POST with `response` as JSON.

    Used only by the validate-rules.sh phase-boundary drill case (test_bash_refusals.py
    `TestValidateRulesBothSites`), which needs a live `/analyze` verdict to reach its
    tail-end sentinel branch. `WRIT_PORT` pointed at a closed port is the default for
    the rest of the drill precisely because THAT arm (daemon unreachable) is the one
    under test everywhere else; this one case is the exception, and it stubs only the
    `/analyze` response shape, never any Writ production code.
    """
    body = json.dumps(response).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (stdlib-mandated name)
            length = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # silence stdlib access log
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def real_blackbox_log_path() -> Path:
    """The REAL capture log this drill must never write to (trap 3)."""
    return Path(os.path.expanduser("~/.claude/writ-blackbox.jsonl"))


def real_blackbox_snapshot() -> tuple[bool, int]:
    """(exists, size) of the real capture log, taken before/after a drill run."""
    path = real_blackbox_log_path()
    if not path.exists():
        return False, 0
    return True, path.stat().st_size
