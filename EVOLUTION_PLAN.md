# WRIT -- Evolution Plan

**From Coding Rule Retrieval to General-Purpose Experiential Memory**

| | |
|---|---|
| **Author** | Lucio Saldivar (Infinri) |
| **Date** | 2026-03-23 |
| **Status** | Approved for implementation |
| **Codename** | Dwarf in the Glass |
| **Prerequisite** | All 9 original phases complete. 80 rules, 191 tests, 12 benchmarks. |

---

## How to Read This Document

This plan was developed through three rounds of adversarial review. Each design decision has been pressure-tested against concrete failure modes. Where a decision was revised based on feedback, the revision is noted.

The plan is sequenced by dependency order. Each phase must be proven before the next begins. Phase 5 is conditional -- it proceeds only if a concrete generalizability test passes.

---

## 1. Current State (Baseline)

Writ is a hybrid RAG knowledge retrieval service. Five-stage pipeline: domain filter, BM25 keyword search (Tantivy), ANN vector search (hnswlib), graph traversal (pre-computed adjacency cache), two-pass RRF ranking. FastAPI service at localhost:8765.

### What Exists

| Component | Status |
|---|---|
| Schema (Rule, Abstraction, Domain, Evidence, Tag + 9 edge types) | Complete |
| 5-stage retrieval pipeline | Complete, validated |
| Ranking formula (w_bm25=0.198, w_vector=0.594, w_severity=0.099, w_confidence=0.099, w_graph=0.01) | Tuned, stored in writ.toml |
| Authoring tools (suggest_relationships, check_redundancy, check_conflicts) | Complete |
| Compression layer (HDBSCAN + k-means, abstraction nodes) | Complete |
| Session tracker (client-side, stateless server) | Complete |
| Export with round-trip fidelity | Complete |
| 83 ground-truth queries, MRR@5 = 0.7842, hit rate = 97.59% | Validated |
| p95 latency = 6.7ms (80 rules), 8.0ms (10K rules) | Under 10ms budget |

### What Does Not Exist

| Component | Status |
|---|---|
| Neo4j uniqueness constraint on rule_id | Not applied |
| Neo4j indexes on domain, mandatory | Not applied |
| Mandatory field decoupled from ENF- prefix | Still derived from rule_id.startswith("ENF-") |
| `authority` property (human / ai-provisional / ai-promoted) | Not in schema |
| `times_seen_positive` / `times_seen_negative` / `last_seen` | Not in schema |
| AI rule ingestion workflow | Not implemented |
| Frequency-based ranking | Not implemented |
| Judgment gate / conscience layer | Not implemented |
| Enforcement loop for retrieval consultation (hooks) | Not implemented |
| Human review queue for AI-proposed rules | Not implemented |

---

## 2. Architectural Invariants (Do Not Violate)

These hold across all phases. Taken from the original evolution plan, validated through review.

1. **Enforcement is application-layer, never prompt-layer.** The AI cannot skip Writ consultation because the orchestration code makes the call, not the AI.
2. **Human-authored rules always outrank AI-generated rules at equal relevance.** Enforced mechanically via hard preference rule in ranking, not via weight-based suppression. See Phase 3.
3. **The retrieval pipeline remains the source of truth for "what rules apply."** No system bypasses retrieval to inject rules directly into context.
4. **Every rule in the graph has a defined loading condition.** A rule with no retrieval path is dead.
5. **Writ is a local service.** No cloud dependencies. Graph, indexes, and inference all run on localhost.
6. **Schema changes must be validated against the four-domain test** (coding, business policy, behavioral, operational) before merging. No regression to domain-specific structures.
7. **The structural pre-filter is not the judgment gate.** The pre-filter catches duplicates, vague rules, conflicts, and redundancy. It cannot evaluate whether a rule is correct. Human judgment is the gate for novel rule acceptance until empirical data proves otherwise. Name this honestly at every layer.

---/home/lucio.saldivar/workspaces/upgrade.2ndswing.com/app/code/SecondSwing

## Phase 1: Schema Hardening

**Prerequisite:** Schema audit (done).
**Deliverable:** Updated schema, passing tests, Neo4j constraints applied.

### 1a. Neo4j Constraints and Indexes

Add uniqueness constraint and performance indexes. No data migration. Additive.

```cypher
CREATE CONSTRAINT rule_id_unique IF NOT EXISTS FOR (r:Rule) REQUIRE r.rule_id IS UNIQUE;
CREATE INDEX rule_domain IF NOT EXISTS FOR (r:Rule) ON (r.domain);
CREATE INDEX rule_mandatory IF NOT EXISTS FOR (r:Rule) ON (r.mandatory);
```

**Why now:** The uniqueness constraint is a correctness issue that becomes critical when AI-authored rules enter the graph in Phase 3. Apply it before any new write paths are added.

### 1b. Decouple Mandatory Flag from ENF- Prefix

**Current behavior:** `writ/graph/ingest.py` derives `mandatory` from `rule_id.startswith("ENF-")`. This hardcodes the assumption that only enforcement-prefixed rules are mandatory.

**Change:** Add an explicit `Mandatory: true|false` field to the rule Markdown schema. Default to `false`. During ingestion, read from the Markdown field. Existing ENF-* rules default to mandatory via convention, but other rules (e.g., `COMPLIANCE-SOX-001`, `SAFETY-CRIT-001`) can now opt in.

**Scope:** Ingest parser change + schema documentation update. The boolean property already exists on all graph nodes. Only the derivation logic changes.

### 1c. Extensible Scope Enum

**Decision (revised from original plan):** Do NOT rename `file` to `entity`, `module` to `component`, `pr` to `changeset`. Per adversarial review: "entity" means everything and therefore nothing. The current names are specific and honest about what they mean in the coding domain.

**Instead:** Make scope values domain-specific with the enum extensible per domain:

- **Coding domain (current):** `file`, `module`, `slice`, `pr`, `session`
- **Future domains add values as needed:** e.g., `policy`, `document`, `deployment`, `conversation`

**Implementation:** Change the `Scope` enum in `writ/graph/schema.py` to accept any lowercase string matching `[a-z][a-z0-9_-]*` rather than a closed enum. Validate format, not membership. Domain-specific valid values are documented per domain, not enforced in code.

**Rationale:** Option (a) from the review -- extensible domain-specific scope values -- is cleaner than vague renames (option b). A policy document is a `policy`, not an `entity`. Precision over false generality.

**Data migration:** None. Existing values remain valid.

### 1d. Document Enforcement Field Semantics

The `enforcement` field is free-text. Add documentation and rule-template conventions for non-automated enforcement values: `human-review`, `judgment-gate`, `training-feedback`, `audit-log`, `advisory-only`.

**Scope:** Documentation and rule-template update only. No code or data changes.

### Acceptance Criteria

- [ ] Neo4j constraints applied, verified via `SHOW CONSTRAINTS` and `SHOW INDEXES`
- [ ] `writ ingest` reads `Mandatory:` field from Markdown; ENF-* rules still default to mandatory
- [ ] Scope accepts arbitrary lowercase strings; existing 80 rules unchanged
- [ ] All existing tests pass
- [ ] New tests: constraint violation (duplicate rule_id rejected), mandatory field override, scope extensibility

---

## Phase 2: Structural Pre-Filter + Human Review Queue

**Prerequisite:** Phase 1 complete.
**Deliverable:** Consolidated structural gate function, `writ review` CLI, gate validated against corpus.

### 2a. Structural Pre-Filter

**What this is:** A consolidation of four existing checks into a single gate function that produces a binary accept/reject for candidate rules. This is a structural filter, not qualitative judgment.

**What this is not:** The judgment gate described in the original evolution plan. The actual judgment -- "is this rule correct for this domain?" -- remains a human responsibility. See invariant #7.

**Gate function signature:**

```python
def structural_gate(candidate: dict, pipeline: RetrievalPipeline) -> GateResult:
    """Screen a candidate rule against structural quality checks.

    Returns accept/reject with reasons. Does NOT evaluate correctness.
    """
```

**Checks (all exist in the codebase today, reorganized into one function):**

| Check | Source | Reject When |
|---|---|---|
| Schema validation | Pydantic Rule model | Any required field missing or invalid |
| Redundancy | `authoring.py:check_redundancy` | Cosine similarity > 0.95 with existing rule |
| Conflict | `authoring.py:check_conflicts` | CONFLICTS_WITH edge exists and candidate has weaker evidence |
| Novelty | Vector search via pipeline | Cosine similarity > 0.85 with existing rule (candidate is not novel enough to justify a separate rule) |
| Specificity | Keyword heuristics | Trigger or statement contains disqualifying vague language (see Section 2.2 of handbook: "consider", "be aware of", "where appropriate") |

**GateResult:**

```python
@dataclass
class GateResult:
    accepted: bool
    reasons: list[str]       # Why rejected, or empty if accepted
    similar_rules: list[str] # Rule IDs that triggered novelty/redundancy flags
```

**Similarity dead zone (0.86-0.94):** The novelty threshold (< 0.85 = novel) and redundancy threshold (> 0.95 = duplicate) create a ten-point band where a candidate is considered "novel enough" but "not a duplicate." This band may represent legitimate related-but-distinct rules, or it may become a vector for gradual corpus pollution where AI-generated rules slowly crowd the space around popular existing rules. Do not solve this now. Monitor it during Phase 3 via `writ review --stats` -- if the AI-provisional rule count in this similarity band grows disproportionately relative to other bands, tighten the novelty threshold or add a "similar rule density" check. Both thresholds are configurable in `writ.toml` (see configuration section).

**What this gate catches:** Duplicates, near-duplicates, vague rules, structurally malformed rules, rules that conflict with stronger existing rules.

**What this gate cannot catch:** A rule that is structurally valid, specific, novel, non-conflicting, and wrong. Example: "All async functions must use asyncio.gather for concurrent operations" passes every structural check and is still bad advice in many contexts. This is why the human review queue exists.

### 2b. `writ review` -- Human Review Queue

A CLI command for reviewing AI-proposed rules. Not a dashboard, not email, not a separate service. Stays in the terminal.

| Command | Action |
|---|---|
| `writ review` | List all `authority: ai-provisional` rules, sorted by `last_seen` descending. Shows: rule_id, trigger, statement, date created. |
| `writ review <rule_id>` | Full rule with context: similar human-authored rules (vector neighbors), CONFLICTS_WITH edges, origin context (from SQLite -- see 2c). |
| `writ review <rule_id> --promote` | Sets `authority: ai-promoted`. Allows confidence upgrade to `peer-reviewed`. Requires confirmation. |
| `writ review <rule_id> --reject` | Deletes the rule from the graph. No rejection log stored (see design decision below). |
| `writ review <rule_id> --downweight` | Keeps rule in graph, sets confidence floor (0.1). Effectively suppressed from retrieval but retained for observation. |
| `writ review --stats` | Total AI-provisional, total promoted, total rejected (computed from proposed-minus-remaining), average time-to-review. |

**Enforcement:** `writ validate` gains a new check -- if unreviewed AI-provisional rules exceed the threshold, it warns. Threshold is configured in `writ.toml` as both a percentage of total rules and an absolute floor: warn when unreviewed count exceeds `max(absolute_floor, percentage * total_rules)`. Defaults: 10% of total rules, floor of 5. At 80 rules, triggers at 8. At 500 rules, triggers at 50. Prevents the queue from growing unbounded at any corpus scale.

### 2c. Origin Context Storage -- SQLite

**Decision:** Origin context (what the AI was doing when it proposed the rule) is stored in SQLite, not Neo4j.

**Rationale:** Origin context is a write-once blob, read only during `writ review`. It has no relationships, no traversal use, no retrieval-path relevance. Putting it in Neo4j would add a node type that exists solely to hold text -- that's using graph structure for something that doesn't benefit from it.

**Location:** `~/.cache/writ/origin_context.db`

**Schema:**

```sql
CREATE TABLE origin_context (
    rule_id TEXT PRIMARY KEY,
    task_description TEXT NOT NULL,
    query_that_triggered TEXT,
    existing_rules_consulted TEXT,  -- JSON list of rule_ids
    created_at TEXT NOT NULL        -- ISO datetime
);
```

**Behavior:** Written once at AI rule proposal time. Read by `writ review <rule_id>`. If the file doesn't exist or the rule_id isn't in it, `writ review` shows "no origin context recorded" -- degraded but functional.

### 2d. Design Decision: No Rejected Rules Log

**Decision:** When a rule is rejected via `writ review --reject`, it is deleted from the graph. No dedicated rejection log is stored.

**Rationale:** The original evolution plan proposed storing rejections "for meta-learning about what kinds of rules the AI generates poorly." But no phase in this revised plan specifies a concrete consumer for that data -- no CLI command, no analysis step, no integrity check reads rejection history. Storing data for a consumer that doesn't exist is the same pattern as building plumbing for a filter that doesn't exist. Aggregate rejection counts are computable from `writ review --stats` without dedicated logging.

**Revisit condition:** If a future phase defines a concrete deliverable that reads rejection data (e.g., `writ review --patterns` showing rejection rate by domain/characteristic), add the logging then.

### 2e. Gate Validation Against Corpus

Before the gate is used for AI-proposed rules, validate it:

1. Run all 80 existing Phaselock rules through the structural gate. All 80 must pass (100% recall on known-good rules).
2. Fabricate 20 bad rule candidates: 5 vague (disqualifying language), 5 duplicative (near-copies of existing rules), 5 conflicting (contradicting existing rules with weaker evidence), 5 trivially obvious ("code should work correctly").
3. The gate must reject at least 16 of 20 (80%+ rejection rate on fabricated bad candidates).

**If the gate cannot hit these numbers:** Iterate on the checks (thresholds, keyword lists, similarity cutoffs) until it does. Do not proceed to Phase 3 until validation passes.

### Acceptance Criteria

- [ ] `structural_gate()` function consolidates all five checks
- [ ] `writ review` CLI operational with all subcommands
- [ ] SQLite origin context store created on first write
- [ ] Gate validates: 80/80 existing rules pass, 16+/20 fabricated bad rules rejected
- [ ] `writ validate` warns when unreviewed AI-provisional count exceeds threshold (percentage + absolute floor, configurable in writ.toml)
- [ ] New tests: gate acceptance, gate rejection per check type, review CLI operations, origin context read/write

---

## Phase 3: AI Rule Ingestion (Human-Gated)

**Prerequisite:** Phase 2 complete (structural gate validated).
**Deliverable:** Two-tier authority model, AI rule proposal workflow, hard preference rule in ranking.

**Honest naming:** During this phase, the system is "AI proposes rules that humans manually approve." It is not "AI writes provisional rules" -- that framing implies autonomy the system does not have. The system becomes "AI writes provisional rules" only after the frequency data from Phase 4 demonstrates that AI-proposed rules that pass the structural gate have an acceptable quality rate, and even then only for the coding domain.

### 3a. Authority Property

Add `authority` to the Rule node schema:

| Value | Meaning | Weight Ceiling |
|---|---|---|
| `human` | Human-authored. Default for all existing rules. | No ceiling. Full confidence range. |
| `ai-provisional` | AI-proposed, passed structural gate, awaiting human review. | Capped at `speculative` (0.3). |
| `ai-promoted` | AI-proposed, human-reviewed and approved via `writ review --promote`. | Can reach `peer-reviewed` (0.6). Only human review can set `battle-tested` or `production-validated`. |

**Migration:** All 80 existing rules receive `authority: human`. Additive property, no destructive changes.

### 3b. Hard Preference Rule in Ranking

**Decision (revised from original plan):** The confidence weight (0.099) is too small to create meaningful separation between human and AI rules. The difference between `speculative` (0.3) and `production-validated` (0.8) in final score is 0.099 x 0.5 = 0.0495 -- a 5% tiebreaker, not suppression.

**Mechanism:** A hard retrieval-time preference rule in `writ/retrieval/ranking.py`:

> When a human-authored rule and an AI-provisional rule are both in the final candidate set and their final scores are within `authority_preference_threshold`, the human rule ranks higher.

**Threshold determination (Phase 3 deliverable, not a guess):**

1. Run the existing pipeline against all 83 ground-truth queries.
2. Collect final scores of top-5 results per query.
3. Compute the gap between adjacent candidates (score[rank_n] - score[rank_n+1]) for all queries.
4. Set `authority_preference_threshold` to the **median adjacent-candidate gap**.
5. Store in `writ.toml` under `[ranking]` as `authority_preference_threshold`.

**Rationale:** At the median gap, the preference fires roughly half the time when a human and AI rule are adjacent -- meaningful but not dominant. The threshold is empirical, not arbitrary.

**Caveat:** This measurement assumes the current score distribution (all human-authored rules) will hold when AI-provisional rules enter the candidate set. AI-provisional rules have capped confidence, which compresses the lower end of the score distribution and likely narrows adjacent gaps. The Phase 3 measurement is a defensible starting point, not a permanent value. Re-measurement with a mixed-authority candidate set is an explicit Phase 4 deliverable (see 4e).

### 3c. AI-Provisional Rules Excluded from Graph Proximity Seeding

AI-provisional rules must not appear in the top-N used for graph proximity computation (Stage 5b of the pipeline). If an AI-provisional rule makes it into the top-3, it seeds proximity boosts for its neighbors, potentially pulling other low-quality rules up.

**Implementation:** After first-pass ranking (Stage 5a), filter AI-provisional rules from the top-3 before computing graph proximity. If fewer than 3 human/promoted rules are in the top results, use however many qualify -- do not backfill with AI-provisional rules.

### 3d. AI Rule Proposal Workflow

```
AI encounters novel situation
    |
    v
AI formulates candidate rule (standard schema)
    |
    v
Structural pre-filter (Phase 2a) -- accept/reject
    |
    +-- REJECT: reason logged, candidate discarded
    |
    +-- ACCEPT:
            |
            v
        Ingest with authority: ai-provisional, confidence: speculative
        Write origin context to SQLite
            |
            v
        Rule enters retrieval pipeline (capped weight, excluded from proximity seeding)
            |
            v
        Human reviews via `writ review` (Phase 2b)
            |
            +-- PROMOTE: authority: ai-promoted, confidence upgrade allowed
            +-- REJECT: deleted from graph
            +-- DOWNWEIGHT: confidence floor (0.1), retained for observation
```

### 3e. Enforcement Loop for Retrieval Consultation

For Claude Code: add a PreToolUse hook that calls `writ query` with the current task context before file writes. If Writ returns relevant rules, they are injected into context before the action proceeds. The hook is application-layer -- the AI cannot skip consultation.

For other agent frameworks: deferred. Build per-framework enforcement wrappers when a concrete second framework is adopted. Do not build speculative abstractions.

### Acceptance Criteria

- [ ] `authority` property on all Rule nodes; existing rules set to `human`
- [ ] Hard preference rule implemented in ranking.py; threshold empirically derived from 83-query gap analysis
- [ ] AI-provisional rules excluded from top-3 graph proximity seeding
- [ ] AI rule proposal workflow operational end-to-end
- [ ] `writ review` integration: proposed rules appear in queue, promote/reject/downweight functional
- [ ] Origin context written to SQLite on proposal
- [ ] PreToolUse hook for retrieval consultation (Claude Code)
- [ ] MRR@5 on existing 83-query set unchanged (no regression from authority changes)
- [ ] New tests: authority property validation, hard preference rule, proximity seeding exclusion, proposal workflow, hook integration

---

## Phase 4: Frequency Tracking

**Prerequisite:** Phase 3 complete.
**Deliverable:** Frequency properties on Rule nodes, coding-domain increment triggers, empirical confidence graduation.

### 4a. New Properties on Rule Node

| Property | Type | Description |
|---|---|---|
| `times_seen_positive` | integer (default 0) | Incremented on verified good outcome. |
| `times_seen_negative` | integer (default 0) | Incremented on verified bad outcome. |
| `last_seen` | datetime (nullable) | Timestamp of most recent retrieval. |

**Migration:** All 80 existing rules receive defaults (0, 0, null). Additive properties.

### 4b. Increment Triggers -- Coding Domain Only

| Signal | Trigger | Source |
|---|---|---|
| Positive | Rule retrieved, code written, static analysis passed (exit 0), human did not correct. | Hook exit codes. Mechanical. |
| Negative | Rule retrieved, code written, static analysis failed (exit 1), or human corrected/contradicted the rule. | Hook exit codes + human action. Mechanical. |

**Constraint:** Increment triggers are defined for the coding domain only. Non-coding domains are explicitly out of scope for this phase. See Phase 5 for the generalizability test.

### 4c. Empirical Confidence Graduation

**Decision (revised from original plan):** The original threshold of n=10 is statistically indefensible. At n=10, the 95% Wilson confidence interval on a 0.7 ratio is approximately [0.35, 0.93] -- you cannot distinguish a good rule from a coin flip.

**Revised mechanism:**

- **Graduation threshold:** n = 50 (where n = times_seen_positive + times_seen_negative).
- **Ratio requirement:** Positive ratio must exceed 0.75.
- **Statistical basis:** At n=50 and observed ratio 0.75, the Wilson 95% CI lower bound is approximately 0.61 -- meaningfully above chance.

**Behavior:**

| Condition | Confidence Source |
|---|---|
| n < 50 | Static enum value (human-assigned or default) |
| n >= 50 and positive_ratio >= 0.75 | Empirical ratio replaces static confidence |
| n >= 50 and positive_ratio < 0.75 | Static enum retained; rule flagged for human review by `writ validate` |

Rules that reach n=50 with ratio < 0.75 are not demoted automatically -- they are flagged. Human decides whether to downweight, edit, or leave as-is.

### 4d. Re-Measure Authority Preference Threshold

The `authority_preference_threshold` set in Phase 3 was derived from a homogeneous candidate set (all human-authored rules). With AI-provisional rules now in the pipeline at capped confidence, the score distribution has changed.

**Deliverable:** After Phase 3 has been live long enough to generate AI-provisional candidates in real query results (minimum: 10 AI-provisional rules that have appeared in at least one query's top-10), re-run the gap analysis:

1. Run all 83 ground-truth queries (plus any new queries added since Phase 3).
2. Collect final scores of top-5 results, noting authority per candidate.
3. Compute adjacent-candidate gaps for the mixed-authority set.
4. Compare the new median gap to the Phase 3 baseline.
5. Update `authority_preference_threshold` in `writ.toml` if the median has shifted.

**Why this matters:** If AI-provisional rules compress the lower end of the score distribution (likely, due to capped confidence), the Phase 3 threshold may be too wide -- firing the preference rule in situations where the score gap already provides sufficient separation. Re-tuning prevents the preference from being either too aggressive or decorative.

### 4e. Staleness via Frequency

New `writ validate` check: rules where `times_seen_positive + times_seen_negative == 0` over a rolling window (configurable, default 90 days) are candidates for archival or review. This supplements the existing `staleness_window` / `last_validated` check.

### Acceptance Criteria

- [ ] Frequency properties on all Rule nodes
- [ ] Increment triggers fire on hook exit codes (coding domain)
- [ ] Graduation at n=50, ratio > 0.75; below threshold uses static confidence
- [ ] `writ validate` flags: rules with ratio < 0.75 at n >= 50, rules with zero frequency over 90 days
- [ ] Frequency data visible in `writ review` output
- [ ] Authority preference threshold re-measured with mixed-authority candidate set; writ.toml updated if shifted
- [ ] No regression on retrieval quality (MRR@5 unchanged on 83-query set)
- [ ] New tests: increment triggers, graduation logic, staleness-by-frequency, validate warnings

---

## Phase 5: Domain Generalization (Conditional)

**Prerequisite:** Phases 1-4 proven with coding rules.
**Deliverable:** Conditional on generalizability test. If test fails, Phase 5 requires a fundamentally different feedback architecture per domain.

### 5a. Generalizability Test

Before building anything for non-coding domains, run the following test. Pick one non-coding domain (business policy recommended -- closest analog to mechanical outcome signals).

**The test passes only if ALL of the following hold:**

| Criterion | Requirement |
|---|---|
| Non-absence triggers | At least two automated positive triggers that are NOT "absence of correction within time window" |
| Frequency sufficiency | Projected trigger frequency sufficient to reach n=50 within 6 months per rule |
| Noise rate | Estimated false-positive/false-negative rate below 20% |
| Signal latency | Outcome signal arrives within 24 hours |
| Automation | Triggers can be evaluated without domain-expert involvement |

### 5b. Defined Failure Surface

| Failure Mode | Definition | Example |
|---|---|---|
| Absence-only signals | All positive triggers are "no correction received" | Business policy: user doesn't object within 5 minutes |
| Insufficient frequency | Triggers fire < 1x/week per rule, making n=50 unreachable in reasonable timeframe | Operational runbook rule applied during quarterly deploy |
| High noise ratio | Positive signals fire for bad outcomes (or negative for good) at > 20% rate | User accepts mediocre suggestion because correcting takes effort |
| Expert-dependent | Trigger evaluation requires domain expertise that can't be automated | "Was this business policy correctly applied?" requires a compliance officer |
| Latency mismatch | Outcome signal arrives days/weeks later, making real-time frequency tracking meaningless | Policy change takes effect next quarter; outcome visible in 90 days |

**If any criterion fails:** Phase 5 for that domain requires a different feedback architecture -- not a parameter tweak to the coding-domain mechanism. Document the failure, define what architecture would be needed, and scope it as a separate effort.

### 5c. What Changes (if test passes)

- New domains added to the graph. No schema changes needed after Phase 1 hardening.
- Domain-specific scope values added via the extensible enum.
- Domain-specific increment triggers defined per domain.
- Domain-specific conscience questions supplement the universal structural pre-filter.
- Enforcement wrappers built for non-Claude-Code agent frameworks (per-framework, not speculative).

### 5d. What Does Not Change

- The graph schema (already generalized in Phase 1).
- The retrieval pipeline (already domain-agnostic).
- The ranking formula (already uses generic weights).
- The two-tier authority model (universal).
- The hard preference rule (universal).

### Acceptance Criteria

- [ ] Generalizability test executed against one non-coding domain
- [ ] Results documented: pass/fail per criterion, with measurements
- [ ] If passed: domain added, triggers defined, rules ingested, frequency tracking operational
- [ ] If failed: failure documented, alternative architecture scoped, Phase 5 for that domain deferred
- [ ] Retrieval quality maintained across domains (domain filter isolates correctly)

---

## Execution Sequence Summary

| Phase | Honest Name | What It Actually Is | Prerequisite |
|---|---|---|---|
| 1 | Schema Hardening | Neo4j constraints, mandatory decoupling, extensible scope, enforcement docs | Schema audit (done) |
| 2 | Structural Pre-Filter + Review Queue | Existing checks consolidated into gate function. `writ review` for human judgment. SQLite origin context. Gate validated against corpus + fabricated bad rules. | Phase 1 |
| 3 | AI Rule Ingestion (Human-Gated) | Two-tier authority. AI proposes, pre-filter screens, human approves. Hard preference rule. Proximity seeding exclusion. Honestly named: "AI proposes, human approves." | Phase 2 |
| 4 | Frequency Tracking | Coding-domain increment triggers. Graduation at n=50, ratio > 0.75. Statistical validation before trusting empirical confidence. | Phase 3 |
| 5 | Domain Generalization (Conditional) | Passes only if automated non-absence triggers exist with sufficient frequency, low noise, short latency, and no expert dependency. Five defined failure modes. | Phases 1-4 proven |

---

## What Not to Build

| Item | Why Not |
|---|---|
| Scope renames (file->entity, module->component) | False generality. "Entity" means everything and therefore nothing. Extensible enum is cleaner. |
| Frequency tracking for non-coding domains (Phase 4) | Feedback signal problem is unsolved. Don't design for "absence of correction = positive." |
| Rejected rules log | No concrete consumer exists. Add logging when a specific deliverable needs the data. |
| Multi-user / multi-tenant | Three layers away. No concrete second user. |
| OpenClaw integration | Not prioritized. Agent framework enforcement wrappers built per-framework. |
| "AI learning mirrors human learning" as architectural framing | Metaphorical, not mechanistic. A `times_seen_positive` counter is Bayesian updating on a binary signal. Use the framing only if it produces architectural decisions that "feedback loop with frequency-weighted retrieval" would not. |

---

## Open Questions

| Question | Phase | Resolution Path |
|---|---|---|
| Novelty threshold (cosine < 0.85 for "novel enough") | Phase 2 | Measure similarity distribution of existing 80 rules to calibrate. |
| Vague language keyword list for specificity check | Phase 2 | Start with handbook Section 2.1 disqualifiers. Extend during validation. |
| Authority preference threshold (median gap) | Phase 3, re-measured Phase 4 | Empirical: run 83 queries with homogeneous set (Phase 3), then re-run with mixed-authority set after AI-provisional rules enter pipeline (Phase 4). |
| Graduation threshold fine-tuning (n=50 adequate?) | Phase 4 | After 6 months of frequency data, measure CI width at observed sample sizes. |
| Which non-coding domain to test first | Phase 5 | Business policy (closest to mechanical signals). Decision before Phase 5 starts. |
| What architecture replaces frequency tracking for domains that fail the generalizability test | Phase 5+ | Scoped as separate effort if/when test fails. Not pre-designed. |

---

## Configuration Additions to writ.toml

```toml
[authority]
preference_threshold = 0.0  # Placeholder -- derived empirically in Phase 3
ai_provisional_confidence_ceiling = "speculative"
ai_promoted_confidence_ceiling = "peer-reviewed"

[frequency]
graduation_threshold = 50
graduation_ratio_minimum = 0.75
zero_frequency_window_days = 90

[review]
unreviewed_warning_percentage = 0.10
unreviewed_warning_floor = 5

[gate]
novelty_threshold = 0.85
redundancy_threshold = 0.95

[origin_context]
db_path = "~/.cache/writ/origin_context.db"
```

---

## Appendix: Design Decisions Log

Decisions made during adversarial review, recorded for future reference.

| Decision | Options Considered | Chosen | Rationale |
|---|---|---|---|
| Scope generalization | (a) Extensible enum per domain, (b) Rename to vague abstractions | (a) | "Entity" means everything and nothing. Domain-specific values are honest. |
| Origin context storage | (a) Neo4j OriginContext node, (b) SQLite table | (b) | Write-once blob with no relationships. Graph structure provides no benefit. |
| Authority enforcement in ranking | (a) Weight-based via confidence, (b) Hard preference rule at retrieval time | (b) | 0.099 weight creates 5% tiebreaker, not suppression. Hard rule makes invariant explicit. |
| Rejected rules logging | (a) Store in flat file/SQLite for meta-learning, (b) Delete and don't log | (b) | No concrete consumer. Aggregate counts from review --stats suffice. Add logging when a deliverable needs it. |
| Graduation threshold | (a) n=10, (b) n=30, (c) n=50 | (c) | At n=10, Wilson 95% CI is [0.35, 0.93] for 0.7 ratio. At n=50, CI is [0.61, 0.86]. n=50 is the minimum for statistical meaning. |
| Gate naming | (a) "Judgment gate", (b) "Structural pre-filter" | (b) | The gate catches structural failures. It cannot evaluate correctness. Naming it "judgment" implies capabilities it doesn't have. |
| Phase ordering | (a) Original: memory -> frequency -> gate, (b) Revised: gate design -> memory -> frequency | (b) | Resolves bootstrapping paradox. You need the filter defined before building the plumbing it filters. |
| Preference threshold | (a) Arbitrary (0.05), (b) Empirical (median adjacent-candidate gap) | (b) | Data exists in 83 ground-truth queries. Don't guess when you can measure. |
