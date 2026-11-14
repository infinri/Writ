# Rule review batch-06: documentation (8 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-06.csv](batch-06.csv).

## DOC-API-001

- **Domain / severity / mandatory:** documentation / high / false
- **Statement:** Public API endpoints have request/response documentation that is updated with each change. The documentation source lives alongside the code (OpenAPI generated from Pydantic, GraphQL SDL, framework decorators) and ships with every release.
- **Trigger:** When adding or modifying a public API endpoint.
- **V1 enforcement text:** CI gate: spec generation runs on every PR; diffs are visible.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-CONTRACT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-ARCH-001

- **Domain / severity / mandatory:** documentation / medium / false
- **Statement:** Architecture decisions are recorded in ADRs (Architecture Decision Records) or an equivalent decision log. The ADR documents the context, decision, alternatives considered, and trade-offs.
- **Trigger:** When making a non-trivial architectural decision (choice of database, framework, design pattern, integration approach).
- **V1 enforcement text:** Code review. Repository structure includes an ADR directory. Related: DOC-README-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** DOC-README-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-CONFIG-001

- **Domain / severity / mandatory:** documentation / medium / false
- **Statement:** Configuration options are documented with their defaults, valid ranges, and effects. A new env var or settings flag is accompanied by an entry in the config docs (or in `.env.example` with comments).
- **Trigger:** When introducing or modifying configuration options.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** DRY-CONFIG-001, SCALE-CONFIG-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-INLINE-001

- **Domain / severity / mandatory:** documentation / medium / false
- **Statement:** Complex algorithms have inline comments explaining the approach (the why and the how at a level above the syntax). Comments do not restate the code line by line; they explain the strategy and the constraints.
- **Trigger:** When implementing non-obvious algorithms: graph traversals, ranking formulas, bit manipulation, cryptography, optimization tricks.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-COMMENT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-ONBOARD-001

- **Domain / severity / mandatory:** documentation / low / false
- **Statement:** Advisory only. New-developer onboarding (local setup, test run, deploy to staging, common-task walkthroughs) is documented. Enforced at the repository-integrity-check level rather than per-file (the docs may live across multiple files).
- **Trigger:** When establishing onboarding processes for new developers.
- **V1 enforcement text:** Repository-level integrity check (writ validate). Onboarding feedback as the trigger to update.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** DOC-README-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-README-001

- **Domain / severity / mandatory:** documentation / medium / false
- **Statement:** The repository README includes setup instructions that work on a clean machine: prerequisites, installation, environment setup, how to run tests, how to start the dev server. A new developer can be productive without tribal knowledge.
- **Trigger:** When opening a repository for the first time as a new developer.
- **V1 enforcement text:** Code review on the README. Onboarding checklist verifies the README works.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DOC-ARCH-001, DOC-ONBOARD-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-TYPE-001

- **Domain / severity / mandatory:** documentation / high / false
- **Statement:** Public functions have type annotations on every parameter and the return value. TypeScript types, Python type hints, Go function types, Rust signatures are the default. Untyped public APIs are violations.
- **Trigger:** When writing public functions, methods, or exported APIs.
- **V1 enforcement text:** Type checker (mypy strict, pyright, tsc strict). CI gate.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DOC-TYPE-002, TEST-EXIST-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_

## DOC-TYPE-002

- **Domain / severity / mandatory:** documentation / medium / false
- **Statement:** Return types are explicit. Implicit `Any`, `unknown`, `interface{}`, or unset return types are violations -- the contract is invisible to callers and the type checker.
- **Trigger:** When declaring function or method signatures.
- **V1 enforcement text:** Type checker config (mypy `disallow_untyped_defs`, tsc `noImplicitAny`).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-RETURN-001, DOC-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: general
- **Reviewer verdict:** _not reviewed_
