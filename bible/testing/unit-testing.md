# Unit Testing

## Purpose

This document defines **unit testing principles** to ensure code quality, confidence, and maintainability.

---

<!-- RULE START: TEST-TDD-001 -->
## Rule TEST-TDD-001: Test-Driven Development

**Domain**: Testing
**Severity**: High
**Scope**: slice

### Trigger
When generating implementation code for a new class or modifying a public method signature.

### Statement
Test skeletons with specific assertions must exist and be approved before the implementation they test. Every public method must have at least one test covering its primary behavior.

### Violation (bad)
```
// Implementation written first, then tests added that just confirm what was built:
public function testRelease(): void
{
    $result = $this->handler->release($id);
    $this->assertTrue($result); // meaningless -- asserts nothing about behavior
}
```

### Pass (good)
```php
// Test skeleton written and approved BEFORE release() is implemented:
public function testReleaseChangesStatusToReleased(): void
{
    $reservation = $this->createReservation(status: 'reserved');
    $this->handler->release($reservation->getId());

    $updated = $this->reservationRepository->getById($reservation->getId());
    $this->assertEquals('released', $updated->getStatus());
}

public function testReleaseOnAlreadyReleasedThrows(): void
{
    $reservation = $this->createReservation(status: 'released');
    $this->expectException(AlreadyReleasedException::class);
    $this->handler->release($reservation->getId());
}
```

### Enforcement
ENF-GATE-007 (test-first gate) -- test skeletons must be approved before implementation slice. ENF-POST-004 (tests must cover domain invariants).

### Rationale
Tests written after implementation become confirmation of what was built, not specification of what should be built. Test-first forces clear interface design before implementation details.
<!-- RULE END: TEST-TDD-001 -->

---

<!-- RULE START: TEST-ISO-001 -->
## Rule TEST-ISO-001: Test Isolation

**Domain**: Testing
**Severity**: High
**Scope**: file

### Trigger
When writing a unit test that interacts with external state (database, filesystem, network, shared static variables) or depends on another test's execution.

### Statement
Each unit test must set up its own state via mocks/stubs, must not depend on execution order, and must produce the same result on every run. No shared mutable state between tests.

### Violation (bad)
```php
class OrderTest extends TestCase
{
    private static array $orders = [];

    public function testCreateOrder(): void
    {
        self::$orders[] = new Order(1);
        $this->assertCount(1, self::$orders);
    }

    public function testDeleteOrder(): void
    {
        array_pop(self::$orders); // depends on testCreateOrder running first
        $this->assertCount(0, self::$orders);
    }
}
```

### Pass (good)
```php
class OrderTest extends TestCase
{
    public function testCreateOrder(): void
    {
        $repository = $this->createMock(OrderRepositoryInterface::class);
        $repository->method('getById')->willReturn($this->createOrder(1));
        $service = new OrderService($repository);

        $order = $service->get(1);
        $this->assertEquals(1, $order->getEntityId());
    }

    public function testDeleteOrder(): void
    {
        $repository = $this->createMock(OrderRepositoryInterface::class);
        $repository->method('delete')->willReturn(true);
        $service = new OrderService($repository);

        $this->assertTrue($service->delete(1));
    }
}
```

### Enforcement
Per-slice findings table (ENF-POST-006) must verify no shared mutable state in test classes. Static analysis (ENF-POST-007).

### Rationale
Isolated tests are faster, more reliable, and pinpoint failures precisely. Tests that depend on shared state or execution order produce intermittent failures that erode confidence in the test suite.
<!-- RULE END: TEST-ISO-001 -->

---

<!-- RULE START: TEST-INT-001 -->
## Rule TEST-INT-001: Integration Tests for Unprovable-by-Mocks Behaviors

**Domain**: Testing
**Severity**: Medium
**Scope**: module

### Trigger
When ENF-SYS-005 identifies behaviors that cannot be proven with mocks (DB unique constraints, transaction isolation, queue redelivery, actual multi-step state transitions).

### Statement
Integration tests must exist for every behavior declared as "unprovable by mocks" in the Phase D analysis. Tests must use a real database connection and verify actual constraint enforcement.

### Violation (bad)
```php
// "Testing" a unique constraint with a mock -- proves nothing
public function testDuplicateIsRejected(): void
{
    $this->connection->expects($this->once())
        ->method('insertOnDuplicate')
        ->willReturn(1);

    $this->handler->process($message);
    // Mock returns whatever you tell it -- no DB constraint verified
}
```

### Pass (good)
```php
// Integration test with real DB -- proves the constraint actually works
public function testDuplicateMessageIsRejectedByUniqueConstraint(): void
{
    $this->handler->process($this->createMessage('msg-001', 'SKU-A'));
    $this->handler->process($this->createMessage('msg-001', 'SKU-A'));

    $count = $this->connection->fetchOne(
        "SELECT COUNT(*) FROM vendor_processing_log WHERE message_id = 'msg-001'"
    );
    $this->assertEquals(1, $count); // DB constraint prevents duplicate
}
```

### Enforcement
ENF-SYS-005 (integration reality check) -- declares which behaviors need integration tests. ENF-GATE-FINAL (completion matrix must map each concurrency claim to an integration test).

### Rationale
Unit tests with mocked repositories prove logic flow, not system behavior. A mocked `insertOnDuplicate` proves nothing about whether the actual DB unique constraint prevents duplicates. Integration tests are the only way to validate concurrency claims.
<!-- RULE END: TEST-INT-001 -->
