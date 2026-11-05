# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0, NOT complete. M1 not started; Rust vs Go not chosen (instruction).
3. **Active task:** none in progress; SR-12 fixed locally and IN REVIEW. SR-01..05 and SR-07..11 accepted for the reference harness (reviews 2 and 3). RM-01 roadmap.
4. **Most recent work:** review 3 recorded (`docs/audit/m0/INDEPENDENT-REVIEW-M0-3.md`); artifact and signoff scope (SR-12, D-013); verdict table and M0-05a cover all 26 cases.
5. **Relevant files:** `m0/writ_m0/` (`ingress.py` new), `m0/tests/`, `docs/contracts/`, `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; no push without separate authorization; no new implementation until the security fixes are reviewed; V1 code is reference; README untouched; kernel/broker import no model SDK; packs never grant permissions; `m0/` is not the production kernel.
7. **Do not reverse:** D-001..D-010. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 broker accepts only adapter-sealed requests and the kernel stays key-free; D-011 owner policy decisions; D-012 permits and receipts bind the full request fingerprint, evidence is project-scoped; D-013 artifacts and signoffs are tenant/project/task scoped. Never change a golden expected outcome or `review_status` without a recorded reviewer verdict; owner policy decisions for cases 01, 08, SR-03, SR-04 are in D-011 (not review verdicts). After the security review passes, SR-05 is next.
8. **Failures/blockers:** no failing tests. Local commits after `957e32f` not pushed (needs authorization). Pages deploy blocked by repo settings. M1 blocked.
9. **Last verified tests:** local E-010 280 passed; last CI 270 passed at `957e32f`.
10. **Next action:** see WORK-STATE "Next exact action".
