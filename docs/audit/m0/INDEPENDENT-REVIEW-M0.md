# M0 independent source review: record and disposition

**Received:** 2026-11-05 from the project owner. **Reviewed revision:** `89f8caa134a589905d314296f53fe1d04ebe04ec`. **Type (as stated by the reviewer):** AI-assisted independent source and fixture review; not a human security audit or penetration test. The reviewer did not execute tests; it checked CI run 42099017041.

**Overall verdict (reviewer):** M0 security boundary NOT YET APPROVED.

This file records the review. It does not change any golden case's `review_status`: the reviewer asked that all cases stay pending official review, and cases 01 and 08 need an explicit human policy decision.

## Per-case technical assessment

| # | Case | Reviewer assessment | Repository status |
|---|---|---|---|
| 01 | workspace-file-write | NEEDS POLICY DECISION (permit has no role restriction) | PENDING INDEPENDENT REVIEW |
| 02-07, 09, 11-19, 21 | see review package | provisionally AGREE on the recorded outcome; not production certification | PENDING INDEPENDENT REVIEW |
| 08 | operator-process-still-requires-approval | NEEDS POLICY DECISION (accept require_approval > permit explicitly) | PENDING INDEPENDENT REVIEW |
| 10 | untrusted-issuer | CONDITIONAL on SR-01 | PENDING INDEPENDENT REVIEW |
| 20 | piggyback-approval-capability | CONDITIONAL on SR-02 | PENDING INDEPENDENT REVIEW |
| 22 | attributes-cannot-grant | CONDITIONAL: fixture patch contained no malicious attributes | PENDING INDEPENDENT REVIEW |
| 23 | project-memory-write | CONDITIONAL on SR-05 | PENDING INDEPENDENT REVIEW |
| 24 | workspace-outside-project-prefix-default-deny | AGREE; case id misleading, rename after review links reconciled | PENDING INDEPENDENT REVIEW |

Case-specific notes from the reviewer that call for extra coverage (case/alias secrets paths, competing permit in golden set, encoded traversal, use-time path substitution) are tracked as follow-ups, not as defects.

## Findings and disposition

| ID | Finding (reviewer) | Severity | Disposition |
|---|---|---|---|
| SR-01 | Broker accepts a client-crafted ActionRequest; kernel trusts the `verified_by` string. Principal provenance not enforced end to end | P0 | Fix in M0 harness (authenticated broker ingress) |
| SR-02 | Approval request id and grant omit `requested_capabilities`; capability substitution possible under one grant | P0 | Fix in M0 harness |
| SR-03 | `deadline_at` never enforced at decision or use time | P0 | Fix in M0 harness |
| SR-04 | No grant inheritance is correct; needs human signoff; future delegation must be explicit | design | Open: human signoff |
| SR-05 | Evidence/source catalog scoped by tenant only, not project | P0 before multi-project claims | Open |
| SR-06 | Host crash fail-closed semantics unverified | P0 before enforcement claims | Open: isolated live host |
| SR-07 | Signed broker permit can be replayed directly at the effect adapter within its TTL | P0 | Fix in M0 harness |
| SR-08 | Canonical id with trailing empty segment accepted by the kernel | P1 | Fix in M0 harness |
| Case 22 | Golden patch lost its attributes (generator merged two `action` keys; the second replaced the first) | fixture defect | Repair fixture input; expected outcome unchanged |
