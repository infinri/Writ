# Decision memory

Writ records *why files changed*, mechanically, and plays it back. Source of truth: `writ/session/{harvester,decision_capture,commit_capture,plan_harvest,recall,git_hooks,git_identity,pr_comments,bitbucket_client,remote_parse,registration}.py`, `writ/graph/db/record_store.py`, `writ/server/routes/decision_memory.py`.

Provenance: this feature family (decision capture, session recall, pushing per-file reasons onto commits and open PRs) is adapted from concepts pioneered by JolliAI. The recall eviction policy is adapted from Jolli's ContextCompiler (the policy, not the code; `writ/session/recall.py` documents the policy). Writ's addition is rule grounding: every decision carries its governing rule IDs, and those are never evicted from the recall digest.

## Continuity at a glance

| Mechanism | What it records | Read it with |
|---|---|---|
| Decision capture (post-commit hook, plan approval) | Decision, FileChange and Commit records | `writ recall`, `writ recall --full`; git notes on `refs/notes/writ-decisions`; `writ pr sync` (Bitbucket Cloud) |
| Write-time context | The decision behind a file's last change, open questions about it, files that usually change with it | Shown before an allowed write |
| Open questions | OpenQuestion records | `writ question list`, `writ question answer` and `writ question close` (answer and close need the approval token) |
| Auto-memory mirror | Memory records, latest contents only | `writ memory list`, `writ memory audit`, `writ memory backfill` |
| Compaction handoff | `.claude/handoffs/session-<id>.md` | `writ handoff --session <id>` (also written automatically at compaction) |
| Project documents | Document chunks | `writ docs ingest` |
| Audit stream | Gate and approval rows | `writ audit-session <id>`, `writ logs tail` |

## What this is, and what it is not

This is not conversational memory. Writ does not read your chat history and guess what mattered. It builds the record mechanically, from things that already exist.

**Be clear on what this is.** It is an attribution trail: what the AI was shown and what it claimed to apply. It is not proof that a rule was followed. That is still a reviewer's job, which is exactly why the pull request channel exists. And it is only possible because Writ owns the approval gate. A memory layer bolted onto an AI has no approved plan to join against.

## Records

Three record types plus a registry, all deliberately outside every retrieval registry (they can never enter RAG; recall is a separate query by design):

- **Decision**: `decision_id` (content hash of project + plan text, so re-capture merges), title, rationale, `planned_files` (each `{path, reason, resolved}`), `governing_rule_ids`, phase, session.
- **FileChange**: `change_id` (hash of project + commit + path, so amends merge), path, change type, reason, `queried_rule_ids` (the rules the AI was shown; a deliberate *superset* built from windowed transcript attribution), `cited_rule_ids` (a frozen snapshot of the governing decision's rules so PR comments render after claims resolve), commit hash.
- **Commit**: hash, subject, author, branch.
- **Project registry** (`:Project`): name (unique, the constraint that makes concurrent registration race-safe), repo root, remote URL. Identity is the normalized remote (`host/org/repo`); capture *refuses* to run outside a real git repo rather than scope records to a fallback name.

Edges: `HAS_DECISION` / `HAS_CHANGE` / `HAS_COMMIT` from the project; `MOTIVATED_BY` (FileChange to Decision), `GOVERNED_BY` (Decision to Rule), `INCLUDES` and `REALIZES` (Commit to its changes and decision). All records are stamped `provenance="record"`: permanent, parity-exempt, never graduated.

## Capture paths

All three converge on one shared writer (`harvest_one_commit`), so identical inputs produce identical nodes regardless of path:

1. **Post-commit (primary)**: the installed git hook (POSIX sh, always exits 0, never blocks a commit) posts each commit to `/commit/capture` with a 3-second cap, fail-open when the daemon is down. Capture joins the commit's files against the approved `plan.md` (read from disk, because the planner sub-agent wrote it in its own transcript), merges the parent session's and every sub-agent's per-file queried rules, resolves prior open claims, and flags `committed_file_not_in_plan`.
2. **Backfill**: `writ harvest [--since <rev>]` replays git history against the Claude Code transcript directory, picking each commit's governing plan (latest `plan.md` write at or before the commit) and the windowed file-context rules. Merge commits are skipped; `--since` must be a real revision.
3. **Approval-time** (`capture_decision_at_approve`): snapshots the plan when the planning gate is approved, best-effort; the post-commit path supersedes it in practice and re-derives the same content-hash ids.

Reason fallback chain per file: plan-cited reason, else a prior open claim's reason, else commit-level context, else the bare subject. Nothing is ever manufactured.

**Git hook management**: `writ git-hooks install|uninstall` writes a marker-delimited block into `post-commit` only (the prepare-commit-msg hook is retired and stripped on every install), worktree-safe via the common hooks dir, preserving any pre-existing hook content. First Work-mode entry into a repo auto-installs via `/git-hooks/auto-install` (idempotent, fail-open).

## Recall

The recall section of every prompt (`writ-inject-recall.sh`, through `/prompt-bundle` and `/recall`) compiles a briefing ranked by the prompt (`writ/session/recall.py`, `docs/adr/ADR-decision-recall.md`):

1. **Path tier.** Decisions behind the files the prompt names: `prompt_path_tokens` keeps slash-bearing and dotted-extension tokens (at most 10), `path_candidates` turns each into repo-relative candidates under the session's project root or up to 4 of its ancestors, and one batched read (`get_decisions_for_paths`) follows `FileChange -[MOTIVATED_BY]-> Decision` for them. The decision found is the one behind the file's most recent change that has a `MOTIVATED_BY` edge.
2. **Term tier.** Decisions and mirrored memories whose text matches the prompt, through an in-memory BM25 index over the project's 200 most recent decisions (title, rationale, planned paths, planned reasons) and its live memories (name and description), at or above `term_score_floor(N)`, a fraction (`RECALL_TERM_FLOOR_FRACTION`, about 0.19) of the score a term found in one of the N indexed documents gets, so the floor rises with the corpus (0.5 at 20 documents). A bare filename matches through the indexed planned paths. The corpus and its index are cached per project for `RECALL_CORPUS_TTL_S` (60 seconds); a commit capture, memory mirror or approval capture through the daemon drops the cached entry so the next compile reloads.
3. **Recent tier.** The remaining decisions, newest first, on the first brief of an epoch only. Memories never fill by recency.

Ties inside a tier break newest first. Each kept item renders as a card:

```
- <title> [RULE-A, RULE-B]
  why: <rationale, clipped to 160 characters>
  <matched path>: <that file's reason, clipped to 120>   (at most 2)
- memory <name>: <description, clipped to 160>
```

The title is derived at read time from the first sentence of the rationale (the stored title when the rationale is empty), and harvest now stores the same first sentence (or the commit subject) instead of the first 80 characters. A decision with no rule ids renders `[no rules cited]`.

**Budget.** The briefing is costed by its rendered text inside the recall section budget, `PROMPT_SECTION_TOKENS["recall"]` (500 tokens), and the route clamps it to 1,998 characters. At most 5 cards, at most 2 of them memories. Eviction order under pressure: the rationale (a memory's description) first, then each matched file's reason; ids, titles, rule ids and memory names are never evicted, and an item that still does not fit is dropped whole with everything ranked below it.

**Cadence.** The ids shown are recorded per epoch in the session's shown record (`--mark-shown recall`). The first prompt of an epoch briefs with recency fill; a later prompt in the same epoch shows only unseen cards the prompt matches by path or term, and nothing otherwise. A compaction or a phase change starts a new epoch, so the next prompt briefs again.

**CLI.** `writ recall` prints what the first prompt of an epoch receives without a prompt (the section budget, recency cards). `writ recall --full` uses `RECALL_FULL_BUDGET` (20,000 tokens) so nothing is evicted, and prints each kept decision's full rationale and governing rule statements, up to `--limit`.

**Before a write.** `/pre-write-check` adds `decision_context` on an allowed write to a file under the session's project whose past change was motivated by a decision: one line of at most 1,000 characters naming the path, the decision's title and rule ids, its rationale, the file's recorded reason and the commit subject and short hash. It shows once per (path, decision) per epoch (`--mark-shown pre_write_decision`), fails open with a `pre_write_decision_failed` friction row, and the dispatch hook appends it to the file-context rules. Two more blocks follow it in the same string (program items 7a and 7d, `docs/adr/ADR-open-questions-and-co-change.md`): at most 3 lines of at most 300 characters for the open questions that bear on the write (about a decision behind any motivated change of the file, or about a rule this write's RAG block returned), each naming the question id, its about ids, the question, who can answer and what would settle it; then at most 3 lines of at most 200 characters for the files this one usually changes with (`<path> usually changes with <other> (<support> of <base> recent commits)`), counted over the file's 200 most recent commits of at most 20 files each, with support of at least 2 and confidence of at least 0.4, and never naming a lockfile or generated file. All three are read concurrently in one coroutine, each under 0.9 of the one-second backstop and failing alone with its own friction row (`pre_write_decision_failed`, `pre_write_questions_failed`, `pre_write_cochange_failed`). A question shows once per epoch by id (`pre_write_questions`) and a file is hinted once per epoch (`pre_write_cochange`), so a second write of the file in the epoch skips the co-change read entirely.

## Auto-memory mirror

Claude Code's own auto-memory files are mirrored into the graph as `:Memory` nodes, one node per file, keyed `(name, project)` where `project` is the encoded project directory above the file's `memory/` dir. The mirror hook covers writes from now on, `writ memory backfill` covers what is already on disk and doubles as the deletion reconciler (a deleted file becomes `status="deleted"`, never a hard delete), and `writ memory list --project <p>` reads a project's set back. `Memory` is deliberately outside every retrieval registry, exactly like the three record types: a memory can never enter RAG.

`create_memory` MERGEs on `(name, project)` and then applies a bare `SET m += $props`: no `ON CREATE` / `ON MATCH` split, no versioning, no history. Last writer wins. So a memory filed under the **wrong** project is not merely mis-scoped: the next write of a same-named memory in that project overwrites the body, description and path of the node already sitting there, and the old content is gone. Project scope is a data-loss boundary here, not a filing convenience.

`writ memory audit [--projects-root PATH] [--json]` is the watch on that boundary. It reports four buckets and repairs nothing:

- **MISMATCH**: the stored `project` disagrees with the project derived from the stored `path`. An invariant guard: every current writer sets both properties in one `create_memory` call from the same derivation, so they agree by construction and no node violates this today, but nothing in `create_memory` enforces the pair, so a future caller that derives the project some other way surfaces here.
- **EMPTY**: an empty or missing `project`, or an empty or missing `path` (a record whose scope cannot be checked at all).
- **DISK DRIFT**: the stored `path` no longer exists, or it exists and the project derived from the **live** path no longer equals the stored `project`. This is the bucket that catches real regressions, because it compares the record against the world as it is now instead of against a property written in the same instant.
- **COLLISION**: two on-disk project directories under `--projects-root` that derive the same project segment. Claude Code encodes a cwd by replacing `/` with `-` and does not escape hyphens already present, so `/home/u/foo/bar` and a real directory named `/home/u/foo-bar` both encode to `-home-u-foo-bar`, two working directories sharing one set of Memory nodes. Nothing collides in a real projects root today; this is a structural risk being watched, not an observed failure.

The buckets are assertions, not partitions: a node whose stored `project` disagrees with both its stored path and the live path is counted in MISMATCH and in DISK DRIFT, because those are two different claims about it. Counts are per finding.

The audit is **read-only**: a plain `MATCH (m:Memory) RETURN ...` plus a `stat` of each stored path and a symlink-refusing walk of `--projects-root`. It never writes, and its output says so (`repaired=0`). Repair is a separate cycle by design, because re-keying a mis-filed node is itself a destructive write against content that has no history to restore from. `/memory-record` requests that arrive without a `project` are served from the path derivation and logged as `memory_project_derived`, so the guess that decides scope is auditable rather than silent.

## PR sync

`writ pr sync [--pr-id N]` posts one comment per changed file on the open Bitbucket PR: the captured reason, "rules the AI was shown" (queried), and "rules the AI cited" (governing snapshot). Idempotent by an attribution marker (it updates its own comments), `skipped_no_reason` for files with no record, fail-loud on non-429 API errors. The client is SSRF-hardened: hardcoded `api.bitbucket.org` base, redirect allowlist, bounded 429 retries, and the token never appears in logs. Self-hosted Bitbucket Server is unsupported by design (`parse_bitbucket_remote` rejects non-bitbucket.org remotes). A parallel channel writes the same bodies as git notes on `refs/notes/writ-decisions`, best-effort.

## Caveats

`queried_rule_ids` is documented over-attribution: a windowed union across turns, never a false "not shown" negative, acceptable for an audit aid. Symlinked working directories are a known path-join edge in transcript attribution. `writ recall`'s branch argument is cosmetic; recall is project-scoped.
