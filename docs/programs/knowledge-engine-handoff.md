# Knowledge-engine program: handoff (2026-10-06)

Read this with `docs/programs/knowledge-engine-program.md` (items, decisions, order).

## Where things stand

Stacked local branches; nothing merged or pushed. The user said: do not merge until the
whole program is done.

| Branch | Commit | Contents |
|---|---|---|
| program/1a-injection-split | 8e96c03 | 1a: four capped UserPromptSubmit hooks (9,500 chars each) |
| program/1b-1e-silent-fixes | 7c6850f | 1b-1e: language filter, compaction exclusion, gate after filter, label lists |
| program/3-security-baseline | d696610 | 3: loopback ports, private Neo4j password, `writ neo4j` commands, lock |
| program/wave1 | 170e268, 92a008b | 2 live reload, 1f measurement + rule rewording, 7c tool-failure budget, 1g gate over-match; suite fixes |
| program/wave2 (current) | f0dc450 .. 591a020 | 6 trust records; 4 decision recall; support page; approval-race fix; 5 long documents; 7a open questions; 7d co-change hints |

Run the suite with `.venv/bin/python3 -m pytest tests/` (about 19 minutes).

PROGRAM COMPLETE (2026-10-08) except 7b, deferred (needs ~470 human relevance labels). Done: 1a-1g, 2, 3, 4, 5, 6, 7a, 7c, 7d, the support page, and an approval-race fix. Last full suite on program/wave2 before 591a020: 14758 passed, 0 failed. Ready to merge on the user's word.
Item 5 decisions (2026-10-07): Document/Chunk are record labels; one shared parametrized index builder; documents join the live-reload generation.
Done after the program (2026-10-08, 8df1f08 and ef36f36): README and docs rewrite with the hero image; deterministic HNSW builds; snapshot-read token claims on advance/replan; timing-safe secret comparison; credential refusals never escalate to a prompt; shared atomic-file module; corrected `writ git-hooks install` messages. Then (ranking-ties commit): ranking ties ordered by rule id (benchmarks/RANKING-TIES-2026-10-08.md) and co-change checked once per file per epoch. The item 4 decision read is pinned to its index (591a020).

Last full suite (2026-10-07, before the item 4 commit): 13677 passed, 1 failed. The failure, tests/test_trust_review.py::TestConcurrentDisputesRealProcesses, passed 9 times alone, under CPU load, and in a 712-test related run; not reproduced. Re-check it in the next full run.

## Not live until the end

The running daemon is the installed plugin (~/.claude/plugins/cache/writ/writ/1.11.1), not
this repo. After the final merge and release, the user must: run `writ neo4j set-password`
(or re-run scripts/bootstrap-plugin.sh), restart the daemon/service, start a new session,
then fill the operational items (live hook sizes in ADR-prompt-injection-split.md, reload
generation in /health). Do not run the password change early: the old daemon would lose
its login.

## How each work cycle runs

1. New task: `writ mode set work <session_id>`; move the previous plan files out of
   `.claude/plans/<session_id>/` (archives: archive-1a, archive-1b-1e, archive-3).
   Create the next stacked branch from the current one.
2. Design: read-only `writ:writ-explorer` research, present 2-3 options, user approves.
   Then a reuse audit (explorer) before the plan is approved: the user requires reusing
   working project code, never recreating logic.
3. Plan: `writ:writ-planner` may only write files named plan.md / capabilities.md. For
   parallel workstreams, have each planner write `<scratch>/waveN/<X>/plan.md`, then merge
   with a script (see below) into `.claude/plans/<session_id>/`. Validate with
   `python3 -c "import sys; sys.path.insert(0,'.'); from writ.session.approval_workflow
   import _validate_phase_a; print(_validate_phase_a('$PWD','<session_id>'))"` (None = ok).
   ## Rules Applied may cite only rule IDs injected in this session; accepted so far:
   DOC-ARCH-001, PERF-QBUDGET-001, ENF-SYS-005, PY-PROTO-001. Rule IDs must lead each
   bullet. Planners cannot POST the quality judgment; the orchestrator does:
   `curl -s --unix-socket ~/.cache/writ/run/writ.sock -X POST
   http://localhost/session/<session_id>/quality-judgment -H 'Content-Type:
   application/json' -d '{"artifact_path": "<abs plan.md>", "score": 4,
   "failing_section": null, "rationale": "..."}'`.
4. User types "approved" -> test writers (one per workstream, same checkout, disjoint
   files) -> user "approved" -> implementers -> re-run tests -> `writ:writ-reviewer`
   -> fix findings -> full suite -> commit on the branch.
5. Tests: isolated graph only (`PYTHON=$(command -v python3) bash scripts/test-graph.sh
   up`). Never set WRIT_TEST_NO_ISOLATION=1 (it wipes the production graph). Parallel
   agents share one isolated graph: wrap every pytest in
   `flock <scratch>/wave1/testgraph.lock ...`; load-then-measure steps in ONE lock hold.
   Use `.venv/bin/python3` (repo venv, has onnxruntime and uvicorn).
6. Never name the external product that inspired the program in docs, code, commits or
   chat. Never read credential files or writ.toml.

Merge script used for wave 1 (splits each workstream plan on the four top-level sections
outside code fences, concatenates Files with shared paths merged into one line, nests
each Analysis under "### Workstream X", leads Rules Applied bullets with the rule id, and
prefixes Capabilities with the workstream letter). Recreate it if the scratchpad is gone.

## Approved designs for the remaining items

Item 6, attribution and trust records (wave 2, first):
- Hybrid: flat summary props on Rule (approved_by, approved_at, approval_via, layer,
  basis, verify_interval_days default 180, last_verified seeded to the migration date,
  deliberate, disputed, superseded) plus a TrustEvent record label (approval, edit,
  dispute, verify) for history. TrustEvent is a RECORD label: preserved across import
  wipes, never in writ-corpus.cypher. Approver identity stays local (user decision).
- Identity is captured by auto-approve-gate.sh at token mint (outside the agent's turn):
  OS login (anchor) plus global git name (display), appended as extra lines in the gate
  token file (gate_token.py _line() tolerates absent lines). Never from agent arguments.
- Traps: new Rule fields are MANAGED_PROP_NAMES and get wiped by reconcile unless in
  RUNTIME_EXEMPT_PROPS or authored in markdown; reconcile prunes edges not in markdown;
  the dump drops edge properties; last_validated resets on every ingest (cannot be the
  re-verify clock). Supersede = SUPERSEDES edge authored in markdown plus a superseded
  flag; RANKED_INCLUDE_WHERE / INJECTION_RULE_WHERE and detect_ranked_exclusion_mismatch
  must change in lockstep. reject stays for ai-provisional; add a token-gated dispute.
- Injection: short STALE / DELIBERATE tags in the existing header slot (cmd_format,
  _HEADER_FIELDS in ranking.py, pipeline entry dict, always-on renderer).

Item 4, decision recall (wave 2, after 6):
- Rank decisions by file paths named in the prompt (FileChange -[MOTIVATED_BY]-> Decision,
  indexed by (project, path)) plus a per-project BM25 over decision text; recency
  tie-break. Cards: title + rule ids, rationale clipped ~160 chars, per-file reason for
  matched paths, within the 500-token recall section (pass the real budget; today
  compile_recall gets 20000 so its eviction never runs).
- Cadence: once per session plus re-run when the prompt names a path/term matching an
  unseen decision (replace the recall_briefed bool with a shown-ids set).
- Before a write: /pre-write-check adds the decision behind the file's last change,
  deduped per (path, decision_id), fail open, ~600-1000 chars.
- Memory nodes read back (lexical only, max 2 of 5 slots). Titles: first sentence of the
  rationale or the commit subject instead of rationale[:80].

Item 5, long documents (wave 3): separate DocumentPipeline + fifth hook
(writ-inject-documents.sh), 128-token embeddings over breadcrumb + chunk head, BM25 over
full chunk text. Phases: 0 fencing on every channel + raw similarity + "absent block
means nothing matched"; 1 collector registry + allowlist collapse (keep DOCTRINE_NODE_TYPES
explicit); 2 Document/Chunk as record labels + markdown splitter + `writ docs ingest`;
3 retrieval section + hook (promote document_chunks into PROMPT_SECTION_TOKENS); 4
neighbour/parent expansion. Sources: docs/, ADRs, READMEs by default; CLAUDE.md and
memory files opt-in; ingest only via `writ docs ingest`. Measure rule ranking unchanged
with scripts/measure_retrieval.py.

Item 7a open questions: OpenQuestion record label, edges to Rule/Decision, surfaced at
write time (max 3 lines), lifecycle via an authenticated route; follow item 6's record
conventions. Item 7d co-change hints: on-demand Cypher over Commit/FileChange siblings
(exclude commits over ~20 files, lockfiles, generated files; min support, confidence
>= 0.4), max 3 lines once per file per session in the pre-write context. Build 7a then 7d
(both extend the same pre-write output).

## Known follow-ups

- The three end-of-turn checks (pending tests, quality score, reply style) exit 1 and only report; the user chose to keep that.
- Expand tests/fixtures/ground_truth_negatives.json (~100, more near-domain) before any
  abstention-threshold change; hold the cosine/max vector-norm candidate until a weight
  re-sweep.
- Several logins on one machine and shared-server access control are out of scope.
