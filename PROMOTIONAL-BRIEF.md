# Writ: Promotional Brief

## Elevator pitch

Writ is a Claude Code harness that gives every coding session two helpers: a librarian that picks the rules that fit the current task in well under a millisecond, and a process keeper that blocks risky writes until you have approved a plan and tests. At the live 276-rule production corpus (post Phase 1-5 public-rulebook expansion) the librarian returns ranked results in 0.590 ms p95; at the 10,000-rule synthetic scale it still holds at 0.557 ms while reducing context tokens by 726 times versus loading the whole rulebook. The process keeper makes self approval structurally impossible.

## The problem

Three failure modes that any team hits as they grow a coding rulebook:

1. **Token cost grows with the rulebook, not the work.** A 10,000-rule corpus is over a million tokens of rule text per turn. Cache hit rates collapse, latency climbs, the bill scales linearly with the rulebook. Curated skill files only work if you already know which slice applies to the task, which defeats the point on multi-domain work.
2. **Process discipline cannot live in a prompt.** Telling the model "always write tests first" does not stop it from writing the implementation first. Discipline that lives only in natural language is a suggestion. It has to live at the boundary where Claude actually calls tools.
3. **AI-discovered patterns disappear.** When the model spots a recurring antipattern mid task, there is no path for that observation to become a vetted rule with weight in future retrievals. Knowledge accumulates in transcripts and is lost when the session ends.

Writ addresses all three with one architecture: a five-stage hybrid retrieval pipeline for relevance, a mode plus gate state machine for discipline, and an AI-proposes / frequency-graduates / human-promotes evolution model for accumulation.

## Key differentiators

What Writ does that nothing else in the Claude Code ecosystem does:

1. **Sub-millisecond ranked rule retrieval over a Neo4j-backed knowledge graph.** Five stages composed in 0.557 ms end-to-end p95 at 10K rules: domain filter, BM25 keyword (Tantivy), ANN vector (hnswlib over ONNX `all-MiniLM-L6-v2` embeddings), graph traversal (pre-computed adjacency cache), two-pass ranking with reciprocal-rank fusion plus severity, confidence, and graph-proximity weights.

2. **Graph-aware bundle expansion that no skill-file approach can match.** When BM25 surfaces a rule, the pipeline can also surface its `DEPENDS_ON`, `SUPPLEMENTS`, `CONFLICTS_WITH` neighbors via a pre-computed adjacency dictionary (O(1) lookup, 0.001 ms p95). Static skill files cannot represent these relationships; flat retrieval misses them.

3. **Mandatory-vs-retrieved structural split with an architectural invariant.** No change to the retrieval pipeline can cause an enforcement rule to disappear from agent context. Mandatory rules are excluded from BM25 and the vector store at index build time and loaded out of band by hooks with their own 5,000 token budget. Guaranteed by structure, not by configuration.

4. **Mode plus gate state machine that ties enforcement to declared intent.** Conversation, Debug, and Review modes are read only by definition. Work mode requires plan approval (`phase-a`) and test-skeleton approval (`test-skeletons`) before code writes are unblocked. Gate advancement requires a one-time token written by the actual approval hook, so the agent cannot self approve via raw bash.

5. **AI rule proposal through a 5-check structural gate.** Schema validity, mechanical-enforcement-path requirement (for mandatory rules), specificity (10 vague-language patterns blocked), redundancy and novelty (cosine thresholds), and conflict detection. All five run before the rule is written to the graph as `ai-provisional`.

6. **Frequency-driven graduation.** Rules accumulate `times_seen_positive` and `times_seen_negative` counters from automatic feedback. At 50 or more total observations and at least 75 percent positive, the empirical ratio replaces the static enum weight in ranking. Rules earn their relevance from observed effectiveness.

7. **Sub-agent isolation with two flags.** `is_subagent: true` (set by `writ-subagent-start.sh`) bypasses gates and budget skip checks; workers get a fresh 8,000 token budget. `is_orchestrator: true` (set by `mode set work --orchestrator`) suppresses the broad rule injection on the orchestrator master and emits a compact status line instead. Two flags, separate semantics, composing cleanly.

8. **Sticky rule preference for prompt cache stability.** Within score bands of 0.02, the ranker stabilizes injection order from `last_injected_rule_ids`, so cache reuse across turns is preserved when the corpus is otherwise indistinguishable in score.

9. **Pre-computation philosophy throughout.** Tantivy index, hnswlib index, ONNX model, adjacency cache, abstraction summaries: all built at ingest, persisted with corpus-hash invalidation, served from memory. Nothing is computed at query time that could have been computed earlier.

10. **Zero per-project configuration.** Drop the plugin into Claude Code; the bootstrap script handles Neo4j (Docker), virtualenv, ingestion, server, and hook wiring. Every project the user works in inherits the same rule corpus and the same discipline.

## By the numbers

All numbers verified against the live system on 2026-05-10 or against `SCALE_BENCHMARK_RESULTS.md` (2026-04-13).

### Latency at the live 276-rule corpus (post Phase 1-5 public-rulebook expansion)

- **End-to-end p95: 0.590 ms** (target 10 ms; 17 times headroom)
- Median: 0.338 ms
- Cold start (full pipeline build from Neo4j): 2-3 seconds at 276 rules
- 198 new rules seeded across 12 domains; 19 new mandatory rules each backed by a real cross-language regex analyzer in `bin/run-analysis.sh`.

### Latency at synthetic scale

- 80 rules: 0.278 ms p95
- 500 rules: 0.359 ms p95
- 1,000 rules: 0.399 ms p95
- 10,000 rules: 0.557 ms p95

### Compression at scale

- 80 rules: 4.4 times reduction (13,876 tokens to 3,155)
- 276 rules (live corpus): ~52 times reduction (~83,000 tokens to ~1,600)
- 1,000 rules: 75.8 times reduction (121,473 tokens to 1,602)
- 10,000 rules: **726.1 times reduction** (1,174,142 tokens to 1,617)

### Quality

- MRR at 5 (ambiguous queries, n=19): **0.6904** (floor 0.45; pre-expansion baseline was 0.78 against the smaller 72-rule corpus)
- Hit rate (Phase 6 ground-truth corpus, 165 queries covering pre-expansion + new public-rulebook rule IDs): **0.800** (floor 0.75)
- Domain hit rate top-5 (all 164 queries): **0.945** (floor 0.90, enforcement gate added in v1.1.0)
- Methodology MRR at 5 (n=40, signed off corpus): 0.8583
- Methodology hit rate: 1.0000 (40/40)
- ONNX vs PyTorch ranking stability: identical top-1 and top-5 sets on the fixed inline 12-rule / 8-query test (Item 10 rewrite, v1.1.0)

The v1.1.0 release closed most of the v0->v1.0.0 quality regression that the original critique flagged. v1.0.0 measured MRR@5 at 0.4886; auditing the 6 then-current ambiguous-set misses found 4 were measurement-quality issues (3 mislabeled queries plus 2 rules whose text under-specified how users actually phrase the symptom). 4 small edits (3 label fixes in Item 1a, 1 in Item 1c; rule-text clarifications for SEC-DATA-PII-002 in Item 1b and SOLID-DIP-002 in Item 1d) moved MRR@5 from 0.4886 to 0.6904 with zero changes to the retrieval algorithm or ranking weights. Remaining ~12% gap to the v0 0.78 baseline is genuine corpus-growth dilution (3.8x more rules competing per query). The floors at 0.45 / 0.75 stand for v1.1.0; a set-expansion pass to characterize variance at larger N is the next-if-needed step. Methodology retrieval is unaffected (a separate, signed-off corpus).

### Coverage

- **1,512 tests** (v1.1.0), all passing; 14 skipped
- **13 contractual benchmark targets**, all pass (one added in v1.1.0: domain hit rate top-5)
- 30 hook scripts wired through Claude Code (3 legacy hooks removed 2026-05-10)
- 7 cross-language static-analysis functions in `bin/run-analysis.sh` (injection, auth/authz/validation, crypto/headers, data protection, N+1, stateless processes, plus the original per-language linters)
- 36 HTTP endpoints (11 top-level plus 25 under `/session/{id}/`)
- 17 graph edge types in the schema (10 in active use today)
- 12 node types
- 4 modes; 2 Work-mode gates
- 6 sub-agent role definitions
- 276 rules across 12 domains; 30 mandatory; 19 of the 30 are public-rulebook additions seeded in Phases 1-5

### Resource footprint at synthetic scale

| Corpus      | Cold start (median) | Memory (RSS, peak) |
|-------------|--------------------:|-------------------:|
| 80 rules    | 0.494 s             | 1,570 MB           |
| 1,000 rules | 5.782 s             | 2,674 MB           |
| 10,000 rules| 70.788 s            | 2,943 MB           |

## Use cases

### 1. Multi-language enterprise codebase

A rulebook spanning PHP framework idioms (Magento Plugin/Observer pattern), JS/TS conventions, Python typing rules, SQL safety patterns, and security boundaries. Static skill files force you either to hand each rule to the model every turn (token blow-up) or curate per-language bundles (out of date the moment a feature crosses a boundary). Writ retrieves the right rules per file via path-derived queries: PostToolUse hooks read the file path, infer language and framework, query with the relevant terms, inject rules with score at or above 0.4.

### 2. AI-discovered pattern capture

Mid task, the model spots a `Factory->create()->load` antipattern in legacy code that does not have a corresponding rule. With Writ, it calls `POST /propose` with the candidate rule. The structural gate filters out specificity, redundancy, and conflict failures; the rule lands as `ai-provisional` with `confidence: speculative`; frequency counters start accumulating; the maintainer reviews via `writ review`. Without Writ, the observation lives in a transcript and is lost.

### 3. Sub-agent orchestration with discipline

A planner sub-agent produces `plan.md`, the user approves, a test-writer sub-agent writes test skeletons, the user approves, an implementer sub-agent writes production code. Each sub-agent has its own 8,000 token RAG budget, bypasses orchestrator gates (the orchestrator already cleared the human approval flow), and gets PostToolUse rule injection on every file write. The orchestrator master suppresses its own broad RAG, saving thousands of tokens per session.

### 4. Pressure-tested authoring

Authoring a new methodology node (Skill, Playbook, Technique, AntiPattern, Rule, ForbiddenResponse) is itself a structured workflow: write a baseline pressure scenario, run it, observe the agent violate (RED), draft the node, re-run and observe compliance (GREEN), close loopholes (REFACTOR), link via `PressureScenario` edge, write the migration. Writ gives the human an enforcement loop comparable to RED-GREEN-REFACTOR for code, applied to documentation.

### 5. Friction-driven retrospective

The friction log accumulates JSONL events: gate denials, phase transitions, hook timing, rule-effectiveness signals, sub-agent completions, quality-judge overrides, escalations. `writ analyze-friction` and `/dashboard` surface aggregate metrics: which rules are stuck (denial-stick rate), which skills load but do not complete, which playbooks have common skip points, which rubrics have high override rates. Maintainers tune the rulebook based on data, not anecdote.

## Architecture highlights

The parts that make engineers say "that's clever":

### Pre-computation philosophy

Nothing is calculated at query time that could have been calculated at ingestion time. The graph, the embeddings, the BM25 index, the adjacency cache, and the abstraction summaries are all pre-built. Retrieval serves from memory. End-to-end latency stays sub-millisecond at 10K rules because nothing is doing work in the hot path.

### The adjacency cache

A live Cypher round trip for a one-hop neighbor query at 1,000 nodes is about 6 ms. The adjacency cache builds the entire edge set into a Python dictionary at startup (about 50 ms at 80 rules, about 500 ms at 10,000) and serves O(1) lookups at 0.001 ms, a roughly 6,000 times speedup. Stage 4 of the pipeline becomes effectively free, opening up bundle expansion that would otherwise be too slow to justify.

### ONNX elimination of PyTorch

The PyTorch sentence-transformers dependency is about 600 MB. Exporting `all-MiniLM-L6-v2` to ONNX (via `optimum`, optimization level O2 for fused attention) drops the model surface to about 50 MB, runs 30 to 60 percent faster on CPU, and produces identical top-5 rankings on every query in the 83-query ground truth set. The runtime gets a smaller surface area and faster inference for free.

### Mandatory/retrieved structural split

Mandatory rules are not just weighted high in the pipeline. They are excluded from BM25 and the vector store at build time and loaded by a separate `/always-on` endpoint with its own independent 5,000 token budget cap. No change to ranking weights, embedding model, BM25 tuning, or graph traversal can cause an enforcement rule to disappear from agent context. The architectural invariant is enforced by structure, not by configuration.

### Two-flag sub-agent system

`is_subagent: true` bypasses budget skip checks and gate enforcement: workers get unlimited RAG and do not need to set a mode. `is_orchestrator: true` suppresses the broad rule injection on the master and emits a compact status line. Two flags, separate semantics, composing cleanly.

### Gate token enforcement

Phase advancement requires a `--token` argument matching `/tmp/writ-gate-token-${SESSION_ID}`. The token (32 hex characters from `secrets.token_hex(16)`) is written to disk by `auto-approve-gate.sh` only when the user actually types an approval phrase. The agent calling `advance-phase` directly via raw bash hits an "Invalid or missing gate token" rejection, logged as `agent_self_approval_blocked`. The gate cannot be self served.

### Sticky tiebreak for prompt cache

Within score bands of 0.02, the ranker stabilizes injection order from `last_injected_rule_ids` (the rules from the previous turn). When the corpus is otherwise indistinguishable in score, stable order keeps the prompt cache warm. Prompt-cache reuse across turns becomes a first-class concern, not an afterthought.

### Plain ratio graduation, invoked at query time

Graduation uses a plain ratio threshold: at 50+ observations with at least 75 percent positive, the empirical positive ratio replaces the static confidence-tier weight. The Wilson 95 percent CI at n=50, p=0.75 has acceptable width (about 0.61 to 0.86), which is the justification for picking n=50; the runtime check is the simpler ratio comparison. The check fires inside `compute_score` on every ranked candidate, so a rule with sustained positive feedback overrides its static tier without any maintainer action.

## Integration story

How Writ plugs into Claude Code with zero per-project config:

1. **Install once globally.** `bash scripts/bootstrap.sh` from `~/.claude/skills/writ`. Verifies prerequisites, sets up a virtualenv, installs the `writ` console entry point, ingests `bible/` rules into Neo4j, starts the FastAPI server. Renders `templates/CLAUDE.md` into `~/.claude/` with `envsubst '$HOME'` substitution. Idempotent.

2. **Plugin manifest auto-loads.** `.claude-plugin/plugin.json` declares `defaultEnabled: true`. Claude Code's plugin lifecycle invokes `scripts/ensure-server.sh` on Init (warms the server if absent) and `scripts/stop-server.sh` on Shutdown (graceful SIGTERM, leaves Neo4j alone since it may be shared).

3. **Hooks fire automatically.** `~/.claude/settings.json` wires 30 hooks across UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, SubagentStart, SubagentStop, PreCompact, PostCompact, CwdChanged, and InstructionsLoaded events.

4. **Every project gets the same rules.** No per-project setup. Hooks read the user's session cache from `${WRIT_CACHE_DIR}` (default `tempfile.gettempdir()`), detect domain via `cwd` heuristics (composer.json maps to php, pyproject.toml to python, package.json to javascript, and so on), and inject domain-scoped rules. The model gets the right rules for the project it is in without the user configuring anything.

5. **Mode is set per session.** First user prompt triggers a mode-classification directive if no mode is set. The user (or the model on the user's behalf) runs `mode set work` (or one of the other three modes) to unblock writes.

6. **Failures degrade open.** If the Writ server is down, hooks fall back to subprocess CLI invocations (about 300 to 700 ms versus about 10 ms over HTTP). The model never gets blocked by Writ infrastructure problems; it just temporarily loses the rule injection feature.

## Competitive positioning

What Writ replaces, and why those approaches fail at scale:

### vs. Context stuffing (load every rule every turn)

Token cost is linear in rulebook size. At 10K rules: 1.17 million tokens per turn. The model treats none of them as load bearing because they are indistinguishable. Cache hit rates collapse. Writ replaces this with ranked retrieval that returns 5 to 10 relevant rules plus a separate always-on bundle for safety-critical ones.

### vs. Static skill files (per language, per domain curated bundles)

Skill files are point-in-time snapshots that go stale. They cannot represent rule-rule relationships (`DEPENDS_ON`, `CONFLICTS_WITH`). They force the user to know in advance which bundle applies, but multi-domain tasks (backend Python writing JS frontend, for example) crash through bundle boundaries. Writ replaces this with dynamic, query-driven retrieval over a graph that captures both content and relationships.

### vs. Per-project configurations (rules-as-code in each repo)

Every project has to maintain its own rulebook. Cross-project knowledge does not propagate. New rules require touching every repo. Writ centralizes the rulebook in a single Neo4j instance shared across all projects the user works in; hooks detect project context (language, framework) and surface relevant rules without per-project configuration.

### vs. LLM-as-rule-validator (call Claude on every diff)

Expensive (one LLM call per file write times N file writes per turn times the cost per call). High latency. Inconsistent (LLM verdicts drift). Writ uses a hybrid analysis pipeline: pattern-based matchers run first (regex extraction from rule violations, sub-millisecond), and LLM escalation triggers only when patterns are ambiguous or rules carry high retrieval scores without pattern matches. The calibration mode logs paired pattern-vs-LLM verdicts for the first 100 calls so the rubric can be tuned to maximize agreement.

### vs. Rule-as-prompt (prepend the rules section to every system prompt)

Same as context stuffing, but worse: it pollutes the system prompt where it is hardest to evict. Writ keeps rules out of the system prompt and injects them as a bounded user-prompt prefix per turn, with the rule set varying by query. Prompt cache stays warm; rule set stays relevant.

## Status

**Production ready** (shipped):
- All five retrieval stages with budget headroom at every level.
- Mode plus gate enforcement.
- AI rule proposal through the 5-check structural gate.
- Frequency-driven graduation logic plus origin context.
- 38 hook scripts wired through Claude Code via `hooks/hooks.json` (12 registered hook events).
- Sub-agent isolation (`is_subagent`) and orchestrator suppression (`is_orchestrator`).
- ONNX-optimized embedding inference verified identical to PyTorch.
- HNSW persistence with corpus-hash invalidation.
- 1,512+ tests (v1.2.0 adds 14 new test files covering context-window watcher, hook-spawn consolidation, friction-log mode canonicalization, and the warn-with-zero-violations analyzer fix), 13 contractual benchmark targets (v1.1.0; was 1,442 / 12 at v1.0.0).
- Hook hot-path latency p95 enforced via `tests/test_hook_perf_floors.py` (v1.2.0).
- Friction log analytics with a dashboard (`GET /dashboard`).
- Proactive context-window management (v1.2.0): soft directive at 50%, hard block at 75% on PreToolUse via `writ-context-watcher.sh`.
- Public out-of-the-box rulebook seeded: 198 new universal rules across 12 domains; 19 new mandatory rules each with a cross-language analyzer.

**Roadmap:** v1.2.0 closed the workflow-observability cluster (cosmetic PostToolUse banner, friction-log mode case-drift, hook hot-path latency) and added proactive context-window management. v1.1.0 closed most of the v0->v1.0.0 quality regression (MRR@5 0.4886 -> 0.6904 via measurement-quality fixes; the underlying floors at 0.45 / 0.75 stand). Open: ambiguous-set expansion to ~70 queries to characterize variance at larger N before any floor raise; rewrite the synthetic scale generator to produce realistic rule text length and embedding diversity (the v1.0.0 synthetic curve underpredicts real-corpus per-query cost, per the v1.1.0 Item 2 investigation); multi-query session simulation work; consider a Qdrant-backed vector store for corpora over 100K rules; consider an optional remote-graph mode and distributed sub-agent dispatch.

## TL;DR by audience

| Audience | Pitch |
|---|---|
| **CTO / VP Engineering** | Writ replaces context stuffing with sub-millisecond ranked retrieval at 10K rules, gives you an enforceable workflow discipline (plan first, test first), and an AI-proposes-with-gate evolution model that turns transcript observations into vetted rules. 726 times context reduction at 10K rules. Zero per-project config. |
| **Tech lead** | Single shared rulebook across all your repos, retrieved relevance first with graph-aware bundle expansion. Replace skill files with a Neo4j knowledge graph plus a 5-stage hybrid retrieval pipeline. Hooks enforce mode plus gate state machine; sub-agents inherit isolation; the orchestrator suppresses noise. |
| **Engineer using Claude Code** | Drop the plugin in. Stop maintaining per-project rule files. The model sees the right rules for the file you are in, every turn, in under half a millisecond. When you say "approved" the workflow advances; when you do not, writes are gated. Sub-agents work without re-policing. |
| **Maintainer of the rule corpus** | Rules live in a Neo4j graph with explicit relationships. AI proposes new rules through a 5-check structural gate; you triage with `writ review`. Frequency drives graduation; you decide the authority. Friction log plus analytics plus dashboard let you tune the corpus from data, not vibes. |
| **Adversarial reviewer** | Architectural invariant: no change to retrieval pipeline can cause an enforcement rule to disappear. Mandatory rules are not just weighted high; they are structurally separated from the retrieval indexes. Gate advancement requires a token written by the actual approval hook. Tests pin the conftest contract, the post-suite Neo4j restoration, the gate categories, and the budget constants: every load-bearing piece. |

## Related documents

- `HANDBOOK.md` for the architecture handbook with full pipeline mechanics and graph schema.
- `README.md` for installation, quick start, CLI and API reference.
- `SCALE_BENCHMARK_RESULTS.md` for live measurements plus the synthetic scale curve.
- `CONTRIBUTING.md` for rule authoring workflow and monthly review cadence.
