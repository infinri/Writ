# Writ V2 work state

**Last updated:** 2026-11-13 (after M3-07; M3 in progress, NOT complete; M3-08 blocked on owner decisions) · **Branch:** v2/architecture-rebuild · **Milestone:** M3. Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md) section "Current recovery state".

## Current

- **Verified code baseline:** `146260e` (M3-07 and M3-08 gate preparation), CI run [43475168083](https://github.com/infinri/Writ/actions/runs/43475168083), all 6 jobs success. Previous: `6be251c` (M3-05) run 43465393620. The documentation checkpoint after it has its own CI run, pending until verified. The separate website workflow fails at the Pages deploy (github-pages environment protection rejects this branch); it is not required CI and protections are not to be changed.
- **Production crates:** `writ-kernel` (M2, accepted, unchanged since), `writ-broker` and `writ-ledger-sqlite` (M3-00..M3-05 and D-035: ingress, use-time authorization, policy gate, trusted time, approvals with provenance, grants, execute lifecycle with revalidation, owned and ledger-bound effect store, evidence-based reconciliation, transition service with authenticated records, administration and effect prerequisites; evidence E-058..E-068). Schemas: ledger `writ-ledger-5`, effects `writ-effects-2`. M1 prototypes frozen as comparison material.
- **Language:** **Rust** selected (ADR-0002 ACCEPTED 2026-11-12, D-030). Go prototype kept as comparison material only.
- **Decisions:** D-035 and D-036 approved and implemented; not to be requested again. ADR-0008 APPROVED IN PRINCIPLE.
- **M3-07 implemented (E-069):** runner, process lifecycle gate, unlock-before-close ownership locks; production configuration refuses every job until a contained sandbox exists (M5). Exact-SHA CI: `146260e` run 43475168083, 6/6 (after the E-071 CI-host fix).
- **Open in M3:** M3-06 integration items (b)..(f); M3-08 closure gate built and failing (E-070), blocked on owner questions OQ-1..OQ-12; differential, property, chaos suites. See SESSION-HANDOFF section 2.
- **Next task:** owner decisions on OQ-1..OQ-12; meanwhile the unblocked items in SESSION-HANDOFF section 6.
- **Enterprise direction:** [readiness plan](../security/ENTERPRISE-READINESS.md), ENT-OWNER-01 and ENT-00..ENT-08. PostgreSQL targets enterprise operational state, SQLite remains local/offline; durable multi-worker coordination requires separate future acceptance. Controls, SOC 2 scope and any examination remain planned, not implemented or attested.
- **Historical benchmarks (M1, complete):** three official M1-06 run sets plus the process-concurrency correction (E-046, E-052, E-054) and real-edit M1-07 build timings (E-053); bundles in `m1/bench/evidence/`.

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

- Gates G1-G3 remain release obligations; D-014 accepted development-only deferral. This plan does not waive them.
- Pages deploy blocked by repository environment protection (pre-existing).

## Last verification

Local (2026-11-13, integrated M3-07 tree): workspace tests 360 passed 3 times (4 ignored), kill tests 4/4 alone, process-lifecycle stress 3/3, kernel parity 320/320, dependency gate 0 problems, closure self-tests 10 OK, docs check OK (E-069, E-070). Exact-SHA CI: `146260e` run 43475168083, 6/6; `7eb995f` failed (E-071, fixed). Previous: `82971fd` run 43466394626 and `6be251c` run 43465393620, each 6/6.

## Next exact action

Owner decisions on M3-08 questions OQ-1..OQ-12 ([M3-08-CLOSURE-MAP](M3-08-CLOSURE-MAP.md)); meanwhile the unblocked items in SESSION-HANDOFF section 6 (M3-06 (b) and (f) where no new semantics are needed; M3-08 differential, property, chaos and storage-failure suites). Historical instructions (M1-08, M3-07 design) are complete.
