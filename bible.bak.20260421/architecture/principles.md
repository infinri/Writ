# Architecture Principles

## Purpose

This document defines **architectural principles and design patterns** that govern how code is organized, extended, and maintained in this codebase.

---

<!-- RULE START: ARCH-ORG-001 -->
## Rule ARCH-ORG-001: Code Organization -- Layer Separation

**Domain**: Architecture
**Severity**: High
**Scope**: module

### Trigger
When creating or modifying a class that contains logic belonging to a different architectural layer (e.g., SQL queries in a controller, HTML in a service class, business logic in a resource model).

### Statement
Each class must belong to exactly one architectural layer: presentation (controllers, templates, view models), business logic (services, handlers, processors), or data access (repositories, resource models). A class must not contain logic from a different layer.

### Violation (bad)
```php
// Controller contains SQL query -- presentation layer doing data access
class OrderController extends Action
{
    public function execute()
    {
        $connection = $this->resourceConnection->getConnection();
        $orders = $connection->fetchAll(
            "SELECT * FROM sales_order WHERE customer_id = :id",
            [':id' => $this->getRequest()->getParam('customer_id')]
        );
        return $this->resultJsonFactory->create()->setData($orders);
    }
}
```

### Pass (good)
```php
// Controller delegates to service, service delegates to repository
class OrderController extends Action
{
    public function __construct(
        private readonly OrderServiceInterface $orderService
    ) {}

    public function execute()
    {
        $customerId = (int) $this->getRequest()->getParam('customer_id');
        $orders = $this->orderService->getByCustomerId($customerId);
        return $this->resultJsonFactory->create()->setData($orders);
    }
}
```

### Enforcement
Per-slice findings table (ENF-POST-006) must verify layer separation for each generated file. Code review.

### Rationale
Mixed layers create classes that are untestable, unreusable, and fragile. A controller with SQL queries cannot be unit tested without a database, and its query logic cannot be reused by a CLI command or queue consumer.
<!-- RULE END: ARCH-ORG-001 -->

---

<!-- RULE START: ARCH-EXT-001 -->
## Rule ARCH-EXT-001: Extend, Don't Modify Core

**Domain**: Architecture
**Severity**: High
**Scope**: file

### Trigger
When a task requires changing behavior of a vendor/core class, and the proposed change involves directly editing the vendor file or copying it into the project.

### Statement
Behavior changes to vendor or core classes must use the framework's extension mechanism (plugin, preference, event observer, layout override). Direct modification of vendor files or copy-paste of vendor classes into the project is forbidden.

### Violation (bad)
```php
// Directly editing vendor file or copying it
// vendor/magento/module-sales/Model/Order.php -- modified line 234
public function canCancel()
{
    // CUSTOM: added check for custom status
    if ($this->getStatus() === 'custom_hold') {
        return false;
    }
    return parent::canCancel();
}
```

### Pass (good)
```php
// Plugin on the concrete class
class CanCancelPlugin
{
    public function afterCanCancel(Order $subject, bool $result): bool
    {
        if ($subject->getStatus() === 'custom_hold') {
            return false;
        }
        return $result;
    }
}
```

### Enforcement
Static analysis (ENF-POST-007) -- Magento Coding Standard flags direct core modifications. Per-slice findings table (ENF-POST-006). Code review.

### Rationale
Direct core modifications are lost on composer update. Extension-based changes survive upgrades and make customizations discoverable.
<!-- RULE END: ARCH-EXT-001 -->

---

<!-- RULE START: ARCH-DI-001 -->
## Rule ARCH-DI-001: Constructor Injection Over Direct Instantiation

**Domain**: Architecture
**Severity**: Critical
**Scope**: file

### Trigger
When a class constructor or method body contains `new SomeClass(` where SomeClass is a service, repository, handler, or factory -- not a DTO, value object, or exception.

### Statement
Dependencies must be received via constructor injection typed to an interface. Direct instantiation (`new`) is permitted only for DTOs, value objects, exceptions, and test doubles.

### Violation (bad)
```php
class OrderProcessor
{
    public function process(int $orderId): void
    {
        $repo = new OrderRepository($this->connection);
        $order = $repo->getById($orderId);
    }
}
```

### Pass (good)
```php
class OrderProcessor
{
    public function __construct(
        private readonly OrderRepositoryInterface $orderRepository
    ) {}

    public function process(int $orderId): void
    {
        $order = $this->orderRepository->getById($orderId);
    }
}
```

### Enforcement
Magento Coding Standard PHPCS (ENF-POST-007) flags direct ObjectManager usage. PHPStan custom rule flags `new` on injectable types. Per-slice findings table (ENF-POST-006) must quote constructor params.

### Rationale
Direct instantiation hides dependencies, breaks testability, and bypasses DI container configuration (preferences, plugins, proxies).
<!-- RULE END: ARCH-DI-001 -->

---

<!-- RULE START: ARCH-CONST-001 -->
## Rule ARCH-CONST-001: No Magic Values

**Domain**: Architecture
**Severity**: High
**Scope**: file

### Trigger
When a literal number, string, or ID appears in logic that controls program behavior: conditionals, thresholds, timeouts, retry counts, business rules (group IDs, status values, prefixes, flags). Exemptions: logging strings, display text, test assertions with obvious intent.

### Statement
Every behavioral literal must be a named constant. Business-logic literals must include a comment referencing the business requirement. If the value varies by environment or store, it must be sourced from configuration (see ENF-SYS-004). This applies to all languages.

### Violation (bad)
```php
if ($order->getItemCount() > 5) {       // magic number -- what is 5?
    $this->applyBulkDiscount($order);
}
```
```python
if retries > 3:                         # magic number -- what is 3?
    raise TimeoutError("too many retries")
timeout = 0.05                          # magic number -- seconds? ms?
```

### Pass (good)
```php
/** Minimum item count for bulk discount eligibility (BUS-REQ-042) */
private const BULK_DISCOUNT_MIN_ITEMS = 5;

if ($order->getItemCount() > self::BULK_DISCOUNT_MIN_ITEMS) {
    $this->applyBulkDiscount($order);
}
```
```python
MAX_RETRIES = 3
FALLBACK_TIMEOUT_S = 0.05

if retries > MAX_RETRIES:
    raise TimeoutError("too many retries")
timeout = FALLBACK_TIMEOUT_S
```

### Enforcement
PHPMD magic number detection, ruff, eslint no-magic-numbers (ENF-POST-007). Per-slice findings table (ENF-POST-006) must flag any behavioral literal.

### Rationale
Magic values obscure intent, make maintenance error-prone, and create implicit dependencies. `0.05` could be seconds, milliseconds, or a probability. `FALLBACK_TIMEOUT_S` communicates exactly what it is. Named constants create a single place to change the value.
<!-- RULE END: ARCH-CONST-001 -->

---

<!-- RULE START: ARCH-SSOT-001 -->
## Rule ARCH-SSOT-001: Single Source of Truth for Derived Views

**Domain**: Architecture
**Severity**: Critical
**Scope**: module

### Trigger
When a feature produces data visible through multiple channels (REST API total segments, GraphQL response fields, frontend template blocks, quote extension attributes) and the implementation writes the same value to two or more storage locations independently.

### Statement
Multi-channel features must choose one canonical storage location for each computed value. All secondary views must derive from the canonical source during each computation cycle. Never write the same value to two locations independently.

### Violation (bad)
```php
// Totals collector sets discount in Total object
$total->setTotalAmount('custom_discount', -$discount);

// SEPARATELY, a GraphQL resolver computes and sets the same discount on extension attribute
// from a DIFFERENT code path -- no shared source
$quote->getExtensionAttributes()->setCustomDiscount(
    $this->recalculateDiscount($quote) // independent calculation -- can diverge
);
```

### Pass (good)
```php
// Totals collector is the canonical source -- sets both from one computation
$discount = $this->calculateDiscount($quote);
$total->setTotalAmount('custom_discount', -$discount);
$address->getExtensionAttributes()->setCustomDiscount($discount);

// GraphQL resolver READS from the canonical source, never recomputes
public function resolve(/* ... */): array
{
    $totals = $this->cartTotalRepository->get($cartId);
    return ['custom_discount' => $totals->getTotalAmount('custom_discount')];
}
```

### Required declaration
Before implementing any multi-channel feature, declare:
```
Canonical source: [location]
REST reads from: [X]
GraphQL reads from: [Y]
Frontend reads from: [Z]
All derive from [canonical source] via [mechanism]
```

### Enforcement
Phase A call-path declaration must name the canonical source. Per-slice findings table (ENF-POST-006) must verify all channels read from the same source.

### Rationale
Independent population of the same value in multiple locations creates stale data when one location is updated but another isn't, inconsistent behavior between REST and GraphQL, and cleanup/reversal bugs where one location is cleared but the other retains stale data.
<!-- RULE END: ARCH-SSOT-001 -->

---

<!-- RULE START: ARCH-DRY-001 -->
## Rule ARCH-DRY-001: Logic Duplication Extraction

**Domain**: Architecture
**Severity**: Medium
**Scope**: module

### Trigger
Identical or near-identical logic (same control flow, different variable names) exists in 2+ locations. Logic must exceed 3 statements to qualify. Single-expression operations (type conversions, dict lookups, string formatting, guard clauses) are exempt.

### Statement
Logic duplicated across 2+ locations must be extracted to a shared function or module and imported by all callers. The exemption for single-expression operations prevents over-abstraction of trivial code.

### Violation (bad)
```python
# In graph/ingest.py
def validate_id_format(rule_id: str) -> bool:
    pattern = r'^[A-Z]+-[A-Z]+-\d{3}$'
    if not re.match(pattern, rule_id):
        raise ValueError(f"Invalid rule_id format: {rule_id}")
    return True

# In retrieval/pipeline.py (same logic, different variable name)
def check_rule_id(rid: str) -> bool:
    pattern = r'^[A-Z]+-[A-Z]+-\d{3}$'
    if not re.match(pattern, rid):
        raise ValueError(f"Invalid rule_id format: {rid}")
    return True
```

### Pass (good)
```python
# In graph/schema.py (shared utility)
def validate_rule_id(rule_id: str) -> bool:
    pattern = r'^[A-Z]+-[A-Z]+-\d{3}$'
    if not re.match(pattern, rule_id):
        raise ValueError(f"Invalid rule_id format: {rule_id}")
    return True

# Both callers import from schema
from graph.schema import validate_rule_id
```

### Enforcement
Code review. Static analysis tools (ruff, PHPStan copy-paste detection) flag near-duplicate blocks.

### Rationale
Duplicate logic means duplicate bugs. When a fix is applied to one copy, the other silently diverges. Extraction ensures a single source of truth for that logic.
<!-- RULE END: ARCH-DRY-001 -->

---

<!-- RULE START: ARCH-FUNC-001 -->
## Rule ARCH-FUNC-001: Function Size Cap

**Domain**: Architecture
**Severity**: Medium
**Scope**: file

### Trigger
Function or method body exceeds 30 lines of logic. Excluded from count: docstrings, type annotations, decorators, blank lines, and single-line comments.

### Statement
Functions must not exceed 30 lines of logic. Functions exceeding this limit must be decomposed into named sub-functions that the parent composes.

### Violation (bad)
```python
def process_rule(raw_data: dict) -> Rule:
    # 45 lines: parses input, validates schema, transforms fields,
    # computes embeddings, writes to DB, updates index
    ...
```

### Pass (good)
```python
def process_rule(raw_data: dict) -> Rule:
    validated = validate_rule_schema(raw_data)
    rule = transform_to_rule(validated)
    embedding = compute_embedding(rule)
    store_rule(rule, embedding)
    return rule
```

### Enforcement
Linter rules: ruff (Python), phpcs (PHP). Configure max function length at 30 logical lines.

### Rationale
Long functions mix multiple levels of abstraction, are harder to test in isolation, and resist reuse. A 30-line cap forces decomposition without being so restrictive that trivial functions get split unnecessarily.
<!-- RULE END: ARCH-FUNC-001 -->

---

<!-- RULE START: ARCH-TYPE-001 -->
## Rule ARCH-TYPE-001: Public Interface Type Annotations

**Domain**: Architecture
**Severity**: High
**Scope**: file

### Trigger
Any public function (not prefixed with `_` in Python, not `private`/`protected` in PHP/TS) lacks complete parameter and return type annotations.

### Statement
All public functions must have complete type annotations on every parameter and the return value. Language-specific enforcement tools validate correctness.

### Violation (bad)
```python
def search(query, limit):
    ...
```

### Pass (good)
```python
def search(query: str, limit: int) -> list[ScoredResult]:
    ...
```

### Enforcement
- Python: `mypy --strict` or `pyright`
- PHP: PHPStan level 8 (see also PHP-TYPE-001 for docblock-specific guidance)
- TypeScript: `tsc --strict`

### Rationale
Public interfaces are contracts. Unannotated parameters force callers to read implementation to understand expected types. Type annotations enable static analysis, IDE autocompletion, and catch type errors before runtime.
<!-- RULE END: ARCH-TYPE-001 -->

---

<!-- RULE START: ARCH-COMP-001 -->
## Rule ARCH-COMP-001: Composition Over Inheritance, Max Depth 2

**Domain**: Architecture
**Severity**: High
**Scope**: module

### Trigger
Class hierarchy depth exceeds 2 levels of project code. Language/framework base classes (`ABC`, `Protocol`, `BaseModel`, `AbstractController`, `AbstractPlugin`) are excluded from the count.

### Statement
Class inheritance depth must not exceed 2 levels of project code. Deeper hierarchies must be refactored to use composition via constructor injection.

### Violation (bad)
```python
class SpecificValidator(BaseValidator(AbstractValidator)):
    # 3 levels of project code -- too deep
    ...
```

### Pass (good)
```python
class SpecificValidator:
    def __init__(self, strategy: ValidationStrategy):
        self._strategy = strategy

    def validate(self, data: dict) -> bool:
        return self._strategy.validate(data)
```

### Enforcement
Code review. Check class hierarchy depth during PR review.

### Rationale
Deep inheritance hierarchies create tight coupling, make behavior hard to trace, and resist testing. Composition via injection produces the same polymorphism with explicit, traceable dependencies.
<!-- RULE END: ARCH-COMP-001 -->

---

<!-- RULE START: ARCH-ERR-001 -->
## Rule ARCH-ERR-001: Errors Must Propagate Context

**Domain**: Architecture
**Severity**: Critical
**Scope**: file

### Trigger
Any catch/except/rescue block that discards the original error context.

### Statement
When re-raising or wrapping an exception, the original exception must be preserved as the cause. Discarding the original error destroys the diagnostic trail. Language-specific rules (PHP-TRY-001, PHP-ERR-001) provide additional guidance per language.

### Violation (bad)
```python
except Exception:
    raise ValueError("processing failed")  # original exception lost
```
```php
catch (\Throwable $e) {
    throw new ProcessingException("failed");  // original exception lost
}
```

### Pass (good)
```python
except Exception as e:
    raise ValueError("processing failed") from e  # chain preserved
```
```php
catch (\Throwable $e) {
    throw new ProcessingException("failed", 0, $e);  # chain preserved
}
```

### Enforcement
- Python: ruff rule B904 (raise-without-from-inside-except)
- PHP: see PHP-TRY-001 and PHP-ERR-001

### Rationale
Swallowed exception context turns debugging from "read the stack trace" into "guess what happened." The cost of preserving context is one argument; the cost of losing it is hours of investigation.
<!-- RULE END: ARCH-ERR-001 -->