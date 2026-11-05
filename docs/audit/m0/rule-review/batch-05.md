# Rule review batch-05: code-quality, communication, database (24 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-05.csv](batch-05.csv).

## DRY-CONFIG-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Each configuration value has exactly one source of truth (env var, config file, constants module, feature-flag service). Multiple definitions across files are violations; the single definition is the canonical and all callers reference it. The classic drift: the same fee rate or threshold defined once in a REST handler and again in a GraphQL resolver, updated in one and silently stale in the other.
- **Trigger:** When defining configuration values: feature flags, thresholds, environment URLs, retry counts, timeouts, fee rates.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `writ/session/config.py`, `writ/session/log_rotation.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 6 file(s)
- **Related rules (conflict candidates):** DOC-CONFIG-001, DRY-CONFIG-002, DRY-DUP-002, ERR-TIMEOUT-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-CONFIG-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Feature flags are defined in a single registry (feature-flag module, env var, flag service). Inline `if env == 'prod'` or `if VERSION == 'v2'` checks scattered across modules are violations.
- **Trigger:** When introducing or modifying a feature flag.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/analysis/instrumentation.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-ENV-001, ARCH-FEATURE-001, DRY-CONFIG-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-DUP-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Duplicated logic of 5+ substantively identical lines must be extracted to a shared function or module. Cosmetic differences (variable names, formatting) do not exempt duplication. The exemption for one-off scripts is narrow and documented.
- **Trigger:** When the same logic block appears in two or more locations.
- **V1 enforcement text:** Code review. Duplicate-detection tools (jscpd, pylint duplicate-code).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/cli.py`, `writ/session/log_backup.py`
- **Rule id referenced in V1 tests:** 8 file(s)
- **Related rules (conflict candidates):** DRY-DUP-002, DRY-QUERY-001, DRY-TEMPLATE-001, DRY-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-DUP-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Constants used in 2+ locations are centralized in a constants/config module and imported. Repeating a tax rate, a timeout, a status string, an API base URL across files is a violation.
- **Trigger:** When the same constant value (number, string, regex) appears in multiple files.
- **V1 enforcement text:** Code review. Linter rules can flag literal magic numbers (overlaps with CLEAN-MAGIC-001).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 4 file(s): `writ/authoring.py`, `writ/cli.py`, `writ/graph/methodology_ingest.py`, `writ/retrieval/prompt_bundle.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-MAGIC-001, DRY-CONFIG-001, DRY-DUP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-DUP-003

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Validation logic is shared via schemas (Pydantic, Zod, JSON Schema) or validator functions, not copy-pasted across handlers. A given field's rules live in one place.
- **Trigger:** When writing input validation logic.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-DTO-001, PY-PYDANTIC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-QUERY-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Common queries are wrapped in repository or DAO methods, not inlined at every call site. A given query (`active users by tenant`, `unpaid invoices older than N`) lives in one method.
- **Trigger:** When writing database queries from handler or service code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-001, DRY-DUP-001, SEC-AUTHZ-TENANT-001, SOLID-SRP-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-TEMPLATE-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Repeated UI patterns (cards, modals, form rows, error displays) are extracted to shared components, not copied between views. A shared component is the source of truth for the pattern's behavior and styling.
- **Trigger:** When building UI views that repeat the same visual pattern in multiple pages.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** DRY-DUP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DRY-TYPE-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Shared types/interfaces are defined once and imported, not redeclared in each consumer. TypeScript `interface User` repeated in three files is a violation; one definition + three imports is the structure.
- **Trigger:** When declaring types or interfaces consumed by multiple modules.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-DTO-001, DRY-DUP-001, PY-IMPORT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-CIRCUIT-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Repeated failures to an external service trigger a circuit breaker: the breaker opens, subsequent calls fail fast with a fallback or error, the breaker half-opens after a cooldown to probe recovery. Libraries: resilience4j, polly, py-breaker, opossum.
- **Trigger:** When an external dependency fails repeatedly.
- **V1 enforcement text:** Code review. Breaker libraries with metrics.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-001, ERR-FALLBACK-001, ERR-RETRY-001, ERR-RETRY-002, ERR-TIMEOUT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-FALLBACK-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Critical paths have defined fallback behavior, documented in code or comments. The fallback path is tested. 'If the cache is down, use the database' is explicit, not implicit.
- **Trigger:** When implementing critical paths that depend on optional or recoverable inputs (cache, secondary service, personalization).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** ERR-CIRCUIT-001, ERR-RETRY-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-GRACEFUL-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** The application handles SIGTERM and SIGINT gracefully: stops accepting new work, drains in-flight requests within a deadline, flushes buffers, exits cleanly. Hard-kill on signal is a violation.
- **Trigger:** When implementing long-running processes (web servers, workers, daemons, CLI tools that accept signals).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/session/doctor.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** ERR-GRACEFUL-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-GRACEFUL-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Workers have shutdown hooks that complete in-progress work or requeue it. A worker killed mid-job leaves either a half-done state or a re-runnable job; never a silently dropped job.
- **Trigger:** When implementing background jobs, queue workers, or scheduled tasks.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `hooks/scripts/writ-session-end.sh`, `writ/analysis/instrumentation.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ERR-GRACEFUL-001, SCALE-QUEUE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-HANDLE-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Every external call is wrapped in error handling with an explicit timeout. Bare network or file calls without try/except + timeout are violations. The handler logs, maps to a domain error, or retries deliberately.
- **Trigger:** When making any call to an external system: HTTP, database, file I/O, message queue, third-party SDK, IPC.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/git_identity.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-ERR-001, ERR-HANDLE-002, ERR-TIMEOUT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-HANDLE-002

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Errors propagate with context: the wrapping exception preserves the original via `raise ... from e` (Python), `cause:` (JS), `Throwable.initCause` (Java), `wrap` (Go errors.Is/As). Bare re-raise that loses the chain is a violation. The symptom: a postmortem or error tracker shows only the wrapping exception's message and the original upstream error detail is gone by the time it reaches you.
- **Trigger:** When re-raising or wrapping caught exceptions.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-ERR-001, ERR-HANDLE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-HANDLE-003

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** User-facing error messages are helpful and human-readable but never expose internal details: file paths, stack traces, SQL fragments, library names, internal IDs. A correlation ID lets support map the user-facing message to the internal log.
- **Trigger:** When constructing error messages returned to end users.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 4 file(s): `writ/session/log_backup.py`, `writ/session/log_read.py`, `writ/session/log_rotation.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** API-ERROR-001, CLEAN-ERR-003, CLEAN-LOG-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-RETRY-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Retries use exponential backoff with jitter, not fixed intervals. Backoff base is at least 100ms; jitter is random within a window to avoid thundering herd; growth multiplier is 2-3x.
- **Trigger:** When implementing retry logic for external calls or transient failures.
- **V1 enforcement text:** Code review. Retry libraries (tenacity, retry, polly) implement this correctly.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-IDEMPOTENT-001, ERR-CIRCUIT-001, ERR-RETRY-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-RETRY-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Retry attempts are capped (typically 3-5). Infinite retry loops are forbidden. After the cap, the call fails through to the next layer of error handling (fallback, circuit break, error response).
- **Trigger:** When implementing retry logic.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ERR-CIRCUIT-001, ERR-FALLBACK-001, ERR-RETRY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-TIMEOUT-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** All external calls have explicit timeouts. Unbounded waits are forbidden. The library default is not acceptable as documentation: the timeout is passed at the call site or set via shared client config.
- **Trigger:** When making any external call (HTTP, DB, RPC, lock acquisition, queue receive).
- **V1 enforcement text:** Code review. Linter rule (ruff S113 for requests without timeout).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-001, ERR-CIRCUIT-001, ERR-HANDLE-001, ERR-TIMEOUT-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-TIMEOUT-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Timeout values are configurable (env var, config file, settings module). Hardcoded timeouts spread across the codebase are violations; one config controls them by environment and downstream-service.
- **Trigger:** When configuring timeout values for external calls.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** DRY-CONFIG-001, ERR-TIMEOUT-001, SCALE-CONFIG-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ERR-VALIDATION-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Validation errors are returned as structured responses with field-level errors and error codes. A flat string like 'Validation failed' is a violation; the response identifies which field failed, why, and a machine-readable code.
- **Trigger:** When returning validation errors from an API.
- **V1 enforcement text:** Code review. Pydantic + FastAPI produces this shape automatically.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-ERROR-001, API-STATUS-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ENF-COMMS-001

- **Domain / severity / mandatory:** communication / high / false
- **Statement:** Advisory rule: agent responses must not contain performative agreement or unverified success claims. Lexical match against FRB-COMMS forbidden_phrases surfaces violation to friction log.
- **Trigger:** Any time the agent responds to code review, user correction, or technical evaluation input, and the response text contains a forbidden phrase from FRB-COMMS-001 or FRB-COMMS-002.
- **V1 enforcement text:** Advisory only — post-response pattern match. No pre-response deny (blocking every turn on forbidden-phrase detection would cause false positives). Violations collected for Phase 5 review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `writ/export.py`, `writ/graph/integrity/_common.py`, `writ/graph/predicates.py`
- **Rule id referenced in V1 tests:** 10 file(s)
- **Related rules (conflict candidates):** none
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## ENF-COMMS-OUTPUT-001

- **Domain / severity / mandatory:** communication / medium / false
- **Statement:** Output is clear, precise, and to-the-point (understandable by a non-technical or entry-level reader, not only an expert). Lead with the answer (BLUF); give the 'why' as a clause; anchor claims to a concrete reference (file:line or the value); put evidence inline (ran X -> Y -> therefore Z); state the decision and road-not-taken in one line. Use plain language; define a term or acronym the first time it appears; use standard punctuation only (no em dash, no en dash as punctuation, no double hyphen as an em-dash substitute; hyphens only to join words, and commas, colons, semicolons, or parentheses for clause breaks); cut bloat, preamble, restating the question, hedging when you know, option-surveys you will not pursue, narrating what a tool already showed, and any detail irrelevant to the point. Terse means NO BLOAT, never cryptic: keep enough to UNDERSTAND, then stop. Generated code is surgical: the smallest change covering {stated problem x real inputs x codebase altitude}, and for a shared interface do not break the contract for callers you cannot see; no speculative abstraction, defensive code for impossible states, or error handling beyond what was asked. PHASE-CONDITIONED: in plan/investigate modes, calibrated uncertainty and surveying options ARE the deliverable; in work/execution they are waste.
- **Trigger:** Every response to the user, and every block of generated code.
- **V1 enforcement text:** Advisory and dogfooded: Writ injects this contract and the agent's responses follow it; the human gate reviews output against it. The phrase-level floor is enforced by FRB-COMMS-001/002 (no performative agreement / unverified success claims) and claim->evidence by ENF-POST-006. An automated waste-phrase detector is intentionally NOT shipped: a surface detector of a semantic property is high false-positive (prose-only and statement-Jaccard variants were retired in Phase 3). Waste is relational, so it is surfaced for oversight, not auto-cut.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `hooks/scripts/writ-comms-output-gate.sh`, `writ/export.py`, `writ/promotion.py`
- **Rule id referenced in V1 tests:** 7 file(s)
- **Related rules (conflict candidates):** ENF-META-CONCISE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DB-SQL-002

- **Domain / severity / mandatory:** database / medium / false
- **Statement:** Define SQL in a single heredoc or multi-line string. Split only when conditional WHERE clauses or dynamic JOINs require it.
- **Trigger:** When a raw SQL query is constructed using string concatenation (`.` operator in PHP) across more than 2 fragments without conditional logic requiring the split.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** DB-SQL-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## DB-SQL-003

- **Domain / severity / mandatory:** database / medium / false
- **Statement:** Format SQL vertically. Each JOIN on its own line with ON condition. Column lists comma-separated per line. WHERE conditions on separate lines with AND/OR alignment.
- **Trigger:** When writing any SQL query longer than one line.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DB-SQL-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
