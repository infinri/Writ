# M1-02 amendment 4: refusal mapping and transition support (D-020)

**Date:** 2026-11-07. **Basis:** owner decision [D-020](../../docs/implementation/DECISION-LOG.md) after the M1-03 and M1-04 differential findings. All cases remain **PENDING INDEPENDENT REVIEW**; no expectation was changed to make an implementation pass.

## Changed expected results

| Case | Before | After | Reason |
|---|---|---|---|
| external-refusal `ext-revision-conflict` | `REVISION_CONFLICT` (pass-through) | `PROMOTION_REFUSED` | D-020: trusted-state revision conflicts can reveal protected workflow state; consistent with `STATE_REVISION_CONFLICT` |

## Added cases (16)

| Set | Added | Ids |
|---|---|---|
| external-refusal | 7 | `REQUEST_SCHEMA_INVALID` and `DEADLINE_EXCEEDED` pass through; `STATE_REVISION_CONFLICT`, `RESOURCE_SCOPE_MISMATCH`, `PRINCIPAL_UNVERIFIED`, `UNSUPPORTED_CAPABILITY`, `UNKNOWN_CAPABILITY` map to `PROMOTION_REFUSED` (negative disclosure tests) |
| transition-decisions | 6 | tx-56..tx-61: no transition support (unknown capability; other capabilities still allowed), known but unsupported (`UNSUPPORTED_CAPABILITY`; other capabilities allowed), incomplete protection rejected at load, valid protection with no rule (default deny) |
| broker-scenarios | 3 | b-44..b-46: permit replay, idempotent retry and reconciliation-required across a simulated `restart_broker` |

New bundle variants: `m1-transition-no-transition-support`, `m1-transition-transition-known-unsupported` (both schema-valid), `m1-transition-protection-incomplete` (schema-invalid by design).

Totals: canonical-vector 1, strict-json 37, golden 26, acceptance-binding 97, transition-decisions 62, transition-scenarios 66, external-refusal 63, broker-scenarios 46; **398 cases**.

## Differences resolved

| Finding (E-022) | Rust before | Go before | Resolution |
|---|---|---|---|
| `REQUEST_SCHEMA_INVALID`, `DEADLINE_EXCEEDED` external | pass-through | `PROMOTION_REFUSED` | pass-through (Go corrected) |
| `STATE_REVISION_CONFLICT` external | pass-through | `PROMOTION_REFUSED` | `PROMOTION_REFUSED` (Rust corrected) |
| `REVISION_CONFLICT` external | pass-through | pass-through | `PROMOTION_REFUSED` (both corrected; changed expectation above) |
| Transition under a bundle with no protection and no transition statements | `unavailable` / `PROTECTION_UNAVAILABLE` | `unavailable` / `POLICY_BUNDLE_REJECTED` | `deny` / `UNSUPPORTED_CAPABILITY` (both corrected) |
| `restart_broker` | absent | absent | added to both |
