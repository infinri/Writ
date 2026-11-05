# Writ V2 work state

**Last updated:** 2026-11-05 · **Branch:** v2/architecture-rebuild · **Milestone:** M0

## Current

- **Work package:** A (V1 baseline) complete for automated tasks; B/C/D next.
- **Active task:** M0-02a contract test harness (NOT STARTED at this checkpoint).
- **Last relevant commit:** see `git log -1`; the M0-00/M0-01 checkpoint commit.

## Completed and verified

- M0-00a/b: tracking docs, CLAUDE.md, isolated `m0/` harness (E-001).
- M0-01a/b/c/g: pinned baseline manifest, 52-hook inventory, 288-rule first-pass disposition, V1 test inventory in `docs/audit/m0/` (E-001).

## Known blockers

- Rust and Go toolchains are not installed on this workstation; M1 prototypes cannot be built here until they are (affects M0-04b evidence only).

## Outstanding risks and defects

- Rule dispositions and hook failure posture are unreviewed static results (M0-01d, M0-01e).
- V1 findings confirmed statically: Stop gates log "deny" but exit 1 (non-blocking) (H15, H16, H17); 5 pre-effect hooks fail open when the daemon is unreachable; 2 UserPromptSubmit hooks mint authority from prompt text; 14 registrations match only part of a capability.

## Relevant files

`m0/writ_m0/audit/*`, `m0/tests/test_audit.py`, `docs/audit/m0/*`, `docs/contracts/*`, `docs/packs/examples/*`.

## Last tests

E-001: `m0` pytest 18 passed; audit check current. Docs check: see TEST-EVIDENCE.

## Next exact action

Start M0-02a: add `m0/writ_m0/contracts.py` (schema registry with format checking) and `m0/tests/test_contracts.py` validating every schema and the existing fixtures, then M0-02b canonical digest.
