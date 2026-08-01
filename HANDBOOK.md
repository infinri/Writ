# The Writ Handbook

The operating manual for Writ: what it is, what happens on a turn, and how to run it. This is the *how to use it* guide. For the *why it's built this way* design rationale, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); this handbook points there for deep internals and does not duplicate them.

> **North star.** Writ relocates oversight; it does not remove it. The gate is the product. Writ never automates self-approval: a human token-gates any write of canon. Every "the agent could otherwise..." seam in this doc is closed by a human-held token, not by trust.

Every load-bearing number below is anchored to a file, a line, or a command you can run. Where this handbook and the code disagree, the code wins. Live counts were measured against project `writ` on 2026-06-16.

---

## 1. What Writ is, and why

**In one line:** Writ is a local service that gives Claude Code two things on every turn: the *right rules for what you're doing now*, and a *workflow gate that you control*.

Two problems motivated it.

- **Context stuffing does not scale.** Pasting a whole rulebook into every prompt works at ten rules and collapses at ten thousand: the model can't tell which rules are load-bearing, cache hit rates fall, and the bill scales with your rulebook instead of your work. Writ replaces stuffing with *relevance* - it retrieves only the rules that fit this file, this domain, this turn.
- **Process discipline cannot live in a prompt.** A skill file can *say* "write the failing test first," but nothing stops the model from writing the implementation first anyway. Writ moves discipline to the boundary where Claude actually calls tools: a write that violates the workflow is *denied*, not just discouraged.

**The four runtime pieces** (see ARCHITECTURE §1):

| Piece | Role | Where |
|---|---|---|
| FastAPI daemon | The single HTTP service all hooks talk to (retrieval, session state, gates, self-authoring). Binds `127.0.0.1:8765`. | `writ/server.py` |
| Hooks + session state | Thin bash hooks intercept the Claude Code tool lifecycle; a Python package tracks mode/phase/budget/gates per session. | `hooks/`, `writ/session/` |
| Neo4j canonical store | The graph is the source of truth for all rules and methodology. Runs in Docker (`writ-neo4j`). | `writ/graph/db.py` |
| The CLI | Operator control surface: ingest, export, reconcile, validate, author, query, analyze. | `writ/cli.py` |

**Canonical vs derived.** Neo4j is canonical. The `bible/` Markdown tree is a *derived, exported view* (think `dist/`). When they disagree, the graph wins on `reconcile`. Do not hand-edit exported Markdown expecting it to be authoritative - the exception is dual-location canonical files under `bible/methodology/` (§11).

---

## 2. A single turn, end to end

You type a prompt. Claude Code fires `UserPromptSubmit`. Here is what runs, in order:

1. **Daemon ensure + RAG inject** (`writ-rag-inject.sh`, the primary path). It health-checks the daemon (`/health`), starts `writ-neo4j` and the server if needed, then POSTs your prompt to `/query` and `/always-on`.
2. **Session read.** The hook reads the session cache: which mode, which phase, which rules are already loaded, how much of the turn's token budget is left (`default_budget=8000`). If no mode is set, the hook nudges Claude to declare one. If context is over ~75% full or budget is spent, the hook stays quiet.
3. **Retrieval** (`/query`). The daemon runs the 5-stage pipeline (ARCHITECTURE §6) against pre-warmed in-memory indexes: domain/route filter → BM25 (Tantivy) → ANN (hnswlib) → graph traversal → rank + budget trim. It returns a ranked, budget-capped JSON list.
4. **Always-on inject** (`/always-on`). A separate path returns the mandatory floor plus `ForbiddenResponse` nodes, on its own 5000-token budget so the floor can never be starved by retrieval (§10).
5. **Methodology companion.** Skill/Playbook/Technique/AntiPattern guidance is *also* injected. Today the companion still rides the `/query` path; the deterministic `/methodology-companion` channel exists but is built-not-wired (§4, ARCHITECTURE §7).
6. **Render.** The hook formats a compact `--- WRIT RULES ---` block, deducts its token cost from the turn budget, and prints it. Claude Code injects it ahead of the reply. A status line leads each turn:
   ```
   [Writ: mode=work, phase=implementation, gates=[], violations=0]
   ```
7. **Write gate** (`PreToolUse` → `writ-pre-write-dispatch.sh` → `POST /pre-write-check`). On a file write, the daemon consults session state and may deny with a `[RULE-ID]`-prefixed reason (e.g. a plan-gate denial in Work mode before `plan.md` is approved).
8. **Stop hooks.** When Claude finishes a turn, Stop hooks run pending tests (in implementation/complete phase, `writ-run-pending-tests.sh`), self-review (`writ-quality-judge.sh`), and verification. Any *blocking* Stop hook must check `stop_hook_active` or Claude loops to its 9-turn block cap (§14).
9. **Friction log.** Throughout, events append to `workflow-friction.log` (NDJSON): rule loads, gate denials, phase transitions, hook timing, sub-agent completions. This is the learning loop (§18).

That is one turn: relevant rules in, a hard gate on risky writes, and a telemetry trail out.

---

## 3. The mode system

You declare a mode per session. The mode decides what state initializes and whether gates apply. There are **five modes** (`writ/session/mode_engine.py`, `VALID_MODES = set(MODE_CONFIG)`):

| Mode | Purpose | Gates / source-edit block |
|---|---|---|
| `conversation` | Discussion, questions, brainstorming | None |
| `debug` | Investigate one specific failure (runtime lens) | Root-cause source-edit gate (§5) |
| `investigate` | Evidence-grounded audit / explore / research | Per-source-type gates (§4); advisory by default |
| `review` | Evaluate code against rules, read-only | None |
| `work` | Build or modify code | Two phase gates (§6) |

`MODE_CONFIG` is the single source for per-mode gate behavior - a new mode is one dict entry, not new branches.

**`mode set` vs `mode switch`.** This distinction is load-bearing:

- **`mode set <mode>`** initializes *fresh* state for that mode (clears phase, gates, budget for the new mode's lifecycle). Use it when you start a new piece of work.
- **`mode switch <mode>`** *preserves paused work state*. If you're mid-task in Work mode and switch to `debug` to chase a failure, switching back resumes the paused Work phase/gates rather than resetting them.

**Mode auto-routing.** `classify_mode_hint` (defined in `bin/lib/writ_mode_hint.py`, re-exported from `mode_engine.py` as the single definition) suggests a mode from the prompt text; you still confirm.

---

## 4. Investigate mode

`investigate` unifies audit, explore, and research into one evidence-grounded process (the "investigation engine," INV-1..9, in `writ/session/gates.py` and the session package). The differences between an audit, an exploration, and a research task are expressed as a **lens**, not as separate modes.

**The source-type lens** (`source_type` on the mode config) selects what counts as evidence and how strict the gates are:

| Lens | `source_type` | Evidence | Gate strictness |
|---|---|---|---|
| Codebase / audit / explore | `code` (or `None` until set) | File reads, greps | advisory |
| Research | `web` | Captured web sources | hard (overrides to fail-closed) |
| Runtime | `runtime` | Command output (this is what `debug` mode is - the runtime lens) | INV-9 read gate (below) |

**Key invariants:**

- **Coverage scope freeze (INV).** Once an investigation's scope is fixed, the coverage map is frozen; partitioning into worker assignments uses a first-fit-decreasing algorithm. You cannot silently widen scope mid-investigation.
- **Synthesis gate (advisory).** Findings should be synthesized, not dumped; this gate warns but does not fail.
- **Triangulation gate (hard, fail-closed).** A conclusion must be supported by ≥2 independent evidence domains. This one fails closed when strictness is hard (research). See `gates.py:109` (advisory baseline) and the strictness override.
- **Evidence + Narrowing read gate (INV-9).** In the runtime lens, code reading and searching are *blocked* until both `## Evidence` and `## Narrowing` sections of the investigation/debug doc have real (non-placeholder) content (`_validate_evidence_narrowing`, `gates.py:152`). This forces you to record what you observed and how you narrowed before you go spelunking in source - the discipline that separates investigation from flailing.
- **Staleness check.** Evidence excerpts are stamped with an `excerpt_hash`; if the underlying source changed, the citation is flagged stale.

`aggregate_findings` ranks findings by an attention formula so the highest-signal items surface first.

---

## 5. Debug mode

`debug` is the runtime lens of the investigation engine (`MODE_CONFIG["debug"]` carries `source_type: "runtime"`). Its command citations are runtime evidence.

**Root-cause gate.** Source edits are *blocked* until `debug.md` has a populated `## Root cause` section. You investigate first, find the cause, write it down, and only then are you allowed to change code. This stops "fix by guessing."

**INV-9 read gate also applies** (§4): in the runtime lens, code read/search is gated behind recorded Evidence + Narrowing.

**Debug → Work handoff.** When you transition from `debug` to `work`, `_promote_root_cause_to_plan` seeds `plan.md` from `debug.md` (best-effort; it never raises). Your diagnosis becomes the starting point of the plan you'll then gate through Work mode.

---

## 6. The Work-mode gates

Work mode is the only mode that gates writes to source. The lifecycle is `planning → testing → implementation → complete`, with `gate_sequence: ["phase-a", "test-skeletons"]` and `phase_after_gate: {phase-a: testing, test-skeletons: implementation}`.

1. **`mode set work` → `planning`.** Writes to source are blocked.
2. **Gate phase-a (the plan gate) → `testing`.** A `plan.md` must exist with four sections - `## Files`, `## Analysis`, `## Rules Applied`, `## Capabilities` - and the plan must cite *real* rule IDs from the rules loaded in the session, not invented ones. Test skeletons are also expected at this stage.
3. **Gate test-skeletons → `implementation`.** At least one test file with real assertions (not empty bodies) must exist. After this gate, source writes flow freely.

**How you advance a phase.** When you (the human) approve - by typing an approval phrase - `auto-approve-gate.sh` writes the gate token (§7) and the phase advances. `/advance-phase` consumes the token.

**ExitPlanMode integration.** `validate-exit-plan.sh` validates the plan's *format* on a successful `ExitPlanMode` and resets the task phase (`--reset-task-phase`). It does **not** create the phase-a gate - that gate is created by `auto-approve-gate.sh` on genuine user approval. (A common misconception is that `ExitPlanMode` clears the gate; it only validates and resets phase.)

**Gate exemptions.** Sub-agents, the skill dir, settings files, `plan.md` before a mode is set, `capabilities.md`, and the paths listed in `gate-categories.json` `exclusions` (tests, migrations, `__init__.py`, etc.) are exempt from write blocking. Deny reasons carry a `[RULE-ID]` prefix. Under a `.claude/` directory only CONFIG is exempt (`settings.json`, `settings.local.json`, and `*.md`): a blanket `*/.claude/*` exclusion left every implementation file under `~/.claude/skills/<name>/` ungated, and directory globs like `*/.claude/agents/*` re-opened it at any depth (the matcher's `*` spans `/`, so `..` in a path escaped the directory entirely). Keep new patterns extension-anchored or exact filenames.

---

## 7. Anti-self-approval security model

This is the keystone. Writ could otherwise let the agent approve its own writes. It cannot.

- **The token.** A 32-hex-character secret at `/tmp/writ-gate-token-<session_id>` (chmod 600).
- **Who writes it.** `auto-approve-gate.sh`, and *only* when the user's prompt matches an approval pattern (exact phrases, Levenshtein ≤2, length-bounded regex). This is input the agent cannot forge - it must come from the human's typed turn.
- **Who consumes it.** `/advance-phase` (§6) and `/promote-candidate` (§12) - the only two routes that write canon. The token is consumed on success, so **one user approval authorizes exactly one advance**.
- **What happens without it.** A phase advance or promotion attempted without a valid token logs an `agent_self_approval_blocked` friction event and is refused (`server.py` around the advance-phase handler).

In one sentence: the agent can draft, propose, and lobby, but the *write of canon* requires a token only your keystroke produces.

---

## 8. Sub-agents and orchestration

Writ supports multi-session workflows (an orchestrator delegating to workers) as a first-class feature. There are **five named roles**, each both a `SubagentRole` graph node and a Claude Code agent file under `agents/` (the plugin root's auto-discovered location):

| Role | Node id | Agent file | Default model |
|---|---|---|---|
| Explorer | `ROL-EXPLORER-001` | `writ-explorer.md` | per `model_preference` on the node |
| Planner | `ROL-PLANNER-001` | `writ-planner.md` | per `model_preference` |
| Test writer | `ROL-TEST-WRITER-001` | `writ-test-writer.md` | per `model_preference` |
| Implementer | `ROL-IMPLEMENTER-001` | `writ-implementer.md` | per `model_preference` |
| Reviewer | `ROL-REVIEWER-001` | `writ-reviewer.md` | per `model_preference` |

(`writ role-prompt <role>` prints the graph-canonical prompt template; the agent files are exported *from* the graph, so the graph is authoritative.)

**Three behaviors that matter:**

- **`is_subagent` bypasses gates.** When the orchestrator dispatches a worker, `writ-subagent-start.sh` creates an isolated session with `is_subagent: true`. The worker bypasses the Work gates - the orchestrator already cleared them, and re-policing the worker would just produce false denials. Sub-agents also get unlimited budget (`subagent_budget: null`).
- **`is_orchestrator` suppresses broad RAG.** Set the orchestrator session with `--orchestrator` and `is_orchestrator: true` tells the daemon to suppress broad rule injection on the orchestrator (it coordinates; the workers need the rules) and emit a compact status line instead. This is the ~1400-token RAG saving.
- **Mode is inherited FILE-DIRECT.** A worker reads its parent's mode from the session cache *file*, not daemon-first - this avoids a `mode=None` race where the daemon hasn't yet seen the parent's mode. Caches are keyed by `agent_id` for isolation (with one deliberate exception: `writ-mark-pending-test.sh` uses the RAW session id).

Dispatch discipline (`writ-dispatch-discipline.sh`) steers generic dispatches toward the named roles in Work and Investigate modes; escape hatches are `[general-purpose]` and `[writ:dispatch-ok]`.

---

## 9. The knowledge graph (operator view)

**13 node types** (`writ/graph/schema.py`, `NODE_ID_FIELDS`):
`Rule`, `Abstraction`, `Category`, `Skill`, `Playbook`, `Technique`, `AntiPattern`, `ForbiddenResponse`, `Phase`, `Rationalization`, `PressureScenario`, `WorkedExample`, `SubagentRole`.

**Live counts** (project `writ`, 2026-06-16; total **399 nodes / 1048 edges**; run `writ validate` for current):

| Node type | Count | Retrievable? |
|---|---:|---|
| Rule | 286 | yes |
| Category | 22 | bundle / routing |
| Phase | 20 | bundle only |
| Playbook | 15 | yes |
| Skill | 15 | yes |
| AntiPattern | 13 | yes |
| Technique | 11 | yes |
| SubagentRole | 5 | bundle only (via `/subagent-role/{name}`) |
| Rationalization | 4 | bundle only |
| WorkedExample | 3 | bundle only |
| PressureScenario | 3 | bundle only |
| ForbiddenResponse | 2 | yes (also in the always-on bundle) |
| Abstraction | 0 | yes (generated on demand by `writ compress`) |

Census command:
```bash
docker exec writ-neo4j cypher-shell -u neo4j -p writdevpass \
  "MATCH (n) WHERE n.project='writ' RETURN labels(n)[0] AS t, count(*) AS c ORDER BY c DESC;"
```

"Retrievable" = the pipeline can return it directly. "Bundle only" = it surfaces only as a graph neighbor of a retrievable node (Stage 4 traversal).

**17 edge types** (`writ/graph/db.py`, `ALLOWED_EDGE_TYPES`):
`DEPENDS_ON`, `PRECEDES`, `CONFLICTS_WITH`, `SUPPLEMENTS`, `SUPERSEDES`, `RELATED_TO`, `ABSTRACTS`, `TEACHES`, `COUNTERS`, `DEMONSTRATES`, `DISPATCHES`, `GATES`, `PRESSURE_TESTS`, `CONTAINS`, `ATTACHED_TO`, `BELONGS_TO`, `INVOKES`.

Three directed invariants are enforced by `writ validate`: `DISPATCHES` targets `SubagentRole` only; `INVOKES` never targets `SubagentRole`; `TEACHES` never originates from a `Rule` (ARCHITECTURE §3).

---

## 10. The mandatory floor

A retrieval system that *ranks* safety rules is dangerous: a bad ranking day drops a critical rule off the list. Writ refuses that risk by splitting the rulebook into two pools, with **one source of truth** for the split: two Cypher predicates in `writ/graph/predicates.py`.

- **Ranked pool** - `RANKED_INCLUDE_WHERE = "r.mandatory IS NULL OR r.mandatory = false"`. Domain guidance, indexed and ranked, surfaces when relevant.
- **Injection (floor)** - `INJECTION_RULE_WHERE = "r.mandatory = true OR r.always_on = true"`. Reaches the agent every turn via `/always-on`, never ranked, cannot be ranked away. The two flags are orthogonal-but-overlapping: an advisory always-on rule (like `ENF-COMMS-001`) is injected without being mandatory.

Both the `/always-on` endpoint and the integrity validator import these predicates, so selection and validation cannot drift. This closes the historical "stranded-mandatory" bug class, where two mechanisms keyed on different fields left mandatory rules reachable by *neither* path:

- **`detect_stranded_mandatory`** fails `writ validate` on any mandatory rule reachable by neither the ranked pool nor injection (a UNION-predicate fix: the complement of the ranked pool must equal exactly `{mandatory}`, asserted by `detect_ranked_exclusion_mismatch`).
- **`/always-on` post-1.7** returns the mandatory `Rule` set *plus* all `ForbiddenResponse` nodes (and any Skill/Playbook flagged always-on).

**Budget cap = 5000 tokens** (`always_on_cap`, `budget.json`), measured on the *summary* render (trigger + statement). `detect_always_on_budget_breach` fails validate if the bundle exceeds it; corpus growth breaches near ~54 always-on rules.

---

## 11. The corpus: `bible/` and `methodology/`

**The graph is canonical; `bible/` is a derived export** (§1). Ingest reads three Markdown formats in strict precedence (ARCHITECTURE §4):

1. **YAML front-matter** - one node per file (must begin exactly with `---\n`).
2. **`<!-- NODE START type=X id=Y -->` block markers** - multi-node per file.
3. **`<!-- RULE START: id -->` legacy markers** - routed as `Rule`.

`writ/graph/methodology_ingest.py` is the unified ingest library (it replaced the old Rule-only loop in `cli.py` and the methodology loop in `scripts/migrate.py` - those are no longer the entry points).

**Declaring typed edges in source.** A node declares its outgoing edges where it lives, and they round-trip through export:
- **Front-matter nodes** use an `edges:` list: `- { target: <ID>, type: <TYPE> }`.
- **`<!-- RULE START -->` rules** use an `### Edges` section whose body is lines `- <TYPE>: <ID>`.

Only *declared* types persist this way. `RELATED_TO` is derived from rule-id mentions in prose, and `BELONGS_TO` from the `**Category**:` field - both are re-derived on ingest, so do **not** hand-declare them (export omits them from `### Edges` for the same reason). Edit the source, then `writ import-markdown bible/` to land the edges; `writ validate` (edge-parity) confirms the graph matches the source oracle. `ABSTRACTS` is the exception: it is machine-generated by `writ compress` (a graph-only materialized view, not authored in `bible/`).

**Regenerating the abstraction layer.** `writ import-markdown bible/ --compress` re-runs the compression pipeline after ingest, regenerating the graph-only `Abstraction` layer in one step (maintainer-run; needs the `[fallback]` sentence-transformers dep, else it warns and continues without failing the ingest). Default is `--no-compress`: without the flag the abstraction layer is simply absent. The layer is a regenerable materialized view, **not** source in `bible/`.

**Dual-location rules.** A small hand-maintained set of `Rule` ids (`_METHODOLOGY_CANONICAL_RULE_IDS`, `export.py`, e.g. `ENF-COMMS-001`) live canonically as front-matter under `bible/methodology/<ID>.md`. Export routes them there and excludes them from the domain `rules.md`, so there's no lossy duplicate. **Hand-editing these files is safe** - export will not overwrite them. New graduated canon must be added to this list or it will re-export to a domain file.

**`methodology/` is hand-authored.** Methodology nodes (Skill, Playbook, etc.) are authored as source, not generated. They are not auto-exported the way derived Rule files are.

**Fields that never round-trip** (`GRAPH_ONLY_FIELDS`): `confidence`, `evidence`, `staleness_window`, `last_validated`, `authority`, `times_seen_*`, `last_seen`, `source_origin`. They are stripped on export and re-derived to defaults on re-ingest. `mandatory` *is* written and round-trips. `provenance` is omitted from export only when it holds the default `hand-authored`.

---

## 12. How rules grow: propose → graduate → promote

This replaces the older "run `writ review` to grant authority" narrative. The current model is a **four-state provenance lifecycle** with a frequency-driven flip and an *informed human gate* (ARCHITECTURE §14).

**Provenance states** (`schema.py`, `VALID_PROVENANCE`):
`hand-authored` (the bible at rest) — or the graph-first arc: `proposed` → `graduation_pending` → **human gate** → `graduated` (exported to `bible/methodology/<id>.md`).

**Step 1 - propose** (`POST /propose`, `writ/gate.py` `structural_gate`, 5 checks in order):
1. **Schema** - Pydantic `Rule` validity.
2. **Mechanical-enforcement-path policy** - a `mandatory=True` rule with no `mechanical_enforcement_path` is rejected (the alternative is `mandatory=False`, advisory).
3. **Specificity** - vague-language disqualifiers in trigger/statement are rejected.
4. **Redundancy / novelty** - cosine similarity: reject ≥ `0.95` (redundant) or in the `0.85`–`0.95` novelty band (likely an undiscovered duplicate). Thresholds in `writ.toml [gate]`.
5. **Conflict** - an unjustified `CONFLICTS_WITH` edge to an existing rule rejects.

A passing proposal is force-stamped `authority="ai-provisional"`, `confidence="speculative"` - **no caller can raise these through the propose path** (`gate.py` `propose_rule`) - and ingested as graph-first (`source_origin="graph-authored"`, so reconcile won't delete a node with no Markdown home). For the graduation arc to engage, the candidate carries `provenance="proposed"`: the frequency flip (below) acts only on nodes where `r.provenance = 'proposed'` (`db.py:748-761`). The proposal context (task, triggering query, consulted rules) is recorded write-once in `OriginContextStore` for the human's later review.

**Step 2 - graduate (statistical flip, not approval).** Hooks accumulate `times_seen_positive` / `times_seen_negative`. Frequency thresholds (`writ/frequency.py`, `writ.toml [frequency]`): `DEFAULT_GRADUATION_THRESHOLD = 50` total observations, `DEFAULT_GRADUATION_RATIO_MIN = 0.75` positive ratio. Below 50: no decision. Above 50 with ratio < 0.75: flagged for human review. Above 50 with ratio ≥ 0.75: the flip from `proposed` → `graduation_pending` **only**. It never changes authority and never writes to `bible/`.

**Step 3 - promote (the informed human gate).** `build_promotion_review_artifact()` (`writ/promotion.py`) aggregates three conflict signals so the human is not rubber-stamping: `CONFLICTS_WITH` targets, same-category members, and high-similarity neighbors (cosine ≥ `0.7`). `promote_candidate()` is the edit-at-gate path: approve as-is (`graduated_via="human-approve-asis"`) or supply `edited_fields` (`graduated_via="human-edit"`, which **re-runs the structural gate** before export). Promotion goes through the token-gated `/promote-candidate` (§7) and exports the node to `bible/methodology/<id>.md`.

> Known deferred gap: a substantive edit at the gate does **not** reset or fork the observation counts (`promotion.py` open question; ARCHITECTURE §20 #5).

---

## 13. Multi-project foundation

Writ is **one shared graph accumulating all projects** - it is meant to become the center of knowledge across everything you build, not a per-repo install.

- **Isolation by `project` property + composite `(id, project)` keys.** Two projects may hold the same logical id without collision (`apply_constraints()` creates a per-label composite uniqueness constraint).
- **`:Project` registry** (`create_project` / `get_projects` / `resolve_project_for_cwd`) maps a project name to its `repo_root` and `bible_root`. Registry nodes carry no `project` property and survive project-scoped reconciles.
- **Scoped reconcile** is project-scoped (default `project='writ'`) and never touches another project's data.
- **Retrieval scopes to `{project, '_shared'}`** - the cross-project anti-leak guarantee. `create_edge` matches endpoints *within* `$project`, so an edge never resolves to another project's same-id node.

> **Data-integrity seams CLOSED (commit 912a29e).** The three latent multi-project seams are fixed before any second project: `Abstraction` is on the composite `(id, project)` key (and `create_abstracts_edge` / `delete_abstractions` / `write_abstractions_to_graph` are project-scoped); edge-resolution `known_ids` is project-scoped via `get_all_rules(project=)` (a cross-project id reference goes dangling, not silently resolved); and the `create_edge` OR-match is derived from `NODE_ID_FIELDS`. See ARCHITECTURE §15, §20.

---

## 14. Hooks layer (operator reference)

**Single registration.** `hooks/hooks.json` (not `.claude/hooks/hooks.json`) binds every Claude Code event to a script via `${CLAUDE_PLUGIN_ROOT}`. It registers **41 hook scripts** across twelve events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `PostCompact`, `Stop`, `SubagentStart`, `SubagentStop`, `CwdChanged`, `SessionEnd`. (A standalone `~/.claude/settings.json` path is legacy and being sunset; the two must stay in sync until then.)

**Advisory vs blocking.** All hooks are advisory (`exit 0`) **except**:
- Blocking `PreToolUse` gate denials: `writ-pre-write-dispatch.sh`, `writ-memory-policy-guard.sh`, `writ-verify-before-claim.sh`, `writ-worktree-safety.sh`, `writ-bash-write-gate.sh` (gates file writes done through Bash: `>`/`tee`/`dd`/`cp`/`mv`/`sed -i`; denies credential-path writes in any mode).
- The `Stop` test-failure hook `writ-run-pending-tests.sh` (in implementation/complete phase).

The full inventory (34 scripts), grouped by what they do:

- **Prompt / RAG:** `writ-rag-inject.sh` (primary RAG + daemon auto-start), `writ-read-rag.sh`, `inject-tier-workflow.sh`, `session-start-bootstrap.sh`, `writ-cwd-changed.sh`.
- **Write gating & validation** (grouped by function; only the `PreToolUse` ones *block* per "Advisory vs blocking" above -- the `PostToolUse` validators run after the write and are advisory): *PreToolUse (blocking):* `writ-pre-write-dispatch.sh` (consolidates gate-approval + final-gate + pretool-rag into one `/pre-write-check`), `writ-memory-policy-guard.sh`, `writ-worktree-safety.sh`, `writ-debug-code-gate.sh`, `pre-validate-file.sh`, `validate-test-file.sh` (TDD gate). *PostToolUse (advisory):* `validate-file.sh`, `validate-design-doc.sh`, `validate-handoff.sh`, `validate-rules.sh` (sentinel-driven `exit 2`), `validate-exit-plan.sh`, `writ-bible-authoring-push.sh`.
- **Approval / gate:** `auto-approve-gate.sh` (writes the gate token, §7).
- **PostTool / RAG / telemetry:** `writ-posttool-rag.sh` (1500-token cap, 0.4 score threshold, orchestrator-suppressed), `writ-mark-pending-test.sh`, `enforce-violations.sh`, `friction-logger.sh`, `writ-web-capture.sh`.
- **Stop / verify:** `writ-run-pending-tests.sh`, `writ-quality-judge.sh`, `writ-verify-before-claim.sh`, `writ-pressure-audit.sh`.
- **Sub-agents:** `writ-dispatch-discipline.sh`, `writ-subagent-start.sh`, `writ-subagent-stop.sh`.
- **Compaction / lifecycle:** `writ-precompact.sh`, `writ-postcompact.sh`, `writ-session-end.sh`.

**Patterns every hook follows:**
- **`load_hook_env()`** - the single-spawn envelope parser. It distinguishes `HOOK_SESSION_ID_RAW` from `HOOK_SESSION_ID` (the latter prefers `agent_id`), which is load-bearing for sub-agent isolation.
- **`_writ_session()`** - curl-first (0.1s connect timeout to `localhost:8765`), subprocess fallback. Three complex-arg subcommands (`add-pending-violation`, `invalidate-gate`, `update`) always use subprocess.
- **`stop_hook_active`** - any *blocking* Stop hook must check `common.sh`'s `stop_hook_active()` helper, or Claude Code treats the Stop hook's `additionalContext` as a turn-block and loops to its 9-turn cap.

> Editing a hook *script's content* takes effect immediately. Adding a *new event→script mapping* in `hooks.json` requires a fresh Claude Code session to register.

---

## 15. Configuration

**`writ.toml`** (`<package_root>/writ.toml`; readers in `writ/config.py`, all with coded `DEFAULT_*` fallbacks). The sections you'll touch:

- **`[neo4j]`** - `uri = "bolt://localhost:7687"`, `user = "neo4j"`, `password = "writdevpass"`, `database = "neo4j"`. **`writdevpass` is a dev-only default** (`DEFAULT_NEO4J_PASSWORD`, `config.py:22`); any real deployment must override it.
- **`[hnsw]`** - `cache_dir` (default `~/.cache/writ/hnsw`; `get_hnsw_cache_dir()` expands `~` on read - prefer an absolute path in the file, TOML does not expand `~`).
- **`[ranking]`** - the six weights (semantic preset `w_bm25=0.198`, `w_vector=0.594`, `w_severity=0.099`, `w_confidence=0.099`, `w_graph=0.01`; `w_bundle_cohesion` defaults to 0). Plus `[ranking.severity_values]` and `[ranking.confidence_values]` lookup tables.
- **`[gate]`** - `novelty_threshold = 0.85`, `redundancy_threshold = 0.95`.
- **`[frequency]`** - `graduation_threshold = 50`, `graduation_ratio_minimum = 0.75`.
- **`[context_budget]`** - `summary_threshold = 2000`, `standard_threshold = 8000`.

**`writ/shared/budget.json`** - the token budget: `default_budget=8000`, per-rule render costs `full=200 / standard=120 / summary=40`, `subagent_budget=null` (unlimited), `always_on_cap=5000`.

**`bin/lib/gate-categories.json`** - framework detection + the write-gate `exclusions` glob list (tests, migrations, `__init__.py`, and `.claude/` CONFIG only: `settings.json`, `settings.local.json`, `*.md`).

**Env vars:**
- `WRIT_CONTEXT_WINDOW_TOKENS` - validated 1000–10000000 at startup; the watcher hook uses it for the ~75% pressure threshold (default 200000 in the hook).
- `WRIT_FRICTION_LOG` - override the friction log path.
- `WRIT_ALLOW_EMBEDDING_FALLBACK=1` - permit the dev-only PyTorch `SentenceTransformer` fallback (production uses ONNX).
- `WRIT_CACHE_DIR` - session cache location (else `/tmp`).

Python 3.11+ uses stdlib `tomllib`; older needs `tomli`.

---

## 16. Operations: daemon lifecycle

**Primary: systemd user service.** `scripts/install-server-service.sh` installs `writ-server.service` (`ExecStart = writ serve --port 8765 --host localhost`, with an `ExecStartPre` that waits up to 60s for Neo4j on `:7687`). It runs `systemctl --user enable --now writ-server.service`.

- **Restart after code changes:** `systemctl --user restart writ-server` — **not** `stop-server.sh` (that fights systemd's auto-restart).
- **Status / logs:** `systemctl --user status writ-server` or `journalctl --user -u writ-server -f`.
- **Boot start:** requires `sudo loginctl enable-linger $USER` (the installer prompts for this).

**Fallback: `scripts/ensure-server.sh`.** For environments without the user service. It is flock-guarded and singleton-safe (the shared start lib must close the lock fd or the daemon holds the flock for life). The `writ-rag-inject.sh` hook calls this ensure path as a start-only fallback.

**When a restart is required.** Module-level Python edits (anything imported at server import time - schema, predicates, server routes) need a restart to take effect. Editing a hook *script* does not. Adding a *new hooks.json mapping* needs a fresh Claude Code session.

**Health.** `GET /health` returns `status`, `rule_count`, `mandatory_count`, `category_count`, `route_distribution`, `index_state`, `startup_time`, `cache_dir`, `friction_log`. `status: "degraded"` means the index warmed but `rule_count == 0` (the DB and the retrieval index disagree).

---

## 17. The CLI in full

`writ <command>` (Typer, entry point `writ.cli:app`). All 17 subcommands (`writ/cli.py`):

| Command | What it does |
|---|---|
| `status` | `GET /health` summary (`cli.py:1496`). |
| `query "<text>"` | Run retrieval and print ranked rules (`:1226`). |
| `import-markdown <dir>` | Ingest Markdown into the graph. Auto-export guard fires only on a full `bible/` root import, not a subdir (`:374`). `--compress` (default off) regenerates the graph-only `Abstraction` layer after ingest (maintainer-only, needs the `[fallback]` dep; warns + continues if absent). |
| `export` | Export the graph back to `bible/` Markdown (`:1059`). |
| `migrate` | Backward-compat shim for the old methodology loop; **`import-markdown` is now the primary import** (`:1199`). |
| `prune` | Remove nodes. Currently whole-graph (not project-scoped) (`:470`). |
| `validate` | Run `run_all_checks` integrity validators (`:515`). See below. |
| `add` | Interactive rule authoring; creates with the graph-protecting origin so reconcile won't delete it (`:829`). |
| `edit` | Interactive edit; warns about graph-only edges reconcile would delete if not declared in front-matter (`:955`). |
| `propose` | AI-rule proposal through the structural gate (`:1324`). |
| `review <rule_id>` | Triage AI-provisional rules; shows origin context (`:1386`). |
| `role-prompt <role>` | Print the graph-canonical `SubagentRole` prompt template (`:1156`). |
| `feedback` | Record a rule observation (positive/negative) (`:1294`). |
| `compress` | `[fallback]`-only: cluster non-mandatory rules into `Abstraction` nodes (`:1078`). Not a production-path command. |
| `analyze-friction` | Six analytical lenses over the friction log (`:23`). See below. |
| `audit-session` | Per-session friction breakdown, incl. `push_by_action` / `push_channels` (`:148`). |
| `serve` | Run the FastAPI daemon (`:358`). |

**`analyze-friction` — six mutually-exclusive lenses** (`cli.py:34-39`):
`--rule-effectiveness` (per-rule denial stick-rate), `--skill-usage` (skill loads vs playbook completion), `--playbook-compliance` (per-playbook in-order compliance), `--graduation-candidates` (rules ready to graduate), `--trim-candidates` (low-activation rules/skills), `--quality-judge-false-positives` (per-rubric override rates). Log-path resolution order: explicit arg > `WRIT_FRICTION_LOG` > `./workflow-friction.log`.

**`validate` — advisory vs hard-fail.** `run_all_checks` runs ~28 checks. **Hard-fail** (sets exit 1): conflicts, orphans, stale, redundant (when it ran), parity (node/edge/prop), dispatch/invokes/teaches invariants, floor completeness, trigger-keyword parity, push reachability, example lint, domain enum, counter-node parity, enforceable-severity coupling, forbidden-phrase overlap, shared code example, stranded mandatory, ranked-exclusion mismatch, always-on budget breach. **Advisory** (reported, never fails): redundancy *unavailability*, unreviewed count, frequency-stale, graduation flags. Redundancy *raises* (not returns empty) when the `[fallback]` extra is missing, so `validate` prints "Redundancy check skipped" rather than silently reporting clean.

---

## 18. Friction log & dashboard

**The log.** NDJSON appended to `workflow-friction.log` (one JSON object per line). Located via `$WRIT_FRICTION_LOG` or a project-marker walk; fail-soft on `OSError`. The base schema each line carries (`bin/lib/friction-append.py`, `writ/session/friction.py`): `ts` (ISO UTC), `session`, `mode` (one of the five mode names, or `null`), `event` (the event-type name), plus event-specific fields.

`mode: null` is *expected*, not dirty data, for events that fire before a mode is set (e.g. a `pre_write_decision` from the PreToolUse hook on the first write, or a `post_compaction` event - compaction is a Claude Code-level action, not mode-scoped). The dashboard treats the null bucket as its own category.

**The dashboard.** `GET /dashboard` renders a self-refreshing (60s), no-JS HTML view computed by the `analyze_*` functions (ARCH-SSOT-001: the dashboard never recomputes; it always delegates). The six analytical lenses are the same as `analyze-friction` (§17).

> **Caveat: test-synthetic contamination.** In the Writ self-repo, the friction log is ~86% test-synthetic (tests write events). Real project logs are clean but show a coverage gap: ~92% of dispatched sub-agents are general-purpose with 0 rules - i.e. Writ does not yet reach most dispatched work. Read self-repo friction numbers with that in mind.

---

## 18b. Exploring the graph: `/explore` and Neo4j Browser

Two ways to see and interact with the knowledge graph, for two audiences.

**`/explore` (everyone).** With the daemon running, open **`http://localhost:8765/explore`**. It is a single, data-backed page (served by the daemon, reading the live graph) that walks Writ from a specific rule up to the overview, in seven sections: the overview + north star, the three runtime pieces, the 5-stage pipeline with a **live query playground** (type a task -> `POST /query` -> the real ranked result), modes & gates, a **live graph explorer** (Cytoscape over `/graph` + `/node/{id}` - filter by node type / domain / provenance / mandatory, click a node for its statement/examples/neighbors, expand neighbors), the self-authoring/graduation lifecycle, and a requirements browser. Because it reads the live endpoints it never goes stale (it replaced the old hand-maintained HTML flowcharts). The graph view loads Cytoscape from a CDN (needs internet); everything else works offline. It is **read-only** - the backing endpoints (`/graph`, `/node`) expose no write or arbitrary-query surface.

**Neo4j Browser (developers).** The Docker Community Neo4j ships its own UI at **`http://localhost:7474`** (Bolt `bolt://localhost:7687`; default dev creds `neo4j` / `writdevpass` from `writ.toml`). Use it for raw Cypher and free-form graph visualization the `/explore` page intentionally does not expose. Read-only starters:

```cypher
MATCH (n) WHERE n.project = 'writ' RETURN labels(n)[0] AS type, count(*) ORDER BY count(*) DESC;   // node census
MATCH (r:Rule {rule_id: 'SEC-AUTH-TOKEN-001'})-[e]-(m) RETURN r, e, m;                              // a rule + its edges
MATCH (n)-[e:CONFLICTS_WITH]-(m) RETURN n.rule_id, m.rule_id;                                       // conflicts
```

> **Not available on Community:** Neo4j **Bloom** (the no-code graph explorer) is an Enterprise/Aura feature; Neo4j **Aura** is cloud *hosting*, not a viz tool. Writ self-hosts Community, so neither applies - `/explore` (Writ-tailored, shows provenance/mandatory/channels) plus Neo4j Browser (raw Cypher) cover both needs without them.

---

## 19. Testing & benchmarks

```bash
make test    # full pytest suite
make bench   # contractual + scale + traversal + methodology benchmarks
make check   # lint / type / format gate
```

The full suite is **2921 passed + 52 skipped** (~4.5 min) as of 2026-06-16 (a point-in-time count; run the command below for the current figure):
```bash
.venv/bin/python -m pytest    # use the venv interpreter (it has onnxruntime)
```
Run tests with `.venv/bin/python`, not the system Python - the system interpreter lacks `onnxruntime` and fails the embedding tests.

`conftest.py` restores the methodology corpus after tests that wipe the shared graph (a test that runs `pipeline_db` would otherwise leave the shared `writ` graph empty; teardown re-migrates via `migrate.py`).

---

## 20. By the numbers

Per-stage and scale figures come from `SCALE_BENCHMARK_RESULTS.md`. Treat them as the published synthetic curve, and flag pre-B5.2 figures as superseded (the batch-ingest and warmup reworks changed cold-start and per-query cost models; older single-number latency claims in stale docs do not match current benches).

**Per-stage retrieval p95** (synthetic curve, 73 → 10K rules; `SCALE_BENCHMARK_RESULTS.md`):

| Stage | p95 @ 73 | p95 @ 10K |
|---|---:|---:|
| BM25 (Tantivy) | 0.162 ms | 0.262 ms |
| Vector (hnswlib) | 0.046 ms | 0.108 ms |
| Cache (adjacency) | 0.001 ms | 0.001 ms |
| Ranking | 0.103 ms | 0.218 ms |
| **End to end** | **0.278 ms** | **0.557 ms** |

The headline is **context reduction at scale**: retrieving only the relevant slice instead of stuffing the full corpus saves an order of magnitude or more in tokens per turn as the rulebook grows. The per-query latency stays sub-millisecond because every index (BM25, vector, adjacency) is pre-warmed in memory - retrieval does no sync I/O (the lifespan warms Neo4j → pipeline → trigger index → LLM before serving).

> The exact corpus-size and quality numbers in older HANDBOOK/README versions (276 rules, MRR tables, etc.) are stale. For current scale behavior read `SCALE_BENCHMARK_RESULTS.md`; for the live corpus census run the command in §9.

---

## 21. What's solid, what's still moving

**Solid:**
- The 5-stage retrieval pipeline, sub-millisecond p95 at every stage.
- The five-mode / two-gate enforcement and the anti-self-approval token model.
- The structural gate (5 checks) and frequency-driven graduation flip.
- Mandatory-floor single-source predicates + stranded-mandatory validator.
- Sub-agent isolation (`is_subagent`) and orchestrator RAG suppression (`is_orchestrator`).
- ONNX embedding inference with verified ranking parity vs PyTorch; HNSW corpus-hash invalidation.
- Friction analytics + dashboard.

**Still moving (known seams; ARCHITECTURE §20):**

| Seam | Status |
|---|---|
| Stage 1 route-filter branch | Dead path until `build_pipeline` loads category routes. |
| Channel 2 `/methodology-companion` | Built-not-wired; hook still uses `/query` until the floor-authoring cutover. |
| Substantive-edit observation-count fork | Deferred decision. |
| `authority` `ai-provisional → ai-promoted` | Defined, no live path. |
| `DEFAULT_NEO4J_PASSWORD='writdevpass'` | Dev default; override in production. |
| `writ reconcile` referenced in CLI messages | No such CLI command (reconcile is library-only); cosmetic message bug. |

*Closed this round (commit 912a29e):* the `NODE_ID_FIELDS`↔`create_edge` OR-match (now registry-derived), `Abstraction` multi-project identity, and `get_all_rules()` cross-project id resolution.

---

## 22. Glossary

**Always-on bundle.** The mandatory `Rule` set plus all `ForbiddenResponse` nodes (plus any Skill/Playbook flagged always-on), injected every turn via `/always-on` on its own 5000-token cap. Never ranked.

**Category.** A node type that defines retrieval routing as *data* (its `routes` field: `semantic`, `scoped`, `state`, `action`, `always_on`, `pull`, `ride_along`) rather than via a central table. Categories form a tree via `BELONGS_TO`.

**`floor_modes` / `action_triggers` / `trigger_keywords`.** The deterministic Channel-2 matching fields on methodology nodes: `floor_modes` = modes where the node is an always-injected obligation; `action_triggers` = actions that *push* the node (bypassing dedup); `trigger_keywords` = curated keywords for budget-flexible *pull*.

**graduation_pending.** The provenance state a `proposed` node enters after crossing the frequency threshold - a statistical flip awaiting the human promotion gate. It is not approval.

**INVOKES.** An edge meaning the orchestrator applies a methodology *inline* (one level deep). Must never target a `SubagentRole` (that's `DISPATCHES`).

**investigate mode.** The unified evidence-grounded mode (audit / explore / research) with a per-invocation source-type lens (`code` / `web` / `runtime`) and INV-1..9 gates (§4).

**methodology-companion.** The deterministic Channel-2 endpoint (`/methodology-companion`) that matches methodology nodes by floor/push/pull without embeddings. Built-not-wired today (§4).

**project.** The isolation key (`project` property + composite `(id, project)` keys) that lets one Writ graph accumulate many projects without collision (§13).

**provenance.** A node's lifecycle state: `hand-authored`, `proposed`, `graduation_pending`, `graduated`. The graph-first states (`proposed`, `graduation_pending`) are parity-exempt because they have no Markdown home yet (§11, §12).
