# Magento 2 Runtime Constraints

## Purpose

This document defines **Magento 2-specific runtime constraints** that provide framework-specific implementation guidance for the generic enforcement rules in `enforcement/system-dynamics.md`, `enforcement/security-boundaries.md`, and `enforcement/operational-claims.md`.

These are the Magento 2 answers to questions those generic rules force the AI to ask.

---

## System Dynamics -- Magento 2 Specifics

### Temporal Truth Sources in Magento (applies to ENF-SYS-002)

<!-- RULE START: FW-M2-RT-001 -->
## Rule FW-M2-RT-001: MSI Salability Is Decided at Order Placement

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When writing post-order-placement code (observer, consumer, cron) that checks product availability or stock status for an already-placed order.

### Statement
When MSI allows an order to be placed, salability has been authoritatively decided. Post-placement code must NOT re-evaluate salability. Processing all items from a placed order is always correct.

### Violation (bad)
```php
// Observer on sales_order_save_after RE-CHECKS availability
public function execute(Observer $observer): void
{
    $order = $observer->getEvent()->getOrder();
    foreach ($order->getItems() as $item) {
        $stockStatus = $this->stockRegistry->getStockStatusBySku($item->getSku());
        if (!$stockStatus->getIsInStock()) {
            $this->skipItem($item); // skips valid order items!
        }
    }
}
```

### Pass (good)
```php
// Observer trusts the placement decision -- processes ALL items
public function execute(Observer $observer): void
{
    $order = $observer->getEvent()->getOrder();
    foreach ($order->getItems() as $item) {
        $this->processItem($item);
    }
}
```

### Authoritative decision points

| Fact | Authoritative Event | Re-evaluation Valid? |
|------|---------------------|---------------------|
| Product salability | `sales_order_place_after` | No -- order placement is the authority |
| Payment authorization | `sales_order_payment_pay` | No -- payment gateway is the authority |
| Coupon validity | `sales_order_place_after` | No -- validated at cart-to-order transition |
| Stock assignment (source) | MSI source selection at shipment | No -- re-selecting sources contradicts allocation |
| Credit memo eligibility | `sales_order_creditmemo_save_after` | No -- Magento validates qty constraints |

### Enforcement
ENF-SYS-002 (temporal truth source declaration). Per-slice findings table (ENF-POST-006) must verify no post-authority re-evaluation exists.

### Rationale
"Respect MSI" means "trust MSI's placement decision," not "re-evaluate MSI after the fact." Re-checking salability post-placement produces false negatives when stock levels change between placement and processing, causing valid orders to be silently skipped.
<!-- RULE END: FW-M2-RT-001 -->

---

### Magento Order State Machine (applies to ENF-SYS-003)

<!-- RULE START: FW-M2-RT-002 -->
## Rule FW-M2-RT-002: Magento Order State Transitions Must Be Declared

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: module

### Trigger
When writing code that reacts to or depends on Magento order state changes (observers on `sales_order_save_after`, `sales_order_place_after`, order processing consumers).

### Statement
Features that depend on order state must explicitly declare which states and transitions they handle and which they ignore. Silent assumptions about state transitions are a violation.

### Violation (bad)
```
// Phase A output -- no state handling declared:
"We observe sales_order_save_after and process the order."
// Which states? What happens on cancel? On hold? On partial refund?
```

### Pass (good)
```
// Phase A output -- explicit state handling:
"Observer fires on sales_order_save_after.
Handles: new → processing (creates reservation).
Handles: processing → canceled (releases reservation).
Ignores: processing → complete (no reservation action needed).
Ignores: partial refund (state stays 'processing', no reservation change).
Ignores: holded (no reservation action -- hold is temporary)."
```

### Magento order states reference

| State | Meaning | Typical Triggers |
|-------|---------|------------------|
| `new` | Order created, not yet processed | `sales_order_place_after` |
| `pending_payment` | Awaiting payment confirmation | Offline/deferred payment methods |
| `processing` | Payment received, fulfillment in progress | `sales_order_payment_pay` |
| `complete` | Shipped and invoiced | All items shipped + invoiced |
| `closed` | Fully refunded | Credit memo covers all items |
| `canceled` | Order canceled | Admin action or payment failure |
| `holded` | Manually held | Admin action |
| `payment_review` | Fraud/payment review | Gateway fraud detection |

### Non-obvious behaviors
- Partial refund does NOT change state to `closed`
- `pending_payment` may or may not transition to `processing` depending on payment method
- Custom states can be added by third-party modules

### Enforcement
ENF-SYS-003 (state transition atomicity). Phase A call-path declaration must list all handled and ignored transitions.

### Rationale
Magento's order state machine has non-obvious behaviors. Explicit declaration prevents the AI from assuming a simplified state model that breaks on edge cases like partial refunds or custom states.
<!-- RULE END: FW-M2-RT-002 -->

---

### Magento Configuration Patterns (applies to ENF-SYS-004)

<!-- RULE START: FW-M2-RT-003 -->
## Rule FW-M2-RT-003: Policy Configuration Must Use system.xml + ScopeConfigInterface

**Domain**: Frameworks / Magento 2
**Severity**: High
**Scope**: module

### Trigger
When a value classified as "deployment-variable policy" by ENF-SYS-004 is hardcoded in a Magento 2 module instead of being sourced from admin configuration.

### Statement
Store-specific policy values must be declared in `etc/adminhtml/system.xml`, defaulted in `etc/config.xml`, read via `ScopeConfigInterface` with proper scope, and encapsulated in a dedicated Config class.

### Violation (bad)
```php
// Hardcoded policy -- different stores cannot customize
private const SKIP_STATES = ['canceled', 'holded'];

public function shouldProcess(OrderInterface $order): bool
{
    return !in_array($order->getState(), self::SKIP_STATES, true);
}
```

### Pass (good)
```php
// Policy read from scoped config via dedicated Config class
class ReservationConfig
{
    public function __construct(
        private readonly ScopeConfigInterface $scopeConfig
    ) {}

    public function getSkipStates(): array
    {
        $value = $this->scopeConfig->getValue(
            'custom_reservation/general/skip_states',
            ScopeInterface::SCOPE_STORE
        );
        return $value ? explode(',', $value) : [];
    }
}
```

### Required configuration files
1. `etc/adminhtml/system.xml` -- field with section/group/field, source model, scope flags
2. `etc/config.xml` -- default value
3. Dedicated Config model class -- encapsulates `scopeConfig->getValue()` calls
4. `ScopeInterface::SCOPE_STORE` or `SCOPE_WEBSITE` -- never scopeless

### Enforcement
ENF-SYS-004 (policy vs mechanism). Per-slice findings table (ENF-POST-006). ENF-POST-008 (proof trace from config declaration to enforcement).

### Rationale
Magento's multi-store architecture requires per-store configuration. `ScopeConfigInterface` with proper scope is the only mechanism that correctly handles default -> website -> store fallback. Inline reads without scope are unreliable in multi-website setups.
<!-- RULE END: FW-M2-RT-003 -->

---

## Security -- Magento 2 Specifics

### Access Control Patterns (applies to ENF-SEC-001)

<!-- RULE START: FW-M2-RT-004 -->
## Rule FW-M2-RT-004: Magento Endpoint Authorization Patterns

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When writing a REST endpoint (`webapi.xml`), GraphQL resolver, or admin controller in Magento 2.

### Statement
Every endpoint must use the framework-specific authorization mechanism for its type. REST uses `<resource>` in `webapi.xml`. GraphQL resolvers must check `$context->getUserType()` and verify ownership. Admin controllers must override `_isAllowed()`.

### Violation (bad)
```php
// GraphQL resolver with no auth check -- any caller gets any data
public function resolve($field, $context, $info, $value, $args): array
{
    return $this->orderRepository->getById($args['order_id'])->getData();
}
```

### Pass (good)
```php
// GraphQL resolver with proper auth + ownership
public function resolve($field, $context, $info, $value, $args): array
{
    $userId = (int) $context->getUserId();
    $userType = $context->getUserType();

    if ($userType === UserContextInterface::USER_TYPE_ADMIN
        || $userType === UserContextInterface::USER_TYPE_INTEGRATION) {
        // Admin/integration: unrestricted
    } elseif (!$userId || $userType !== UserContextInterface::USER_TYPE_CUSTOMER) {
        throw new GraphQlAuthorizationException(__('Not authorized.'));
    } else {
        // Customer: verify ownership
        $order = $this->orderRepository->getById($args['order_id']);
        if ((int) $order->getCustomerId() !== $userId) {
            throw new GraphQlAuthorizationException(
                __('Not authorized for this resource.')
            );
        }
    }

    return $this->formatOrder(
        $this->orderRepository->getById($args['order_id'])
    );
}
```

### Authorization patterns by endpoint type

**REST (`webapi.xml`)**:
```xml
<route url="/V1/example/:id" method="GET">
    <service class="Vendor\Module\Api\ExampleInterface" method="getById"/>
    <resources>
        <resource ref="Magento_Sales::sales"/>
    </resources>
</route>
```
- `resource ref="anonymous"` requires explicit justification
- `resource ref="self"` restricts to authenticated customer accessing own data

**Admin controllers**:
```php
protected function _isAllowed(): bool
{
    return $this->_authorization->isAllowed('Vendor_Module::resource_id');
}
```

**ACL resource** (`etc/acl.xml`):
```xml
<resource id="Vendor_Module::resource_id" title="Resource Title" sortOrder="10"/>
```

### Specific violations
- REST endpoints without `<resource>` in `webapi.xml`
- GraphQL resolvers without `$context->getUserType()` checks
- Admin controllers without `_isAllowed()` override
- Customer-facing endpoints without ownership verification

### Enforcement
ENF-SEC-001 (access boundary declaration). Per-slice findings table (ENF-POST-006). ENF-GATE-007 test skeletons must include unauthorized + ownership violation + valid caller tests.

### Rationale
Magento has four distinct caller types (admin, customer, integration, guest), each with different authorization mechanisms. The framework does NOT apply authorization automatically for GraphQL -- resolvers must check explicitly.
<!-- RULE END: FW-M2-RT-004 -->

---

## Operations -- Magento 2 Specifics

### Queue Infrastructure (applies to ENF-OPS-001, ENF-OPS-002)

<!-- RULE START: FW-M2-RT-005 -->
## Rule FW-M2-RT-005: Magento Message Queue Configuration Completeness

**Domain**: Frameworks / Magento 2
**Severity**: High
**Scope**: module

### Trigger
When building a feature that uses Magento's message queue framework (any of: `communication.xml`, `queue_publisher.xml`, `queue_topology.xml`, `queue_consumer.xml`).

### Statement
Queue-based features must declare all four XML configuration files plus a DLQ exchange. Missing any one of the four files causes silent failures.

### Violation (bad)
```xml
<!-- Only primary queue -- no DLQ. Failed messages silently dropped. -->
<exchange name="vendor.module.exchange" type="topic" connection="amqp">
    <binding id="vendor.module.binding"
             topic="vendor.module.process"
             destinationType="queue"
             destination="vendor.module.queue"/>
</exchange>
```

### Pass (good)
**1. `etc/communication.xml`** -- topic and schema:
```xml
<topic name="vendor.module.process"
       request="Vendor\Module\Api\Data\MessageInterface"/>
```

**2. `etc/queue_publisher.xml`** -- publisher binding:
```xml
<publisher topic="vendor.module.process">
    <connection name="amqp" exchange="vendor.module.exchange"/>
</publisher>
```

**3. `etc/queue_topology.xml`** -- exchange, bindings, DLQ:
```xml
<exchange name="vendor.module.dlx" type="topic" connection="amqp">
    <binding id="vendor.module.dlq.binding"
             topic="vendor.module.process"
             destinationType="queue"
             destination="vendor.module.dlq"/>
</exchange>
<exchange name="vendor.module.exchange" type="topic" connection="amqp">
    <binding id="vendor.module.binding"
             topic="vendor.module.process"
             destinationType="queue"
             destination="vendor.module.queue">
        <arguments>
            <argument name="x-dead-letter-exchange" xsi:type="string">vendor.module.dlx</argument>
            <argument name="x-delivery-limit" xsi:type="number">3</argument>
            <argument name="x-message-ttl" xsi:type="number">30000</argument>
        </arguments>
    </binding>
</exchange>
```

**4. `etc/queue_consumer.xml`** -- consumer handler:
```xml
<consumer name="vendor.module.consumer"
          queue="vendor.module.queue"
          handler="Vendor\Module\Model\Consumer\Handler::process"
          connection="amqp"
          maxMessages="1000"/>
```

### Enforcement
ENF-OPS-002 (queue configuration completeness). ENF-POST-008 (proof trace for retry -> DLQ -> escalation chain). ENF-GATE-FINAL verifies all 4 XML files exist on disk.

### Rationale
Magento's queue framework splits configuration across four XML files. Missing any one causes silent failures: messages published but never consumed, consumers registered but never bound, or failed messages dropped without DLQ routing.
<!-- RULE END: FW-M2-RT-005 -->

---

### Multi-Website Stock Resolution (applies to ENF-SYS-001, ENF-SYS-002)

<!-- RULE START: FW-M2-RT-006 -->
## Rule FW-M2-RT-006: MSI Website-to-Stock Mapping Must Be Declared

**Domain**: Frameworks / Magento 2
**Severity**: High
**Scope**: module

### Trigger
When writing code that queries stock/inventory data and the Magento installation uses MSI (Multi-Source Inventory) with multiple websites.

### Statement
Features that interact with inventory must declare which stock is being referenced (default vs MSI), how stock is resolved (StockResolverInterface vs legacy StockRegistryInterface), and whether the feature works in multi-website/multi-stock setups.

### Violation (bad)
```php
// Uses legacy single-stock API -- wrong for multi-website MSI setups
$stockStatus = $this->stockRegistry->getStockStatusBySku($sku);
$isInStock = $stockStatus->getIsInStock();
// Returns default stock only -- incorrect for Website B mapped to Stock 2
```

### Pass (good)
```php
// Resolves stock per website via MSI
$websiteCode = $this->storeManager->getWebsite()->getCode();
$stockId = $this->stockResolver->execute(
    SalesChannelInterface::TYPE_WEBSITE,
    $websiteCode
)->getStockId();
$salableQty = $this->getProductSalableQty->execute($sku, $stockId);
```

### Required declaration
1. **Which stock**: Default stock (single-source) or MSI stock (multi-source)?
2. **How resolved**: Via `StockResolverInterface` using sales channel, or `StockRegistryInterface` (legacy)?
3. **Multi-website**: Does this feature work when different websites map to different stocks?

### Enforcement
ENF-SYS-002 (temporal truth source -- which stock authority?). Per-slice findings table (ENF-POST-006). Phase A call-path declaration must document stock resolution strategy.

### Rationale
MSI decouples stock from websites. A product can be salable on Website A (Stock 1) but not on Website B (Stock 2). Code assuming a single global stock produces incorrect results in multi-website, multi-stock configurations.
<!-- RULE END: FW-M2-RT-006 -->
