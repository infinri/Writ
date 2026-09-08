# ADR: an unbounded value never crosses an exec boundary, in argv or in env

Status: accepted (plan `dfacff61-23d5-474e-846c-2e2f0f0ea482`, 2026-08-29)

## Context

`hooks/scripts/writ-subagent-start.sh` handed the parent session's ENTIRE cache JSON to
three embedded `python3` blocks as `argv[1]`. One of the three, the block that seeds the
sub-agent's cache with its inherited mode and gates, never read that argument at all: the
inheritance rules had moved into `writ/session/subagent_seed.py`, which re-reads the parent
cache itself from the bounded session id it is handed, and the parameter that fed the old
inline implementation was left in the call.

Linux caps a SINGLE argument string at `MAX_ARG_STRLEN`, 32 pages. Measured on this machine
rather than assumed:

    derived MAX_ARG_STRLEN (32 * SC_PAGE_SIZE): 131072
    argv string of 135168 bytes: refused, errno 7 E2BIG
    env  string of 135168 bytes: refused, errno 7 E2BIG

The live parent cache measured 181,529 bytes. So `execve` failed before python started, and
`2>/dev/null || true` on the seed call swallowed it. The hook continued, `_writ_session
update` later created the cache from defaults, and the sub-agent ran with `mode: null`,
`is_subagent: false`, `cache_source: ""`. Both controls that depend on those fields, the
role write-scope boundary from cycle M and the `[ENF-GATE-MODE]` write refusal outside the
skill directory, were silently inert on every dispatch from a long session.

Driving the real hook against a 181,528-byte parent cache reproduces it exactly, and the
same run against the fixed hook is the counter-example:

    before  exit 0   child cache: mode=None  is_subagent=False  cache_source=''
                     injected context: [Writ sub-agent: isolated session]
    after   exit 0   child cache: mode='work' is_subagent=True cache_source='subagent_start'
                     injected context: [Writ sub-agent: mode=work, phase=implementation,
                                        gates=phase-a,test-skeletons]

The failure is THRESHOLD-TRIGGERED BY SESSION AGE. It works while the parent cache is small
and stops once it crosses the cap, so it concentrates in exactly the long sessions where
sub-agents are used most.

## Decision

**A value whose size is not bounded by construction never crosses an exec boundary.** The
process on the far side receives an identifier and reads the value itself, in process, from
the same authoritative source the caller would have read.

Three properties follow, and each one is a rule rather than a preference:

1. **`env` IS NOT AN ESCAPE HATCH.** The measurement above shows `MAX_ARG_STRLEN` applies to
   each environment string exactly as it applies to each argument, so moving an unbounded
   value from `argv` to `env` moves the same defect one line over. This cycle DID move four
   values onto `env` (`WRIT_SA_AGENT_ID`, `WRIT_SA_ROLE`, `WRIT_SA_ROLE_SOURCE`,
   `WRIT_SA_PARENT`): an agent id, a role name, a source label, a session id.

   **BOUNDED IN PRACTICE IS NOT BOUNDED, and this ADR said otherwise in its first draft.**
   Those four are short in every real dispatch, but only the two ids are bounded by anything
   structural (`_VALID_ID` caps them at 128 characters). `agent_type` arrives from the
   payload and is NOT constrained anywhere: this cycle's own adversarial test constructs a
   200,000-character role, which is precisely why the trailing `subagent_start` row now
   truncates the value it logs. Say "bounded" only where something enforces the bound, and
   name the enforcer. Everywhere else, truncate at the boundary or send it on stdin.

   THE NARROW CASE IS WHAT MAKES THIS MATTER rather than being pedantry: because a JSON
   wrapper adds around 50 to 90 bytes, a value can sit UNDER the cap for one exec and OVER
   it for the next. A role name in that band seeds successfully, writes no
   `subagent_seed_failed` row, and still loses its `subagent_start` row, leaving a genuinely
   governed dispatch counted as unreachable. A limit that two call sites straddle is worse
   than one they both exceed, because the failure becomes selective and therefore invisible.
2. **A payload that must cross goes on STDIN, never on the command line.** A heredoc or a
   pipe has no per-string cap. This is already the majority pattern in `hooks/scripts/`.
3. **THE DECISION BLOCK'S OUTCOME IS OBSERVED.** `2>/dev/null || true` on a block that
   decides something is what turned a hard `execve` failure into two cycles of invisible
   non-enforcement. The block prints a status word, the caller captures it, and an EMPTY
   status is the signal that the block did not run to completion. Nothing is inferred from a
   missing telemetry row: the failure writes its own positive record,
   `subagent_seed_failed`, carrying only bounded fields, because a row built from the value
   that killed the block would die exactly where the block did.

## Amendment, 2026-09-08: the two write doors and the worktree gate

Same plan id, second cycle. The decision above was not re-opened; three sites were brought
into compliance with it after the user measured both write doors switching off, silently,
above roughly 128 KiB:

    Bash door, credential probe:   131,014 chars DENY -> 131,060 chars SILENT, rc 0
    Write door, credential path:   120,000 bytes DENY -> 131,000/140,000/300,000 SILENT, rc 0

Each of the three had the same three-part shape as the sub-agent seed: a value whose size
nothing bounds crosses an exec boundary, the failure of that exec is discarded, and the
consumer reads the resulting EMPTY value as a decision, which in all three cases was an
allow. The Write door was the worst of them, because by the failing line the server had
ALREADY decided and written its `write_attempt` row: the process that died was only
translating that verdict into the reply envelope, so a gate that ran and refused was
reported to the model as an allow.

**The transport, per site.** Property 2 prefers stdin, and only one of the three could take
it:

- `writ-bash-write-gate.sh` (the extractor, ~1,900 lines) and `writ-worktree-safety.sh`
  (~600) carry their PROGRAM on stdin as a quoted heredoc, so the payload cannot use it.
  The command now goes to a `mktemp` file and the PATH crosses (`WRIT_BASH_CMD_FILE`,
  `WRIT_WT_CMD_FILE`), which is bounded by construction from a fixed template: that
  template is the enforcer this ADR asks to be named. Costs accepted and stated: the
  command text lands on disk transiently (mode 600, unpredictable name, removed through
  `writ_on_exit` rather than a second EXIT trap), and each gated command pays two more
  processes, one `mktemp` and one `rm`. Measured: a command that misses the prefilter pays
  nothing (5 execve before and after); a command that reaches the extractor goes 21 -> 23.
- `writ-pre-write-dispatch.sh`'s translator is a `python3 -c` program, so STDIN IS FREE and
  no file is needed. `RESULT` and `CHECK_BODY` cross as two NUL-separated records
  (`printf '%s\0%s' ... | python3 -c`). NUL and not newline: `CHECK_BODY` is lines 2 onward
  of the parse joined by newlines, so it can legitimately contain one, while neither value
  can contain a NUL (both arrived through command substitution, which strips them). The
  emission was verified with `od -c`, not assumed.

Moving the extractor's program to a file would delete the temp file and is the right end
state; it is deferred because seven test modules slice that program out of the hook by its
heredoc marker and one compares its mirror block textually against
`writ/session/bash_tokens.py`, which is a structural refactor of the most heavily pinned
file in the repo. Passing the ~100 KB program on argv instead is this ADR's "bounded in
practice" trap at its worst.

**Property 3, made concrete.** `[ -z "$TARGETS" ] && exit 0` conflated two states that must
never be conflated: "the decision block ran and found nothing", the hot path for almost
every command, and "the block did not run", the defect. Both Bash-side blocks now print
`status<TAB>complete` as their LAST line and their consumers are three-way: sentinel with
rows in front of it (strip it, decide as before), sentinel alone (exit 0, silently), no
sentinel (fault). The sentinel is STRIPPED once rather than ignored per consumer, because
five arms key on the first TSV column and two read a row positionally. The deliberate
fail-open on unbalanced quotes prints the sentinel BEFORE exiting, because it is a
completed decision; without that print every quote-unbalanced command would ask. The Write
door needs no new sentinel: its decision line IS the status, and
`DECISION="${DECISION:-allow}"` was exactly the line that erased it.

**The posture on a fault, which is not the same as the posture on an outage.** A decider
that could not run ASKS rather than allowing. This is not a new posture: the Bash gate
already asks in exactly this epistemic state when a target's expansion cannot be resolved
or a destination cannot be named. Non-blocking is still the rule for infrastructure
faults, and every fault writes two positive records: one `[WRIT CRITICAL]` line on stderr,
which needs no interpreter, and one `gate_decider_incomplete` row on the AUDIT stream
carrying `hook` and `stage` only, both source literals, for the reason property 3 already
gives. One helper (`writ_decider_fault` in `bin/lib/common.sh`) owns all three sites.

**A machine with no `python3`, said out loud.** Each fault path probes once with the
`command` builtin (no fork) and keeps the two states apart: interpreter present and the
block incomplete asks; interpreter ABSENT allows, exits 0 and prints exactly one
`[WRIT CRITICAL]` line naming that no Writ Bash or write decision can be made on this
machine at all. Asking there would be worse than dishonest, because every Writ decision
path is interpreter-bound, so an ask would imply a protection that does not exist while
making write-shaped commands unusable. The probe is also what keeps the ask path itself
from going silent: `emit_ask` cannot build its envelope without an interpreter, and
`emit_hook_reply` returns 0 on an empty payload.

**Two dependency edits in `bin/lib/common.sh`, not opportunistic cleanup.** `emit_deny` and
`emit_ask` passed their reason as an ENV string, and the Bash gate's reasons embed extractor
row values that `flat()` collapses but does not truncate. Those reasons were unreachable at
size only while the extractor died first, so fixing site 1 made them reachable and would
have moved the silence one layer down. The reason now rides stdin. Separately `_gd_emit_now`
crosses the reason and target as env strings with `|| true` behind them, which would have
dropped the AUDIT ROW for a denial while the denial still reached the model; that one is
fixed by TRUNCATION rather than transport, at a named bound (`WRIT_GD_FIELD_MAX`, 4,000
characters, at most 16 KB in the worst multibyte case against a 131,072-byte cap), because
the user-facing text and the evidence row want opposite answers to the same limit.

**One measurement that was not in the plan and is now fixed too.** `curl -d "$body"` puts
the body on ARGV, so even with the hook side fixed a large write could never reach the
daemon: measured, `curl -d` refuses a 132,000-byte value ("Argument list too long", rc 126)
and accepts 130,000. Both write-decision call sites in `_writ_session` (`pre-write-check`
and `can-write`) now pass their body on curl's stdin with `--data-binary @-`. The other
`-d "$..."` sites in that file were checked: `writ_http_post`, the generic POST tail and
the `format` arm carry bodies built in place from short literals or a bounded /query
response, and none of them decides a write.

## Alternatives rejected

- **Truncate the parent state before passing it.** Rejected: it keeps an argument that has
  no reader, and it makes correctness depend on a cutoff nobody can justify. For the two
  blocks that DO read the value it would silently corrupt the parse.
- **Raise a limit.** Rejected: `MAX_ARG_STRLEN` is a compile-time kernel constant, not a
  tunable, and a fix that needed it to move would be a fix that cannot ship.
- **Pass the cache through a temporary file.** Rejected for the seed block because the
  argument had no reader at all, so this trades a size bug for a filesystem lifecycle bug in
  service of a value nobody wants. Rejected for the other two because the reader can open
  the real cache directly and the temp copy is a second source that can disagree with it.
- **Keep an empty placeholder at `argv[1]` so the later indices do not shift.** Rejected: it
  preserves exactly the shape that caused the bug, an argument whose only job is to hold a
  slot, and every future reader has to be told why it is there. Named env variables delete
  the index hazard instead of navigating it. Renumbering four positional indices by hand was
  rejected for the opposite reason: a wrong shift swaps the role with the parent session and
  produces a quieter version of the same silent failure.

## Consequences accepted

- **Two python spawns become one fewer.** The `python3 "$SESSION_HELPER" read` that produced
  the parent state is deleted; the two blocks that consumed it read the cache in process
  through `writ.session.cache._read_cache`, which is literally what `cmd_read` printed
  (`json.dump(_read_cache(session_id))`), so the file-direct property those blocks depend on
  is preserved rather than restored.
- **A quoted heredoc changes what interpolates.** The seed block was a double-quoted
  `python3 -c "..."` whose comments carry markdown backticks around `cache_source` and
  `envelope`; bash ran them as command substitutions on every dispatch
  ("cache_source: command not found"). `<<'PY'` makes backticks, `$` and apostrophes inert,
  and the price is that `$WRIT_DIR` no longer interpolates either, so it rides the same env
  channel. A heredoc feeds the PROGRAM on stdin, so this form is available only to a block
  that reads no stdin data of its own.
- **The census is DERIVED, not written down here.** The prose that stood in this bullet
  said the search over `hooks/scripts/*.sh` found ONE other site with this shape and that
  "every other match passes its payload on stdin or passes bounded values". Both halves
  were false within one cycle: the real population is dozens of sites, and verification
  added two that no hand-written list had named, including one (`common.sh`) that this
  sentence's own `hooks/scripts/*.sh` scope could not see. A prose census rotted in a
  cycle, so it is replaced by a mechanism and a ratchet:

  `tests/_inventory.py::exec_boundary_payload_sites(scripts_dir=...)` derives, per script,
  the `python3` invocations that carry a PAYLOAD-DERIVED value on argv or in env, where
  payload-derived means the value flows from the hook's payload interface (`HOOK_ENVELOPE`,
  `HOOK_COMMAND`, `HOOK_FILE_PATH`, or a bare `$(cat)` of stdin) into the crossing string,
  directly or through a chain of assignments. `tests/test_exec_boundary_census.py` holds
  that population against a MAP of site to (status, reason), so a site that DISAPPEARS, or
  one whose status says `fixed` while the detector still sees it crossing, reddens a test
  instead of sitting stale until someone re-derives it by hand. Statuses are `fixed`,
  `open` and `bounded` (with the enforcer named). The detector is proved conditional
  against five adversarial fixtures rather than assumed to work.

  Its one stated limit: a value that crosses only inside an UNQUOTED heredoc's own nested
  command substitution is not traced, because heredoc bodies are skipped uniformly. That
  is exactly the shape of the site named next.

- **Sites left open after the 2026-09-08 amendment, each with its reason.** These are
  recorded, not fixed, and the census map is where their status lives:

  - `writ-memory-policy-guard.sh` is THE RECOMMENDED NEXT SITE, and it is a live
    decision-flipping fail-open rather than a latent one. The crossing value is the memory
    file's content, model-controlled and unbounded, and the shape is worse than the class
    above: an inner `python3 -c` receives the content as argv and its output is
    INTERPOLATED INTO THE OUTER PROGRAM TEXT, so over the cap the inner call yields
    nothing, the outer program becomes `content = ` (a syntax error), the match is empty
    and the memory write is allowed. Measured 2026-09-08 with the same rule-weakening
    phrase in both payloads: 200 bytes DENY, 200,000 bytes SILENT. It was excluded from
    the amendment because a program spliced from another program has to be restructured,
    the same shape appears twice in the file, and mixing that into a transport swap would
    double the cycle.
  - `writ-state-write-gate.sh` is the same class with no consequence, and the enforcer is
    nameable: the only unbounded value crossing is the TARGET PATH, and a path long enough
    to break `execve` (over 131,072 bytes) is far past the kernel's `PATH_MAX` of 4,096,
    so the write it would allow cannot land.
  - `common.sh`'s daemon-down `can-write` body fails CLOSED over the cap, not open: the
    `skill_dir` fold-in does not happen and the body falls back without it, losing an
    EXEMPTION. Recorded because the retired sentence claimed no such site existed.
  - `writ-bash-write-gate.sh`'s pytest venv swap crosses the whole envelope in env. Over
    the cap the swap silently does not happen and pytest runs on the system interpreter: a
    convenience regression, no write decision changes, the arm exits 0 either way.
  - `writ-pre-write-dispatch.sh`'s fallback `PARSED_INPUT` parse is held open
    DELIBERATELY, and it looks like the same fix, which is why it is spelled out. It reads
    its payload as argv and has the same cap, but it runs only when jq is absent or
    failed, and closing it means deciding what a hook does when it cannot parse its own
    envelope AT ALL, which is a different question from "the decider could not run" and
    needs its own tests against the jq/python parity pins in
    `tests/test_pre_write_parse_parity.py`.
  - `writ-subagent-stop.sh` passes the sub-agent's own session cache as `argv[1]` and
    READS it, so removal is not its fix. That cache is the same schema that grew the
    parent to 181 KB; it is created clean at spawn and lives for one dispatch, and no
    instance of it crossing the cap has been observed.
  - The crash class (`writ-dispatch-discipline.sh`, `validate-design-doc.sh`,
    `writ-rag-inject.sh`) contradicts its own "always exit 0" headers, which is a defect,
    but a non-zero hook exit is VISIBLE as a hook-error notice, which is the compensating
    control `docs/reference/session-and-gates.md` section 8 names. Noise and correctness
    debt, not silent non-enforcement.

  `WRIT_STRICT` was deliberately NOT extended to any of these. It governs only the
  daemon-down `can-write` fallback today, and adding a strict-deny arm here would be a new
  posture rather than compliance with an accepted one.
- **Restoring the seeding makes dormant controls live.** `ENF-ROLE-SCOPE` requires
  `is_subagent` true AND `cache_source == "subagent_start"`, so it begins applying to
  long-session dispatches for the first time. That is identical to what a short-session
  dispatch already got; what grows is the population, not the policy.
