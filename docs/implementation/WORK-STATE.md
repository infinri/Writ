# Writ V2 work state

**Last updated:** 2026-11-19 (M6 slice 2 implemented for disposable development under D-051, M3 and M4 accepted for scoped development) · **Branch:** v2/architecture-rebuild · **Milestones:** M5 open, M6 in progress. Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md) section "Current recovery state".

## Current

- D-050 delivered the [offline synthetic compiler](../../m6/compiler/README.md). D-051 delivered the [disposable signed publication lab](../../m6/publication/README.md), including selection, rollback, recovery and controlled effects with test authorities. Evaluator publication, waiver semantics and management UI remain outside D-051. Legacy rule imports require independent human review.
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

The D-051 starting SHA `7553cdc5bfb4f8ebebe362e51b7dc9433ac87fc4` passed all six required jobs on [run 44538627601](https://github.com/infinri/Writ/actions/runs/44538627601). E-098 records this batch’s local evidence. E-097 preserves the unresolved earlier CI stall. E-094 through E-096 carry the M5 guest source, preflight and development shim evidence.

Earlier local evidence (2026-11-13, `b60bf33`, E-074): workspace tests 394 passed (4 ignored), kill tests 4/4 alone, closure self-tests 40, property self-tests 31, closure gate PASS and report verifies, property gate 18,430 checks 0 failed, parity 320/320, dependency gate 0 problems, docs OK. CI run 43518287362, 6/6.

## Next exact action

Start a fresh chat from [SESSION-HANDOFF](SESSION-HANDOFF.md#current-recovery-state). A separate owner decision is required for a later M6 slice. When the owner provides and designates the Ubuntu 24.04 guest, run the documented inventory and record actual evidence before extending collection. Keep the development shim closed to live effects until verified M5a foundations support real ingress and broker composition.
