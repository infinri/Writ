# PHP Coding Standards

## Purpose

This document defines **PHP-specific coding standards** for consistent, maintainable code.

---

<!-- RULE START: PHP-TYPE-001 -->
## Rule PHP-TYPE-001: DocBlock for Non-Obvious Types

**Domain**: PHP / Coding Standards
**Severity**: Low
**Scope**: file

### Trigger
When a variable is assigned from a method call where the return type is not inferrable by static analysis tools (e.g., repository `getById()` returns an interface type that PHPStan reports as `mixed`, or a factory method with no return type declaration).

### Statement
Add `/** @var Type $variable */` inline annotation when the type cannot be inferred by static analysis. This eliminates PHPStan mixed-type errors and enables IDE autocompletion.

### Violation (bad)
```php
$order = $this->orderRepository->getById($orderId);
// PHPStan: Cannot call method getStatus() on mixed.
$status = $order->getStatus();
```

### Pass (good)
```php
/** @var OrderInterface $order */
$order = $this->orderRepository->getById($orderId);
$status = $order->getStatus(); // PHPStan: OK -- type is known
```

### Enforcement
PHPStan level 8 (ENF-POST-007) catches mixed/unknown type errors. If a PHPStan error exists on a variable access, add the docblock.

### Rationale
Inline type annotations are self-documenting and eliminate entire categories of PHPStan errors without adding runtime type checks.
<!-- RULE END: PHP-TYPE-001 -->

---

<!-- RULE START: PHP-TRY-001 -->
## Rule PHP-TRY-001: Try-Catch Standards

**Domain**: PHP / Error Handling
**Severity**: High
**Scope**: file

### Trigger
When writing a try-catch block in any PHP file.

### Statement
Catch `\Throwable` (not `\Exception`). Always log the error with context before re-throwing or handling. Never swallow exceptions silently.

### Violation (bad)
```php
try {
    $this->processOrder($order);
} catch (\Exception $e) {
    // Catches only Exception -- misses TypeError, Error, etc.
    throw $e;
    // No logging -- error is untraceable in production
}
```

### Pass (good)
```php
try {
    $this->processOrder($order);
} catch (\Throwable $e) {
    $this->logger->error($e->getMessage(), [
        'exception' => $e,
        'order_id'  => $order->getEntityId(),
    ]);
    throw $e;
}
```

### Enforcement
PHPMD (ENF-POST-007) flags empty catch blocks. Per-slice findings table (ENF-POST-006) must verify catch blocks log before handling.

### Rationale
`\Throwable` catches both exceptions and errors (TypeError, ArgumentCountError, etc.). Logging ensures issues are traceable in production -- a re-thrown exception without logging is invisible if an outer catch swallows it.
<!-- RULE END: PHP-TRY-001 -->
