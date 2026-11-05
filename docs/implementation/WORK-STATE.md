# Writ V2 work state

**Last updated:** 2026-11-05 · **Branch:** v2/architecture-rebuild (pushed through `d3f722d`) · **Milestone:** M0, **NOT complete**

## Current

- **Phase:** security corrections from the independent source review ([record](../audit/m0/INDEPENDENT-REVIEW-M0.md)).
- **Active task:** none in progress. SR-01, SR-02, SR-03, SR-07, SR-08, SR-09 fixed and IN REVIEW; awaiting review of the fixes and regression tests.
- **Last commits:** `f4489c9` SR-09, `94d3701` SR-01, `2800da3` SR-07, `f8ad085` SR-02, `4947489` SR-03, `64a5b01` SR-08, `1c53247` review record; plus the docs commit following this file.

## Completed and verified

| Area | Result |
|---|---|
| Continuity | TASKS, DECISION-LOG (D-001..D-010), TEST-EVIDENCE (E-001..E-006), SESSION-HANDOFF, CLAUDE.md |
| V1 audit | Pinned manifest, 52 hooks, 288-rule first pass, test inventory, retrieval golden export, unmediated tools, 21 ported regressions |
| Contracts and domains | 17 schemas; canonical digest; cross-domain evaluation with negatives |
| Kernel oracle | 26 golden cases (all PENDING INDEPENDENT REVIEW); deadlines; strict canonical ids |
| Broker | Sealed ingress (D-010), capability-set-bound grants, single-use permits, idempotency, receipts |
| CI | `d3f722d`: 256 passed, audit and docs checks pass (E-007) |

## Open M0 acceptance requirements

| Item | Needs |
|---|---|
| Review of SR-01/02/03/07/08/09 fixes and tests | Reviewer |
| Golden cases 01 and 08 | Explicit human policy decision |
| All 26 golden cases | Independent verdicts (none self-approved) |
| SR-04 subagent grant isolation | Human signoff |
| SR-05 project-scoped evidence | Contract and evaluator change plus negative tests |
| SR-06 hook crash fail-closed | Isolated live Claude Code host |
| M0-01d rule semantic review, M0-01e live hook posture | Human reviewer; isolated host |
| M0-05c go/no-go | All of the above |

## Known blockers

- No implementation work until the security fixes are independently reviewed (instruction).
- Rust and Go toolchains absent (M1 not started, by instruction).
- Pages deploy blocked by repository environment protection (pre-existing).

## Relevant files

`m0/writ_m0/{ingress,broker,decision,hosts,runner}.py`, `m0/tests/test_{broker,decision,independence,v1_regressions}.py`, `docs/contracts/approval-grant.schema.json`, `docs/contracts/fixtures/decisions/m0-golden.json`, `docs/audit/m0/{INDEPENDENT-REVIEW-M0,GOLDEN-DECISION-REVIEW}.md`.

## Last tests

E-007: CI 256 passed at `d3f722d`; audit and docs checks pass.

## Next exact action

Wait for the independent review of the GitHub code and regression tests for SR-01/02/03/07/08/09 (all IN REVIEW). Start no implementation work, SR-05 included, until that review is in. Propose the M0 go/no-go only after the fixes are reviewed and the open items above are resolved or explicitly risk-accepted.
