# Rule review batch-01: api-design (12 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-01.csv](batch-01.csv).

## API-BREAKING-001

- **Domain / severity / mandatory:** api-design / high / false
- **Statement:** Breaking changes (removed field, renamed field, changed type, removed endpoint, narrowed accepted input) are versioned. Silent contract changes are violations: the old version stays usable until clients migrate.
- **Trigger:** When modifying an existing API endpoint or schema.
- **V1 enforcement text:** Code review. API diff tools (openapi-diff) detect breaking changes in CI.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/server/models.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-CONTRACT-001, API-VERSION-001, PROC-CHANGELOG-001, SCALE-MIGRATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-CONTRACT-001

- **Domain / severity / mandatory:** api-design / high / false
- **Statement:** API request/response schemas are documented via OpenAPI, JSON Schema, GraphQL SDL, or framework-native equivalent. The documentation is generated from the source of truth (Pydantic models, TypeScript types, etc.), not hand-written.
- **Trigger:** When designing or modifying an API endpoint.
- **V1 enforcement text:** CI gate: generated spec is committed; CI verifies it matches code.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-BREAKING-001, API-VERSION-001, ARCH-DTO-001, DOC-API-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-ERROR-001

- **Domain / severity / mandatory:** api-design / high / false
- **Statement:** Error responses follow a consistent schema: error code (machine-readable), message (human-readable), and field-level details where applicable. The schema is documented and applied across every endpoint.
- **Trigger:** When returning error responses from API endpoints.
- **V1 enforcement text:** Code review. Pydantic-based response models enforce the shape.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/budget_tracking.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** API-ERROR-002, API-STATUS-001, ERR-HANDLE-003, ERR-VALIDATION-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-ERROR-002

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** Error messages are actionable: they tell the caller what to fix, not just what failed. 'Invalid input' is a violation; 'Field `email` must be a valid email address' is the structure.
- **Trigger:** When writing error messages returned to API consumers.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/cli.py`, `writ/graph/methodology_ingest.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** API-ERROR-001, CLEAN-ERR-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-IDEMPOTENT-001

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** PUT and DELETE are idempotent: calling the same endpoint twice produces the same final state. PUT replaces; DELETE removes (deleting an already-deleted resource is not an error). Repeated calls do not produce duplicate side effects.
- **Trigger:** When implementing PUT and DELETE endpoints.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** API-REST-001, ARCH-IDEMPOTENT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-PAGINATION-001

- **Domain / severity / mandatory:** api-design / high / false
- **Statement:** List endpoints are paginated with a consistent scheme across the API: cursor-based (preferred for large/changing sets) or offset-based with total count. The pagination metadata (next, prev, total) is included in the response envelope.
- **Trigger:** When implementing list endpoints.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-PAGINATION-002, PERF-QUERY-004, SEC-RATE-QUERY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-PAGINATION-002

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** Default page size is small (10-25). Maximum page size is enforced server-side. Clients cannot bypass the limit by passing a larger `per_page`.
- **Trigger:** When parameterizing page-size on list endpoints.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-PAGINATION-001, PERF-QUERY-004
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-REST-001

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** HTTP methods match semantics: GET for reads (idempotent, no side effects), POST for creates, PUT/PATCH for updates, DELETE for deletes. A GET that mutates state is a violation.
- **Trigger:** When defining HTTP endpoints in a REST-style API.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-IDEMPOTENT-001, API-REST-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-REST-002

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** Resource URLs are nouns. `/users`, `/orders/123`, `/orders/123/cancel`. Verb-style URLs (`/getUsers`, `/deleteOrder`) are violations except for explicit action endpoints (`/orders/123/cancel` is acceptable because it's an action on a noun).
- **Trigger:** When defining URL paths for REST endpoints.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-REST-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-STATUS-001

- **Domain / severity / mandatory:** api-design / high / false
- **Statement:** Response status codes match the outcome: 201 for created, 204 for no-content, 400 for client error, 401 unauth, 403 forbidden, 404 not found, 409 conflict, 422 validation, 5xx for server errors. Returning 200 for failures or 500 for validation is a violation.
- **Trigger:** When returning responses from API endpoints.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-ERROR-001, API-STATUS-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-STATUS-002

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** 5xx codes are never caused by invalid user input. Bad input produces 4xx (typically 400 or 422). 5xx is reserved for server-side faults the user could not have caused (DB down, dependency failure, unhandled exception).
- **Trigger:** When deciding the status code for a handled error.
- **V1 enforcement text:** Code review. Monitoring should track 5xx separately from 4xx.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-STATUS-001, ERR-VALIDATION-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## API-VERSION-001

- **Domain / severity / mandatory:** api-design / medium / false
- **Statement:** API versioning strategy is defined and enforced: URL path (`/v1/`, `/v2/`), header (`Accept: application/vnd.api.v2+json`), or query param. Mixing strategies or omitting versioning is a violation; a documented choice is consistently applied.
- **Trigger:** When designing an API that may change over time.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-BREAKING-001, API-CONTRACT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
