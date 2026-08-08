# The Writ Handbook

The operating manual for Writ: what it is, what happens on a turn, and how to run it. This is the *how to use it* guide; [`docs/reference/architecture.md`](docs/reference/architecture.md) holds the deep internals and this handbook does not duplicate them.

> **North star.** Writ relocates oversight; it does not remove it. The gate is the product. Writ never automates self-approval: a human token-gates any write of canon. Every "the agent could otherwise..." seam in this doc is closed by a human-held token, not by trust.

Every load-bearing number below is anchored to a file, a command you can run, or a dated measurement. Where this handbook and the code disagree, the code wins. Counts were taken from the shipped corpus dump and source tree on 2026-07-31.

---

## 1. What Writ is, and why

**In one line:** Writ is a local service that gives Claude Code two things on every turn: the *right rules for what you're doing now*, and a *workflow gate that you control*.

Two problems motivated it.

- **Context stuffing does not scale.** Pasting a whole rulebook into every prompt works at ten rules and collapses at ten thousand: the model can't tell which rules are load-bearing, cache hit rates fall, and the bill scales with your rulebook instead of your work. Writ replaces stuffing with *relevance*: it retrieves only the rules that fit this file, this domain, this turn.
- **Process discipline cannot live in a prompt.** A skill file can *say* "write the failing test first," but nothing stops the model from writing the implementation first anyway. Writ moves discipline to the boundary where Claude actually calls tools: a write that violates the workflow is *denied*, not just discouraged.

**The four runtime pieces:**

| Piece | Role | Where |
|---|---|---|
| FastAPI daemon | The single HTTP service all hooks talk to (retrieval, session state, gates, self-authoring). Binds `127.0.0.1:8765`, 49 endpoints, no auth (localhost only). | `writ/server/` |
| Hooks + session state | 40 thin bash hooks intercept the Claude Code tool lifecycle (44 registrations across 12 events; 41 scripts on disk, one of which is the statusLine, not a hook); a Python package tracks mode/phase/budget/gates per session. | `hooks/`, `writ/session/` |
| Neo4j canonical store | The graph is the source of truth for all rules and methodology. Runs in Docker (`writ-neo4j`). | `writ/graph/db/` |
| The CLI | Operator control surface: ingest, export, reconcile, validate, author, query, doctor, logs, decision memory. | `writ/cli.py` |

**Canonical vs derived.** Neo4j is canonical. The tracked, shipped form of the corpus is `writ-corpus.cypher` (a portable dump). The `bible/` Markdown tree is a *derived, local, untracked export* (think `dist/`): `writ export` materializes it, `writ import-markdown` pushes edits back, `writ export-cypher` refreshes the dump. When graph and markdown disagree, `writ reconcile` makes the source win. The exception is dual-location canonical files under `bible/methodology/` (§11).

---

## 2. A single turn, end to end

You type a prompt. Claude Code fires `UserPromptSubmit`. In order:

1. **Approval scan** (`auto-approve-gate.sh`). If your prompt matches an approval phrase, it mints the single-use gate token (§7) and, in Work mode with a pending gate, advances the phase. It also publishes your session id to `/tmp/writ-current-session` for everything downstream.
2. **Daemon ensure + RAG inject** (`writ-rag-inject.sh`). Health-checks the daemon, starts Neo4j and the server if needed (unless `WRIT_NO_AUTOSTART=1`), and reads the session cache once: mode, phase, orchestrator flag, remaining budget (`default_budget=8000`).
3. **Mode auto-route.** If no mode is set and the prompt is clearly audit/explore/research-shaped, the hook sets `investigate`; clearly build-shaped, `work`. Otherwise it asks you to declare one.
4. **One warm bundle call** (`POST /prompt-bundle`). This single request runs the ranked query, the always-on floor, and the methodology companion in-process on the daemon and returns all three blocks. It replaced roughly sixteen cold Python spawns per turn with one warm call.
5. **Retrieval** inside that call: the 5-stage pipeline (candidate filter, BM25, ANN vector, graph traversal, two-pass ranking) against pre-warmed in-memory indexes, with an abstention gate that returns nothing rather than noise when the best raw cosine is under 0.30.
6. **Always-on floor** inside the same call: the mandatory bundle on its own 5,000-token budget, so the floor can never be starved by retrieval (§10).
7. **Methodology companion** inside the same call: deterministic floor/push/pull matching of Skill/Playbook/Technique/AntiPattern nodes by workflow state, no embeddings (§9).
8. **Render.** The hook prints the `--- WRIT RULES ---` block plus a status line, and deducts the token cost from the turn budget:
   ```
   [Writ: mode=work, phase=implementation, gates=[], violations=0]
   ```
9. **Write gate** (`PreToolUse` on Write/Edit/NotebookEdit -> `writ-pre-write-dispatch.sh` -> `POST /pre-write-check`). One combined call: gate decision, denial escalation, and file-context RAG. Denials carry a `[RULE-ID]`-prefixed reason. Bash-mediated writes (`>`, `tee`, `cp`, `sed -i`, ...) go through the same `/can-write` check via `writ-bash-write-gate.sh`, and credential paths are denied in every mode.
10. **Stop hooks.** When Claude finishes a turn: pending tests run (failures block only in implementation/complete phase), unresolved violations block, unverified quality scores block, and a deterministic punctuation gate rejects em-dash output. Blocking Stop hooks surface via stderr + non-zero exit, guarded by `stop_hook_active` so they cannot loop.
11. **Telemetry.** Throughout, events land in the typed log streams under `<install>/var/logs/<project>/` (§18): audit for decisions, friction for things worth fixing, metrics for volume, errors for exceptions.

That is one turn: relevant rules in, a hard gate on risky writes, and a durable trail out.

---

## 3. The mode system

You declare a mode per session. The mode decides what state initializes and whether gates apply. There are **five modes** (`writ/session/mode_engine.py`, `MODE_CONFIG` is the single source; a new mode is one dict entry, not new branches):

| Mode | Purpose | Gates / blocks |
|---|---|---|
| `conversation` | Discussion, questions, brainstorming | None |
| `debug` | Investigate one specific failure (the runtime lens) | Root-cause source-edit gate; evidence-first read gate (§5) |
| `investigate` | Evidence-grounded audit / explore / research | Per-lens gates (§4); advisory except web research |
| `review` | Evaluate code against rules | None |
| `work` | Build or modify code | Two phase gates (§6) |

**`mode set` vs `mode switch` vs auto-route.** This distinction is load-bearing:

- **`mode set <mode>`** initializes *fresh* state: phase reset to the mode's initial phase, gates cleared, denial counts cleared. Use it when you start a new piece of work.
- **`mode switch <mode>`** *preserves paused work state*. Mid-task in Work mode, switch to `debug` to chase a failure; switching back restores the paused phase and gates instead of resetting them.
- **Auto-route** (`classify_mode_hint`, `bin/lib/writ_mode_hint.py`) only ever fires when no mode is set, and never overwrites a live session.

**Session rotation.** If Claude Code assigns a new session id mid-conversation, the SessionStart hook carries the *mode only* forward into the fresh session, same project only. Gates are never inherited across a rotation: a rotated session re-earns its approvals.

---

## 4. Investigate mode

`investigate` unifies audit, explore, and research into one evidence-grounded engine (`writ/session/investigations.py`). The differences between an audit, an exploration, and a research task are expressed as a **lens**, not separate modes.

| Lens | `source_type` | Evidence | Gate strictness |
|---|---|---|---|
| Codebase audit / explore | `code` | File reads, greps, recorded analyses | advisory |
| Research | `web` | Captured web sources (auto-recorded from WebFetch/WebSearch) | **hard, fail-closed** |
| Runtime | `runtime` | Command output (this is what `debug` mode is) | read gate (§5) |

**Key invariants:**

- **Coverage scope freeze.** You freeze the file scope once (`--freeze-scope`); the coverage denominator cannot silently grow or shrink afterward (re-freezing requires `--force`). Coverage is examined-in-scope over the *frozen* total, so it cannot be inflated by narrowing scope after the fact.
- **Synthesis gate (advisory).** Warns when you try to conclude with zero coverage evidence. It checks presence of attention, not correctness, and says so.
- **Triangulation gate (hard).** A web-research synthesis is *blocked* until citations span at least two independent source domains. Same-site sources collapse into one. Zero captured sources blocks outright.
- **Staleness check.** Citations carry an `excerpt_hash`; two different hashes for the same source flag drift.
- **Audit fan-out.** For large scopes, the engine partitions the frozen scope into worker-sized tiles (2,000 LOC / 30 files per worker), rolls worker coverage back up, aggregates findings with contradiction detection, and ranks partitions by an attention score so the highest-signal areas surface first.

Only the triangulation gate fails closed. The code and runtime lenses are deliberately advisory: the reports state what was examined, never that the investigation is complete.

---

## 5. Debug mode

`debug` is the runtime lens of the investigation engine (`MODE_CONFIG["debug"]` carries `source_type: "runtime"`).

**Evidence-first read gate.** Code reading and searching (Read/Grep/Glob on source files) are blocked until `debug.md` has real, non-placeholder `## Evidence` and `## Narrowing` sections. Record what you observed and how you narrowed before spelunking in source; logs, docs, and Bash (the evidence-gathering tools) stay open. The gate fails open on any internal error: a gate bug must never wedge the agent.

**Root-cause gate.** Source edits are blocked until `debug.md` has a populated `## Root cause`. Investigate first, write the cause down, then change code. This stops "fix by guessing." `debug.md` itself, `plan.md`, and the excluded paths stay writable.

**Debug to Work handoff.** On the debug-to-work transition, the root cause is seeded into `plan.md` as `## Root Cause Evidence` (best-effort, idempotent, never fires when switching back into a paused implementation phase). Your diagnosis becomes the start of the plan you then gate through Work mode.

In debug mode, every Bash command's output is auto-captured into the citation log as runtime evidence, so the gate reads observed data, not self-reported claims.

---

## 6. The Work-mode gates

Work mode is the only mode that gates writes to source. The lifecycle is `planning -> testing -> implementation`, with `gate_sequence: ["phase-a", "test-skeletons"]`.

1. **`mode set work` -> `planning`.** Writes to source are blocked.
2. **Gate `phase-a` (the plan gate) -> `testing`.** `plan.md` must exist with four sections: `## Files` (each entry a path, change type, and reason), `## Analysis`, `## Rules Applied` (citing *real* rule IDs from the rules actually loaded this session; invented IDs are flagged as hallucinated), and `## Capabilities` (unchecked `- [ ]` checkboxes; pre-checked boxes are rejected).
3. **Gate `test-skeletons` -> `implementation`.** At least one test file with real assertions must exist. After this gate, source writes flow freely.

**After the last gate: the reviewer's verdict (added 2026-08-06).** The two gates end at `implementation`, so nothing in the phase machine can act on a code review, which runs later. The reviewer's contract already said "Critical blocks merge" (`agents/writ-reviewer.md`) and nothing enforced it: the verdict reached only the agent whose code was reviewed, leaving the author to adjudicate its own critic. Enforcement now sits at the commit instead of the phase advance. `writ-subagent-stop.sh` records a `writ-reviewer` verdict from the `SubagentStop` payload, so the orchestrator is never the courier, and `writ-bash-write-gate.sh` asks for human confirmation on `git commit` while CRITICAL findings stand. It asks rather than refuses on purpose: a refusal needs an override, and any override the agent can set re-opens the defect. A verdict that cannot be parsed counts as blocking, because failing to understand the critic must not read as approval. Fixing the findings and re-running the reviewer records a clean verdict, which lifts the block; asserting they are fixed does not.

The validators are presence checks by design: they confirm the artifact exists in the expected shape. A regex cannot tell a real root cause or plan from a fabricated one; what makes the gate meaningful is that *you* read the artifact before typing the approval.

**How you advance.** You approve by typing an approval phrase (pattern path: `auto-approve-gate.sh` mints the token and calls `/advance-phase`) or by `/writ-approve` (tool path: same endpoint, same token). One approval advances exactly one gate. A validation failure *spends* the token: a changed artifact needs a fresh approval.

**ExitPlanMode integration.** Leaving Claude Code's plan mode validates `plan.md` with the same validator the gate uses and resets the task phase so a stale `implementation` phase from a prior task cannot swallow the new task's first approval. It does **not** approve the gate; only your approval does.

**Gate exemptions.** Sub-agents, the Writ install's own tree, `~/.claude/settings.json` / `settings.local.json` (exact basenames, symlink-resolved), `plan.md` before a mode is set, `capabilities.md`, and the `exclusions` globs in `bin/lib/gate-categories.json` (tests, migrations, `__init__.py`, `.claude/` config and markdown). Credential paths (`.env`, keys, `~/.ssh/`, ...) are denied in every mode and override every exemption. One matcher caution: glob `*` spans `/` and matches the raw path, so keep new exclusion patterns extension-anchored or exact filenames; a directory glob exempts any depth.

**When a write slips through anyway.** Post-write validators analyze every written file. At the plan boundary, a confirmed violation of a rule that was loaded at planning time *invalidates* the phase-a gate: the next write is blocked until you re-approve a corrected plan. Three invalidation cycles escalate with a differential diagnosis of what kept failing.

---

## 7. Anti-self-approval security model

This is the keystone. Writ could otherwise let the agent approve its own writes. It cannot.

- **The token.** A 32-hex-character secret at `/tmp/writ-gate-token-<session_id>` (chmod 600).
- **Who writes it.** `auto-approve-gate.sh`, and *only* when the user's typed prompt matches an approval pattern (exact phrases, small-edit fuzzy match, bounded regexes; single source `bin/lib/approval_match.py`). The agent cannot forge your keystroke.
- **Who consumes it.** `/advance-phase` (§6) and `/promote-candidate` (§12), the only two routes that advance workflow or write canon. Claiming the token is an atomic filesystem rename, so two concurrent requests with the same token produce exactly one advance. Claiming *is* consuming: one approval, one advance.
- **What spends it.** A successful advance, or a failed validation (the artifact changed; re-approve). A request that cannot even resolve the project root does not spend it.
- **What happens without it.** The attempt is refused and logged as `agent_self_approval_blocked` on the audit stream.

In one sentence: the agent can draft, propose, and lobby, but the *write of canon* requires a token only your keystroke produces.

---

## 8. Sub-agents and orchestration

Writ ships **five named roles**, each both a `SubagentRole` graph node and a Claude Code agent file under `agents/` at the plugin root (the auto-discovered location; declaring an `agents` key in the plugin manifest would load zero of them):

| Role | Node id | Tools |
|---|---|---|
| Explorer | `ROL-EXPLORER-001` | Read, Glob, Grep, Bash (structurally read-only) |
| Planner | `ROL-PLANNER-001` | Read, Glob, Grep, Write |
| Test writer | `ROL-TEST-WRITER-001` | Read, Glob, Grep, Write, Bash |
| Implementer | `ROL-IMPLEMENTER-001` | Read, Glob, Grep, Write, Edit, Bash |
| Reviewer | `ROL-REVIEWER-001` | Read, Glob, Grep, Bash (read-only, JSON verdicts) |

`writ role-prompt <role>` prints the graph-canonical prompt; the agent files are exported *from* the graph, and a drift check keeps them byte-identical.

**Four behaviors that matter:**

- **`is_subagent` bypasses the write gates.** Workers are dispatched by an orchestrator that already passed the human gate; re-policing them would produce false denials. The trust boundary is the role definition (the explorer and reviewer physically lack Write) plus the spawn prompt. Workers also get unlimited RAG budget (`subagent_budget: null`).
- **`is_orchestrator` suppresses broad RAG.** Set with `mode set work --orchestrator`; the dispatching session skips the ~1,400-token broad injection (workers need the rules, the coordinator doesn't) and still receives the methodology companion. The flag is manual; forget it and the orchestrator pays full injection every turn.
- **Mode is inherited file-direct.** A worker reads its parent's mode from the session cache *file*, never daemon-first: a daemon whose in-memory view diverged once served `mode=None` to every worker. Worker caches are keyed by `agent_id` for isolation.
- **Worker rule usage flows back.** At commit time, the rules each worker queried per file are merged into the decision-memory record, so `queried_rule_ids` on a FileChange reflects the whole fan-out, not just the parent session.

Dispatch discipline (`writ-dispatch-discipline.sh`) governs Work, Investigate, and mode-unset sessions: a generic dispatch (`general-purpose`, `Explore`, empty) is *rewritten in place* to the matching `writ-*` role when the prompt classifies confidently, and denied-with-ask when ambiguous. Escape hatches: `[general-purpose]` or `[writ:dispatch-ok]` in the dispatch prompt.

---

## 9. The knowledge graph (operator view)

**13 node types** (`writ/graph/schema.py`): `Rule`, `Abstraction`, `Category`, `Skill`, `Playbook`, `Technique`, `AntiPattern`, `ForbiddenResponse`, `Phase`, `Rationalization`, `PressureScenario`, `WorkedExample`, `SubagentRole`. Decision-memory records (`Decision`, `FileChange`, `Commit`) are deliberately outside this registry: they never enter retrieval.

**Shipped corpus** (`writ-corpus.cypher`, 2026-07-31: **464 nodes / 731 edges**; run the census below for your live graph):

| Node type | Count | Retrieval path |
|---|---:|---|
| Rule | 287 (32 mandatory) | ranked pipeline; mandatory via `/always-on` only |
| Abstraction | 62 | summary-mode substitution (generated by `writ compress`) |
| Category | 22 | routing data, never retrieved |
| Phase | 20 | graph neighbor only |
| Skill / Playbook | 15 / 15 | companion channel + ranked pipeline |
| AntiPattern | 13 | companion channel + ranked pipeline |
| Technique | 11 | companion channel + ranked pipeline |
| SubagentRole | 7 | `/subagent-role/{name}` only |
| Rationalization / WorkedExample / PressureScenario | 4 / 3 / 3 | graph neighbor only |
| ForbiddenResponse | 2 | always-on bundle + ranked pipeline |

```bash
docker exec writ-neo4j cypher-shell -u neo4j -p writdevpass \
  "MATCH (n) WHERE n.project='writ' RETURN labels(n)[0] AS t, count(*) AS c ORDER BY c DESC;"
```

**Two retrieval channels.** Channel 1 is the ranked 5-stage pipeline plus the always-on floor (Rules and ForbiddenResponse). Channel 2 is the deterministic methodology companion: `floor_modes` (always injected in matching modes, never dropped for budget), `action_triggers` (pushed on observable actions like a gate denial or `git worktree add`, deliberately bypassing dedup because the timing is the value), and `trigger_keywords` (budget-flexible pull, trimmed first). Methodology nodes are also embedded in the ranked pool, so "methodology is never ranked" is only true of the companion channel itself.

**24 edge types**: 17 corpus edges (`DEPENDS_ON`, `PRECEDES`, `CONFLICTS_WITH`, `SUPPLEMENTS`, `SUPERSEDES`, `RELATED_TO`, `ABSTRACTS`, `TEACHES`, `COUNTERS`, `DEMONSTRATES`, `DISPATCHES`, `GATES`, `PRESSURE_TESTS`, `CONTAINS`, `ATTACHED_TO`, `BELONGS_TO`, `INVOKES`) plus 7 decision-memory record edges (`HAS_DECISION`, `HAS_CHANGE`, `HAS_COMMIT`, `MOTIVATED_BY`, `GOVERNED_BY`, `INCLUDES`, `REALIZES`).

Directed invariants enforced by `writ validate`: `DISPATCHES` targets `SubagentRole` only; `INVOKES` never targets `SubagentRole`; `TEACHES` never originates from a `Rule`.

---

## 10. The mandatory floor

A retrieval system that *ranks* safety rules is dangerous: a bad ranking day drops a critical rule off the list. Writ refuses that risk by splitting the rulebook into two pools, with **one source of truth** for the split: two Cypher predicates in `writ/graph/predicates.py`.

- **Ranked pool**: `RANKED_INCLUDE_WHERE = "r.mandatory IS NULL OR r.mandatory = false"`. Domain guidance, indexed and ranked, surfaces when relevant.
- **Injection floor**: `INJECTION_RULE_WHERE = "r.mandatory = true OR r.always_on = true"`. Reaches the agent through `/always-on`, never ranked, cannot be ranked away. The two flags overlap but differ: an advisory always-on rule (like the communication rules) is injected without being mandatory.

Both the `/always-on` endpoint and the integrity validator import these predicates, so selection and validation cannot drift. This closed the historical stranded-mandatory bug class, where two mechanisms keyed on different fields left 29 of 32 mandatory rules reachable by *neither* path. `writ validate` fails on any stranded mandatory rule and on any mismatch between the ranked-pool complement and the mandatory set.

The floor is also *scoped* so it doesn't spam: non-mandatory process rules only inject in `work` and `debug`; rules can declare an `applicability_scope` (prompt, write, bash, stop) and `trigger_keywords` so a write-time rule fires at write time against file content rather than on every prompt. A rule with no routing data is universal: nothing silently disappears for lacking metadata.

**Budget: 5,000 tokens** (`always_on_cap`, `writ/shared/budget.json`), measured on the summary render. `writ validate` fails when the bundle breaches it; the endpoint itself reports the cap but does not trim.

**How a rule earns each tier** (the original public-rulebook criteria, preserved from its design doc):

- Severity: **critical** = a violation creates an exploitable vulnerability, data loss, or system failure in production, no exceptions; **high** = bugs, maintenance debt, or security weakness that compounds over time, exceptions require documented justification; **medium** = degrades code quality or developer experience, exceptions acceptable with team agreement; **low** = style or hygiene, advisory only.
- A rule is **mandatory** (always-on, never ranked) only when ALL four hold: severity is critical; a violation is exploitable or causes data loss in production; the rule is universal across languages and frameworks; and an AI agent can mechanically detect violations from code inspection.

**The measurement consequence (established 2026-08-05).** Because mandatory rules never enter the ranked index, a /query-style benchmark scores every query targeting one as a guaranteed miss -- it is measuring the wrong delivery channel, and the "miss" is the invariant working. Two corollaries follow. First, when a mandatory rule has a non-mandatory sibling (SEC-AUTH-TOKEN-001 mandatory, -002 ranked), the sibling is the only family member in the index, so the sibling winning is structural, not a ranking defect. Second, the same channel logic applies to category-routed content: a rule whose Category routes it to the methodology-companion channel (the PROC-* process rules) can rank first on both raw retrievers and still be correctly absent from /query results. Retrieval quality must therefore be scored per delivery channel: rank metrics for index-eligible targets, presence-in-bundle verification for the always-on floor, and channel-membership verification for routed content. The benchmark implements exactly this split (section 19).

---

## 11. The corpus: `writ-corpus.cypher` and `bible/`

**The graph is canonical; the tracked source is the Cypher dump.** `bible/` is a local, gitignored export. Ingest reads three Markdown formats in strict precedence:

1. **YAML front-matter**: one node per file (must begin exactly with `---`).
2. **`<!-- NODE START type=X id=Y -->` block markers**: multi-node per file.
3. **`<!-- RULE START: id -->` legacy markers**: routed as `Rule`.

`writ/graph/methodology_ingest.py` is the unified ingest library; `scripts/migrate.py` is a deprecated shim.

**Subset imports orphan cross-type edges.** `writ import-markdown --only <type>` recreates the selected nodes, but its edge pass resolves targets only within the imported batch, so the recreated nodes' `BELONGS_TO` edges to Category nodes are skipped as dangling. The symptom is quiet and expensive: the affected ids fall off data-driven routing, Stage-1 logs `node_routes incomplete` and reverts to the legacy fallback filter, and retrieval quality degrades for routed content. Follow any `--only` import with a full `writ import-markdown` (a full pass rebuilds every edge; observed 1,061 created, 0 dangling).

**Declaring typed edges in source.** Front-matter nodes use an `edges:` list; RULE-START blocks use an `### Edges` section (`- TYPE: ID` lines). Only declared types persist this way. `RELATED_TO` is derived from rule-id mentions in prose and `BELONGS_TO` from the category field; both are re-derived on ingest, so never hand-declare them. `ABSTRACTS` is machine-generated by `writ compress`.

**Ingest is upsert-only; `writ reconcile` is the only prune.** Deleting or renaming a source node leaves an orphan until reconcile runs. Reconcile deletes graph nodes, edges, and managed properties absent from the source oracle, project-scoped, and always exempts graph-first nodes (`proposed`, `graduation_pending`) and `record` nodes, which have no markdown home by design. Run it against the *full* corpus root only: reconciling a partial source treats everything the partial omits as deleted.

**Dual-location rules.** A hand-maintained set of 13 rule ids (`_METHODOLOGY_CANONICAL_RULE_IDS`, `writ/export.py`) live canonically as front-matter under `bible/methodology/<ID>.md`. Export routes them there and excludes them from domain `rules.md`, so hand-editing those files is safe. New graduated canon must be added to that list or export produces a duplicate domain copy.

**Fields that never round-trip.** Runtime and derived fields (`confidence`, `authority`, `times_seen_*`, `evidence`, `staleness_window`, `last_validated`, `source_origin`, `provenance`) are stripped on export and restored to graph values or defaults on re-ingest; reconcile's property-clear phase is allowlisted so it can never wipe them. `mandatory` does round-trip.

**Integrity.** `writ validate` runs roughly 30 checks: structure (conflicts, orphans, staleness, near-duplicates), reachability (stranded mandatory, floor completeness, trigger-keyword parity, push reachability), parity (graph vs source for nodes, edges, and property values), edge contracts, content lint (code examples must parse), and artifact freshness. Most findings fail the build; a few (unreviewed counts, frequency staleness, graduation flags) are advisory.

---

## 12. How rules grow: propose, graduate, promote

The lifecycle is a **provenance arc** with a frequency-driven flip and an *informed human gate*.

**Provenance states** (`schema.py`, `VALID_PROVENANCE`): `hand-authored` (the corpus at rest), the graph-first arc `proposed` -> `graduation_pending` -> **human gate** -> `graduated` (exported to `bible/methodology/<id>.md`), and `record` (decision memory; permanent, never promoted).

**Step 1: propose** (`POST /propose` or `writ propose`, through `structural_gate` in `writ/gate.py`), five checks in order: schema validity; the mechanical-enforcement policy (a `mandatory` rule must cite an enforcement path); specificity (hedge words like "consider" or "where appropriate" are rejected); redundancy and novelty by embedding cosine (reject at 0.95, flag the 0.85-0.95 band); and conflicts (an unjustified `CONFLICTS_WITH` edge rejects). A passing proposal is force-stamped `authority="ai-provisional"`, `confidence="speculative"`; no caller can raise these through the propose path. It lands graph-first (`source_origin="graph-authored"`, so reconcile will not delete it) with `provenance="proposed"`, and the proposal context (task, triggering query, consulted rules) is stored write-once for the human's later review.

**Step 2: graduate (a statistical flip, not approval).** Hooks accumulate `times_seen_positive` / `times_seen_negative`. At 50+ observations with a 0.75+ positive ratio (`writ/frequency.py`), the node flips `proposed` -> `graduation_pending`, and nothing else: authority unchanged, no export, race-guarded so concurrent evaluations cannot double-flip.

**Step 3: promote (the informed human gate).** `writ review` surfaces the candidate with its content and canon-fit context (conflict targets, same-category members, high-similarity neighbors), so approval is informed, not rubber-stamped. Promotion runs through the token-gated `/promote-candidate` (§7). Approve as-is (`graduated_via="human-approve-asis"`) or edit at the gate (`graduated_via="human-edit"`, which re-runs the structural gate and resets the observation counters when the statement materially changed). The node is stamped `graduated`, `authority="ai-promoted"`, and exported to `bible/methodology/`.

---

## 13. Decision memory

Writ records *why files changed*, mechanically, and plays it back.

- **Capture.** The post-commit git hook (installed automatically on first Work-mode entry into a repo, marker-delimited, never blocks a commit) posts each commit to the daemon. Capture joins the commit's files against the approved `plan.md` (written by the planner sub-agent, read from disk), the rules each session and sub-agent queried per file, and prior open decisions. It writes `Decision`, `FileChange`, and `Commit` records with content-hash ids, so re-capture and amends merge instead of duplicating. Capture refuses to run outside a real git repo: records are never scoped to a fallback name.
- **Recall.** `writ recall` (and a once-per-session briefing on your first prompt) compiles recent decisions into a token-budgeted digest: titles and governing rules survive eviction; rationale and per-file reasons trim first. Recall is deliberately a separate query, never part of RAG ranking.
- **PR sync.** `writ pr sync` posts one comment per changed file on the open Bitbucket PR: the captured reason, the rules the AI was shown, and the rules it cited. Idempotent (updates its own comments), fail-loud on API errors, SSRF-hardened, and it never logs the token. A parallel channel writes the same bodies as git notes on `refs/notes/writ-decisions`.

**Multi-project.** One shared graph accumulates every project: composite `(id, project)` uniqueness keys, a `:Project` registry mapping names to repo roots, project-scoped reconcile, and retrieval scoped to `{project, "_shared"}` so rules never leak across projects.

---

## 14. Hooks layer (operator reference)

**Single registration.** `hooks/hooks.json` binds 12 Claude Code events to 40 scripts via `${CLAUDE_PLUGIN_ROOT}` (44 registrations; some scripts serve multiple events). One more script, `writ-statusline.sh`, is wired through the `statusLine` settings channel, not hooks. Editing a script takes effect immediately; changing `hooks.json` needs a fresh Claude Code session.

**What can block:**

| Surface | Hook | Blocks when |
|---|---|---|
| PreToolUse Write/Edit/NotebookEdit | `writ-pre-write-dispatch.sh` | Work gates unapproved, debug root-cause missing, credential path (any mode) |
| PreToolUse Bash | `writ-bash-write-gate.sh` | Bash-mediated write to a gated or credential path |
| PreToolUse Bash | `writ-bash-write-gate.sh` | `git commit` while a reviewer's CRITICAL findings stand (confirms, does not refuse) |
| PreToolUse Bash | `writ-worktree-safety.sh` | `git worktree add` into an un-ignored project-local path |
| PreToolUse Write | `pre-validate-file.sh` | Static analysis finds an error in the proposed content |
| PreToolUse Write | `validate-test-file.sh` | New source has no assertion-bearing test (the TDD gate) |
| PreToolUse Write | `validate-design-doc.sh` | A design doc misses required sections or substance |
| PreToolUse Write | `writ-memory-policy-guard.sh` | A memory write tries to weaken verification rules |
| PreToolUse Grep/Read/Glob | `writ-debug-code-gate.sh` | Runtime lens active and `debug.md` lacks Evidence + Narrowing |
| PreToolUse Task | `writ-dispatch-discipline.sh` | Ambiguous generic dispatch (confident ones are rewritten, not blocked) |
| PreToolUse ExitPlanMode | `validate-exit-plan.sh` | `plan.md` fails the phase-a validator (Work mode only) |
| Stop | `enforce-violations.sh` | Unresolved rule violations in Work mode |
| Stop | `writ-run-pending-tests.sh` | A marked test fails in implementation/complete phase |
| Stop | `writ-verify-before-claim.sh` | A quality self-review scored under 3 and was not overridden |
| Stop | `writ-comms-output-gate.sh` | The reply contains an em dash, en dash, or ` -- ` in prose |

Everything else is advisory or observational: the RAG injectors (`writ-rag-inject`, `writ-read-rag`, `writ-posttool-rag`, `writ-pre-write-dispatch`'s allow path), post-write validators (`validate-file`, `validate-rules`, `validate-handoff`), methodology pushes (`inject-tier-workflow`, `writ-bible-authoring-push`), lifecycle (`session-start-bootstrap`, `writ-precompact`, `writ-postcompact`, `writ-session-end`, `writ-pressure-audit`, `writ-cwd-changed`), sub-agent lifecycle (`writ-subagent-start/stop`), evidence capture (`writ-web-capture`), the observe-by-default read-junk gate, and the opt-in blackbox capture. One nuance: `validate-rules.sh` is advisory per write, but at the plan boundary a confirmed violation invalidates the phase-a gate, which the *next* run enforces with a blocking exit.

**Patterns every hook follows:** one-spawn envelope parsing (`load_hook_env`), daemon-first with subprocess fallback (`_writ_session`, curl timeouts in the 100-500 ms range), fail-open when the daemon is down, an EXIT trap that logs every hook execution on every exit path, and delivery through `additionalContext` (bare stdout on Pre/PostToolUse never reaches the model). Blocking Stop hooks use stderr plus a non-zero exit and check `stop_hook_active`, because a Stop hook's `additionalContext` is treated as a turn-continue and loops.

---

## 15. Configuration

**`writ.toml`** (at the install root, gitignored; template `writ.toml.example`; readers in `writ/config.py`). It has five sections:

- **`[neo4j]`**: `uri` (default `bolt://localhost:7687`), `user` (`neo4j`), `password` (**`writdevpass` is a dev-only default**, silently used when the file is absent; override for any real deployment).
- **`[hnsw]`**: `cache_dir` for the vector-index cache (default `~/.cache/writ/hnsw`).
- **`[bitbucket]`**: `email`, `token` for PR sync (absent means PR sync is off; the token is never logged).
- **`[logs]`**: `backup_dest` for `writ logs backup`.
- **`[egress]`**: `allow_hosts`, the hosts the Bash egress guard may send local data to without prompting (`get_egress_allow_hosts`, `writ/config.py`). Absent means every outbound host is questioned.

Everything else people expect to find in config is deliberately code: ranking weights (`writ/retrieval/ranking.py`: bm25 0.198, vector 0.594, severity 0.099, confidence 0.099, graph 0.01), the abstention threshold (0.30, `writ/retrieval/pipeline.py`), gate thresholds (redundancy 0.95, novelty band 0.85, `writ/gate.py` and `writ/graph/schema.py`), graduation thresholds (50 observations, 0.75 ratio, `writ/frequency.py`), and context-budget bands (summary under 2,000 tokens, standard to 8,000, full above; `writ/retrieval/ranking.py`).

**`writ/shared/budget.json`**: `default_budget=8000`, per-rule render costs full 200 / standard 120 / summary 40, `subagent_budget=null` (unlimited), `always_on_cap=5000`.

One rule this project applies to its own writing: Writ ships a forbidden-response rule that blocks the AI's own output when it contains an em dash, an en dash used as punctuation, or a double hyphen standing in for one. A Stop hook enforces it at the end of every turn, and the README and this handbook are both written to it. It is the smallest demonstration of the general mechanism: a constraint that lives at the tool boundary rather than in a style guide nobody re-reads.

**Env vars:** `WRIT_HOST`/`WRIT_PORT` (daemon target, default `localhost:8765`), `WRIT_CACHE_DIR` (session caches, default `<install>/var/session`; deliberately not `/tmp`, which systemd empties at boot), `WRIT_LOG_ROOT` (log streams, default `<install>/var/logs`), `WRIT_LOG_PROJECT`, `WRIT_FRICTION_LOG` (collapse all streams into one file), `WRIT_DEBUG` (debug sinks, default off), `WRIT_HOOK_LOG`, `WRIT_NO_AUTOSTART`, `WRIT_ALLOW_EMBEDDING_FALLBACK=1` (permit the sentence-transformers path when the ONNX model is absent), `WRIT_CONTEXT_WINDOW_TOKENS` (validated 1,000-10,000,000 at daemon startup), `WRIT_BLACKBOX=1` (raw payload capture). Neo4j credentials resolve from `WRIT_NEO4J_URI` / `WRIT_NEO4J_USER` / `WRIT_NEO4J_PASSWORD`, then `writ.toml`, then a dev-only built-in default. `WRIT_TEST_GRAPH=1` plus a non-production URI is what the destructive-wipe guard requires (`writ/graph/db/_safety.py`).

---

## 16. Operations: daemon lifecycle

**Primary: systemd user service.** `scripts/install-server-service.sh` installs `writ-server.service` (waits for Neo4j, `Restart=on-failure`) plus the daily `writ-logs-rotate.timer`.

- **Restart after code changes:** `systemctl --user restart writ-server`, **not** `stop-server.sh` (that fights systemd's auto-restart).
- **Status / logs:** `systemctl --user status writ-server`, `journalctl --user -u writ-server -f`.
- **Boot start:** `sudo loginctl enable-linger $USER`.

**Fallback: on-demand start.** The SessionStart hook and `scripts/ensure-server.sh` both go through one flock-guarded singleton start routine (`scripts/lib/writ-server-lib.sh`), so concurrent sessions cannot race up two daemons. A healthy daemon is never restarted by these paths.

**When a restart is required.** Module-level Python changes (server routes, retrieval, schema). Hook script edits: never. New `hooks.json` mappings: a fresh Claude Code session.

**Health.** `GET /health` returns status, rule and mandatory counts, category count, route distribution, index state, startup time, cache dir, and friction-log path. `status: "degraded"` means the index warmed but `rule_count == 0` (the DB and the index disagree, usually a daemon that outlived a re-seed). `writ doctor` runs 13 deeper checks and `--fix` repairs 6 of them.

---

## 17. The CLI

`writ <command>` (Typer, entry point `writ.cli:app`), grouped:

- **Serve and query:** `serve`, `status`, `query` (server-first, in-process fallback).
- **Corpus:** `import-cypher` / `export-cypher` (the tracked dump; import wipes and replays), `import-markdown` (upsert-only; `--only`, `--dry-run`, `--compress`), `export`, `reconcile` (the only prune), `prune` (misleadingly named: it only *reports* parity violations, deletes nothing), `validate` (~30 integrity checks), `compress`, `migrate` (deprecated shim).
- **Authoring:** `add`, `edit`, `propose`, `review` (`--promote --reject --downweight --stats`), `feedback`, `role-prompt`.
- **Decision memory:** `git-hooks install|uninstall|bootstrap`, `harvest` (backfill from git + transcripts), `recall`, `pr sync`.
- **Operations:** `doctor` (13 checks, `--fix`, `--net`), `logs tail|stats|list|rotate|backup`.
- **Analytics:** `analyze-friction` (six mutually-exclusive lenses: rule effectiveness, skill usage, playbook compliance, graduation candidates, trim candidates, quality-judge false positives), `audit-session`, `token-audit` (transcript cost scorecard), `corpus-footprint` (per-rule token cost), `efficacy-ab` (live A/B harness; dry-run by default, real `claude` spawns behind `--live`).

The hook-facing session CLI is separate: `bin/lib/writ-session.py <subcommand>` drives mode, gates, coverage, citations, and cache state; hooks call it when the daemon is unreachable.

---

## 18. Logging and observability

**Typed streams** (`writ/shared/logging.py`), one directory per project under `<install>/var/logs/`:

| Stream | Holds | Retention |
|---|---|---|
| `audit.jsonl` | Governance decisions: gate allow/deny, mode changes, phase advances, self-approval blocks, promotions, verification evidence, citations | 365 days |
| `friction.jsonl` | Things worth fixing: repeated denials, hallucinated rule ids, capture failures, fallbacks | 365 days |
| `metrics.jsonl` | Volume telemetry: hook timings, daemon requests, retrieval results, RAG injections | 90 days |
| `errors.jsonl` | Exceptions (`emit_exception`), bounded tracebacks | 365 days |

Every writer (Python and bash) funnels through one router that never raises: a failed write degrades to `_fallback.jsonl` rather than dropping a security event. Files rotate at 50 MB at the source, and a daily systemd timer sweeps, gzips, and prunes; the analyzers read archives plus live files, so rotation never blinds them. `writ logs tail|stats|list` is the read surface; `GET /dashboard` renders the same analyzer lenses as HTML.

**Caveat for the Writ self-repo:** the pre-2026-07 legacy log is ~86% test-synthetic; real project streams are clean. Read historical self-repo friction numbers with that in mind.

## 18b. Exploring the graph

**`/explore` (everyone).** With the daemon up, open `http://localhost:8765/explore`: a live, data-backed page with a query playground (`POST /query` with real results) and a graph explorer over `/graph` and `/node/{id}` (filter by type, domain, provenance, mandatory; click for statements and neighbors). Read-only by construction. The static companion site with the architecture diagrams is `docs/architecture/index.html`.

**Neo4j Browser (developers).** `http://localhost:7474` (Bolt `bolt://localhost:7687`, dev creds `neo4j`/`writdevpass`) for raw Cypher:

```cypher
MATCH (n) WHERE n.project = 'writ' RETURN labels(n)[0] AS type, count(*) ORDER BY count(*) DESC;
MATCH (r:Rule {rule_id: 'SEC-INJ-SQL-001'})-[e]-(m) RETURN r, e, m;
```

---

## 19. Testing and benchmarks

```bash
make test    # pytest tests/ -x -q
make bench   # benchmarks/bench_targets.py, the contractual perf floors
make check   # test + bench + writ validate
```

398 test modules, 7,137 collected tests. Always run with the venv interpreter (`.venv/bin/python`); the system interpreter lacks `onnxruntime` and fails the embedding tests. Roughly half the suite needs a reachable Neo4j: unreachable skips, but a reachable-and-empty graph *fails* by design, so a broken corpus can never masquerade as a skip. The suite runs on its own daemon port (8799), isolates caches and logs per test, and restores the shipped corpus from `writ-corpus.cypher` when it finishes.

Benchmarks: `bench_targets.py` (14 pass/fail targets: cold start, memory, per-stage latency, retrieval floors), `scale_benchmark.py` (the synthetic 80/500/1K/10K curve; wipes and restores), `methodology_bench.py` (read-only), `run_benchmarks.py` (traversal latency at 1K/10K).

---

## 20. By the numbers

Per-stage and scale figures live in `SCALE_BENCHMARK_RESULTS.md` (dated measurements; both the synthetic curve and the live measurement below are 2026-08-01, at the 287-rule corpus, on the machine described in that file's "Measurement environment" section):

| Stage | p95 @ 287 rules (live) | p95 @ 10K rules (synthetic) |
|---|---:|---:|
| BM25 (Tantivy) | 0.250 ms | 0.323 ms |
| Vector (hnswlib) | 0.140 ms | 0.102 ms |
| Adjacency cache | 0.004 ms | 0.002 ms |
| Ranking | 0.112 ms | 0.418 ms |
| **End to end** | **0.6 ms** | **0.827 ms** |

The headline is **context reduction at scale**: retrieved tokens stay flat (~1,600-2,000) while stuffing scales linearly, a 749x reduction at 10,000 rules. Latency stays sub-millisecond because every index is pre-warmed in memory; retrieval does no synchronous I/O.

Retrieval quality gates are *floors*, not targets (MRR@5 >= 0.45 on the 47 ambiguous queries, hit rate >= 0.75 on all 193; measured 0.5681 / 0.7824 on 2026-08-01). The floors were deliberately walked down as the corpus grew 4x; the history and rationale live in `tests/fixtures/regression_floors.py`.

---

## 21. What's solid, what's still moving

**Solid:** the 5-stage pipeline with abstention; the five-mode / two-gate model and the token-based anti-self-approval; sub-agent isolation and dispatch discipline; the mandatory-floor single-source predicates and their validators; decision memory end to end (capture, recall, PR sync); the typed logging program; ONNX inference with verified PyTorch ranking parity; HNSW persistence with corpus-hash and checksum guards; plugin install with auto-discovered hooks and agents.

**Still moving (true seams as of 2026-07-31):**

| Seam | Status |
|---|---|
| ~~Authority-preference re-ranking~~ | RESOLVED 2026-08-06: configurable via `[retrieval] authority_preference_threshold`, still defaulting to 0.0 (a sweep measured no change from enabling it, because the corpus's one ai-provisional node is not semantic-routed). The sticky-tiebreak coupling is fixed: the preference now runs last. |
| ~~Bundle-cohesion ranking weight~~ | RESOLVED 2026-08-06: deleted. No weight improved all three metrics, and the methodology channel it was built for shipped deterministic instead. See `benchmarks/RANKING-LEVERS-2026-08-06.md`. |
| `/analyze` LLM escalation | Real code, but the `anthropic` SDK is not a dependency; without installing it, escalation returns "SDK not installed" placeholders. |
| BM25 index persistence | The vector index persists across restarts; the BM25 index rebuilds every start. |
| `debug` log stream | Reserved retention entry, never written; debug output stays in gated `/tmp` sinks. |
| Marketplace submission | Install works end to end; community-marketplace listing still pending. |

---

## 22. Glossary

**Always-on bundle.** The mandatory rules plus ForbiddenResponse nodes (plus any Skill/Playbook flagged always-on), injected via `/always-on` on its own 5,000-token cap. Never ranked.

**Abstention.** The pipeline returns nothing (`mode: "abstained"`) when the best raw vector cosine is under 0.30; injecting weak matches proved worse than injecting nothing. Gated at the daemon call site, on raw cosine, because the normalized score cannot separate off-domain queries.

**Category.** A node type that defines retrieval routing as *data* (its `routes` field) rather than a central table. Categories form a tree via `BELONGS_TO`.

**Channel 1 / Channel 2.** Ranked retrieval plus the always-on floor (rules) versus deterministic floor/push/pull methodology matching (skills, playbooks, techniques, anti-patterns).

**`floor_modes` / `action_triggers` / `trigger_keywords`.** The Channel-2 matching fields: modes where a node always injects; actions that push it (bypassing dedup); keywords for budget-flexible pull.

**Gate token.** The single-use secret at `/tmp/writ-gate-token-<sid>` that only your typed approval mints; claiming it is an atomic rename, and claiming is consuming.

**graduation_pending.** The provenance state after the frequency flip: a statistical signal awaiting the human promotion gate. It is not approval.

**INVOKES.** An edge meaning the orchestrator applies a methodology inline, one level deep. Must never target a `SubagentRole` (that's `DISPATCHES`).

**project.** The isolation key (composite `(id, project)` uniqueness) that lets one Writ graph accumulate many projects without collision.

**provenance.** A node's lifecycle state: `hand-authored`, `proposed`, `graduation_pending`, `graduated`, or `record` (decision memory: permanent, parity-exempt, never promoted).

**Sticky tiebreak.** Within a 0.02 score window of the group best, previously injected rules keep their order across turns, for prompt-cache friendliness.
