"""The ONE owner of the prompt-turn harness the two prompt-injection modules
duplicated: the isolated-daemon fixture, the session-cache seeder, the hook
subprocess env, and the two-hook turn runner.

WHY ONE OWNER. `tests/test_debug_playbook_injection.py` and
`tests/test_inv3_research_doctrine.py` each carried a near-copy of `_seed_cache`
and `_run_hook_events`. ONE routing change broke both copies at once and neither
was noticed for a month (`c47d7bf`, 2026-08-07, when a turn's `rag_query` rows
moved from a synchronous per-hook write to buffer-then-drain; zero commits
touched either module after it), because a skip on an unreachable shared address
hid the reds. `hook_env` below is the sharper half of the argument: two copies of
that dict are two chances to drop `WRIT_SOCKET` or `WRIT_CACHE_DIR`, and either
omission produces a green-looking run against the wrong daemon.

A TURN IS TWO SUBPROCESSES. Production registers `writ-rag-inject.sh` on
UserPromptSubmit (`hooks.json:47`) and `friction-logger.sh` on Stop
(`hooks.json:89`), and the Stop hook is the ONLY drain on this path: the
UserPromptSubmit hook appends the complete `rag_query` entry to a raw delimited
buffer under `WRIT_CACHE_DIR` (`bin/lib/common.sh::writ_friction_buffer_append`)
and does nothing else with it. Turning that buffer into JSON lines on the
`metrics` stream happens only in `writ_event_buffer_flush`, whose three call
sites are Stop, SessionEnd and SubagentStop. So both hooks run here, and both
are the REAL callers: invoking `bin/lib/writ-flush-events.py` directly would
MODEL the trigger, which is precisely the link that broke, and would have read
green for the whole month the routing was broken. Running the real Stop hook
also covers one property for free: the drain is registered through
`writ_on_exit` (`friction-logger.sh:69`) BEFORE the mode gate at line 76, so it
fires even on the early `exit 0` an empty mode takes.

MODULE TOP LEVEL IS STDLIB ONLY (plus pytest, which the fixture needs), the
discipline `tests/_daemon.py`, `tests/_audit_stream.py` and
`tests/_daemon_leak.py` state for themselves: `writ.shared.logging` is imported
inside `run_prompt_turn`, so importing this module pulls in no `writ.*` tree.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._daemon import start_isolated_daemon, stop_isolated_daemon

_REPO_ROOT = Path(__file__).resolve().parent.parent

HOOK_PROMPT = str(_REPO_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh")
HOOK_STOP = str(_REPO_ROOT / "hooks" / "scripts" / "friction-logger.sh")

# The prompt hook pays a real retrieval against a real graph and the Stop hook spawns
# the drain, so the ceiling is generous. Its purpose is that a wedged hook fails one
# test instead of hanging the run.
HOOK_TIMEOUT = 60

# THE POPULATION THE TWO DIRECT-`/query` RETRIEVAL TESTS SELECT FROM, which is why this
# is the precondition rather than a total node count. Both of them POST
# `node_types=["Playbook","Technique"], domain="process"`, so their query can only
# return a hit when THIS population is non-empty, and non-empty is the whole
# discrimination: the measured failure was the population at ZERO, which is a different
# fact from a node that exists and did not rank. A total is a fact about today's dump
# (468 nodes on 2026-09-10) and would decay into a magic number.
#
# MEASURED on the isolated instance before this predicate was written, rather than read
# off the schema: `domain` is a plain lowercase string property on these nodes, 26 of the
# 29 Playbook|Technique nodes carry `'process'`, and both PBK-PROC-DEBUG-001 and
# PBK-PROC-RESEARCH-001 are inside it. `toLower(coalesce(...))` so a casing change cannot
# make this read 0 against a healthy graph and refuse a correct run.
_CORPUS_PRECONDITION_QUERY = (
    "MATCH (n) WHERE (n:Playbook OR n:Technique) "
    "AND toLower(coalesce(n.domain, '')) = 'process' RETURN count(n)"
)


def _require_retrievable_corpus() -> int:
    """Make the corpus a CHECKED precondition of starting the daemon, replaying it
    only when it is absent. Returns the count it verified.

    ORDER IS LOAD-BEARING: this runs BEFORE `start_isolated_daemon`, because the
    daemon builds its indexes AT STARTUP, so a replay afterwards is invisible to
    the process that needed it.

    THE SUITE PROPERTY THIS DEFENDS AGAINST, which is PRE-EXISTING and not
    introduced here. `tests/conftest.py`'s corpus rebuild is a SESSION-START
    operation (`_preflight_isolated_graph` wipes and replays once, printing
    "corpus rebuilt"), while three modules leave the isolated instance at ZERO
    nodes and nothing restores it until session end: `tests/test_ingest.py`,
    `tests/test_infrastructure.py` and `tests/test_integrity.py`, each measured
    reproducing this on its own. Any module-scoped daemon started after one of
    them indexes an empty graph. MEASURED: in one pytest invocation,
    `tests/test_ingest.py` followed by `tests/test_inv3_research_doctrine.py`
    failed with `got []` -- zero rules, not a ranking miss -- while
    `tests/test_debug_playbook_injection.py` escaped only because it sorts
    alphabetically before those three.

    REPLAY ONLY WHEN ABSENT: an unconditional replay would charge every module
    that takes this fixture the ~1.2s the rebuild costs, for nothing.

    IT FAILS RATHER THAN SKIPS, which is `tests/_graph.py::isolation_refusal_message`'s
    stated posture for the same condition one scope up: a skip is cheap to produce
    and indistinguishable from a green run, and an empty graph reading as a skip has
    masked a real regression in this repo more than once. The count travels in the
    message because the entire purpose is that a future empty graph reports the number
    it saw instead of surfacing as an empty-list assertion three layers away.
    """
    from tests import _graph

    present = _graph.count(_CORPUS_PRECONDITION_QUERY)
    if present:
        return present
    _graph.replay_dump()
    present = _graph.count(_CORPUS_PRECONDITION_QUERY)
    if not present:
        pytest.fail(
            f"corpus precondition unmet: the isolated graph holds {present} "
            f"Playbook/Technique node(s) with domain='process' even after a "
            f"`writ import-cypher` replay, and {_graph.count('MATCH (n) RETURN count(n)')} "
            f"nodes in total. A daemon started now would index an empty corpus, so every "
            f"retrieval assertion here would fail as an empty RESULT instead of as a "
            f"missing PRECONDITION. Restore with tests/_graph.py::replay_dump() or any "
            f"pytest session (its session-start preflight rebuilds).",
            pytrace=False,
        )
    return present


@pytest.fixture(scope="module")
def isolated_prompt_daemon(tmp_path_factory):
    """One daemon this MODULE owns, on an OS-assigned free port.

    ALL of these tests need a daemon PROCESS: the turn is a bash hook subprocess
    that curls over HTTP, and no in-process ASGI transport can serve a
    subprocess.

    MODULE SCOPE IS MANDATORY, not a preference. `tests/conftest.py:136`'s
    `_daemon_leak_guard` is module-scoped and autouse and fails any module that
    leaves a live daemon behind, excluding only the suite port, so a
    session-scoped daemon would be reported as a leak at the first module
    boundary it survived. The autouse guard is set up first, so this fixture
    finalizes before the boundary snapshot is taken.

    The port is never named here: `start_isolated_daemon` takes one from
    `tests/fixtures/net.py::free_port`. An OS-assigned port is out of reach of
    `conftest.py`'s `pytest_sessionfinish` (which stops the daemon answering on
    `WRIT_PORT`) and out of the leak guard's suite-port exclusion, which is why
    this fixture stops its own daemon and asserts the verdict.

    The socket path is `<basetemp>/pturn<n>/writ.sock`, MEASURED at 59 bytes on
    this machine (four-digit pytest counter, fourteen-character user name)
    against `writ/config.py::MAX_SOCKET_PATH` = 107, so 48 bytes of headroom.
    That figure is a function of TMPDIR and the user name, which is why an
    over-cap path is a STATED start-verdict reason rather than a silent None.

    `tcp_readonly` is left FALSE. Nothing on this path uses
    `writ_daemon_client`, so refusing TCP buys no isolation here (`WRIT_SOCKET`,
    `WRIT_HOST` and `WRIT_PORT` all name this daemon, so the exit-7 TCP retry can
    only reach it), and turning it on would make `/prompt-bundle` return 403 on
    that retry, since `writ/server/transport.py:79` allowlists only `/query`.
    Isolation is proven POSITIVELY instead, by the `queries` counter this daemon
    writes into the cache dir its own `/health` reports.

    The corpus is checked BEFORE the start, never after: see
    `_require_retrievable_corpus`, whose whole argument is that this daemon
    indexes at startup.
    """
    _require_retrievable_corpus()
    tmp_dir = tmp_path_factory.mktemp("pturn")
    daemon = start_isolated_daemon(
        log_root=str(tmp_dir / "logs"),
        socket_path=str(tmp_dir / "writ.sock"),
        cache_dir=str(tmp_dir / "cache"),
        tcp_readonly=False,
    )
    if not daemon.get("started"):
        pytest.skip(daemon["reason"])
    yield daemon
    verdict = stop_isolated_daemon(daemon)
    assert verdict["stopped"], (
        f"isolated_prompt_daemon teardown could not stop the daemon on port "
        f"{verdict.get('port')}: pids {verdict.get('pids')}, surviving pids "
        f"{verdict.get('survivors')}, table measured "
        f"{verdict.get('pids_measured')}, health {verdict.get('health')!r}"
    )


def seed_session_cache(
    cache_dir: str, sid: str, mode: str, *, current_phase: str | None = None,
) -> str:
    """Seed a non-orchestrator session cache in the DAEMON's own cache dir.

    `/prompt-bundle` reads the session cache SERVER-SIDE
    (`writ/server/routes/query.py:228`, `_read_cache(sid)`), so the seed must
    land in the dir that daemon resolved. Callers therefore pass
    `daemon["health"]["cache_dir"]`, which is what the SERVER PROCESS reports,
    rather than what the fixture asked for.

    The union of the two `_seed_cache` copies this replaces, which differed in
    exactly one field: the debug module set `current_phase` to "implementation"
    for work mode and the inv3 module always wrote None.
    """
    path = os.path.join(cache_dir, f"writ-session-{sid}.json")
    with open(path, "w") as handle:
        json.dump(
            {
                "mode": mode,
                "is_orchestrator": False,
                "is_subagent": False,
                "current_phase": current_phase,
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


def hook_env(daemon: dict) -> dict:
    """The subprocess env for BOTH hook runs of a turn, built ONCE so the two
    cannot drift. Every entry closes a measured trap.

    - `WRIT_SOCKET` is the arm that actually WINS. `bin/lib/common.sh:1817` takes
      the socket whenever `WRIT_SOCKET` is set and that socket exists, AHEAD of
      `WRIT_HOST`/`WRIT_PORT`, so setting it is what makes the transport
      deterministic instead of dependent on whether the operator's default
      socket happens to exist.
    - `WRIT_HOST` and `WRIT_PORT` are not redundant beside it. The hook builds
      its URL from them (`writ-rag-inject.sh:565`), and `_writ_transport_for`
      (`common.sh:227-236`) applies the socket flag ONLY to a URL whose port is
      `WRIT_SESSION_PORT`, so both spellings must name the same daemon or the
      socket is silently not used. They also make the exit-7 TCP retry
      (`common.sh:253-256`) land on this same isolated daemon.
    - `WRIT_CACHE_DIR` is taken from what the SERVER PROCESS reports, and must
      be IDENTICAL for the two runs: `writ_event_buffer_flush` spawns the drain
      only when `[ -s "$buf" ]`, and `writ_event_buffer_path` is under this dir,
      so a Stop hook with a different cache dir looks for a buffer that is not
      there and drains nothing, silently.
    - `WRIT_FRICTION_LOG` is POPPED, not merely assumed absent. It is
      `emit_destination`'s collapse branch: surviving into the child, it puts
      EVERY stream in one file and no `metrics` row exists to read. Both calling
      modules carry `no_friction_isolation`, which makes
      `tests/conftest.py:110-113` delete it, and popping it here means a stray
      ambient value cannot undo that either.
    - `WRIT_NO_AUTOSTART` is set explicitly even though conftest forces it at
      import. Without it, `writ-rag-inject.sh:48-79` launches a REAL daemon on
      whatever port it was handed when its health check fails; that daemon
      outlives the test and fails the module on the leak guard. Setting it here
      is how this leak class stays closed rather than inherited.

    `WRIT_LOG_ROOT` is deliberately INHERITED (`tests/conftest.py:109` points it
    at the per-test `tmp_path/logs`), because it is both the drain's destination
    and the test's read side, and this env is built at CALL time so the
    function-scoped monkeypatch has already applied.
    """
    env = dict(os.environ)
    env.pop("WRIT_FRICTION_LOG", None)
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


def _queries_count(cache_dir: str, sid: str) -> int:
    """The `queries` counter read out of the session file in the daemon's own
    cache dir.

    Only the SERVER PROCESS writes it (`writ/server/routes/query.py:318-324` and
    `366-369`, `cmd_update --inc-queries`), and no other daemon on this machine
    writes into a throwaway cache dir, so a DELTA across one turn positively
    identifies which daemon served that turn. Absent or unreadable answers 0
    rather than raising, because this number is triage rung 1 of a FAILURE
    message: a read that raised here would replace the diagnosis with its own
    traceback.
    """
    path = Path(cache_dir) / f"writ-session-{sid}.json"
    try:
        return int(json.loads(path.read_text())["queries"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def run_prompt_turn(
    daemon: dict, tmp_path, sid: str, prompt: str,
) -> tuple[list[dict], str, int]:
    """One production-shaped turn: the real UserPromptSubmit hook, then the real
    Stop hook, then the drained `metrics` rows.

    Returns `(events, stdout, queries_delta)`, the three facts a triager reads
    off one failure message instead of re-running: `events` empty with a ZERO
    delta is transport (the turn never reached this daemon), `events` empty with
    a POSITIVE delta is the drain, and one source missing from a NON-EMPTY
    `events` is retrieval. A drain problem cannot be selective (it takes the
    whole list), so those two symptoms do not overlap.

    ONE cwd LOCAL AND ONE env DICT, passed to both runs, is what closes the cwd
    trap by construction. `bin/lib/writ-flush-events.py` calls `emit(None, ...)`,
    and with `project=None` `emit_destination`
    (`writ/shared/logging.py:692-697`) resolves the scope from
    `resolve_project(_REQUEST_PROJECT_ROOT.get())`, which in the flush process is
    `resolve_project(None)`, which is `os.getcwd()` AT FLUSH TIME; it never calls
    `request_project_scope`. A Stop hook launched from a different cwd would
    therefore drain the row under a different project than the
    `read_streams(resolve_project(...))` below reads.
    """
    from writ.shared.logging import read_streams, resolve_project

    project_root = tmp_path / "proj"
    project_root.mkdir(exist_ok=True)
    (project_root / ".git").mkdir(exist_ok=True)  # marker for router project scope
    cwd = str(project_root)
    env = hook_env(daemon)
    cache_dir = daemon["health"]["cache_dir"]

    before = _queries_count(cache_dir, sid)
    prompt_run = subprocess.run(
        ["bash", HOOK_PROMPT],
        input=json.dumps({"session_id": sid, "prompt": prompt}),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=HOOK_TIMEOUT,
    )
    subprocess.run(
        ["bash", HOOK_STOP],
        input=json.dumps({"session_id": sid}),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=HOOK_TIMEOUT,
    )
    after = _queries_count(cache_dir, sid)
    events = read_streams(resolve_project(cwd), ["metrics"])
    return events, prompt_run.stdout, after - before
