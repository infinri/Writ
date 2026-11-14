# Rule review batch-08: frameworks, languages, meta-authoring (23 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-08.csv](batch-08.csv).

## FW-M2-001

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** Legitimacy checks must use a repository, service contract, or resource model lookup. String matching, prefix checking, or format inference alone does not validate a domain entity's legitimacy.
- **Trigger:** When writing validation logic that determines if a domain entity (coupon, product, rule, customer group, category) is legitimate.
- **V1 enforcement text:** ENF-PRE-002 (domain invariant declaration) must declare whether validation is format-based or persistence-based. If format validation is intentional, a code comment referencing the business requirement is mandatory.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-PRE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-002

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** All entity retrieval must go through the repository layer. Factory/model-load patterns are forbidden in new code. If no repository method exists for the required lookup, declare the gap and create a repository method -- do not fall back to deprecated patterns.
- **Trigger:** When entity retrieval code contains `Factory->create()->load(`, `Model::load(`, `Model::loadByCode(`, or `Model::loadByAttribute(`.
- **V1 enforcement text:** Magento Coding Standard PHPCS (ENF-POST-007) flags factory/load patterns. PHPStan custom rule.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ENF-CTX-003, ENF-POST-007
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-003

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** Totals-dependent logic must execute after `collect_totals` has run. A `before` plugin must not read totals data. An `around` plugin that needs post-apply state must read data after `proceed()` and re-fetch the quote.
- **Trigger:** When writing a plugin or observer that reads quote totals, subtotal, discount amounts, or any other totals-dependent data.
- **V1 enforcement text:** ENF-PRE-003 (seam justification) must declare what state is available at the interception point.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-PRE-003, FW-M2-006
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-004

- **Domain / severity / mandatory:** frameworks / high / false
- **Statement:** Plugins must target the concrete class, not the interface, unless multi-implementation coverage is explicitly required and justified. Interface-level plugins intercept all implementations, which may include internal framework classes.
- **Trigger:** When writing a `<plugin>` or `<type>` declaration in `di.xml` that targets an interface rather than a concrete class.
- **V1 enforcement text:** ENF-PRE-003 (seam justification must declare why interface vs concrete and which implementations are covered).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-PRE-001, ENF-PRE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-005

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** Every custom totals collector's `collect()` method must be fully idempotent. Calling `collect()` twice must produce identical results. Calling `collect()` after eligibility changes must clear all prior values.
- **Trigger:** When writing or modifying a custom totals collector class (extends `AbstractTotal` or implements `collect()` in the quote totals pipeline).
- **V1 enforcement text:** ENF-PRE-002 (domain invariant -- idempotency checklist). ENF-POST-004 (tests must cover collect-twice scenario and eligibility-change scenario).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-POST-004, ENF-PRE-002, FW-M2-006
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-006

- **Domain / severity / mandatory:** frameworks / high / false
- **Statement:** `CartTotalRepository::get()` only calls `$quote->collectTotals()` for virtual quotes. For non-virtual quotes, it reads previously-collected totals directly. Do not assume REST totals endpoints trigger fresh collection for all quote types.
- **Trigger:** When implementing a feature that depends on fresh totals being available via `GET /V1/carts/mine/totals` (REST) for non-virtual quotes.
- **V1 enforcement text:** Self-enforced via code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** FW-M2-003, FW-M2-005
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-001

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** When MSI allows an order to be placed, salability has been authoritatively decided. Post-placement code must NOT re-evaluate salability. Processing all items from a placed order is always correct.
- **Trigger:** When writing post-order-placement code (observer, consumer, cron) that checks product availability or stock status for an already-placed order.
- **V1 enforcement text:** ENF-SYS-002 (temporal truth source declaration).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-SYS-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-002

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** Features that depend on order state must explicitly declare which states and transitions they handle and which they ignore. Silent assumptions about state transitions are a violation.
- **Trigger:** When writing code that reacts to or depends on Magento order state changes (observers on `sales_order_save_after`, `sales_order_place_after`, order processing consumers).
- **V1 enforcement text:** ENF-SYS-003 (state transition atomicity).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-SYS-003, ENF-SYS-006
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-003

- **Domain / severity / mandatory:** frameworks / high / false
- **Statement:** Store-specific policy values must be declared in `etc/adminhtml/system.xml`, defaulted in `etc/config.xml`, read via `ScopeConfigInterface` with proper scope, and encapsulated in a dedicated Config class.
- **Trigger:** When a value classified as "deployment-variable policy" by ARCH-ENV-001 is hardcoded in a Magento 2 module instead of being sourced from admin configuration.
- **V1 enforcement text:** ARCH-ENV-001 (configuration over hardcoded environment behavior).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/graph/schema.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ARCH-ENV-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-004

- **Domain / severity / mandatory:** frameworks / critical / false
- **Statement:** Every endpoint must use the framework-specific authorization mechanism for its type. REST uses `<resource>` in `webapi.xml`. GraphQL resolvers must check `$context->getUserType()` and verify ownership. Admin controllers must override `_isAllowed()`.
- **Trigger:** When writing a REST endpoint (`webapi.xml`), GraphQL resolver, or admin controller in Magento 2.
- **V1 enforcement text:** ENF-SEC-001 (access boundary declaration). ENF-GATE-007 test skeletons must include unauthorized + ownership violation + valid caller tests.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-GATE-007, ENF-SEC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-005

- **Domain / severity / mandatory:** frameworks / high / false
- **Statement:** Queue-based features must declare all four XML configuration files plus a DLQ exchange. Missing any one of the four files causes silent failures.
- **Trigger:** When building a feature that uses Magento's message queue framework (any of: `communication.xml`, `queue_publisher.xml`, `queue_topology.xml`, `queue_consumer.xml`).
- **V1 enforcement text:** Self-enforced via code review. Static analysis (xmllint plus a custom check for the four XML files) catches missing files. ENF-OPS-002 covers the equivalent runtime invariant.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-OPS-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## FW-M2-RT-006

- **Domain / severity / mandatory:** frameworks / high / false
- **Statement:** Features that interact with inventory must declare which stock is being referenced (default vs MSI), how stock is resolved (StockResolverInterface vs legacy StockRegistryInterface), and whether the feature works in multi-website/multi-stock setups.
- **Trigger:** When writing code that queries stock/inventory data and the Magento installation uses MSI (Multi-Source Inventory) with multiple websites.
- **V1 enforcement text:** ENF-SYS-002 (temporal truth source -- which stock authority?).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-SYS-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PHP-ERR-001

- **Domain / severity / mandatory:** languages / high / false
- **Statement:** Validate preconditions at the start of the function. Throw a specific, meaningful exception immediately on failure. Never return null, false, or a default value to signal an error that should halt execution.
- **Trigger:** When a function or method receives input that could be invalid, or when a precondition must hold for the function to work correctly -- and the code returns a default value or continues silently instead of throwing.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** CLEAN-ASSERT-001, CLEAN-ERR-001, PHP-TRY-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PHP-ERR-002

- **Domain / severity / mandatory:** languages / medium / false
- **Statement:** Error responses must match the execution context. User-facing code returns friendly messages and logs technical details. API code returns structured error responses with HTTP status codes. Background jobs log errors and implement retry/escalation logic. Stack traces and internal details are never exposed to callers.
- **Trigger:** When writing error handling for code that runs in different execution contexts (user-facing storefront, REST API, GraphQL, admin panel, CLI, background job/consumer).
- **V1 enforcement text:** SEC-DATA-MASK-001 (error responses must not expose internals) catches debug info in responses. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SEC-DATA-MASK-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PHP-TRY-001

- **Domain / severity / mandatory:** languages / high / false
- **Statement:** Catch `\Throwable` (not `\Exception`). Always log the error with context before re-throwing or handling. Never swallow exceptions silently.
- **Trigger:** When writing a try-catch block in any PHP file.
- **V1 enforcement text:** PHPMD (ENF-POST-007) flags empty catch blocks.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-POST-007, PHP-ERR-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PHP-TYPE-001

- **Domain / severity / mandatory:** languages / low / false
- **Statement:** Add `/** @var Type $variable */` inline annotation when the type cannot be inferred by static analysis. This eliminates PHPStan mixed-type errors and enables IDE autocompletion.
- **Trigger:** When a variable is assigned from a method call where the return type is not inferrable by static analysis tools (e.g., repository `getById()` returns an interface type that PHPStan reports as `mixed`, or a factory method with no return type declaration).
- **V1 enforcement text:** PHPStan level 8 (ENF-POST-007) catches mixed/unknown type errors. If a PHPStan error exists on a variable access, add the docblock.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-POST-007
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PY-ASYNC-001

- **Domain / severity / mandatory:** languages / critical / false
- **Statement:** Async call chains must use async I/O end-to-end. A sync I/O call inside an async function blocks the event loop, defeating the purpose of async and violating PERF-IO-001.
- **Trigger:** Calling a sync I/O function (requests.get, neo4j.Session.run, open(), subprocess.run) inside an async def function, or using a sync DB driver in an async call chain.
- **V1 enforcement text:** ruff rule ASYNC100 (blocking-http-call-in-async-function). Code review for sync DB driver usage inside async functions.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 4 file(s): `writ/graph/db/_common.py`, `writ/graph/integrity/_common.py`, `writ/server/__init__.py`, `writ/server/routes/session_state.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ARCH-ASYNC-002, PERF-ASYNC-001, PERF-IO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PY-IMPORT-001

- **Domain / severity / mandatory:** languages / high / false
- **Statement:** Circular imports are forbidden. If module A imports from module B and module B imports from module A, extract shared types into a third module that both import from. `TYPE_CHECKING`-guarded imports may be used for annotation-only imports but must never be used at runtime.
- **Trigger:** Any import that would fail at module load time, or any `TYPE_CHECKING`-guarded import used at runtime (not just for annotations).
- **V1 enforcement text:** Python raises `ImportError` on circular imports at module load time. ruff rule TID252 (banned-module-level-imports). Integration tests catch load-time failures.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** CLEAN-COUPLING-002, DRY-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PY-PROTO-001

- **Domain / severity / mandatory:** languages / medium / false
- **Statement:** Pure interfaces (no implementation) must use `typing.Protocol`, not `abc.ABC`. Protocol enables structural subtyping -- a class satisfies the interface by having the right methods, without inheriting from the protocol. ABC is appropriate when the base class provides shared implementation (template method pattern).
- **Trigger:** Abstract base class used solely to define an interface (all methods abstract, no concrete implementation).
- **V1 enforcement text:** Code review. mypy verifies Protocol conformance at type-check time.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/graph/db/_common.py`, `writ/retrieval/embeddings.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ARCH-COMP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## PY-PYDANTIC-001

- **Domain / severity / mandatory:** languages / high / false
- **Statement:** All data entering the system from external sources must be validated through a Pydantic model before use. Direct dictionary access on unvalidated external data is forbidden.
- **Trigger:** Data crossing a trust boundary (HTTP request, JSON file, DB result mapping, CLI input) without Pydantic model validation.
- **V1 enforcement text:** Code review. mypy with pydantic plugin catches unvalidated dict access patterns. FastAPI enforces this automatically for endpoint parameters.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 5 file(s): `writ/graph/ingest.py`, `writ/graph/schema.py`, `writ/retrieval/embeddings.py`, `writ/server/__init__.py`, `writ/server/models.py`
- **Rule id referenced in V1 tests:** 8 file(s)
- **Related rules (conflict candidates):** DRY-DUP-003, ENF-SEC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ENF-META-CONCISE-001

- **Domain / severity / mandatory:** meta-authoring / low / false
- **Statement:** A node body must justify its token cost. The statement is the retrieval surface and carries the rule; the body stays lean. Per-node-type body budgets: Rule/AntiPattern <= 200 words, Technique/Skill <= 320, Playbook <= 600. Cut anything the agent already knows or that another node already says.
- **Trigger:** When authoring or revising any methodology node, before committing it.
- **V1 enforcement text:** Authoring-time word-budget check (tests/test_inc3_authoring_uplift.py::TestConcise asserts authored meta nodes are within budget); reviewers apply the budget to other node types during review.
- **V1 mechanical path:** tests/test_inc3_authoring_uplift.py::TestConcise (per-node-type word-budget check on authored nodes) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 12 file(s)
- **Related rules (conflict candidates):** ENF-COMMS-OUTPUT-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## META-AUTH-001

- **Domain / severity / mandatory:** meta-authoring / high / false
- **Statement:** The trigger field of a node must describe WHEN the node should activate, not WHAT the node does. Description-as-workflow-summary causes Claude to follow descriptions instead of reading skill bodies, fragmenting methodology retrieval at scale.
- **Trigger:** When the agent authors a new Writ Skill, Playbook, or Technique node, and is about to populate the trigger or description field.
- **V1 enforcement text:** Authoring-time review of the trigger field for action-verb / workflow-summary phrasing. A `writ add`/`edit` lint warning on action verbs is planned but not yet implemented.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 6 file(s)
- **Related rules (conflict candidates):** none
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## META-AUTH-002

- **Domain / severity / mandatory:** meta-authoring / high / false
- **Statement:** Skills must pass a pressure test before deployment: apply the baseline scenario, confirm RED (agent violates), write or refine the skill, re-run, confirm GREEN (agent complies). No skill ships without RED-GREEN-REFACTOR applied to itself.
- **Trigger:** When the agent authors a new Writ Skill, Playbook, or Technique and is about to mark it ready for use without running a pressure test against the rule's target behavior.
- **V1 enforcement text:** Authoring-time review for a linked PressureScenario before deploy. A `writ add` advisory check (warn, not block) is planned but not yet implemented.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** none
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_
