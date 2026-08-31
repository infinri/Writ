# ADR: the capture record carries the event, the exit code, and the hook's own pid

Status: accepted (plan `dfacff61-23d5-474e-846c-2e2f0f0ea482`, 2026-08-31)

## Context

`bin/lib/common.sh::blackbox_log` wrote a six-field record: `ts`, `hook`, `direction`,
`session`, `pid`, `payload`. Three separate things were wrong with that schema, and only the
first two read as missing features.

NO EVENT AT THE RECORD LEVEL. `writ/analysis/blackbox.py` read the event out of the PAYLOAD,
from `hook_event_name` on an IN row and `hookSpecificOutput.hookEventName` on an OUT row. A
row whose payload is not a JSON object therefore had no event at all and censused as
`unknown`. That is exactly the shape of the two deferred capture cycles: plain stdout is
plain text, and an exit row has no payload.

NO EXIT CODE. The mechanism Claude Code reacts to on a Stop-hook block is a non-zero exit
with stderr. `writ/shared/delivery.py` has declared both names for it since the delivery
table gained `EXIT_MECHANISMS`, and `delivery_provenance` could never answer `observed` for
either, because no captured record could carry a code.

THE PID HAD NEVER WORKED, and this is the part that was not a missing feature. The record's
`pid` was `os.getpid()` inside the ephemeral `python3 -c` encoder that `blackbox_log` forks
per call, so an IN row and an OUT row from ONE hook invocation carried DIFFERENT pids and the
`(hook, pid, session)` join in `build_census` could never match. Measured in one bash
process: `$$` was 836599 while the IN row recorded 836605 and the OUT row 836612. The
consequence is visible in the committed census, where all four real OUT classes read
`origins: {harness: 0, undetermined: N}` for 162, 64, 140 and 68 rows.

The defect survived because its only tests hand-built the IN and the OUT row with one
identical literal pid. A join that never fires looks exactly like a join with nothing to
join.

## Decision

### Three fields, and the absent-versus-empty rule that makes two of them readable

    blackbox_log <direction> <hook> <session> [event] [exit_code]

`event` is written on EVERY row this writer produces: a string when the calling process
observed one, and JSON `null` when it did not. It is deliberately not defaulted to `""`.
The existing 9,388-record corpus has the key ABSENT, a new row has it PRESENT, so
"this row predates the schema" and "this process observed no event" stay distinguishable.
Both would be falsy under `.get()`, so the reader tests `"event" in record` instead, the
same membership form `_classify_in_origin` and `manifest_read` already use. The reader files
the null case under the stated name `event_not_observed` and reserves `unknown` for the
key-absent case, so no NEW row can ever land in `unknown`. That matters because `unknown`
was the previous cycle's defect class: a capture bug wearing the costume of a data category.

`exit_code` is written only on a row whose direction is `exit`, always as an integer. Bash
validates it with the same `case "$x" in ''|*[!0-9]*)` idiom the size cap uses and DROPS THE
WHOLE ROW when it is not numeric, rather than coercing it or writing null. An exit row
without a code therefore cannot exist and there is no third state for a reader to interpret.
On `in` and `out` rows the key is absent, because a hook's exit status is not knowable when
its IN row is written.

`pid` is the hook shell's own `$$`, passed into the encoder through the environment as
`WRIT_BB_PID` and coerced with `int()`. The encoder is told the pid rather than observing
itself, because what it can observe about itself is precisely the wrong answer. `$$` stays
the hook shell's pid inside both the pipeline and the command substitution, which is the
value both call sites need. The `int()` is load-bearing: a str on one row and an int on
another is a silently failing join, which is the defect being fixed.

THE RESIDUE, stated rather than engineered around: the OS recycles pids, so two invocations
of the same hook in the same session could share one. The join keeps the MOST RECENT IN row
per key in a sequential pass, so the worst case is an OUT or exit row inheriting the origin
of another invocation of the same hook in the same session, which is almost always the same
origin class.

### The exit row is written by the trap that already owns every hook's exit path

`hook_instrument` installs `trap '_writ_hook_exit_trap' EXIT` and `common.sh` auto-invokes it
for every script under `hooks/scripts/` except `writ-statusline.sh`. It is already the
structural owner for all 40 instrumented hooks, `writ_on_exit` APPENDS handlers rather than
replacing them, and the trap restores the real `$?` before each handler. This cycle adds NO
trap; it reads `$rc` inside the one that exists:

    if [ "$rc" -ne 0 ] && blackbox_enabled; then
        blackbox_log exit "${_WRIT_HOOK_NAME:-unknown}" \
            "${HOOK_SESSION_ID:-${SESSION_ID:-}}" "${HOOK_EVENT:-}" "$rc" </dev/null || true
    fi

Four decisions sit in those lines.

LAST, after the `hook_execution` append and after the registered handlers. Capture is a debug
switch, and nothing with retention may be delayed or skipped by it. `set +e` is already in
force there and `exit "$rc"` uses the saved value, so a failure here cannot change the hook's
status.

NON-ZERO ONLY. An unconditional row would add a python spawn to every one of the 40
instrumented hooks whenever capture is on. Nothing is lost: the denominator, every hook's
exit code including 0, is the `hook_execution` row the same trap appends 15 lines earlier.

`</dev/null`, NOT a pipe. `blackbox_log` reads stdin, and `printf '' | blackbox_log` forks a
subshell before the logger can decide it has nothing to do. A redirect costs no process and
gives the encoder the empty payload an exit row should carry.

THE SESSION EXPRESSION IS REVERSED from the telemetry row's `${SESSION_ID:-${HOOK_SESSION_ID:-}}`
on the same function, deliberately. The other side of the join is the IN row, which
`load_hook_env` writes with `${HOOK_SESSION_ID:-}`. An exit row preferring `SESSION_ID` would
key differently in every hook where the two differ, and the join this row exists to feed
could not fire.

AN EXIT ROW RECORDS THE CODE, NOT THE STDERR TEXT. Capturing stderr means owning the
process's stderr for its whole lifetime, which is the redirect shape the previous ADR refused
on the grounds that capture must never sit in the path of a reply. The refusal reason is
already durable elsewhere: 6 of the 7 scripts with a non-zero exit call `log_gate_decision`
before exiting, which is a 365-day audit record.

### Where an exit row's event comes from, and where it does not

`HOOK_EVENT`, set by `load_hook_env` from the envelope's `hook_event_name`, and nothing else.
4 of the 7 scripts with a non-zero exit DO call `load_hook_env` (`writ-run-pending-tests.sh`,
`validate-file.sh`, `validate-handoff.sh`, `validate-rules.sh`), so their exit rows carry a
real observed event. The 3 that do not (`enforce-violations.sh`, `writ-verify-before-claim.sh`,
`writ-comms-output-gate.sh`) are all Stop hooks that read stdin themselves;
`writ-comms-output-gate.sh` says outright that it cannot call `load_hook_env` because
`$STDIN_JSON` has already consumed stdin. Their rows carry `event: null` and census as
`event_not_observed`.

The cycle's `+1 event` claim does not rest on those three rows: `writ-run-pending-tests.sh`
calls `load_hook_env` and exits 1, so Stop is OBSERVED from a real envelope.

### Coverage is held by derivation, not by vigilance

Two source-derived populations in `tests/_inventory.py`, in the shape the previous cycle
established. `direct_blackbox_exit_calls()` must be EMPTY: the token `blackbox_log exit` may
not appear in any comment-stripped hook source, because a hook that cannot reach the logger
cannot record an exit code other than the one it returned. `nonzero_exit_scripts()` must be
NON-EMPTY and every member must be auto-instrumented, which is the registry argument made
mechanical.

## Alternatives refused, with the reason each was refused

**(a) An enumerated list of exit sites, each registering its own capture.** Refused because
the repo has already MEASURED what that costs. `tests/firedrill/_census.py` is the only such
list in the tree and it declares 7 of the 10 real non-zero exit sites: the two
`exit "$WARN_EXIT"` sites in `validate-rules.sh` are undeclared, and `validate-file.sh:91` is
declared as a deferred debt with a reason rather than being invisible. The file documents its
own incompleteness. One shared registry needs no registration for the eleventh site.

**(b) Hard-coding `HOOK_EVENT="Stop"` in the three scripts that cannot call
`load_hook_env`.** Refused: that is an enumerated list whose failure mode is a WRONG value if
one of them is ever re-registered under another event, which is worse than an absent one.

**(c) Resolving an exit row's event from `hooks/hooks.json` on the reader side.** Available
today, since each of the 7 scripts is registered exactly once, and refused for this cycle
anyway: it is INFERENCE inside a module whose docstring says absence is stated and never
inferred, and it would need a provenance field to stay honest. A later cycle can add it as an
explicitly labelled derived value.

**(d) Defaulting `event` to `""` with `${x:-}`.** Refused because it collapses the two facts
the field exists to separate. See the absent-versus-empty rule above.

## Consequences accepted

**`directions_never_observed` CHANGES SHAPE, permanently.** Its vocabulary gains `exit` as a
third member, so every event that has never produced a captured refusal exit now lists
`exit` there, including events like PreToolUse whose hooks refuse with a `permissionDecision`
and contain no non-zero exit site at all. That is a true and permanent statement rather than
a gap, and the population that CAN produce an exit row is derivable from
`tests/_inventory.py::nonzero_exit_scripts()`.

**The mechanism is recorded on the strength of the exit code alone.** The shared vocabulary's
two names, `exit2_stderr` and `exit_nonzero_stderr`, bundle the code AND the stderr, and this
cycle observes only the code. That is the same shape as `_STDOUT_MECHANISM` being read off a
payload with no recognized key. It is written down so nobody reads a later `observed` as
proof that the stderr text reached the user.

**OUT-side event coverage goes from 3 events to 4, not to 12.** PreToolUse, PostToolUse and
SubagentStart have an OUT row; exit-code capture adds Stop. The remaining 8 gain nothing from
this cycle, checked hook by hook, because Writ's hooks on them emit no envelope and never
exit non-zero. "Structural" means the mechanism is owned by one seam, not that the event gap
is closed.

**Capture off adds no process, and capture on adds one python spawn per non-zero exit.** The
trap's added cost with capture off is `[ "$rc" -ne 0 ]` (a builtin, and the cheapest
discriminator, so it is tested first) followed by `blackbox_enabled`, which is one variable
test and one `[ -f ]`. Neither forks. A zero exit costs nothing at all even with capture on.
The reader's fold stays one sequential pass with O(1) work per row: an exit row adds two dict
bumps and one `classify_delivery` call, which is a frozenset membership test.
`tests/test_write_path_process_budget.py`'s ratchet is NOT edited; it is the standing
measurement.

**The pid fix cannot be proved by a hand-built record, so it is not tested that way.**
`tests/test_blackbox_record_schema.py` drives real bash subprocesses and anchors both rows to
the probe's own `$$`, read out of the probe's stdout. Row-to-row equality would not be proof:
two encoders could coincidentally agree with each other while both disagreeing with the hook.

## Deferred, with the cost stated so nobody re-scopes it casually

PLAIN STDOUT is roughly 44 inline emission sites, not a handful. `writ-rag-inject.sh` has
about 22 plus three through the single-sourced `emit_mode_directive` and
`emit_post_compact_directive`; `auto-approve-gate.sh` has about 21 (10 heredocs including a
shared hint reached from 4 call sites, plus 11 standalone echoes); `writ-manual-test-grant.sh`
has 1. All of them are on UserPromptSubmit, and the value is +1 event.

This cycle makes that work possible WITHOUT a second schema change, because a plain-text
payload will carry its event in the record's own field.

CORRECTION TO A PRIOR ASSUMPTION, recorded so it is not re-litigated:
`session-start-bootstrap.sh` contributes NOTHING to that count. Its two messages go to stderr
through `cat >&2` and it always exits 0.

## The committed census artifact is left stale, and that is a ruling rather than an oversight

`docs/reference/blackbox-census.json` is NOT regenerated by this cycle. It was already stale
before it: it still lists `PostToolUse` under `directions_never_observed` even though the
previous cycle converted `writ-posttool-rag.sh`, because the artifact predates that
conversion and capture is currently OFF. This cycle makes it stale in a second way, with no
exit rows and no `exit` member in `directions_never_observed`.

Regenerating requires a real capture session, which costs about 20 ms and one python spawn on
EVERY hook for its duration. That is the user's decision and not the implementer's. Until it
is run, `delivery_provenance("Stop", "exit_nonzero_stderr")` correctly stays `unproven`,
which is the system working as designed rather than a defect.
