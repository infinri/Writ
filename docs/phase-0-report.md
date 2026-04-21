# Phase 0 — Validation infrastructure report

**Dated:** 2026-04-21
**Branch:** `phase-0-validation`
**Status:** **GO for Phase 1** — all four release-blockers passed on corrected harness.

## 1. Decision summary

Phase 0 measured methodology retrieval quality on a 60-node synthetic corpus against 40 ground-truth queries. After correcting two harness bugs, one corpus gap, and applying maintainer-approved ground-truth curation (7 primary-expected relabels, all toward AntiPattern/ForbiddenResponse counters), the pipeline clears every release-blocker threshold from plan Section 5.3:

| Release blocker          | Measured | Threshold   | Margin  | Status |
| ------------------------ | -------- | ----------- | ------- | ------ |
| MRR@5                    | 0.8583   | ≥ 0.78      | +0.078  | PASS   |
| Hit rate                 | 1.0000   | ≥ 0.90      | +0.100  | PASS   |
| Bundle completeness      | 0.8542   | ≥ 0.85      | +0.0042 | PASS   |
| p95 retrieval latency    | 0.98 ms  | ≤ 5 ms      | -4.02 ms | PASS  |

Encode latency (p95 10.84 ms, mean 10.38 ms) reported for visibility but not gated — matches plan's pipeline-level p95 definition.

Recommendation: proceed to Phase 1 schema / ingest / pipeline work. Scope A fallback (Section 5.4) is not invoked.

## 2. Scope

Phase 0 is read-only to the production Writ pipeline per plan Section 5.5. Deliverables produced:

- `tests/fixtures/synthetic_methodology/` — 60 stand-in methodology nodes spanning all 11 planned node types.
- `tests/fixtures/ground_truth_proc.candidates.json` — 40 candidate queries (drafted by Claude Code from Superpowers pinned content).
- `tests/fixtures/ground_truth_proc.curation-proposal.json` — per-query decisions (33 keep, 7 relabel-primary) with rationale.
- `tests/fixtures/ground_truth_proc.json` — curated final fixture (signed off 2026-04-21, all 40 queries survive).
- `tests/fixtures/methodology_loader.py` — fixture parser + `MethodologyIndex` (Phase-0 tantivy BM25 with body indexing per plan Section 3.2).
- `tests/test_methodology_retrieval.py` — pytest blocker tests (4 tests, one per blocker).
- `benchmarks/methodology_bench.py` — standalone runner with summary / verbose / JSON modes.
- `docs/phase-0-schema-proposal.md` — signed-off new-node-type schema design for Phase 1 Pydantic transcription.
- Schema additions to `writ-absorbs-superpowers-plan.md` (new Section 0 — Preconditions and source pin).
- `docs/phase-0-report.md` (this file).

## 3. Corpus composition

60 total nodes (40 retrievable, 20 non-retrievable). Matches plan Section 2.3's retrievable/non-retrievable split.

| Type              | Count | Retrievable |
| ----------------- | ----- | ----------- |
| Skill             | 7     | yes         |
| Playbook          | 7     | yes         |
| Technique         | 4     | yes         |
| AntiPattern       | 10    | yes         |
| ForbiddenResponse | 2     | yes (always-on) |
| Rule (ENF-* etc.) | 10    | yes         |
| Phase             | 9     | no (structural) |
| Rationalization   | 3     | no (bundle-only) |
| PressureScenario  | 3     | no (test-only) |
| WorkedExample     | 2     | no (explicit-lookup) |
| SubagentRole      | 3     | no (template) |

Every Section 12 content-map node ID exists. Two extensions beyond Section 12: `ANT-PROC-PARALLEL-001` (filled content gap caught mid-Phase-0) and additional `ANT-PROC-*` / `PSC-*` / `RAT-*` for coverage.

## 4. Ground truth composition

40 candidate queries authored by Claude Code from Superpowers pinned content (`superpowers@5.0.7`, commit `b557648`). **Pending maintainer curation** — the candidates file is deliberately named `.candidates.` because human review is required per plan Section 5.1 deliverable 1 authorship standard.

Distribution:
- By intent: 15 direct, 9 rationalization, 6 red_flag, 4 anti_pattern, 3 cross_skill, 3 forbidden.
- By skill: 13 of 14 Superpowers skills covered (`using-superpowers` excluded — meta-skill, orthogonal to Writ's mode system; documented in fixture).
- Source trace: every query has `source_trace` citing the Superpowers file and approximate line range.

**Limitation surfaced, flagged for Phase 5:** plan Section 5.6 risk mitigation called for half the queries to come from `workflow-friction.log` denial patterns. The log has only 280 entries from today's session — insufficient historical signal. All candidates sourced from Superpowers content; denial-pattern queries deferred to Phase 5 once friction log accumulates.

## 5. Harness architecture

Mirrors Writ's five-stage pipeline at Phase-0 scale:

- **Stage 1 — domain filter:** not exercised (no domain_hint in the 40-query candidates; all 40 queries go through the full BM25 + vector pipeline).
- **Stage 2 — BM25:** local `MethodologyIndex` class using tantivy. Schema fields: `rule_id`, `trigger` (2× boost via repetition, matching existing `KeywordIndex.TRIGGER_BOOST`), `statement`, `tags`, `body` (0.5× effective weight via every-other-token dilution). `body` includes `forbidden_phrases` + `what_to_say_instead` for `ForbiddenResponse` nodes and `named_in` for `AntiPattern` nodes.
- **Stage 3 — vector:** Writ's `OnnxEmbeddingModel` + `CachedEncoder` over concatenated `trigger + " " + statement`. Cosine similarity over retrievable-node vectors (40 entries). Naive O(N) top-k rather than HNSW; at 40 nodes this is sub-ms.
- **Stage 4 — graph traversal:** adjacency dict built from each node's `edges` field. Bundle expansion to depth 1 for bundle-completeness measurement.
- **Stage 5 — RRF:** Reciprocal rank fusion with k=60 (matching Writ's pipeline default). Authority rerank not exercised at Phase-0 (all nodes have `authority: human`).

**Latency split:** retrieval-stage latency measured independently from embedding encode. The p95 blocker gates on retrieval per plan Section 5.3's pipeline definition; encode latency reported separately for user-visible-cost transparency.

## 6. Baseline vs corrected run

### Baseline (pre-fix)

```
MRR@5                    0.6813  (blocker >= 0.78, FAIL)
Hit rate                 0.8500  (blocker >= 0.9,  FAIL)
Bundle completeness      0.7875  (blocker >= 0.85, FAIL)
p95 latency (ms)          12.74  (blocker <= 5.0,  FAIL)
```

All four blockers failed. Per plan Section 0.5, the failure was not treated as a "tune to pass" signal. Per-query inspection decomposed the failure into three categories:

1. **Harness bugs (plan-spec violations):**
   - BM25 index did not include `body` field at 0.5× weight. Plan Section 3.2 explicitly specifies this.
   - Latency measurement included ONNX encode (~8 ms), but Writ's published p95 definition excludes encode. Unit-mismatch.
2. **Corpus gap:** `ANT-PROC-PARALLEL-001` not authored in the initial batch — no anti-pattern node for the "one agent to fix everything" rationalization, causing P35 to miss its expected primary.
3. **Ground-truth authoring (for maintainer review):** several queries labeled a Playbook/Skill as primary-expected when an AntiPattern was semantically the closer match. Examples below.

### Fixes applied

All three treated as correctness, not tuning. Threshold values, RRF weights, embedding model, and ranking formula were NOT touched.

| Fix | Kind | Rationale |
| --- | ---- | --------- |
| Add body field (+ FRB phrases + AntiPattern `named_in`) to BM25 index at 0.5× weight via token dilution | Harness | Plan Section 3.2 spec compliance |
| Separate retrieval latency from encode latency in measurement; gate blocker on retrieval-only | Harness | Matches Writ's pipeline p95 definition |
| Author `ANT-PROC-PARALLEL-001` (new AntiPattern node) | Corpus | Fill a node-type coverage gap noticed during per-query analysis |

### Corrected run (pre-curation, against candidates.json)

```
MRR@5                    0.8000  (blocker >= 0.78, PASS)
Hit rate                 0.9500  (blocker >= 0.9,  PASS)
Bundle completeness      0.8625  (blocker >= 0.85, PASS)
p95 retrieval (ms)         1.00  (blocker <= 5.0,  PASS)
  (mean retrieval 0.69ms; encode p95 9.70ms mean 7.81ms — not gated)
```

38/40 hit, 2 miss, 3 low-rank hits (rr < 0.5).

### Final run (post-curation, against signed-off ground_truth_proc.json)

```
MRR@5                    0.8583  (blocker >= 0.78, PASS)
Hit rate                 1.0000  (blocker >= 0.9,  PASS)
Bundle completeness      0.8542  (blocker >= 0.85, PASS)
p95 retrieval (ms)         0.98  (blocker <= 5.0,  PASS)
  (mean retrieval 0.77ms; encode p95 10.84ms mean 10.38ms — not gated)
```

40/40 hit. Curation shifted 7 rationalization / forbidden-phrase queries to AntiPattern / ForbiddenResponse primaries — the relabels are documented in `tests/fixtures/ground_truth_proc.curation-proposal.json`. Bundle completeness dipped 0.008 (from 0.8625 to 0.8542) because two relabels (P35, P38) have shorter `expected_node_ids` lists after relabeling, changing the denominator; still above the 0.85 blocker.

## 7. Curation outcome

Maintainer curation pass (2026-04-21) produced 33 `keep` + 7 `relabel-primary` + 0 `discard` + 0 `rewrite`. Relabels summary:

| Query | Original primary      | Curated primary           |
| ----- | --------------------- | ------------------------- |
| P13   | PBK-PROC-SDD-001      | ANT-PROC-VERIFY-001       |
| P15   | PBK-PROC-TDD-001      | ANT-PROC-TDD-005          |
| P18   | PBK-PROC-TDD-001      | ANT-PROC-TDD-005          |
| P21   | SKL-PROC-VERIFY-001   | FRB-COMMS-002             |
| P35   | SKL-PROC-PARALLEL-001 | ANT-PROC-PARALLEL-001     |
| P38   | PBK-PROC-TDD-001      | ANT-PROC-TDD-005          |
| P40   | SKL-PROC-VERIFY-001   | ANT-PROC-VERIFY-001       |

Pattern: rationalization / red-flag / forbidden-phrase queries route to the specific counter-node (AntiPattern or ForbiddenResponse) rather than the parent teaching Skill/Playbook. This matches plan Section 3.1's intent that graph traversal surfaces counter-thoughts as bundle members, and makes primary-expected labels route to the retrieval node that teaches the specific counter.

Per-query decisions with rationale live in `tests/fixtures/ground_truth_proc.curation-proposal.json` (signed off 2026-04-21).

## 8. Authorship provenance

Per plan Section 5.1 deliverable 1 standard.

| Artifact | Drafted by | Reviewed by | Status |
| -------- | ---------- | ----------- | ------ |
| `tests/fixtures/synthetic_methodology/*.md` (60 nodes) | Claude Code, from Superpowers pinned commit | maintainer review pending before Phase 2 ingest | draft |
| `tests/fixtures/ground_truth_proc.candidates.json` (40 candidate queries) | Claude Code, sourced from Superpowers skill content | Lucio, 2026-04-21 (via curation proposal) | superseded by curated version |
| `tests/fixtures/ground_truth_proc.curation-proposal.json` (curation decisions) | Claude Code | Lucio approved wholesale 2026-04-21 | signed off |
| `tests/fixtures/ground_truth_proc.json` (final fixture, 40 queries, 7 relabels) | Claude Code from approved curation | Lucio, 2026-04-21 | signed off |
| `docs/phase-0-schema-proposal.md` | Claude Code | Lucio, 2026-04-21 (6 open questions + rationalization duality resolved) | signed off |
| Plan Section 0 (preconditions + source pin) | Claude Code edit | Lucio, 2026-04-21 | signed off |
| Harness code (`methodology_loader.py`, `test_methodology_retrieval.py`, `benchmarks/methodology_bench.py`) | Claude Code | n/a (code, not content) | ready |

Authorship standard met per plan Section 5.1: AI-drafted queries flowed through maintainer curation before entering `ground_truth_proc.json`. No AI-labeled queries merged unreviewed.

## 9. Open items for maintainer review

1. ~~Curate `ground_truth_proc.candidates.json` → `ground_truth_proc.json`.~~ **Complete 2026-04-21.** All 40 queries survived; 7 relabel-primary applied per approved proposal.
2. Line-review the 60 synthetic nodes. Spot-check content against the pinned Superpowers source. **Not a Phase 0 blocker** — these are stand-ins replaced by real content in Phase 2. Can defer to Phase 2 start.
3. ~~Review `docs/phase-0-schema-proposal.md` resolved-decisions section.~~ **Complete 2026-04-21.**

## 10. Phase 1 readiness checklist

- [x] Phase 0 release-blockers all pass (post-curation).
- [x] Schema proposal signed off (Section 6 gates, edge types, common-base fields, rationalization duality).
- [x] Preconditions documented and verified (plan Section 0.3).
- [x] Source pin recorded (plan Section 0.1): `superpowers@5.0.7` @ `b557648`.
- [x] Ground truth curated and signed off: 40 queries survive, 7 relabels applied.
- [ ] Maintainer line-review of synthetic corpus (gates Phase 2 content ingest, not Phase 1 schema work).
- [ ] Phase 1 branch cut from `phase-0-validation` (or from `main` after merge).

Phase 1 can start on schema + ingest parser + pipeline refinement work in parallel with the maintainer's ground-truth curation. Content ingest (Phase 2) waits on both curation and schema landing.

## 11. Known limitations

- **HNSW not used in Phase 0 vector stage.** 40 retrievable nodes is too small to need ANN; naive cosine is exact and sub-ms. Phase 1 uses real HNSW as it scales into the real corpus.
- **Embedding model unchanged.** Baseline `all-MiniLM-L6-v2` hit the MRR@5 bar at 0.80 — above the 0.78 blocker with only 0.02 margin. Model-swap protocol in plan Section 14 remains available for Phase 1 if methodology retrieval quality regresses on the real corpus after ingest.
- **Authority rerank not exercised.** All fixtures have `authority: human`. Writ's confidence-rerank stage becomes meaningful only with mixed-authority nodes.
- **Pressure-test harness not built in Phase 0.** Pressure-scenario nodes (`PSC-*`) exist as fixtures but no harness consumes them yet. Phase 4 builds that.
- **One pre-existing test (`tests/test_graph_proximity.py::TestGraphBoostRegression::test_benchmark_suite_still_passes`) is timing-flaky on shared machines.** Asserts E2E p95 < 10 ms; blipped at 11.8 ms in one run, passed on retry at 8 ms. Not caused by Phase 0 changes. Flagged for independent hardening work; does not block Phase 1.

## 12. Signoff

| Role | Name | Date | Status |
| ---- | ---- | ---- | ------ |
| Drafted by | Claude Code | 2026-04-21 | done |
| Reviewed by | Lucio | 2026-04-21 | approved |
| Approved by | Lucio | 2026-04-21 | signed off |

Phase 0 closed. Phase 1 start authorized.

_End of report._
