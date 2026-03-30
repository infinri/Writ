# Evolution Reference

Slim reference for still-relevant decisions from the original evolution plan
(Dwarf in the Glass, approved 2026-03-23). Phases 1-4 are complete and implemented.

---

## Phase 5: Domain Generalization (Conditional)

**Status:** Not started. Proceeds only if generalizability test passes.

### Test Criteria (ALL must hold)

| Criterion | Requirement |
|---|---|
| Non-absence triggers | At least two automated positive triggers that are NOT "absence of correction within time window" |
| Frequency sufficiency | Projected trigger frequency sufficient to reach n=50 within 6 months per rule |
| Noise rate | Estimated false-positive/false-negative rate below 20% |
| Signal latency | Outcome signal arrives within 24 hours |
| Automation | Triggers can be evaluated without domain-expert involvement |

### Defined Failure Modes

| Failure | Example |
|---|---|
| Absence-only signals | "User doesn't object within 5 minutes" as positive trigger |
| Insufficient frequency | Rule applied only during quarterly deploys |
| High noise ratio | User accepts mediocre suggestion because correcting takes effort |
| Expert-dependent | Evaluating correctness requires a compliance officer |
| Latency mismatch | Outcome visible 90 days later |

If any criterion fails, that domain requires a different feedback architecture -- not a parameter tweak.

### What changes if test passes

- New domains added to graph (no schema changes needed)
- Domain-specific scope values via extensible enum
- Domain-specific increment triggers
- Per-framework enforcement wrappers (built when adopted, not speculatively)

---

## Design Decisions Log

| Decision | Chosen | Rationale |
|---|---|---|
| Scope generalization | Extensible enum per domain | "Entity" means nothing. Domain-specific values are honest. |
| Origin context storage | SQLite, not Neo4j | Write-once blob with no relationships. Graph provides no benefit. |
| Authority enforcement | Hard preference rule, not weight-based | 0.099 weight creates 5% tiebreaker, not suppression. Hard rule makes invariant explicit. |
| Rejected rules logging | Delete, don't log | No concrete consumer. Add logging when a deliverable needs the data. |
| Graduation threshold | n=50 | At n=10, Wilson 95% CI is [0.35, 0.93]. At n=50, CI is [0.61, 0.86]. Minimum for statistical meaning. |
| Gate naming | "Structural pre-filter" | Gate catches structural failures, not correctness. Honest naming. |
| Phase ordering | Gate design before memory plumbing | Resolves bootstrapping paradox -- define the filter before the plumbing it filters. |
| Preference threshold | Empirical median adjacent-candidate gap | Data exists in 83 queries. Don't guess when you can measure. |

---

## What Not to Build

| Item | Why |
|---|---|
| Scope renames (file->entity, module->component) | False generality |
| Frequency tracking for non-coding domains (before Phase 5 test) | Feedback signal problem unsolved |
| Multi-user / multi-tenant | No concrete second user |
| "AI learning mirrors human learning" framing | Metaphorical, not mechanistic |
