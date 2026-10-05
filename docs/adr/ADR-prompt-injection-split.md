# ADR: split the per-prompt injection into four capped UserPromptSubmit hooks

Status: accepted
Date: 2026-10-05
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (program item 1a,
`docs/programs/knowledge-engine-program.md`)
Touches: `hooks/hooks.json`, `hooks/scripts/writ-rag-inject.sh`,
`hooks/scripts/writ-inject-always-on.sh`, `hooks/scripts/writ-inject-methodology.sh`,
`hooks/scripts/writ-inject-recall.sh`, `bin/lib/common.sh`, `bin/lib/writ-prompt-section.sh`,
`bin/lib/writ_friction_rows.py`, `writ/server/routes/query.py`, `writ/server/models.py`,
`writ/retrieval/injection_ceiling.py`, `writ/session/injection_state.py`,
`writ/shared/injection_text.py`, `writ/shared/budget.json`, `writ/shared/tokens.py`

## Context

The host caps each hook command's injected text at 10,000 characters. Over the cap it does
not truncate: it hands the model a file path plus a 2,000-character preview. Writ had one
UserPromptSubmit injection hook, `writ-rag-inject.sh`, which printed the always-on block,
the ranked rules, the methodology companion, the once-per-session recall briefing and all
of its control text (routing announcement, status line, mode reminders, nudge, escalation)
in one stdout. Measured at 10.4 to 17.2 KB per turn, so most turns reached the model as a
preview: the rules Writ exists to deliver were mostly not delivered.

Three facts about the host shape the fix. The cap is per hook COMMAND. The outputs of
several hooks on one event are concatenated. Hooks on one event run in PARALLEL, and the
concatenation order is not something Writ controls.

## Decision

### Four hooks, one section each

| Hook | Section | Also owns |
|---|---|---|
| `writ-rag-inject.sh` (name kept) | ranked rules (`rules_text`) | daemon autostart, session-id publish, skip check, mode auto-route and its announcement, post-compact directive and handoff line, orchestrator status line, mode directive, mode reminders, review-feedback push, escalation, backward context, proposal nudge |
| `writ-inject-always-on.sh` | `always_on_block` | nothing else |
| `writ-inject-methodology.sh` | `methodology_block` | nothing else |
| `writ-inject-recall.sh` | `recall_block` | nothing else |

All control text stays in the ranked hook, so a banner or reminder exists in exactly one
hook. The three section hooks share one body, `writ_prompt_section_main` in
`bin/lib/writ-prompt-section.sh`, and one friction-row emitter, `writ_bundle_friction`,
which the ranked hook calls too. The python arm of that emitter moved out of the hook into
`bin/lib/writ_friction_rows.py` so every hook runs one program.

### One route with a `sections` parameter

`POST /prompt-bundle` gains `sections` (any of `always_on`, `ranked`, `methodology`,
`recall`) and `reserve_chars`. Each hook names exactly one section. Omitted, `sections` is
the legacy trio (ranked, always-on, methodology) so direct callers keep their contract;
recall is opt-in so a legacy caller never spends the once-per-session briefing. The
response keeps its keys and adds `recall_block`, `nudge_text` and `skipped`. Each section
response carries only its own `*_meta`, so the existing friction builder emits exactly one
row per section with no edit to `bin/lib/friction-rows.jq`.

### A hard ceiling of 9,500 characters per hook, enforced server-side

`writ/retrieval/injection_ceiling.py` renders every section to fit
`PROMPT_CHAR_CEILING` (9,500, `prompt_char_ceiling` in `writ/shared/budget.json`) together
with the two framing characters a section hook adds, and within the section's own token
budget in characters (4 per token). 500 characters of headroom stay below the host's cap.

- Ranked rules degrade detail before rules: as retrieved; examples stripped; full to
  standard; summary; then rules dropped from the lowest-ranked end. The rendered header
  count always equals the recorded `rule_ids`. The ranked section renders into what
  `reserve_chars` (the control text the ranked hook already buffered, measured by the hook)
  and the nudge leave of the ceiling. The two nudge sentences moved server-side for that
  reason.
- Methodology drops pull rules before any floor rule, collapses floor rules to pointer
  lines before dropping any, and only then drops from the tail.
- Always-on collapses full rules to pointer lines from the tail, then drops rules behind an
  "omitted" line. With nothing shown and nothing over the limit its text is byte-identical
  to the old `render_always_on`.
- Recall is clamped at whole lines with a truncation marker.

Every hook also releases its output through `writ_emit_capped` in `bin/lib/common.sh`:
whole lines, never more than `WRIT_PROMPT_CHAR_CEILING` characters, ending with
`[Writ: output truncated at 9500 characters]` when it cuts. The ranked hook buffers its
whole output to a temp file and releases it from the exit handler. This bash backstop
catches a stale daemon, an oversize escalation history, or control text alone over the
ceiling. In the C locale bash counts bytes, which are never fewer than characters, so the
bound holds there too.

### Token budgets

| Section | Tokens |
|---|---|
| always-on | 2,000 |
| ranked | 2,300 |
| methodology | 2,000 |
| recall | 500 |
| reserved: document chunks (a future fifth hook) | 2,700 |
| total | 9,500 |

Ranked retrieval asks for `min(remaining_budget, 2300)` tokens. `apply_context_budget`
selects standard mode for 2,000 to 8,000, so the default turn and the capped turn both
retrieve standard mode with five rules; only the render ceiling is lower.

### Collapse repeats: an epoch-scoped shown record

The always-on rules and a mode's floor methodology rules repeat every turn. They render in
full once per EPOCH and as one `[ID] WHEN: trigger` line per rule after that, while their
ids are still recorded for citation validation. The epoch is the pair
(`compaction_epoch`, `current_phase`) (`writ/session/injection_state.py`):
`cmd_reset_after_compaction` increments the counter and every phase write changes the
phase, so both boundaries reset the record without any phase writer knowing the module
exists. Outside work mode `current_phase` stays None and only the counter moves, which is
why the reset is correct in every mode. Only rules rendered in FULL are marked shown; a
rule the ceiling collapsed or dropped on its first turn renders in full on the next turn
that has room. A mark-shown write computed from a snapshot of an epoch that is no longer
current is dropped.

## Ordering analysis: what the parallel hooks may not depend on

Parallel hooks can neither read each other's stdout nor rely on a session-cache write
another hook makes in the same turn. The single script had these dependencies:

1. **Mode auto-route set the mode before the bundle call.** Always-on mode scoping and the
   methodology query source depend on that mode. The routing decision is now a pure
   function of (prior mode, mode_source, prompt hint), `writ_route_decision`, which the
   ranked hook acts on and the section hooks use through `writ_turn_mode` to predict the
   mode this turn runs under. The prediction is idempotent with respect to the write: a
   section hook that reads the cache after the ranked hook's `mode init` sees prior equal
   to the hint and predicts the same mode. Residual race: a `mode init` or `mode switch`
   that fails leaves one turn where the sections used the predicted mode. Accepted.
2. **Budget accounting.** Every section request reads its own snapshot at the same
   pre-turn point, so the methodology gate (`remaining_budget > 600`) and the ranked budget
   see the value they saw before; each charge is applied inside `mutate_cache`, which is
   flock-serialized across threads and processes, so decrements still compose.
3. **The exclude list.** Methodology ids added this turn exclude ranked hits only on the
   next turn, as before.
4. **The ranked render needs this turn's always-on ids** (`tag_overlap`). The ranked
   request computes them itself with the same read-only `always_on_bundle` call and the
   same renderer, so it does not depend on the always-on hook having run. The ranked
   pointer reads `SEE: [ID] in the ALWAYS-ACTIVE RULES block`, no longer "above".
5. **recall_briefed** is read and set only by the recall section, server-side, on the
   snapshot the recall request took. Turns are sequential, so one turn's recall request
   cannot race another's.
6. **post_compact_pending** is read and cleared only by the ranked hook. The collapse reset
   does not use it (it would race the clear); it uses `compaction_epoch`.
7. **The orchestrator flag** matters only to the ranked section (`include_ranked=false`);
   the section hooks deliver their sections to a master, as before.
8. **The skip check.** The ranked hook keeps its `/prompt-state` skip exit. A request with
   explicit `sections` is skipped server-side by `should_skip_cache`, the same rule
   `cmd_should_skip` applies, returning `skipped: true` and no text.
9. **Daemon autostart** stays in the ranked hook only. Section hooks fail open when the
   daemon is down, so on the one turn after a cold daemon start only the ranked hook's
   output arrives.

Concurrency is proven against a real cache, not a mocked `cmd_update`:
`tests/test_injection_collapse.py::TestConcurrentSections` gathers four section requests on
one session (their writes come from different threads) and runs several OS processes that
write `--mark-shown` at once, asserting no write is lost.

## Query budget

Per turn: the ranked request runs the pipeline (in-memory indexes) plus `always_on_bundle`
(two read-only, indexed graph queries); the always-on request runs `always_on_bundle`
again; methodology reads the in-memory trigger index; recall runs one project resolution
plus the recall compile, first turn only. Worst case four always-on queries per turn, two
more than before, in parallel requests so they add no wall-clock latency. An orchestrator
master skips ranked retrieval and its overlap read. Session-cache writes per turn are
unchanged in count (ranked 1, always-on 1, methodology 1, recall 1 on turn one); the recall
write moved from a CLI process into the daemon. No caching was added; a short-lived
in-process memo of the always-on rows is the fallback if contention is measured.

## Failure and fallback

- Daemon down: the ranked hook autostarts as before and prints
  `[Writ: server unavailable, proceeding without rules]` if the call still fails; section
  hooks print nothing and exit 0.
- Error response: the ranked hook prints `[Writ: query failed, proceeding without rules]`;
  section hooks print nothing.
- A stale daemon that ignores `sections`: output is still correct and not duplicated,
  because each hook prints only its own field. Until restart each request runs the legacy
  trio, so budget and always-on counters are charged up to four times per turn, and no
  recall briefing is shown (the flag is not set, so it appears after the restart).
- No exit trap (a script that is not instrumented): no buffer and no bash backstop for the
  ranked hook; the server ceiling still bounds `rules_text` against a reserve of 0.

## Alternatives rejected

- **Four routes, one per section.** Four request models and four response shapes would
  fork the friction builder, `parsed_fields` and every stub daemon in the suite, and move
  the route count every inventory test pins. One route with `sections` keeps one shape,
  and an old daemon degrades safely by construction.
- **Renaming the ranked hook.** Dozens of test modules, the docs and the inventory derive
  facts from `writ-rag-inject.sh`; renaming it would move the blast radius without buying
  anything.
- **Autostart in every hook.** Three more autostart attempts per turn, three more entries in
  the daemon-launch inventory, for the one turn after a cold start.
- **`post_compact_pending` as the collapse reset signal.** The ranked hook clears it during
  the same turn the section hooks would read it, so the reset would race the clear. A
  counter only ever moves forward and is read from the request's own snapshot.
- **Truncating in bash only.** A bash cut cannot know which lines are a rule's examples and
  which are its id, so it would drop rules before detail and desynchronize the recorded ids
  from what was shown. The server renders to the ceiling; bash is only the backstop.

## Consequences

- Rules now follow the reminders in the ranked hook's output (routing announcement,
  post-compact directive, status line, mode directive, mode reminder, review push,
  escalation or backward context, WRIT RULES, nudge). Across hooks the order is the host's
  concatenation order, so no consumer may rely on it.
- An escalation turn still receives its ranked rules: escalation no longer exits the hook.
- The review-feedback push keeps its own `/methodology-companion` call in the ranked hook
  and can repeat a node the methodology hook pulls by keyword in the same turn
  (pre-existing; open question).
- The collapse record is written when the server builds the response, before the hook
  delivers it. If the hook's HTTP timeout fires or the bash backstop truncates the block,
  rules are marked shown that the model never saw, and they stay pointers until the next
  compaction or phase change. Accepted: the pointer still names the rule and its trigger.
- Item 1c (compaction clears only `by_phase[current_phase]`) is not fixed here; the
  collapse record does not depend on it.

## Measured output sizes (operational)

To be recorded from a live session with a long prompt, one row per hook, confirming no
hook's output was replaced by a file preview:

| Hook | Characters |
|---|---|
| `writ-rag-inject.sh` | pending live measurement |
| `writ-inject-always-on.sh` | pending live measurement |
| `writ-inject-methodology.sh` | pending live measurement |
| `writ-inject-recall.sh` | pending live measurement |
