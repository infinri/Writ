# Writ Architecture

Contributor-facing system design. This is the spine the other docs (`README.md`, `HANDBOOK.md`) reference. Every load-bearing claim is anchored to a file, line, or command you can run.

> **North star.** Writ relocates oversight; it does not remove it. The gate is the product. Writ never automates self-approval: a human token-gates any write of canon. Read the rest of this document with that invariant in mind: every "the agent could otherwise..." seam below is closed by a human-held token, not by trust.

---

## 1. System overview

Writ is a local rule-enforcement service for Claude Code. It answers two questions on every prompt and every file write: *what rules apply here* and *is this write allowed right now*. It does this with four runtime pieces.

| Piece | What it is | Where |
|---|---|---|
| **FastAPI daemon** | The single HTTP service all hooks talk to. Retrieval, session state, gates, self-authoring. Binds `127.0.0.1:8765`, no external surface. | `writ/server.py` |
| **Hooks + session state machine** | Thin bash hooks intercept the Claude Code tool lifecycle; a Python session package tracks mode/phase/budget/gates per session. | `hooks/`, `writ/session/` |
| **Neo4j canonical store** | The graph is the source of truth for all rules and methodology. Runs in Docker (`writ-neo4j`). | `writ/graph/db.py` |
| **The CLI** | Operator control surface: ingest, export, reconcile, validate, author, query, analyze. | `writ/cli.py` |

**Canonical store vs. derived view.** Neo4j is canonical. The `bible/` Markdown tree is a *derived, exported view*. When the two disagree, the graph wins on `reconcile` (`writ/graph/methodology_ingest.py`). Never hand-edit exported Markdown expecting it to be authoritative; the next reconcile will overwrite or delete it. (The exception is dual-location canonical files under `bible/methodology/`; see §4.)

**Request flow (typical prompt).**

1. `UserPromptSubmit` hook (`writ-rag-inject.sh`) ensures the daemon is up (health check → `docker start writ-neo4j` → start server), then POSTs the prompt to `/query` and `/always-on`.
2. The daemon runs the 5-stage retrieval pipeline (§6) against pre-warmed in-memory indexes and returns ranked rules within a token budget.
3. The hook renders them into the agent's context.
4. On a file write, a `PreToolUse` hook POSTs `/pre-write-check`; the daemon consults session state and may deny the write with a `[RULE-ID]`-prefixed reason.

The daemon is normally a **systemd user service** (`scripts/install-server-service.sh`); restart with `systemctl --user restart writ-server`, not by killing the process (that fights the auto-restart). The start-server hooks are a fallback for environments without the service.

---

## 2. Graph schema

There are **13 node labels** (`writ/graph/schema.py`, `NODE_ID_FIELDS`):

`Rule`, `Abstraction`, `Category`, `Skill`, `Playbook`, `Technique`, `AntiPattern`, `ForbiddenResponse`, `Phase`, `Rationalization`, `PressureScenario`, `WorkedExample`, `SubagentRole`.

Each label has a fixed primary-key field declared once in `NODE_ID_FIELDS` (e.g. `Rule → rule_id`, `Skill → skill_id`, `Category → category_id`). `NODE_TYPE_MODELS` maps each label to its Pydantic model.

**ID format and prefixes.** Every node id must match `RULE_ID_PATTERN` (`schema.py:13`). Methodology and structural types enforce a type prefix at the Pydantic boundary (`_validate_node_id` with `expected_prefix`): `SKL-`, `PBK-`, `TEC-`, `ANT-`, `FRB-`, `PHA-`, `RAT-`, `PSC-`, `EXM-`, `ROL-`, `CAT-`. `Rule` and `Abstraction` ids match the regex but enforce no prefix (`Abstraction` uses `_validate_node_id("abstraction_id")` with no `expected_prefix`, `schema.py:291`); `ABS-DOMAIN-NNN` is the authoring convention for abstractions, not a Pydantic constraint.

**Composite identity `(id, project)`.** Two projects may hold the same logical id without collision. `apply_constraints()` creates a composite uniqueness constraint per label. `_node_write_spec` (`db.py:69`) is the single source for a node's MERGE identity and properties, so per-node and batch writes produce byte-identical graph state.

**Idempotent MERGE.** All writes MERGE on `{id_field, project}` then `SET n += $props`. Re-ingesting the same source is a no-op on identity.

**Edge endpoint matching is registry-derived.** The general edge path matches endpoints with an `OR` over every id field, built from `NODE_ID_FIELDS` by `_id_or_match` (`db.py`, mirrors `_GRAPH_ID_COALESCE`) and shared by `create_edge` and the `batch_create_edges` fallback. Adding a node type to `NODE_ID_FIELDS` propagates automatically — no hand-edit, no drift (commit 912a29e closed the former manual-sync hazard; guarded by `tests/test_phaseM2b_data_integrity.py`).

---

## 3. Edges

There are **17 allowed edge types** (`writ/graph/db.py`, `ALLOWED_EDGE_TYPES`):

`DEPENDS_ON`, `PRECEDES`, `CONFLICTS_WITH`, `SUPPLEMENTS`, `SUPERSEDES`, `RELATED_TO`, `ABSTRACTS`, `TEACHES`, `COUNTERS`, `DEMONSTRATES`, `DISPATCHES`, `GATES`, `PRESSURE_TESTS`, `CONTAINS`, `ATTACHED_TO`, `BELONGS_TO`, `INVOKES`.

> The inline comment near the definition says "15 edge types total" — that count is stale build commentary. The authoritative count is the size of the frozenset: 17. (Change C retired `APPLIES_TO` and `JUSTIFIED_BY`.)

**Direction is a design contract** (graph traversal is undirected, but the model and validators are directed). Three invariants are enforced by `writ validate`:

- **`DISPATCHES → SubagentRole only.** A `DISPATCHES` edge that targets anything other than a `SubagentRole` fails `detect_dispatch_invokes_invariant`. `DISPATCHES` is reserved for spawning a sub-agent role.
- **`INVOKES` never → `SubagentRole`.** `INVOKES` means the orchestrator applies a methodology inline (one level deep); it must not target a role. Same validator.
- **`TEACHES` never from a `Rule`.** Instructional nodes (`Skill`/`Playbook`/`Technique`) teach; a `Rule` does not. Enforced by `detect_teaches_source_invariant`.

**`BELONGS_TO` is the category tree.** Any node may `BELONGS_TO` a `Category`; a `Category` with a `parent` `BELONGS_TO` its parent category. These edges are derived at ingest (derive-edges source 4), not hand-authored.

---

## 4. Ingest & export round-trip

**Three Markdown formats, strict precedence** (`writ/graph/ingest.py:234`, `parse_nodes_from_file`):

1. **YAML front-matter** — one node per file, highest precedence. File must begin exactly with `---\n`; the block ends at the next `\n---\n`; post-block text becomes the node's `body`. `node_type` is optional (inferred from whichever primary id field is present), which keeps round-trips lossless because export never writes `node_type` (it is a graph label, not a property).
2. **`<!-- NODE START type=X id=Y -->` / `<!-- NODE END: Y -->`** block markers — multi-node per file.
3. **`<!-- RULE START: id -->` / `<!-- RULE END: id -->`** legacy markers — routed as `Rule`.

A file with front-matter is consumed entirely as a single node; the other formats are not checked.

**`methodology_ingest.py` is the unified ingest library** (v1.5.0), replacing the old dual loop (Rule-only in `cli.py`, methodology in `scripts/migrate.py`). `INGESTER_REGISTRY` auto-populates from `NODE_ID_FIELDS`. The write path is `batch_create_nodes` / `batch_create_edges`: a write failure rolls back the whole batch (not per-node isolation).

**Fields that never round-trip (`GRAPH_ONLY_FIELDS`, `export.py:44`).** `confidence`, `evidence`, `staleness_window`, `last_validated`, `authority`, `times_seen_positive`, `times_seen_negative`, `last_seen`, `source_origin`. These are stripped from exported Markdown and **re-derived** to defaults on re-ingest (`_apply_rederived_defaults`). `mandatory` *is* written (`**Mandatory**: true/false`) and round-trips losslessly. `provenance` is omitted from export only when it holds the default (`hand-authored`); round-tripping the default for hand-authored nodes relies on the graph retaining the value, not on Markdown.

**Dual-location rules.** A small, hand-maintained set of `Rule` ids (`_METHODOLOGY_CANONICAL_RULE_IDS`, `export.py:63`, e.g. `ENF-COMMS-001`, `ENF-PROC-SDD-001`) live canonically as front-matter under `bible/methodology/<ID>.md`. `group_rules_by_file` routes them there and excludes them from the domain `rules.md`, so export never produces a lossy duplicate. Hand-editing these files is safe. This list is hand-maintained; new graduated canon must be added or it will re-export to a domain file.

---

## 5. Reconcile & parity

**The upsert-only no-prune gap.** Ingest only MERGEs (upserts). Renaming or deleting a source node leaves an orphan in Neo4j. `reconcile()` (`methodology_ingest.py`) is the corrective tool: it deletes graph nodes absent from the source **oracle** (`compute_expected_graph`, computed via the same `parse_source` the writer uses, so the target cannot drift from what ingest actually writes), deletes stale edges, and clears stale managed properties. It is **project-scoped** (default `project='writ'`) and never touches another project's data.

**`MANAGED` vs `RUNTIME_EXEMPT` properties** (`schema.py:733`). Reconcile and prop-parity operate only on `MANAGED_PROP_NAMES` (every model field minus the exempt set). `RUNTIME_EXEMPT_PROPS` = `{times_seen_positive, times_seen_negative, last_seen, source_origin, provenance}` ∪ all id fields. These are never cleared or flagged. The safety story: only names in the managed allowlist are ever removed, so a frequency counter or a future runtime field is never touched.

**Provenance-aware exemption (`GRAPH_FIRST_PROVENANCE`, `schema.py:38`).** Nodes whose `provenance` is in `{proposed, graduation_pending}` are *graph-first* — they have no Markdown home yet. They are exempt from reconcile deletion, from prop clearing, and from parity checks. This widens the older `source_origin == 'graph-authored'` bit. The canonical states `{hand-authored, graduated}` must have a source home; the graph-first states are transient.

---

## 6. The 5-stage retrieval pipeline

`writ/retrieval/pipeline.py`. A natural-language query becomes a ranked, budget-capped list. Mandatory rules are excluded *before* Stage 1 (they reach the agent only via `/always-on`, §9).

| Stage | What | Engine |
|---|---|---|
| **1. Filter** | Domain or route pre-filter to a candidate subgraph. | — |
| **2. BM25** | Sparse keyword retrieval over `trigger`, `statement`, `tags`, `body`. | Tantivy (`keyword.py`) |
| **3. ANN** | Dense cosine nearest-neighbor on pre-computed embeddings. | hnswlib (`embeddings.py`) |
| **4. Graph traversal** | Enrich every candidate with graph neighbors. | `AdjacencyCache` (`traversal.py`) |
| **5. Rank** | 5a first-pass (RRF + weighted linear, no proximity) → 5b graph proximity from top-3 → 5c final re-score + context-budget trim. | `ranking.py` |

**Ranking weights — six, summing to 1.0** (`ranking.py:25-45`). Semantic preset (default): `w_bm25=0.198`, `w_vector=0.594`, `w_severity=0.099`, `w_confidence=0.099`, `w_graph=0.01`, `w_bundle_cohesion=0.0`. Literal preset: bm25 and vector both `0.396`, others unchanged. The `Weights` constructor raises if they do not sum to 1.0 (`ranking.py:96-100`). Configurable in `writ.toml [ranking]`.

**Graph proximity** is discrete `0.0 / 0.5 / 1.0` (1-hop / 2-hop / none) measured from the top-`FIRST_PASS_TOP_N` (=3, `pipeline.py:58`) first-pass non-`ai-provisional` seeds. The seeds themselves score `0.0`. `FIRST_PASS_TOP_N` is a constant, not configurable from `writ.toml`.

> **Dead path warning.** Stage 1 has a route-filter branch (`pipeline.py:276-279`) that admits a candidate only if its category routes contain `semantic`. It is **dead in production today**: `build_pipeline()` (`pipeline.py:779`) constructs the `RetrievalPipeline` without passing `node_routes`, so `_node_routes` is `None` and the legacy fallback (`Rule`-only + domain-exclude) always applies. The branch activates only when `build_pipeline` is updated to load category routes.

---

## 7. Two retrieval channels

The two channels are separate code paths with separate indexes, not variants of one pipeline.

- **Channel 1 — semantic / literal (`/query`, `/always-on`).** The 5-stage pipeline above plus the always-on injection bundle. Embeddings-based ranking for `Rule` and the retrievable methodology types.
- **Channel 2 — deterministic (`/methodology-companion`).** `MethodologyTriggerIndex` (`trigger_index.py`) matches methodology nodes by **floor** (mode obligation, always injected), **push** (action-triggered, bypasses dedup), and **pull** (curated `trigger_keywords`, budget-flexible) — no embeddings. The response reuses `/query`'s shape so the shared formatter renders both.

> **Channel 2 is BUILT-NOT-WIRED** (`server.py:225`). The endpoint exists and is operational, but the UserPromptSubmit hook still routes methodology through the legacy `/query` path until the floor-authoring cutover (internal "1.7"). Do not describe Channel 2 as the live path for all methodology nodes yet.

---

## 8. Embeddings & caching

**Model.** `all-MiniLM-L6-v2` at **384 dimensions**, run via **ONNX Runtime** in production (`writ/retrieval/embeddings.py`; export with `scripts/export_onnx.py`). Model files: `~/.cache/writ/models/onnx/model.onnx` and `tokenizer.json`.

**HNSW cache + corpus-hash invalidation.** The index lives at `~/.cache/writ/hnsw/` (`writ_hnsw.bin` + `writ_hnsw.json` sidecar). The sidecar stores a `corpus_hash` computed from rule text. On startup, a matching hash means the O(N) `encode_batch` pass is skipped (the index is loaded from disk instead of re-embedding every rule); a mismatch (`embeddings.py:337`) forces a rebuild. Force a rebuild by changing any rule text or wiping `~/.cache/writ/hnsw/`.

**Dev fallback.** Setting `WRIT_ALLOW_EMBEDDING_FALLBACK=1` permits a `SentenceTransformer` (PyTorch) fallback from the `[fallback]` optional-dependency group. Dev-only; production uses ONNX.

---

## 9. Mandatory / always-on contract

Two Cypher predicates in `writ/graph/predicates.py` are the **single source** for the mandatory/always-on distinction. Both the `/always-on` endpoint and the integrity validator import them, so selection and validation cannot drift (this closes the "29-stranded" bug class — two mechanisms keyed on different fields with nothing failing loud).

- `INJECTION_RULE_WHERE = "r.mandatory = true OR r.always_on = true"` — a rule reaches the agent by injection if it is a mandatory obligation **or** flagged always-on. The two flags are orthogonal-but-overlapping (an `always_on` advisory rule like `ENF-COMMS-001` is not mandatory).
- `RANKED_INCLUDE_WHERE = "r.mandatory IS NULL OR r.mandatory = false"` — the ranked pool excludes mandatory rules. Its complement over all rules must equal `{mandatory}`; `detect_ranked_exclusion_mismatch` asserts this.

**Always-on budget cap = 5000 tokens**, measured on the **summary render** (trigger + statement only), not full prose. `detect_always_on_budget_breach` fails validate if the bundle exceeds it. Corpus growth breaches near ~54 always-on rules.

**Stranded-mandatory fix.** A mandatory rule reachable by *neither* the ranked pool (excluded) *nor* injection is a silent no-op gate. `detect_stranded_mandatory` fails validate on any such rule.

---

## 10. Session state machine

`writ/session/*` is a Python package; `bin/lib/writ-session.py` is a **facade** that re-exports every public symbol and holds `main()`. The submodules are the real implementations (POL-6 god-module split). Hooks call the facade as a CLI; session route handlers in `server.py` import it as a module.

**Modes** (§11) initialize different state. **Budget model** (`writ/shared/budget.json` via `writ/session/config.py`): `default_budget=8000`, `always_on_cap=5000`, per-rule render costs `full=200 / standard=120 / summary=40`, `subagent_budget=null` (sub-agents get unlimited budget). `cmd_should_skip` defaults to skipping injection at ~75% context pressure.

**Citation ledger.** Bounded to 10 entries, excerpts truncated to 500 chars, each stamped with a SHA-256 `excerpt_hash`; legacy `command_log` migrates forward.

**Friction log.** NDJSON appended to `workflow-friction.log`, located via `$WRIT_FRICTION_LOG` or a project-marker walk; fail-soft on `OSError`.

**Escalation.** Three gate invalidations trigger an escalation state with a diagnosis (`same-rule` / `different-rules` / `mixed`); `cmd_check_escalation` is read-only.

**Cache.** `$WRIT_CACHE_DIR` or `/tmp`, atomic write-via-rename, forward-compat defaults on read.

---

## 11. Mode / gate state machine

**Five modes** (`writ/session/mode_engine.py`, `VALID_MODES` from `MODE_CONFIG`): `conversation`, `debug`, `investigate`, `review`, `work`. `MODE_CONFIG` is the single source for per-mode gate behavior; a new mode is one entry, not new branches.

**Work mode has two gates** (`gate_sequence: ["phase-a", "test-skeletons"]`, `phase_after_gate: {phase-a: testing, test-skeletons: implementation}`). The phase lifecycle is `planning → testing → implementation → complete`:

1. `mode set work` → `planning`. Writes to source are blocked.
2. **Gate phase-a** (plan validation) → `testing`.
3. **Gate test-skeletons** → `implementation`, where source writes are allowed.

**Anti-self-approval (the keystone).** A phase advance (`/advance-phase`) *requires* a gate token at `/tmp/writ-gate-token-<session_id>` (chmod 600). `auto-approve-gate.sh` writes the token **only** when the user's prompt matches an approval pattern — input the agent cannot forge. The token is **consumed** on a successful advance, so one user approval authorizes exactly one advance. A missing or wrong token logs an `agent_self_approval_blocked` friction event and refuses (`server.py:656-705`). This is the seam that makes the human the approver.

**Debug mode** has a root-cause gate: source edits are blocked until `debug.md` has a populated `## Root cause` section. The INV-9 Evidence+Narrowing gate additionally blocks code read/search in the runtime lens.

**Compaction boundary.** `clear-rules-for-compaction` (PreCompact) drops full rule objects; `reset-after-compaction` (PostCompact) clears the phase exclusion list and resets the budget.

**Gate exemptions.** Sub-agents, the skill dir, settings files, `plan.md` before a mode is set, `capabilities.md`, and `gate-categories.json` exclusions are exempt from write blocking. Deny reasons carry a `[RULE-ID]` prefix.

---

## 12. Hook layer

**Single registration.** `hooks/hooks.json` (not `.claude/hooks/hooks.json`) binds every Claude Code event to a script via `${CLAUDE_PLUGIN_ROOT}`. It registers hooks across twelve events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `PostCompact`, `Stop`, `SubagentStart`, `SubagentStop`, `CwdChanged`, `SessionEnd`. (A standalone `~/.claude/settings.json` path is legacy and being sunset; the two must stay in sync until then.)

**All hooks are advisory (exit 0)** except a small set of blocking `PreToolUse` gate denials (`writ-pre-write-dispatch.sh`, `writ-memory-policy-guard.sh`, `writ-verify-before-claim.sh`, `writ-worktree-safety.sh`) and the `Stop` test-failure hook (`writ-run-pending-tests.sh` in implementation/complete phase).

**`load_hook_env()`** is the single-spawn envelope parser every hook uses. It distinguishes `HOOK_SESSION_ID_RAW` from `HOOK_SESSION_ID` (which prefers `agent_id`) — load-bearing for sub-agent test marking (`writ-mark-pending-test.sh` deliberately uses the RAW id for sub-agent isolation).

**`_writ_session()`** is the curl-first / subprocess-fallback helper: a 0.1s-connect curl to `localhost:8765` first, subprocess fallback. Three subcommands with complex args (`add-pending-violation`, `invalidate-gate`, `update`) always use subprocess.

**Stop-hook loop guard.** Any blocking `Stop` hook must check `stop_hook_active`; omitting it makes Claude Code loop to its 9-turn block cap (a Stop hook's `additionalContext` is treated as a turn block). `common.sh` provides the `stop_hook_active()` helper.

**Consolidated `/pre-write-check`.** `writ-pre-write-dispatch.sh` folds three previously separate `PreToolUse` hooks (gate-approval, final-gate, pretool-rag) into one HTTP call.

The primary RAG path is `writ-rag-inject.sh` on `UserPromptSubmit`; it is also the only hook that manages daemon auto-start.

---

## 13. The FastAPI daemon

**45 endpoints** (the `writ/server/` package; `writ/server.py` was split in W2 into `__init__.py` + `models.py` + `routes/*.py`; count with `grep -rcE '^@(app|router)\.(get|post|put|delete|patch)' writ/server/`). By group:

- **Retrieval:** `/query`, `/methodology-companion`, `/always-on`, `/prompt-bundle`, `/conflicts`, `/rule/{rule_id}`, `/subagent-role/{name}`.
- **Self-authoring & feedback:** `/propose`, `/feedback`, `/analyze`.
- **Session core:** `GET /session/{sid}`, `/session/{sid}/update`, `/session/{sid}/should-skip`, `/session/{sid}/current-phase`, `/session/format`, `/session/{sid}/context-percent`.
- **Mode & gates:** `GET|POST /session/{sid}/mode`, `/session/{sid}/can-write`, `/session/{sid}/advance-phase`, `/session/{sid}/promote-candidate`.
- **Violations & escalation:** `/session/{sid}/add-pending-violation`, `/session/{sid}/clear-pending-violations`, `GET /session/{sid}/pending-violations`, `/session/{sid}/invalidate-gate`, `GET /session/{sid}/check-escalation`.
- **Compaction:** `/session/{sid}/clear-rules-for-compaction`, `/session/{sid}/reset-after-compaction`.
- **Workflow telemetry:** `GET|POST /session/{sid}/active-playbook`, `GET|POST /session/{sid}/verification-evidence`, `GET|POST /session/{sid}/quality-judgment`, `GET /session/{sid}/coverage`, `/session/{sid}/auto-feedback`, `/pre-write-check`.
- **Ops:** `/health`, `/dashboard`.

**Lifespan order** (`server.py:170`): Neo4j connection → `build_pipeline` → `MethodologyTriggerIndex.build_from_db` → LLM client + instrumentation. Indexes are pre-warmed so request handlers do no sync I/O (PERF-IO-001).

**`/health` fields** (`server.py:422`): `status`, `rule_count`, `mandatory_count`, `category_count`, `route_distribution`, `index_state`, `startup_time`, `cache_dir`, `friction_log`. **`degraded`** = index warm but `rule_count == 0` (DB/index split: the daemon's graph DB and retrieval index disagree); the payload adds a `warning`.

**Two token-gated routes** (the only writes of canon): `/advance-phase` (§11) and `/promote-candidate` (§14). Both require the agent-unforgeable gate token and consume it on success.

---

## 14. Self-authoring / graduation loop

This is where Writ could otherwise write its own memory unsupervised. It cannot.

**Structural gate — 5 checks, in order** (`writ/gate.py:55`, `structural_gate`):

1. **Schema** (Pydantic `Rule`).
2. **Mechanical-enforcement-path policy** — a `mandatory=True` rule with no `mechanical_enforcement_path` is rejected; the alternative is `mandatory=False` (advisory).
3. **Specificity** — vague-language disqualifiers in trigger/statement.
4. **Redundancy / novelty** — vector similarity: reject if cosine > `0.95` (redundant) or > `0.85` (novelty, `NOVELTY_THRESHOLD`). `0.95` is the single-source `REDUNDANCY_SIMILARITY_THRESHOLD` from `schema.py`.
5. **Conflict** — a `CONFLICTS_WITH` edge to an existing rule.

**Authority / confidence ceiling.** `propose_rule()` (`gate.py:224`) always stamps `authority="ai-provisional"` and `confidence="speculative"`. No caller can raise these through this path.

**4-state provenance lifecycle** (`schema.py:32`, `VALID_PROVENANCE`):

`hand-authored` (the bible corpus at rest) — or the graph-first arc → `proposed` (set by `/propose` + `cli add`) → `graduation_pending` (set by the frequency crossing) → **human gate** → `graduated` (exported to `bible/methodology/<id>.md`).

**Statistical flip is not approval.** Frequency thresholds (`writ/frequency.py`): `DEFAULT_GRADUATION_THRESHOLD=50` total observations, `DEFAULT_GRADUATION_RATIO_MIN=0.75` positive ratio. Below threshold: no decision. Above with ratio < 0.75: flagged for human review. Above with ratio ≥ 0.75: the flip from `proposed` → `graduation_pending` only — it never changes authority and never writes to `bible/`.

**The informed human gate.** `build_promotion_review_artifact()` (`writ/promotion.py:36`) aggregates three conflict signals so the human is not rubber-stamping: `CONFLICTS_WITH` targets, `same-category` members, and high-similarity neighbors (cosine ≥ `0.7`, `HIGH_SIMILARITY_DEFAULT`). `promote_candidate()` (`promotion.py:153`) is the edit-at-gate path: the human approves as-is (`graduated_via="human-approve-asis"`) or supplies `edited_fields` (`graduated_via="human-edit"`); the edited text **re-runs the structural gate** before export. The provenance lineage (`graduated`) is preserved through a human edit. The token gate at `/promote-candidate` is the same agent-unforgeable token as `/advance-phase`.

**`OriginContextStore`** (`writ/origin_context.py`) write-once-records the proposal context (task description, triggering query, consulted rules) for the human's later review; default DB `~/.cache/writ/origin_context.db`.

> **Deferred open question.** A *substantive* edit at the gate does **not** currently reset or fork the observation counts (`times_seen_*`); `graduated_via` annotates authorship but threshold-earned counts travel unchanged (`promotion.py:177-180`). Tracked, silently defaulted (§20).

---

## 15. Multi-project architecture

Writ is one shared graph accumulating all projects. Isolation is by a `project` property + composite `(id, project)` keys. The `:Project` registry (`create_project` / `get_projects` / `resolve_project_for_cwd`) maps a project name to its `repo_root` and `bible_root`; registry nodes carry no `project` property and survive project-scoped reconciles. Retrieval scopes to `{project, '_shared'}` (the cross-project anti-leak guarantee). `create_edge` matches endpoints *within* `$project` (`db.py:198`), so an edge never resolves to another project's same-id node.

> **Data-integrity seams — CLOSED (commit 912a29e).** Three latent multi-project
> seams were fixed before any second project:
> - **`Abstraction` composite key.** `create_abstraction` MERGEs on `{abstraction_id, project}` (its constraint), `create_abstracts_edge` matches within `$project` and stamps `e.project`, `delete_abstractions(project=)` is project-scoped, and `write_abstractions_to_graph` propagates `project` end-to-end.
> - **Project-scoped edge resolution.** `get_all_rules(project=)` scopes `ingest_edges`' `known_ids` to the ingest project, so a cross-project id reference is correctly *dangling*, not silently resolved to another project's node.
> - **`create_edge` OR-match** is derived from `NODE_ID_FIELDS` via `_id_or_match` (below), not a hardcoded list.
>
> Per the multi-project decision, project-scoped reconcile/parity must be solid *before* a second project is ingested; these closures satisfy that for the node/edge/abstraction write + resolution paths.

---

## 16. Validators

`writ validate` runs `run_all_checks` (`writ/graph/integrity.py:1249`) — roughly **28 distinct checks**. The split that matters:

**Hard-fail (sets `exit_code=1`):** conflicts, orphans (Rule + all-labels), stale, redundant (only when it ran), dangling dispatched roles, parity violations (node / edge / prop), `dispatch_invokes`, `teaches_source`, floor completeness, trigger-keyword invariant, push reachability, action vocabulary, example lint, domain enum, counter-nodes parity, dispatched-by parity, enforceable-severity coupling, forbidden-phrase overlap, shared code example, category reachability (when not skipped), stranded mandatory, ranked-exclusion mismatch, always-on budget breach.

**Advisory (reported, never fails the run):** redundancy *unavailability*, unreviewed count, frequency-stale, graduation flags. (`detect_confidence_defaults` exists at `integrity.py:356` but is not wired into `run_all_checks` — it is a callable check, not part of the standard `writ validate` run.)

> **Redundancy raises, it does not return empty.** `detect_redundant()` requires the sentence-transformers `[fallback]` extra and raises `RuntimeError` when unavailable, so a caller can distinguish "no redundancies" from "the check did not run." `run_all_checks` catches it, records `redundancy_unavailable`, and `writ validate` prints "Redundancy check skipped" — it does not silently report clean.

Two legacy shims to know: `detect_orphans()` is Rule-only and superseded by `detect_orphans_all_labels()` (both run; per-type counts come from the latter). `detect_stale()` returns the node id under the key `rule_id` for *every* node type (backward-compat shim).

---

## 17. Analysis & compression

**`/analyze`** (`writ/analysis/`) is a pattern-based violation scanner with optional LLM escalation. Three modes (`analysis_method`): `pattern` (only), `hybrid` (pattern + LLM), `calibration` (always both, LLM authoritative). Calibration counts paired comparisons to `log_root()/calibration.jsonl` (under the skill's `var/logs/`, overridable via `WRIT_LOG_ROOT`); after `CALIBRATION_THRESHOLD=100` samples the mode flips to `production` (`analysis/instrumentation.py`). LLM model selection: Haiku for all phases except `planning` (Sonnet); `temperature=0`, 10s timeout; LLM failures yield `uncertain` findings, never exceptions. `/analyze` is not on the main hook enforcement path (see the `bin/run-analysis.sh` security note in `server.py`).

**`writ compress`** (`writ/compression/clusters.py`, CLI `cli.py:1078`) clusters non-mandatory rules by embedding similarity, comparing HDBSCAN vs k-means by silhouette and choosing the winner, then writes `Abstraction` nodes (`ABS-DOMAIN-NNN`). An abstraction's `summary` is the centroid-nearest rule's statement. `write_abstractions_to_graph(…, project="writ")` deletes that project's prior abstractions first (idempotent recompression, project-scoped) and stamps `project` on the nodes + `ABSTRACTS` edges. Requires the `[fallback]` extra; not a production-path command.

---

## 18. Plugin packaging

Three layers, distinct roles:

- **`.claude-plugin/plugin.json`** — the plugin manifest (name, version, and pointers to `commands` and `agents`). It deliberately declares no `hooks` path: `hooks/hooks.json` is auto-discovered at the plugin root, and declaring it again collides and fails every hook.
- **`.claude-plugin/marketplace.json`** — the same-repo marketplace that makes `claude plugin marketplace add infinri/Writ` resolve. Its entry version must match `plugin.json`.
- **`hooks/hooks.json`** — the event-to-script registration (§12), located via `${CLAUDE_PLUGIN_ROOT}`.
- **`.claude/`** — commands, agents (the five sub-agent role files), and the hook scripts themselves.

`CLAUDE_PLUGIN_ROOT` is the install-root variable every hook command interpolates so scripts resolve regardless of install location. Templates under the skill provide the standalone `CLAUDE.md` and config patches.

---

## 19. Sub-agent roles

Five role contracts, each a node *and* a Claude Code agent file:

| Role | Node id | Agent file |
|---|---|---|
| Explorer | `ROL-EXPLORER-001` | `.claude/agents/writ-explorer.md` |
| Planner | `ROL-PLANNER-001` | `.claude/agents/writ-planner.md` |
| Test writer | `ROL-TEST-WRITER-001` | `.claude/agents/writ-test-writer.md` |
| Implementer | `ROL-IMPLEMENTER-001` | `.claude/agents/writ-implementer.md` |
| Reviewer | `ROL-REVIEWER-001` | `.claude/agents/writ-reviewer.md` |

`SubagentRole` nodes (`schema.py:586`) carry `prompt_template`, `dispatched_by`, `model_preference`, `tools`, `description`. They are reachable only via `DISPATCHES` edges (§3) and surfaced by `GET /subagent-role/{name}`. The implementer's contract includes **post-write disk-verification**: after writing, it re-reads every planned file and reports a hard `VERIFICATION FAILED` error if any are missing. The reviewer runs a two-pass JSON review.

---

## 20. Known seams & invariants

| # | Seam / invariant | Status | Anchor |
|---|---|---|---|
| 1 | `NODE_ID_FIELDS` ↔ `create_edge` OR-match | **CLOSED (912a29e).** Derived from `NODE_ID_FIELDS` via `_id_or_match`; guarded by `test_phaseM2b_data_integrity`. | `db.py` `_id_or_match` |
| 2 | `ENFORCEMENT_CONVENTIONS` | Convention only, **not validated in code** — exists for discoverability. | `schema.py:64-71` |
| 3 | Stage 1 route-filter branch | **Dead path** until `build_pipeline` loads category routes. | `pipeline.py:276`, `:779` |
| 4 | Channel 2 `/methodology-companion` | **Built-not-wired**; hook uses `/query` until floor-authoring cutover. | `server.py:225` |
| 5 | Substantive-edit observation-count fork | **Deferred decision.** Edit at gate does not reset/fork `times_seen_*`. | `promotion.py:177-180` |
| 6 | `Abstraction` multi-project identity | **CLOSED (912a29e).** Composite `(id, project)` MERGE; `create_abstracts_edge`/`delete_abstractions`/`write_abstractions_to_graph` project-scoped. | `db.py` `create_abstraction` |
| 7 | `get_all_rules()` cross-project ids | **CLOSED (912a29e).** `get_all_rules(project=)` scopes `ingest_edges` `known_ids`; cross-project ref goes dangling. | `methodology_ingest.py` |
| 8 | `authority` `ai-provisional → ai-promoted` | **Defined, no live path.** `propose_rule` forces `ai-provisional`; promote does not change authority. | `schema.py:24` |
| 9 | Mandatory/always-on single source | **Enforced.** Both selection and validator import the same predicates. | `predicates.py` |
| 10 | Always-on 5000-token cap | **Enforced** on summary render; breaches near ~54 rules. | `predicates.py`, validator |
| 11 | `DEFAULT_NEO4J_PASSWORD='writdevpass'` | **Dev default.** Production must override in `writ.toml [neo4j]`. | `config.py` |

---

## 21. Phase history

Writ was built in phases. The internal `WRIT-BLUEPRINT.md` tracker was retired once the planned sequence shipped; the `PHASE-*` documents and `SCALE_BENCHMARK` that remain are **historical artifacts** — read them for context, not current state.

Rough arc: Phase 0 introduced data-driven `Category` routing and parity. Phase 1 added the methodology node types, routing-as-data, and the trigger index. Phase 3 added authoring governance, the structural gate, and many validators. Phase 5 added friction analytics. Phase 6 added the self-authoring / graduation loop and (6.1) the 4-state `provenance` enum that replaced the older binary `source_origin` model. The multi-project foundation (`project` property, composite keys, scoped reconcile) is in place; abstraction methods and cross-project edge resolution are the remaining ports (§15, §20).

When in doubt about current behavior, trust the code and the live graph over any phase doc:

```bash
# Live graph census (project 'writ')
docker exec writ-neo4j cypher-shell -u neo4j -p writdevpass \
  "MATCH (n) WHERE n.project='writ' RETURN count(n);"   # 399 nodes (snapshot 2026-06-16)
# Endpoint count
grep -rcE '^@(app|router)\.(get|post|put|delete|patch)' writ/server/  # was: writ/server.py  # 45
# Full suite
.venv/bin/python -m pytest    # ~2921 passed, ~52 skipped, ~4.5 min (2026-06-16; run for current)
```
