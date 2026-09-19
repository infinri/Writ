# ADR: A leaked daemon is caught by the process table, not by a source detector

Status: accepted (daemon leak guard cycle, 2026-09-09).

## Context

Two test-suite defects put LIVE Writ daemons on the operator's machine and kept them
there. Both were found by reading `ps` by hand while diagnosing something else, and
neither produced a single red test in the runs that caused them.

**D1, the autostart.** `hooks/scripts/writ-rag-inject.sh` starts a daemon when its health
probe fails, unless `WRIT_NO_AUTOSTART` is set. Two tests in
`tests/test_daemon_client_coverage.py` pointed that hook at `WRIT_PORT: "19998"` without
stating the guard. A daemon came up on 19998 and stayed up for 3h21m. Its own `/health`
reported `cache_dir` as `/tmp/tmp.2AgLz4jWBt/c` and `audit_log` as the PRODUCTION
`<skill>/var/logs/github.com/infinri/Writ/audit.jsonl`, so any gate decision it served
landed in the operator's real audit trail. It also squatted the port
`tests/test_write_path_process_budget.py` used as its dead port, which made the
daemon-down branch measure a live daemon and report a plausible wrong answer.

One fact that changes what any fix here can claim: the launcher of that specific process
was never established. `/tmp/tmp.2AgLz4jWBt/c` is not a pytest `tmp_path` shape, and this
tree contains no `mktemp -d`. So the attribution to any known launch site is an
assumption, not a measurement.

**D2, the teardown that could not miss loudly.** `tests/_daemon.py` matched
`writ serve --port {port}`. `scripts/lib/writ-server-lib.sh:93-99` builds the launch two
ways: `$VENV_DIR/bin/writ serve --port N --host H` when that console script exists, else
bare `writ serve ...` resolved through PATH, which on this machine reaches a shim that
execs `<python> -m writ.cli`. The literal words "writ serve" never appear in the second
spelling, so the pkill pattern could not match it. Both spellings were live at diagnosis
time: pid 52806 (port 19998) matched, pid 352366 (port 59015) did not, and 352366's
throwaway socket had already been unlinked, which is `stop_isolated_daemon` having run to
completion and left the daemon up. The function then discarded its own second
"is it down" result, so a stop that missed reported nothing at all.

## Decision

### 1. The guard is a runtime process-table chain, one snapshot per module boundary

`tests/conftest.py` gains a session-scoped snapshot and a module-scoped autouse fixture.
Each module boundary reads `ps -eww -o pid=,args=`, compares against the previous
boundary's snapshot, reports survivors, and then becomes the next baseline. The deciding
half is
`tests/_daemon_leak.py`, which takes snapshot TEXT and returns data, so every branch is
proven from literal fixtures with no daemon in play.

The property is deliberately weak and deliberately blind: did a Writ daemon appear during
a module and survive that module's end. It asks nothing about who started it. Given that
D1's launcher was never established, a guard that recognizes launch sites would have
missed the exact process that motivated the cycle.

Chaining per module rather than snapshotting once around the whole session is what makes
the report useful. A leak is attributed to the module that caused it instead of to a chunk
of roughly two hundred, and the end of the session falls out of the same chain as the last
module boundary. Cost is one `ps` per module instead of two.

Both exclusions are derived, and neither is a name:

- The operator's long-lived daemon needs no exclusion at all. It is running before the
  first module boundary, so it is in the baseline and can never be a survivor. That is
  structural, and it holds for everything else the machine happens to be running.
- The suite's own daemon is excluded by PORT, resolved live from `WRIT_PORT` the same way
  `tests/_daemon.py::_port` resolves it. Hardcoding 8799 would make a suite invoked under
  a different port report its OWN daemon as a leak.
- A process is a candidate only when its command line names `serve --port <digits>` AND
  carries a `writ` token, so a developer's `esbuild serve --port 3000` started mid-suite
  is not reported.

An UNMEASURABLE snapshot (no `ps`, or `ps` returning nothing at all) is recorded as
unmeasured and refuses rather than reading clean. A guard that goes green because it could
not look is the failure mode this repo has already paid for, and "measured, zero
candidates" and "could not measure" are separate outcomes with separate behaviour.

The guard REPORTS and never signals. It names pid, port, the full command line, and that
daemon's own `/health` destinations (`audit_log`, `friction_log`, `cache_dir`,
`startup_time`), or an explicit "no /health answer" when nothing replies. That is the same
evidence shape that identified the two leaked daemons. Stopping a process the suite did
not prove it started is an operator action, so nothing in this cycle kills one.

Residual, stated rather than dropped: a leak ON the suite port is excluded by the port
rule, and `stop_test_daemon` runs in `pytest_sessionfinish`, after every fixture teardown.
Making that loud would mean raising out of `pytest_sessionfinish`, which
`tests/conftest.py` deliberately avoids because it flips exitstatus and masks the test
results. One `curl localhost:$WRIT_PORT/health` answers it by hand.

### 2. Identity is pattern plus pid, and `/health` gains nothing

The match pattern becomes the substring every observed spelling shares, built in ONE place
as `_serve_pattern(port)`, giving `serve --port N --host`. `writ-server-lib.sh` appends
`--host $WRIT_HOST` in both launch arms, so all three spellings on this machine contain
it: the venv console script, the `<python> -m writ.cli` shim, and the
`~/.cache/writ/.venv` console script. The same pattern is used for the pkill and for the
pid lookup, so a stop and its verification cannot disagree about what a daemon is.

After a successful start, `start_isolated_daemon` reads the pids matching that pattern out
of the process table and returns them. This answers "capture the pid at start" without
touching `writ_ensure_server`, which backgrounds the daemon under `nohup` inside a flock
subshell and does not hand the pid back. `stop_isolated_daemon` signals by port, verifies
with `/health`, and returns a verdict rather than None.

Collision risk, stated because the pattern embeds the exact port: an isolated daemon's
port comes from `tests.fixtures.net.free_port()`, which asks the OS for an unused port,
and both protected ports (the operator's 8765 and the suite's 8799) have listeners at that
moment, so the OS cannot hand either one out. The test proves the pattern rather than the
argument: for a port P it matches all three spellings of P and matches no spelling naming
8765 or 8799.

### 3. `stop_isolated_daemon` returns a verdict and never raises

The verdict is `{"port", "pids", "stopped", "survivors", "health"}`, and `stopped` is true
only after nothing answers `/health` on that port. The reason it must not raise is in that
function's own docstring: it unlinks the throwaway socket LAST, and a socket outliving its
listener is exactly the stale-but-present file `writ serve` unlinks and takes over. An
exception thrown mid-teardown would skip that unlink and convert one defect (a daemon that
would not stop) into two. So the teardown completes on every path, `stopped: False`
included, and the owning fixture asserts the verdict afterwards.
`tests/test_advance_phase_token_gate.py::own_daemon` is the only live caller and does
exactly that: the failure surfaces as a teardown error on that module, with the port and
the surviving pids named, individual test results intact, exit code non-zero.

## Alternatives considered

- **A source detector over test modules**, asserting that any test which runs an
  autostart-capable hook with a non-suite `WRIT_PORT` sets `WRIT_NO_AUTOSTART` in the same
  env. Rejected on measurement. `tests/conftest.py` already sets that variable for every
  subprocess at conftest import, so the modules that omit the literal are not leaking, and
  there are roughly thirty of them (about forty `WRIT_PORT` sites across `tests/`, most
  inheriting the guard). The detector is red on arrival with no defect behind it, and the
  only ways to green it are an allowlist or thirty mechanical edits. It would also have to
  pair a `subprocess.run` target with the env-dict expression feeding it, statically,
  across every test module: a large fragile detector for a property the runtime guard
  decides exactly. What was kept from the idea is the derived-population discipline,
  pointed at the production side where the population is small and the property is real:
  `tests/_inventory.py::daemon_starting_hooks()` maps every hook under `hooks/scripts/`
  that can reach a launch to whether that launch consults the guard. Verified population
  today is two, one of which was unguarded, so the check was non-vacuous on arrival.
- **Add `pid` to the `/health` payload** and have the teardown read it back. Rejected: it
  changes a production route (`writ/server/routes/query.py`, which reports no pid today)
  for a test-teardown need, and the operator's long-lived daemon comes from an older build
  that would not report it, so the process-table path is needed anyway. Two mechanisms
  where one suffices, and the added one is the one that ships to users.
- **One snapshot pair around the whole session** instead of a per-module chain. Rejected:
  it costs the same order of magnitude and reports "something leaked somewhere in these
  two hundred modules", which is a finding nobody can act on. The chain also gets the
  end-of-session case for free.
- **Have the guard kill what it finds.** Rejected outright. The guard cannot prove the
  suite started the process it is looking at (D1's launcher was never established), and a
  test fixture that signals a process on that basis is a worse defect than the leak. It
  reports; the operator stops.
- **Exclude the operator's 8765 daemon and the suite's 8799 by name.** Rejected: 8765
  needs no rule at all because it is in the baseline, and a name list is the shape that
  goes stale. The port exclusion that remains reads the variable the suite itself sets.
- **Fix the four other modules carrying their own copy of the one-spelling pkill**
  (`test_fix2_cache_alignment.py`, `test_methodology_companion_orchestrator.py`,
  `test_orchestrator_injection.py`, `test_phase3b_approval_rewrap.py`). Deferred, named
  rather than silently left. They have the same coupling, but they all target the suite's
  own port and are followed by an ensure-server restart, so they cannot leak a daemon on a
  throwaway port. The fixed helper in `tests/_daemon.py` is the canonical spelling for a
  follow-up cycle; changing four modules across three chunks now would widen this cycle
  past the leak.
- **Keep the fixed dead-port literal and assert it closed at each call site.** Rejected:
  that is what the module did, and the assertion existed at one of the sites and not at
  the one that failed. A fixed literal is a rendezvous point across modules and across
  runs, which is how a leaked daemon came to occupy it. The port now comes from
  `_dead_port()`, which asks the OS and refuses with the port named if anything answers,
  so every port in that module arrives with its precondition.

## Consequences

- Every module boundary in all four suite chunks pays one `ps` and one dict comparison. On
  a run with no leak the guard is invisible.
- A leaked daemon fails the module that leaked it, once, with the pid, the port, the
  command line and that daemon's own log destinations in the message. The next boundary
  does not repeat it, because the snapshot is stored before the failure is raised.
- A machine with no working `ps` cannot run the suite green. That is the intended
  direction: the alternative is a guard that certifies what it never looked at.
- The snapshot asks for unlimited width in argv (`-ww`) rather than trusting the ambient
  environment, and that is a measurement, not caution. Inside a real pytest process
  `ps -eo pid=,args=` came back with every line truncated at 79 characters, because
  something sets `COLUMNS=80` at the C level where `os.environ` never sees it (GNU
  readline does this on init when there is no tty). Every real daemon's line lost its
  `serve --port N` tail behind an absolute venv path, so the snapshot reported zero
  candidates against a table holding four live daemons. The first synthetic survivor used
  to check the guard had a short command line and was detected anyway, which is how a
  guard passes its own test while being blind in production. The width flag alone would
  leave that defect one edit from returning with every test green, so the snapshot also
  carries a POSITIVE self-check: every live read goes through one entry point that
  requires the table to still show this process's own whole command line, and a table
  that came back full but cut is unmeasured with truncation named. It pins the property,
  not the argv spelling, and it is blind only where the defect itself could not fire
  (when our own command line is shorter than the cut).
- A DYING PROCESS IS NOT A LEAK, and learning that cost one false positive against a
  correct teardown. The boundary snapshot is taken the instant a module's own teardown
  returns, and a daemon that was stopped properly stops answering `/health` 0.05 to 0.10
  seconds BEFORE its process leaves the table (three measured runs, idle machine). In a
  2056-test smoke run the guard reported exactly that process, which was gone by the time
  the run finished. Two changes, one at each end: `_wait_for_isolated_down` now requires
  the process to be gone as well as `/health` to be silent, so a teardown's own verdict
  agrees with what the guard can see, and the guard re-asks the same question for two
  seconds before failing. Neither exempts anything by name, and a real survivor still
  fails (proved by re-running the owned-spoof probe, which pays the two seconds and then
  reports). The window is about twenty times the measured linger, against a leak that
  lasted 3h21m.
- THE TWO SECOND WINDOW IS A PROBABILISTIC MITIGATION, NOT A GUARANTEE, and it is stated
  that way on purpose. It rests on three idle-machine points, so a slower reap under real
  load can still false-positive, more rarely; when it does, the boundary fails the module
  loudly, so the residual cost is CI flakiness rather than masked unsafety, which is the
  direction to fail in. What would turn the margin into durable evidence, and what has to
  exist before it is treated as a load-bearing constant: a test that deliberately delays
  a probe's reap PAST the window, so the behaviour is observed in the middle of the range
  instead of only at its two extremes, and a smoke run under real load or parallelism to
  get a DISTRIBUTION of reap times rather than three quiet samples. Neither is shipped
  here, and until they are, the number is a measured starting point.
- The one production-source change has no end-to-end test, and that is a deliberate
  refusal rather than an omission. `hooks/scripts/session-start-bootstrap.sh` was verified
  three ways short of running it whole: `bash -n`, the existing source contract (it still
  sources `writ-server-lib.sh` and still calls `writ_ensure_server`), and executing the
  guard line extracted from the file itself, which calls the launch with the variable
  unset and does not call it with the variable set. Running the FULL hook from the suite
  is refused because it writes the operator's session pointer (`/tmp/writ-current-session`
  feeds the rotation carry-forward) and, with the variable unset, would reach
  `writ_ensure_server` against the real 8765 daemon, which is precisely the class of side
  effect this cycle exists to remove from the suite. The residual risk is bounded by
  reading: the file uses `set -u` without `-e` and ends in an explicit `exit 0`, so the
  non-zero status of a skipped guard cannot change the hook's exit, and no later step
  consumes anything `writ_ensure_server` produces.
- `stop_isolated_daemon`'s return type changed from None to a dict. The only live caller is
  `own_daemon`, which now asserts it. Four other modules kill by port with their own
  helpers and are untouched.
- The one production-source change in the cycle is a single guarded line in
  `hooks/scripts/session-start-bootstrap.sh`. It is behaviour-neutral where
  `WRIT_NO_AUTOSTART` is unset, which is production, and it keeps that hook's existing
  source contract (it still sources `writ-server-lib.sh` and still calls
  `writ_ensure_server`).
- `/health` is unchanged, so no daemon restart is needed to adopt any of this.
