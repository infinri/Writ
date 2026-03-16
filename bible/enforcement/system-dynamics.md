# System Dynamics Enforcement

## Purpose

This document defines **mandatory system-dynamics reasoning** the AI must satisfy before, during, and after implementation of any feature involving concurrency, state transitions, asynchronous processing, multi-context behavior, or temporal dependencies. These rules close the gap between structural correctness and production realism.

Static structure enforcement (ENF-PRE, ENF-POST, ENF-GATE) ensures the AI builds things correctly.
System dynamics enforcement ensures the AI builds things that **survive real production stress**.

---

## Block 1 -- Pre-Implementation System Modeling

<!-- RULE START: ENF-SYS-001 -->
## Rule ENF-SYS-001: Concurrency Simulation Phase

**Domain**: System Dynamics
**Severity**: Critical
**Scope**: module

### Trigger
When implementing any feature that involves shared state, database writes from multiple actors, message queues, multi-consumer processing, or any code path reachable by more than one concurrent caller (e.g., two queue consumers, admin + cron, REST + GraphQL simultaneously).

### Statement
Before implementation, produce a written Concurrency Model that identifies all actors, all race windows, atomicity mechanisms, double-delivery outcomes, and crash recovery behavior.

### Violation (bad)
```
// Phase D output:
"The consumer processes messages from the queue and updates the database."
// No actors identified, no race windows, no atomicity mechanism declared.
```

### Pass (good)
```
// Phase D output -- Concurrency Model:
"Actors: (1) Queue consumer instance A, (2) Queue consumer instance B,
 (3) Admin cancel action, (4) Cron cleanup job.

Race window 1: Consumer A and Consumer B both receive the same message
(redelivery). Both read status='reserved', both attempt release.
Guard: UPDATE reservation SET status='released' WHERE id=:id AND status='reserved'
-- second actor gets affected_rows=0, throws AlreadyReleasedException.

Race window 2: Admin cancels order while consumer is processing.
Guard: Consumer checks order status inside transaction after SELECT FOR UPDATE.

Double-delivery: Idempotent -- INSERT ON DUPLICATE KEY on (order_id, sku).
Second delivery is a no-op.

Crash recovery: Uncommitted transaction rolls back. Message returns to queue
via AMQP nack. Redelivery triggers double-delivery guard."
```

### Required Concurrency Model sections
1. **Actors**: All concurrent actors that may execute this code simultaneously
2. **Race windows**: For each write operation, what happens if two actors reach it at the same time -- describe the specific interleaving
3. **Atomic boundaries**: Which operations MUST be atomic and the SQL pattern enforcing it
4. **Double-delivery**: What happens if the same message is processed twice -- is the outcome identical?
5. **Crash recovery**: What happens if the process crashes mid-transaction -- is partial state left behind?

### Enforcement
ENF-GATE-005 (Phase D hard gate) -- concurrency model must be approved before implementation. Per-slice findings table (ENF-POST-006) must quote the atomicity mechanism for each race window.

### Rationale
The most dangerous production bugs are temporal, not structural. Code that passes all unit tests but fails under concurrent load is "clean but naive." Explicit race-window modeling before implementation prevents check-then-act patterns that break under concurrency.
<!-- RULE END: ENF-SYS-001 -->

---

<!-- RULE START: ENF-SYS-002 -->
## Rule ENF-SYS-002: Temporal Truth Source Declaration

**Domain**: System Dynamics
**Severity**: Critical
**Scope**: file

### Trigger
When writing validation, check, or decision logic that depends on a fact established by a prior system event (e.g., "is this product salable?", "has this order been paid?", "is this coupon valid?") -- specifically when the code runs AFTER the authoritative event.

### Statement
For any decision depending on a prior state transition, declare: the authoritative decision point, the temporal position of the current check, whether re-evaluation is valid, and the staleness risk. Re-checking a fact that was already authoritatively decided by a prior event is a violation if re-evaluation could contradict the original decision.

### Violation (bad)
```php
// Post-order-placement observer RE-CHECKS product availability
// Order placement already validated this through MSI
public function execute(Observer $observer): void
{
    $order = $observer->getEvent()->getOrder();
    foreach ($order->getItems() as $item) {
        $stock = $this->stockRegistry->getStockStatusBySku($item->getSku());
        if (!$stock->getIsInStock()) {
            $this->skipProcessing($item); // skips valid items!
        }
    }
}
```

### Pass (good)
```php
// Post-order-placement observer TRUSTS the placement decision
public function execute(Observer $observer): void
{
    $order = $observer->getEvent()->getOrder();
    // Temporal truth: salability was decided at order placement.
    // Re-evaluation invalid -- stock may have changed, but this order was authorized.
    foreach ($order->getItems() as $item) {
        $this->processItem($item);
    }
}
```

### Required declaration format
```
Fact: [what is being checked]
Authoritative event: [which system event decided this]
Temporal position: [BEFORE or AFTER the authoritative event]
Re-evaluation valid: [Yes/No -- if No, explain why]
Staleness risk: [what could have changed since the authoritative event]
```

### Enforcement
Phase D output must include temporal truth source declarations. Per-slice findings table (ENF-POST-006) must verify no post-authority re-evaluation exists in code.

> **Framework-specific guidance**: See `bible/frameworks/magento/runtime-constraints.md` for Magento 2 authoritative decision points (MSI salability, order state, payment).

### Rationale
"Respect the upstream authority" means "trust the decision," not "re-evaluate it." Temporal truth source confusion is a recurring pattern where the AI adds defensive checks that actually introduce logic flaws -- re-checking availability after placement can reject valid orders.
<!-- RULE END: ENF-SYS-002 -->

---

<!-- RULE START: ENF-SYS-003 -->
## Rule ENF-SYS-003: State Transition Atomicity Rule

**Domain**: System Dynamics
**Severity**: Critical
**Scope**: file

### Trigger
When writing code that changes a status/state column in a database (e.g., `reserved` to `released`, `pending` to `processing`, `active` to `expired`) where more than one actor could attempt the same transition.

### Statement
Every state transition must declare a state machine (legal states and transitions), transition guards, an atomicity strategy, and contention handling. Bare read-then-write without a concurrency guard is always a violation.

### Violation (bad)
```php
// Read-then-write with no concurrency guard
$reservation = $this->reservationRepository->getById($id);
if ($reservation->getStatus() === 'reserved') {
    $reservation->setStatus('released');
    $this->reservationRepository->save($reservation);
}
// Two consumers can both read 'reserved', both set 'released', both succeed
// Second save silently overwrites -- potential duplicate side effects
```

### Pass (good)
```php
// Atomic CAS -- second actor gets affected_rows=0
$affected = $connection->update(
    $this->resource->getTableName('vendor_reservation'),
    ['status' => self::STATUS_RELEASED, 'released_at' => $now],
    ['entity_id = ?' => $id, 'status = ?' => self::STATUS_RESERVED]
);
if ($affected === 0) {
    throw new AlreadyReleasedException(
        __('Reservation %1 is no longer in reserved state.', $id)
    );
}
```

### Allowed atomicity strategies
The AI must choose and declare one:
- **Atomic CAS (compare-and-swap)**: `UPDATE ... SET status = :new WHERE status = :old` -- second actor gets affected rows = 0
- **Pessimistic locking**: `SELECT ... FOR UPDATE` followed by status check + update in same transaction
- **Idempotent upsert**: `INSERT ... ON DUPLICATE KEY UPDATE` for idempotent creation
- **Optimistic locking with version column**: Read version -> update with `WHERE version = :read_version` -> retry or fail on mismatch
- **Transaction isolation + unique constraint**: DB unique constraint within serializable/repeatable-read transaction to reject duplicates
- **Event sourcing / append-only log**: State derived from event sequence; no mutable status column

### Required state machine declaration
```
States: reserved, released, expired
Transitions:
  reserved → released (guard: actor holds reservation, atomicity: CAS)
  reserved → expired (guard: TTL exceeded, atomicity: cron CAS batch)
  released → (terminal)
  expired → (terminal)
Contention: Second actor on same transition gets affected_rows=0, throws exception.
```

### Enforcement
Phase D output must include state machine declaration and atomicity strategy for every transition. Per-slice findings table (ENF-POST-006) must quote the SQL guard. ENF-SYS-006 (dead state detection) verifies every state has incoming transitions.

### Rationale
Naive read-then-write patterns are the #1 source of race conditions in queue-driven systems. Two consumers can both read the same status, both decide to transition, and both succeed -- causing duplicate side effects. The guard is not optional.
<!-- RULE END: ENF-SYS-003 -->

---

<!-- RULE START: ENF-SYS-004 -->
## Rule ENF-SYS-004: Policy vs Mechanism Separation

**Domain**: System Dynamics
**Severity**: High
**Scope**: file

### Trigger
When a literal value in code represents a business decision that could reasonably differ per store, tenant, website, or deployment (e.g., which order states to skip, retry counts, threshold quantities, payment method lists).

### Statement
Classify every hardcoded business-semantic value into one of three categories: deployment-variable policy (must be configurable), universal domain constant (may be hardcoded as named constant), or infrastructure mechanism (may be hardcoded). Deployment-variable policy hardcoded in code is a violation.

### Violation (bad)
```php
// Deployment-variable policy hardcoded
private const SKIP_STATES = ['canceled', 'holded'];
// Different stores may need different skip lists -- this requires code changes

private const MAX_RETRIES = 3;
// Operations team should control this -- not buried in code
```

### Pass (good)
```php
// Deployment-variable policy read from config
$skipStates = $this->config->getSkipStates(); // reads from system.xml via ScopeConfigInterface

// Universal domain constant -- hardcoded is correct
private const STATUS_RESERVED = 'reserved'; // changing this breaks domain integrity
private const STATUS_RELEASED = 'released';
```

### Classification guide

| Category | Configurable? | Example |
|----------|--------------|---------|
| **Deployment-variable policy** | MUST be | Order states to skip, retry counts, threshold quantities, payment method lists |
| **Universal domain constant** | Named constant OK | Status enum values, entity relationships, state machine transitions |
| **Infrastructure mechanism** | Named constant OK | Table names, column names, UUID format, SQL patterns |

The test: **"Would a different store/tenant reasonably need a different value?"** If yes, it's deployment-variable policy and must be configurable.

### Enforcement
Phase D output must classify all hardcoded values. Per-slice findings table (ENF-POST-006) must flag any deployment-variable policy that is hardcoded. ENF-POST-008 (proof trace) must trace config-to-enforcement for policy values.

> **Framework-specific guidance**: See `bible/frameworks/magento/runtime-constraints.md` for Magento 2 configuration patterns (`system.xml`, `config.xml`, `ScopeConfigInterface`).

### Rationale
Multi-tenant and multi-context platforms have business semantics that differ per context. Hardcoding policy into mechanism creates technical debt requiring code changes for what should be configuration changes.
<!-- RULE END: ENF-SYS-004 -->

---

<!-- RULE START: ENF-SYS-005 -->
## Rule ENF-SYS-005: Integration Reality Check

**Domain**: System Dynamics
**Severity**: Critical
**Scope**: module

### Trigger
When the implementation claims concurrency safety, idempotency, or atomicity, and the only tests validating those claims use mocked database operations.

### Statement
For any feature involving concurrent actors, state transitions, or DB constraints, declare which behaviors cannot be proven with mocks and which require integration tests against a real database. Claiming production-readiness for concurrent/async features with only mocked tests is a violation.

### Violation (bad)
```php
// "Testing" a unique constraint with a mock -- proves nothing
public function testDuplicateMessageIsRejected(): void
{
    $this->connection->expects($this->once())
        ->method('insertOnDuplicate')
        ->willReturn(1);

    $result = $this->handler->process($message);
    $this->assertTrue($result); // mock always returns what you told it to
}
```

### Pass (good)
```php
// Integration test with real DB -- proves the constraint actually works
public function testDuplicateMessageIsRejectedByDbConstraint(): void
{
    // First insert succeeds
    $this->handler->process($this->createMessage('msg-001', 'SKU-A'));

    // Second insert with same message_id is a no-op
    $this->handler->process($this->createMessage('msg-001', 'SKU-A'));

    // Verify only one record exists
    $count = $this->connection->fetchOne(
        "SELECT COUNT(*) FROM vendor_processing_log WHERE message_id = 'msg-001'"
    );
    $this->assertEquals(1, $count);
}
```

### Required declaration
1. **Which behaviors cannot be proven with mocks alone**: List specific scenarios (DB unique constraint enforcement, transaction isolation, queue redelivery, multi-step state transitions)
2. **Which require integration tests**: For each unprovable behavior, describe the integration test
3. **Production-safety assessment**: Can this module be declared production-safe without integration test validation?

### Hard Gate
The AI must not mark implementation as complete for async/concurrent features unless:
- Integration test structure exists (even if tests require a test database to run)
- Each concurrency claim maps to a specific integration test
- The test validates the DB constraint or atomic operation, not a mock of it

### Enforcement
ENF-GATE-007 test skeletons must include integration test stubs for all concurrency claims. ENF-GATE-FINAL completion matrix must map each concurrency claim to its integration test. Per-slice findings table (ENF-POST-006).

### Rationale
Unit tests with mocked repositories prove logic flow, not system behavior. A mocked `insertOnDuplicate` proves nothing about whether the actual DB unique constraint prevents duplicates. Integration tests are the only way to validate concurrency claims.
<!-- RULE END: ENF-SYS-005 -->

---

<!-- RULE START: ENF-SYS-006 -->
## Rule ENF-SYS-006: State Machine Completeness -- Dead State Detection

**Domain**: System Dynamics
**Severity**: Critical
**Scope**: module

### Trigger
When the implementation declares a state machine (per ENF-SYS-003) and the AI is verifying completeness at the end of a slice or at ENF-GATE-FINAL.

### Statement
Every declared state must have at least one incoming code path (except initial state), at least one outgoing code path (except terminal state), and at least one assignment in generated code. A constant with no assignment anywhere in the codebase is a dead declaration.

### Violation (bad)
```php
// State machine declares four states
private const STATUS_RESERVED = 'reserved';
private const STATUS_RELEASED = 'released';
private const STATUS_EXPIRED  = 'expired';  // DEAD -- nothing ever sets this
private const STATUS_FAILED   = 'failed';   // DEAD -- nothing ever sets this

// Only two transitions exist in code:
// reserved → released (in releaseReservation())
// Nothing transitions TO expired or failed
```

### Pass (good)
```php
private const STATUS_RESERVED = 'reserved';
private const STATUS_RELEASED = 'released';
private const STATUS_EXPIRED  = 'expired';
private const STATUS_FAILED   = 'failed';

// All states have incoming transitions:
// → reserved: createReservation() sets STATUS_RESERVED
// → released: releaseReservation() sets STATUS_RELEASED WHERE status='reserved'
// → expired: expireStalReservations() cron sets STATUS_EXPIRED WHERE status='reserved' AND created_at < :ttl
// → failed: handleProcessingError() sets STATUS_FAILED WHERE status='reserved'
```

### The dead-state test
Before marking implementation complete, for each status constant:
- Search all implementation files for assignments TO that status value
- If no assignment exists (only the constant definition): **DEAD STATE**
- Dead states are constraint violations

### Enforcement
ENF-GATE-FINAL completion matrix must verify every declared state has incoming transitions. Per-slice findings table (ENF-POST-006) must list all state constants and quote their assignment code.

### Rationale
STATUS_RELEASED in PartialCaptureInventory: constant declared, tests written, but nothing ever SET the status to released. Structurally present, functionally dead. This rule requires verifying not just that states are declared but that they are reachable.
<!-- RULE END: ENF-SYS-006 -->

---

## Block 2 -- Phased Protocol Integration

### Phase D -- Failure & Concurrency Modeling

When a task triggers ENF-SYS-001 through ENF-SYS-005, add **Phase D** to the Phased Implementation Protocol:

**After Phase C approval, before implementation:**

1. Produce the Concurrency Model (ENF-SYS-001)
2. Produce the Temporal Truth Source declarations (ENF-SYS-002)
3. Produce the State Transition definitions (ENF-SYS-003)
4. Produce the Policy vs Mechanism classification (ENF-SYS-004)
5. Produce the Integration Reality Check (ENF-SYS-005)
6. Produce the Dead State audit (ENF-SYS-006)

Present Phase D as a **single output** for review. The AI must halt and wait for approval before proceeding to implementation.

### Trigger conditions for Phase D

Phase D is mandatory when the task involves ANY of:
- Database writes from multiple actors (queue consumers, cron, admin, API)
- Message queue processing
- Status/state transitions
- Asynchronous side effects
- Multi-website or multi-store behavior
- Inventory, payment, or order state management
- Any claim of idempotency, atomicity, or concurrency safety
