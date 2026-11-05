# Writ V2 work state

**Last updated:** 2026-11-05 · **Branch:** v2/architecture-rebuild (pushed through `487b95d`; later commits local) · **Milestone:** M0

## Current

- **Work packages:** A (baseline audit) automated parts done; B, C, D done in the Python reference harness; E specified.
- **Active task:** none in progress. M0-05a review package delivered (`docs/audit/m0/GOLDEN-DECISION-REVIEW.md`), all 24 cases PENDING INDEPENDENT REVIEW; M0-01f done.
- **Last relevant commits:** `5e011c9` (V1 regression port), `cd76fdd` (review package); pushed head `487b95d`.

## Completed and verified (E-001, E-002)

| Area | Result |
|---|---|
| Continuity | TASKS, DECISION-LOG (D-001..D-008), TEST-EVIDENCE, SESSION-HANDOFF, CLAUDE.md |
| V1 audit | Pinned manifest, 52-hook inventory, 288-rule first pass, test inventory, retrieval golden export, unmediated-tool table |
| Contracts | 17 schemas strictly validated; canonical digest (D-006); 4 new authority schemas; Evidence extension (D-007) |
| Cross-domain | Code review, research with contradictory sources, noncode document review through one evaluator; fabricated citation and missing counterevidence negatives |
| Kernel oracle | Pure decide(), 24 hand-reasoned golden cases, deny precedence, scoped resource ids (D-008) |
| Broker | SQLite ledger, atomic single-use grants, idempotency, HMAC permits, receipts and reconciliation |
| Independence | Two host extractors, principal proofs, two stub providers, managed runner, pack registry, import-layer checks |
| M1 prep | Parity suite spec, benchmark environment record |
| Review and regressions | Golden review package (pending reviewer); 21 ported V1 regressions with provenance; GRANT_MALFORMED fix (D-009) |

## Known blockers

- Rust and Go toolchains are not installed; M1 prototypes cannot be built here yet.
- Commits after `487b95d` not yet pushed or run in CI (awaiting user approval to push).
- GitHub Pages publish for this branch is blocked by repository environment protection (pre-existing).

## Outstanding risks and defects

- Golden decision expectations are author-reasoned, not independently reviewed (M0-05a).
- Rule dispositions and hook failure posture are static (M0-01d, M0-01e).
- V1 findings confirmed statically on the live V1 code: Stop hooks H15-H17 log deny but exit 1 (non-blocking); 5 pre-effect hooks allow when the daemon is down (default, not WRIT_STRICT); 2 UserPromptSubmit hooks mint authority from prompt text; WebFetch/WebSearch and MCP tools have no pre-effect gate. Reported to the user; not fixed (V1 is out of scope on this branch).

## Relevant files

`m0/writ_m0/{contracts,canonical,evaluation,reference_packs,decision,broker,hosts,providers,runner,packs}.py`, `m0/writ_m0/audit/`, `m0/tests/`, `docs/contracts/fixtures/{domains,decisions,policy}/`, `docs/audit/m0/`, `docs/implementation/{M1-PARITY-SUITE,BENCHMARK-ENVIRONMENT}.md`.

## Last tests

Local E-004: 223 passed, 0 failed. CI E-003 (`487b95d`): 201 passed, both jobs success.

## Next exact action

Wait for the independent reviewer's verdicts on `GOLDEN-DECISION-REVIEW.md`; apply only reviewer-recorded corrections. Ask the user to approve pushing `cd76fdd` and `5e011c9` (and the state commit) to run CI. Remaining M0 items: M0-01d rule semantic review, M0-01e live-host failure posture, M0-05a review, M0-05c go/no-go. Do not start M1.
