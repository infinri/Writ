"""The module-boundary daemon leak guard: the pure half.

WHY THIS EXISTS. Two suite defects leaked LIVE daemons onto the operator's machine and
neither was visible from inside the run. A hook auto-started a daemon on a throwaway port
and it squatted that port for 3h21m, answering `{"mode": ""}` for every session it was
asked about; and `stop_isolated_daemon` matched ONE spelling of the launch command, so a
stop that missed left the daemon up and reported nothing. Both were found by reading `ps`
by hand, not by a red test.

So this guard reads `ps` too, and it is deliberately BLIND TO WHO LAUNCHED a daemon. The
launcher of the daemon the diagnosis found on port 19998 was never established (its
`cache_dir` was `/tmp/tmp.2AgLz4jWBt/c`, which is not a pytest `tmp_path` shape, and this
tree contains no `mktemp -d`), so a detector keyed on a known launcher would have missed
the exact process that motivated it. It asks one question: did a Writ daemon appear during
a module and survive that module's end.

EVERY DECIDING FUNCTION HERE IS PURE, and that split is what makes the guard provable with
no daemon in play. `snapshot_text` runs `ps` and `health_of` performs one read-only GET;
everything that classifies takes snapshot TEXT or already-parsed dicts and returns data,
so `tests/test_daemon_leak_guard.py` drives every branch from literal fixtures.

`suite_port` MIRRORS `tests/_daemon.py::_port` rather than importing it, so the two stay
independently testable: this module is the leaf the daemon helpers depend on, never the
other way round.

MODULE TOP LEVEL IS STDLIB ONLY, the discipline `tests/_graph.py` states for itself:
`tests/conftest.py` reaches this module at every module boundary in all four suite chunks,
so a `writ.*` import here would be paid by every one of them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

# How long a reported survivor is re-checked before the boundary fails, and how often.
# MEASURED: a correctly stopped daemon leaves the process table 0.05 to 0.10 seconds
# after its /health goes silent (three runs, idle machine), and two seconds is about
# twenty times that. The leak this guard exists for lasted 3h21m, so the window costs
# nothing against a real one and only ever elapses on a hit.
#
# A MITIGATION, NOT A GUARANTEE, and the margin is three quiet samples. A slower reap
# under real load can still false-positive, more rarely, and it fails the boundary
# loudly when it does, so the cost is flakiness rather than a missed leak. Before this
# number is treated as load-bearing it needs two things neither of which is shipped: a
# test that delays a probe's reap PAST the window, so the middle of the range is
# observed and not only its ends, and a smoke run under load or parallelism for a
# distribution of reap times. See docs/adr/ADR-daemon-leak-guard.md.
_REAP_GRACE = 2.0
_REAP_POLL = 0.1

# `args=` and not `command=`: the empty header suppresses the header line, and `args`
# gives the FULL command line, which is the only place the port appears.
#
# `-ww` IS LOAD-BEARING, and it was found by measurement, not by reasoning. procps
# truncates the args column to `COLUMNS - 1` when COLUMNS is set, and something in the
# pytest process sets it to 80 at the C level (`os.environ.get("COLUMNS")` reads None, so
# Python never saw it: GNU readline calls setenv on init when there is no tty). Measured
# from inside a real run: `ps -eo pid=,args=` returned 697 lines with the widest at 79
# characters, so EVERY daemon's line lost its `serve --port N` tail behind an absolute
# venv path and the snapshot reported zero candidates against a table holding four live
# daemons. `-ww` asks for unlimited width in argv instead of depending on the ambient
# environment: same table, widest line 17764. A guard whose blindness looks exactly like
# a clean result is the one failure mode this module must not have.
_PS_ARGV = ("ps", "-eww", "-o", "pid=,args=")

# The one literal both recognizers are built from. `scripts/lib/writ-server-lib.sh:96,98`
# appends `--host $WRIT_HOST` in BOTH launch arms, so `serve --port N --host` is the
# substring every observed spelling shares: the venv console script, the
# `<python> -m writ.cli` shim, and the `~/.cache/writ/.venv` console script. The old
# `writ serve --port N` pattern matched only the first, which is defect D2.
_SERVE = "serve --port"

# The port-DISCOVERING form, for a snapshot where the ports are not known in advance.
# `--host` is deliberately NOT required here: a kill pattern must not over-match, but a
# leak detector must not miss a spelling, and those pull in opposite directions.
_CANDIDATE = re.compile(re.escape(_SERVE) + r"\s+(?P<port>\d+)")

# A candidate needs a `writ` token too, or a developer's `esbuild serve --port 3000`
# started mid-suite reads as a leaked Writ daemon.
_WRIT_TOKEN = "writ"

# How much of this process's own command-line TAIL a live snapshot must show intact. Any
# length detects a cut, because procps truncates the END of the line; 24 characters is
# chosen to keep the refusal message readable.
_SELF_TAIL_CHARS = 24

# The four /health fields the diagnosis used to identify the two leaked daemons: where the
# survivor writes, what session cache it answers from, and how long it has been up.
_HEALTH_FIELDS = ("audit_log", "friction_log", "cache_dir", "startup_time")

NO_HEALTH_ANSWER = "no /health answer"


class UnmeasurableSnapshot(Exception):
    """A snapshot that could not be taken, kept distinct from one that found nothing.

    A guard that reads green because it could not look is the failure mode this repo has
    already paid for, so "measured, zero candidates" and "unmeasured" are different
    outcomes with different behaviour, not one code path answering to two names.
    """


def _serve_pattern(port: int | str) -> str:
    """The substring every observed launch spelling of `port` shares.

    Used for the pkill pattern and the pid lookup in `tests/_daemon.py`, which is why it
    embeds the exact port: an isolated daemon's port comes from
    `tests.fixtures.net.free_port()`, and both protected ports (the operator's 8765 and
    the suite's 8799) have listeners when that call is made, so the OS cannot hand either
    one out.
    """
    return f"{_SERVE} {port} --host"


def suite_port() -> int:
    """The suite's own daemon port, resolved LIVE from WRIT_PORT (default 8765).

    A function, not a module constant, for the reason `tests/_daemon.py::_port` gives: a
    constant would freeze 8765 at import time, and a suite invoked under a different
    WRIT_PORT would then report its OWN daemon as a leak.
    """
    return int(os.environ.get("WRIT_PORT", "8765"))


def snapshot_text() -> str | None:
    """The process table as text, or None when it could not be read.

    None is the UNMEASURED input to `parse_snapshot`: `ps` missing, `ps` failing, or `ps`
    returning nothing at all. It is never an empty success.
    """
    try:
        done = subprocess.run(
            list(_PS_ARGV), capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


def _own_cmdline() -> str:
    """This process's own command line, joined exactly as `ps -o args=` renders it.

    MEASURED, not assumed: the `ps` line for `os.getpid()` and this join of
    `/proc/self/cmdline` compare byte for byte on this machine (140 characters, equal),
    which is what lets the check below use a plain suffix comparison. Empty string when
    `/proc` cannot be read, and the caller turns that into a refusal rather than skipping
    the check.
    """
    try:
        with open("/proc/self/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


def _self_visibility_reason(processes: dict[int, str]) -> str | None:
    """Why this snapshot cannot be certified complete, or None when it can.

    THE POSITIVE SIGNAL, and the reason it exists is a defect this cycle shipped and then
    found: a snapshot can hold hundreds of lines and still be blind, because procps
    truncates the `args` column to `COLUMNS - 1` and something in the pytest process sets
    COLUMNS=80 at the C level where `os.environ` never sees it. Measured from a real run
    before `-ww` was added: 697 lines, widest 79 characters, so every daemon's
    `serve --port N` tail sat past the cut and the snapshot reported ZERO candidates
    against a table holding four live daemons. An emptiness check cannot see that, because
    nothing is empty.

    So the snapshot is asked to prove it can see a line it MUST contain: this process's
    own, whole. Our own pid is in every table we take, and our own command line is known
    exactly, so a cut tail is detectable without trusting any argv spelling. Comparing the
    TAIL is what makes it a truncation detector: procps removes the final characters, so
    any cut at all breaks the suffix.

    KNOWN LIMIT, stated because it bounds the guard rather than defeating it: the check
    can only see a cut that reaches OUR line, so it is blind when this process's own
    command line is shorter than the truncation width. Under the documented invocation
    (`.venv/bin/python -m pytest <paths>`) it is about 105 characters against a 79
    character cut, so the live case this cycle hit is covered.
    """
    own = _own_cmdline()
    if not own:
        return (
            "this process's own command line could not be read from /proc/self/cmdline, "
            "so the snapshot cannot be certified against truncation. A snapshot that "
            "cannot be certified is not a measurement."
        )
    pid = os.getpid()
    line = processes.get(pid)
    if line is None:
        return (
            f"the snapshot does not contain this process (pid {pid}), which every table "
            f"we take must hold, so it is not a view of this machine's process table."
        )
    tail = own[-_SELF_TAIL_CHARS:]
    if not line.endswith(tail):
        return (
            f"the snapshot's line for this process (pid {pid}) is TRUNCATED: it ends "
            f"{line[-_SELF_TAIL_CHARS:]!r} where this process's command line ends "
            f"{tail!r}. procps cuts the args column to COLUMNS-1, which drops the "
            f"`{_SERVE} N` tail of every daemon line and makes a full table read as zero "
            f"candidates. `-ww` in the snapshot argv is what prevents it."
        )
    return None


def parse_snapshot(ps_output: str | None, *, self_check: bool = False) -> dict:
    """`{"measured": bool, "processes": {pid: cmdline}, "reason": str | None}` from
    `ps -eww -o pid=,args=` text.

    Every process is kept, not only the Writ ones: the same snapshot is the BASELINE for
    the next boundary, and "this pid was already here" has to be answerable for any pid.

    `self_check` DEFAULTS OFF because this function's other job is to turn literal
    fixtures into snapshots, and a literal fixture does not contain the process reading
    it. Every LIVE read goes through `live_snapshot` below, which turns it on, so no
    caller has to remember: there is one live entry point and it always checks.
    """
    if not ps_output or not ps_output.strip():
        return {
            "measured": False,
            "processes": {},
            "reason": (
                f"`{' '.join(_PS_ARGV)}` could not be run or returned nothing at all"
            ),
        }
    processes: dict[int, str] = {}
    for line in ps_output.splitlines():
        pid, _sep, cmdline = line.strip().partition(" ")
        if not pid.isdigit() or not cmdline.strip():
            continue
        processes[int(pid)] = cmdline.strip()
    reason = _self_visibility_reason(processes) if self_check else None
    return {"measured": reason is None, "processes": processes, "reason": reason}


def live_snapshot() -> dict:
    """The one live snapshot entry point: read the table AND certify it.

    Both live callers (the module-boundary guard in `tests/conftest.py` and
    `tests/_daemon.py::_pids_serving_port`) come through here, so the self-check cannot be
    lost by a caller that forgets a keyword.
    """
    return parse_snapshot(snapshot_text(), self_check=True)


def find_survivors(baseline: dict, current: dict, *, suite_port: int) -> list[dict]:
    """The Writ daemons in `current` that are not in `baseline`, one dict each.

    Two exclusions, and neither is a name list. The operator's long-lived daemon needs no
    entry at all: it is running before the first module boundary, so it is in the baseline
    and can never be a survivor, which is structural. The suite's own daemon is excluded
    by PORT, and the caller resolves that port live from the variable the suite itself
    sets.

    The port is part of a candidate's IDENTITY rather than a field read afterwards, so a
    matching process can never be dropped for having no port to report.
    """
    for which, snapshot in (("baseline", baseline), ("current", current)):
        if not snapshot.get("measured"):
            raise UnmeasurableSnapshot(
                f"the {which} process snapshot is UNMEASURED, so no leaked daemon can be "
                f"ruled out from it. This is a refusal, not a clean boundary. Reason: "
                f"{snapshot.get('reason') or 'not recorded'}"
            )
    already_running = set(baseline["processes"])
    survivors: list[dict] = []
    for pid, cmdline in sorted(current["processes"].items()):
        if pid in already_running or _WRIT_TOKEN not in cmdline:
            continue
        match = _CANDIDATE.search(cmdline)
        if match is None:
            continue
        port = int(match.group("port"))
        if port == suite_port:
            continue
        survivors.append({"pid": pid, "port": port, "cmdline": cmdline})
    return survivors


def confirm_survivors(survivors: list[dict], *, grace: float = _REAP_GRACE) -> list[dict]:
    """The survivors still in the table after the measured reap window.

    A SECOND MEASUREMENT, not an exemption, and it exists because the first one was
    wrong once. A daemon the suite stops correctly goes silent on /health 0.05 to 0.10
    seconds BEFORE its process leaves the process table (three runs, idle machine), so a
    boundary snapshot taken the instant a teardown returns can catch a process that is
    already exiting. During a 2056-test run it did exactly that and reported a working
    teardown as a leak. A guard that fails a correct teardown is worse than no guard.

    The definition of a leak is unchanged (a Writ daemon that is still here), and nothing
    is excluded by name: this re-asks the same question after `grace` seconds, matching
    pid AND cmdline so a reused pid cannot pass for the original. The leak this cycle
    exists for lasted 3h21m, so a process that is gone within two seconds was never the
    thing being missed. `grace` is a keyword so both branches can be pinned quickly.
    """
    deadline = time.monotonic() + grace
    remaining = list(survivors)
    while remaining and time.monotonic() < deadline:
        time.sleep(_REAP_POLL)
        recheck = live_snapshot()
        # An uncertifiable re-read drops nothing. A cut table loses the tail of every long
        # cmdline, so filtering on one would DISCARD a real survivor for being invisible,
        # which is the direction this module refuses to fail in.
        if not recheck["measured"]:
            return remaining
        remaining = [
            s for s in remaining if recheck["processes"].get(s["pid"]) == s["cmdline"]
        ]
    return remaining


def health_of(port: int, timeout: float = 2.0) -> dict | None:
    """A survivor's own /health payload, or None when nothing answers.

    The one live call in this module, and it is a read: the guard REPORTS a leaked daemon
    and never signals it, because a process the suite did not start is the operator's to
    stop. An absent reply degrades to None and `format_report` names the absence.
    """
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout) as reply:
            return json.load(reply)
    except Exception:  # noqa: BLE001
        return None


def format_report(survivor: dict, health: dict | None) -> str:
    """One survivor as the evidence shape that identified the two leaked daemons: pid,
    port, full cmdline, and where that daemon writes.

    A missing /health reply is stated, never rendered as a clean line: a survivor whose
    destinations are unknown is worse news than one whose destinations are known.
    """
    if health is None:
        destinations = NO_HEALTH_ANSWER
    else:
        destinations = ", ".join(f"{field}={health.get(field)}" for field in _HEALTH_FIELDS)
    return (
        f"pid {survivor['pid']} still answers on port {survivor['port']} "
        f"({destinations}): {survivor['cmdline']}"
    )
