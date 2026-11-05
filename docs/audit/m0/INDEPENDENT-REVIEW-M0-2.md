# M0 independent source review 2: record and disposition

**Received:** 2026-11-05 from the project owner. **Reviewed revision:** `61b0ecad73d167c7e6075d0d6a4e0d3171ada8ff`. **Type (as stated):** independent AI-assisted source, test and fixture inspection; not a human security certification, live-host validation, penetration test or independent execution of the suite. CI run 42112799616 checked.

**Verdicts (reviewer):** all 26 golden expected outcomes provisionally AGREE with the M0 reference semantics. Security boundary and M0 signoff: NOT APPROVED YET. Owner instruction: the 26 outcomes are provisionally accepted as consistent with M0 reference semantics; this is not production certification or M0 approval.

**Repository status:** formal `review_status` stays PENDING INDEPENDENT REVIEW on all 26 cases until a named reviewer records verdicts through the controlled process. This file records the assessment only.

## Remediation assessment

| Finding | Reviewer disposition | Repository status |
|---|---|---|
| SR-01 ingress seal | Accepted conditionally for the reference harness; production key provisioning, rotation, registration and host-process authentication open. A key holder can seal any schema-valid request | Accepted for harness (owner); production limits recorded |
| SR-02 capability set | Accepted, subject to SR-10 | Accepted for harness |
| SR-03 deadline | Accepted; production needs trusted clocks and effect-time checks on real adapters | Accepted for harness |
| SR-04 subagent approval | Design approved by owner; reference behavior agrees | Accepted for harness |
| SR-07 permit replay | Accepted conditionally for a single local ledger and controlled key; no distributed, network-filesystem or power-loss claims | Accepted for harness; E-008 limitations stand |
| SR-08 canonical trailing segment | Accepted | Accepted for harness |
| SR-09 case 22 fixture | Accepted | Accepted for harness |

## New findings

| ID | Finding | Priority | Disposition |
|---|---|---|---|
| SR-10 | Effect adapter compares only digest, resource and tenant; a valid unused permit could be consumed for a request with a different principal, kind, capabilities or other fields | high, before parity | Fix in harness |
| SR-11 | Idempotent replay returns an earlier receipt keyed by tenant, key and digest without checking principal, project, capabilities, request or the supplied grant | secondary (attribution; no new dispatch) | Fix in harness |

## Open M0 acceptance requirements (reviewer)

SR-05 project-scoped evidence; SR-06 / M0-01e live host; M0-01d human rule review; formal named review of 26 cases; M0 go/no-go. Production hardening (key registration and rotation, trusted host boundary, policy signatures, trusted clocks, OS effect mediation, filesystem TOCTOU, crash reconciliation, distributed storage) is M3/M5 roadmap, not M0 code.
