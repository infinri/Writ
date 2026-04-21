# PHP Error Handling

## Purpose

This document defines **error handling principles** for PHP code to ensure robust, debuggable, and maintainable applications.

---

<!-- RULE START: PHP-ERR-001 -->
## Rule PHP-ERR-001: Fail Fast on Invalid Preconditions

**Domain**: PHP / Error Handling
**Severity**: High
**Scope**: file

### Trigger
When a function or method receives input that could be invalid, or when a precondition must hold for the function to work correctly -- and the code returns a default value or continues silently instead of throwing.

### Statement
Validate preconditions at the start of the function. Throw a specific, meaningful exception immediately on failure. Never return null, false, or a default value to signal an error that should halt execution.

### Violation (bad)
```php
public function getDiscount(int $orderId): float
{
    $order = $this->orderRepository->getById($orderId);
    if ($order === null) {
        return 0.0; // silently returns default -- caller never knows order was missing
    }
    return $this->calculateDiscount($order);
}
```

### Pass (good)
```php
public function getDiscount(int $orderId): float
{
    try {
        $order = $this->orderRepository->getById($orderId);
    } catch (NoSuchEntityException $e) {
        throw new NoSuchEntityException(
            __('Cannot calculate discount: order %1 does not exist.', $orderId),
            $e
        );
    }
    return $this->calculateDiscount($order);
}
```

### Enforcement
Per-slice findings table (ENF-POST-006) must flag functions that return default values for error conditions. Code review.

### Rationale
Silent failures mask bugs and make debugging extremely difficult. A `return 0.0` on missing order produces a valid-looking discount of zero -- the bug is invisible until someone audits the final numbers. Early, explicit failures surface problems immediately.
<!-- RULE END: PHP-ERR-001 -->

---

<!-- RULE START: PHP-ERR-002 -->
## Rule PHP-ERR-002: Context-Appropriate Error Responses

**Domain**: PHP / Error Handling
**Severity**: Medium
**Scope**: file

### Trigger
When writing error handling for code that runs in different execution contexts (user-facing storefront, REST API, GraphQL, admin panel, CLI, background job/consumer).

### Statement
Error responses must match the execution context. User-facing code returns friendly messages and logs technical details. API code returns structured error responses with HTTP status codes. Background jobs log errors and implement retry/escalation logic. Stack traces and internal details are never exposed to callers.

### Violation (bad)
```php
// In a REST API endpoint -- leaks internal details to caller
catch (\Throwable $e) {
    echo $e->getTraceAsString(); // stack trace to API consumer
    return ['error' => $e->getMessage()]; // internal exception message exposed
}
```

### Pass (good)
```php
// In a REST API endpoint -- logs internally, returns safe message
catch (\Throwable $e) {
    $this->logger->error($e->getMessage(), [
        'exception' => $e,
        'request'   => $this->getRequest()->getParams(),
    ]);
    throw new LocalizedException(__('Unable to process request.'));
}
```

### Enforcement
ENF-SEC-002 (data exposure minimization) catches debug info in responses. Per-slice findings table (ENF-POST-006). Code review.

### Rationale
Different contexts require different error handling strategies. A stack trace shown to users is both a security risk (exposes file paths, class names, SQL) and poor UX. Internal details belong in logs, not in responses.
<!-- RULE END: PHP-ERR-002 -->
