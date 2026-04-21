# Playbook: Queue-Based Feature

Step-by-step build sequence for any feature using message queues.
References rules -- does not duplicate them.

## Phase sequence
Phase A → Phase B → Phase C → Phase D (mandatory) → Tests → Slices

## Phase D is always mandatory for queue features
ENF-SYS-001 trigger conditions are always met:
- Multiple consumers can process the same message
- Messages can be redelivered
- State transitions happen across async boundaries

## Required slice order (Magento 2 / PHP)
Slice 1: Schema + interfaces + DTOs
Slice 2: Persistence (resource model, repository)
Slice 3: Consumer handler (Phase D approved first)
Slice 4: Observer + event wiring (Phase A approved first)
Slice 5: Queue XML (communication, publisher, topology, consumer)
Slice 6: Configuration (di.xml, system.xml, config.xml)

## Minimum required queue files (ENF-OPS-002)
communication.xml, queue_publisher.xml, queue_topology.xml (with DLQ binding),
queue_consumer.xml (primary consumer + DLQ consumer -- both required)

## Completion checklist
[ ] Every declared state has an incoming code path (ENF-SYS-006 dead-state test)
[ ] DLQ consumer exists and is wired to the DLQ exchange (ENF-OPS-002)
[ ] Retry config proof trace complete: config declared → read → enforced (ENF-POST-008)
[ ] x-delivery-limit in queue_topology.xml matches configurable max_retries source
[ ] plan-guardian completion matrix shows zero MISSING rows (ENF-GATE-FINAL)
