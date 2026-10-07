# ADR: decision recall ranked by the prompt (cards, shown ids, the write-time decision)

Status: accepted
Date: 2026-10-07
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (knowledge-engine program item 4)
Touches: `writ/shared/injection_text.py`, `writ/session/recall.py`, `writ/session/harvester.py`,
`writ/graph/db/record_store.py`, `writ/session/injection_state.py`,
`writ/session/budget_tracking.py`, `writ/server/models.py`,
`writ/server/routes/decision_memory.py`, `writ/server/routes/query.py`,
`writ/server/routes/gate.py`, `writ/cli.py`, `writ/shared/logging.py`,
`hooks/scripts/writ-pre-write-dispatch.sh`
Sibling: `docs/adr/ADR-prompt-injection-split.md`, whose recall section this changes.

## Context

Recall read back the 20 most recent decisions on the first prompt of a session, whatever the
prompt was about, and rendered each as its title (the first 80 characters of the rationale)
and rule ids. Its default budget was 20,000 tokens while the section that delivers it holds
500, so eviction never ran. A decision about the file the agent was about to edit was shown
only if it happened to be recent, never again after the first prompt, and never at the
moment of the write. Mirrored memories were written to the graph and never read back.

## Decision

### 1. Three ranking tiers, recency as the tie-break

`compile_recall` ranks candidates in tier order, and ties inside a tier break newest first:

- **path**: decisions behind the files the prompt names. One batched read,
  `get_decisions_for_paths`, follows `FileChange -[MOTIVATED_BY]-> Decision` for the
  candidate paths (at most 40, 3 changes per path), sorted by the most recent matched
  change, then the decision's own timestamp. The read returns the decision's own fields, so
  a decision older than the corpus window still renders.
- **term**: decisions and memories whose text matches the prompt, through the existing
  `KeywordIndex` built in memory (no index directory) and queried through its own
  sanitizer, so no second tokenizer exists. Field mapping, with no schema change: id to
  `rule_id` (memories prefixed `memory:`), display title or memory name to `trigger`
  (boosted), rationale or description to `statement`, planned paths to `tags`, planned
  reasons to `body`. The query is the prompt's first 2,000 characters, limit 20.
- **recent**: the remaining decisions newest first, only when `matched_only` is false (the
  first brief of an epoch). Memories never fill by recency.

### 2. The score floor

BM25 scores are not normalized, and their scale moves with the corpus: the IDF of a term
found in one document out of N is ln(1 + (N - 0.5) / 1.5), the value the keyword index
itself assigns. So the floor scales with N, the decisions plus memories in the project's
index: a term hit must score at least `term_score_floor(N)` =
`RECALL_TERM_FLOOR_FRACTION` x ln(1 + (N - 0.5) / 1.5). The fraction is 0.5 / ln(14), about
0.1895, chosen so floor(20) is 0.5, the value the 20-decision fixture set (generic prompts
such as "ok, continue with the next step" score under 0.2 there and a distinctive term
above 2). The floor is then about 0.05 at N = 1, 0.19 at N = 3 and 0.93 at N = 200, so one
distinctive word shared with one decision clears it at every size while words present in
every document do not. On a very small corpus several generic words can still add up past
it (at N = 3, two words present in all three documents do); that is the tuning point.

Each compile that runs the term tier writes one `recall_term_floor` row on the metrics
stream: the project, the corpus size N, the floor used, the top term score and how many
term hits cleared the floor. It carries no prompt or decision text, never raises into
recall (a failed write is swallowed), and is the data to revisit the fraction from real
sessions.

### 3. Cards inside the real budget

Each kept item renders as one card: title and rule ids (or `[no rules cited]`), a `why:`
line with the rationale clipped to 160 characters, and one line per matched file (at most 2)
with that file's reason clipped to 120; a memory renders `- memory <name>: <description>`.
The header line is unchanged. A card is costed by its rendered text (plus one token for its
joining newline and rounding, so the sum never undercounts the joined briefing), and the
budget is `PROMPT_SECTION_TOKENS["recall"]`, the one source for the section's 500 tokens;
the three hardcoded 20,000s are gone. The existing `_decision_token_cost`, `_evict` and
`_build_briefing` were extended rather than paralleled: the rationale (a memory's
description) evicts first, then each matched file's reason; the id, title, rule ids and
memory name are protected; an item that still does not fit is dropped whole and nothing
ranked below it is kept. At most 5 cards, at most 2 of them memories. Rule statements are
read (one batched call) only under `full`, the CLI listing that prints them.

Titles are derived at read time (`display_title`: the first sentence of the rationale,
else the stored title), so existing decisions show a real title without a migration;
harvest now stores the same first sentence, or the commit subject when the rationale is
empty. The decision id hashes the plan text, so a re-harvest re-merges the same node.

### 4. Shown ids over the bool, with the epoch re-brief

`recall_briefed` is retired. The recall section records the ids whose card head survived
the 1,998-character clamp in the per-epoch shown record that the always-on and floor
sections already use (`injection_shown`, `apply_mark_shown`, `--mark-shown`), with
`recall` and `pre_write_decision` added to `COLLAPSIBLE_SECTIONS`. The first prompt of an
epoch always marks the epoch, even with nothing shown or after a recall failure, so a
broken store is not retried every prompt. A later prompt asks for matched cards only,
excluding the shown ids, and writes only when it showed one. A compaction or a phase change
starts a new epoch and the next prompt briefs again; that is intended, because the model
lost the earlier briefing. `--set-recall-briefed` is no longer registered; `cmd_update`
skips an unknown flag, so a stale caller is harmless.

### 5. One graph read, and its reading of "the file's last change"

`get_decisions_for_paths` is modeled on `get_latest_filechange_per_path` and placed beside
it: caller-normalized paths, an IN-list on the existing `filechange_project_path` index,
both ends of the edge scoped to the project, newest change first, a per-path slice, the
Commit joined for its subject, deduped by decision. "The decision behind the file's last
change" is read as the decision behind the most recent change that HAS a `MOTIVATED_BY`
edge: a strict latest-change reading would go silent whenever the latest commit was an
unplanned fix-up, the common shape right after a planned change. Restoring the strict
form is a one-clause change (filter after ordering instead of before).

### 6. One prompt path extractor

`prompt_path_tokens` and `path_candidates` live in `writ/session/recall.py` and are the only
definitions; the write gate imports `path_candidates` from there. Tokens split on
whitespace and quoting characters, lose trailing punctuation and a `:LINE[:COL]` suffix,
drop URLs, and are kept when they contain a slash or end in a dotted extension (at most 10).
Candidates are the repo-relative spellings under the project root and up to 4 of its
ancestors (never the filesystem root), each through `normalize_path`, the normalization the
FileChange writer uses. Limits: a bare filename is never matched against the graph (no
suffix scan); it matches lexically through the indexed planned paths. A version number or
"e.g." reads as a dotted token and costs one path read that matches nothing.

### 7. Memories: name and description only

`list_memories` is read unchanged (tombstones excluded). Only the name and the one-line
description are indexed and rendered: the description is the summary the mirror writes for
exactly this purpose, a card has room for one line, and bodies would widen the read and the
corpus. Indexing bodies is a follow-up if name and description prove too thin.

### 8. The corpus cache

The corpus (up to 200 decisions, the live memories and the built index) is cached per
(db object, project) in a module-level `WeakKeyDictionary` for `RECALL_CORPUS_TTL_S` (60
seconds), so a prompt does not ship 200 rationales and rebuild an index. A daemon route
that writes a Decision or Memory drops the cached entry once its write succeeds
(`invalidate_corpus`): the commit capture and memory mirror routes for their project, the
planning-gate approval capture for every project on that db. The next compile reloads, so
those writes reach the term and recency tiers at once. The TTL stays as the backstop for
writers outside the daemon: a decision captured by the CLI appears within a minute. Keying
weakly by the db means test fakes and a CLI process never share entries. The path read and
the write-time read are never cached.

### 9. The write-time decision and its transport

On an allowed write, `/pre-write-check` calls `write_decision_context` with the file's
candidates: one read (`per_path` 1), the longest matched path, the key
`<path>#<decision_id>`, and one line of at most 1,000 characters (path, title, rule ids,
rationale clipped to 400, the file's reason, commit subject and short hash). The key goes
through the same shown record (`pre_write_decision`), so it shows once per file and
decision per epoch. It fails open under a one-second timeout with a
`pre_write_decision_failed` friction row. The response gains an additive `decision_context`
field; the dispatch hook appends it to `rag_rules` inside its existing translator, so no
process, blob line or branch is added and an older server changes nothing.

## Query budget

| path | statements |
|---|---|
| recall, cold corpus (or after a daemon capture) | 2 (decisions, memories) plus 1 when the prompt names a path |
| recall, warm corpus (within 60 seconds) | 0, or 1 when the prompt names a path |
| allowed write to a file under the project | 1 |
| allowed write outside the project, deny or ask | 0 |
| `writ recall` / `writ recall --full` | 2 / 3 (plus the cached project registry read) |

## Alternatives rejected

- **A persisted decision index.** The rule index's generations are keyed by the rule
  corpus; a second persisted index would need its own invalidation from commit capture.
  An in-memory build per minute per project costs less than that machinery.
- **Matching paths from planned_files JSON.** The `MOTIVATED_BY` edge written at commit
  capture is the authoritative link; re-deriving it from JSON would duplicate the claim
  matcher and scan every decision.
- **A suffix scan for bare filenames.** It would read every FileChange of the project per
  prompt; the lexical match through planned paths covers the case.
- **A new cache key and update verb for each record.** The shown record already has the
  epoch semantics both cadences need.
- **A fifth prompt hook or a new field in the hook's blob.** Both would grow the hook
  surface the ceiling tests pin; the existing recall hook and the existing `rag_rules`
  flattening carry the new content.

## Trade-offs

Re-briefing after a compaction or phase change spends up to 500 tokens again. A decision
written outside the daemon reaches the term and recency tiers up to a minute late (the path
tier, and anything captured through a daemon route, is immediate). The score floor is
calibrated on fixtures, not on live data, until the `recall_term_floor` rows say otherwise.
