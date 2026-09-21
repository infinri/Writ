# ADR: which layer owns "already seeded", and which way the fast path fails

Status: accepted
Date: 2026-09-21
Plan: `.claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md`
Touches: `writ/session/subagent_seed.py`, `bin/lib/common.sh`,
`hooks/scripts/writ-debug-code-gate.sh`, `hooks/scripts/validate-exit-plan.sh`,
`tests/_inventory.py`

## Context

Two defects closed together, because they are the same failure of reachability: a
sub-agent that Writ can see is a sub-agent Writ can govern, and in both cases it could not
see one.

FIRST, A DENIAL DISARMED THE SEEDER. `gates._log_gate_denial` counts a refusal inside
`with mutate_cache(session_id)`, and that context manager ends in an unconditional
`_write_cache`. `_read_cache` returns `_default_cache()` on a miss without writing
anything, so for an ungoverned sub-agent the DENIAL is the first thing that ever creates
its cache file. That file carries `denial_counts` populated and every other key at its
default: `mode` null, `cache_source` empty, `is_subagent` false. The bash guard was
`[ -f "$_cache_file" ] && return 0` and the python pre-lock guard was
`os.path.exists(_cache_path(agent))`, both of them testing FILE EXISTENCE where they meant
ALREADY SEEDED. So the refusal that proved an agent needed governance is what locked it
out of governance permanently.

SECOND, THREE TOOLS REACHED NO SEEDER AT ALL. The seeder was only ever called from
`load_hook_env`, and `load_hook_env` reads stdin. `writ-debug-code-gate.sh` (the only hook
registered for `Grep` and `Glob`) and `validate-exit-plan.sh` (the only hook registered for
`ExitPlanMode`) each run their own `$(cat)` before they source `common.sh`, and stdin
cannot be re-read. A sub-agent confined to those three tools never seeded a cache no matter
how many times it called them.

## Decision

### The rule lives in python; bash gets a skip predicate, not a second copy

`seed_subagent_cache` already held the correct test inside the lock
(`cache.get("cache_source") or cache.get("is_subagent")`), which is the shape of "already
seeded" rather than "a file exists". That expression is now the named predicate
`subagent_seed.already_seeded(cache)`, called at BOTH the pre-lock check and the in-lock
re-check, so there is exactly one python spelling of the rule. The pre-lock call reads the
cache only when the path exists, which keeps today's cheap common case (one json load, no
lock) and today's behaviour for a missing file.

The shell had to change too, and NOT because the rule belongs there. The shell guard
SHADOWS python: it returns before the interpreter is ever spawned, so a python-only fix
would never have run. Its job is a fast path, so it stays a fast path with the predicate
narrowed. `writ_cache_already_seeded <cache_file>` exits 0 only on a positive reading that
the file declares a non-empty `cache_source` or `is_subagent: true`.

The inheritance rules themselves (mode, current phase, approved gates, the parent link, the
role, the role scope, the clean operational state) did not move and were not copied. Bash
gained a SKIP PREDICATE, not a second copy of the rules, and the two spellings of that
predicate are held equal by `tests/test_subagent_seed_reach.py`, which runs both real
callers (the bash function through `bash -c`, the python function through an import) over
one corpus of eight cache documents.

### Uncertainty falls through to the seeder

`writ_cache_already_seeded` exits 1 on everything it cannot resolve: a missing file, an
unparseable file, a non-object document, a document with neither field, an absent `jq`, and
`WRIT_NO_JQ`. Unresolved falls through to the python seeder, which is the authority and
which declines cheaply.

This mirrors `writ_runtime_lens_check_required`, whose stated rule is that uncertainty pays
the full cost and never grants, applied to the other side of the same seam. Failing toward
SEEDING is the safe direction here for a reason that is worth stating plainly: a seeded
cache is marked `lazy_seed` and `gates._authority_mode` resolves that cache's mode as
ABSENT, so a redundant seed grants nothing, where a wrongly skipped seed leaves an agent
ungovernable.

The jq arm emits a SENTINEL rather than a truthiness verdict, for the reason the read-path
lens predicate records: `jq`'s `//` falls through only on null and false while python's
`or` also falls through on an empty string, and jq calls `0` truthy where python calls it
falsy. Emitting "the field was truthy" would leak that divergence across the seam. A
document that yields more than the one sentinel line (a JSON stream, a forged value) is not
the sentinel and falls through.

There is DELIBERATELY NO PYTHON FALLBACK ARM in `writ_cache_already_seeded`. It would pay
one python start to answer a strictly smaller question than the seeder answers for the same
price, so it would buy nothing. The cost this change adds is one jq process per hook inside
a sub-agent whose cache already exists, where today that state costs zero processes; a
jq-less host pays one python start instead. A main session pays nothing new, because
`writ_seed_subagent_from_fields` returns before any process when the agent id is empty or
equal to the session id.

### One entry point, reached by hooks that have already read stdin

`bin/lib/common.sh` splits the existing body into
`writ_seed_subagent_from_fields <agent_id> <parent_session_id> <agent_type>`, which holds
the guards, the fast path, the python spawn and the bash-side failure row exactly as they
stood. `_writ_seed_subagent_cache` is now a one-line wrapper passing `HOOK_AGENT_ID`,
`HOOK_SESSION_ID_RAW` and `HOOK_AGENT_TYPE`, so `load_hook_env`'s call site and its callers
are unchanged. The parent argument is the RAW session id, never the agent-preferring
collapsed id, because seeding needs both halves of the pair.

`writ-debug-code-gate.sh` already ran one `python3 -c` parse of `$STDIN_DATA` on every
Read, Grep and Glob and printed one collapsed id. It now prints three lines, `agent_id`,
`session_id` and `agent_type`, each coerced to the empty string when the value is not a
string and each stripped of newlines and carriage returns so a value cannot forge a line
and shift the positional split. Bash reads them with `mapfile -t`, a builtin, and explicit
`:-` defaults, then computes `SID="${AGENT_ID:-$RAW_SESSION_ID}"`, which is byte for byte
the identity rule that file already used. NET PROCESS CHANGE ON THIS HOOK: ZERO.

`validate-exit-plan.sh` holds the whole envelope in `$STDIN_JSON` and had no bash-side
field parse of its own, so it calls `parsed_fields`, the existing helper that takes JSON as
an argument rather than from stdin, then calls the same entry point. That is one added
process on a tool that fires once per plan.

BOTH CALLS SIT ABOVE THE SCRIPT'S FIRST EARLY EXIT, and that ordering is pinned by a
derived test rather than left to review: in `writ-debug-code-gate.sh` the call precedes
`[ -n "$SID" ] || exit 0`, and in `validate-exit-plan.sh` it precedes
`writ_require_session ... || exit 0` and the mode check below it. A future edit to either
guard cannot silently drop seeding ahead of it.

### denial_counts is carried across the seed

The cache being seeded over is not a fresh agent's: it has already been refused at least
once. `_CLEAN_OPERATIONAL_STATE` would reset `denial_counts` to empty, and that key is the
only one in it with an enforcement consequence, because the write gate escalates deny to
ask on a repeat count. So the seeder captures `denial_counts` from the locked cache before
applying the clean state and writes it back after, WITH NO BRANCH: a missing prior cache
yields an empty dict, which is exactly what the clean state already carried. Every other
key in the clean state is budget or telemetry and is reset as it is today.

The seed row gains one field, `seeded_over_existing`, so the census can measure how often
this path fires in the field instead of inferring it. That is the only telemetry change.

### What this cannot loosen

A lazily seeded cache is marked `lazy_seed`, `gates._authority_mode` resolves that cache's
mode as absent, and the sub-agent write bypass additionally requires `subagent_start`. The
write decision for a seeded agent is therefore identical to the decision for an agent with
no cache at all, reason string included, and closing these two holes cannot grant a write
anywhere. `tests/test_subagent_seed_after_denial.py` proves that for the one cache shape
the earlier cycle did not cover: a cache lazily seeded OVER a prior denial artifact.

It does bind the child to the parent's mode for RETRIEVAL and for the runtime read lens,
which is a tightening that already exists on the Read path today (`writ-read-junk-gate.sh`
seeds, and `writ-debug-code-gate.sh` then judges a seeded cache). Extending it to Grep and
Glob makes those tools consistent with Read rather than introducing a new class of denial.

## Alternatives rejected

SUPPRESSING THE DENIAL'S CACHE WRITE. `gates.py` is not touched. The count is what drives
escalation, so suppressing the write would trade one gap for another; and every other
`mutate_cache` writer keyed on an agent id (a rules update, a queried-file update) creates
the same file with the same shadowing effect, so the fix would have been partial. The guard
was wrong, not the write.

ALLOWLISTING ExitPlanMode. The coverage population is derived from `hooks.json`, so it
names every tool in a PreToolUse matcher. Today Grep, Glob and ExitPlanMode were the three
with no reaching script. A name-keyed exemption for the third would be the allowlist that
makes a population go blind, which this repo has paid for twice, so the gap was closed with
the same one-line call rather than excused.

EDITING `writ-pre-write-dispatch.sh`. It has the same stdin-already-consumed shape, but the
property being restored is PER TOOL, and Write, Edit and NotebookEdit are already reached
by `writ-state-write-gate.sh`, which calls `load_hook_env`. Editing the dispatcher would
mean changing both arms of the jq/python parse parity that
`tests/test_pre_write_parse_parity.py` holds, on the hottest gate path, for coverage that
already exists.

A PYTHON FALLBACK ARM IN THE BASH PREDICATE. Rejected on cost, above: it answers a smaller
question for the same price the seeder charges.

## Consequences

- A sub-agent whose first Writ-visible act is a refused write can still be governed by the
  next hook that runs inside it, instead of never.
- Grep, Glob and ExitPlanMode reach the seeder, so a sub-agent confined to any of them
  inherits a mode for retrieval and a parent link for the census.
- An agent's escalation record survives a seed, so a repeat denial still escalates.
- One jq process per hook inside a sub-agent whose cache already exists; zero new processes
  for a main session; zero new processes on the Grep/Glob path.
- No write decision changes anywhere, for any cache shape.
