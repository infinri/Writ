# Rule review batch-04: code-quality (25 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-04.csv](batch-04.csv).

## CLEAN-ASSERT-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Assertions are reserved for invariants -- conditions that the developer believes can never be false. Assertions are not for input validation, control flow, or error handling. Production builds may strip assertions; security-critical or business-critical checks must use explicit raise/throw.
- **Trigger:** When using `assert` statements (or equivalent: console.assert, debug_assert).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PHP-ERR-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-BOOL-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Boolean comparisons to True/False literal are forbidden. Use the value directly: `if active:` not `if active == True:`. Negation: `if not active:` not `if active == False:`.
- **Trigger:** When writing conditional expressions.
- **V1 enforcement text:** Linter rules (pylint comparison-with-callable / eqeq, eslint eqeqeq).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-NAME-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-COMMENT-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Comments explain WHY the code is the way it is (constraint, hidden invariant, surprising business rule, workaround for a known bug). Comments that restate WHAT the next line does in natural language are deleted or rewritten to add intent.
- **Trigger:** When writing inline comments adjacent to code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-FORMAT-001, DOC-INLINE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-COMMENT-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Commented-out code is never committed. Version control is the history; comments are not. Removed code is removed entirely; if it might be needed later, that future-self can recover it from git.
- **Trigger:** When opening a pull request or committing.
- **V1 enforcement text:** Pre-commit hook can flag large commented-out blocks. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-DEAD-001, CLEAN-TODO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-COUPLING-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Modules depend on abstractions (interfaces, protocols, base classes), not concrete implementations. Direct construction of concrete classes inside business logic is a violation; dependencies are injected.
- **Trigger:** When importing or constructing dependencies inside a module.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** SOLID-DIP-001, SOLID-DIP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-COUPLING-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Circular imports or circular module dependencies are forbidden. A reaches B and B reaches A indicates the responsibilities are tangled; extract the shared concept to a third module or invert the dependency.
- **Trigger:** When organizing module dependencies.
- **V1 enforcement text:** Linter rules (ruff cyclic-import, eslint import/no-cycle).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PY-IMPORT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-DEAD-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** No dead code in committed code: no unreachable statements (code after a return, raise, break or continue, or under a condition that is always false), no unused imports, no unused variables, and no unused functions. The linter is configured to flag these as errors.
- **Trigger:** When committing changes that leave dead code behind: statements after a return, raise, break or continue that can never run, unused imports, unused variables, unused functions, or branches that are never taken.
- **V1 enforcement text:** Linter rules (ruff F401/F841, eslint no-unused-vars/no-unreachable, gosec deadcode). CI gate.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** CLEAN-COMMENT-002, ENF-SYS-006
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-ERR-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** No empty catch blocks. Caught exceptions are either re-raised (preserving cause), logged with full context, or converted to a domain-specific error type. Swallowing exceptions silently is forbidden.
- **Trigger:** When writing try/except, try/catch, try/rescue, or any exception-handling block.
- **V1 enforcement text:** Linter rules (eslint no-empty, ruff S110, pylint bare-except, PHPCS Generic.CodeAnalysis.EmptyStatement).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/doctor.py`
- **Rule id referenced in V1 tests:** 7 file(s)
- **Related rules (conflict candidates):** CLEAN-ERR-002, ERR-HANDLE-001, ERR-HANDLE-002, PHP-ERR-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-ERR-002

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Exception types caught are specific. `except Exception`, `catch(Exception)`, `rescue` without a class are forbidden except at top-level boundary handlers (uncaught-error middleware, job runner). Specific catches signal which conditions the code is prepared to handle.
- **Trigger:** When writing exception-handling blocks.
- **V1 enforcement text:** Linter rules.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** CLEAN-ERR-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-ERR-003

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Error messages include context: what was being attempted, the input value (when safe to log), and any relevant identifier. Bare messages like 'Invalid' or 'Failed' are violations.
- **Trigger:** When raising or constructing exception/error objects.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/authoring.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-ERROR-002, ERR-HANDLE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-FORMAT-001

- **Domain / severity / mandatory:** code-quality / low / false
- **Statement:** Formatting is enforced by a configured formatter (Prettier, Black, rustfmt, gofmt, ktfmt). Style-only review comments are forbidden; the formatter wins. Disagreements about formatting are settled by changing the formatter config, not by per-PR debate.
- **Trigger:** When committing or reviewing source code.
- **V1 enforcement text:** Pre-commit hook. CI gate. Related: CLEAN-COMMENT-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-COMMENT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-FUNC-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Functions do one thing. Command-Query Separation: a function either returns a computed value (query) or mutates state (command), never both. Functions that mutate-and-return are split.
- **Trigger:** When writing a function that both returns a value and changes state.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** CLEAN-FUNC-004, CLEAN-SIDE-001, SOLID-SRP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-FUNC-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Functions stay at or below 50 lines of logic (excluding declarations, comments, and blank lines). Longer functions are decomposed into named sub-functions that read as a top-down narrative.
- **Trigger:** When writing or modifying any function or method.
- **V1 enforcement text:** Linter rules (eslint complexity/max-lines-per-function, pylint too-many-statements). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-NEST-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-FUNC-003

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Functions accept at most 4 positional parameters. Beyond that, an options/config object (Pydantic model, dataclass, dict) is passed. Long argument lists indicate the function is doing too much or that several arguments belong together as a named concept.
- **Trigger:** When defining or modifying a function signature.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-DTO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-FUNC-004

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Flag arguments that switch function behavior are forbidden. Split the function into two named functions, one per branch. Distinct behavior belongs to distinct names.
- **Trigger:** When adding a boolean parameter that switches function behavior.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-FUNC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-LOG-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Logs use structured logging (JSON or key-value) with severity levels. `print()`, `System.out.println`, `console.log` for application logs are violations. Use the configured logger with appropriate level.
- **Trigger:** When emitting log output from application code.
- **V1 enforcement text:** Linter rule (ruff T201 for `print`, eslint no-console). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-LOG-002, SEC-INJ-LOG-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-LOG-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Log messages include a correlation/request ID propagated from the inbound request. The ID is set at the entry point (middleware) and attached to every log line for the request, including downstream calls.
- **Trigger:** When emitting logs from request-handling code.
- **V1 enforcement text:** Middleware that injects the correlation ID into the logger's contextvars. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/analysis/instrumentation.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-LOG-001, ERR-HANDLE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-MAGIC-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Magic literals are extracted to named constants with domain context. Numbers like 86400, 1000, 3, strings like 'admin', 'success' are replaced with named constants. Tax rates, fee thresholds, retry counts, status codes all carry names.
- **Trigger:** When using a numeric or string literal in code that has business meaning beyond its raw value.
- **V1 enforcement text:** Linter rules. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DRY-DUP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-NAME-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Identifiers use descriptive, domain-meaningful names. Single-letter names are reserved for loop counters (i, j, k) and short lambda parameters. Abbreviations are reserved for established conventions (id, url, http).
- **Trigger:** When naming a function, variable, class, type, constant, module, or file.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-NAME-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-NAME-002

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Boolean names start with is/has/can/should/needs or read as a predicate (active, enabled). Names that imply nouns (`status`, `flag`) for booleans are violations.
- **Trigger:** When naming a boolean variable, function returning bool, or property of bool type.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-BOOL-001, CLEAN-NAME-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-NEST-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Maximum nesting depth is 3 levels. Deeper code is extracted via early returns, guard clauses, or helper functions. Pyramids of doom are violations.
- **Trigger:** When the body of a function or method exceeds 3 levels of nesting (loops, conditionals, try/with).
- **V1 enforcement text:** Linter rules (eslint complexity/max-depth, pylint too-many-nested-blocks). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** CLEAN-FUNC-002, CLEAN-TERNARY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-RETURN-001

- **Domain / severity / mandatory:** code-quality / medium / false
- **Statement:** Functions return a single, predictable type. A function whose return type varies (string-or-None-or-list) is split or refactored to return an explicit result/optional type. Sentinel return values (`return False on error, return the_value on success`) are forbidden.
- **Trigger:** When a function hands back values of different types on different branches, or signals failure with a sentinel value such as False, -1 or an empty string instead of None, an optional type or a result type.
- **V1 enforcement text:** Type checker (mypy strict, pyright). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** DOC-TYPE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-SIDE-001

- **Domain / severity / mandatory:** code-quality / high / false
- **Statement:** Functions named as getters, computers, builders, or formatters (`get_*`, `compute_*`, `build_*`, `to_*`, `as_*`, `format_*`) have no side effects: no I/O, no mutation of arguments, no state changes. Naming and behavior agree.
- **Trigger:** When writing or naming a function.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** CLEAN-FUNC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-TERNARY-001

- **Domain / severity / mandatory:** code-quality / low / false
- **Statement:** Nested ternaries are forbidden. `a if c else (b if d else e)` is rewritten as an if/elif/else block. Single-level ternary is acceptable when both branches are short.
- **Trigger:** When writing conditional expressions.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-NEST-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## CLEAN-TODO-001

- **Domain / severity / mandatory:** code-quality / low / false
- **Statement:** TODO comments include a tracking reference (issue/ticket number) or an author + date. Bare 'TODO' without context becomes orphaned forever.
- **Trigger:** When writing a TODO, FIXME, HACK, or XXX comment.
- **V1 enforcement text:** Linter rule, code review. Related: CLEAN-COMMENT-002.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-COMMENT-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
