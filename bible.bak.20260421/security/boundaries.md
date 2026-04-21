# Security Boundaries

Universal security rules for all projects and frameworks.
Framework-specific patterns live in bible/frameworks/.

---

<!-- RULE START: SEC-UNI-001 -->
## Rule SEC-UNI-001: Authentication Is Not Authorization

**Domain**: Security
**Severity**: Critical
**Scope**: file

### Trigger
When an endpoint checks that a caller is authenticated (token exists, session valid) but does not verify the caller has rights to the specific resource being accessed.

### Statement
Every endpoint that returns user-specific data must verify both authentication (who is the caller?) AND authorization (does this caller own this resource?). A logged-in user must not be able to access another user's data by changing an ID in the request.

### Violation (bad)
```php
public function getOrderDetails(int $orderId): OrderInterface
{
    if (!$this->customerSession->isLoggedIn()) {
        throw new AuthorizationException(__('Not authorized.'));
    }
    // Only checks that SOME customer is logged in
    // Returns ANY order -- no ownership check
    return $this->orderRepository->getById($orderId);
}
```

### Pass (good)
```php
public function getOrderDetails(int $orderId): OrderInterface
{
    $customerId = (int) $this->customerSession->getCustomerId();
    if (!$customerId) {
        throw new AuthorizationException(__('Not authorized.'));
    }
    $order = $this->orderRepository->getById($orderId);
    if ((int) $order->getCustomerId() !== $customerId) {
        throw new AuthorizationException(__('Not authorized for this resource.'));
    }
    return $order;
}
```

### Enforcement
ENF-SEC-001 (access boundary declaration) requires ownership rules. Per-slice findings table (ENF-POST-006) must quote the ownership comparison code. ENF-GATE-007 test skeletons must include unauthorized + ownership violation + valid cases.

### Rationale
This is the #1 security flaw in AI-generated endpoints: authentication is implemented but authorization is omitted. Any authenticated customer can access any other customer's data by changing the ID parameter.
<!-- RULE END: SEC-UNI-001 -->

---

<!-- RULE START: SEC-UNI-002 -->
## Rule SEC-UNI-002: Ownership Verification Must Be In Code

**Domain**: Security
**Severity**: Critical
**Scope**: file

### Trigger
When the design document or Phase A declaration states an ownership rule (e.g., "customer can only see their own orders") but the implementation code contains no comparison between caller identity and resource owner identity.

### Statement
Every ownership rule declared in design must have a corresponding code path that compares authenticated caller identity to resource owner identity. If the comparison fails, the request is rejected. Design-only ownership rules are not enforcement.

### Violation (bad)
```php
// Design says: "customer can only see own orders"
// Code uses the customer_id from the request argument, not from auth context
public function resolve($field, $context, $info, $value, $args): array
{
    return $this->orderRepository->getByCustomerId($args['customer_id']);
    // customer_id comes from GraphQL argument -- caller can pass any ID!
}
```

### Pass (good)
```php
public function resolve($field, $context, $info, $value, $args): array
{
    $authenticatedCustomerId = (int) $context->getUserId();
    if ((int) $args['customer_id'] !== $authenticatedCustomerId) {
        throw new GraphQlAuthorizationException(__('Not authorized.'));
    }
    return $this->orderRepository->getByCustomerId($authenticatedCustomerId);
    // Uses the authenticated ID for the query, not the argument
}
```

### Enforcement
ENF-SEC-001 hard gate (ownership verification must exist in code). Per-slice findings table (ENF-POST-006) must quote the identity comparison line.

### Rationale
Design documents declaring ownership rules create false confidence. The only ownership rule that matters is the one enforced by a code path that compares caller identity to resource owner identity.
<!-- RULE END: SEC-UNI-002 -->

---

<!-- RULE START: SEC-UNI-003 -->
## Rule SEC-UNI-003: Explicit Response Field Selection

**Domain**: Security
**Severity**: High
**Scope**: file

### Trigger
When an API endpoint returns entity data to a caller and the response includes the full entity object or uses `toArray()`/`getData()`/`jsonSerialize()` without field filtering.

### Statement
API responses must explicitly select which fields to include. Never return the full entity. Internal IDs, credentials, PII, and internal state are excluded unless the endpoint specifically requires them and the caller has rights.

### Violation (bad)
```php
public function getCustomer(int $id): array
{
    $customer = $this->customerRepository->getById($id);
    return $customer->__toArray();
    // Exposes: password_hash, rp_token, rp_token_created_at, failures_num, lock_expires
}
```

### Pass (good)
```php
public function getCustomer(int $id): CustomerResponseInterface
{
    $customer = $this->customerRepository->getById($id);
    return $this->responseFactory->create([
        'name'  => $customer->getFirstname() . ' ' . $customer->getLastname(),
        'email' => $customer->getEmail(),
    ]);
}
```

### Enforcement
ENF-SEC-002 (data exposure minimization). Per-slice findings table (ENF-POST-006) must list all exposed response fields and justify each.

### Rationale
API responses that return entire entity objects "for convenience" create attack surfaces. Every exposed field is a potential information leak. Minimizing response data reduces the blast radius of any future authorization bypass.
<!-- RULE END: SEC-UNI-003 -->

---

<!-- RULE START: SEC-UNI-004 -->
## Rule SEC-UNI-004: No Secrets in Code

**Domain**: Security
**Severity**: Critical
**Scope**: file

### Trigger
When a string literal in source code matches patterns for API keys, tokens, passwords, or secrets (long alphanumeric strings, `sk_live_*`, `AKIA*`, `password =`, bearer tokens), or when `env.php`/`.env` values appear as hardcoded defaults in committed config files.

### Statement
API keys, tokens, passwords, and secrets are never hardcoded in source files. Read from environment variables or a secrets manager. Config files that may be committed must never contain secrets, even as defaults.

### Violation (bad)
```php
private const API_KEY = 'sk_live_EXAMPLE_DO_NOT_USE_1234567890';

public function callPaymentGateway(): void
{
    $this->client->setApiKey(self::API_KEY);
}
```

### Pass (good)
```php
public function __construct(
    private readonly DeploymentConfig $deploymentConfig
) {}

public function callPaymentGateway(): void
{
    $apiKey = $this->deploymentConfig->get('payment/gateway/api_key');
    if (!$apiKey) {
        throw new LocalizedException(__('Payment gateway API key not configured.'));
    }
    $this->client->setApiKey($apiKey);
}
```

### Enforcement
Static analysis (ENF-POST-007) -- PHPCS secret detection sniff. Git pre-commit hook secret scanning. Per-slice findings table (ENF-POST-006).

### Rationale
Hardcoded secrets in source code end up in git history, CI logs, error messages, and any system that processes the codebase. Once committed, a secret is effectively public and must be rotated.
<!-- RULE END: SEC-UNI-004 -->
