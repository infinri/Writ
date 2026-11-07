# Writ V2 work state

**Last updated:** 2026-11-07 (M1-06a done) · **Branch:** v2/architecture-rebuild · **Milestone:** M1 active (development only, D-014); M1-00..M1-05 and M1-06a DONE; M1-06 blocked by environment and an owner decision. Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md).

## Current

- **Code and CI:** `fe7de57a430355a529bc39088e659b5f5f915f39`, CI https://github.com/infinri/Writ/actions/runs/42394888125 (5 jobs success, including the required property job).
- **Prototypes:** Rust `m1/rust/`, Go `m1/go/`: both 414/414 fixtures, 11/11 property regressions, 0 property mismatches (E-025).
- **Benchmark harness:** `m1/bench/` (M1-06a, E-026): dry run passes; the guard refuses measurement in the current environment. No measurement taken.
- **Next:** M1-06 measurements, after the owner sets the `performance` governor and decides the workload B ledger question (SESSION-HANDOFF section F). M1-07 build and packaging assessment can proceed without measurements.

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

- Gates G1-G3 need human reviewers or a credential and live trials; PROPOSED D-014 would replace or defer them for M1 development only.
- Pages deploy blocked by repository environment protection (pre-existing).

## Last tests

CI E-013: run [42309913797](https://github.com/infinri/Writ/actions/runs/42309913797) at `e8aa771`, 281 passed; audit 8 files current; docs and schema checks pass. Signoff-package evidence baseline: `6007d09`, run 42308217716.

## Next exact action

See SESSION-HANDOFF section F. M1-06 runs are blocked: governor `powersave`; workload B ledger decision pending.
