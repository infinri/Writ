# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0, NOT complete. M1 not started; Rust vs Go not chosen (instruction).
3. **Active task:** none in progress; SR-10, SR-11, SR-05 fixed locally and IN REVIEW. SR-01..04, 07..09 accepted for the reference harness (review 2).
4. **Most recent work:** independent review 2 recorded (`docs/audit/m0/INDEPENDENT-REVIEW-M0-2.md`); request-fingerprint binding for permits and receipts; project-scoped evidence.
5. **Relevant files:** `m0/writ_m0/` (`ingress.py` new), `m0/tests/`, `docs/contracts/`, `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; no push without separate authorization; no new implementation until the security fixes are reviewed; V1 code is reference; README untouched; kernel/broker import no model SDK; packs never grant permissions; `m0/` is not the production kernel.
7. **Do not reverse:** D-001..D-010. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 broker accepts only adapter-sealed requests and the kernel stays key-free; D-011 owner policy decisions; D-012 permits and receipts bind the full request fingerprint, evidence is project-scoped. Never change a golden expected outcome or `review_status` without a recorded reviewer verdict; owner policy decisions for cases 01, 08, SR-03, SR-04 are in D-011 (not review verdicts). After the security review passes, SR-05 is next.
8. **Failures/blockers:** no failing tests. Local commits after `61b0eca` not pushed (needs authorization). Pages deploy blocked by repo settings. M1 blocked.
9. **Last verified tests:** local E-009 270 passed; last CI 256 passed at `61b0eca`.
10. **Next action:** see WORK-STATE "Next exact action".
