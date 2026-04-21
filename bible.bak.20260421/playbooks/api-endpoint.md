# Playbook: API Endpoint

Step-by-step build sequence for any REST or GraphQL endpoint.
References rules -- does not duplicate them.

## Phase sequence
Phase A → Phase B → Phase C → [Phase D if async] → Tests → Slices

## Required phase outputs
Phase A: entry point per context (REST, GraphQL, admin, storefront, CLI)
Phase C: concrete class targeted, hook type, state available at interception
Security: access boundary declaration (ENF-SEC-001) -- before any code

## Required slice order
Slice 1: Service contract interface + DTO
Slice 2: Service implementation
Slice 3: Authorization service (ownership check -- separate from authentication)
Slice 4: webapi.xml / schema.graphqls (exposure layer)
Slice 5: di.xml wiring

## Completion checklist
[ ] Authentication verified (caller identity established)
[ ] Authorization verified (caller has rights to THIS resource) (SEC-UNI-001)
[ ] Ownership check in code, not just in design doc (SEC-UNI-002)
[ ] Response fields explicitly selected -- no full entity passthrough (SEC-UNI-003)
[ ] Every declared endpoint in Phase A has a corresponding implementation
[ ] plan-guardian completion matrix shows zero MISSING rows (ENF-GATE-FINAL)