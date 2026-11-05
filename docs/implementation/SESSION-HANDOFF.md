# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0 (audit and cross-domain contracts). No production language chosen.
3. **Active task:** M0-02a contract test harness.
4. **Most recent work:** V1 baseline audit generators and evidence (`docs/audit/m0/`), continuity docs, project CLAUDE.md.
5. **Relevant files:** `m0/` (harness), `docs/audit/m0/` (generated evidence), `docs/contracts/` (schemas, fixtures), `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; V1 code is reference; README untouched; no production data; kernel/broker import no model SDK; packs never grant permissions.
7. **Do not reverse:** D-001 (Python M0 oracle, not kernel), D-002 (audit reads pinned SHA `ebdafe5`), D-003 (generated evidence committed and drift-checked), D-004 (rule disposition is first pass only), D-005 (m0 tests isolated).
8. **Failures/blockers:** none failing. Rust/Go toolchains absent locally.
9. **Last verified tests:** E-001, m0 pytest 18 passed; audit check current.
10. **Next action:** create `m0/writ_m0/contracts.py` and `m0/tests/test_contracts.py` (M0-02a).
