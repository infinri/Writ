"""The ONE owner of "run a production hook script as a subprocess against a
daemon this module owns": the daemon START (one context manager, two module
fixtures over it), the hook-subprocess env in its two shapes, the session-cache
seeder, the seeded-cache read-back check, the hook runner, and the access-log
request counter.

WHY A SECOND OWNER BESIDE `tests/_prompt_turn.py`, which is the proven pattern
and the right precedent. That module's subject is one production TURN: two
specific hooks, a drain only the Stop hook performs, and a corpus precondition
written for the two direct-`/query` retrieval modules (`Playbook|Technique` with
`domain='process'`). The three modules that take this harness drive five
different hooks on four different events and need a different precondition, the
injection-rule population `/prompt-bundle`'s always-on channel selects from. So
`hook_env` and `seed_session_cache` MOVED here and `tests/_prompt_turn.py`
imports them back, which leaves the hook-subprocess env dict existing exactly
ONCE in the suite where it existed six times before: two full copies, plus a
degenerate `{**os.environ, "WRIT_HOST": "localhost"}` in each of the three
modules below, each of those missing four of its five entries.

THE FIXTURE IS DELIBERATELY NOT COLLAPSED with `isolated_prompt_daemon`. The two
differ in exactly one load-bearing way, the corpus predicate each needs, and
collapsing them would mean a parameter only one caller ever varies. The env dict
is the part whose drift is SILENT (a green-looking run against the operator's
daemon on 8765, which is what a dropped `WRIT_SOCKET` produces); a fixture
body's drift is LOUD (a daemon that does not start fails the module). So the
silent one got one owner and the loud one stayed where it is.

THE START ITSELF IS ONE CONTEXT MANAGER AND THE PRECONDITION IS NOT IN IT, which
is the split this cycle added. `instrumented_daemon(tmp_dir)` owns the start, the
access-log positive control and the asserted stop verdict; `isolated_hook_daemon`
is that plus the injection-rule population `/prompt-bundle`'s always-on channel
selects from, and `isolated_daemon` is that with NO corpus precondition at all.
The corpus-free sibling exists on measurement rather than for symmetry: four of
this cycle's consumers (`test_fix2_cache_alignment`, `test_pol5a_statusline`,
`test_inc1b_daemon_alignment`, `test_bash_write_gate`) retrieve NOTHING, so
reusing the injection-rule precondition would fail four modules on a predicate
unrelated to their subject, which is the same class of error as a skip that hides
a real state. A module whose subject needs a DIFFERENT population calls
`require_population` in its own module fixture and then this context manager
(`test_fix3_metrics.py::owned_daemon`, two lines), because the precondition is a
fact about that module's PROPERTY rather than about the daemon.

THE INSTRUMENT IS THE DAEMON'S OWN ACCESS LOG. `writ serve` runs uvicorn with
`log_level="info"` and the default access log (`writ/cli.py:467,491`), and
`start_isolated_daemon` pops `WRIT_LOG` and sets `WRIT_LOG_ROOT`, so
`writ_default_server_log` (`scripts/lib/writ-server-lib.sh:130`) resolves the
daemon's stdout to `<log_root>/server.log` inside the module's own tmp dir.
MEASURED against a daemon started that way on 2026-09-10, one line per served
request: `INFO:     127.0.0.1:39442 - "GET /health HTTP/1.1" 200 OK`, two health
lines from the start's own poll and one `"POST /analyze HTTP/1.1"` line for one
POST. Every request that daemon serves therefore leaves a line in a file this
module owns. That is the daemon reporting what it was ASKED, which is the only
honest way to prove a hook took (or did not take) a round trip, and it works
whether or not the query returned a hit.

MODULE TOP LEVEL IS STDLIB ONLY (plus pytest, which the fixture needs), the
discipline `tests/_daemon.py` and `tests/_prompt_turn.py` state for themselves:
`writ.graph.predicates` and `tests._corpus` are imported inside the fixture, so
importing this module pulls in no `writ.*` tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests._daemon import start_isolated_daemon, stop_isolated_daemon

# The access-log signature of one served request, and the fixture's own POSITIVE
# CONTROL. The start's health poll has already made this call by the time the
# fixture body runs, so an absent line means the INSTRUMENT is unavailable rather
# than that the daemon was idle. That distinction is the whole point: a dead
# instrument reads as ZERO, and zero is exactly what several absence assertions
# in these modules want to see, so it has to fail once and loudly instead.
_HEALTH_REQUEST_LINE = '"GET /health HTTP'

# The uvicorn access line's own marker, used to keep `daemon_requests` to the
# request lines and off the startup banner, the onnxruntime device warnings and
# the "Pre-warming indexes..." chatter that share this file.
_ACCESS_LINE_MARKER = 'HTTP/1.1"'


def daemon_requests(daemon: dict) -> list[str]:
    """Every request line the daemon has served, read from `<log_root>/server.log`.

    TOLERANT OF AN UNREADABLE FILE ON PURPOSE, with the loudness in exactly one
    place instead: `isolated_hook_daemon` refuses to yield a daemon whose log
    holds no health line, so an unavailable instrument fails ONCE, naming the
    file, rather than making every count in the module read zero. Raising here
    would instead bury that one diagnosis in a traceback inside whichever test
    happened to read the log first.
    """
    path = Path(daemon["log_root"]) / "server.log"
    try:
        text = path.read_text()
    except OSError:
        return []
    return [line for line in text.splitlines() if _ACCESS_LINE_MARKER in line]


def count_requests(daemon: dict, pattern: str) -> int:
    """How many served request lines contain `pattern`.

    Callers pass the quoted request line up to (not including) the HTTP version,
    e.g. `'"POST /query HTTP'`, and read a DELTA around one hook run: no live
    total is ever pinned, and a delta is the only form that stays true across a
    module whose earlier tests already made requests.
    """
    return sum(1 for line in daemon_requests(daemon) if pattern in line)


@contextmanager
def instrumented_daemon(tmp_dir):
    """One daemon the CALLER owns, on an OS-assigned free port, with its
    instrument proven before the caller can read it and its stop verdict
    asserted afterwards. `with instrumented_daemon(tmp_dir) as daemon:`.

    THE ONE OWNER OF THE START, so the three things that travel with it cannot
    drift apart: the isolation (log root, socket, cache dir under `tmp_dir`), the
    access-log positive control, and the verified stop. A caller adds only what
    its own subject requires, which today is a corpus precondition or nothing.

    A CONTEXT MANAGER RATHER THAN A PARAMETERIZED FIXTURE, because the two
    fixtures below differ in the PREDICATE each runs before the start, and a
    fixture parameter only one caller ever varies is the shape this repo already
    rejected once for `isolated_prompt_daemon`. It also lets a module fixture that
    needs its own precondition compose the two in two lines
    (`test_fix3_metrics.py::owned_daemon`) instead of copying the start.

    THE PORT IS NEVER NAMED HERE: `start_isolated_daemon` takes one from
    `tests/fixtures/net.py::free_port`. An OS-assigned port is out of reach of
    `conftest.py`'s `pytest_sessionfinish` (which stops the daemon answering on
    `WRIT_PORT`) and out of the leak guard's suite-port exclusion, which is why
    this stops its own daemon and ASSERTS the verdict.

    The socket path is `<tmp_dir>/writ.sock`, MEASURED at 56 bytes on this machine
    (`/tmp/pytest-of-lucio.saldivar/pytest-61/hookd0/writ.sock`) against
    `writ/config.py::MAX_SOCKET_PATH` = 107. That figure is a function of TMPDIR
    and the user name, which is why an over-cap path is a STATED start-verdict
    reason rather than a silent None, and why the only skip in this whole chain is
    a daemon that could not start, carrying the daemon's own distinct reason.

    `tcp_readonly` is left FALSE, and here that is load-bearing twice over: these
    hooks POST `/prompt-bundle`, `/query` and `/analyze` over TCP, and
    `writ/server/transport.py:79` allowlists only `/query`, so a read-only
    posture would answer 403 to the very calls whose round trips these tests
    count.

    THE STOP IS IN A `finally` and the verdict is asserted after it. The `with`
    body is a fixture's own `yield`, and pytest can CLOSE that generator instead
    of resuming it (GeneratorExit at the yield), which would carry a bare cleanup
    path straight past the stop and leave a live daemon on an OS-assigned port
    that no later guard can attribute to us.
    """
    tmp_dir = Path(tmp_dir)
    daemon = start_isolated_daemon(
        log_root=str(tmp_dir / "logs"),
        socket_path=str(tmp_dir / "writ.sock"),
        cache_dir=str(tmp_dir / "cache"),
        tcp_readonly=False,
    )
    if not daemon.get("started"):
        pytest.skip(daemon["reason"])
    server_log = Path(daemon["log_root"]) / "server.log"
    if count_requests(daemon, _HEALTH_REQUEST_LINE) < 1:
        refusal = (
            f"the daemon on port {daemon['port']} started and answered /health, but its "
            f"access log at {server_log} holds no {_HEALTH_REQUEST_LINE}...' line "
            f"(file present: {server_log.exists()}, request lines seen: "
            f"{len(daemon_requests(daemon))}). Every request-count assertion in this "
            f"module would read ZERO for want of an instrument, and zero is what several "
            f"of them expect to see, so this fails here instead of there."
        )
        # THE DAEMON IS STOPPED BEFORE THE REFUSAL, and that order was MEASURED
        # rather than reasoned. A failure raised between the start and the `yield`
        # skips a fixture's own teardown, so the first draft of this control turned
        # one loud failure into two: with the instrument moved away on purpose, the
        # four behavioral tests errored AND conftest's module-scoped leak guard
        # reported the surviving daemon, which it deliberately refuses to signal
        # ("a process the suite did not prove it started is the operator's to
        # stop"). The stop verdict is not asserted here: the diagnosis above is the
        # one this failure is for.
        stop_isolated_daemon(daemon)
        pytest.fail(refusal, pytrace=False)
    try:
        yield daemon
    finally:
        verdict = stop_isolated_daemon(daemon)
    assert verdict["stopped"], (
        f"instrumented_daemon teardown could not stop the daemon on port "
        f"{verdict.get('port')}: pids {verdict.get('pids')}, surviving pids "
        f"{verdict.get('survivors')}, table measured "
        f"{verdict.get('pids_measured')}, health {verdict.get('health')!r}"
    )


@pytest.fixture(scope="module")
def isolated_hook_daemon(tmp_path_factory):
    """`instrumented_daemon` plus the injection-rule corpus precondition, for a
    module whose hooks RETRIEVE.

    ALL of these tests need a daemon PROCESS: every one of them is a bash hook
    subprocess that curls, and no in-process ASGI transport can serve a
    subprocess.

    MODULE SCOPE IS MANDATORY, not a preference. `tests/conftest.py:135`'s
    `_daemon_leak_guard` is module-scoped and autouse and fails any module that
    leaves a live daemon behind, excluding only the suite port, so a
    session-scoped daemon would be reported as a leak at the first module
    boundary it survived.

    THE CORPUS IS CHECKED BEFORE THE START, never after, for the reason
    `tests/_prompt_turn.py:79-82` gives and which applies unchanged: the daemon
    builds its indexes AT STARTUP, so a repair afterwards is invisible to the
    process that needed it. The population is the one `/prompt-bundle`'s
    always-on channel selects from, `INJECTION_RULE_WHERE` imported from the
    production predicate so this precondition cannot drift from what
    `writ/server/routes/query.py:705` interpolates.
    """
    from writ.graph.predicates import INJECTION_RULE_WHERE

    from tests._corpus import require_population

    require_population(
        f"MATCH (r:Rule) WHERE {INJECTION_RULE_WHERE} RETURN count(r)",
        "the injection-rule population /prompt-bundle's always-on channel selects from",
    )
    with instrumented_daemon(tmp_path_factory.mktemp("hookd")) as daemon:
        yield daemon


@pytest.fixture(scope="module")
def isolated_daemon(tmp_path_factory):
    """`instrumented_daemon` with NO corpus precondition, for a module whose
    subject is the daemon PROCESS itself.

    THE ABSENCE OF A PRECONDITION IS THE POINT, and it is measured per consumer
    rather than assumed: `test_fix2_cache_alignment` asserts on the `cache_dir`
    the server process resolved, `test_pol5a_statusline` on a `POST
    /context-percent` round trip, `test_inc1b_daemon_alignment` on cache-dir
    alignment and a PostCompact reset, and `test_bash_write_gate` on a write-gate
    round trip decided from the session cache. None of the four retrieves a rule,
    so `isolated_hook_daemon`'s injection-rule population would fail all four on
    a predicate that has nothing to do with their subject: the same class of
    error as a skip that hides a real state, in the opposite direction.

    MODULE SCOPE for the reason `isolated_hook_daemon` gives: the leak guard is
    module-scoped and autouse, so a longer-lived daemon on an OS-assigned port
    reads as a leak at the first module boundary it survives.
    """
    with instrumented_daemon(tmp_path_factory.mktemp("isod")) as daemon:
        yield daemon


def hook_env(daemon: dict | None = None, *, cache_dir: str | None = None) -> dict:
    """The subprocess env for a production hook run, in its TWO shapes, built in
    ONE place so no caller can drift. Every entry closes a measured trap.

    - `WRIT_SOCKET` is the arm that actually WINS. `bin/lib/common.sh:1817` takes
      the socket whenever `WRIT_SOCKET` is set and that socket exists, AHEAD of
      `WRIT_HOST`/`WRIT_PORT`, and `bin/lib/writ_daemon_client.py:122,137` reads
      the same variable with the operator's `~/.cache/writ/run/writ.sock` as its
      DEFAULT. Setting it is what makes the transport deterministic instead of
      dependent on whether the operator's socket happens to exist.
    - `WRIT_HOST` and `WRIT_PORT` are not redundant beside it. The hooks build
      their URLs from them, and `_writ_transport_for` (`common.sh:227-236`)
      applies the socket flag ONLY to a URL whose port is `WRIT_SESSION_PORT`, so
      both spellings must name the same daemon or the socket is silently not
      used. They also make the exit-7 TCP retry (`common.sh:253-256`) land on
      that same daemon.
    - `WRIT_CACHE_DIR` is taken from what the SERVER PROCESS reports
      (`daemon["health"]["cache_dir"]`), not from what the fixture asked for, and
      must be identical across every run of a module: `/prompt-bundle` reads the
      session cache server-side, and the event buffer a Stop hook drains lives
      under this dir.
    - `WRIT_FRICTION_LOG` is POPPED, not merely assumed absent. It is
      `emit_destination`'s collapse branch: surviving into the child it puts
      EVERY stream in one file and no `metrics` row exists to read.
    - `WRIT_NO_AUTOSTART` is set explicitly even though conftest forces it at
      import. Without it `writ-rag-inject.sh:48-79` launches a REAL daemon on
      whatever port it was handed when its health check fails; that daemon
      outlives the test and fails the module on the leak guard.

    `WRIT_LOG_ROOT` is deliberately INHERITED (`tests/conftest.py:109` points it
    at the per-test `tmp_path/logs`), because it is both the hook's destination
    and the test's read side, and this env is built at CALL time so the
    function-scoped monkeypatch has already applied.

    THE NO-DAEMON SHAPE (`daemon=None`) IS FAIL-CLOSED, and both of its addresses
    are named rather than left inherited, which is the whole difference between
    "this hook reached nothing" and "this hook reached the operator's daemon":
    `WRIT_SOCKET` names a path inside the caller's cache dir that does not exist
    (so `[ -S ]` is false and the python client's default socket is out of
    reach), and `WRIT_PORT` names a free port nothing serves. It exists for
    `writ-run-pending-tests.sh`, which reaches no daemon by design:
    `is_work_mode` reads the cache FILE through `writ_session_mode_direct`
    (`common.sh:950-969`), `current-phase` is a local `writ-session.py` call, and
    both loggers write through local python.
    """
    env = dict(os.environ)
    env.pop("WRIT_FRICTION_LOG", None)
    if daemon is not None:
        env.update(
            {
                "WRIT_SOCKET": daemon["socket_path"],
                "WRIT_HOST": "localhost",
                "WRIT_PORT": str(daemon["port"]),
                "WRIT_CACHE_DIR": daemon["health"]["cache_dir"],
                "WRIT_NO_AUTOSTART": "1",
            }
        )
        return env
    if not cache_dir:
        raise ValueError(
            "hook_env(daemon=None) is the fail-closed no-daemon shape and needs an "
            "explicit cache_dir: an inherited WRIT_CACHE_DIR would point the hook at "
            "the suite's shared cache dir instead of this test's, so the seed it reads "
            f"would not be the one the test wrote (cache_dir={cache_dir!r})"
        )
    from tests.fixtures.net import free_port

    env.update(
        {
            "WRIT_SOCKET": os.path.join(cache_dir, "writ-no-daemon.sock"),
            "WRIT_HOST": "localhost",
            "WRIT_PORT": str(free_port()),
            "WRIT_CACHE_DIR": cache_dir,
            "WRIT_NO_AUTOSTART": "1",
        }
    )
    return env


def seed_session_cache(
    cache_dir: str,
    sid: str,
    mode: str,
    *,
    current_phase: str | None = None,
    is_orchestrator: bool = False,
    gates_approved: list[str] | None = None,
    analysis_results: dict[str, str] | None = None,
) -> str:
    """Seed a session cache in the DAEMON's own cache dir, explicitly.

    `/prompt-bundle` and the session routes read the cache SERVER-SIDE
    (`writ/server/routes/query.py:228`, `_read_cache(sid)`), so the seed must land
    in the dir that daemon resolved. Callers therefore pass
    `daemon["health"]["cache_dir"]`, which is what the SERVER PROCESS reports,
    rather than what the fixture asked for. That explicit-directory contract is
    the reason this replaces the three `importlib`-by-path loads of
    `bin/lib/writ-session.py` the hook modules used: their `_read_cache` and
    `_write_cache` resolve the cache dir at CALL time, in the test process, which
    is a different question from where the daemon reads.

    A PARTIAL DICT IS THE PRODUCTION SHAPE, not a shortcut:
    `writ/session/cache.py::_read_cache` fills every missing key from
    `_default_cache`, so a reread of this file carries the full keyset. The four
    keyword fields are the ones consumers of this harness gate on:
    `is_orchestrator` (the status-line branch), `current_phase` (the phase gates),
    `gates_approved` (so no gate is pending) and `analysis_results` (which
    `validate-rules-helper.py:223` requires at "pass" for a file before
    `should_proceed` lets the hook reach `/analyze`).
    """
    path = os.path.join(cache_dir, f"writ-session-{sid}.json")
    with open(path, "w") as handle:
        json.dump(
            {
                "mode": mode,
                "is_orchestrator": is_orchestrator,
                "is_subagent": False,
                "current_phase": current_phase,
                "gates_approved": list(gates_approved or []),
                "analysis_results": dict(analysis_results or {}),
                "loaded_rule_ids": [],
                "loaded_rule_ids_by_phase": {},
                "remaining_budget": 8000,
                "context_percent": 0,
                "queries": 0,
                "files_written": [],
                "loaded_rules": [],
            },
            handle,
        )
    return path


def verify_seeded_mode(daemon: dict, sid: str, mode: str) -> None:
    """Read the seeded mode BACK out of the daemon and fail if it disagrees.

    THIS IS THE INC-1b DESYNC IN ONE ASSERTION. `_writ_session "mode get"` falls
    through to a LOCAL subprocess whenever the daemon answers an EMPTY mode
    (`bin/lib/common.sh:1876-1892`), which is the correct production degradation
    and also means a diverged cache dir lets every mode-gated test pass while the
    daemon knows nothing about the session. Asking the daemon what mode it holds,
    before any assertion depends on it, turns that into one loud failure.

    It runs BEFORE a test takes its `before` counts, so the
    `GET /session/{sid}/mode` line it adds to the access log is outside every
    delta.
    """
    url = f"{daemon['base_url']}/session/{sid}/mode"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read())
    assert payload.get("mode") == mode, (
        f"the daemon on port {daemon['port']} reports mode {payload.get('mode')!r} for "
        f"session {sid}, not the seeded {mode!r}. The seed did not land in the dir this "
        f"daemon reads ({daemon['health'].get('cache_dir')!r}), so every mode-gated "
        f"assertion below would be measuring the local-subprocess fallback instead of "
        f"this daemon."
    )


def run_hook(
    script,
    envelope: str,
    *,
    env: dict,
    cwd: str,
    timeout: int,
    debug_log: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ONE production hook script as a subprocess, the way Claude Code does:
    `bash <hook>` with the JSON envelope on stdin.

    `cwd` IS PASSED, NEVER DEFAULTED, and that is the trap `run_prompt_turn`
    documents at its lines 315-323: `log_gate_decision` and the friction rows
    resolve their project from the process cwd AT EMIT TIME, so a test that reads
    a row back must run the hook from the same directory it reads under.

    `debug_log` sets `WRIT_DEBUG=1` for ONE run and points both debug sinks at
    the caller's file: `WRIT_DEBUG_LOG` (`writ-rag-inject.sh:26`, an
    env-overridable knob whose default is a shared `/tmp/writ-rag-debug.log`) and
    `WRIT_HOOK_LOG` (`hook_log_sink`, `common.sh:320-330`, default
    `/tmp/writ-hooks.log`). It is per-run rather than part of the shared env
    because `auto-approve-gate.sh:151` writes its breadcrumb to a HARDCODED
    `/tmp/writ-prompt-debug.log`, and no test of ours may append to a shared
    operator file.
    """
    run_env = dict(env)
    if debug_log:
        run_env["WRIT_DEBUG"] = "1"
        run_env["WRIT_DEBUG_LOG"] = debug_log
        run_env["WRIT_HOOK_LOG"] = debug_log
    return subprocess.run(
        ["bash", str(script)],
        input=envelope,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=run_env,
        timeout=timeout,
    )
