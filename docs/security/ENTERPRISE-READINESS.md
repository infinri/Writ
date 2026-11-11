# Enterprise security and SOC 2 readiness

**Status:** owner-authorized planning direction (2026-11-10, America/Chicago); controls below are PLANNED, not implemented or independently examined. Tracking: ENT-00..ENT-08 in [TASKS](../implementation/TASKS.md). Release and production remain NO-GO. This plan does not accept ADR-0002 or ADR-0008.

## 1. Product and assurance direction

- Rust is the owner's preferred direction for the trusted kernel and broker, subject to the targeted M1-08 confirmation and final owner decision on [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md). Preserve the existing measurements and complete the narrowly scoped verification; do not restart the entire bake-off.
- PostgreSQL is the target for enterprise operational state; SQLite remains the local/offline profile. These are separate deployment profiles behind persistence contracts, not a mandatory database replacement for every installation. PostgreSQL acceptance is a future M10 gate, not a result of the SQLite benchmarks. Neo4j, orchestration and Control Center technology choices remain separate.
- Build secure defaults, operational documentation and exportable evidence into the relevant milestones. SOC 2 is an examination of controls around a defined service organization's system, not a property conferred on a Rust binary or PostgreSQL database.
- For self-hosted Writ, release requires the applicable product security gates and a customer assurance package. A vendor SOC 2 report is not automatically a prerequisite for distributing the software. Customer contracts may add requirements.
- Before offering a hosted or managed enterprise service, define the actual legal entity, service boundary, commitments and control owners; seek a qualified CPA's scope/readiness assessment and record the required assurance milestones. Type I may be an initial milestone; Type II requires evidence of operating effectiveness over a period. Reports for our service remain our responsibility; a customer's report does not replace them.
- Do not claim SOC 2 Type I, Type II, certification, audit readiness, or real-host enforcement based only on this plan, mappings or passing unit tests. Describe only the scope and assurance actually supported by evidence and any issued report.

No auditor engagement, paid compliance platform, certification claim or production deployment is authorized by this planning decision.

## 2. Deployment responsibilities

| Responsibility | Self-hosted deployment | Writ-operated hosted/managed service |
|---|---|---|
| Product design, fixes and release provenance | Writ maintains software and supplies verified release evidence | Writ maintains software and release evidence |
| Infrastructure, database, backups and restore | Customer operates; Writ provides supported configuration and tested procedures | Writ owns the controls for components it operates; providers and exclusions are documented |
| Tenant configuration, users and approvals | Customer administers identities, roles, reviews and policy | Customer administers its users/policy; Writ protects service administration and tenant boundaries |
| SOC 2 examination | Customer decides its own scope with its auditor; Writ provides product evidence | Writ owns any examination of its service; customer retains its own complementary controls and audit obligations |
| Incident response and retention | Document customer duties and any contracted Writ support | Document Writ response, notification, retention and customer duties |

Managed customer infrastructure can be a mixed model. Before contracting, assign each control to the party that actually operates it, including identity, support access, patching, keys, backups and incident notification. No responsibility is transferred merely by calling a deployment self-hosted.

## 3. Control requirements and acceptance evidence

These are Writ engineering requirements, not a complete auditor-approved interpretation of the Trust Services Criteria. Detailed criterion mapping and examination scope remain ENT-01 work. Security/common criteria are the planning baseline; evaluate Availability and Confidentiality against commitments, and Processing Integrity and Privacy against actual services and data. Do not claim coverage before scope is reviewed.

| ID | Requirement and boundary | Milestone | Required evidence before the applicable release |
|---|---|---|---|
| ENT-R01 | Trusted identity, least privilege and administrative role separation; enterprise SSO integration and MFA for privileged human access; scoped service identities, revocation and controlled emergency access | M2, M4, M9, M10 | Forged/expired/wrong-realm identity and role-escalation refusals; revocation and emergency-access drills; recorded access reviews |
| ENT-R02 | Tenant/project binding across requests, grants, receipts, policy, retrieval, graph, exports, backups and management APIs | M2..M10 | Cross-tenant negative tests including pooled connections, workers, caches and exports; isolation review; supported backup/restore scope documented |
| ENT-R03 | Atomic grant consumption, durable dispatch/receipt states and safe reconciliation under multiple processes | M3, M10 | Real process termination/restart and storage-failure tests; concurrent worker/reconciler campaigns; no grant overuse, unauthorized effect or duplicate dispatch after uncertainty; unresolved outcomes remain reconcilable |
| ENT-R04 | Protected audit records with access control, integrity verification, retention and authorized export; separate durable authorization evidence from best-effort telemetry | M3, M6, M9 | Missing/corrupt record detection; export authorization and redaction tests; retention/deletion and integrity-restoration drills; documented privileged-admin threat limits |
| ENT-R05 | Data inventory/classification, minimization, approved transport/storage encryption and key lifecycle; secrets excluded from logs, exports and repository | M2, M5, M8..M10 | Key provisioning/rotation/revocation tests; secret-redaction checks; documented retention/deletion including backups, residency and support access; use maintained standard cryptography |
| ENT-R06 | Reviewed changes, protected releases, pinned dependencies, SBOM/license inventory, provenance and vulnerability response | M2 onward, M9, M12 | Release-to-source/build traceability, dependency and secret scans, reviewed exceptions, signed release verification, patch/rollback drill and a published reporting channel |
| ENT-R07 | Recovery objectives, backups, restore, upgrade/migration rollback, monitoring and incident response | M9, M10, M12 | Explicit RPO/RTO and SLOs; actual restore and upgrade drills; loss-of-storage/network and restart tests; alert and incident exercises; agreed notification procedures |
| ENT-R08 | Organizational controls have accountable humans, repeatable procedures and retained evidence | Throughout delivery; before service operation | Risk register, access/change/vendor reviews, training, personnel lifecycle, incident and business-continuity procedures; reviewer/date/evidence for each completed cycle |
| ENT-R09 | Honest coverage and independent assurance claims; customer responsibilities and deployment limits visible | M5, M9, M10, M12 | Security/operations review of coverage labels, assurance package and claims; approved system boundary; CPA-issued report required for any SOC 2 report claim |

Relates to existing [invariants](invariants.md), especially INV-01, INV-05..INV-10, INV-12, INV-15 and INV-27..INV-30. New controls must not weaken existing invariants.

### PostgreSQL acceptance constraints

Before horizontal broker workers are supported, specify the database transaction/isolation model, concurrency and unique-key constraints, retry boundaries, migrations and durable coordination/fencing. Prove stale or restarted workers cannot perform effects after losing authority. PostgreSQL itself does not resolve the current process-local reconciliation limitation. Row security, if adopted, is defense in depth and requires tests for connection-pool tenant state and privileged roles; it does not replace broker authorization.

Test replay and uncertainty against a controlled effect adapter with its own durable observations; database transactions alone cannot guarantee exactly-once effects in an arbitrary external system. Publish supported adapter guarantees and unresolved outcomes. Do not generalize SQLite timings to PostgreSQL. Keep multi-process reconciliation an unresolved architectural risk until the replacement is specified and verified.

## 4. Evidence and ownership

Until named people are assigned, the project owner is accountable for assigning a technical security/release owner and an operations/compliance owner. An unassigned or unevidenced control remains open. A model-generated checklist or self-review is not independent attestation.

The control register must record: control/requirement ID, exact TSC edition and criterion mapping with rationale, deployment scope, accountable owner and operator, procedure and frequency, implementation status, evidence location and retention/access policy, reviewer and last review, exceptions, and next review. Use PLANNED, IMPLEMENTED, TESTED and INDEPENDENTLY EXAMINED distinctly; an issued report also needs its exact scope, date/period and opinion.

Keep customer data, identities, credentials, incident details and personnel/access records out of the public repository. Public docs contain sanitized summaries and evidence manifests, not private operational evidence. Collect evidence from the start of operation; do not manufacture historical records later. Preserve the only complete copy of existing benchmark evidence under the M1 owner directive.

## 5. Delivery gates

1. **Current M1:** complete evidence preservation, two additional seeded independent run sets with alternating order, Go-overhead investigation, compiler-relevant incremental build measurements and real child-process interruption tests. Every official measurement must pass the full environmental guard at the start of each run. Keep successful throughput separate from refusals. Update the scorecard, recommend a language, and await final owner acceptance of ADR-0002.
2. **M2..M8:** design and implement the applicable ENT controls with their existing kernel/broker/approval/adapter work. Keep inference, graph and management outside the deterministic permission path.
3. **M9/M12 self-hosted release:** satisfy existing release gates plus ENT-R01..R09 as applicable to supported features; ship threat/coverage limits, deployment hardening, role and data-flow descriptions, SBOM/provenance, recovery procedures, evidence export instructions and the responsibility matrix. Independent security review and recorded remediation/disposition precede enterprise enforcement claims.
4. **M10 enterprise profile:** verify PostgreSQL coordination, isolation, migrations, backups/restores, failure handling, load and resource quotas on supported configurations. Multi-worker support stays disabled/unclaimed until its gate passes.
5. **Hosted/managed commercial offering:** document scope and commitments, assign operators, complete readiness/gap assessment and remediate launch blockers; owner records assurance requirements with CPA/customer input before commitments are sold. If Type I is promised, obtain the report first. Plan and retain Type II operating evidence for any Writ-operated service that requires it; it is not delegated to customers.

No gate is passed by documentation alone. Existing independent fixture review, live-host obligations, ADR-0008 conditions and production/release NO-GO remain in force.

## 6. Authoritative references

- [AICPA SOC suite and report resources](https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services/): service organization examination framework.
- [AICPA Trust Services Criteria, revised points of focus 2022](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022): mapping baseline, subject to edition review at examination scoping.
- [AICPA SOC 2 report review checklist](https://assets.ctfassets.net/rb9cdnjh59cm/3xbcLlNc5rd72So4nQpNIk/7a3e8e5945c78c35fc5e116859b96e6a/SOC_2_Report_%C3%82_Review_Checklist.pdf): report scope, period, opinion and licensed CPA issuer.
- [PostgreSQL continuous archiving and PITR](https://www.postgresql.org/docs/current/continuous-archiving.html): backup/recovery capability; deployment configuration and restore verification are separate obligations.
