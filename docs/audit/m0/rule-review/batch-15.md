# Rule review batch-15: security, testing (22 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-15.csv](batch-15.csv).

## SEC-VAL-TYPE-001

- **Domain / severity / mandatory:** security / high / false
- **Statement:** Request payloads must be validated against a typed schema (Pydantic, Marshmallow, Zod, Yup, JSON Schema, Joi, class-validator, Bean Validation) before business logic runs. Hand-rolled `if 'field' in request.json` checks are insufficient.
- **Trigger:** When implementing any endpoint that accepts a JSON or form body.
- **V1 enforcement text:** Code review. Look for handler functions that read request.json/request.body without an immediate schema constructor or validate() call.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-DTO-001, SEC-AUTHZ-MASS-001, SEC-DATA-PII-002, SEC-INJ-DESER-001, SEC-VAL-LENGTH-001, SEC-VAL-SERVER-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-ASSERT-001

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Every test has at least one assertion. Tests that exercise code without asserting an outcome are smoke tests at best and false-positive guarantees at worst.
- **Trigger:** When writing a test function.
- **V1 enforcement text:** Linter rule (pytest-style assertion check; pylint custom rule).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** TEST-ASSERT-002, TEST-EXIST-001, TEST-MOCK-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-ASSERT-002

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Assertions are specific. `assertEqual(actual, 5)` beats `assertTrue(actual == 5)`; `assertGreater(x, y)` beats `assertTrue(x > y)`. Specific assertions produce specific failure messages.
- **Trigger:** When writing test assertions.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** TEST-ASSERT-001, TEST-NAME-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-CI-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** All tests pass in CI before merge. 'Known failing' tests are not left in the suite; flaky tests are quarantined with a tracked ticket and fixed or removed, not silently skipped.
- **Trigger:** When merging code.
- **V1 enforcement text:** CI gate. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** ENF-TEST-001, TEST-ISOLATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-COVERAGE-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Critical business logic paths have at least 80% branch coverage. Coverage targets apply where the code's correctness matters (billing, auth, data integrity); UI and trivial accessors are exempted.
- **Trigger:** When measuring test coverage.
- **V1 enforcement text:** Coverage tool (pytest-cov, jest --coverage, istanbul, JaCoCo) with CI gate.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-POST-004, TEST-EXIST-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-EDGE-001

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Error paths are tested alongside the happy path: invalid input, missing data, permission denied, timeout, downstream failure. The test suite proves the code handles failure as well as success.
- **Trigger:** When testing a public function.
- **V1 enforcement text:** Code review. Coverage tools that report branch coverage.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 7 file(s)
- **Related rules (conflict candidates):** TEST-EDGE-002, TEST-EDGE-003, TEST-REGRESSION-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-EDGE-002

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Boundary values are tested: empty collection, zero, one, max int, max length, empty string, None/null. Off-by-one bugs and overflow bugs live at the boundaries.
- **Trigger:** When testing a function that operates on collections, numeric values, or string inputs.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-POST-005, TEST-EDGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-EDGE-003

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Concurrent access paths are tested where applicable: race conditions, lock contention, deadlocks. The test suite includes scenarios that exercise multiple parallel callers.
- **Trigger:** When implementing concurrent code (locks, async coroutines, shared state, queues).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-STATE-001, ENF-SYS-005, TEST-EDGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-EXIST-001

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Every public function or method has at least one test. New public APIs are not committed without a test covering at least the happy path.
- **Trigger:** When adding or modifying a public function, method, or class.
- **V1 enforcement text:** Code review. Coverage tools report public-function coverage.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** DOC-TYPE-001, PROC-TEST-001, TEST-ASSERT-001, TEST-COVERAGE-001, TEST-EXIST-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-EXIST-002

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Every API endpoint has at least one integration test that exercises it end-to-end (request -> response). Unit tests on the handler function are not a substitute.
- **Trigger:** When adding or modifying an API endpoint (REST, GraphQL, RPC).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** TEST-EXIST-001, TEST-INT-001, TEST-MOCK-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-FIXTURE-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Test fixtures or factories are used for object setup. Constructing the same domain object inline across many tests is a violation; factory functions (factory_boy, FactoryBot, pytest fixtures) own the construction logic.
- **Trigger:** When writing test setup code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 17 file(s)
- **Related rules (conflict candidates):** TEST-FIXTURE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-FIXTURE-002

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Fixtures are minimal: each test sets up only what it actually needs. Kitchen-sink setup that creates 10 related objects when the test only uses 2 is a violation. The test reads as 'given these inputs, expect this outcome' without noise.
- **Trigger:** When writing test setup code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 9 file(s)
- **Related rules (conflict candidates):** TEST-FIXTURE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-INT-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Integration tests must exist for every behavior declared as "unprovable by mocks" in the Tests must use a real database connection and verify actual constraint enforcement.
- **Trigger:** When ENF-SYS-005 identifies behaviors that cannot be proven with mocks (DB unique constraints, transaction isolation, queue redelivery, actual multi-step state transitions).
- **V1 enforcement text:** Self-enforced via design review. Reviewers should verify integration tests exist for concurrency claims. Related: ENF-SYS-005 (integration reality check), ENF-SYS-003 (atomicity guards).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ENF-SYS-003, ENF-SYS-005, TEST-EXIST-002, TEST-ISOLATE-002, TEST-ISOLATE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-ISOLATE-001

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Tests do not depend on execution order. State is set up per-test and torn down per-test. Cross-test mutable state (module globals modified by one test, read by another) is a violation.
- **Trigger:** When writing tests that share modules, fixtures, or process-level state.
- **V1 enforcement text:** Test framework config (pytest's randomize order plugin catches this). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 19 file(s)
- **Related rules (conflict candidates):** TEST-CI-001, TEST-ISOLATE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-ISOLATE-002

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Tests do not make real network calls. External dependencies are mocked, stubbed, or replaced with a local fake (VCR, MSW, responses, requests-mock). Tests do not hit a real Stripe sandbox, Slack webhook, or third-party API.
- **Trigger:** When writing tests that exercise code with external dependencies (HTTP APIs, third-party services, payment gateways).
- **V1 enforcement text:** Code review. Network-blocking pytest fixtures (pytest-socket).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/log_rotation.py`
- **Rule id referenced in V1 tests:** 8 file(s)
- **Related rules (conflict candidates):** TEST-INT-001, TEST-ISOLATE-003, TEST-MOCK-001, TEST-MOCK-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-ISOLATE-003

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Test database state is reset between tests: transaction rollback after each test, fixture reload, or a fresh ephemeral database. Tests do not leak rows that affect other tests.
- **Trigger:** When writing tests that read or write a database.
- **V1 enforcement text:** Test framework config. Pytest fixtures with transactional scope.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 19 file(s)
- **Related rules (conflict candidates):** TEST-INT-001, TEST-ISOLATE-001, TEST-ISOLATE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-MOCK-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Mocks verify behavior: that the mock was called with specific arguments, the right number of times, in the right order. Mocks that exist purely to suppress a dependency without assertions on the call are weak tests.
- **Trigger:** When using mocks in tests.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** TEST-ASSERT-001, TEST-ISOLATE-002, TEST-MOCK-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-MOCK-002

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Mock return values match the actual API/service response shape. A mock that returns `{'ok': True}` when the real service returns `{'data': {...}, 'status': 'success'}` produces tests that pass while real integration fails.
- **Trigger:** When stubbing return values for mocked dependencies.
- **V1 enforcement text:** Code review. Contract tests verify mock shapes against real responses.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** TEST-EXIST-002, TEST-ISOLATE-002, TEST-MOCK-001, TEST-SNAPSHOT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-NAME-001

- **Domain / severity / mandatory:** testing / medium / false
- **Statement:** Test names describe the scenario and the expected outcome. `test_method_name` is insufficient; `test_method_name_returns_X_when_Y` or BDD-style `should_X_when_Y` is the structure.
- **Trigger:** When naming a test function.
- **V1 enforcement text:** Code review. Related: TEST-ASSERT-002.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** TEST-ASSERT-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-PERF-001

- **Domain / severity / mandatory:** testing / low / false
- **Statement:** Performance-sensitive paths have benchmark tests with a documented baseline. The baseline is checked in CI (or nightly) and regressions exceeding a threshold fail the build.
- **Trigger:** When implementing performance-sensitive code (hot paths, query pipelines, retrieval, ranking, real-time loops).
- **V1 enforcement text:** Benchmark framework (pytest-benchmark, criterion, JMH).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/log_read.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PERF-OPT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-REGRESSION-001

- **Domain / severity / mandatory:** testing / high / false
- **Statement:** Every bug fix is accompanied by a regression test that reproduces the bug. The test fails before the fix and passes after. The bug ticket / commit message references the test.
- **Trigger:** When fixing a bug.
- **V1 enforcement text:** Code review. Bug-tracker template requires a 'test added' field.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 20 file(s)
- **Related rules (conflict candidates):** ENF-PROC-DEBUG-001, ENF-PROC-TDD-001, TEST-EDGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## TEST-SNAPSHOT-001

- **Domain / severity / mandatory:** testing / low / false
- **Statement:** Snapshot updates are explicitly reviewed; blind snapshot regeneration (-u flag in commits without review) is forbidden. The PR description explains why the snapshot changed.
- **Trigger:** When using snapshot testing (Jest snapshots, React Testing Library snapshots, Storybook visual snapshots).
- **V1 enforcement text:** Pre-commit hook. PR template requires snapshot diff acknowledgment.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PROC-REVIEW-001, TEST-MOCK-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
