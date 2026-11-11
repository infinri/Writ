# Writ V2 work state

**Last updated:** 2026-11-10 (America/Chicago; enterprise readiness planning; M1 measurements recorded under UTC date 2026-11-11) · **Branch:** v2/architecture-rebuild · **Milestone:** M1 active (development only, D-014). Release and production NO-GO. Recovery entry point: [SESSION-HANDOFF](SESSION-HANDOFF.md).

## Current

- **Verified published baseline:** `a91af1d775d59fab932a03506ea36384642cdae0`; [CI run 42943984397](https://github.com/infinri/Writ/actions/runs/42943984397) passed all 5 jobs. This readiness change is documentation only; verify the resulting HEAD and its CI separately.
- **Prototypes:** D-027..D-029 implemented; historical local verification E-042 and published CI above. No new runtime or benchmark results are claimed by this planning update.
- **Language:** Rust is the owner's provisional leader and preferred trusted kernel/broker direction. ADR-0002 remains OPEN pending targeted M1-08 confirmation and final owner decision.
- **Benchmarks:** official M1-06 and M1-07 results exist at `138163d` (E-046/E-047). Real incremental build, repeatability, Go process overhead and real interruption investigations remain pending. Complete raw M1-06 evidence is reported local/ignored and is not available in this checkout; preservation must be completed on the original measurement host.
- **Next task:** the already authorized targeted M1-08 work; see SESSION-HANDOFF section F. No full bake-off restart. Each official measurement requires a fresh passing environment guard.
- **Enterprise direction:** [readiness plan](../security/ENTERPRISE-READINESS.md), ENT-OWNER-01 and ENT-00..ENT-08. PostgreSQL targets enterprise operational state, SQLite remains local/offline; durable multi-worker coordination requires separate future acceptance. Controls, SOC 2 scope and any examination remain planned, not implemented or attested.

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

Published baseline CI: [42943984397](https://github.com/infinri/Writ/actions/runs/42943984397), all 5 jobs success on `a91af1d`. Documentation-only readiness checks are recorded separately as ENT-E01 in [TEST-EVIDENCE](TEST-EVIDENCE.md); they do not validate controls or create SOC 2 assurance.

## Next exact action

On the original measurement/development host, inspect the current tree and local evidence, preserve the complete M1-06/M1-07 evidence, and perform only the targeted M1-08 checks under the owner's existing authorization and full per-run environmental guards. Reconcile any unpublished local changes before integrating this documentation update. Do not accept ADR-0002/ADR-0008 or release to production.
