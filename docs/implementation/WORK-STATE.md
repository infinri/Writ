# Writ V2 work state

**Last updated:** 2026-11-17 (M5b portability; M3 and M4 accepted for scoped development) · **Branch:** v2/architecture-rebuild · **Milestone:** M5 in progress. Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md) section "Current recovery state".

## Current

- The M5b-01/04 test batch compares canonical ActionRequests and complete Decisions through independent Claude and CLI normalizers with fixed scope, clock, snapshot and policy. Input refusals and non-allow Decisions never reach the test callback. See E-091.
- Production adapter integration and live host coverage remain open. The proposed next batch scopes fixture-backed work on the missing P-AGENT-DESKTOP producer. The private untracked hook-audit material and historical worktrees are retained.
- M5a remains implemented in user space with its gate open. Full VM verification and isolated containment CI remain required. Local tooling/designated guest, separate CI infrastructure, later live-model credential and later real hardware are separate prerequisites.
- D-048 covers only locked-but-unbuilt Chrono 0.4.45. MCP adoption remains blocked on the built dependency and concrete lifecycle integration. Governed integration requires verified foundations. M5 remains in progress, ADR-0008 APPROVED IN PRINCIPLE, release and production NO-GO.

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

The prior development baseline `ff0e2eb0e828e98d00fc11e4982586238290cfe3` passed [required CI run 44187034059](https://github.com/infinri/Writ/actions/runs/44187034059), six jobs. The current batch's focused results are in E-091. The handoff identifies where to verify publication and exact-SHA CI.

Earlier local evidence (2026-11-13, `b60bf33`, E-074): workspace tests 394 passed (4 ignored), kill tests 4/4 alone, closure self-tests 40, property self-tests 31, closure gate PASS and report verifies, property gate 18,430 checks 0 failed, parity 320/320, dependency gate 0 problems, docs OK. CI run 43518287362, 6/6.

## Next exact action

Start a fresh chat from [SESSION-HANDOFF](SESSION-HANDOFF.md#current-recovery-state) and verify local/remote state and exact-SHA CI before scoping the P-AGENT-DESKTOP producer batch.
