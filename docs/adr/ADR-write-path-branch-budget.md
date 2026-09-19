# ADR: the write path's per-branch process budget, and the one gated stderr tee

Status: accepted
Date: 2026-09-01
Scope: `hooks/scripts/writ-pre-write-dispatch.sh`, `bin/lib/common.sh`,
`tests/test_write_path_process_budget.py`, `tests/_stub_daemon.py`

This records the decisions a later reader would otherwise re-derive, plus one
measured rejection that looks like an obvious win and is not.

## Context

A single `Write` fires 15 hooks. Almost all of that cost is process startup, and
`tests/test_write_path_process_budget.py` is the ratchet that keeps it from
creeping back. Two problems motivated this cycle.

First, `writ-pre-write-dispatch.sh` paid external processes on every write to do
work bash does with builtins. Its two positional line splits alone were eleven
external commands (one `head`, eight `sed`, one `tail`, one `tr`), plus a
bootstrap `dirname`, plus a `cat` that existed only to drain a pipe nobody
wanted, plus a `tee` that wrote every stderr byte into `/dev/null`. Separately,
`bin/lib/common.sh` ran `mkdir -p` unconditionally before three buffer appends,
one of which runs at exit on every instrumented hook.

Second, the ratchet only ever measured one branch of that hook: the daemon-down
deny. `tests/conftest.py` pins `WRIT_PORT` at a port with nothing listening, so
every hook takes its daemon-down path, and for this hook that path is always a
refusal. The branch real source-code writes actually take, allow with
file-context rules injected, was never counted at all.

## Decision 1: gate the stderr tee's FORK, in this hook only

The line was:

```bash
exec 2> >(tee -a "$(_writ_debug_enabled && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" || echo /dev/null)" >&2)
```

That is unconditional. It gates the tee's DESTINATION between the real log and
the null device, not the fork. With `WRIT_DEBUG` unset it still forked `tee`,
wrote every stderr byte into `/dev/null`, and re-emitted the same bytes to
stderr. Measured before the change: one `tee` execve in BOTH debug states, and 44
total execve attempts in both, so the fork was paid on every write regardless of
the gate. After the change: 29 attempts with debug unset and no `tee`, 30 with
debug set and a `tee`. The fork bought nothing when debug was off, because the
file sink was already the null device.

It is now:

```bash
if _writ_debug_enabled; then
    exec 2> >(tee -a "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" >&2)
fi
```

Alternatives considered, and why they lost:

- Leave it alone for uniformity. Rejected: the cost is real and paid on the
  hottest gate path in the system, and it is not only a process. Routing stderr
  through another process adds a buffering hop that can reorder stderr against
  stdout, so skipping the redirect makes the quiet path more faithful, not less.
- Gate all four hooks that carry the idiom. Rejected as out of scope, not as
  wrong. `writ-dispatch-discipline.sh`, `writ-subagent-start.sh` and
  `writ-subagent-stop.sh` are not on the write path, so changing them is outside
  this cycle's approved scope.

Accepted trade-offs, named rather than glossed:

1. This process can no longer start teeing later, because the redirect either
   happened at startup or did not. That cost is theoretical here: `WRIT_DEBUG`
   is fixed at exec, and the retired line read it exactly once too, so nothing
   could enable capture mid-process under either spelling.
2. The idiom now has two spellings in the tree. That is the real cost. The
   divergence is recorded here and in a comment at the call site, so the next
   reader does not restore consistency by putting the fork back.

The source-shape test that greps this hook
(`tests/test_phase4c_stderr_capture.py::TestPreWriteDispatchStillCovered`) looks
for three substrings, all of which appear in both spellings, so it passes either
way and is not a test of this change. The behavioural test is
`TestPreWriteDispatchForkBothStates` in the same file: it runs the real hook
under strace and asserts no `tee` process with debug off, a `tee` process plus a
written breadcrumb with debug on, and that the two states differ in total execve
count so neither assertion can pass vacuously.

## Decision 2: several named branches against a loopback stub, not one corner against a dead port

The old measurement had one number for this hook, produced by pointing
`WRIT_PORT` at a dead port. That is a branch only the test produces. Measured
against the live corpus, a trivial 6-byte `.py` stub matched 9 rules where a
`.txt` matched zero, so for source files the injection branch is typical.

The budget is now four named branches, each with its own constant:
`daemon_down_deny`, `live_allow_no_rules`, `live_allow_with_rag`, `live_deny`.

Why per-branch rather than one number: the honest answer is that one number
cannot represent a hook whose cost is chosen by the daemon's response. Measured
hermetically, the four branches span 7 to 13 execve attempts. Averaging them
would produce a figure no run ever produces.

Why a stub rather than the real daemon: the measurement has to stay
deterministic and hermetic. `tests/_stub_daemon.py` runs a stdlib
`ThreadingHTTPServer` on port 0 bound to 127.0.0.1, serves canned bodies for the
only two routes this hook reaches (`POST /pre-write-check` and
`GET /always-on`), and 404s everything else. `bin/lib/common.sh:1672` already
resolves the transport to TCP whenever `WRIT_PORT` is set, which conftest does,
so no socket plumbing is needed. No Neo4j, no live daemon, no network beyond
loopback, and the bodies are literals, so two runs of a branch give the same
count.

Why every branch carries a POSITIVE proof: a stub that 404s, or a canned body
the hook cannot parse, sends the hook down the daemon-down fallback and produces
a plausible count that reads green. Measured: the 404 run and the no-listener
run are identical at 13 attempts, so the count alone cannot tell them apart.
Each branch therefore asserts a positive signal before it reports a number. The
allow branches assert that the stub saw `GET /always-on` with `mode=work` in the
query, which can only be there if the canned response was received, split out of
the dispatch blob, whitespace-stripped and used. The deny branches assert the
canned reason string reached the hook's own stdout. The RAG branch asserts the
canned rule text reached `additionalContext`.
`test_a_404_pre_write_check_fails_the_live_proof` proves the proofs discriminate,
by running the 404 case and asserting the live proofs raise.

Why attempts rather than real processes for the branch constants: attempts are
deterministic here (identical across three consecutive runs of every branch)
while the successful-execve counter moves by one or two between runs, because
`strace -f` interleaves concurrent children and can split a line into
`<unfinished ...>` and a later `resumed`, which the end-of-line `= 0` match then
misses. The measured real counts are recorded beside each constant anyway.

### What the hermetic measurement cannot prove

Stated here rather than discovered at review.

- It does not prove the real server returns these shapes. The bodies are
  literals.
- It does not prove live wall time. Process counts only.
- It cannot validate gate POLICY. The allow, ask or deny decision is made
  server-side, and the stub just asserts one. The existing gate tests keep that
  job.
- It speaks TCP, and the real daemon does not serve this route over TCP when
  enforcement is on. Found during the live spot check, not by reading: with
  `WRIT_TCP_READONLY` enabled, which the operator's interactive daemon has,
  `POST /pre-write-check` returns 403 over TCP and is served only over the
  daemon's unix socket. So the stub's transport corresponds to a daemon with that
  enforcement off, and it is also why a live spot check of the live branches has
  to go through the socket.

  The per-branch process counts still transfer to the socket path, and that is
  MEASURED rather than reasoned from the code. `WRIT_CURL_TRANSPORT` is spliced
  into the same `curl` invocation as either an empty string or the two words
  `--unix-socket <path>`, so it changes curl's arguments and not the process
  count. Traced: `curl -s --unix-socket <path> http://localhost/health` and
  `curl -s http://127.0.0.1:9/health` are both 1 execve when curl is traced
  directly, and both 2 when traced through a `sh -c` wrapper, which adds the
  shell's own exec. Equal in both harnesses, which is the claim; the absolute
  figure depends only on whether a shell is in the traced tree.

### The live spot check, and what it changed

Run by hand on 2026-09-01, three runs per branch, recorded in `LIVE_SPOT_CHECK`
in the budget test. Two branches were reproduced and two were not, and the
distinction is kept because "not measured" and "measured equal" are different
claims.

- `daemon_down_deny`: 13 attempts against the live daemon over TCP, identical to
  the hermetic figure. It lands on the local fallback for the 403 reason above,
  which also means the suite always takes this path even when a real daemon is
  running on `WRIT_PORT`.
- `live_deny`: 15 attempts over the unix socket, against 7 hermetically. The gap
  is identified rather than absorbed: a real denial's `writ_action_push` reaches
  `POST /methodology-companion` and gets a body back, which costs the
  push-observability python plus the reason-merge python. The stub 404s that
  route, so the hermetic number is the deny branch WITHOUT the methodology push.
- `live_allow_no_rules` and `live_allow_with_rag`: not reproduced. A live allow
  needs a session with a mode set, which is gate state, and this cycle does not
  manipulate gate state to take a measurement.

An earlier draft of this record carried the plan's live figures (23, 31, 32, 30)
as if they had been reproduced here. They had not been. The numbers above are the
ones this cycle actually measured, and the branches it could not measure say so.

### Named follow-up, not silently absorbed

The whole-path 15-hook sums stay on the daemon-down branch. The branch spread
that was measured lives in ONE hook, so four extra traced runs cover it, while
making the whole path branch-aware costs 15 hooks times 4 branches and needs
canned responses for every route the other 14 hooks call. The other hooks do
behave differently with a daemon up, so this is a real gap and it is left open
deliberately.

## Decision 3: guard the mkdir, and keep the row

`writ_event_buffer_append`, `writ_friction_buffer_append` and the buffered
`log_gate_decision` path each ran `mkdir -p` with no directory test. The first
runs at exit on every instrumented hook, so it was 10 processes on a single file
write.

A bare `[ -d ] || mkdir -p` is the obvious fix and it introduces a real defect.
The unconditional call was doing work in one case: a cache directory removed
mid-session was silently recreated and the row landed. With a bare guard the
create is skipped, the append fails, and the `|| true` beside it swallows a
telemetry or audit row in silence. Trading a governance record for a process is
the wrong direction.

So the shape is: test the directory, skip the create while it exists, and if the
append fails anyway, create the directory and retry once. That keeps the `mkdir`
reachable exactly when it is needed and never otherwise, and it is provable by
deleting the directory between two appends
(`TestBufferAppendKeepsTheRow` in `tests/test_write_path_process_budget.py`).

The gate-decision call site is guarded but deliberately NOT retried. A failed
buffered append there already falls through to `_gd_emit_now`, which writes the
audit record synchronously, so retrying the buffer would take the row off the
durable path to save a spawn on the one branch where the spawn is warranted.

Note that `writ-pre-write-dispatch.sh` does not call `hook_instrument`, so this
decision changes the cost of the OTHER write-path hooks. The two halves are
therefore measured separately, or one gets credited with the other's win:

- Half A, the dispatch hook's own spawns: 14 processes, measured by binary name
  in its trace (head 1 to 0, sed 8 to 0, tail 1 to 0, tr 1 to 0, dirname 2 to 1,
  cat 3 to 2, tee 1 to 0).
- Half B, the mkdir guards: 16 processes, measured on the fourteen write-path
  hooks half A does not touch, whose summed execve attempts went 111 to 96,
  exactly the mkdir count they were paying, plus one more inside the dispatch
  hook.

## Decision 4: the builtin stdin read, MEASURED AND REJECTED

`STDIN_DATA=$(cat)` at the top of the hook looks like an obvious fork to remove:
`IFS= read -r -d '' STDIN_DATA` is a builtin. It is NOT taken, and this is
recorded because "a builtin must beat a fork" is exactly the kind of reasoning
this program has been wrong about before.

THE OPERATIVE MEASUREMENT, taken on the channel this hook actually reads: stdin
delivered through a PIPE, which is how Claude Code delivers a hook envelope.
Median of seven runs per size.

| payload | `$(cat)` | `read -r -d ''` | ratio |
| ------- | -------- | --------------- | ----- |
| 1 KB    | 2.09 ms  | 1.66 ms         | 0.80x |
| 64 KB   | 2.42 ms  | 28.65 ms        | 11.8x |
| 1 MB    | 6.96 ms  | 444.64 ms       | 63.9x |

The abort criterion was declared before the run: slower than `$(cat)` by more
than 25 percent at any of the three sizes and the `cat` stays. It is slower by
an order of magnitude at two of them, so the `cat` stays and the capability is
recorded as not taken.

Note that `tests/test_pre_write_dispatch_line_split.py::TestStdinReadPayloadSizeSweep`
encodes that abort criterion as an ASSERTION, so it stays RED at 64 KB and 1 MB
on this machine. That is the measurement announcing its result, not a defect in
the implementation: the decision the red test demands (keep the `cat`) is the
decision that shipped. Two red tests are a poor way to record a finding, and
converting them into a recorded measurement plus a green assertion would need an
edit to that file, which was out of scope this cycle.

### The harness trap that nearly made this look acceptable

bash's `read` builtin does unbuffered single-byte reads from a NON-SEEKABLE fd,
and the write path carries the entire file content in `tool_input.content`. A
SEEKABLE fd is a different code path: bash reads in blocks and seeks back.

An earlier attempt at this same swap measured `< file` instead of a pipe and
reported 1.8x at 1 MB (7.6 ms against 13.5 ms), which reads as acceptable. That
figure is recorded here ONLY as the trap it is. It is not a competing result of
equal standing: it measured a channel no hook is ever handed, so it does not
describe this line at all.

The two channels were then isolated directly, same script and same payload:

| stdin fd at 1 MB              | `read -r -d ''` |
| ----------------------------- | --------------- |
| pipe (what a hook gets)       | 0.44 s          |
| regular-file redirect         | 0.01 s          |

An independent second run of the same comparison put it at 446.0 ms against
14.6 ms, roughly 30x, so the direction and order of magnitude reproduce.

THE LESSON, which is about the harness and not about bash: the number moved by
more than an order of magnitude on a detail of how the input was delivered, while
nothing about the code under test changed. A benchmark that does not reproduce
the production channel is not a slower or faster measurement of the same thing;
it is a measurement of something else.

Two correctness differences would have needed handling even if the timing had
gone the other way, and they are recorded so nobody re-opens this on the
strength of the 1 KB row alone:

- `read` returns exit status 1 at EOF when it never finds the delimiter, so it
  reports failure after successfully reading all 1,048,576 bytes. Harmless in
  this file only because it sets no `set -e`.
- `read -d ''` truncates at the first NUL byte, where command substitution drops
  the NUL and keeps the rest.

## One out-of-plan edit, and its authorization

`tests/_inventory.py` is not in plan.md's `## Files` list and was edited anyway.
Recorded here because review flagged it as an unexplained out-of-plan edit and
was right to: the authorization was given in a message, and a message is not an
artifact a later reader can find.

WHAT CHANGED: comment text only, no code, no assertion. The docstring of
`_blanked_lines` cites a real file that contains a literal U+2028, as the reason
that function splits on `\n` instead of using `str.splitlines()`. It named line
200 of `writ-pre-write-dispatch.sh`, and this cycle's edits moved that line to
308, so the citation had become false.

WHY IT WAS AUTHORIZED: the standing rule in this repo forbids ASSERTING on
documentation prose, because doc staleness is not a code defect and a test that
pins wording fails for the wrong reason. It does not say to leave prose that has
become false. Correcting a stale citation carries none of the risk the rule
exists to prevent: nothing asserts on it, so nothing can break, and a reader who
follows the citation to line 200 now finds an unrelated line and concludes the
explanation is wrong.

THE DURABLE HALF, which is why the fix is not just a new number: the line number
drifts with every edit to that hook and nothing asserts it, so re-derive it
rather than trusting it. What does not drift is that a real file in this tree
contains the character, and that is the whole reason the function avoids
`str.splitlines()`. The correction says so in place, so the next reader who finds
the number stale knows it is expected to go stale and knows what the example was
for.

## OPERATOR PROCEDURE: one pytest process at a time

This belongs in the write-path document because this is what a future operator
reads before running the write-path measurements, and the hazard is in the
procedure rather than in any file.

The idempotence fix in `4c96a77` made the session-start preflight WIPE AND REWARM
the whole isolated graph on EVERY pytest invocation. That is what makes a run
reproducible regardless of what a previous run left behind, and it is worth
paying for. The cost, which neither plan.md nor the review anticipated:

TWO CONCURRENT PYTEST RUNS ARE NOW MUTUALLY DESTRUCTIVE, and more
deterministically than before. A second run started while a first is in flight
deletes the first's state at a predictable moment, where previously the two merely
shared a dirty graph and produced ambient flakiness.

HOW IT WAS TRIPPED, an hour after the cycle that caused it shipped: a delegation
said four specific test files were "self-contained and do not touch the graph".
THERE IS NO SUCH FILE ANY MORE. The preflight runs at `pytest_sessionstart`
regardless of which files are selected, so file selection cannot make a run
graph-safe. The reviewer ran those files while a full suite was mid-run, and the
full suite lost its state.

THE RULE, stated plainly:

- One pytest process at a time against the isolated instance.
- When delegating, say "do not run pytest at all while a suite is running".
  Never name files as safe: naming files is what failed, because the hazard is
  attached to the SESSION, not to the tests.
- A stray concurrent run is detectable after the fact. The preflight prints its
  `graph isolation: wiped` line, so count those occurrences against the number of
  runs intended; a surplus is a concurrent run that already happened.

Framed honestly: this is the price of the fix, not a defect in it. It traded
ambient flakiness for a sharp, visible, attributable failure. That is the better
trade only if the procedure knows about it, which is why it is written down here
rather than remembered.

## Consequences

- `PYTHON_BUDGET` 17 to 16, `TOTAL_BUDGET` 180 to 112, `REAL_PROCESS_BUDGET` 155
  to 110, all from a post-change traced measurement rather than from arithmetic
  on a spawn count. The pre-cycle values are pinned beside the new ones and a
  test asserts the direction, so the ratchet cannot be relaxed instead of
  converting a call site.
- The parse-parity mirror in `tests/test_pre_write_parse_parity.py` tracks the
  hook's new splitter. A mirror that stops tracking turns a parity claim into a
  claim about a splitter nothing runs.
- `tests/test_pre_write_dispatch_line_split.py` holds the executed differential:
  both splitters, one corpus of shaped inputs, compared byte for byte per field.
  It is the reason the `tail -n +3` semantics (line 3 ONWARD, not line 3) and the
  unset trailing index survived the conversion intact.
