# ADR: hook reply capture sits at the emit site, not at the process boundary

Status: accepted (plan `dfacff61-23d5-474e-846c-2e2f0f0ea482`, 2026-08-29)

## Context

Black-box capture records the raw payloads Claude Code and Writ hooks exchange, so the
contract can be inspected empirically instead of inferred. The IN direction is wired into
`load_hook_env`, which roughly 20 hooks share, so it is structural. The OUT direction was
not. It was four hand-placed `blackbox_log out` calls in three scripts:

    hooks/scripts/writ-pre-write-dispatch.sh   lines 243 and 297
    hooks/scripts/writ-dispatch-discipline.sh  line 143
    hooks/scripts/writ-subagent-start.sh       line 440

Three separate problems came out of that, and only the first is the one the coverage
framing suggests.

COVERAGE. At least 15 other emission sites across 13 scripts printed a
`hookSpecificOutput` envelope with no capture at all, and the 20 call sites that refuse
through `emit_deny` or `emit_ask` captured nothing either, because those two helpers only
printed. `docs/reference/blackbox-census.json` shows the consequence: `PreToolUse` and
`SubagentStart` were the only two registered events with an OUT row.

CORRECTNESS. `writ-pre-write-dispatch.sh` printed its allow-path reply from inside a
`python3 <<'PY'` heredoc and then logged `${RAG_RULES_RAW}${AO_WRITE_BLOCK}`, which is the
ingredients of that reply rather than the reply. `writ/analysis/blackbox.py` degrades a
payload that is not a JSON object to `{}` and files it under the event name `unknown`,
which is the committed `unknown|writ-pre-write-dispatch|out` class: count 68, no
`hook_specific_output_keys`, no `mechanisms`. A reconstruction of a reply is not the reply.

ORDER. `writ-dispatch-discipline.sh` logged before it emitted; `writ-subagent-start.sh`
emitted before it logged. One order had to win.

The property this cycle owns is narrow and it is the one the three problems share: an OUT
row contains the bytes that were written to stdout.

## Decision

One emitter in `bin/lib/common.sh`:

    emit_hook_reply <payload> [hook] [session]

It returns immediately on an empty payload, writes `printf '%s\n' "$payload"` to stdout,
and only then, and only when capture is enabled, pipes that same shell variable into
`blackbox_log out`. EMIT BEFORE LOG, always, so capture never sits between a hook and its
answer. `emit_deny` and `emit_ask` keep their python-built envelope and hand the result to
the funnel instead of printing it, so their 20 call sites gained capture without being
edited.

The seam needs no registration or discovery: all 41 scripts in `hooks/scripts/` already
source `bin/lib/common.sh`.

Coverage is then held by three source-derived checks in `tests/_inventory.py`, not by a
list of converted sites. The token `blackbox_log out` may not appear under
`hooks/scripts/` at all (a hook that cannot reach the logger cannot record something other
than what it sent); the population of scripts that build an envelope must be non-empty and
every member must reach the funnel; and a heuristic scan reports any envelope emission
that still reaches stdout uncaptured.

## Alternatives refused, with the reason each was refused

**(a) Capture at the process boundary, with an EXIT trap or a `tee` process substitution
in `common.sh`.** Refused because it puts capture machinery in the path of the reply.
`exec > >(tee ...)` replaces the hook's stdout with a pipe to a process bash does not wait
for: if the tee dies the hook takes SIGPIPE mid-reply, and if it outlives the shell the
tail of the reply can be lost. The EXIT-trap variant means redirecting stdout to a temp
file for the whole process and printing it at exit, which delays every reply to process end
and puts a file write between the hook and its answer.

There is also a live precedent for a trap collision in this tree. The note at
`tests/test_write_path_process_budget.py` line 79 records that `pre-validate-file.sh` and
`inject-tier-workflow.sh` each installed their own `trap ... EXIT` and silently REPLACED the
instrumentation trap, discarding audit records with 365-day retention. A capture trap would
be the third claimant on that slot. Its one genuine advantage, seeing the exit code, is
unusable anyway: the capture record has no field for one.

**(b) A wrapper script named in `hooks.json`.** Refused on cost and blast radius. It edits
44 registrations, makes one wrapper bug break every hook at once, and adds a bash start to
every hook invocation whether or not capture is on. It is NOT refused forever. It is the
only shape that could capture an exit code, so the deferred cycle below should re-price it
rather than treat this ADR as having settled it.

## What is deferred, and why it is ONE cycle rather than two

Plain stdout and refusal exit codes are both out of scope here, and they are the same piece
of work because they need the same change first: the capture record must gain an `event`
and an `exit_code`.

PLAIN STDOUT. It reaches the model only on `UserPromptSubmit`, `UserPromptExpansion` and
`SessionStart` (`writ/shared/delivery.py::STDOUT_TO_MODEL_EVENTS`). Routing those emitters
through this funnel would manufacture the defect this cycle fixes. An OUT row's event is
read from `hookSpecificOutput.hookEventName` (`writ/analysis/blackbox.py`), so a plain-text
payload carries no event, `_parse_payload` returns `{}`, `_event_name` returns `unknown`,
and the row is shaped exactly like the 68 rows this cycle deleted.

EXIT CODES. `blackbox_log`'s record schema carries `ts`, `hook`, `direction`, `session`,
`pid` and `payload`. The mechanism Claude Code actually reacts to on a Stop-hook block is a
non-zero exit with stderr, and that cannot be represented at all.

So the next author should give the record an `event` and an `exit_code` and bring plain
stdout and refusal exit codes in together, re-pricing alternative (b) at that point.

## Consequences accepted

**OUT coverage goes from 2 events to 3, not from 2 to 12.** `PostToolUse` joins
`PreToolUse` and `SubagentStart`, because four converted emitters run on it. The other
registered events stay in `directions_never_observed` afterwards, because Writ's hooks emit
NO envelope on them: they use plain stdout, a non-zero exit with stderr, or nothing at all.
Reading "OUT capture is now structural" as "the event gap is closed" is a misreading. This
cycle makes capture structural for the ENVELOPE mechanism and leaves the other two
measured, named and deferred.

**The funnel does not validate its payload.** Its contract is that the payload is a
`hookSpecificOutput` envelope. Validating that would cost a spawn on the reply path, and
the scanner enforces the inverse direction statically instead.

**The capture-off path adds no process, measured rather than argued.** Under
`strace -f -qq -e trace=execve`, sourcing `common.sh` alone and sourcing it then calling
`emit_hook_reply` with capture off produce the SAME two execve records on this machine
(bash itself, plus one `dirname` fork that `common.sh` already made at source time). The
existing ratchet in `tests/test_write_path_process_budget.py` is the standing check.

**The heuristic scan has a residue, and it is stated rather than implied.** An emission
whose capture cannot be decided from its own opening line, for instance an envelope printed
by a shell function defined elsewhere and called bare, is caught only if it lands in a
script that never reaches the funnel.

One earlier residue was closed rather than accepted, and how it surfaced is the point. The
first CAPTURED rule was the plan's wording: the opening line contains `NAME=$(`, `$(` or a
backtick anywhere. Review reproduced its hole instead of reasoning about it, by writing the
pre-conversion shape of two real lines into a scratch scripts directory and getting an empty
finding list. `WRIT_AC="[Writ: ... $(basename "$FILE_PATH")]" python3 <<'PY'` is an
environment-variable PREFIX feeding a separate command: the substitution's output goes into
the variable and the heredoc's output still goes straight to stdout, so nothing is captured.
CHECK 2 cannot see that either, because the script already reaches the funnel on some other
line, which is the ordinary state of every converted script. A converted script that gained
a new bare emission would have gone green on all three checks, which is the exact recurrence
this scanner exists to prevent. CAPTURED now means the emission's OWN output is consumed by
an assignment: the line opens with `NAME=$(` and the substitution either does not close on
that line or is followed by nothing. The three read-only uses the plan named still pass, and
`tests/test_hook_out_capture.py::TestBareEnvelopeEmissionScannerPrecision::test_an_env_var_prefix_holding_a_substitution_is_not_a_capture`
pins the shape as a finding.

**Two hooks still cannot join an OUT row to its IN row.** `writ/analysis/blackbox.py` joins
on `(hook, pid, session)`. `writ-pre-write-dispatch.sh` logs IN with no session argument
because `SESSION_ID` is not parsed until later in the file, and `writ-subagent-start.sh`
has the same split. Hooks that use `load_hook_env` get the join for free from the funnel's
session default; these two do not. Repairing it means moving the IN capture past the parse,
which is IN-side work that risks losing rows on an early-exit path, so it belongs to the
deferred IN-capture cycle.

**`validate-handoff.sh` is recorded, not fixed.** It echoes a bare JSON blob on
PostToolUse. It is not a `hookSpecificOutput` envelope, so it is outside this funnel's
population by definition, and PostToolUse plain stdout reaches only the Claude Code debug
log, so it is a delivery question rather than a capture question. It stays declared in the
fire-drill census as an `exit_nonzero_stderr` refusal, which is the mechanism that actually
carries its failure arm.
