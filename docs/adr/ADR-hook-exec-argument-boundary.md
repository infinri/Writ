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
- **The census of call sites is dated, not final.** As of this cycle the search over
  `hooks/scripts/*.sh` for `python3` invocations found ONE other site with this shape:
  `writ-subagent-stop.sh` passes the sub-agent's own session cache as `argv[1]` and READS
  it, so removal is not its fix. That cache is the same schema that grew the parent to
  181 KB (`loaded_rules`, `token_snapshots`, `files_written`), so it is not bounded by
  construction; it is created clean at spawn and lives for one dispatch, and no instance of
  it crossing the cap has been observed. It is named here and left alone deliberately: its
  fix is a different edit against different telemetry with its own tests. Every other match
  passes its payload on stdin or passes bounded values.
- **Restoring the seeding makes dormant controls live.** `ENF-ROLE-SCOPE` requires
  `is_subagent` true AND `cache_source == "subagent_start"`, so it begins applying to
  long-session dispatches for the first time. That is identical to what a short-session
  dispatch already got; what grows is the population, not the policy.
