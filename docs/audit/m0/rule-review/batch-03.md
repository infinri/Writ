# Rule review batch-03: architecture (3 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-03.csv](batch-03.csv).

## SOLID-SRP-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Each class has exactly one reason to change. A class that handles both HTTP parsing and business logic, both persistence and validation, both presentation and computation, is split.
- **Trigger:** When designing or modifying a class.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-FUNC-001, SOLID-SRP-002, SOLID-SRP-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-SRP-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Controllers and handlers delegate to a service layer. Business logic does not live in route functions. The handler's job: parse input, call a service, format output.
- **Trigger:** When implementing a route handler, controller method, or HTTP endpoint.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-001, SOLID-DIP-003, SOLID-SRP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-SRP-003

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Data access is separated from business rules via a repository or DAO layer. Services depend on repository interfaces; ORM queries are not inlined into business logic.
- **Trigger:** When organizing data access alongside business rules.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-002, DRY-QUERY-001, SOLID-DIP-001, SOLID-SRP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
