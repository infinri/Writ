# Writ V2 work state

**Last updated:** 2026-11-05 · **Branch:** v2/architecture-rebuild (local only, not pushed) · **Milestone:** M0

## Current

- **Work packages:** A (baseline audit) automated parts done; B, C, D done in the Python reference harness; E specified.
- **Active task:** none in progress. Remaining M0 work needs either a human reviewer (M0-01d, M0-05a), an isolated live host (M0-01e), more V1 mining (M0-01f) or a push (M0-05b).
- **Last relevant commit:** the WP-D/E checkpoint (`git log -1`); previous `fd0ce8f`, `e8773ce`, `5791cb7`.

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

## Known blockers

- Rust and Go toolchains are not installed; M1 prototypes cannot be built here yet.
- CI unverified until the branch is pushed (needs user approval).

## Outstanding risks and defects

- Golden decision expectations are author-reasoned, not independently reviewed (M0-05a).
- Rule dispositions and hook failure posture are static (M0-01d, M0-01e).
- V1 findings confirmed statically on the live V1 code: Stop hooks H15-H17 log deny but exit 1 (non-blocking); 5 pre-effect hooks allow when the daemon is down (default, not WRIT_STRICT); 2 UserPromptSubmit hooks mint authority from prompt text; WebFetch/WebSearch and MCP tools have no pre-effect gate. Reported to the user; not fixed (V1 is out of scope on this branch).

## Relevant files

`m0/writ_m0/{contracts,canonical,evaluation,reference_packs,decision,broker,hosts,providers,runner,packs}.py`, `m0/writ_m0/audit/`, `m0/tests/`, `docs/contracts/fixtures/{domains,decisions,policy}/`, `docs/audit/m0/`, `docs/implementation/{M1-PARITY-SUITE,BENCHMARK-ENVIRONMENT}.md`.

## Last tests

E-002: 201 passed, 0 failed; audit check current; docs check OK.

## Next exact action

Ask the user to approve pushing `v2/architecture-rebuild` (M0-05b) and to name a reviewer for M0-05a. Without that, continue M0-01f: mine `tests/test_approval_*.py`, `tests/test_cross_project_retrieval_isolation.py` and `tests/test_gates_fail_open.py` at `ebdafe5` into V2 golden decision or broker cases with source lineage.
