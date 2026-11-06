# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

1. **Objective:** build Writ V2, a model- and domain-agnostic governance, retrieval and memory platform, per [master plan](MASTER-IMPLEMENTATION-PLAN.md).
2. **Milestone:** M0, NOT complete. M1 not started; Rust vs Go not chosen (instruction).
3. **Active task:** none in progress. Development GO framework A-1 approved in principle; D-014 PROPOSED; GO Decision table blank pending the owner's attributable signature. SR-06: SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED; sandbox torn down 2026-11-06.
4. **Most recent work:** SR-06 sandbox prepared, preflighted, telemetry diagnostic run (ORT_DISABLE_TELEMETRY=1 stopped V1 daemon egress attempts in a 10-minute window), torn down; evidence in `docs/audit/m0/live-host/2026-11-06-sandbox-readiness/`; recommended dispositions for all 16 risks in `docs/implementation/M0-GO-NO-GO.md`. The hook-audit folder stays local and unpublished pending disclosure review.
5. **Relevant files:** `m0/writ_m0/` (`ingress.py` new), `m0/tests/`, `docs/contracts/`, `docs/audit/m0/`, `docs/implementation/TASKS.md`.
6. **Constraints:** branch `v2/architecture-rebuild` only; no push without separate authorization; no new implementation until the security fixes are reviewed; V1 code is reference; README untouched; kernel/broker import no model SDK; packs never grant permissions; `m0/` is not the production kernel.
7. **Do not reverse:** D-001..D-010. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 broker accepts only adapter-sealed requests and the kernel stays key-free; D-011 owner policy decisions; D-012 permits and receipts bind the full request fingerprint, evidence is project-scoped; D-013 artifacts and signoffs are tenant/project/task scoped. D-014 is PROPOSED only: do not treat it as accepted. Never change a golden expected outcome or `review_status` without a recorded reviewer verdict; never fill reviewer columns in rule-review batches or the worksheet; owner policy decisions for cases 01, 08, SR-03, SR-04 are in D-011 (not review verdicts). After the security review passes, SR-05 is next.
8. **Failures/blockers:** no failing tests. Gates need humans and a disposable host. M1 blocked.
9. **Last verified tests:** CI E-012, run 42129295198 at `6ab870d`: 281 passed, audit and docs checks pass. Gate packages are on GitHub.
10. **Next action:** see WORK-STATE "Next exact action".
