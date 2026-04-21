# Writ Absorbs Superpowers: Implementation Plan (v3, reviewer-signed-off)

_Dated 2026-04-21. Supersedes v1 and v2. Reflects nine accumulated items established through reviewer iteration._

## How to read this document

- **Section 0:** preconditions and source pin — operational preamble that must hold before any phase starts.
- **Sections 1-4:** why we are doing this — strategic context, design philosophy, architecture overview, review-shaped decisions.
- **Sections 5-11:** what to build, in what order, with release-blocker gates per phase.
- **Sections 12-18:** appendices — content map, token-budget math, embedding evaluation protocol, artifact-quality gate specifications, scope-boundary appendix, ENF-* audit protocol, open decisions.

## Quick orientation

**Problem:** Agentic coding systems hit a knowledge-scaling wall. Flat-directory rule/skill loading fragments past ~30 items. No existing system in this category has solved methodology knowledge at scale.

**Claim:** Writ's graph-native hybrid RAG — proven at 10K coding rules, sub-millisecond retrieval, 726× token reduction — is the one infrastructure that can absorb process methodology and retrieve it at the same scale. Node-type heterogeneity is handled through empirical gates, not hand-waving.

**Plan:** Absorb Superpowers' methodology into Writ's graph as new node types; enforce discipline via hooks + artifact-quality gates; measure retrieval quality on heterogeneous content as a release-blocker before content lands; evolve the corpus via frequency graduation + pressure testing.

**Timeline:** 15 focused weeks, 6 phases. Every phase gated on concrete measurements; failures halt the phase instead of sailing past.

**Minimum viable fallback:** if methodology retrieval fails Gate 2 after mitigations, Scope A (enforcement-only with new process rules) ships at week 7-8; methodology content (Scope B) waits until retrieval quality is proven or is permanently deferred. The worst case is documented, not theoretical.

---

## Section 0 — Preconditions and source pin

Operational preamble. Every item here must hold before Phase 0 drafting starts, and several items are one-time decisions that persist through all six phases. This section is the closed-loop half of the plan — the why/what lives in Sections 1-18, the "is the floor solid" lives here.

### 0.1 Source pin

**All content extraction references Superpowers at version `5.0.7`, commit `b557648` (2026-04-16, message: "formatting"). Content changes in later releases are not absorbed unless the pin is explicitly bumped and the affected nodes re-evaluated.**

This pin is load-bearing: if Superpowers ships a 5.1 or 6.0 during absorption, Writ's content is still sourced from `b557648`. Bumping the pin is a discrete decision (new commit, re-run of Phase 0 sanity checks on changed nodes), not an implicit drift.

`~/workspaces/superpowers/` is the working location of the pinned tree during development. Treat it as **read-only reference** — never write back, never symlink, never import code from it. After Phase 5, deleting the directory must leave Writ fully functional. That self-containment is the litmus test for absorption completion.

### 0.2 Attribution schema

`source_attribution: str | None = None` on every absorbed node. Format: `"superpowers@5.0.7"`.

Reserved sibling field: `source_commit: str | None = None`. Not populated in Phase 1 schema; added to the model so it can be used later without migration if finer-grained provenance becomes necessary.

Attribution is metadata. Writ never dereferences it at runtime, test time, or CI time. It exists for credit, license tracking, and future re-absorption decisions.

### 0.3 Preconditions

Before Phase 0 drafting begins, and verified at every phase transition:

- **Git hygiene:** work happens on a non-main branch (`phase-0-validation` through Phase 0). Branch-per-phase for subsequent phases. No direct commits to main.
- **Test baseline captured:** `pytest` green, count recorded in `docs/phase-N-report.md`. Failures triaged to root cause before the phase begins, never pushed through.
- **`bible/` backup present:** `bible.bak.<YYYYMMDD>` created before any ingest/migration work. Backup is not deleted during implementation.
- **Feature flag off:** `enforcement.superpowers_absorb.enabled = false` in `writ.toml`. Stays false throughout all six phases. Flipping it on is a post-Phase-5 human decision, not an implementation step.
- **Neo4j reachable:** `bolt://localhost:7687` accepts connections; HTTP `:7474` returns 200.
- **`writ serve` healthy:** `/health` returns 200; `writ status` shows `index_state: warm` and expected `rule_count`.
- **Superpowers tree at pinned commit:** `cd ~/workspaces/superpowers && git rev-parse HEAD` equals `b557648`. If HEAD has drifted, reset to pin before extracting.

### 0.4 Resolved Section 18 decisions

Items 1-4 resolved before Phase 2 start; items 5-6 defer to Phase 5:

1. **Mandatory rules in non-work modes: NO.** Hooks enforcing ENF-PROC-* rules check `session.mode == "work"` and skip otherwise. Rules remain retrievable for citation in review/debug/conversation modes; they do not mechanically gate writes outside Work.
2. **Prototype mode trigger: MANUAL ONLY.** `session.mode == "prototype"` entered by explicit user declaration. No keyword auto-detection from prompt text.
3. **Always-on cap raised to 5,000 tokens (pre-emptive ceiling), replacing the universal 800 + work 1,500 + debug 600 decomposition.** Actual usage stays near current ~1,190 tokens. The 5k headroom prevents retroactive-audit-discovered mandatory rules from forcing scope demotion.
4. **Quality-judge override threshold: 3 per session.** Fourth `--override-quality-judge` invocation writes a marker to `workflow-friction.log` for monthly-review attention. Does not block the override.

Items 5 (N=3 voting criteria) and 6 (per-project config override) are Phase 5 decisions.

### 0.5 Failure-mode policy

Release-blockers halt phases. When a blocker fails, the maintainer writes `docs/phase-{N}-report.md` and escalates. Do not tune parameters in a loop until the number passes. Do not lower thresholds. Do not bypass gates. Gate 2 failure specifically invokes the Scope A fallback documented in Section 5.4 — the worst outcome is documented, not "kept iterating past the escalation point."

### 0.6 Scope guardrails

The plan is the scope. Do not add helpful-seeming extras, unrelated refactors, "future flexibility" abstractions, or dependency upgrades beyond what a phase requires. Ambiguity is resolved by flagging and asking, not by inference. Phase work lands as PR-per-phase, never as one monolithic PR.

---

## Section 1 — Why this exists: the unsolved problem

### 1.1 The scaling wall

Every agentic coding system today stores knowledge as a flat directory the harness ships into every session. This works up to ~20-30 files. Past that:

- Context windows saturate. At 1,000 rules, flat-loading is 121,473 tokens/turn; at 10,000, 1,174,142.
- Agent-side selection degrades. Superpowers documents (`writing-skills/SKILL.md:150-157`) that workflow-summary descriptions cause Claude to follow descriptions instead of reading skill bodies.
- Cross-references rot silently on rename.
- No learning happens. Flat files can't track which content is load-bearing, which is never triggered, which is rationalized around.

No system has solved this. Anthropic's guidance recommends palliatives (conciseness, progressive disclosure); selection is still agent judgment applied to a flat list.

### 1.2 Writ's unique position

Writ solved this for coding rules. Benchmarks (`SCALE_BENCHMARK_RESULTS.md`):

| Corpus | Flat tokens | Retrieved tokens | Reduction | p95 |
|---|---:|---:|---:|---:|
| 80 | 13,876 | 3,155 | 4.4× | 0.28ms |
| 1,000 | 121,473 | 1,602 | 76× | 0.40ms |
| 10,000 | 1,174,142 | 1,617 | 726× | 0.56ms |

MRR@5 = 0.7842 on ambiguous queries; hit rate = 97.59%. Five-stage hybrid: domain filter → Tantivy BM25 → ONNX vector (hnswlib, all-MiniLM-L6-v2) → graph traversal (adjacency cache) → two-pass RRF + authority rerank.

**Strategic insight:** the pipeline is node-type agnostic. Extending the graph to methodology content makes the same scaling properties apply — IF heterogeneous retrieval quality is proven empirically, not assumed.

### 1.3 Why absorb Superpowers specifically

Superpowers has the most carefully tuned open-source methodology curriculum: adversarially pressure-tested skills, persuasion-engineered content (Cialdini 2021 + Meincke et al. 2025, N=28k conversations, 33%→72% compliance uplift), 94% PR rejection rate, empirical findings that diverge from Anthropic's own published guidance where testing warranted. Content Writ currently lacks and cannot credibly author from scratch.

### 1.4 Why absorb rather than reference

Keeping systems side-by-side means duplicate flat-loading, no cross-references, no learning across the boundary. The unified graph removes all three.

### 1.5 "Well-rounded Writ" means three capabilities

- **Enforce.** Mechanical gates block critical violations.
- **Teach.** Retrieved methodology content shapes agent behavior in context.
- **Learn.** Frequency graduation + pressure testing + Wilson CI bounds let the corpus improve from use.

Dropping any one defeats the purpose.

---

## Section 2 — Design philosophy

### 2.1 Gate AND classroom (policy, not hope)

Mechanical enforcement is primary; persuasion content is defense-in-depth. No mandatory rule ships without a mechanical enforcement path. Rules with persuasion content but no viable gate are honestly labeled advisory high-severity, not mandatory.

Policy enforced at rule-authoring: `writ add` refuses `mandatory: true` when `mechanical_enforcement_path` is empty or references a hook that doesn't exist. Universal; applied to existing mandatory rules via Phase 2 retroactive audit.

### 2.2 Artifact quality, not just artifact existence

Existence checks are insufficient (an empty plan.md with correct headers passes). Two-tier enforcement:

- **Tier 1 (structural):** word counts, required sections, placeholder blocklists, lexical assertion counts. Hot path, blocking, deterministic. Catches placeholder-tier gaming.
- **Tier 2 (LLM-as-judge):** Haiku rubric evaluation, deferred to PostToolUse / completion-claim. Catches plausible-boilerplate-tier gaming.

Honest ceiling: Gate 5 catches placeholder and plausible-boilerplate failures. Architectural-correctness failures are caught by review subagents (Phase 3), not gates. Don't pretend otherwise.

### 2.3 Retrievable vs. non-retrievable node types

Not every new node type enters the RAG retrieval corpus:

| Node type | Retrievable | Notes |
|---|---|---|
| Rule (existing) | yes | coding + discipline rules |
| Abstraction (existing) | yes (summary mode) | rule clusters |
| Skill | yes | methodology units |
| Playbook | yes | multi-phase workflows |
| Technique | yes | reusable subprocedures |
| AntiPattern | yes | what to avoid + counter |
| ForbiddenResponse | yes (always-on) | phrase-level rules |
| Phase | no | structural, bundle-expansion only |
| Rationalization | no | attached to parent, bundle-only |
| PressureScenario | no | test-only |
| WorkedExample | no | explicit lookup |
| SubagentRole | no | template-only |

Resolves the heterogeneity concern by naming what gets indexed and what doesn't.

### 2.4 Scope boundary — prescriptive, not descriptive

**A node belongs in Writ if and only if its content tells the agent what to do, not what exists.**

- **In scope** (prescriptive, behavior-shaping): coding rules, process playbooks, debugging techniques, rationalization counters, forbidden responses, security disciplines, deployment behavior IF the content is prescriptive ("when deploying, you must run X then Y").
- **Out of scope** (descriptive, reference): API specifications, architecture diagrams, incident reports, meeting notes, general coding knowledge. These may belong in a separate project-local RAG; they do not belong in Writ.

The test goes in `CONTRIBUTING.md` as the explicit scope-boundary rule. It prevents future scope creep by giving contributors a one-sentence heuristic. Writ extends "coding behavior" to "coding process behavior" via absorption; further extensions (on-call, incident response, release management) are in scope if prescriptive, out of scope if reference.

### 2.5 Evidence-gated phases

Every phase has release-blockers: concrete measurements that must pass before the next phase starts. Failure halts the phase; the maintainer writes a report; either the phase is re-scoped or the project pauses. Nine accumulated release-blockers (detailed per phase):

1. Methodology retrieval MRR@5 ≥ 0.78 (Phase 0)
2. Methodology hit rate ≥ 90% (Phase 0)
3. Bundle completeness rate ≥ 85% (Phase 0 and Phase 1)
4. No regression on coding-rule benchmarks (Phase 1)
5. Always-on token audit passes (Phase 2)
6. Zero mandatory rules without mechanical paths, including retroactive audit (Phase 2)
7. Artifact-quality Gate 5 achieves ≥90% true-negative rate on 50-artifact difficulty-spectrum fixture (Phase 2)
8. Pressure-test compliance ≥70% on critical rules (Phase 4)
9. Quality gates meet false-positive rate targets on legitimate-content fixture (Phase 2)

### 2.6 Feature flag everything

All behavioral changes land behind `enforcement.superpowers_absorb.enabled` in `writ.toml`, default off. Per-rule flags allow granular rollout. Rollback is flipping flags.

---

## Section 3 — Architecture overview

### 3.1 Graph as unified substrate

11 node types, 15 edge types. RAG returns bundles (primary node + proximal neighbors), not flat lists. New edges: `TEACHES`, `COUNTERS`, `DEMONSTRATES`, `DISPATCHES`, `GATES`, `PRESSURE_TESTS`, `PRECEDES`.

### 3.2 Retrieval pipeline with heterogeneous content

Stages unchanged architecturally; refined operationally:

1. Domain filter with `retrievable: true` predicate (excludes 5 non-retrievable types).
2. Tantivy BM25 on trigger + statement + tags + body (new, 0.5× weight for Skill/Playbook/Technique bodies to avoid generic methodology vocabulary swamping specific matches).
3. ANN vector on concatenated trigger + statement. Current embedding: `all-MiniLM-L6-v2` (384-dim). Phase 0 evaluates and swaps if MRR@5 < 0.78 on methodology queries.
4. Graph traversal over all edge types via adjacency cache.
5. Two-pass RRF + `w_bundle_cohesion = 0.05` (tuned in Phase 0) + authority rerank.

### 3.3 Learning loop

Writ's existing frequency graduation (n ≥ 50, ratio ≥ 0.75, Wilson 95% CI) extends unchanged to new node types. Feedback sources:

- Stop-hook correlation (existing): links injected rules to lint outcomes.
- Pressure test results (Phase 4): each scenario run writes positive/negative feedback.
- Session audit (Phase 2 `writ-pressure-audit.sh`): session-end JSONL.
- Gate denial ratios: surfaces trigger-too-broad and trigger-too-narrow patterns.

Monthly review ritual (Phase 5) acts on aggregated signals.

### 3.4 Mode-scoped always-on injection

Budget managed via Writ's existing mode system:

- **Universal always-on** (all modes, cap 800 tokens): `ENF-PROC-VERIFY-001`, `ENF-COMMS-001`.
- **Work-mode always-on** (cap 1,500 additional tokens): six build-time discipline rules.
- **Debug-mode always-on** (cap 600 additional tokens): `ENF-PROC-DEBUG-001` advisory.

Render depth is conditional: summary form in always-on path, full form when retrieved via RAG or in `pending_violations`. Saves ~40% per rule in always-on injection.

---

## Section 4 — What the review shaped

Three review rounds produced the current shape. Each round's contribution:

### Round 1 — scope reality check

Critique: graph-wide schema extension is a category change, not addition; retrieval pipeline validated only on Rule nodes; always-on budget math doesn't fit; ground-truth set missing; mechanical vs. persuasive enforcement philosophies aren't reconciled.

Response: Phase 0 added as validation layer; release-blocker gates introduced; mechanical-first policy stated; advisory-vs-mandatory honest scoping.

### Round 2 — gate mechanism specification

Critique: gates converted critiques into thresholds, but several gates were load-bearing in appearance and hollow in mechanism. Gate 2 fallback undefined; Gate 3 makes Scope B retrieval-dependent (single point of failure); Gate 4 applies to new rules but not existing ones; Gate 5 identified hardest problem but didn't specify mechanism.

Response: Gate 2 fallback state concretely defined (Scope A ships at week 7-8 if methodology retrieval fails); bundle completeness rate added as Phase 0 and Phase 1 release-blocker; retroactive audit of existing mandatory rules added to Phase 2; Gate 5 two-tier (structural + LLM-as-judge) specified.

### Round 3 — implementation detail and operational policy

Critique: ground-truth authorship needs human-authored standard; Gate 5 cost/latency acceptable with 5s timeout; blocking vs. advisory default; audit authorship protocol.

Response: Phase 0 explicitly requires human-authored queries with AI drafts as candidates only; Gate 5 hard 5s timeout with timeout-pass-with-warning fallback; Gate 5 blocks on score <3 with `--override-quality-judge` flag logged to friction log; ENF-* audit runs as Claude-Code-proposes / human-verifies with required file:line references per classification.

All three rounds yield a plan where every critique is addressed by a specific release-blocker, mechanism, or deliverable.

---

## Section 5 — Phase 0: Validation infrastructure (1 week)

**Why this exists.** All subsequent phases depend on heterogeneous retrieval quality. Phase 0 measures this before any content investment. If Phase 0 fails, project enters fallback state at week 1 rather than week 8.

### 5.1 Deliverables

1. **Ground-truth query set** at `tests/fixtures/ground_truth_proc.json`. Minimum 30 queries, each with `query` / `expected_node_ids` (ranked) / `rationale`. **Authorship: human maintainer (Lucio or designated reviewer).** Claude Code may propose candidates; final query text and label review are human. Authorship-provenance section in `docs/phase-0-report.md` records who wrote what and when.

2. **Synthetic methodology corpus** at `tests/fixtures/synthetic_methodology/*.md`. ~50 stand-in nodes spanning each planned type. Drafted by hand or reviewed line-by-line by a human before use. Replaced by real content in Phase 2.

3. **Baseline retrieval measurement** via new `tests/test_methodology_retrieval.py` and `benchmarks/methodology_bench.py`. Records MRR@5, hit rate, bundle completeness rate, p95 latency, per-query miss analysis.

4. **Model-evaluation protocol** (Section 14). Triggered if MRR@5 < 0.78 on baseline.

5. **Bundle completeness measurement.** Instrument the pipeline to emit per-query telemetry of (top_ranked_node, expected_bundle_members, actually_rendered_members). Diff yields completeness rate.

6. **Release-blocker decision** documented in `docs/phase-0-report.md`.

### 5.2 Files to touch

- `tests/fixtures/ground_truth_proc.json` (human-authored)
- `tests/fixtures/synthetic_methodology/*.md` (human-authored or reviewed)
- `tests/test_methodology_retrieval.py`
- `benchmarks/methodology_bench.py`
- `docs/phase-0-report.md`
- `writ/retrieval/embeddings.py` (only if model swap needed)
- `writ/retrieval/keyword.py` (only if BM25 tuning needed)
- `writ/retrieval/pipeline.py` (telemetry hooks for bundle completeness)

### 5.3 Acceptance criteria (release blockers)

- **[BLOCKER]** MRR@5 ≥ 0.78 on methodology queries after mitigations.
- **[BLOCKER]** Hit rate ≥ 90% on methodology queries.
- **[BLOCKER]** Bundle completeness rate ≥ 85% on methodology queries.
- **[BLOCKER]** p95 latency on methodology queries ≤ 5ms.
- Coding-rule benchmarks unchanged (Phase 0 read-only to coding pipeline).
- `docs/phase-0-report.md` signed off by maintainer, including authorship provenance.

### 5.4 Fallback on Gate 2 failure

If methodology MRR@5 < 0.78 OR bundle completeness < 85% after all mitigations (embedding swap + BM25 tuning + bundle-cohesion weight increase):

- Phase 1 schema work ships (additive, cheap).
- Phase 2 ships **discipline rules only** as Rules in the existing schema (with new fields: `rationalization_counters`, `red_flag_thoughts`, `always_on`, `mechanical_enforcement_path`). Zero Skill/Playbook/Technique/AntiPattern/ForbiddenResponse content.
- Phase 3 (subagent + approval) proceeds unchanged.
- Phase 4 pressure testing covers discipline rules only.
- Phase 5 observability proceeds unchanged.
- Methodology content (Scope B) enters **deferred status**: documented re-entry criteria, weeks-to-quarters-to-never timeline. `docs/phase-0-report.md` records the specific quality delta and the recommended next steps.

This is "minimum viable ship" — Scope A alone, ~7-8 weeks from Phase 0 start, independently valuable.

### 5.5 Rollback

Phase 0 is read-only to production. Delete fixture files if abandoning.

### 5.6 Risks and mitigations

- **Risk:** synthetic corpus unrepresentative of real content. **Mitigation:** each synthetic node approximates a planned real node in structure and vocabulary; half the ground-truth queries sourced from Superpowers' pressure-test scenarios; other half from observed denial patterns in `workflow-friction.log`.
- **Risk:** ground-truth authorship shortcut under time pressure. **Mitigation:** maintainer halts Phase 0 rather than proceeding with AI-labeled measurement; closed-loop evaluation is worse than delay.

---

## Section 6 — Phase 1: Schema + ingest + pipeline (3 weeks)

### 6.1 Deliverables

1. **Schema additions** in `writ/graph/schema.py`:
   - Rule model gains: `rationalization_counters: list[dict[str, str]]`, `red_flag_thoughts: list[str]`, `always_on: bool = False`, `mechanical_enforcement_path: str | None = None`.
   - 10 new node models (per Section 2.3 retrievable classification).
   - `source_attribution: str | None` on every absorbed node.

2. **Ingest parser** in `writ/graph/ingest.py`: new `<!-- NODE START type=X id=Y -->` marker, edge markers, Pydantic validation with reference resolution. Backwards-compat via `<!-- RULE START -->` alias.

3. **Neo4j migration** in `scripts/migrate.py`: MERGE queries for 10 new labels + 7 new edge types. Idempotent.

4. **Retrieval pipeline refinement** in `writ/retrieval/pipeline.py`, `ranking.py`, `keyword.py`:
   - Stage-1 `retrievable: true` filter.
   - Stage-2 body-field indexing at 0.5× weight for Skill/Playbook/Technique.
   - Stage-4 expanded edge-type support.
   - Stage-5a `w_bundle_cohesion` bonus (seeded 0.05, tuned per Phase 0 results).
   - Mode-scoped always-on injection with conditional-render-depth policy.

5. **Session state** in `bin/lib/writ-session.py`: `active_playbook`, `active_phase`, `playbook_phase_history`, `review_ordering_state`, `verification_evidence`, `quality_judgment_state` (new for Gate 5 Tier 2). Forward-compat defaults.

6. **Server endpoints**: `POST /session/{sid}/verification-evidence`, `GET /session/{sid}/active-playbook`, `POST /session/{sid}/quality-judgment`, etc.

7. **Tests:** 5 new files covering schema roundtrip, multi-node ingest, bundle-cohesion, retrievable filter, playbook state transitions.

8. **Re-run methodology + coding-rule benchmarks** after pipeline changes.

### 6.2 Acceptance criteria (release blockers)

- **[BLOCKER]** All new and existing tests pass.
- **[BLOCKER]** Coding-rule benchmark: MRR@5 ≥ 0.78, hit rate ≥ 90%, p95 ≤ 1.1× pre-phase baseline. No regression.
- **[BLOCKER]** Methodology benchmark re-run: MRR@5 ≥ 0.78, hit rate ≥ 90%, bundle completeness ≥ 85%, p95 ≤ 5ms.
- **[BLOCKER]** `python scripts/migrate.py` idempotent.
- Session caches forward-compat.

### 6.3 Rollback

`git revert` Phase 1 commits. `docker compose down -v && docker compose up -d && python scripts/migrate.py` restores from `bible.bak`.

---

## Section 7 — Phase 2: Discipline rules + hooks + quality gates (4 weeks)

### 7.1 Deliverables

1. **Eight new rules** (ENF-PROC-* and ENF-COMMS-*) per Section 12 content map. Each includes:
   - Trigger, statement, violation, pass_example, enforcement, rationale (existing Rule fields).
   - 3-5 `rationalization_counters`.
   - 3-8 `red_flag_thoughts`.
   - `mechanical_enforcement_path` string naming hook + matcher + deny condition. Empty only for advisory rules.

2. **Rule classifications:**
   - Mandatory + mechanical: ENF-PROC-BRAIN, PLAN, TDD, VERIFY, SDD, WORKTREE.
   - Advisory high-severity (no viable mechanical path): ENF-PROC-DEBUG, ENF-COMMS.

3. **Four new hooks** wired into `templates/settings.json`:
   - `writ-verify-before-claim.sh` (PreToolUse TodoWrite + Stop)
   - `writ-sdd-review-order.sh` (PreToolUse Task)
   - `writ-worktree-safety.sh` (PreToolUse Bash matcher `git worktree add`)
   - `writ-pressure-audit.sh` (SessionEnd)

4. **Artifact-quality Tier 1 (structural)**:
   - `validate-exit-plan.sh` extended (Section 15.1).
   - New `validate-test-file.sh` (Section 15.2).
   - New `validate-design-doc.sh` (Section 15.3).

5. **Artifact-quality Tier 2 (LLM-as-judge)**:
   - New `writ-quality-judge.sh` hook (PostToolUse on spec/plan/design writes).
   - Integration with `writ/analysis/llm.py` for Haiku rubric calls.
   - 5-second hard timeout; timeout = "pass with warning" written to session state.
   - Blocks completion claims when unresolved score < 3 exists.
   - `--override-quality-judge` flag available; override logged to friction log.
   - Rubrics per artifact type (Section 15.4-15.6).

6. **Rule-authoring policy** in `writ/gate.py`:
   - Rejects `mandatory: true` without `mechanical_enforcement_path`.
   - Rejects `rationalization_counters` with fewer than 2 entries.
   - Warns on `red_flag_thoughts` outside 3-8 range.

7. **Retroactive audit of existing mandatory rules** (per Section 17 protocol):
   - Claude Code proposes classifications for all existing `mandatory: true` rules with file:line references.
   - Human maintainer spot-checks and finalizes.
   - Rules without viable mechanical paths demoted to advisory via `writ edit`.
   - Audit report in `docs/mandatory-rule-audit.md`.

8. **50-artifact fixture** for Gate 5 true-negative measurement at `tests/fixtures/gamed_artifacts/`:
   - 15-20 trivially bad (empty sections, lorem ipsum, single-word content).
   - 15-20 plausible-boilerplate (generic content that structurally passes, says nothing task-specific).
   - 10-15 near-miss (references real files and real steps, specific quality failure such as missing success criteria, circular reasoning, contradictory requirements).
   - Hand-crafted by maintainer. The rubric's value is proven against the hard cases.

9. **Render changes** in `writ-rag-inject.sh`: always-on bundle labeled `=== ALWAYS-ACTIVE RULES ===`, authority verbs preserved, conditional render-depth per Section 3.4.

10. **Feature-flag gating** on all new hooks via `enforcement.superpowers_absorb.enabled` check; per-rule flags via `enforcement.rules.<rule-id>.enabled`.

11. **Tests:** rule hook behavior, quality-gate behavior, rule-authoring policy, rubric evaluation on 50-artifact fixture.

### 7.2 Acceptance criteria (release blockers)

- **[BLOCKER]** All new hooks pass test fixtures (deny on violation, allow on compliance).
- **[BLOCKER]** Real token audit: universal always-on ≤ 800 tokens, work-mode ≤ 1,500 additional, debug-mode ≤ 600 additional.
- **[BLOCKER]** Zero mandatory rules (new or existing, per retroactive audit) with empty `mechanical_enforcement_path`.
- **[BLOCKER]** Gate 5 Tier 2 achieves ≥ 90% true-negative rate on 50-artifact fixture across the difficulty spectrum.
- **[BLOCKER]** Gate 5 Tier 1 false-positive rate ≤ 5% on 50-legitimate-document fixture; Tier 2 false-positive rate ≤ 10%.
- **[BLOCKER]** Feature flag off: sessions behave identically to pre-Phase-2.
- `writ validate` reports 0 conflicts, 0 orphans, 0 staleness warnings for new rules.

### 7.3 Rollback

Flip `enforcement.superpowers_absorb.enabled = false`. Per-rule flags allow granular rollback. Full revert via `git revert`.

### 7.4 Risks and mitigations

- **Risk:** quality-judge false positives (legitimate content scored <3 due to rubric limitations). **Mitigation:** `--override-quality-judge` flag; monthly review ritual (Phase 5) includes rubric refinement from false-positive analysis; false positives are friction but preferable to false negatives.
- **Risk:** Haiku judge fooled by sophisticated gaming. **Mitigation:** N=3 voting upgrade available if Phase 4 pressure tests show consistent fooling; ultimately, architectural-correctness failures are caught by review subagents, not gates.
- **Risk:** Always-on token audit exceeds cap after retroactive-audit-discovered mandatory rules add content. **Mitigation:** conditional-render-depth policy compresses further; if still over, advisory-demote candidates named in Section 16 decision #7.
- **Risk:** LLM API latency blocks session. **Mitigation:** 5-second hard timeout, timeout = pass with warning; API dependency accepted as bounded-cost, bounded-latency, not hot-path-critical.

---

## Section 8 — Phase 3: Subagent harmonization + approval flow (3 weeks)

### 8.1 Deliverables

1. Two new subagent definitions: `writ-spec-reviewer`, `writ-code-quality-reviewer`, ported from Superpowers SDD prompts.

2. SubagentRole nodes in graph for all 6 subagents; `.claude/agents/*.md` auto-exported from graph.

3. Approval flow replacement:
   - `/writ-approve` slash command registered via Claude Code plugin.
   - MCP-style `writ_approve` tool as fallback.
   - `auto-approve-gate.sh` rewrapped: substring "approved" triggers confirmation ask-prompt, not silent advance.
   - `POST /session/{sid}/advance-phase` gains `confirmation_source: "tool" | "pattern" | "explicit"`.

4. Integration tests: review ordering, approval sources, backwards-compat for in-flight sessions.

### 8.2 Acceptance criteria

- `/writ-approve design` advances gate with `confirmation_source = "tool"` logged.
- "approved" substring triggers ask-prompt, not silent advance.
- Review-ordering enforcement: spec before code quality; reverse order denied.
- `writ review prompt <role>` returns graph-canonical text.

### 8.3 Rollback

Additive. Revert slash-command registration if problematic; string-match fallback unchanged.

---

## Section 9 — Phase 4: Pressure-scenario harness (4 weeks)

### 9.1 Deliverables

1. **Pressure-scenario corpus** at `tests/pressure/*.md`. Minimum 30 scenarios, 3+ per critical rule (24 for the 8 ENF-PROC/COMMS rules + 7 for top existing Writ rules).

2. **CLI**: `writ test-pressure --rule|--scenario|--model`. Output per-scenario JSON with pass/fail + rationalization text captured on failure.

3. **CI integration**: `.github/workflows/pressure-tests.yml`. Nightly Sonnet, per-PR Haiku. Baseline in `tests/pressure/baseline.json`. CI fails if compliance drops >10 percentage points.

4. **Authoring-gate extension** in `writ/gate.py`: new mandatory ENF-* rules require ≥1 linked PressureScenario; `--skip-pressure-check` override logged.

5. **Feedback integration**: pressure-test runs write `/feedback` signals; captured rationalization text informs future `rationalization_counters` content.

### 9.2 Acceptance criteria (release blockers)

- **[BLOCKER]** All 8 ENF-PROC/COMMS rules have ≥3 linked PressureScenario nodes.
- **[BLOCKER]** Each critical rule's compliance rate ≥ 70% on Claude Sonnet baseline. Failing rules revised and re-tested before Phase 5 baseline is declared stable.
- **[BLOCKER]** Feedback signals flow to frequency counters (verified via `writ analyze-friction --rule-effectiveness`).

### 9.3 Rollback

Opt-in test infrastructure. Disable CI workflow; CLI remains available. Authoring-gate can flip to `warn` mode.

---

## Section 10 — Phase 5: Measurement + graduation + trim (ongoing)

### 10.1 Deliverables

1. **`writ analyze-friction` extensions**: `--playbook-compliance`, `--rule-effectiveness`, `--skill-usage`, `--graduation-candidates`, `--trim-candidates`, `--quality-judge-false-positives`.

2. **Monthly review ritual** in `CONTRIBUTING.md`:
   - High-denial rules: triage (trigger too broad? missing mechanical path?).
   - Low-activation rules (<5 per 90 days): trim or consolidate.
   - Repeated rationalizations: add counters from captured text.
   - Low-activation skills (<2 per 60 days): deprecate.
   - Quality-judge false positives: refine rubrics.

3. **Optional dashboard** at `writ/server.py /dashboard`: zero-dep HTML+JSON render.

### 10.2 Acceptance criteria

- `writ analyze-friction --rule-effectiveness --since 30` produces readable table.
- Monthly review ritual documented, referenced from `CONTRIBUTING.md`.
- Dashboard (if built) renders in major browsers without JS framework.

---

## Section 11 — Timeline

| Phase | Weeks | Risk | Release blockers |
|---|---|---|---|
| 0 | 1 | low | MRR@5 ≥ 0.78 on methodology; hit rate ≥ 90%; bundle completeness ≥ 85% |
| 1 | 3 | low | no coding-rule regression; methodology benchmark holds with pipeline changes |
| 2 | 4 | medium | token audit passes; zero mandatory-without-mechanical; Gate 5 ≥90% true-negative; retroactive audit complete |
| 3 | 3 | medium | approval tool works; review ordering enforced |
| 4 | 4 | low | pressure compliance ≥70% on critical rules |
| 5 | ongoing | none | — |

**Total to Phase 4 completion:** 15 weeks.

**Parallelizable to 10-11 weeks** with two people: Phase 0 authorship + Phase 1 schema in parallel; Phase 2 rule authoring + Phase 4 scenario authoring independent; Phase 3 independent of rule authoring.

**Fallback timeline** (if Gate 2 fails in Phase 0): Scope A ships weeks 7-8 with schema + 8 discipline rules + 4 hooks + quality gates + pressure harness + approval fix. Scope B methodology content waits for retrieval fix.

---

## Section 12 — Appendix: content extraction source map

| Writ node | Source | Notes |
|---|---|---|
| `SKL-PROC-BRAIN-001` + `PBK-PROC-BRAIN-001` | `skills/brainstorming/SKILL.md` | HARD-GATE (12-14) → ENF-PROC-BRAIN statement |
| `SKL-PROC-VISUAL-001` | `skills/brainstorming/visual-companion.md` | Decision tree (5-26) → Technique |
| `SKL-PROC-PLAN-001` + `PBK-PROC-PLAN-001` | `skills/writing-plans/SKILL.md` | No-placeholders (106-114) → quality gate |
| `SKL-PROC-EXEC-001` | `skills/executing-plans/SKILL.md` | CONFLICTS_WITH → PBK-PROC-SDD-001 |
| `PBK-PROC-SDD-001` + 3 `ROL-*` | `skills/subagent-driven-development/` | Two-stage review ordering (247) |
| `PBK-PROC-FINISH-001` | `skills/finishing-a-development-branch/SKILL.md` | 4-options decision (153-159) |
| `TEC-PROC-WORKTREE-001` | `skills/using-git-worktrees/SKILL.md` | Gitignore safety (64) |
| `PBK-PROC-DEBUG-001` + techniques | `skills/systematic-debugging/` | Red flags (215-232); rationalizations (245-256) |
| `PBK-PROC-TDD-001` + 5 `ANT-PROC-TDD-*` | `skills/test-driven-development/` | Five anti-patterns → AntiPattern nodes |
| `SKL-PROC-VERIFY-001` | `skills/verification-before-completion/SKILL.md` | Common Failures (42-50); Forbidden Responses |
| `PBK-PROC-REVREQ-001` + `ROL-CODE-REVIEWER-001` | `skills/requesting-code-review/` | Reviewer prompt template |
| `SKL-PROC-REVRECV-001` + `FRB-COMMS-001` | `skills/receiving-code-review/SKILL.md` | Forbidden responses (27-38) |
| `SKL-PROC-PARALLEL-001` | `skills/dispatching-parallel-agents/SKILL.md` | Decision tree (17-34) |
| `META-AUTH-*` rules | `skills/writing-skills/` | Description-triggering-conditions (150-157) |

---

## Section 13 — Appendix: token-budget math

### 13.1 Per-rule render shapes

Full render (retrieved via RAG or in pending_violations): ~380 tokens.
Summary render (always-on path): ~160 tokens.

### 13.2 Always-on budget accounting

- **Universal tier**: `VERIFY` + `COMMS` summary + preamble = **360 tokens** (cap 800; fits).
- **Work-mode tier**: `BRAIN` + `PLAN` + `TDD` + `SDD` + `WORKTREE` summary + preamble = **830 tokens** (cap 1,500; fits).
- **Debug-mode tier**: `DEBUG` summary + preamble = **190 tokens** (cap 600; fits).

Worst case (work mode): 360 + 830 = 1,190 tokens. Leaves 6,800+ tokens in session budget for retrieved bundles and context.

### 13.3 Retrieved-bundle accounting

Typical methodology retrieval: 1 Skill (400) + 2 Rules (760) + 1 AntiPattern (150) + 3 Rationalizations (90) = ~1,400 tokens. Plus always-on (1,190 in work mode) = 2,590. Well within 8,000.

### 13.4 Gate 5 Tier 2 latency / cost budget

- Haiku call: ~500-1500ms typical, 5s hard timeout.
- Per-session cost: $0.01-0.05 typical (one judge call per significant artifact write; 3-10 artifacts per session).
- Heavy usage (50 sessions/day): $0.50-2.50/day.
- Timeout policy: 5s exceeded → pass with warning written to session state; completion claim not blocked. Prevents stuck API from blocking agent indefinitely.

---

## Section 14 — Appendix: embedding model evaluation protocol

Triggered when Phase 0 methodology MRR@5 < 0.78 with baseline `all-MiniLM-L6-v2`.

| Model | Dim | Size | Evaluate because |
|---|---|---|---|
| `all-MiniLM-L6-v2` | 384 | 25MB | baseline |
| `bge-small-en-v1.5` | 384 | 33MB | stronger on instruction text (MTEB) |
| `bge-base-en-v1.5` | 768 | 110MB | highest quality if memory budget allows |
| `gte-small` | 384 | 33MB | alternative strong small |

Swap in `writ/retrieval/embeddings.py`, rebuild HNSW index, re-run methodology and coding-rule benchmarks. Adopt if methodology improves AND coding-rule doesn't regress AND memory fits. Memory ceiling: 3.5GB RAM at 1K rules + 500 methodology nodes.

Fallback if no model produces 0.78: structured-trigger boosting with controlled vocabulary on methodology triggers. If that also fails: Phase 0 declares Gate 2 failure; project enters Scope-A-only fallback per Section 5.4.

---

## Section 15 — Appendix: artifact-quality gate specifications

### 15.1 Tier 1 — plan quality gate (`validate-exit-plan.sh` extended)

Checks per `plan.md` section (`## Files`, `## Analysis`, `## Rules Applied`, `## Capabilities`):
- ≥ 30 words of non-blocklist content per section.
- `## Files`: ≥ 1 concrete file path.
- `## Capabilities`: ≥ 1 checkbox item, each ≥ 10 words.
- Blocklist: `TODO`, `TBD`, `fill in`, `appropriate`, `similar to above`, `as needed`, `placeholder`, `<describe>`, `<your text>`.
- Override: `--skip-quality-check` flag, logged.

### 15.2 Tier 1 — test-file assertion gate (new `validate-test-file.sh`)

On PreToolUse Write matching `{src}/**/*.{py,js,ts,php,go,rs,java}`:
- Find corresponding test file via extension + naming convention.
- If test file missing: deny (existing TDD gate).
- If test file exists: lexical count `assert|expect|should|test_`. If zero: deny.
- Bypass via `session.mode == "prototype"` (new mode, reserved for throwaway work).

### 15.3 Tier 1 — design-doc quality gate (new `validate-design-doc.sh`)

On Write to `docs/**/specs/*-design.md`:
- Required subsections: `## Goal`, `## Constraints`, `## Alternatives Considered`, `## Chosen Approach`, `## Risks`.
- Each subsection: ≥ 50 words of non-blocklist content.
- `## Alternatives Considered`: ≥ 2 named alternatives.
- `## Risks`: ≥ 1 risk with named mitigation.
- Override logged.

### 15.4 Tier 2 — plan-quality rubric (Haiku judge)

Triggered on PostToolUse Write to `docs/**/plans/*.md`. Prompt:
```
You are evaluating a plan document. Score 0-5 on whether each ## section contains
substantive content SPECIFIC to this task, or generic boilerplate that could be
pasted into any plan. Specific content: names concrete files, states verifiable
success criteria, lists realistic implementation steps. Generic boilerplate:
placeholder-level content that structurally exists but conveys no task-specific
information. Score ≥3 passes.

Plan:
<plan.md content>

Return JSON: {"score": N, "failing_section": "X or null", "rationale": "one sentence"}
```
Temperature 0. N=1 baseline; N=3 voting available via `writ.toml` `[gate_5] voting_n = 1`.

### 15.5 Tier 2 — design-doc rubric

Triggered on PostToolUse Write to `docs/**/specs/*-design.md`. Rubric evaluates whether Alternatives Considered section names real alternatives (not straw-men), Constraints section names actual constraints (not generic platitudes), Risks section names actionable risks with plausible mitigations.

### 15.6 Tier 2 — test-file rubric

Triggered on PostToolUse Write to test files. Rubric evaluates whether assertions test real behavior (call real code, verify outputs against expectations) vs. testing mocks or trivially-true conditions. Integrates with `skills/test-driven-development/testing-anti-patterns.md` content (5 named anti-patterns).

### 15.7 Tier 2 policy

- 5-second hard timeout per call.
- Timeout = "pass with warning", written to session state. Does not block.
- Score < 3 blocks completion claims via `writ-verify-before-claim.sh` integration at TodoWrite or Stop.
- `--override-quality-judge` flag available. Override logged to friction log with scored section and override reason.
- N=3 voting upgrade available for high-stakes gates if Phase 4 shows consistent fooling.

### 15.8 50-artifact fixture for ≥90% true-negative acceptance criterion

`tests/fixtures/gamed_artifacts/` with difficulty spectrum:
- **Trivially bad** (15-20): empty sections, lorem ipsum fills, single-word content, pure structural compliance with zero content.
- **Plausible-boilerplate** (15-20): generic prose that structurally passes, references files that don't exist or uses vague verbs ("improve performance", "handle edge cases" without specifying which).
- **Near-miss** (10-15): references real files, real steps, specific quality failure — missing success criteria, circular reasoning in chosen-approach, contradictory requirements, alternatives-considered lists "do nothing" and "do the thing".

Hand-crafted by maintainer. The rubric's value is proven against the hard cases. Run Gate 5 Tier 2 over the fixture; require ≥90% correctly flagged as failing (true-negative on a quality test).

### 15.9 False-positive fixture

`tests/fixtures/legitimate_artifacts/` with 50 hand-labeled legitimate plans/designs/test files. Gate 5 must achieve false-positive rate ≤ 5% on Tier 1, ≤ 10% on Tier 2. Exceeding targets triggers rubric refinement before Phase 2 ships.

---

## Section 16 — Appendix: scope boundary landing in CONTRIBUTING.md

New section for `CONTRIBUTING.md`:

> ### Scope test for new Writ content
>
> A node belongs in Writ if and only if its content tells the agent what to do, not what exists.
>
> **In scope** (prescriptive, behavior-shaping):
> - Coding rules ("must use named binds")
> - Process playbooks ("brainstorm before code")
> - Debugging techniques ("investigate root cause before fixing")
> - Rationalization counters ("when you think X, counter with Y")
> - Forbidden responses ("never say X")
>
> **Out of scope** (descriptive, reference):
> - API specifications
> - Architecture diagrams
> - Incident reports
> - Meeting notes
> - General coding knowledge
>
> Reference material may belong in a separate project-local RAG system. It does not belong in Writ.
>
> Extensions to new domains (deployment, on-call, incident response) are in scope if prescriptive, out of scope if reference. When proposing a new node, apply the one-sentence test first.

---

## Section 17 — Appendix: ENF-* retroactive audit protocol

### 17.1 Scope

All existing rules in `bible/` with `mandatory: true`. Estimate: 20-30 rules.

### 17.2 Protocol

**Claude Code proposes, human verifies.** For each mandatory rule, Claude Code produces one row of a classification table:

| rule_id | classification | mechanical_enforcement_path | verification |
|---|---|---|---|
| ENF-GATE-PLAN-001 | has path | `.claude/hooks/validate-exit-plan.sh:45-92` matcher `ExitPlanMode`, denies on missing sections | `writ-session.py:312` reads `gates_approved` |
| ENF-SEC-001 | has path | `bin/run-analysis.sh:78` routes to PHPStan for PHP; static-analysis catches missing ownership check | file:line references |
| ENF-CTX-004 | no viable path | n/a — rule is about context-retrieval discipline, no lexical detector possible | — |
| ... | ... | ... | ... |

**Required for "has path" classification:**
- Specific hook filename.
- Specific matcher (tool + pattern).
- Specific deny condition (what the hook checks before denying).
- File:line reference for each claim, verifiable in 30 seconds.

**Required for "could have path" classification:**
- Name the hypothetical hook and what it would check.
- Flag as Phase 2.5 work candidate.

**Required for "no viable path" classification:**
- Explain why lexical/static detection is impossible for this rule.
- Demotion to advisory recommended.

### 17.3 Human verification

Maintainer reads the proposed table. For each row:
- **"has path" rows:** spot-check 1 in 3 by opening the cited hook file at the cited lines. Verify the matcher and deny condition match the claim. If any spot-check fails, all rows re-reviewed.
- **"no viable path" rows:** read reasoning, accept or push back. If push back, Claude Code re-classifies.
- **"could have path" rows:** defer to Phase 2.5 backlog or accept demotion now.

### 17.4 Output

- `docs/mandatory-rule-audit.md` contains the full classification table with reasoning per row.
- `writ edit` applied to each demoted rule: `mandatory: true` → `mandatory: false`, severity remains `critical` or `high`.
- Audit complete before Phase 2 release-blocker ("zero mandatory without mechanical") applies to the full corpus.

### 17.5 Estimate

2-3 days of Claude Code audit + 1 day human verification = ~4 days total. Parallel to other Phase 2 work.

---

## Section 18 — Open decisions still requiring human input

(Prior decisions resolved in review rounds: attribution, approval tool, feature-flag scope, embedding default, subagent prompt authority, pressure-test model, commands directory, debug-mode trigger, meta-skill, LLM cost, advisory-vs-blocking default, audit authorship.)

Still needing input:

1. **Scope of mandatory in non-work modes.** Should `ENF-PROC-BRAIN-001` fire in `review` mode (auditing existing code, not building new)? Default: no. Confirm.

2. **Prototype mode definition.** New `mode=prototype` reserved for throwaway work bypasses TDD gate (Section 15.2). Trigger: manual user declaration only, or keyword-detected from prompt, or both? Default: manual only. Confirm.

3. **Always-on demotion order** (if Phase 2 token audit exceeds cap). Default: demote ENF-PROC-SDD-001 first (specific to subagent workflows, not universal). Confirm.

4. **Quality-judge false-positive override threshold.** How many overrides per session before escalating to maintainer review? Default: 3. Confirm.

5. **N=3 voting criteria.** Which gates warrant N=3 upgrade from N=1 baseline? Default: no gates unless Phase 4 pressure tests show fooling. Confirm timing.

6. **Per-project override config.** Does `.writ.toml` in a project override global `writ.toml` for rule enablement? Default: yes, Phase 5 nice-to-have.

---

## Summary

Fifteen weeks. Six phases. Nine release-blocker gates. Five rounds of reviewer critique converged into concrete mechanisms. Every hand-wave replaced with a specific threshold, a specific mechanism, or an honest advisory-vs-mandatory distinction.

**What the plan delivers:**
- Enforce — mechanical gates block critical violations at artifact-quality level, not just existence level
- Teach — retrieved methodology content shapes behavior via persuasion-engineered rationalization counters, red flags, forbidden responses
- Learn — frequency graduation + Wilson CI + pressure testing + quality-judge false-positive refinement cycles improve the corpus from use

**Worst case:** Gate 2 fails. Project enters Scope A fallback: discipline rules + hooks + quality gates + pressure harness + approval fix ship at week 7-8. Methodology content deferred until retrieval proves out. Independently valuable.

**Best case:** all gates pass. Writ becomes the first system to solve process-methodology knowledge at scale, with mechanical enforcement, learning from use, and a defensible scope boundary (prescriptive, not descriptive) that prevents future drift.

Feature-flagged end-to-end. Per-rule rollout. Honest advisory classification. Attribution preserved. Rollback defined per phase. Authorship provenance required where bias matters (ground-truth), delegated where verifiable (retroactive audit).

Ready for implementation.

_End of plan._
