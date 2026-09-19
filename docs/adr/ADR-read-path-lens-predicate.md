# ADR: a cheap predicate in front of the runtime-lens read gate

Status: accepted
Date: 2026-08-29
Plan: `.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md`
Touches: `bin/lib/common.sh`, `hooks/scripts/writ-debug-code-gate.sh`,
`scripts/bench_read_path_latency.py`

## Context

`writ-debug-code-gate.sh` runs on every `Read`, `Grep` and `Glob`. It paid a full python
interpreter start plus a `writ` package import to ask `writ-session.py can-read-code`
whether the runtime (debug) lens wanted to block the read, and in most sessions the
answer was "the lens is not even active". Measured on this machine before the change,
that hook alone cost a median of 122 ms per invocation, and the Read event as a whole
(three registered PreToolUse hooks) cost a median of 290 ms.

The expensive call happened before the hook knew whether its lens could apply at all.

## Decision

A predicate answers that question first, from the session cache, in one `jq` process.

    writ_runtime_lens_check_required <session_id>
        exit 0  the expensive check is REQUIRED
        exit 1  the lens provably cannot deny; skipping is safe

    REQUIRED when   source_type == "runtime"
                 OR (mode == "debug" AND source_type is absent)

That predicate was derived from the MEASURED deny set, not from reading the code. The
first version of it assumed the lens could only deny in debug or investigate mode; run
over the full cross product of 6 modes, 4 source types and 3 tools, that is false. The
lens denies whenever `source_type == "runtime"` in ANY mode, including work, review and
conversation. Shipping the mode-only version would have silently changed behaviour for
every session that had set an explicit source type.

The code corroborates the measurement rather than replacing it:
`gates._can_read_code_check` returns allow immediately unless
`mode_engine._effective_source_type(cache) == "runtime"`, and `_effective_source_type`
returns the cache's own `source_type` when truthy, else `MODE_CONFIG[mode]["source_type"]`,
where `debug` is the only mode carrying a static `"runtime"`.

`tests/test_debug_lens_predicate.py` re-derives that matrix from `VALID_MODES`,
`_VALID_SOURCE_TYPES` and the `Grep|Read|Glob` matcher in `hooks/hooks.json`, and
compares the REAL bash predicate (a subprocess) against the REAL
`gates._can_read_code_check` case for case. 51 of the 72 cases skip and 21 deny, with an
empty intersection, so a predicate that simply never skipped would fail the module just
as loudly as one that skipped a denying state.

### Where the predicate lives, and the alternatives rejected

IN `bin/lib/common.sh`, beside `writ_session_mode_direct`, whose contract it copies.

- A NEW SOURCED FILE was rejected: the hook already sources `common.sh`, and sourcing is
  barely size driven (measured 4.1 ms for 600 lines against 6.1 ms for 2,123), so a
  second file would have cost about 4 ms of a roughly 75 ms saving to buy nothing.
- INSIDE THE PYTHON HELPER was rejected: the interpreter start IS the cost, so an early
  return inside it saves nothing.
- INLINED IN THE HOOK was rejected: a single named function is one site for the property
  test to target and one site to go stale, where logic inlined in a hook is reachable by
  no test directly.

It resolves the file through `writ_session_cache_dir`, THE bash-side definition of the
cache location, already pinned against the package's value by
`tests/test_session_cache_dir_parity.py`. That inherits the parity guarantee instead of
restating a path, so `WRIT_CACHE_DIR` moves the predicate's read and the real check's
read together or neither.

### Uncertainty takes the expensive path

Anything that does not resolve to a parsed JSON object returns "check required": an
absent file, an unreadable one, bytes that are not JSON, a non-object top level, a
session id that names no reachable path, a line the function cannot split
unambiguously, and `common.sh` itself failing to source. Skipping on uncertainty would
be a silent gate bypass, so uncertainty always pays the full cost and never grants a
read.

Two consequences are deliberate and worth stating plainly.

A SESSION WITH NO CACHE FILE PAYS TODAY'S FULL COST on every read. That state is
provably skippable today, because `_read_cache` on an absent file yields no mode and no
source type, and it is deliberately NOT skipped: the proof would depend on absence
continuing to mean "no lens", and a lazy cache seeder is exactly a mechanism that fills
absent caches. A guard that is safe only because a record is missing loosens the moment
someone fills the gap.

THE `type` GUARD ON THE CALL SITE IS LOAD BEARING. The hook calls the predicate through
the same `type <name> >/dev/null 2>&1` idiom it already uses for `hook_instrument`. An
undefined function means `common.sh` did not source; without the guard, `set -e` would
abort the hook there, before it could refuse anything, which turns a refusing surface
off. With it, a missing library falls to the expensive path and to today's behaviour,
including today's ability to deny.

### The jq / WRIT_NO_JQ seam cannot leak

This repo has already been bitten here: `jq`'s `//` falls through only on null and
false, while python's `or` also falls through on an empty string, and that difference
made three governance hooks disagree across the seam on an identity field.

Neither arm of the predicate decides anything. Each emits a sentinel-prefixed,
unit-separated line ONLY when the document parses AND is an object, and each field is
emitted as `s:<value>` when it is a JSON string and `x` otherwise. The comparison
against the literals `"debug"` and `"runtime"` happens once, in bash.

Transporting the JSON TYPE rather than a truthiness verdict is what closes the seam.
`//` was avoided as planned, but a truthiness test would have leaked anyway: jq calls
`0` and `""` truthy where python calls them falsy, so an emitted "the field was truthy"
flag would disagree between the arms on a numeric `source_type`. A value that is not a
string can never be the string `"runtime"`, so treating every non-string as "no usable
source type" and deferring to the mode is safe in the required direction, and it is
identical on both arms by construction. `tests/test_debug_lens_predicate.py` asserts
that as a property over all 28 shapes rather than trusting this argument.

### The other python starts on the allow path

THE SESSION ID EXTRACTION STAYS AS INLINE PYTHON, and it is now the single largest
remaining cost on the skip arm at about 17 ms of the 48. `json_transform` or
`writ_require_session` would do it in one jq call, and that is declined. This is the
hook's IDENTITY resolution: with no id the hook exits 0 and refuses nothing, so routing
it through `common.sh` would convert a broken library from "still denies,
uninstrumented" into "silently allows". That 17 ms is left on the table on purpose.

THE DECISION AND REASON PARSES BECAME ONE `parsed_fields` CALL. Two python starts became
one jq start on the helper's own output, which the hook already holds in a variable.
Both variables are initialized to the empty string BEFORE the `eval`, because an
unparseable document makes `parsed_fields` print nothing and `set -u` would then abort
the hook.

THE DENY EMITTER WAS REORDERED, NOT REWRITTEN. It is the same python block, byte for
byte, and it now runs only when the parse did not positively say `allow`. Converting it
to jq would have put the `//` versus `or` divergence on a refusal message's default and
on the JSON encoding of a reason that can carry quotes and newlines, to save about 8 ms
on a path that only runs when the gate refuses.

ONE DEVIATION FROM THE PLAN'S LITERAL WORDING, made deliberately. The plan said the
emitter "runs only when the decision is deny"; the implementation tests `!= "allow"`
instead of `== "deny"`. The two differ in exactly one state: when the parse produces
nothing, which is what happens if `common.sh` never sourced and `parsed_fields` is
therefore undefined. Under `== "deny"` that state would print no refusal at all, so a
missing library would turn this gate off; under `!= "allow"` the emitter still runs, and
being self-contained it re-reads the same JSON and reaches the same verdict it reached
before this reordering. That is the same fail direction the predicate uses, applied to
the refusal.

BOTH BRANCHES STILL WRITE THEIR AUDIT ROW. The skip arm calls `log_gate_decision` with
the same gate name, decision and target the un-skipped allow arm uses, and the two rows
are pinned equal by the property module. A gate that gets faster by disappearing from
the audit stream is the regression the decision record exists to prevent.

## Measurement

`scripts/bench_read_path_latency.py`, 15 timed iterations per cell after 3 untimed
warmups, arms interleaved within each iteration and the leading arm alternating, before
arm materialized from `HEAD:hooks/scripts/writ-debug-code-gate.sh`. Per EVENT, in
milliseconds. `paired` is before minus after within the same iteration.

    state        tool   hooks   before med   after med   paired med   paired min/max
    skip         Read     3         290.1       213.9         77.8      70.2 /  88.8
    skip         Grep     1         121.9        46.8         74.1      69.2 /  82.9
    skip         Glob     1         123.8        47.6         76.8      71.5 /  88.2
    lens-active  Read     3         479.2       444.1         37.6      24.7 /  46.1
    lens-active  Grep     1         121.0        85.7         35.5      31.7 /  43.1
    lens-active  Glob     1         123.1        85.9         38.4      33.5 /  40.4

Grep and Glob are this hook alone, so those rows are the hook's own cost: 122 ms to 47
ms on the skip arm, a 61 percent reduction. The check-required arm also improved, by
about 36 ms, purely from collapsing the two inline parses and skipping the deny
emitter's interpreter start on an allow. Every paired minimum is positive in all six
cells, so the effect is larger than this machine's noise.

THE PLAN'S PREDICTIONS WERE WRONG AND ARE CORRECTED HERE, which is what the plan asked
for. It predicted 25 ms for the skip arm and 70 ms for the check-required arm. Measured:
about 47 ms and about 86 ms. The prediction's per-invocation process budget counted the
bash spawn, the source, the session id python and one jq, and omitted three costs the
hook actually pays. Bisected on this machine:

    bash spawn + source common.sh          7.2 ms
    hook_instrument                       +5.0 ms
    reading stdin                         +2.0 ms
    the inline session id python          +17.0 ms
    writ_runtime_lens_check_required      +5.7 ms
    log_gate_decision + exit trap         +8.1 ms
    -----------------------------------------------
    skip arm total                        ~45 ms median, ~48 ms p95

`hook_instrument` and `log_gate_decision` were not in the budget table, and
`log_gate_decision` re-reads the same cache file the predicate just read in order to
stamp the row's mode, which is a second jq. None of the three can be removed under this
cycle's constraints: instrumentation and the audit row are required on both branches,
and the session id must stay inline python.

CONSEQUENCE FOR THE PERF FLOOR. `DEBUG_CODE_GATE_SKIP_P95_FLOOR_MS` in
`tests/test_hook_perf_floors.py` was set at 35.0, which is 1.3x the PREDICTED 25 ms. The
measured post-change p95 is about 50 ms, so that floor is unreachable and the test is
red on correct code. Following that module's own recorded calibration rule of roughly
1.3x measured p95, the floor should be 65.0. It still discriminates: the regression it
exists to catch, the `can-read-code` call creeping back onto the skip arm, costs about
122 ms.

## Consequences

- Reads in any session whose cache proves the lens cannot deny cost about 75 ms less per
  Read, Grep or Glob event. Reads in a lens-active session cost about 36 ms less.
- No allow or deny verdict changes for any of the 72 states in the derived matrix.
- No state is slower than before: every unresolvable cache takes the check-required arm,
  which is the pre-change path minus one interpreter start.
- The skip arm is now the common path, and it is the one this cycle created, so it is
  the one with a p95 floor.

## Out of scope, named so it is not rediscovered as new

`writ-posttool-rag.sh` IS NOT TOUCHED. It costs about 135 ms on every write and its
injection may never be used, but that has to be MEASURED and the measurement does not
exist yet. The question is whether a rule injected after a write is subsequently cited
or acted on, which needs the injection recorded with its rule ids and joined against
later citations in the same session, keyed by `prompt_id`. Today's counters cannot
answer it: every graduation and observation counter in this repo is still zero, and `ts`
on audit rows is the buffer flush time rather than the decision time, so a log row is
not an event. Building that measurement is its own cycle. Changing the Read path and the
Write path in the same cycle would also have made this ADR's attribution ambiguous.

THE DISPATCHER (Tier 2) IS DEFERRED, not dropped, and the numbers behind the deferral
are recorded here so they are not rediscovered. A cheap hook's roughly 27 ms decomposes
as 2.6 ms of bash spawn, 6.3 ms of sourcing `common.sh`, 3 to 6 ms of jq parsing, and 12
to 15 ms of the hook's OWN logic, which a dispatcher cannot remove. Consolidating nine
hooks recovers about 115 ms, not the 180 to 200 the design assumed, and it is the only
item in the cycle that trades away failure isolation.

THE UNGUARDED `log_gate_decision` at the end of this hook is pre-existing and unchanged.
With `common.sh` missing it exits 127 after the refusal has already been printed. It is
noted here so the next reader does not file it as a regression from this change.
