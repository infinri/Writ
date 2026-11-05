# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0 (audit and cross-domain contracts). No production language chosen (ADR-0002 OPEN).
3. **Active task:** none in progress; waiting on independent review of the 24 golden cases and implementation (M0-05a) with security priorities SR-01..SR-06. M0 is NOT complete.
4. **Most recent work:** pushed `38ccbf6` and CI passed with 223 tests (run 42098268490); golden review package `docs/audit/m0/GOLDEN-DECISION-REVIEW.md` (all PENDING INDEPENDENT REVIEW, do not self-approve); V1 regression port `m0/tests/test_v1_regressions.py` with `docs/audit/m0/V1-REGRESSION-PORT.md`.
5. **Relevant files:** `m0/writ_m0/`, `m0/tests/`, `docs/contracts/` (schemas and fixtures), `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; V1 code is reference; README untouched; no production data; kernel/broker import no model SDK; packs never grant permissions; nothing in `m0/` is the production kernel.
7. **Do not reverse:** D-001 Python oracle not kernel; D-002 audit reads pinned `ebdafe5`; D-003 generated evidence drift-checked; D-004 rule disposition first pass only; D-005 m0 tests isolated; D-006 canonical JSON without floats; D-007 evaluators ignore producer verification status; D-008 precedence, scoped resource ids `<scheme>:<tenant>/<project>/<path>`, broker re-decides at use time; D-009 no grant inheritance, malformed grants refused. Never change golden expected outcomes without a recorded reviewer verdict. Do not start M1 or choose Rust/Go.
8. **Failures/blockers:** no failing tests. Pages deploy blocked by repo environment protection (pre-existing). Rust/Go toolchains absent. M1 not started by instruction.
9. **Last verified tests:** CI E-005 223 passed at `38ccbf6`.
10. **Next action:** see WORK-STATE "Next exact action".
