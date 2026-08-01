# Retrieval

The two channels that put rules and methodology in front of the agent, and the exact constants that govern them. Source of truth: `writ/retrieval/` (pipeline, embeddings, ranking, keyword, traversal, trigger_index, always_on_filter, prompt_bundle) and `writ/graph/predicates.py`.

## 1. Two channels

- **Channel 1, ranked + floor**: the 5-stage hybrid pipeline (`/query`) for non-mandatory Rules and the retrievable methodology types, plus the always-on floor (`/always-on`) for mandatory and always-on nodes. Embedding-based.
- **Channel 2, deterministic companion** (`/methodology-companion`): floor/push/pull matching of Skill, Playbook, Technique, and AntiPattern nodes by workflow state; no embeddings. Live in production inside every `/prompt-bundle` call.

Nuance a doc must not flatten: the four companion node types are *also* embedded in Channel 1's candidate pool, so "methodology is never ranked" is true only of Channel 2 itself. `ForbiddenResponse` likewise rides both the ranked pool and the always-on bundle.

## 2. The ranked pipeline (`RetrievalPipeline.query`)

**Candidate pool** (built at daemon startup): Rules matching `RANKED_INCLUDE_WHERE` (`mandatory IS NULL OR false`; mandatory rules are structurally absent from ranking) plus the five retrievable methodology labels, each normalized with a `rule_id` alias and auxiliary text folded into `body`.

**Stage 1, candidate filter.** Explicit `node_types` wins; `retrieval_mode="literal"` unlocks the full pool; semantic mode uses the Category route map (a candidate needs a `semantic` route). If the route map is empty or *incomplete*, the whole pipeline falls back to the legacy filter (Rule-only plus a methodology-domain exclude) rather than partially dropping uncategorized nodes: fail-closed wholesale, logged with examples.

**Stage 2, BM25** (Tantivy): fields `trigger` (2x boost), `statement`, `tags`, `body` (0.5x dilution); top 50; query sanitized; a parse error degrades to zero keyword hits, vector search still runs. The BM25 index is in-memory only and rebuilds every daemon start (unlike HNSW).

**Stage 3, ANN vector** (hnswlib, cosine): top 10; encoder is an LRU-cached ONNX model (below).

**Abstention (S4).** If the best *raw* cosine is under `RULE_INJECTION_ABSTENTION_THRESHOLD = 0.30`, the query returns `mode: "abstained"` with no rules. Measured basis: the normalized top-1 score cannot separate off-domain queries (~100% false injection); raw cosine can (negative median 0.23 vs positive 0.52). The gate is **call-site opt-in**: `build_pipeline` defaults it off, the daemon and `writ query` turn it on, authoring and diagnostics stay ungated. Every response carries `abstain_signal` (the top raw cosine), hit or miss, so the threshold can be retuned from telemetry.

**Stage 4, graph enrichment**: an in-memory `AdjacencyCache` of every edge except `BELONGS_TO` (category membership would link all siblings), keyed by the canonical id-coalesce, O(1) at query time.

**Stage 5, two-pass ranking** (`ranking.py`):
- First pass without proximity; the top 3 non-`ai-provisional` results seed proximity.
- Graph proximity is discrete: 1.0 (1-hop from a seed), 0.5 (2-hop), 0.0 (else; seeds score 0, no self-boost).
- Final score = weighted sum with the semantic preset `w_bm25=0.198, w_vector=0.594, w_severity=0.099, w_confidence=0.099, w_graph=0.01, w_bundle_cohesion=0.0` (literal preset: 0.396/0.396/0.099/0.099/0.01). Weights validate to sum 1.0. These are code constants in `ranking.py`, not configuration.
- Rank normalization is plain reciprocal rank `1/(rank+1)`, deliberately not classical RRF; a test polices the naming.
- Severity weights 1.0/0.75/0.5/0.25; confidence weights 1.0/0.8/0.6/0.3, overridden by the empirical pass ratio once a rule has 50+ observations at >= 0.75.
- **Sticky tiebreak**: with `prefer_rule_ids` set (the per-turn hook path), rules within 0.02 of the group best keep last turn's order, for prompt-cache stability. This logic assumes `apply_authority_preference` stays a no-op (its threshold is 0.0 and nothing configures it); wiring that feature without revisiting the coupling silently breaks ordering.

**Context budget** (`apply_context_budget`): under 2,000 tokens renders summaries (statement + trigger, up to 10, substituting Abstraction summaries where one covers the top rule); 2,000-8,000 renders standard (top 5, adds violation/pass example); above renders full (top 10, adds rationale and relationships). No budget means full. Caveat: `budget_tokens` is an integer compared against these thresholds to pick a render mode; the pipeline never measures the rendered length.

**Project scoping**: with a project set, candidates are limited to `{project, "_shared"}`; the cross-project anti-leak guarantee, enforced identically for keyword and vector hits.

## 3. Embeddings and persistence

**Encoder**: `all-MiniLM-L6-v2` via ONNX Runtime (no PyTorch), 384 dims (hardcoded; the only supported model), max 128 tokens, mean-pooled and L2-normalized, wrapped in a 1,024-entry LRU. Model files at `~/.cache/writ/models/onnx/` (produced by `scripts/export_onnx.py`; the bootstrap runs it). Resolution is three-state: ONNX, else `WRIT_ALLOW_EMBEDDING_FALLBACK=1` permits the sentence-transformers `[fallback]` extra with a warning, else a hard, actionable `RuntimeError`; nothing falls back silently. The onnxruntime GPU-discovery warning on CPU machines is unsuppressible and harmless.

**HNSW cache** (`~/.cache/writ/hnsw/`, overridable via `writ.toml [hnsw]`): `writ_hnsw.bin` plus a JSON sidecar carrying the corpus hash and the bin's SHA-256. Writes are atomic and ordered (bin renamed before sidecar) so a reader sees either the old pair or a checksum mismatch, never torn state; any mismatch forces a rebuild instead of serving wrong vectors. The corpus hash is computed from rule *text*, so a cache hit skips the encode pass entirely (~84% of cold start). Hits and misses emit `hnsw_cache` metrics; a failed save is an error event because it makes every future start repay the encode.

## 4. The always-on floor

One predicate pair in `writ/graph/predicates.py` is the single source both for selection and validation, closing the historical class where 29 of 32 mandatory rules reached neither channel:

- `INJECTION_RULE_WHERE = "r.mandatory = true OR r.always_on = true"` (the floor; plus every `ForbiddenResponse`).
- `RANKED_INCLUDE_WHERE = "r.mandatory IS NULL OR r.mandatory = false"` (the ranked pool).

**Scoping so the floor doesn't spam**: non-mandatory `process`-domain rules inject only in `work` and `debug` (mandatory rules are exempt from the strip); an `applicability_scope` (`prompt`, `write`, `bash`, `stop`) plus `trigger_keywords` narrows a rule to its injection point, matched against context (file path and content at write time). No routing data means universal: absence of metadata never silently drops a rule. `stop` is never used as a scope because a Stop-hook context injection acts as a turn block.

**Budget**: `always_on_cap = 5000` tokens on the summary render. The `/always-on` response reports the cap but does not trim; the enforcement is `writ validate`'s budget-breach check.

## 5. The deterministic companion (Channel 2)

`MethodologyTriggerIndex` matches by three fields on the nodes themselves (routing as data, no central table):

- **floor** (`floor_modes`): an obligation in matching modes; never dropped for budget; validated against an expected fixture per mode.
- **push** (`action_triggers`, closed 6-action vocabulary: dispatch, gate-denial, review-feedback, worktree, bible-authoring, finish): fired by observable actions, deliberately bypassing the exclude list because re-surfacing at the right moment is the point.
- **pull** (`trigger_keywords`): keyword-scored flexible tail, trimmed lowest-count-first on budget overflow (default 5,000). `over_budget` is only true if floor+push alone exceed it: a loud signal, never a silent drop.

## 6. Quality gates

Regression floors (`tests/fixtures/regression_floors.py`; floors are the fail line, deliberately below measurement, never targets): MRR@5 >= 0.45 on the 47 ambiguous queries, hit rate >= 0.75 on all 193, domain-hit-rate@5 >= 0.90, nDCG@10 >= 0.65. Measured 2026-08-01: 0.5681 / 0.7824 (domain-hit 0.9323, nDCG@10 0.7071). The floor history records each downward walk as corpus-growth dilution with the measurements that justified it. Negative-query ground truth (20 off-domain and near-domain queries) pins the false-injection diagnostic behind the abstention gate. The methodology corpus has its own signed-off 40-query set (blocker floors 0.78 MRR, 0.90 hit rate).

One more distinction worth keeping straight: `writ/retrieval/session.py` (`SessionTracker`) is a *client-side* accumulator for sequential queries; the server-side session cache under `writ/session/` is an unrelated mechanism that happens to share the word. One non-obvious `SessionTracker` behavior: when an Abstraction is returned, every member rule id joins the exclude set, not just the abstraction's own id.
