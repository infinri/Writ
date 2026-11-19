# Writ V2 work state

**Last updated:** 2026-11-19 (M6 slices 5A through 5E implemented for disposable stores and test authorities under D-054 through D-058, M3 and M4 accepted for scoped development) · **Branch:** v2/architecture-rebuild · **Milestones:** M5 and M6 in progress. M5 VM and isolated containment CI gates are open. Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md) section "Current recovery state".

## Current

- D-058 delivers [headless custom-contract administration](../../m6/management/README.md#run-custom-contract-administration). The owning registry protects source-owned core schemas and commits immutable custom versions, scoped lifecycle changes, audit and receipts. The custom schema validator and registry are outside the kernel and broker.
- D-057 adds [disposable signed retirement](../../m6/management/README.md#run-signed-policy-retirement) to the shared API and fixture CLI. The owner commits retirement and audit together after confirming that the target is inactive and a complete dependency check found no active references. The compiler profile refuses deactivation and the registry protects mandatory acceptance criteria on direct publication and rollback.
- D-056 delivers [evaluator and waiver management](../../m6/management/README.md#run-evaluator-and-waiver-management) through the shared API and fixture CLI. D-052 and D-053 remain the registry and waiver owners. Management dispatch correlates with authoritative receipts across separate transactions. Validation rechecks current waiver state independently of historical inspection.
- D-055 exposes [signed publication, atomic selection and rollback](../../m6/management/README.md#run-signed-publication-and-rollback) through the same in-process boundary and opt-in CLI. Separate human authorization binds the exact proposed operation and draft context. The registry owns selection and audit. Management resolves uncertain outcomes by exact intent and authoritative receipt.
- D-054 delivers the [disposable /manage/v1 policy draft workflow](../../m6/management/README.md) with scoped version history, compiler validation, pure-kernel simulation, structural and complete Decision comparison, and a shared fixture CLI. CC-M2, CC-M3 and CC-M6 are partial. Optional signed policy removal, pack administration, UI, transport and production authentication remain open.

- D-053 delivers the [disposable scoped waiver scenario](../../m6/waivers/README.md). A distinct human approval records `waived` with an audited receipt. Reference checks enforce the exact signed policy and current workflow and authority. Mandatory waiver criteria stay incomplete under the existing kernel checks.
- D-050 delivered the [offline synthetic compiler](../../m6/compiler/README.md). D-051 delivered the [disposable signed publication lab](../../m6/publication/README.md), including selection, rollback, recovery and controlled effects with test authorities. Evaluator publication, waiver semantics and management UI remain outside D-051. Legacy rule imports require independent human review.
- D-052 delivers the [disposable evaluator publication scenario](../../m6/evaluators/README.md) with authenticated publication and revocation, durable scoped registry revisions, exact pack rubric binding and existing use-time acceptance checks. High-stakes evaluator quorum and the remaining AG-ADV-16 obligations are open.
- P-AGENT-DESKTOP has a test-only evaluator (E-092), fixture-backed reconciliation (E-093) and a guarded guest inventory source for local accounts, agent credentials and socket ownership (E-094). Guest execution awaits the [owner provisioning and designation sequence](../../m5/reference-vm/PROVISIONING.md#first-guest-inventory). Other target classes, desktop denials, control execution and readiness-report integration remain open. See the [desktop obligation map](../../m5/reference-vm/DESKTOP-PROBE.md).
- M5b-01/04 synthetic portability passed in E-091. Production adapter integration and live host coverage remain open.
- M5a remains implemented in user space with its gate open. Full VM verification and isolated containment CI remain required. Local tooling/designated guest, separate CI infrastructure, later live-model credential and later real hardware are separate prerequisites.
- The [development MCP shim](../../m5/mcp-shim/README.md) has SDK stdio, ordered lifecycle and authenticated recording-peer forwarding. D-049 permits the exact reviewed built Chrono 0.4.45 shim graph until 2026-12-18 UTC. D-048 covers the original locked-only graph. Governed activation and real service/broker ingress await verified M5a foundations.

## Security findings

| Status | Findings |
|---|---|
| DONE (harness), accepted by AI-assisted reviews 2-4 with production limits | SR-01..SR-05, SR-07..SR-12 |
| Open | SR-06 (gate G3): SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED |
| Roadmap | RM-01 composite multi-effect requests |

## M0 gates ([checklist](M0-GO-NO-GO.md))

| Gate | Prepared | Needs |
|---|---|---|
| G1 golden cases (M0-05a) | [worksheet](../audit/m0/GOLDEN-HUMAN-REVIEW-WORKSHEET.md) | Named human reviewer; 0 of 26 |
| G2 288 rules (M0-01d) | [15 batches](../audit/m0/rule-review/README.md) | Human reviewer; 0 of 288 |
| G3 live host (M0-01e, SR-06) | [trial plan](../audit/m0/SR-06-TRIAL-PLAN.md); [sandbox readiness](../audit/m0/live-host/2026-11-06-sandbox-readiness/README.md): Claude Code 2.1.318, PF-1..PF-8 PASS, C2 verified | Dedicated revocable model credential; acceptance of shared rootful Docker; 0 of 69 experiments run |
| G4 go/no-go (M0-05c) | [checklist](M0-GO-NO-GO.md) | Owner decision and risk table |

## Hook re-audit (2026-11-05, static only)

A static re-audit of the V1 hook system exists in the owner's working tree (`docs/audit/m0/hook-audit/`, uncommitted). It is held back from the public repository pending a separate disclosure review and is not summarized here.

## SR-06 sandbox (2026-11-06)

SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED; sandbox **torn down** 2026-11-06 (3 containers, 2 networks; nothing else touched). Evidence and hashes: [readiness record](../audit/m0/live-host/2026-11-06-sandbox-readiness/README.md). Limitations: shared rootful Docker daemon (owner: no injection, bypass or approval-provenance experiments here; VM or separate machine required); blocked egress attempts to `mobile.events.data.microsoft.com` from the V1 daemon process, most likely ONNX Runtime telemetry, stopped by `ORT_DISABLE_TELEMETRY=1` in a 10-minute controlled run (not packet-proven); only a SessionStart exit-0 hook observed. Plan corrections from official docs (600 s default timeout; H-6 probe `{not json}`) are plan revisions, not test evidence.

## Known blockers

- M5a needs the owner-designated Ubuntu 24.04 reference guest, the full VM checklist, and a verified isolated containment CI runner (M5a-12).
- Gates G1-G3 remain release obligations; D-014 accepted development-only deferral. This plan does not waive them.
- Pages deploy blocked by repository environment protection (pre-existing).

## Last verification

D-058 started from `43eb8bf0993fc359e937f1290bb3f6a2dd353b14`, verified on [required run 44612305086](https://github.com/infinri/Writ/actions/runs/44612305086). [E-105](TEST-EVIDENCE.md#e-105-d-058-disposable-custom-contract-administration-2026-11-19) records the slice evidence. The [recovery checkpoint](SESSION-HANDOFF.md#current-recovery-state) gives the exact-SHA CI check.

D-057 started from `99861181cab9d5dbff5364e8a0ad420a7063968f`, verified on [required run 44606340000](https://github.com/infinri/Writ/actions/runs/44606340000). [E-104](TEST-EVIDENCE.md#e-104-d-057-disposable-signed-policy-retirement-2026-11-19) records the slice evidence. The [recovery checkpoint](SESSION-HANDOFF.md#current-recovery-state) gives the exact-SHA CI check.

D-056 started from `003e0921934babc67ff2c43357031a2c93588d57`, verified on [required run 44596064005](https://github.com/infinri/Writ/actions/runs/44596064005). [E-103](TEST-EVIDENCE.md#e-103-d-056-headless-evaluator-and-waiver-management-2026-11-19) records the slice evidence. The [recovery checkpoint](SESSION-HANDOFF.md#current-recovery-state) gives the exact-SHA CI check.
D-055 started from `d9e75fce0b63e59abfffcc168e2c58e3f12d9742`, verified on [required run 44586765124](https://github.com/infinri/Writ/actions/runs/44586765124). [E-102](TEST-EVIDENCE.md#e-102-d-055-headless-signed-policy-lifecycle-2026-11-19) records the slice evidence. The [recovery checkpoint](SESSION-HANDOFF.md#current-recovery-state) records publication and CI status.

The D-054 starting SHA `7888daa13ccebe3e77ef8f82e5b3c3e7a7a76fb0` passed all six required jobs on [run 44578501669](https://github.com/infinri/Writ/actions/runs/44578501669). [E-101](TEST-EVIDENCE.md#e-101-d-054-disposable-policy-draft-management-2026-11-19) records the slice evidence. The [recovery checkpoint](SESSION-HANDOFF.md#current-recovery-state) gives the exact-SHA CI check.

D-053 code SHA `03a7accd54886d6ee2b098a35aa50a260bb7068d` passed all six required jobs on [run 44575884746](https://github.com/infinri/Writ/actions/runs/44575884746). The starting SHA `42af745af8759b80b00d6154871f0f079792e156` passed all six required jobs on [run 44565962417](https://github.com/infinri/Writ/actions/runs/44565962417). [E-100](TEST-EVIDENCE.md#e-100-d-053-disposable-scoped-waiver-2026-11-19) records the integrated verification.

The D-052 starting SHA `924fa7c3586b08df5180c63359393f376cfbf5d3` passed all six required jobs on [run 44547085950](https://github.com/infinri/Writ/actions/runs/44547085950). [E-099](TEST-EVIDENCE.md#e-099-d-052-disposable-evaluator-publication-2026-11-19) records the integrated evidence.

The D-051 starting SHA `7553cdc5bfb4f8ebebe362e51b7dc9433ac87fc4` passed all six required jobs on [run 44538627601](https://github.com/infinri/Writ/actions/runs/44538627601). E-098 records this batch’s local evidence. E-097 preserves the unresolved earlier CI stall. E-094 through E-096 carry the M5 guest source, preflight and development shim evidence.

Earlier local evidence (2026-11-13, `b60bf33`, E-074): workspace tests 394 passed (4 ignored), kill tests 4/4 alone, closure self-tests 40, property self-tests 31, closure gate PASS and report verifies, property gate 18,430 checks 0 failed, parity 320/320, dependency gate 0 problems, docs OK. CI run 43518287362, 6/6.

## Next exact action

Resume in a fresh chat from [SESSION-HANDOFF](SESSION-HANDOFF.md#current-recovery-state). Prioritize the owner-designated Ubuntu 24.04 VM inventory and full checklist plus isolated containment CI before M7. Optional policy removal, pack administration, evaluator quorum, remaining AG-ADV-16 obligations, UI, transport and production authentication are open. Any further M6 scope needs separate owner approval. M6 host integration depends on verified M5 foundations.
