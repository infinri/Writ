# Security Boundary Enforcement

## Purpose

This document defines **mandatory security reasoning** the AI must satisfy for any feature that exposes data or functionality through an externally accessible interface (REST API, GraphQL, admin UI, storefront, CLI). These rules close the gap between "structurally correct" and "secure by design."

---

<!-- RULE START: ENF-SEC-001 -->
## Rule ENF-SEC-001: Access Boundary Declaration

**Domain**: Security
**Severity**: Critical
**Scope**: slice

### Trigger
When generating code for any externally accessible endpoint (REST route, GraphQL resolver, admin controller, storefront controller, CLI command).

### Statement
Every endpoint must have a written Access Boundary Declaration presented and approved before implementation code is produced. The declaration must answer four questions: who can call it, how the caller is authenticated, what data ownership rules apply, and what happens when unauthorized.

### Violation (bad)
```
// Phase A output for a GraphQL resolver:
"The reservations query returns reservation data for a given order ID."
// No mention of who can call it, how ownership is verified, or what happens for unauthorized callers.
```

### Pass (good)
```
// Phase A output for a GraphQL resolver:
"reservations(order_id:) Access Boundary:
  Who: Admin (unrestricted), Customer (own orders only), Anonymous (denied)
  Auth: Caller identity + type checked in resolver via $context->getUserType()
  Ownership: Authenticated user ID must match order.customer_id
  Unauthorized: GraphQlAuthorizationException"
```

### Declaration checklist

1. **Who can call it?**
   - Anonymous / Guest
   - Authenticated customer (frontend token)
   - Authenticated admin (admin token)
   - Integration (API token)
   - Internal only (queue consumer, cron, CLI)

2. **How is the caller authenticated?**
   - Framework ACL / role-based access control (specify which resource or role)
   - Token-based identity validation (JWT, OAuth, session -- specify mechanism)
   - No authentication (public endpoint -- must justify why)

3. **What data ownership rules apply?**
   - Does the caller own the requested data? (e.g., customer can only see their own orders)
   - How is ownership verified? (e.g., `order.customer_id === token.customer_id`)
   - What happens if ownership check fails? (403, empty result, exception)

4. **What happens if unauthorized?**
   - REST: HTTP 401/403 with appropriate error message
   - GraphQL: Authorization exception per framework convention
   - Admin/internal UI: redirect or error per framework convention

### Hard Gate -- Ownership Verification Must Exist in Code
The Access Boundary Declaration is necessary but **not sufficient**. The AI must also verify that ownership enforcement is **implemented in the code**, not just written in prose.

For any endpoint where the declaration states ownership rules:

1. **The implementation MUST contain a code path that compares the authenticated caller's identity to the resource owner's identity** (e.g., `$order->getCustomerId() === $authenticatedUserId`).
2. **If the comparison fails, the endpoint MUST reject the request** -- return 403, throw an authorization exception, or return an empty result. It must NOT return the data.
3. **If ownership cannot be verified via the service layer** (e.g., the entity has no owner field), the endpoint MUST reject access for non-admin callers entirely.
4. **"Customer must be logged in" is NOT ownership verification.** Authentication proves identity. Ownership verification proves the authenticated user has a relationship to the specific resource being accessed. Both are required.

### Code-level violation (bad)
```php
// Declaration says "customer can only access their own orders"
// But code only checks authentication, not ownership:
public function resolve($field, $context, $info, $value, $args): array
{
    if (!$context->getUserId()) {
        throw new GraphQlAuthorizationException(__('Not authorized.'));
    }
    // Returns ANY order -- no ownership check
    return $this->orderRepository->getById($args['order_id'])->getData();
}
```

### Code-level pass (good)
```php
public function resolve($field, $context, $info, $value, $args): array
{
    $userId = (int) $context->getUserId();
    $userType = $context->getUserType();
    if (!$userId || $userType !== UserContextInterface::USER_TYPE_CUSTOMER) {
        throw new GraphQlAuthorizationException(__('Not authorized.'));
    }
    $order = $this->orderRepository->getById($args['order_id']);
    if ((int) $order->getCustomerId() !== $userId) {
        throw new GraphQlAuthorizationException(__('Not authorized for this resource.'));
    }
    return $this->formatOrder($order);
}
```

### Enforcement
Phase A call-path declaration must include the Access Boundary Declaration for every endpoint. Per-slice findings table (ENF-POST-006) must quote the ownership comparison code. ENF-GATE-007 test skeletons must include unauthorized caller test, ownership violation test, and valid caller test.

> **Framework-specific guidance**: See `bible/frameworks/magento/runtime-constraints.md` for Magento 2 patterns (`webapi.xml`, `$context->getUserId()`, `_isAllowed()`, `GraphQlAuthorizationException`).

### Rationale
The most common security gap in AI-generated code is omission -- endpoints that work correctly but never ask "who should be allowed to call this?" Forcing the declaration before implementation makes security a first-class design constraint, not an afterthought.
<!-- RULE END: ENF-SEC-001 -->

---

<!-- RULE START: ENF-SEC-002 -->
## Rule ENF-SEC-002: Data Exposure Minimization

**Domain**: Security
**Severity**: High
**Scope**: file

### Trigger
When an API endpoint (REST or GraphQL) returns entity data to a caller, and the response includes the full entity object or uses `toArray()`/`getData()`/`jsonSerialize()` without field filtering.

### Statement
API responses must explicitly select which fields to return. Never return the full entity. Internal IDs, credentials, PII, and internal state are excluded unless the endpoint specifically requires them and the caller has rights.

### Violation (bad)
```php
public function getById(int $orderId): array
{
    $order = $this->orderRepository->getById($orderId);
    return $order->getData(); // exposes internal_note, remote_ip, customer_taxvat, etc.
}
```

### Pass (good)
```php
public function getById(int $orderId): OrderResponseInterface
{
    $order = $this->orderRepository->getById($orderId);
    return $this->responseFactory->create([
        'order_id'    => $order->getEntityId(),
        'status'      => $order->getStatus(),
        'grand_total' => $order->getGrandTotal(),
        'created_at'  => $order->getCreatedAt(),
    ]);
}
```

### Exposure checklist
For every API response, verify:
1. **No internal IDs leaked unnecessarily**: Entity IDs, database primary keys, and internal references only exposed if the consumer needs them.
2. **No sensitive data in default responses**: Customer emails, payment details, addresses, internal notes excluded unless the endpoint is designed for that purpose.
3. **No debug information in production responses**: Stack traces, SQL queries, file paths, internal error details never appear in API responses.

### Enforcement
Per-slice findings table (ENF-POST-006) must quote the response construction code and list exposed fields. ENF-SEC-001 Access Boundary Declaration must specify which fields the consumer needs. Code review.

### Rationale
API responses that return entire entity objects "for convenience" create attack surfaces. Every exposed field is a potential information leak. Minimizing response data reduces the blast radius of any future authorization bypass.
<!-- RULE END: ENF-SEC-002 -->
