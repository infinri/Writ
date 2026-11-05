# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0 (audit and cross-domain contracts). No production language chosen (ADR-0002 OPEN).
3. **Active task:** none in progress; next is M0-05b (push, needs user approval) or M0-01f (V1 test mining).
4. **Most recent work:** Python reference harness in `m0/` covering contracts, canonical digest, evaluation, decision oracle, broker/approvals, host and provider simulation, pack registry; M1 parity spec; CI job definition.
5. **Relevant files:** `m0/writ_m0/`, `m0/tests/`, `docs/contracts/` (schemas and fixtures), `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; V1 code is reference; README untouched; no production data; kernel/broker import no model SDK; packs never grant permissions; nothing in `m0/` is the production kernel.
7. **Do not reverse:** D-001 Python oracle not kernel; D-002 audit reads pinned `ebdafe5`; D-003 generated evidence drift-checked; D-004 rule disposition first pass only; D-005 m0 tests isolated; D-006 canonical JSON without floats; D-007 evaluators ignore producer verification status; D-008 precedence, scoped resource ids `<scheme>:<tenant>/<project>/<path>`, broker re-decides at use time.
8. **Failures/blockers:** no failing tests. Rust/Go toolchains absent. CI not yet run (not pushed).
9. **Last verified tests:** E-002: `cd m0 && .venv/bin/python -m pytest` 201 passed; audit check current; docs check OK.
10. **Next action:** see WORK-STATE "Next exact action".
