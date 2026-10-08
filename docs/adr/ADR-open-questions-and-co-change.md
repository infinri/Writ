# ADR: open questions and co-change hints before a write

Status: accepted
Date: 2026-10-08
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (knowledge-engine program items 7a and 7d)
Touches: `writ/graph/schema.py`, `writ/graph/db/_common.py`, `writ/graph/db/schema_store.py`,
`writ/graph/db/record_store.py`, `writ/graph/db/edge_store.py`, `writ/authoring.py`, `writ/cli.py`,
`writ/session/gate_token.py`, `writ/session/write_context.py`, `writ/session/injection_state.py`,
`writ/server/routes/gate.py`, `writ/shared/logging.py`
Siblings: `docs/adr/ADR-trust-records.md` (the approval guard this extends) and
`docs/adr/ADR-decision-recall.md` (the pre-write decision card this sits beside).

## Context

An agent that hits an unknown (does the cache expire between deploys, who owns this retry budget)
had nowhere to put it. It either guessed, or asked in chat, where the question was lost at the
next compaction and never reached the next session that touched the same code. The rules and
decisions the unknown bears on are already in the graph, so the question belongs beside them.

Separately, the commit history Writ already harvests (Commit and FileChange records) knows which
files usually change together. A write to one of them is the moment that knowledge is useful, and
the pre-write route already reads the graph for the decision card.

Both surface in the same place: the `decision_context` string `/pre-write-check` returns on an
allowed write, which the dispatch hook already appends to the file-context rules.

## Decision

### 1. OpenQuestion is a record, linked by ABOUT

`OpenQuestion` joins `RECORD_LABELS` and `RECORD_ID_FIELDS` (id field `question_id`), outside
`NodeType`, `NODE_TYPE_MODELS` and `NODE_ID_FIELDS`. Preserve-on-wipe, the dump import's preserve
set and the node and edge dump exclusion follow from `RECORD_LABELS` with no second list. A new
record edge type `ABOUT` (in `ALLOWED_EDGE_TYPES` and `RECORD_EDGE_TYPES`, so `CORPUS_EDGE_TYPES`
never offers it to `writ add`) links a question to each Rule and Decision that depends on it,
written by `wire_about`, a thin `create_record_edge` wrapper that refuses any other target label.
Reconcile cannot prune it: its source endpoint carries `provenance="record"`.

`openquestion_question_id_project_unique` is the `_create_record` MERGE race guard, and the
`openquestion_project_status` index backs the pre-write read.

The question id is `OQ-` plus 10 lowercase hex (`QUESTION_ID_PATTERN`). It has no ':' and can never
match `RULE_ID_PATTERN`, so the qualified approval bindings `answer:<id>` and `close:<id>` can never
equal a bare question id or any rule binding.

### 2. Identity lives on the question node

The resolver's identity (`resolved_os_login`, `resolved_git_name`, read from lines 6 and 7 of the
approval token) is stored on the OpenQuestion itself. The node is a record label, so the identity
is local and never dumped, the same property TrustEvent has. The opener carries no identity:
identity only ever comes from a token, never from agent arguments.

### 3. One guard, generalized by target kind

`writ question open` needs no token: opening asserts nothing, like proposing an ai-provisional
rule. `writ question answer` and `writ question close` follow `writ review --dispute`: without
`--token` they surface the question and record the binding; with it they act. The guard is
`_authorize_rule_action`, generalized in place with a `kind` (`rule` or `question`) rather than
copied. One small table, `_ACTION_TARGETS`, holds what varies by kind: the noun in messages, the
audit field naming the target (`rule_id` or `question_id`), the `event_target` prefix (`review_` or
`question_`) and the surfacing and re-run commands. The step order is unchanged; step 2's legality
check is a question being open (a rule being ai-provisional for promote). For kind `rule` every
message, event name and field is byte-identical to item 6.

The refusal classes are the existing ones (`agent_self_approval_blocked`, `gate_token_unbound`,
`rule_promotion_gate_bound`, `gate_token_rule_mismatch`, `rule_promotion_claim_lost`), told apart by
`event_target` `question_answer` or `question_close`. The session cache field stays
`pending_review_rule_id`, which the approval mint passes through opaquely, so no hook changes.

The settle write is one conditional SET that applies only while the status is `open`, so a
question resolved concurrently is never overwritten; if it loses that race after the claim, the
approval is spent, as item 6 documents for a post-claim graph failure. Answered and closed are
terminal.

An answer approval binds the action and the question id (`answer:<id>`), not the answer text,
as item 6's dispute and verify approvals bind the action and rule id but not the note.

### 4. Three blocks, one coroutine, one string

`write_context` runs the decision card (the existing `write_decision_context`, unchanged), the
open-question block and the co-change block under `asyncio.gather`, each wrapped in its own
`asyncio.wait_for` at 0.9 of the existing one-second `_DECISION_CONTEXT_TIMEOUT_S`. A slow or
failing block is cancelled or caught alone, logs one friction row under its own event
(`pre_write_decision_failed`, `pre_write_questions_failed`, `pre_write_cochange_failed`) and the
others still render. A failure before the blocks produce a result (project resolution raising, the
backstop) logs one row per block. The non-empty blocks are joined by newlines into the existing
`decision_context`: no new response key, no hook change, no new process. All marks land in one
`cmd_update`.

Budgets: the card keeps its 1,000 characters; at most 3 question lines of at most 300 characters;
at most 3 co-change lines of at most 200 characters; worst case 2,505 characters. Every field goes
through `sanitize_retrieved` and `clip`, so agent-authored question text cannot start a line or
forge a fence.

Questions surface when they are ABOUT a Rule this write's RAG block returned, or ABOUT a Decision
behind any motivated change of the file. The shown question ids go into the read as an exclude
list, so the LIMIT counts only unseen questions. Dedupe reuses `injection_shown` with two new
`COLLAPSIBLE_SECTIONS` entries, `pre_write_questions` (question ids) and `pre_write_cochange` (the
matched path); a compaction or a phase change shows them again.

### 5. Co-change statistics

One batched statement per write: seek the file's FileChanges on `filechange_project_path`, keep
its 200 most recent commits, count each commit's distinct paths at query time on
`filechange_project_commit_hash` and drop commits over 20 files. base is the surviving commits;
a path's support is how many of them it appears in. Pairs need support of at least 2 and
confidence (support over base) of at least 0.4, ordered by support then path, at most 20 rows.
Python drops lockfiles and generated files (`COCHANGE_EXCLUDED`, one module-level tuple matched
against the path and its basename) and keeps the first 3. Excluded files still count toward their
commit's size, so the cap measures the commit as git recorded it. A write to a noise file, or to a
file already hinted this epoch, issues no co-change read at all.

## Alternatives rejected

- **Record identity on a TrustEvent.** TrustEvent's `rule_id` and kind validator describe a rule's
  trust state; bending them to a question would blur both. The question node is already local.
- **A second guard for questions.** A copied guard drifts: the next fix to the refusal order would
  land in one copy. Generalizing by kind keeps one implementation and one audit vocabulary.
- **New refusal event names per kind.** Alerting and item 6's tests read the existing names; a
  parallel set would fork that vocabulary. `event_target` already distinguishes the kind.
- **A stored commit file count.** It goes stale the moment a late FileChange lands for the commit;
  counting on the existing (project, commit_hash) index is bounded and always current.
- **Three separately scheduled reads.** Three round trips into the event loop and three backstops
  would make the added latency their sum in the worst case; one coroutine bounds it by the slowest.
- **A new response key.** It would need a hook change and a new parse path; the existing string
  and its flattening already carry multiple lines.

## Trade-offs

- A question resurfaces after a compaction or phase change. Accepted: it is still open, and the
  epoch is the boundary every other collapsible section uses.
- Co-change is computed on every first write of a file per epoch. The read is bounded by index
  seeks, 200 commits and a 20-row limit, and runs concurrently with the other two; the first lever
  if it shows in latency telemetry is lowering `COCHANGE_RECENT_COMMITS`.
- Only shown hints are marked, so a file with no co-change result is re-queried on every write
  of it. A known cost, bounded by the same index seeks and limits.
- Mass commits (merges, renames, formatting sweeps) are filtered only by the 20-file cap, not
  by kind. A small sweep can still contribute support; the confidence floor limits its effect.
- Decision ids are matched by id alone when wiring ABOUT, as GOVERNED_BY already relies on; the
  pre-write read scopes the Decision to the project.

## Deferred

- Surfacing open questions at prompt time (recall or the ranked sections).
- A `--file PATH` target on `writ question open` (the decision behind a file).
- A cap on how many questions an agent may open, and dedupe of near-identical questions.
- Re-opening an answered or closed question.
- Skipping co-change hints for files already written this session.
- Regenerating `docs/reference/cli.md`.
