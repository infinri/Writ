# Rule review batch-02: architecture (25 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-02.csv](batch-02.csv).

## ARCH-ASYNC-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Async function results are awaited at all call sites. Fire-and-forget invocations (unawaited Promises, dropped coroutines, ignored futures) are forbidden unless explicitly justified and tracked (background task registry).
- **Trigger:** When calling an async function (Python `async def`, Node `async`/`Promise`, Rust `async fn`).
- **V1 enforcement text:** Linter rule (eslint @typescript-eslint/no-floating-promises, ruff RUF006 / asyncio).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-ASYNC-002, PERF-BATCH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-ASYNC-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Blocking calls inside the event loop are forbidden: no synchronous I/O, no `time.sleep` (use `asyncio.sleep`), no CPU-bound loops in the main coroutine. CPU-bound work runs in an executor; blocking I/O runs in a thread pool.
- **Trigger:** When writing code that runs inside an async event loop (Python asyncio, Node, Tokio).
- **V1 enforcement text:** Linter rule (asyncio-blocker-style check). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-ASYNC-001, PERF-ASYNC-001, PY-ASYNC-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-BOUNDARY-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** External service calls are wrapped in an adapter/client class. Business logic invokes the adapter, never the raw HTTP/SDK call. The adapter centralizes retries, timeouts, error mapping, and observability.
- **Trigger:** When integrating with external services (HTTP APIs, third-party SDKs, message queues, cloud services).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `writ/session/bitbucket_client.py`, `writ/session/git_identity.py`, `writ/session/pr_host.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-002, DRY-QUERY-001, ERR-CIRCUIT-001, ERR-TIMEOUT-001, SOLID-DIP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-BOUNDARY-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Third-party library usage is concentrated in adapter modules. Imports of the library are confined; the rest of the codebase imports the adapter. Swapping the library affects one module, not the codebase.
- **Trigger:** When using a third-party library across multiple modules.
- **V1 enforcement text:** Code review. Import linting can restrict imports of specific libraries to specific modules.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-001, SOLID-DIP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-COMP-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Class inheritance depth must not exceed 2 levels of project code. Deeper hierarchies must be refactored to use composition via constructor injection.
- **Trigger:** Class hierarchy depth exceeds 2 levels of project code. Language/framework base classes (`ABC`, `Protocol`, `BaseModel`, `AbstractController`, `AbstractPlugin`) are excluded from the count.
- **V1 enforcement text:** Code review. Check class hierarchy depth during PR review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** PY-PROTO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-DTO-001

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Data carriers are explicit DTOs (dataclasses, Pydantic models, Zod schemas, TypeScript types). Raw dicts passed through layers are violations: they lose type information, schema validation, and IDE support.
- **Trigger:** When passing data between layers (HTTP <-> service, service <-> data access, service <-> external).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** API-CONTRACT-001, ARCH-STATE-002, CLEAN-FUNC-003, DRY-DUP-003, DRY-TYPE-001, SEC-VAL-TYPE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-ENV-001

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Environment-specific behavior is controlled by configuration values, not by `if env == 'prod'` branches in code. Per-environment config files / env vars carry the differences; code reads config, not the environment name.
- **Trigger:** When implementing behavior that should differ across environments (dev, staging, prod).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** DRY-CONFIG-002, FW-M2-RT-003, SCALE-CONFIG-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-EVENT-001

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Cross-domain communication uses events or messages, not direct method calls between bounded contexts. The sender publishes; the receiver subscribes. Coupling is one-way: events carry data, not references to receivers.
- **Trigger:** When two bounded contexts (subdomains, modules with separate responsibilities) need to communicate.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SCALE-QUEUE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-FEATURE-001

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Feature flags have an explicit expiration date and a documented cleanup plan (who removes the flag, when, and what the cleanup looks like). Permanent feature flags are violations: they accumulate as conditional spaghetti.
- **Trigger:** When introducing or removing a feature flag.
- **V1 enforcement text:** Flag registry with TTL/owner fields. PR template requires sunset date.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** DRY-CONFIG-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-IDEMPOTENT-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Write endpoints are idempotent: replaying the same request produces the same result, not duplicate side effects. Idempotency is implemented via an idempotency key supplied by the client and stored server-side until expiration.
- **Trigger:** When implementing a write endpoint that creates resources or triggers side effects (charges, emails, notifications).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** API-IDEMPOTENT-001, ERR-RETRY-001, SCALE-QUEUE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-LAYER-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Layer boundaries are enforced: presentation calls service, service calls data-access. Layers are not skipped (no controller talking directly to the ORM, no data-access calling the service layer). The dependency graph flows in one direction.
- **Trigger:** When designing or modifying multi-layer applications (presentation, service, data-access, infrastructure).
- **V1 enforcement text:** Code review. Module-import linting (eslint import/no-restricted-paths, pylint).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 4 file(s): `writ/session/log_backup.py`, `writ/session/log_read.py`, `writ/session/log_rotation.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-002, PROC-PLAN-001, SOLID-SRP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-LAYER-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Domain models have no framework imports. Django ORM Model, SQLAlchemy Base, Spring @Entity, Active Record annotations are framework concerns; the domain model is plain data + behavior. Framework types are converted at boundaries.
- **Trigger:** When defining domain models (entities, value objects, aggregates).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-001, SOLID-DIP-003, SOLID-SRP-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-MIGRATION-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Schema changes use versioned migrations (Alembic, Django migrations, Flyway, Liquibase, Rails migrations, Doctrine migrations). Manual DDL applied to production environments outside the migration system is a violation.
- **Trigger:** When changing the database schema.
- **V1 enforcement text:** CI gate that migrations run cleanly against a fresh database. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-MIGRATION-002, PROC-DEPLOY-001, SCALE-MIGRATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-MIGRATION-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Migrations are reversible: both up and down are implemented. Irreversible changes (DROP COLUMN with data, DROP TABLE) are documented with the rationale in the migration file. Reversibility supports staged rollouts and incident rollback.
- **Trigger:** When writing a database migration.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-MIGRATION-001, PROC-ROLLBACK-001, SCALE-MIGRATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-STATE-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Shared mutable state is protected by explicit synchronization (locks, mutexes, atomic operations) or replaced by message-passing / immutable updates. Unguarded global mutation in concurrent code is a violation.
- **Trigger:** When modifying shared mutable state (module-level variables, singletons, in-memory caches, class-level state) from multi-threaded or async code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** PERF-CACHE-004, TEST-EDGE-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## ARCH-STATE-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** A single state-management pattern (Redux, Zustand, MobX, React Context, Vuex, Pinia, NgRx) is used per project. Mixing two patterns scatters state across incompatible mental models. The choice is documented and consistent.
- **Trigger:** When introducing state management for application UI.
- **V1 enforcement text:** Code review. ESLint rule restricting imports of competing libraries. Related: ARCH-DTO-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-DTO-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-DIP-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** High-level modules depend on abstractions (protocols, interfaces, ABCs), not on concrete implementations. The abstraction is owned by the high-level module; the implementation conforms to it.
- **Trigger:** When a high-level module (service, business logic) depends on a low-level module (database driver, HTTP client, file system).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/log_rotation.py`
- **Rule id referenced in V1 tests:** 4 file(s)
- **Related rules (conflict candidates):** ARCH-BOUNDARY-001, ARCH-BOUNDARY-002, CLEAN-COUPLING-001, SOLID-DIP-002, SOLID-OCP-001, SOLID-SRP-003
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-DIP-002

- **Domain / severity / mandatory:** architecture / critical / false
- **Statement:** Dependencies are received via constructor injection, typed to an interface or protocol. Direct instantiation (`new ConcreteClass()`) inside a controller or service-layer class is permitted only for DTOs, value objects, and known framework types. Application services and controllers receive their dependencies; they do not construct them.
- **Trigger:** When constructing a controller, route handler, or service-layer class that requires collaborators (services, repositories, gateways, clients) -- including any time such a class instantiates ("news up") its own dependencies with `new ConcreteClass()` instead of receiving them via constructor injection.
- **V1 enforcement text:** Code review. DI container config (Spring, Magento DI, Symfony DI, Dagger).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/analysis/friction.py`, `writ/shared/logging.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** CLEAN-COUPLING-001, SOLID-DIP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-DIP-003

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Framework-specific types do not leak into domain logic. Django QuerySets, Express Request objects, SQLAlchemy Session, ORM model classes are not consumed by domain services directly; they are adapted at the boundary.
- **Trigger:** When designing service-layer / domain-layer code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-002, SOLID-SRP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-ISP-001

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Interfaces contain only methods that all their consumers use. A 'fat interface' that some consumers ignore signals the interface should be split into smaller cohesive interfaces.
- **Trigger:** When defining an interface, protocol, or abstract base class.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SOLID-ISP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-ISP-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** If an implementation raises NotImplementedError for some interface methods, that signals the interface should be split. The pattern indicates a fat-interface design problem (overlaps with SOLID-ISP-001).
- **Trigger:** When implementing an interface or abstract base class.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/pr_host.py`
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SOLID-ISP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-LSP-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Subclasses or implementations do not weaken preconditions (require less than the parent) or strengthen postconditions (return less than the parent guarantees). A consumer of the parent type works correctly with any subclass.
- **Trigger:** When implementing a subclass or interface implementation.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/pr_host.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SOLID-LSP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-LSP-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Overridden methods maintain the parent's return type contract. A method declared to return `User` does not return `None` in the override. A method declared to return non-empty list does not return `[]`. The contract is documented or implied; the override respects it.
- **Trigger:** When overriding a method in a subclass.
- **V1 enforcement text:** Type checker (mypy strict, pyright). Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ENF-POST-003, SOLID-LSP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-OCP-001

- **Domain / severity / mandatory:** architecture / high / false
- **Statement:** Behavior is extended via composition, strategy, plugin/extension hooks, or framework-native mechanisms (decorators, observers, middleware). Modifying existing code to support a new variant is a violation; new variants are added without changing the old code path.
- **Trigger:** When extending behavior of existing modules, framework classes, or third-party libraries.
- **V1 enforcement text:** Code review. Framework-specific extension hooks (Magento plugins, Django middleware, FastAPI dependencies, Spring AOP).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SOLID-DIP-001, SOLID-OCP-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SOLID-OCP-002

- **Domain / severity / mandatory:** architecture / medium / false
- **Statement:** Switch/match on a type with more than 3 branches is replaced by polymorphism (subclass dispatch, strategy registry, visitor pattern) when the type set is extensible. Chains that hard-code every type prevent additions without editing every chain.
- **Trigger:** When writing a switch/match or chain of if/elif branches on a type discriminator.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/graph/methodology_ingest.py`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SOLID-OCP-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
