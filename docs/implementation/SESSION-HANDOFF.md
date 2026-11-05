# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0, NOT complete. M1 not started; Rust vs Go not chosen (instruction).
3. **Active task:** none in progress; security corrections SR-01/02/03/07/08/09 pushed (`d3f722d`, CI green) and IN REVIEW; no implementation until reviewed.
4. **Most recent work:** independent review recorded in `docs/audit/m0/INDEPENDENT-REVIEW-M0.md`; one commit per fix with failing-first tests; review package updated with a post-review change table.
5. **Relevant files:** `m0/writ_m0/` (`ingress.py` new), `m0/tests/`, `docs/contracts/`, `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; no push without separate authorization; no new implementation until the security fixes are reviewed; V1 code is reference; README untouched; kernel/broker import no model SDK; packs never grant permissions; `m0/` is not the production kernel.
7. **Do not reverse:** D-001..D-010. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 broker accepts only adapter-sealed requests and the kernel stays key-free. Never change a golden expected outcome or `review_status` without a recorded reviewer verdict; owner policy decisions for cases 01, 08, SR-03, SR-04 are in D-011 (not review verdicts). After the security review passes, SR-05 is next.
8. **Failures/blockers:** no failing tests. Pages deploy blocked by repo settings.
9. **Last verified tests:** CI E-007 256 passed at `d3f722d`.
10. **Next action:** see WORK-STATE "Next exact action".
