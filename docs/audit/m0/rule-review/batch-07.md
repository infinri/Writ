# Rule review batch-07: enforcement (19 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-07.csv](batch-07.csv).

## ENF-CTX-003

- **Domain / severity / mandatory:** enforcement / high / true
- **Statement:** Any pattern from training data that conflicts with the Bible's rules is a violation. The project's rules override training data patterns.
- **Trigger:** When generated code uses patterns that appear frequently in training data but conflict with the project's rules -- specifically: factory patterns over repositories, Model::load() over service contracts, string inference over persistence verification.
- **V1 enforcement text:** Magento Coding Standard PHPCS (ENF-POST-007). FW-M2-002 catches factory/load patterns.
- **V1 mechanical path:** bin/run-analysis.sh (PHPCS lint) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-POST-007, FW-M2-002
- **Proposed disposition:** obsolete (names retired V1 mechanism 'the Bible'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-GATE-006

- **Domain / severity / mandatory:** enforcement / high / true
- **Statement:** A handoff file must be valid JSON and contain the required keys. A malformed or incomplete handoff is rejected: downstream slices depend on a well-formed handoff, so a broken one is blocked at the gate rather than silently consumed.
- **Trigger:** When writing a sub-agent / phase handoff file (`.claude/handoffs/*`) for dependency-ordered slice execution.
- **V1 enforcement text:** .claude/hooks/validate-handoff.sh (PostToolUse on the handoff file) parses the JSON and checks required keys; on failure it emits an ENF-GATE-006 violation and blocks.
- **V1 mechanical path:** .claude/hooks/validate-handoff.sh (resolves at baseline: no)
- **Rule id referenced in V1 code:** 2 file(s): `bin/validate-handoff.sh`, `hooks/scripts/validate-handoff.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-POST-006
- **Proposed disposition:** obsolete (names retired V1 mechanism 'handoff'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-GATE-007

- **Domain / severity / mandatory:** enforcement / critical / true
- **Statement:** Test skeletons with specific assertions must be generated, written to disk via the Write tool, and approved before any implementation code. Implementation delivered without pre-approved test skeletons is a violation.
- **Trigger:** In Work mode, after the planning phase has been approved (phase-a gate cleared) and before any implementation code is written.
- **V1 enforcement text:** Mechanically enforced by bin/lib/writ-session.py:1125-1370 (the test-skeletons gate in _can_write_check). Implementation writes are denied with [ENF-GATE-TEST] until the gate clears.
- **V1 mechanical path:** bin/lib/writ-session.py:1125-1370 (can_write test-skeleton gate) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `writ/graph/schema.py`
- **Rule id referenced in V1 tests:** 21 file(s)
- **Related rules (conflict candidates):** ENF-POST-004, ENF-PROC-TDD-001, ENF-SEC-001, FW-M2-RT-004
- **Proposed disposition:** obsolete (names retired V1 mechanism 'Test skeletons'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-OPS-001

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Every operational claim must include supporting evidence for each assertion. A claim without evidence must be downgraded to "untested estimate" or removed entirely.
- **Trigger:** When the AI makes any claim about performance, throughput, scalability, or reliability in design docs, code comments, or plan.md (e.g., "handles 1000 reservations/minute", "scales horizontally", "resilient to failures").
- **V1 enforcement text:** Self-enforced via design review. Operational claims should be flagged in code review and matched against test or benchmark evidence. Static analysis cannot verify performance claims; reviewers must.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-POST-006, PERF-OPT-001, PERF-QBUDGET-001, RESEARCH-CITE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-OPS-002

- **Domain / severity / mandatory:** enforcement / high / false
- **Statement:** Queue-based features must declare complete infrastructure (primary queue, DLQ, retry policy, consumer config, monitoring) AND complete consumer behavior guarantees (idempotency, duplicate handling, retry failure escalation).
- **Trigger:** When implementing any feature that uses message queues (publishing messages, consuming messages, or configuring queue infrastructure).
- **V1 enforcement text:** Self-enforced via design review. Queue infrastructure (DLQ, retry policy, consumer config, idempotency guard) should be verified during code review for every queue-using feature. Framework guidance: see bible/frameworks/magento/runtime-constraints.md for Magento 2 queue patterns.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** FW-M2-RT-005, SCALE-QUEUE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-POST-003

- **Domain / severity / mandatory:** enforcement / critical / true
- **Statement:** Parameter order, parameter types, and return types in the interface must exactly match the implementation and all call sites.
- **Trigger:** After generating a class that implements an interface, or after generating an interface and its implementation.
- **V1 enforcement text:** PHPStan level 8 (ENF-POST-007) catches method signature mismatches.
- **V1 mechanical path:** bin/run-analysis.sh (PHPStan level 8) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ENF-POST-007, SOLID-LSP-002
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-POST-004

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Every declared domain invariant must have corresponding test coverage. The AI must refuse to mark implementation as complete if invariants lack tests.
- **Trigger:** When writing tests for code that depends on declared domain invariants (data integrity rules, business rules with explicit constraints).
- **V1 enforcement text:** Self-enforced via design review. The test-skeletons gate (ENF-GATE-007) requires test files exist; reviewers should verify those tests actually exercise declared invariants, not just call the happy path.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-GATE-007, ENF-POST-005, FW-M2-005, TEST-COVERAGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-POST-005

- **Domain / severity / mandatory:** enforcement / high / false
- **Statement:** For any threshold, tests must cover: exactly at the threshold (must NOT trigger), one unit above (must trigger), and well above (must trigger).
- **Trigger:** When the implementation contains threshold-based logic (item count, subtotal, quantity, date comparisons) and tests exist for that logic.
- **V1 enforcement text:** Code review of test files. Reviewers should verify boundary tests for threshold logic (at-threshold, one-above, well-above).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** ENF-POST-004, TEST-EDGE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-POST-006

- **Domain / severity / mandatory:** enforcement / high / true
- **Statement:** A handoff must carry no unresolved "I cannot verify" items. Every claim is verified, explicitly flagged for human review, or removed before handoff -- an unverifiable assertion must never be passed downstream as if settled.
- **Trigger:** Post-generation verification, before a handoff file is finalized.
- **V1 enforcement text:** .claude/hooks/validate-handoff.sh scans the handoff for unresolved "I cannot verify" strings and emits an ENF-POST-006 violation until each is resolved or flagged.
- **V1 mechanical path:** .claude/hooks/validate-handoff.sh (resolves at baseline: no)
- **Rule id referenced in V1 code:** 2 file(s): `bin/validate-handoff.sh`, `hooks/scripts/validate-handoff.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-GATE-006, ENF-OPS-001, ENF-PROC-VERIFY-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'handoff'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-POST-007

- **Domain / severity / mandatory:** enforcement / critical / true
- **Statement:** Generated code must be validated by static analysis tools. The AI's own reasoning is necessary but not sufficient. PHPStan at level 8 is the minimum for PHP.
- **Trigger:** After generating code, before marking a slice as complete.
- **V1 enforcement text:** bin/run-analysis.sh runs PHPStan level 8 on PHP files via the validate-file.sh PostToolUse hook. validate-rules.sh records pass/fail to the session cache. Self-enforced halt on errors.
- **V1 mechanical path:** bin/run-analysis.sh (PHPStan level 8) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 5 file(s): `bin/lib/analyzers-lang.sh`, `bin/lib/emit-summary.py`, `bin/run-analysis.sh`, `hooks/scripts/pre-validate-file.sh`, `hooks/scripts/validate-file.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-CTX-003, ENF-POST-003, FW-M2-002, PHP-TRY-001, PHP-TYPE-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PRE-001

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Before writing any plugin, observer, or event listener, the AI must produce a written call-path declaration answering: what is the entry point per execution context, does the same class handle all contexts, will the plugin intercept all required contexts, and which contexts are missed.
- **Trigger:** When the task involves writing a plugin, observer, or event listener in Magento 2 or any framework with context-dependent dispatch.
- **V1 enforcement text:** Self-enforced via design review. When writing a Magento plugin/observer/event listener, reviewers should verify the AI considered each execution context (REST, GraphQL, admin, CLI) the dispatch will reach.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ENF-PRE-003, ENF-PRE-004, FW-M2-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PRE-002

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Before writing validation logic, the AI must declare: what makes the entity legitimate at the domain level, whether legitimacy is structural (format) or persistence-based (exists in DB), and if persistence-based, which repository or service contract verifies existence.
- **Trigger:** When writing any validation method that determines whether a domain entity is legitimate, eligible, or active.
- **V1 enforcement text:** Self-enforced via design review. Reviewers should verify validation methods declare whether legitimacy is format-based or persistence-based, and if persistence-based, which repository contract verifies existence.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** FW-M2-001, FW-M2-005
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PRE-003

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Before writing any plugin declaration, the AI must justify: why this specific class (not the interface, not a parent), why this hook type (before/around/after), what state is available at this execution point, and whether the logic depends on data only reliable after a specific side effect.
- **Trigger:** When writing a plugin declaration (`<plugin>` or `<type>` with plugin in `di.xml`).
- **V1 enforcement text:** Self-enforced via design review. Plugin declarations should justify class choice (interface vs concrete vs parent), hook type (before/around/after), and state availability at the chosen seam.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ENF-PRE-001, FW-M2-003, FW-M2-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PRE-004

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Before injecting any dependency, verify it is safe in all execution contexts where the class will be invoked. MessageManager, session, or UI-dependent classes injected into service-layer classes are violations unless explicitly justified.
- **Trigger:** When injecting any class as a constructor dependency into a class that is reachable via REST, GraphQL, or CLI -- especially if the dependency assumes UI context (session, message manager, layout).
- **V1 enforcement text:** Self-enforced via code review. Static analysis (custom PHPStan rule) can flag MessageManager/session/layout dependencies in service-layer classes.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-PRE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-SYS-002

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** For any decision depending on a prior state transition, declare: the authoritative decision point, the temporal position of the current check, whether re-evaluation is valid, and the staleness risk. Re-checking a fact that was already authoritatively decided by a prior event is a violation if re-evaluation could contradict the original decision.
- **Trigger:** When writing validation, check, or decision logic that depends on a fact established by a prior system event (e.g., "is this product salable?", "has this order been paid?", "is this coupon valid?") -- specifically when the code runs AFTER the authoritative event.
- **V1 enforcement text:** Self-enforced via design review. Code review should flag any re-evaluation of facts established by upstream authoritative events (post-order salability checks, post-payment authorization checks, etc.).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/session/commit_capture.py`, `writ/session/gates.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** FW-M2-RT-001, FW-M2-RT-006
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-SYS-003

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Every state transition must declare a state machine (legal states and transitions), transition guards, an atomicity strategy, and contention handling. Bare read-then-write without a concurrency guard is always a violation.
- **Trigger:** When writing code that changes a status/state column in a database (e.g., `reserved` to `released`, `pending` to `processing`, `active` to `expired`) where more than one actor could attempt the same transition.
- **V1 enforcement text:** Self-enforced via design review. Code review must verify that every state-transition write includes an atomicity guard: optimistic concurrency control (CAS in WHERE clause), advisory lock, or transaction with SELECT FOR UPDATE. Look for read-then-write patterns without a WHERE clause matching the prior state.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-SYS-005, ENF-SYS-006, FW-M2-RT-002, TEST-INT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-SYS-005

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** For any feature involving concurrent actors, state transitions, or DB constraints, declare which behaviors cannot be proven with mocks and which require integration tests against a real database. Claiming production-readiness for concurrent/async features with only mocked tests is a violation.
- **Trigger:** When the implementation claims concurrency safety, idempotency, or atomicity, and the only tests validating those claims use mocked database operations.
- **V1 enforcement text:** Self-enforced via design review. Reviewers should verify that concurrency claims are backed by integration tests against a real database, not just mocked unit tests. ENF-SYS-003 (atomicity guards) and TEST-INT-001 (integration test discipline) are the related principles.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 58 file(s)
- **Related rules (conflict candidates):** ENF-SYS-003, TEST-EDGE-003, TEST-INT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-SYS-006

- **Domain / severity / mandatory:** enforcement / critical / false
- **Statement:** Every declared state must have at least one incoming code path (except initial state), at least one outgoing code path (except terminal state), and at least one assignment in generated code. A constant with no assignment anywhere in the codebase is a dead declaration.
- **Trigger:** When implementing or reviewing a state machine declaration: every status constant must have at least one assignment (incoming transition) and at least one read (used by guards or exits), unless explicitly the initial or terminal state.
- **V1 enforcement text:** Self-enforced via design review. Static analysis (grep for status constants assigned vs declared) can flag obvious dead-state cases. Reviewers should verify every declared state has at least one incoming code path.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-DEAD-001, ENF-SYS-003, FW-M2-RT-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-TEST-001

- **Domain / severity / mandatory:** enforcement / critical / true
- **Statement:** Tests written in a session must actually pass before the work is claimed complete. Pending or failing tests block completion: a written-but-unrun test is not evidence, and a failing test is a defect, not a footnote.
- **Trigger:** At session Stop, when test files written or modified during the session have not been run to a passing result.
- **V1 enforcement text:** .claude/hooks/writ-run-pending-tests.sh runs the session's pending tests at Stop and emits an ENF-TEST-001 violation summary on any failure.
- **V1 mechanical path:** .claude/hooks/writ-run-pending-tests.sh (resolves at baseline: no)
- **Rule id referenced in V1 code:** 2 file(s): `hooks/scripts/writ-run-pending-tests.sh`, `hooks/scripts/writ-verify-before-claim.sh`
- **Rule id referenced in V1 tests:** 11 file(s)
- **Related rules (conflict candidates):** ENF-PROC-VERIFY-001, TEST-CI-001
- **Proposed disposition:** unverified (V1 binding path absent at baseline: .claude/hooks/writ-run-pending-tests.sh); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_
