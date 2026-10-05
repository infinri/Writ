# ADR: four silent retrieval and session-state fixes (program items 1b to 1e)

Status: accepted
Date: 2026-10-05
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (program items 1b, 1c,
1d and 1e, `docs/programs/knowledge-engine-program.md`)
Touches: `writ/server/routes/query.py`, `writ/server/routes/gate.py`,
`hooks/scripts/writ-cwd-changed.sh`, `hooks/scripts/writ-postcompact.sh`,
`writ/session/{budget_tracking,cache,injection_state,session_lifecycle}.py`,
`bin/lib/writ_phase_scoped_rules.py`, `writ/retrieval/{pipeline,trigger_index}.py`,
`writ/graph/schema.py`, `writ/graph/integrity/_common.py`, `benchmarks/bench_targets.py`,
`scripts/measure_retrieval.py`

## Context

Each of these made Writ deliver fewer rules than it should, with no error anywhere.

- 1b: changing directory into a project with a marker file tagged the session with a
  language, and the ranked request passed it as an exact domain filter. No rule domain is a
  language, so the ranked channel returned nothing for the rest of the session.
- 1c: compaction cleared only the current phase's exclusion bucket. Outside work mode there
  is no phase, the exclusion fell back to the flat `loaded_rule_ids`, and that was never
  cleared, so rules shown before compaction never came back.
- 1d: the no-good-match gate read the best raw cosine of the top 10 vector hits before the
  filter removed excluded, out-of-domain and out-of-scope hits, and only 10 hits ever reached
  the filter.
- 1e: two hand-written methodology label lists disagreed on ForbiddenResponse, with no test
  saying whether that was intended.

## Decisions

### 1b. A detected language is not a domain filter, and is no longer cached

The ranked request carries no `domain`. The cache field had exactly one reader, that
request, so the hook stops writing it and the `--set-detected-domain` flag and the cache
default are deleted. Detection stays, because the hook's `cwd_changed` metrics row records
it. Rejected: a soft language boost, because no rule carries a language today, so there is
nothing to boost.

### 1c. Compaction empties the exclusion in every mode; the cumulative record stays

`loaded_rule_ids` served two roles: the exclusion outside a work phase, and the record of
everything the session was shown, read by citation validation, auto-feedback, the handoff,
coverage and the session-end metrics. Clearing it would make a rule cited after a compaction
read as hallucinated (spending an approval) and lose feedback for pre-compaction rules. So
the roles are split: `rule_ids_since_compaction` is the exclusion outside a work phase,
`None` until the first compaction (then `loaded_rule_ids` is the exclusion, exactly as
before, so live sessions need no migration), `[]` after each compaction, unioned by
`--add-rules`. One selection function, `injection_state.retrieval_exclude_ids`, replaces two
inline copies, and the stdlib hook helper mirrors it under a parity test. Rejected: clearing
`loaded_rule_ids` (breaks the readers above); keying an "unphased" bucket in
`loaded_rule_ids_by_phase` (changes what work-to-conversation switches exclude, which is not
this item's concern).

### 1d. Filter before the gate; 50 vector candidates

The gate reads the best raw cosine among filter survivors, 0.0 when none survive.
`VECTOR_CANDIDATE_LIMIT` is 50, equal to the index's search beam, so search cost is unchanged.
The surviving top is never above the unfiltered top, so the false-injection rate can only
hold or fall; gated recall can fall where the filter removes every strong hit. Measured
before and after on the isolated graph (`benchmarks/VECTOR-GATE-2026-10-05.md`): every verdict
is no decision, so 1d ships as a correctness fix with no quality claim. Ungated metrics are
flat. On the gated arm MRR@5 (ambiguous set) fell 0.6124 to 0.5663 (overlapping intervals,
sign test 1 win / 4 losses, p=0.375): a few gold queries now abstain because Stage 1 filtering
removes their top raw hit. Gated false injection fell 0.30 to 0.20, which is 6 to 4 of only 20
negative queries and has no interval, so it is suggestive only. The 0.30 threshold was tuned
on unfiltered hits and is not retuned here despite the gated MRR movement; retuning it is
open work for item 1f.

### 1e. One source for the label lists, with the one difference named

The difference is intended: every ForbiddenResponse is injected by the always-on channel each
turn and ranked in Channel 1 (decision D1 of the 1.7 cutover), so the Channel 2 trigger index
leaves it out to avoid a second delivery. `writ/graph/schema.py` now owns
`RANKED_METHODOLOGY_LABELS`, `CHANNEL1_ONLY_METHODOLOGY_LABELS` and the derived
`TRIGGER_INDEX_METHODOLOGY_LABELS`; the pipeline, the trigger index and the integrity
module's Cypher literal read them. `node_scope.DOCTRINE_NODE_TYPES` stays a written-out
literal on purpose (deriving it would make a new label readable by every project).

## Consequences

- A session that changed directory gets ranked rules again.
- After compaction, in any mode, previously shown rules are eligible on the next turn; the
  citation, feedback and coverage records are unchanged.
- `abstain_signal` now means the best surviving cosine, on both the abstained and the normal
  path; threshold tuning from telemetry reads it that way from this release on.
- Adding a methodology label is one edit in `writ/graph/schema.py`, and excluding one from
  the trigger index is an explicit, tested entry in `CHANNEL1_ONLY_METHODOLOGY_LABELS`.
