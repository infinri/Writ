# Magento 2 Implementation Constraints

## Purpose

This document defines **Magento 2-specific implementation constraints** that govern how domain logic, data access, and plugin interception must be implemented. These are hard rules, not suggestions.

---

<!-- RULE START: FW-M2-001 -->
## Rule FW-M2-001: Validation Must Be Backed by Persistence

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When writing validation logic that determines if a domain entity (coupon, product, rule, customer group, category) is legitimate.

### Statement
Legitimacy checks must use a repository, service contract, or resource model lookup. String matching, prefix checking, or format inference alone does not validate a domain entity's legitimacy.

### Violation (bad)
```php
// Format-based validation -- does not prove the coupon actually exists
public function isValidCoupon(string $couponCode): bool
{
    return str_starts_with($couponCode, 'PROMO_') && strlen($couponCode) === 12;
}
```

### Pass (good)
```php
// Persistence-backed validation -- proves the coupon exists and is active
public function isValidCoupon(string $couponCode): bool
{
    try {
        $coupon = $this->couponRepository->getByCode($couponCode);
        $rule = $this->ruleRepository->getById($coupon->getRuleId());
        return $rule->getIsActive();
    } catch (NoSuchEntityException $e) {
        return false;
    }
}
```

### Enforcement
ENF-PRE-002 (domain invariant declaration) must declare whether validation is format-based or persistence-based. Per-slice findings table (ENF-POST-006) must quote the validation code. If format validation is intentional, a code comment referencing the business requirement is mandatory.

### Rationale
Format-based validation creates false positives (entities that look valid but don't exist) and false negatives (entities that exist but don't match expected format). Only the persistence layer is the source of truth for entity legitimacy.
<!-- RULE END: FW-M2-001 -->

---

<!-- RULE START: FW-M2-002 -->
## Rule FW-M2-002: Factory and Model::load() Patterns Are Forbidden

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When entity retrieval code contains `Factory->create()->load(`, `Model::load(`, `Model::loadByCode(`, or `Model::loadByAttribute(`.

### Statement
All entity retrieval must go through the repository layer. Factory/model-load patterns are forbidden in new code. If no repository method exists for the required lookup, declare the gap and create a repository method -- do not fall back to deprecated patterns.

### Violation (bad)
```php
$product = $this->productFactory->create()->load($productId);
$rule = $this->ruleFactory->create()->loadByCode($ruleCode);
$customer = $this->customerFactory->create()->loadByEmail($email);
```

### Pass (good)
```php
$product = $this->productRepository->getById($productId);
$rule = $this->ruleRepository->getById($ruleId);
$customer = $this->customerRepository->get($email); // repository method for email lookup
```

### Enforcement
Magento Coding Standard PHPCS (ENF-POST-007) flags factory/load patterns. PHPStan custom rule. Per-slice findings table (ENF-POST-006).

### Rationale
Factory/model-load patterns bypass service contracts, skip event dispatching, ignore extension attributes, and create tight coupling to the persistence layer. Repository-based retrieval is the Magento 2 architectural standard.
<!-- RULE END: FW-M2-002 -->

---

<!-- RULE START: FW-M2-003 -->
## Rule FW-M2-003: Quote State Must Be Read at the Correct Execution Point

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When writing a plugin or observer that reads quote totals, subtotal, discount amounts, or any other totals-dependent data.

### Statement
Totals-dependent logic must execute after `collect_totals` has run. A `before` plugin must not read totals data. An `around` plugin that needs post-apply state must read data after `proceed()` and re-fetch the quote.

### Violation (bad)
```php
// before plugin reads totals that haven't been collected yet
public function beforeSaveAddressInformation(
    $subject,
    $cartId,
    $addressInfo
) {
    $quote = $this->quoteRepository->getActive($cartId);
    $subtotal = $quote->getSubtotal(); // STALE -- collectTotals hasn't run yet
    if ($subtotal > 100) {
        // This condition may evaluate incorrectly
    }
}
```

### Pass (good)
```php
// after plugin reads totals after collection
public function afterSaveAddressInformation(
    $subject,
    $result,
    $cartId,
    $addressInfo
) {
    $quote = $this->quoteRepository->getActive($cartId);
    $subtotal = $quote->getSubtotal(); // FRESH -- collectTotals has completed
    if ($subtotal > 100) {
        // Condition evaluates correctly against current totals
    }
    return $result;
}
```

### Enforcement
ENF-PRE-003 (seam justification) must declare what state is available at the interception point. Per-slice findings table (ENF-POST-006) must verify no totals reads before collection.

### Rationale
Quote totals are computed lazily and cached. Reading them before collection returns stale or zero values, causing silent logical errors that are extremely difficult to reproduce and diagnose.
<!-- RULE END: FW-M2-003 -->

---

<!-- RULE START: FW-M2-004 -->
## Rule FW-M2-004: Target Concrete Classes for Plugins by Default

**Domain**: Frameworks / Magento 2
**Severity**: High
**Scope**: file

### Trigger
When writing a `<plugin>` or `<type>` declaration in `di.xml` that targets an interface rather than a concrete class.

### Statement
Plugins must target the concrete class, not the interface, unless multi-implementation coverage is explicitly required and justified. Interface-level plugins intercept all implementations, which may include internal framework classes.

### Violation (bad)
```xml
<!-- Targets interface -- intercepts ALL implementations including internal ones -->
<type name="Magento\Quote\Api\CartRepositoryInterface">
    <plugin name="custom_plugin" type="Vendor\Module\Plugin\CartPlugin"/>
</type>
```

### Pass (good)
```xml
<!-- Targets concrete class -- precise, predictable interception -->
<type name="Magento\Quote\Model\QuoteRepository">
    <plugin name="custom_plugin" type="Vendor\Module\Plugin\QuoteRepositoryPlugin"/>
</type>
```

### Enforcement
ENF-PRE-003 (seam justification must declare why interface vs concrete and which implementations are covered). Per-slice findings table (ENF-POST-006).

### Rationale
Interface-level plugins intercept all implementations, which may include internal or framework classes not intended for interception. Concrete-class targeting provides precise, predictable interception scope.
<!-- RULE END: FW-M2-004 -->

---

<!-- RULE START: FW-M2-005 -->
## Rule FW-M2-005: Totals Collector Idempotency and State Reset

**Domain**: Frameworks / Magento 2
**Severity**: Critical
**Scope**: file

### Trigger
When writing or modifying a custom totals collector class (extends `AbstractTotal` or implements `collect()` in the quote totals pipeline).

### Statement
Every custom totals collector's `collect()` method must be fully idempotent. Calling `collect()` twice must produce identical results. Calling `collect()` after eligibility changes must clear all prior values.

### Violation (bad)
```php
public function collect(
    Quote $quote,
    ShippingAssignmentInterface $shippingAssignment,
    Total $total
): self {
    // No reset -- additive accumulation across calls
    $discount = $this->calculateDiscount($quote);
    $total->addTotalAmount($this->getCode(), -$discount);
    $address = $shippingAssignment->getShipping()->getAddress();
    $address->getExtensionAttributes()->setCustomDiscount($discount);
    return $this;
}
// Second call DOUBLES the discount. Ineligibility leaves stale extension attribute.
```

### Pass (good)
```php
public function collect(
    Quote $quote,
    ShippingAssignmentInterface $shippingAssignment,
    Total $total
): self {
    parent::collect($quote, $shippingAssignment, $total);
    $address = $shippingAssignment->getShipping()->getAddress();

    // 1. Zero all owned state FIRST
    $total->setTotalAmount($this->getCode(), 0);
    $total->setBaseTotalAmount($this->getCode(), 0);
    $address->getExtensionAttributes()->setCustomDiscount(null);

    // 2. Check eligibility AFTER reset
    if (!$this->isEligible($quote)) {
        return $this; // cleaned up even on ineligibility
    }

    // 3. Compute from scratch
    $discount = $this->calculateDiscount($quote);
    $total->setTotalAmount($this->getCode(), -$discount);
    $total->setBaseTotalAmount($this->getCode(), -$discount);
    $address->getExtensionAttributes()->setCustomDiscount($discount);
    return $this;
}
```

### Idempotency checklist
Before writing any totals collector, list every piece of state it writes and confirm each is zeroed at the top of `collect()`:
1. **Zero owned total amounts first**: `parent::collect()` + explicit zero of additional amounts
2. **Clear owned extension attributes**: Set to null/empty before eligibility check
3. **Clear on ineligibility**: Return after cleanup, not before
4. **No additive accumulation**: Use `setTotalAmount`, never `addTotalAmount` without prior zero
5. **Implement `_resetState()`**: Clear instance-level arrays, caches, flags; re-set collector code

Reference pattern: `Magento\SalesRule\Model\Quote\Discount::collect()` -- explicitly clears `discountDescription` and `discounts` extension attribute before processing.

### Enforcement
ENF-PRE-002 (domain invariant -- idempotency checklist). Per-slice findings table (ENF-POST-006). ENF-POST-004 (tests must cover collect-twice scenario and eligibility-change scenario).

### Rationale
Totals collectors run multiple times per request (item add, address change, shipping change, payment change). Without idempotent reset, stale values persist from prior cycles -- causing discounts that don't reverse, double-application on re-collection, and ghost line items in REST/GraphQL responses. This is the #1 source of totals-related bugs in custom Magento modules.
<!-- RULE END: FW-M2-005 -->

---

<!-- RULE START: FW-M2-006 -->
## Rule FW-M2-006: CartTotalRepository Does Not Call collectTotals for Non-Virtual Quotes

**Domain**: Frameworks / Magento 2
**Severity**: High
**Scope**: module

### Trigger
When implementing a feature that depends on fresh totals being available via `GET /V1/carts/mine/totals` (REST) for non-virtual quotes.

### Statement
`CartTotalRepository::get()` only calls `$quote->collectTotals()` for virtual quotes. For non-virtual quotes, it reads previously-collected totals directly. Do not assume REST totals endpoints trigger fresh collection for all quote types.

### Violation (bad)
```
// Design assumption -- WRONG for non-virtual quotes:
"The custom discount will be visible when the frontend calls GET /V1/carts/mine/totals.
The totals endpoint triggers collectTotals(), which runs our collector."
// FALSE -- for non-virtual quotes, collectTotals is NOT called.
```

### Pass (good)
```
// Design correctly documents the limitation:
"The custom discount is computed during collectTotals(), triggered by saveAddressInformation
(ShippingInformationManagement::saveAddressInformation).
GET /V1/carts/mine/totals reads previously-collected values for non-virtual quotes.
If the frontend needs guaranteed freshness, it must call saveAddressInformation first.
The collector's fetch() method returns stored amounts -- it does not recompute."
```

### Key behaviors
- REST `GET /V1/carts/mine/totals` for non-virtual quotes returns **previously collected** totals, not freshly computed ones.
- Custom collector output is visible only if `collectTotals()` was called by a prior operation (e.g., `saveAddressInformation`, item update).
- The `fetch()` method reads from the `Total` object's stored amounts, not re-computing.

### Enforcement
Phase A call-path declaration must document which operation triggers collection. Per-slice findings table (ENF-POST-006).

### Rationale
Assuming `collectTotals()` runs in all REST paths leads to implementations that appear to work in testing (where mutations precede totals retrieval) but fail in production when totals are fetched without a preceding mutation.
<!-- RULE END: FW-M2-006 -->
