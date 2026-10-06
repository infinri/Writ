# Program: knowledge engine hardening

Started 2026-10-05. Goal: fix Writ's silent failures, make learning live, close the
security baseline, and extend Writ to long and mixed content with stronger recall and
trust records. Everything is built the Writ way: graph-native, fail-closed, enforced by
hooks and gates rather than prompt text, and measured against the benchmark before
and after.

Each item below runs as its own Writ work cycle (design, plan, test skeletons,
implementation, review). Evidence paths are relative to the repo root.

## Program rules

- **Measure every ranking change.** Run the retrieval benchmark before and after,
  and report hit@5, MRR@5 and nDCG@10 with 95% bootstrap intervals. Overlapping
  intervals mean no decision. No paid A/B runs.

## 1. Fix the silent failures

| # | Defect | Evidence | Smallest fix |
|---|---|---|---|
| 1a | **Hook output over 10,000 characters is replaced by a 2 KB preview.** Claude Code caps one hook's injected context; over the cap, the model sees only a file pointer and the first 2 KB. Observed 2026-10-05: the per-prompt injection was 13 to 17 KB on every turn, and only the preview reached the model. | Session hook outputs, 2026-10-05 | Keep each injection under the cap: split across hook callbacks, or tighten the budget so always-on + ranked + methodology fit |
| 1b | Directory change tags the session with a language (python, php, javascript, rust, go). The ranked filter matches it exactly against rule domains, which never use those values, so ranked rules go empty | `hooks/scripts/writ-cwd-changed.sh:56-68`, `writ/server/routes/query.py:265`, `writ/retrieval/pipeline.py:511` | Stop passing `domain=` from the detected language at `query.py:265`, or map it to a soft boost |
| 1c | After compaction outside work mode, shown rules never come back: reset clears `by_phase[None]`, the query side falls back to the flat `loaded_rule_ids`, which is never cleared | `writ/session/session_lifecycle.py:57-61`, `query.py:231-236`, `writ/session/mode_engine.py:619` | Also clear `loaded_rule_ids` on compaction |
| 1d | No-good-match gate (0.30) reads the raw top-10 vector hits before filtering, and only 10 vector candidates survive to filtering | `pipeline.py:61`, `pipeline.py:376-386` | Filter before gating; raise `VECTOR_CANDIDATE_LIMIT` |
| 1e | Two methodology label lists disagree: trigger index lacks ForbiddenResponse | `writ/retrieval/trigger_index.py:30` vs `pipeline.py:792` | Derive both from one source in `writ/graph/schema.py` |
| 1f | Ranking misses: sibling-rule collisions and the Magento magnet (about 7 misses); 11 of 35 remaining misses are genuine ranking or vocabulary work | `benchmarks/MISS-TRIAGE-2026-08-05.md` | Investigate the Magento scoring artifact in the pipeline; then sibling disambiguation; retune the 0.30 abstention threshold now that it reads filtered hits (`benchmarks/VECTOR-GATE-2026-10-05.md`) |

## 2. Make learning and edits live (hot reload)

Today the daemon loads rules, outcome counts, BM25, HNSW, adjacency, routes and the
trigger index once at start (`writ/server/__init__.py:195-217`). Feedback routes write
counts to Neo4j only (`query.py:460-504`), so learned confidence ranks on startup
values. No reload endpoint exists.

- Feedback routes also update the in-memory metadata (`pipeline._metadata`).
- A reload route that rebuilds the pipeline and swaps it atomically, keeping the
  last good pipeline if the rebuild fails.
- Rebuild only what changed, keyed by content hash per source plus an index version
  that forces a full rebuild when the unit shape changes. Writ already hash-caches
  BM25 and HNSW (`pipeline.py:961`, `980-1008`).

## 3. Security baseline

- Bind Neo4j to localhost: `127.0.0.1:7474:7474` and `127.0.0.1:7687:7687`
  (`docker-compose.yml:9-10` bind all interfaces today).
- Read the password from `WRIT_NEO4J_PASSWORD` in compose and the healthcheck
  (`docker-compose.yml:13`, `:19` hard-code `writdevpass`); refuse to start the
  daemon on the default password unless an explicit dev flag is set
  (`writ/config.py:24`, `163-195`).

## 4. Decision recall

Capture is strong (git post-commit hook to `/commit/capture`,
`writ/session/commit_capture.py:152`, `harvester.py:336-406`). Recall is weak:
20 fetched newest-first, at most 5 shown, title and rule IDs only, no rationale
(`writ/session/recall.py:34`, `66-81`, `97`). The mirrored Claude Code memory files
are never read back into a session (`record_store.py:97` is CLI-only).

- Rank decisions by relevance to the current prompt and touched files, not recency.
- Show the rationale and the per-file reason for the file being edited.
- Deliver at the moment it matters: before a write to a file with past decisions,
  surface the decision that motivated its last change (`MOTIVATED_BY` edge exists).
- Read mirrored Memory nodes back through the same ranked path.
- Harvested titles are `rationale[:80]` (`harvester.py:398`); give them real titles.

## 5. Long and mixed content

- **Document node type plus chunking ingest.** Split markdown at H1 to H3 with a
  heading breadcrumb, pack whole paragraphs up to a size cap, and repeat a short
  header on every chunk. A `Document` node with `Chunk` children linked by graph
  edges, so neighbour and parent expansion are graph hops.
- **Collector registry.** One registry entry per source returning units plus a
  change stamp. The registry is validated against the schema allowlists at startup
  and fails closed on an unknown type. Collapse the scattered allowlists into one
  derived source where safe (`writ/graph/schema.py:192`, `212-220`, `790`, `806`;
  `writ/graph/db/_common.py:51-61`); keep `writ/retrieval/node_scope.py:39-46`
  explicit as designed.
- **Per-kind quota.** Once documents exist they will outnumber rules; reserve slots
  per kind, capped at half the page, promoting only real candidates. Rules keep
  priority.
- **Fence retrieved text as data.** Wrap injected rule and chunk text, strip control
  characters and any closing fence tag. Fence every injection channel. No fencing
  exists today (`writ/session/budget_tracking.py:460` is a plain frame).
- **Show relevance.** Print the raw similarity next to each rule and say plainly
  that an absent block means nothing matched.

## 6. Attribution and trust records

Promotion records only `authority`/`provenance`/`graduated_via`, not who approved
(`writ/authoring.py:219-227`, `writ/promotion.py:172-236`, `writ/graph/schema.py:50`).

- **Approver identity and time** on every promotion and human edit, from the
  git identity of the approving session, stored on the rule and on an approval edge.
- **Seen vs inferred.** A `layer` property (observed, inferred, unknown),
  orthogonal to `basis` (code, document, ticket, testimony); a chain of reasoning
  carries its weakest link's layer.
- **Disputes and revisions as events.** A dispute edge with who, when and note,
  never deleting or adjudicating; supersede creates a revision edge and drops the
  old rule from retrieval. Today `reject` deletes (`writ/authoring.py:230`).
- **Re-verify interval.** Each rule carries a verify interval (default 180 days);
  stale rules are flagged at injection and listed by an integrity check.
- **"Deliberate, do not simplify" flag** shown at injection, so a later agent
  does not refactor away an intentional choice.

## 7. Further additions

- **Open questions.** A graph node for an unknown, with who can answer and what
  would settle it, linked to the decision or rule that depends on it; surfaced when
  that rule or file comes up.
- **Graded benchmark with intervals.** Add 0 to 3 graded labels and bootstrap
  confidence intervals to the existing benchmark. Supports the measurement rule above.
- **Tool failure budget.** After 3 consecutive failures of the same tool, deny the
  next call with a reason; deny an exact repeat of an irreversible call.
- **Co-change hints.** Writ already records commits and file changes; mine files
  that change together and surface them before a write ("this file usually changes
  with X").

## Decisions (2026-10-06)

- 1f: keep the 0.30 abstention threshold (sweep: it is the knee; no move clears the
  interval rule). Report ranked-eligible queries separately from always-on and routed
  targets; reword CLEAN-DEAD-001 and CLEAN-RETURN-001; hold the cosine-normalized vector
  score until a weight re-sweep. Expand the negative query set before any threshold change.
- 1g (added): the Bash write gate refuses read-only commands that mention a `.py` path with
  `python3` or `grep`; fix the over-match.
- 5: default sources docs/, ADRs and READMEs; CLAUDE.md and memory files opt-in; ingest
  only on `writ docs ingest`.
- 6: approver identity stays in local event records, never in writ-corpus.cypher.
- 7b: deferred (needs about 470 human relevance judgments).
- Build order: wave 1 = item 2, 1f, 7c + 1g in parallel; wave 2 = 6 then 4;
  wave 3 = 5, then 7a and 7d.

## Not in scope

- Multi-user and access control: revisit on a real shared deployment.
- Several OS users on one machine: today they share one container name, port pair and data
  volume. Isolating them means a per-user container, ports and volume, and anyone in the
  docker group can still read any container's password. Item 3 covers only one user running
  several sessions at once (a lock around password changes).

## Order

1 (1a first: it affects every turn), 3, 2, 4, 6, 5, 7. Items 1 and 3 are small and
unblock trust in every later measurement; 5 is the largest and benefits from the
reload work in 2.
