# Control Center: phased milestone implementation plan

**Status: PLANNED.** Tasks are tracked in [TASKS](../implementation/TASKS.md) under "Writ Control Center". None has started. The Control Center is not a prerequisite for M1.

| Milestone | Control Center deliverable | Gate (in addition to the milestone's own gate) | Requirements |
|---|---|---|---|
| M1 | None. The bake-off stays limited to the kernel prototypes | n/a | n/a |
| M2 | Management contracts: error model, pagination, revision and idempotency conventions, tenancy and project contracts, principal realms; schemas in `docs/contracts/management/` with fixtures | Contract tests green; CC-SEC-01 harness exists and proves the kernel builds and tests without any management module | CC-G02, CC-G11, CC-G13 |
| M3 | Management service skeleton; management authorization policy; audit query model and read-only views; timeline correlation; effect-state model; explanation service for decisions and effects | CC-SEC-01, 02, 03 (audit), 05 (audit APIs), 08, 11, 14; CC-UX-09 service level | CC-A01..A04, CC-G06 (service), CC-AD03 (key metadata) |
| M4 | Approval experience: inbox, step-up authentication, approve, deny, revoke; CLI equivalents | CC-SEC-07; approval tests from the M4 gate | CC-AP01..AP03, CC-AD04 |
| M5 | Adapter status, installation and configuration flows, coverage labels, unmediated activity, compatibility | Labels match live-host evidence (M5-SR06); no "verified" label without it | CC-I01, CC-I02, CC-I04, CC-A05, CC-O01 (adapters) |
| M6 | Policy lifecycle, simulation, compare, guarded publish and rollback; contract registry and breaking-change detection; pack management; enforcement labels | CC-SEC-04, 12, 13, 16, 18 | CC-R01..R05, CC-C01..C06, CC-I03, CC-G07 |
| M7 | Knowledge administration: import jobs, provenance, metadata, refresh, reindex, archive, delete with reconciliation; health classes; retrieval inspector | CC-SEC-03 (knowledge), 09, 10 | CC-K01..K07 |
| M8 | Memory and graph management: explore, sources, confidence, correct, supersede, archive, delete, consent, retention | CC-SEC-03 (memory), 09 | CC-M01..M04 |
| M9 | Unified local Control Center UI over the existing APIs; Overview; global search; help; Basic and Advanced; guided workflows; administration (users, roles, settings, backups, restores, migrations, logging, posture, diagnostics); CLI parity | All CC-SEC tests for beta scope; CC-UX-01..09 | All tier B requirements |
| M10+ | Enterprise features where a recorded workload or customer requirement justifies each | Per-feature ADR; load and isolation tests | All tier E requirements |

## Order of work inside each milestone

API contract and negative tests first, then the service, then CLI, then UI. A UI screen never ships before its API has passed its security tests. Each milestone's gate requires running code and recorded evidence; documentation alone does not complete a deliverable.

## Technology choices (deferred)

- Management-service language: decided after ADR-0002; it may differ from the kernel language because it is outside the hot path, but one language for both is preferred for operability unless measurements argue otherwise.
- UI framework, design system and packaging: decided in an ADR at the start of M9; requirements are accessibility, offline local serving, no remote asset dependencies at runtime, and a strict Content Security Policy.
