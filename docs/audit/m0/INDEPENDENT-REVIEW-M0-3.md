# M0 independent source review 3: record and disposition

**Received:** 2026-11-05 from the project owner. **Reviewed revision:** `957e32f21ca337392f6de050af4474fa6ecd3b37`. **Type (as stated):** AI-assisted independent static source and contract review; not a human security audit, penetration test or M0 acceptance. The reviewer did not execute the suite; it checked CI run 42123472509. No human reviewed this record.

## Dispositions

| Item | Reviewer verdict | Repository status |
|---|---|---|
| SR-10 permit binds whole request | ACCEPTED for M0 reference harness. Production needs trusted effect adapters, stable cross-language canonical encoding, authentic ingress and signing keys, real OS/network mediation | DONE (harness) |
| SR-11 receipt attribution | ACCEPTED for M0 reference harness. Not a distributed idempotency service, tamper-proof ledger, power-loss recovery, or proof that external providers have native idempotency | DONE (harness) |
| SR-05 evidence/source project isolation | ACCEPTED for the evidence and source lookup contract; adjacent artifact/signoff gap is SR-12 | DONE (harness) |
| 26 golden outcomes | Technical AGREE for the reference semantics; not a named human verdict | All remain PENDING INDEPENDENT REVIEW |

## New finding

**SR-12 (artifact and human-signoff project binding).** Artifact has no `project_id`; `evaluate()` validates the artifact schema but never checks artifact tenant, task or project against the task; `SignoffGrant` binds tenant and artifact digest only. A schema-valid artifact from another task or same-tenant project could receive a passing evaluation with in-scope evidence, and a signoff is not project- or task-scoped. Inferred from code by the reviewer; to be demonstrated by red-first tests.

## Design note (not an M0 code change)

Composite requests evaluate one canonical resource against the union of `action.kind` and `requested_capabilities`. Before production multi-effect actions, bind each sub-effect's target, digest and permission explicitly, or prohibit multiple independent effects per request. Tracked as roadmap item RM-01 (M1/M3 architecture); golden cases 19-21 unchanged.

## Documentation corrections requested

M0-05a and the reviewer verdict table must cover all 26 cases (were 24).

## M0 gate (reviewer)

NOT APPROVED / NOT COMPLETE: named human verdicts on 26 cases; human review of 288 rules (M0-01d); isolated live host hook-failure verification (SR-06 / M0-01e); owner go/no-go. SR-12 resolved or risk-accepted before M1 parity relies on project-scoped evaluation.
