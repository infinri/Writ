# AI Reasoning Discipline

## Purpose

This document defines **mandatory reasoning constraints** the AI must satisfy before, during, and after code generation. These are hard rules, not suggestions. The AI must satisfy each rule before proceeding to the next phase of implementation.

---

## Block 0 -- Task Routing

<!-- RULE START: ENF-ROUTE-001 -->
## Rule ENF-ROUTE-001: Task Complexity Classification

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
When the AI receives any task -- before entering phases, loading phase-specific documents, or generating code.

### Statement
Before entering any phase or generating any code, the AI must classify the task into exactly one tier. The Bible must be consulted for every task regardless of tier -- the tier controls *ceremony*, not *knowledge*.

### Tiers

| Tier | Label | Criteria | Protocol |
|------|-------|----------|----------|
| 0 | **Research** | No code generation. Auditing, investigating, explaining, reviewing, or answering questions about existing code or architecture. | Read relevant Bible docs for the task domain. Deliver findings. No phases, no gates, no slices. |
| 1 | **Patch** | 1-3 files changed. No new interfaces or service contracts. No new state transitions. No new endpoints. Bug fixes, config changes, copy edits, small refactors. | Read relevant Bible docs. Read CORE_PRINCIPLES.md. Write code. Run static analysis (ENF-POST-007). No phases, no gates, no plan.md. |
| 2 | **Standard** | New class or interface, or modifying existing contracts/signatures. Single domain. No concurrency, no queues, no multi-actor writes. | Phases A-C presented as a single combined analysis, one human approval. Test skeletons (ENF-GATE-007). Implementation in a single slice. Static analysis. No Phase D. No ENF-GATE-FINAL. No plan.md. |
| 3 | **Complex** | Multi-domain. Concurrency, state machines, queue consumers, new endpoints, or multi-actor writes. Any Phase D trigger from ENF-SYS-*. | Full protocol: Phases A-D (each a separate gate), test skeletons, verified slices (ENF-GATE-006), post-generation verification, ENF-GATE-FINAL. |

### Violation (bad)
```
User: "Add a plugin to log order saves"
AI: "Here's the plugin code..."
// Skipped classification entirely. No tier declared. No Bible docs consulted.
```

### Pass (good)
```
User: "Add a plugin to log order saves"
AI: "Tier 1 -- Patch. Single file, no new interfaces, no state transitions.
Consulting: FW-M2-004 (plugin targeting), PHP-TRY-001 (try-catch), CORE_PRINCIPLES.md."
```

### Classification procedure
1. Read the task description
2. Identify which Bible documents are relevant (this happens for ALL tiers)
3. Count: files affected, new interfaces, state transitions, endpoints, concurrency concerns
4. Declare the tier and state the reason in one sentence
5. If uncertain between two tiers, choose the higher tier

### Escalation -- tiers only go up, never down
If during implementation the AI discovers the task is more complex than classified:
- A Patch that needs a new interface → escalate to Standard
- A Standard that triggers ENF-SYS-* → escalate to Complex
- State the escalation and new tier explicitly before continuing
- Never downgrade a tier once classified

### Human override
The human may override the classification at any time:
- "Run the full protocol on this" → Complex
- "This is just a patch" → Patch
- "Just look into this for me" → Research

Human override supersedes the AI's classification immediately.

### Enforcement
Self-enforced at session start. If the AI produces Phase A output or implementation code without a tier declaration, it is a constraint violation. The AI must revise and declare the tier first.

### Rationale
Without complexity routing, every task pays the cost of the full protocol. A one-line bug fix should not require six gates with human approval. But every task -- even research -- benefits from the Bible's domain knowledge. The tier system separates ceremony from knowledge. The escalation-only rule prevents optimistic downgrading.
<!-- RULE END: ENF-ROUTE-001 -->

---

## Block 1 -- Mandatory Pre-Implementation Reasoning

<!-- RULE START: ENF-PRE-001 -->
## Rule ENF-PRE-001: Call-Path Declaration Required Before Plugin/Observer Code

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
When the task involves writing a plugin, observer, or event listener in Magento 2 or any framework with context-dependent dispatch.

### Statement
Before writing any plugin, observer, or event listener, the AI must produce a written call-path declaration answering: what is the entry point per execution context, does the same class handle all contexts, will the plugin intercept all required contexts, and which contexts are missed.

### Violation (bad)
```
AI output:
"I'll create an after plugin on CartRepositoryInterface::save() to log quote saves."
// No execution context analysis. Does this cover REST? GraphQL? Admin? CLI?
// No mention of which concrete classes implement the interface.
```

### Pass (good)
```
AI output:
"Call-path declaration:
- Frontend (session): QuoteRepository::save() -- plugin intercepts ✓
- REST (POST /V1/carts): QuoteRepository::save() -- plugin intercepts ✓
- GraphQL (setPaymentMethodOnCart): QuoteRepository::save() -- plugin intercepts ✓
- Admin: AdminQuoteRepository::save() -- DIFFERENT class, plugin does NOT intercept ✗
- CLI: Not applicable -- no CLI quote save path
Gap: Admin quote saves are not covered. Acceptable because requirement is customer-facing only."
```

### Enforcement
ENF-GATE-001 (Phase A gate) -- call-path declaration is the sole output of Phase A. Must be approved before proceeding.

### Rationale
Plugins and observers that appear correct in one context often fail silently in others. A written call-path declaration forces the AI to reason about coverage before committing to an implementation.
<!-- RULE END: ENF-PRE-001 -->

---

<!-- RULE START: ENF-PRE-002 -->
## Rule ENF-PRE-002: Domain Invariant Declaration Required Before Validation Logic

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
When writing any validation method that determines whether a domain entity is legitimate, eligible, or active.

### Statement
Before writing validation logic, the AI must declare: what makes the entity legitimate at the domain level, whether legitimacy is structural (format) or persistence-based (exists in DB), and if persistence-based, which repository or service contract verifies existence.

### Violation (bad)
```
AI output:
"Here's the validation method:"
public function isValid(string $couponCode): bool {
    return str_starts_with($couponCode, 'PROMO_');
}
// No invariant declaration. Is format sufficient? Or must the coupon exist in DB?
```

### Pass (good)
```
AI output:
"Domain invariant declaration:
Coupon legitimacy is persistence-based -- a valid coupon must exist in the sales_rule_coupon
table AND its parent rule must be active. Format alone (prefix, length) is insufficient.
Repository: CouponRepositoryInterface::getByCode() + RuleRepositoryInterface::getById().
Boundary: coupon exists but rule is inactive → invalid."
```

### Enforcement
ENF-GATE-002 (Phase B gate) -- domain invariant declaration is the sole output of Phase B. ENF-POST-002 verifies implementation matches declaration.

### Rationale
Validation logic that infers legitimacy from format alone (string matching, prefix checking) instead of persistence verification is a recurring source of false positives and missed edge cases.
<!-- RULE END: ENF-PRE-002 -->

---

<!-- RULE START: ENF-PRE-003 -->
## Rule ENF-PRE-003: Plugin Seam Justification Required Before di.xml

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: file

### Trigger
When writing a plugin declaration (`<plugin>` or `<type>` with plugin in `di.xml`).

### Statement
Before writing any plugin declaration, the AI must justify: why this specific class (not the interface, not a parent), why this hook type (before/around/after), what state is available at this execution point, and whether the logic depends on data only reliable after a specific side effect.

### Violation (bad)
```
AI output:
"I'll add an around plugin on ShippingInformationManagement::saveAddressInformation
to check the discount amount."
// Why around instead of after? What state is available inside around?
// Does the discount depend on collectTotals running first?
```

### Pass (good)
```
AI output:
"Seam justification:
- Target: ShippingInformationManagement (concrete) -- interface would intercept internal calls
- Hook: after -- we need post-collectTotals state. Using before would read stale totals (FW-M2-003).
  Using around is unnecessary since we don't need to modify the return value or prevent execution.
- State available after proceed(): quote is persisted, collectTotals has run, shipping rates computed.
- Timing dependency: discount amount depends on collectTotals. Confirmed: saveAddressInformation
  calls collectTotals before returning."
```

### Enforcement
ENF-GATE-003 (Phase C gate) -- seam justification is part of Phase C output. Must be approved before implementation.

### Rationale
Incorrect plugin seam selection causes silent failures that are extremely difficult to diagnose. Explicit justification prevents defaulting to the most obvious interception point without verifying it is correct.
<!-- RULE END: ENF-PRE-003 -->

---

<!-- RULE START: ENF-PRE-004 -->
## Rule ENF-PRE-004: API Safety Check Required Before Dependency Injection

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: file

### Trigger
When injecting any class as a constructor dependency into a class that is reachable via REST, GraphQL, or CLI -- especially if the dependency assumes UI context (session, message manager, layout).

### Statement
Before injecting any dependency, verify it is safe in all execution contexts where the class will be invoked. MessageManager, session, or UI-dependent classes injected into service-layer classes are violations unless explicitly justified.

### Violation (bad)
```php
// Service used by REST endpoints -- MessageManager requires session context
class OrderService
{
    public function __construct(
        private readonly OrderRepositoryInterface $orderRepository,
        private readonly ManagerInterface $messageManager // UNSAFE in REST/GraphQL
    ) {}
}
```

### Pass (good)
```php
// All deps are API-safe
class OrderService
{
    public function __construct(
        private readonly OrderRepositoryInterface $orderRepository,
        private readonly LoggerInterface $logger // Safe in all contexts
    ) {}
}
// API safety check: OrderRepositoryInterface (API-safe), LoggerInterface (API-safe).
// No session/UI deps.
```

### Enforcement
ENF-GATE-003 (Phase C gate) -- API safety check is part of Phase C output. Per-slice findings table (ENF-POST-006) must quote constructor params and verify safety.

### Rationale
Dependencies that assume UI context cause fatal errors or undefined behavior in headless execution contexts (REST, GraphQL, CLI). This is a common source of production incidents.
<!-- RULE END: ENF-PRE-004 -->

---

## Block 2 -- Phased Implementation Protocol

<!-- RULE START: ENF-GATE-001 -->
## Rule ENF-GATE-001: Phase A -- Call-Path Declaration Gate

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
When a Tier 3 (Complex) task involves plugins, observers, or event listeners -- after tier classification.

### Statement
The AI must begin with Phase A only: produce the call-path declaration (ENF-PRE-001), present it as the sole output, and halt for human approval. No other phase content or code in the same output.

### Violation (bad)
```
AI output contains both:
"Call-path declaration: [...]"
"Domain invariants: [...]"
// Combined Phase A and Phase B in one output
```

### Pass (good)
```
AI output:
"## Phase A -- Call-Path Declaration
[call-path analysis only]
Awaiting your review before proceeding to Phase B."
```

### Enforcement
Self-enforced. ENF-GATE-004 (anti-collapse) catches combined phases.

### Rationale
Single-phase output prevents front-loading all reasoning in one pass, which collapses review into a rubber-stamp exercise and masks errors in call-path analysis.
<!-- RULE END: ENF-GATE-001 -->

---

<!-- RULE START: ENF-GATE-002 -->
## Rule ENF-GATE-002: Phase B -- Domain Invariant Declaration Gate

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
After receiving human approval of Phase A, when proceeding to domain invariant analysis in a Tier 3 task.

### Statement
The AI must proceed to Phase B only: produce the domain invariant declaration (ENF-PRE-002), present it as the sole output, and halt for human approval.

### Violation (bad)
```
AI output after Phase A approval:
"Domain invariants: [...]
Seam justification: [...]"
// Combined Phase B and Phase C
```

### Pass (good)
```
AI output:
"## Phase B -- Domain Invariant Declaration
[invariant analysis only]
Awaiting your review before proceeding to Phase C."
```

### Enforcement
Self-enforced. ENF-GATE-004 (anti-collapse) catches combined phases.

### Rationale
Domain invariant analysis depends on a reviewed call-path. Presenting it separately ensures the invariant is evaluated against the approved call-path, not a provisional one.
<!-- RULE END: ENF-GATE-002 -->

---

<!-- RULE START: ENF-GATE-003 -->
## Rule ENF-GATE-003: Phase C -- Seam Justification Gate

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
After receiving human approval of Phase B, when proceeding to seam justification in a Tier 3 task.

### Statement
The AI must proceed to Phase C only: produce the seam justification (ENF-PRE-003) and API safety check (ENF-PRE-004), present them as the sole output, and halt for human approval.

### Violation (bad)
```
AI output after Phase B approval:
"Seam justification: [...]
Here's the implementation code: [...]"
// Combined Phase C with implementation
```

### Pass (good)
```
AI output:
"## Phase C -- Seam Justification + API Safety
[seam justification + API safety check only]
Awaiting your review before proceeding to implementation."
```

### Enforcement
Self-enforced. ENF-GATE-004 (anti-collapse) catches combined phases.

### Rationale
Seam justification depends on approved call-paths and domain invariants. Presenting it separately ensures timing and state dependencies are evaluated against the approved analysis chain.
<!-- RULE END: ENF-GATE-003 -->

---

<!-- RULE START: ENF-GATE-004 -->
## Rule ENF-GATE-004: Anti-Collapse -- No Phase Combination or Skipping

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
Continuously monitored during all Tier 2 and Tier 3 tasks.

### Statement
**For Tier 3 (Complex) tasks**, the AI must never combine phases, skip phases, produce code before all phases are approved, or infer approval.

**Tier-aware exceptions** (per ENF-ROUTE-001):
- **Tier 2 (Standard)**: Phases A-C may be combined into a single output with one human approval. This is the designated fast path, not a collapse violation. Phase D, if triggered, still requires a separate gate -- and the task escalates to Tier 3.
- **Tier 0-1 (Research/Patch)**: Phases do not apply. Bible docs are still consulted but no phase gates are required.

### Violation (bad)
```
// Tier 3 task -- AI collapses phases:
"Since the call-path is straightforward, I'll proceed directly to code."
// Skipped Phase B, C, and test skeletons entirely.
```

### Pass (good)
```
// Tier 3 task -- each phase presented individually:
Phase A → [approved] → Phase B → [approved] → Phase C → [approved] → Tests → [approved] → Slice 1

// Tier 2 task -- combined phases are legitimate:
"## Combined Analysis (Phases A-C)
[call-path + invariants + seam justification]
Awaiting your review before proceeding to test skeletons."
```

### Enforcement
Self-enforced. Any output containing content from multiple phases in a Tier 3 task is a constraint violation.

### Rationale
Without anti-collapse, the AI optimizes for output completeness over review quality. This rule prevents regression to single-pass code generation where it matters most (Tier 3), while allowing the fast path for simpler tasks (Tier 2).
<!-- RULE END: ENF-GATE-004 -->

---

<!-- RULE START: ENF-GATE-005 -->
## Rule ENF-GATE-005: Phase D -- System Dynamics Gate (Hard Block)

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
When any ENF-SYS-* rule is triggered (concurrency, state transitions, queues, async processing, multi-website behavior). This forces the task to Tier 3 if not already.

### Statement
When a task triggers any ENF-SYS-* rule, the AI must complete Phase D before producing implementation code. Phase D is a hard blocking gate identical to Phases A-C.

### Violation (bad)
```
"The concurrency model seems straightforward -- just two consumers.
I'll proceed to implementation and handle race conditions inline."
// Skipped Phase D entirely.
```

### Pass (good)
```
"## Phase D -- System Dynamics
Concurrency Model: [ENF-SYS-001 complete]
Temporal Truth Sources: [ENF-SYS-002 complete]
State Machine: [ENF-SYS-003 complete]
Policy vs Mechanism: [ENF-SYS-004 complete]
Integration Reality Check: [ENF-SYS-005 complete]
Awaiting your review before proceeding to test skeletons."
```

### Hard Gate Checklist
Before proceeding past Phase D, ALL must be true:
- [ ] Concurrency model complete (ENF-SYS-001)
- [ ] Temporal truth sources declared (ENF-SYS-002)
- [ ] State transitions defined with atomicity mechanism (ENF-SYS-003)
- [ ] Policy vs mechanism classified (ENF-SYS-004)
- [ ] Integration reality check complete (ENF-SYS-005)
- [ ] Human has explicitly approved Phase D output

### Enforcement
Self-enforced. Producing implementation code before Phase D approval when ENF-SYS-* rules are triggered is a constraint violation.

### Rationale
Without a formal blocking gate, system dynamics analysis degenerates into prose the AI writes and ignores. Phase D must have identical enforcement weight to Phases A-C.
<!-- RULE END: ENF-GATE-005 -->

---

<!-- RULE START: ENF-GATE-006 -->
## Rule ENF-GATE-006: Phased Code Generation -- Verified Slices

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: slice

### Trigger
After all planning phases are approved and code generation begins -- applies to Tier 3 tasks. Tier 2 tasks use a single slice.

### Statement
Code generation must be broken into dependency-ordered verified slices. Each slice covers one dependency layer, is self-validated against the approved plan, and halts for human review before the next slice.

### Violation (bad)
```
AI generates 30 files in one output and calls it "implementation."
No self-validation. No dependency ordering. No per-slice review.
```

### Pass (good)
```
"## Slice 1 -- Schema & Interfaces
Files: db_schema.xml, ReservationInterface.php, ReservationDataInterface.php
Self-validation: interfaces match Phase B domain invariants ✓, schema matches Phase C ✓
Awaiting review before Slice 2."
```

### Slice dependency order
1. **Slice 1 -- Schema & Interfaces**: `db_schema.xml`, service contract interfaces, DTOs
2. **Slice 2 -- Persistence Layer**: Resource models, repositories, collections
3. **Slice 3 -- Domain Logic**: Services, handlers, processors, state machines
4. **Slice 4 -- Integration Layer**: Consumers, observers, plugins, cron, CLI
5. **Slice 5 -- Exposure Layer**: REST endpoints, GraphQL schema/resolvers, admin controllers
6. **Slice 6 -- Configuration & Wiring**: `di.xml`, `events.xml`, `queue_topology.xml`, ACL

### Slice Self-Validation Requirement
At the end of each slice, BEFORE presenting to the human:
- Which approved phase outputs were checked against?
- Were any deviations found? If yes, describe and justify each.
- Does every file reference only types/interfaces from already-approved slices?

### Adaptation
Not every task requires all 6 slices. The AI must declare which slices apply at the start of code generation. For small tasks (1-3 files), slices may be combined -- but the AI must justify the combination and still produce the self-validation statement.

### Enforcement
Self-enforced. Files from multiple dependency layers in a single output without justification is a violation.

### Rationale
Generating all files in one pass is where drift happens. The AI "forgets" earlier approvals as context fills with generated code. Small surfaces with gates prevent error accumulation.
<!-- RULE END: ENF-GATE-006 -->

---

<!-- RULE START: ENF-GATE-007 -->
## Rule ENF-GATE-007: Test-First Gate -- Test Skeletons Before Implementation

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: slice

### Trigger
After all planning phases are approved, BEFORE generating any implementation code -- applies to Tier 2 and Tier 3 tasks.

### Statement
Test skeletons with specific assertions must be generated, written to disk via the Write tool, and approved before any implementation code. Implementation delivered without pre-approved test skeletons is a violation.

### Violation (bad)
```php
// Weak assertions that prove nothing:
$this->assertTrue($result);
$this->assertNotNull($response);

// Or empty stubs:
public function testRelease(): void
{
    // TODO: implement
}

// Or tests shown in chat but not written to disk
```

### Pass (good)
```php
// Specific assertions that serve as executable specifications:
public function testReleaseChangesStatusToReleased(): void
{
    // ... setup ...
    $this->handler->release($reservationId);
    $this->assertEquals('released', $reservation->getStatus());
}

public function testReleaseOnAlreadyReleasedThrows(): void
{
    $this->expectException(AlreadyReleasedException::class);
    $this->handler->release($alreadyReleasedId);
}

public function testReleaseWithDbFailureReturnsSafeDefault(): void
{
    $this->repository->method('getById')->willThrowException(new \RuntimeException());
    $this->expectException(CouldNotSaveException::class);
    $this->handler->release($reservationId);
}
```

### Test Skeleton Requirements
1. **Unit tests** for every service, handler, and domain logic class
2. **Assertion specificity**: assertions must encode approved domain invariants (positive, negative, persistence failure, exception path)
3. **State transition tests** (when Phase D triggered): legal transitions succeed, illegal transitions rejected, concurrent attempts handled
4. **Integration test structure** for ENF-SYS-005 unprovable-by-mocks behaviors
5. **Security boundary tests** for every endpoint: unauthorized rejected, ownership violation rejected, valid caller succeeds
6. **Written to disk**: each test file MUST be written via Write tool -- displaying in chat does not satisfy this gate

### Process Flow
```
Phase A → ✓ → Phase B → ✓ → Phase C → ✓ → [Phase D → ✓] →
Test Skeletons → ✓ (human reviews tests) →
Slice 1 → ✓ → Slice 2 → ✓ → ... → Slice N → ✓
```

### Enforcement
Self-enforced. Implementation code before test skeleton approval is a violation. The AI generates implementation with the directive: "make these approved tests pass."

### Rationale
Tests generated after implementation become afterthought -- they validate what was built, not what was approved. Test-first makes it structurally harder to drift.
<!-- RULE END: ENF-GATE-007 -->

---

## Block 3 -- Post-Generation Verification

<!-- RULE START: ENF-POST-001 -->
## Rule ENF-POST-001: Self-Audit Against Call-Path Declaration

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
After generating all implementation files, before marking the module as complete.

### Statement
The AI must re-read its Phase A call-path declaration and verify that the implementation covers all declared execution contexts. Undeclared coverage gaps are violations.

### Violation (bad)
```
Phase A declared: "Covers frontend, REST, GraphQL, admin."
Implementation: Plugin on QuoteRepository covers frontend, REST, GraphQL.
Admin uses AdminQuoteRepository -- NOT covered. No mention of the gap.
```

### Pass (good)
```
Phase A declared: "Covers frontend, REST, GraphQL, admin."
Implementation: Plugin on QuoteRepository covers frontend, REST, GraphQL.
Known gap: Admin uses AdminQuoteRepository -- documented as out of scope per Phase A approval.
```

### Enforcement
ENF-GATE-FINAL (completion matrix). Per-slice findings table (ENF-POST-006).

### Rationale
Implementation drift from the original call-path declaration is a primary source of incomplete features that pass code review but fail in production.
<!-- RULE END: ENF-POST-001 -->

---

<!-- RULE START: ENF-POST-002 -->
## Rule ENF-POST-002: Self-Audit Against Domain Invariant Declaration

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
After generating all validation logic, before marking complete.

### Statement
The AI must re-read its Phase B domain invariant declaration and verify every validation method satisfies the declared invariant. Format inference where persistence was declared is a violation.

### Violation (bad)
```
Phase B declared: "Coupon legitimacy is persistence-based."
Implementation: if (str_starts_with($code, 'PROMO_')) { return true; }
// Drifted to format-based validation.
```

### Pass (good)
```
Phase B declared: "Coupon legitimacy is persistence-based."
Implementation: $coupon = $this->couponRepository->getByCode($code);
// Matches declaration -- persistence verification.
```

### Enforcement
Per-slice findings table (ENF-POST-006). ENF-GATE-FINAL (completion matrix).

### Rationale
Validation that starts persistence-based in design but drifts to format-based in implementation defeats the purpose of invariant analysis.
<!-- RULE END: ENF-POST-002 -->

---

<!-- RULE START: ENF-POST-003 -->
## Rule ENF-POST-003: Interface Consistency Verification

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: file

### Trigger
After generating a class that implements an interface, or after generating an interface and its implementation.

### Statement
Parameter order, parameter types, and return types in the interface must exactly match the implementation and all call sites.

### Violation (bad)
```php
// Interface:
public function release(int $itemId, string $sku): bool;

// Implementation -- param order SWAPPED:
public function release(string $sku, int $itemId): bool;
```

### Pass (good)
```php
// Interface:
public function release(int $itemId, string $sku): bool;

// Implementation -- matches exactly:
public function release(int $itemId, string $sku): bool;
```

### Enforcement
PHPStan level 8 (ENF-POST-007) catches method signature mismatches. Per-slice findings table (ENF-POST-006).

### Rationale
Interface-implementation mismatches cause subtle runtime errors that surface only when a specific execution path is hit, especially with dependency injection.
<!-- RULE END: ENF-POST-003 -->

---

<!-- RULE START: ENF-POST-004 -->
## Rule ENF-POST-004: Unit Tests Must Cover Domain Invariant -- Hard Gate

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
After generating tests, when verifying test coverage against Phase B domain invariant declarations.

### Statement
Every declared domain invariant must have corresponding test coverage. The AI must refuse to mark implementation as complete if invariants lack tests.

### Required test categories
For every validation method:
1. **Positive case**: valid entity passes
2. **Negative case**: invalid entity fails at exact boundary
3. **Persistence failure case**: DB unavailable, entity not found, rule inactive
4. **Exception path**: unexpected error caught, logged, returns safe default
5. **Idempotency case** (totals collectors): calling collect() twice produces identical results
6. **State reversal case**: conditions that were true become false; all owned state cleaned up

### Violation (bad)
```
Phase B declared 3 invariants:
1. Coupon must exist in DB ← test exists ✓
2. Rule must be active ← NO test
3. Usage limit not exceeded ← NO test
// 2 of 3 invariants untested -- violation.
```

### Pass (good)
```
Phase B declared 3 invariants -- all covered:
1. Coupon must exist: testNonExistentCouponReturnsFalse() ✓
2. Rule must be active: testInactiveRuleCouponReturnsFalse() ✓
3. Usage limit: testCouponAtUsageLimitReturnsFalse() + testCouponBelowLimitReturnsTrue() ✓
```

### Enforcement
ENF-GATE-007 (test skeletons must encode invariants). ENF-GATE-FINAL (completion matrix maps invariants to tests).

### Rationale
Happy-path-only tests create false confidence. Without a hard gate tying tests to declared invariants, the AI consistently under-tests edge cases.
<!-- RULE END: ENF-POST-004 -->

---

<!-- RULE START: ENF-POST-005 -->
## Rule ENF-POST-005: Boundary Values Must Be Tested Explicitly

**Domain**: AI Enforcement
**Severity**: High
**Scope**: file

### Trigger
When the implementation contains threshold-based logic (item count, subtotal, quantity, date comparisons) and tests exist for that logic.

### Statement
For any threshold, tests must cover: exactly at the threshold (must NOT trigger), one unit above (must trigger), and well above (must trigger).

### Violation (bad)
```php
// Threshold: minimum 5 items for bulk discount
// Tests only cover "clearly above" and "clearly below":
testBulkDiscountWith3Items()  // below -- pass
testBulkDiscountWith10Items() // above -- pass
// Missing: exactly 5 (boundary) and exactly 6 (one above)
```

### Pass (good)
```php
testBulkDiscountWith4Items()  // below threshold -- no discount
testBulkDiscountWith5Items()  // AT threshold -- no discount (boundary)
testBulkDiscountWith6Items()  // one above -- discount applied
testBulkDiscountWith20Items() // well above -- discount applied
```

### Enforcement
Per-slice findings table (ENF-POST-006). Code review of test files.

### Rationale
Off-by-one errors at boundaries are among the most common bugs in threshold-based logic.
<!-- RULE END: ENF-POST-005 -->

---

<!-- RULE START: ENF-POST-006 -->
## Rule ENF-POST-006: Structured Findings Table -- Per-File Rule Audit

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: slice

### Trigger
After generating each code slice (per ENF-GATE-006).

### Statement
The AI must produce a structured findings table -- not prose. Self-reporting in natural language is not enforcement; it is unverifiable narrative. Evidence must be a direct quote from the generated code.

### Evidence Standard
**"I believe it complies" is not acceptable.** Evidence must be the specific line(s) that satisfy or violate the rule.

- **Checklist** (not acceptable): "All injected deps are interfaces; no session/UI deps"
- **Audit** (required): `__construct(ReservationRepositoryInterface $repo, LoggerInterface $logger)` -- line 23. All constructor params are interfaces.

### Violation (bad)
```
"I checked for load(), no results found. All dependencies look correct."
// Narrative self-reporting -- unverifiable.
```

### Pass (good)
```
File: Model/ReservationHandler.php
Applicable rules: ENF-PRE-004 (has constructor), ENF-SYS-003 (has state transition)
Not applicable: ENF-SEC-001 (not an endpoint)

| File | Rule | Violation? | Quoted Evidence |
|------|------|------------|-----------------|
| ReservationHandler.php | ENF-PRE-004 | No | `__construct(ReservationRepositoryInterface $repo, LoggerInterface $logger)` -- line 15. No session/UI deps. |
| ReservationHandler.php | ENF-SYS-003 | No | `UPDATE...SET status='released' WHERE status='reserved' AND id=:id` -- line 47. CAS pattern. |
```

### Required table format
| File | Rule | Violation? | Quoted Evidence |
|------|------|------------|-----------------|

### Per-File Rule Identification
Before filling the table, list which rules apply to each file and justify why others don't apply.

### Mandatory checks per file
1. **Interface adherence**: calls only methods declared in injected interfaces?
2. **State transition atomicity**: uses declared atomicity strategy from ENF-SYS-003?
3. **Ownership enforcement**: endpoint enforces ownership before data access (ENF-SEC-001)?
4. **Dependency safety**: all injected dependencies safe in all contexts (ENF-PRE-004)?
5. **Domain invariant compliance**: validation uses persistence where Phase B declared it (ENF-PRE-002)?
6. **Plan alignment**: behavior matches approved Phases A-D?

### The "I Cannot Verify" Rule
If the answer to any check is "I cannot verify," the AI must:
1. State "I cannot verify" in the Evidence column
2. Halt and flag for human review
3. NOT proceed to the next slice

### Enforcement
Self-enforced. Any slice delivered without a findings table is a violation. After fixing a violation, the full table must be regenerated -- no patching single rows.

### Rationale
A structured table with per-file, per-rule evidence converts self-reporting into an auditable artifact. The "I cannot verify" escape forces the AI to admit limits rather than paper over gaps.
<!-- RULE END: ENF-POST-006 -->

---

<!-- RULE START: ENF-POST-007 -->
## Rule ENF-POST-007: Static Analysis Gate -- Tool Verification Required

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: slice

### Trigger
After generating code, before marking a slice as complete.

### Statement
Generated code must be validated by static analysis tools. The AI's own reasoning is necessary but not sufficient. PHPStan at level 8 is the minimum for PHP.

### Violation (bad)
```
plan.md contains:
"## Pending tasks
- ENF-POST-007: Static analysis -- to be run post-deployment"
// Static analysis deferred -- NEVER acceptable.
```

### Pass (good)
```
"Static analysis results:
PHPStan level 8: 0 errors
PHPCS Magento Coding Standard: 0 errors
All files validated."
```

### Required tools (PHP / Magento 2)
1. **PHPStan (level 8+)**: type errors, method-not-found, incorrect return types, undefined variables
2. **PHPMD**: duplicated logic, excessive complexity, unused parameters
3. **Magento Coding Standard (PHPCS)**: missing `@api` annotations, ObjectManager usage, session deps in services

### Execution
1. Run tools (or request human to run them)
2. Paste output inline
3. Address every error -- fix or justify as false positive
4. Re-run until zero errors

### Halt Condition
Any PHPStan error at level 8 = halt. Cannot approve own code while errors exist.

### When Tools Are Not Available
1. State explicitly: "Static analysis tools are not available."
2. Perform manual static analysis reasoning
3. Flag as: "**Pending static analysis validation** -- tool confirmation required before production."

### Pending Tasks Prohibition
ENF-POST-007 may **never** appear in a "pending tasks" list in plan.md. The enforce-final-gate.sh hook blocks plan.md writes when this pattern is detected.

### Enforcement
enforce-final-gate.sh hook. Per-slice validation. Self-enforced halt on errors.

### Rationale
Static analysis tools are an independent verifier with no reasoning bias. PHPStan catches entire categories of bugs that the AI consistently misses because it "knows what it meant."
<!-- RULE END: ENF-POST-007 -->

---

<!-- RULE START: ENF-POST-008 -->
## Rule ENF-POST-008: Operational Proof Trace -- Config-to-Enforcement Path

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
After generating code that makes operational claims about retry logic, dead-letter queues, backoff, max retries, escalation, or idempotency.

### Statement
For every operational claim, the AI must produce a proof trace -- the exact code path from configuration read to runtime enforcement. A claim without a complete trace is unproven.

### Violation (bad)
```
Claim: "Failed messages retry 3 times before DLQ"
Proof trace:
  1. Config: system.xml path "section/group/max_retries", default = 3 ✓
  2. Config read: Config\RetryConfig::getMaxRetries() ✓
  3. Injection: RetryConfig injected into ConsumerHandler ← NOT FOUND in di.xml
  Status: BROKEN at step 3 -- config is never injected into the consumer
```

### Pass (good)
```
Claim: "Failed messages retry 3 times before DLQ"
Proof trace:
  1. Config: system.xml "section/group/max_retries", default = 3
  2. Config read: Config\RetryConfig::getMaxRetries() reads via ScopeConfigInterface
  3. Injection: RetryConfig injected into ConsumerHandler via di.xml
  4. Enforcement: ConsumerHandler::process() line 52:
     if ($message->getDeliveryCount() >= $this->retryConfig->getMaxRetries()) {
         $this->deadLetterPublisher->publish($message);
         return;
     }
  5. DLQ publish: DeadLetterPublisher::publish() writes to 'module.dlq' exchange
  6. DLQ consumer: queue_consumer.xml declares consumer for 'module.dlq'
  Status: PROVEN -- complete path from config to enforcement
```

### Required traces
Produce proof traces for ALL of:
1. **Retry logic**: config → read → count check → action
2. **Dead-letter queue**: DLQ exchange → binding → consumer → escalation
3. **Backoff/delay**: config → calculation → sleep/reschedule
4. **Max attempts**: config → counter increment → max check → halt
5. **Escalation**: trigger condition → notification → recipient config
6. **Idempotency**: unique key → duplicate check → skip/merge

### Enforcement
ENF-GATE-FINAL verifies all operational claims have complete traces. Per-slice findings table (ENF-POST-006).

### Rationale
The audit that exposed this gap: plan said "max_retries config exists" -- and it did -- but nothing read it. The config was declared, the constant defined, and it looked complete. But the code path from config read to enforcement was broken. Proof traces force the AI to find its own dead code.
<!-- RULE END: ENF-POST-008 -->

---

## Block 4 -- Context Retrieval Discipline

<!-- RULE START: ENF-CTX-001 -->
## Rule ENF-CTX-001: Retrieve Only Task-Relevant Context

**Domain**: AI Enforcement
**Severity**: High
**Scope**: session

### Trigger
Before each implementation phase, when the AI is about to load context documents or Bible files.

### Statement
The AI must explicitly identify which documents are relevant to the current task and load only those. Passive scanning of full digests or loading all Bible documents is forbidden.

### Violation (bad)
```
AI loads entire ai_digest (50+ files) at session start.
Or AI loads all 13 Bible documents for a simple PHP bug fix.
```

### Pass (good)
```
"For this task (fix PHP error in order service), loading:
- PHP-TRY-001 (try-catch standards)
- PHP-ERR-001 (fail fast)
- CORE_PRINCIPLES.md
Not loading: Magento queue rules, architecture principles, security boundaries -- not relevant."
```

### Enforcement
Self-enforced. ENF-CTX-004 (context pressure) catches excessive context loading.

### Rationale
Passive absorption of large context windows leads to hallucinated connections and missed critical details. Explicit retrieval forces targeted reasoning.
<!-- RULE END: ENF-CTX-001 -->

---

<!-- RULE START: ENF-CTX-002 -->
## Rule ENF-CTX-002: Missing Context Must Halt Implementation

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
When the AI is about to assert a fact about execution flow, entity relationships, or API behavior that it has not verified from source code or context data.

### Statement
The AI must not fill missing context with training data assumptions. Every factual assertion in a call-path or architecture declaration must be tagged `[verified: source_file]` or `[unverified: needs confirmation]`.

### Violation (bad)
```
"CartRepositoryInterface::save() calls collectTotals() internally."
// Stated as fact -- but was this read from source, or guessed from training data?
```

### Pass (good)
```
"CartRepositoryInterface::save() calls collectTotals() internally.
[verified: vendor/magento/module-quote/Model/QuoteRepository.php line 142]"

// Or if source not available:
"CartRepositoryInterface::save() may call collectTotals() internally.
[unverified: needs confirmation -- source not in context]"
```

### Verification Checklist
Before asserting any of the following, the AI must have read the source or found it in context data:
1. "Class X calls method Y" → read source or call_graph.json
2. "GraphQL field Y is resolved by class Z" → read schema.graphqls
3. "Repository method returns N queries" → read implementation or flag unverified
4. "Extension attribute A is populated by class B" → read class B's source
5. "REST endpoint returns field X from source Y" → read service contract implementation
6. "Area code check prevents execution in context C" → verify App\State is injectable

### Enforcement
Self-enforced. Confident prose that is actually a training-data guess is the most dangerous form of this violation.

### Rationale
Training data assumptions are the primary source of plausible-looking but incorrect implementations. Halting on missing context is safer than generating code based on guesses.
<!-- RULE END: ENF-CTX-002 -->

---

<!-- RULE START: ENF-CTX-003 -->
## Rule ENF-CTX-003: Resist Training Data Bias for Deprecated Patterns

**Domain**: AI Enforcement
**Severity**: High
**Scope**: file

### Trigger
When generated code uses patterns that appear frequently in training data but conflict with the project's rules -- specifically: factory patterns over repositories, Model::load() over service contracts, string inference over persistence verification.

### Statement
Any pattern from training data that conflicts with the Bible's rules is a violation. The project's rules override training data patterns.

### Violation (bad)
```php
// Training-data bias -- factory/load pattern appears in millions of Magento examples
$product = $this->productFactory->create()->load($productId);
// Violates FW-M2-002
```

### Pass (good)
```php
// Project rule takes precedence
$product = $this->productRepository->getById($productId);
```

### Enforcement
Magento Coding Standard PHPCS (ENF-POST-007). Per-slice findings table (ENF-POST-006). FW-M2-002 catches factory/load patterns.

### Rationale
AI models are statistically biased toward deprecated patterns that appear frequently in older codebases. Explicit resistance prevents regression to outdated practices.
<!-- RULE END: ENF-CTX-003 -->

---

<!-- RULE START: ENF-CTX-004 -->
## Rule ENF-CTX-004: Context Pressure Limits on Gate Execution

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: session

### Trigger
At every ENF-GATE halt point (Phase A through FINAL) and whenever context usage approaches thresholds.

### Statement
Context pressure silently degrades verification quality. Hard limits are required:
- **At 75% context**: spawn slice-builder for remaining implementation slices. Main session does gate reviews only.
- **Before ENF-GATE-FINAL**: context must be below 70%. If above, spawn a fresh session.
- **At every gate halt**: append metrics to `{PROJECT_ROOT}/.claude/session-metrics.md`.

### Violation (bad)
```
AI runs ENF-GATE-FINAL at 85% context.
Completion matrix shows all OK -- but at 85%, verification quality is degraded.
// Constraint violation even if output looks correct.
```

### Pass (good)
```
"Context: 72%. Above 70% threshold for ENF-GATE-FINAL.
Spawning fresh session with plan.md path + generated file manifest only.
Fresh session will run ENF-GATE-FINAL at ~5% context."
```

### Session metrics format
At every gate halt, append to `{PROJECT_ROOT}/.claude/session-metrics.md`:
```
## Gate: [gate-name] -- [timestamp]
Context: [N]% ([tokens] tokens)
```

### Enforcement
Self-enforced. enforce-final-gate.sh checks for gate-final.approved. If token metrics are unavailable, assume HIGH and spawn fresh session.

### Rationale
At 93% context in the LoyaltyRewards audit, the session missed zero test files on disk and a missing class in schema.graphqls. The output looked complete. A compressed findings table at 90% context is indistinguishable from a thorough one at 30% context.
<!-- RULE END: ENF-CTX-004 -->

---

<!-- RULE START: ENF-GATE-FINAL -->
## Rule ENF-GATE-FINAL: Plan-to-Code Completeness Verification

**Domain**: AI Enforcement
**Severity**: Critical
**Scope**: module

### Trigger
After all implementation slices are approved and before the module is declared complete -- Tier 3 tasks only.

### Statement
Invoke plan-guardian with the full plan.md and ALL generated file paths. The agent produces a completion matrix mapping every capability from Phases A-D to the specific file and method implementing it. Any MISSING row = module is INCOMPLETE.

### Violation (bad)
```
Completion matrix:
| Capability | File | Method | Status |
|---|---|---|---|
| Reserve inventory | ReservationHandler.php | reserve() | OK |
| Release inventory | -- | -- | MISSING |
AI: "Module is complete. Release will be added in a future iteration."
// MISSING row declared complete -- violation.
```

### Pass (good)
```
Completion matrix: zero MISSING rows.
Context: 45% -- below 70% threshold.
All 4 passes (capability, filesystem, dependency, gate status) passed.
→ touch gate-final.approved
→ write plan.md to app/code/Vendor/Module/plan.md
```

### Specific checks
1. Every state declared in Phase D has at least one code path that transitions INTO it (ENF-SYS-006)
2. Every operational claim has a complete proof trace (ENF-POST-008)
3. Every API endpoint declared in Phase A has a corresponding implementation
4. Every integration declared in Phase C has a corresponding implementation
5. **Filesystem verification**: every file in plan manifest exists on disk
6. **Dependency scan**: every class reference in generated files exists on disk
7. **Context gate**: must be below 70% context (ENF-CTX-004)

### Mechanical enforcement
The enforce-final-gate.sh hook blocks writing plan.md until `{PROJECT_ROOT}/.claude/gates/gate-final.approved` exists. The hook also scans plan.md content for any ENF-POST rule marked as pending -- blocks with: "GATE BLOCKED: plan.md contains pending ENF-POST items."

### Invocation
`Use plan-guardian to verify ALL slices against plan.md`
Zero MISSING rows → touch gate-final.approved → write plan.md

### Enforcement
enforce-final-gate.sh hook. plan-guardian agent runs all four verification passes via bin/ scripts.

### Rationale
'Planned for future iteration' is not acceptable if it was approved in the plan. Every approved capability must have a corresponding implementation.
<!-- RULE END: ENF-GATE-FINAL -->
