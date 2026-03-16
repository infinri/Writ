# Operational Claims Enforcement

## Purpose

This document defines **mandatory operational reasoning** the AI must satisfy when making claims about performance, throughput, scalability, or reliability. Claims without engineering evidence are not claims -- they are marketing.

---

<!-- RULE START: ENF-OPS-001 -->
## Rule ENF-OPS-001: Operational Claim Validation

**Domain**: Operations
**Severity**: Critical
**Scope**: module

### Trigger
When the AI makes any claim about performance, throughput, scalability, or reliability in design docs, code comments, or plan.md (e.g., "handles 1000 reservations/minute", "scales horizontally", "resilient to failures").

### Statement
Every operational claim must include supporting evidence for each assertion. A claim without evidence must be downgraded to "untested estimate" or removed entirely.

### Violation (bad)
```
## Performance
Throughput: 1000 reservations/minute
The system is resilient to failures and scales horizontally.
```

### Pass (good)
```
## Performance
Throughput: ~1000 records/minute (estimated)
Evidence:
- Batch INSERT ON DUPLICATE KEY UPDATE: 1 SQL statement per batch (not per item)
- Indexes: btree on (item_id, status) -- used by ON DUPLICATE KEY
- Complexity: O(n) where n = items per batch (typically 1-20)
- Retry: 3 attempts with 30s TTL, then dead-letter queue
- Consumer: maxMessages=1000, single consumer instance, AMQP connection
- Unproven: actual throughput requires load testing against production-like data volume
```

### Required evidence per claim type

1. **Query count analysis**: How many SQL queries per operation? Per item? Per batch? N+1 risk?
2. **Index usage**: Which indexes support the query patterns? Table scans?
3. **Algorithmic complexity**: Time complexity relative to input size.
4. **Batch strategy**: Batched SQL or individual loop execution?
5. **Retry strategy**: How many retries? Backoff interval? DLQ for exhausted retries? DLQ monitoring?
6. **Backpressure handling**: `maxMessages` limit? Concurrent consumer count? Behavior during restarts?

### Specific violations
- Claiming "high throughput" without batch insert strategy
- Claiming "resilient" without DLQ configuration
- Claiming "scalable" without index analysis
- Claiming "1000/minute" without profiling or complexity analysis

### Enforcement
Per-slice findings table (ENF-POST-006) must flag any operational claim and verify evidence exists. ENF-POST-008 (proof trace) must trace config-to-enforcement for retry/DLQ claims. Code review.

### Rationale
Performance claims create expectations that influence architecture decisions, capacity planning, and SLA commitments. Unsubstantiated claims are worse than no claims -- they create false confidence that leads to production incidents when actual load exceeds imagined capacity.
<!-- RULE END: ENF-OPS-001 -->

---

<!-- RULE START: ENF-OPS-002 -->
## Rule ENF-OPS-002: Queue Configuration Completeness

**Domain**: Operations
**Severity**: High
**Scope**: module

### Trigger
When implementing any feature that uses message queues (publishing messages, consuming messages, or configuring queue infrastructure).

### Statement
Queue-based features must declare complete infrastructure (primary queue, DLQ, retry policy, consumer config, monitoring) AND complete consumer behavior guarantees (idempotency, duplicate handling, retry failure escalation).

### Violation (bad)
```xml
<!-- Primary queue only -- no DLQ, no retry policy -->
<consumer name="vendor.module.consumer"
          queue="vendor.module.queue"
          handler="Vendor\Module\Model\Consumer::process"
          connection="amqp"/>
```
```php
// Consumer with no idempotency guard -- duplicate messages cause double processing
public function process(MessageInterface $message): void
{
    $this->inventoryService->deduct($message->getSku(), $message->getQty());
}
```

### Pass (good)
```xml
<!-- Complete: primary queue + DLQ + retry + consumer config -->
<exchange name="vendor.module.exchange" type="topic" connection="amqp">
    <binding id="vendor.module.binding" topic="vendor.module.process"
             destinationType="queue" destination="vendor.module.queue">
        <arguments>
            <argument name="x-dead-letter-exchange" xsi:type="string">vendor.module.dlx</argument>
            <argument name="x-delivery-limit" xsi:type="number">3</argument>
        </arguments>
    </binding>
</exchange>
<exchange name="vendor.module.dlx" type="topic" connection="amqp">
    <binding id="vendor.module.dlq.binding" topic="vendor.module.process"
             destinationType="queue" destination="vendor.module.dlq"/>
</exchange>
```
```php
// Consumer with idempotency guard
public function process(MessageInterface $message): void
{
    $affected = $this->connection->insertOnDuplicate(
        $this->resource->getTableName('vendor_processing_log'),
        ['message_id' => $message->getId(), 'status' => 'processing'],
        [] // no update on duplicate -- silently skips re-delivery
    );
    if ($affected === 0) {
        $this->logger->info('Duplicate message skipped', ['id' => $message->getId()]);
        return;
    }
    $this->inventoryService->deduct($message->getSku(), $message->getQty());
}
```

### Infrastructure requirements
1. **Primary queue**: exchange, binding, queue name, consumer handler
2. **Dead-letter queue**: DLX exchange, DLQ queue name, binding
3. **Retry policy**: delivery limit, message TTL, backoff behavior
4. **Consumer configuration**: `maxMessages`, connection type, consumer instance count
5. **Monitoring hooks**: How DLQ messages are detected, logging for failed processing

### Consumer behavior requirements
6. **Idempotent processing**: Same outcome for single or multiple deliveries. Declare mechanism (DB unique constraint, idempotency key, atomic upsert).
7. **Duplicate message handling**: Explicit strategy declared. "Ignore and log" and "process idempotently" are both valid -- silent re-processing with side effects (double deduction, duplicate emails) is not.
8. **Retry failure escalation**: What happens after DLQ? Valid: alerting for manual review, automated retry after delay, compensating transaction. Invalid: "sits in DLQ" without monitoring.

### Enforcement
ENF-POST-008 (proof trace) must trace: config declared -> config read -> retry count check -> enforcement action -> DLQ publish -> DLQ consumer -> escalation. ENF-GATE-FINAL verifies all queue XML files exist. Per-slice findings table (ENF-POST-006).

> **Framework-specific guidance**: See `bible/frameworks/magento/runtime-constraints.md` for Magento 2 queue patterns (`queue_consumer.xml`, `queue_topology.xml`, AMQP configuration).

### Rationale
In production, queue consumers crash, connections drop, and messages get redelivered. Without a DLQ, failed messages are retried indefinitely or silently dropped. Complete queue infrastructure is the minimum viable configuration for any production queue.
<!-- RULE END: ENF-OPS-002 -->
