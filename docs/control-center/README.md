# Writ Control Center: product specification

**Status: PLANNED.** Owner-approved first-class product requirement ([D-015](../implementation/DECISION-LOG.md), [ADR-0007](../decisions/ADR-0007-control-center-management-plane.md), 2026-11-06). No Control Center code, management API or UI exists. Every requirement below is a target with a definition of done; none is implemented or verified.

**Guiding principle: simple for ordinary users, powerful for advanced users, every important action explainable.**

The Control Center lets authorized people understand, configure, monitor, troubleshoot and administer Writ without routinely editing JSON, YAML, scripts or Neo4j directly. It is a separate management plane over versioned, authenticated APIs. It never sits in the kernel's decision path and is never required for headless agent operation.

## Documents

| Document | Content |
|---|---|
| This page | Scope, navigation, functional requirements with definitions of done, beta and enterprise feature sets |
| [Journeys and navigation](journeys-and-navigation.md) | Information architecture, global UX, guided workflows, user journeys |
| [Management API](management-api.md) | Service architecture, API conventions, CLI parity, hot-path isolation |
| [Trust and authorization](trust-and-authorization.md) | Trust boundaries, principals, roles, authorization rules for administrative changes |
| [Audit and observability](audit-and-observability.md) | Data classes, retention, correlation, "Why did this happen?" model |
| [Lifecycle semantics](lifecycle-semantics.md) | Create, update, version, publish, rollback, archive and delete semantics for policies, contracts, knowledge and memory |
| [Acceptance tests](acceptance-tests.md) | Security and usability acceptance tests |
| [Implementation plan](implementation-plan.md) | Milestone distribution M2 to M10+, tasks and gates |

## Scope and non-goals

In scope: the six navigation areas below, global search, explanations, guided workflows, CLI and API parity for essential administration.

Not in scope, by design:

- **Not a decision authority.** It cannot permit, deny or approve anything by itself; it calls the services that own those records (approval service, policy registry), which enforce their own rules.
- **Not in the hot path.** A Control Center or management-service outage never changes a kernel decision or broker outcome (INV-27).
- **Not a bypass.** No UI control or management API call can disable a mandatory security invariant (INV-29).
- **Not a secret viewer.** Secret values are never returned by any management API (INV-31).
- **Not a database console.** No raw Cypher or SQL execution against production stores. Advanced users get structured queries with bounds.
- **Not required.** Kernel, broker and host adapters run without it (CC-G13).

## Navigation

| # | Area | Contains |
|---|---|---|
| 1 | Overview | Health, activity, errors, latency, resource use, pending approvals, warnings |
| 2 | Activity & Audit | Decisions, effects, approvals, policy evaluations, logs, traces, timelines, explanations |
| 3 | Rules & Policies | Policies, approval requirements, advisory guidance, unverified requirements, **Contracts** |
| 4 | Knowledge & Memory | **Knowledge Base** (sources, documents, indexes) and **Persistent Memory** (memories, decisions, relationships) |
| 5 | Integrations | Host adapters (Claude Code, Codex, future hosts), Capability Packs, coverage |
| 6 | Administration | Projects, tenants, users, roles, keys, approval settings, backups, migrations, logging, security, diagnostics |

Global elements on every screen: search, filters, breadcrumbs, contextual help, Basic/Advanced toggle, tenant and project scope selector, and the "Why did this happen?" action on any decision, effect, failure or unavailable state. Details in [journeys and navigation](journeys-and-navigation.md).

## Enforcement state labels

Every rule, capability, integration path and effect carries exactly one label, derived from registry and test evidence, never from configuration intent alone:

| Label | Meaning | Required evidence |
|---|---|---|
| ENFORCED | Broker-mediated; no effect without a valid permit | Registered enforcement point, passing negative test (INV-01, INV-20) |
| APPROVAL REQUIRED | Enforced, and a human approval is needed per effect | As ENFORCED plus approval binding tests (INV-06) |
| HOOK-INTEGRATED (ADVISORY) | Host hook can block selected events; not an OS sandbox | Host adapter coverage record; SR-06-class live evidence before any "blocks" claim |
| ADVISORY | Guidance shown to agents; no blocking | Registry entry in the advisory registry |
| UNMEDIATED | Writ cannot see or stop it | Host coverage record (for example `UNMEDIATED-TOOLS.md`) |
| UNVERIFIED | Declared requirement without verified implementation | Default for anything lacking the evidence above |

A missing or stale evidence link downgrades the label to UNVERIFIED; it never upgrades.

## Functional requirements

Tier: **B** = minimum usable Control Center for the first local V2 beta; **E** = enterprise feature set. Milestones refer to the [master plan](../implementation/MASTER-IMPLEMENTATION-PLAN.md). Test IDs refer to [acceptance tests](acceptance-tests.md). Numeric targets are initial targets, calibrated when measured; they are not measured facts.

### Global

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-G01 | Global search across audit events, rules, contracts, knowledge sources, memories, integrations, users | One query returns typed results from each entity class within the caller's authorized scope; zero out-of-scope results in CC-SEC-03; p95 under 1 s on the reference dataset (100k audit events, 10k sources, 50k memories) | M9 | B |
| CC-G02 | Filtering, pagination, bounded data | Every list API is cursor-paginated, default 50, maximum 500 per page; every query has a server-side timeout (default 5 s); no endpoint returns an unbounded set (CC-SEC-14) | M2/M3 | B |
| CC-G03 | Breadcrumbs and stable deep links | Every entity and filtered view has a stable URL that reproduces the view for an authorized user and shows an access-denied page (no data, no existence leak across tenants) for others | M9 | B |
| CC-G04 | Contextual help | Every screen and every API error code links to a help topic; coverage check in CI reports 100% | M9 | B |
| CC-G05 | Basic and Advanced views | Basic hides raw ids, digests and JSON; Advanced shows them read-only by default. Switching view never changes authorization (CC-SEC-17) | M9 | B |
| CC-G06 | "Why did this happen?" explanation | Available for every decision status (ALLOW, DENY, REQUIRE_APPROVAL, UNAVAILABLE, INVALID), every effect state and every recorded failure; built deterministically from recorded records only, never from model output (CC-SEC-11); golden explanation fixtures pass for all 26 M0 golden cases | M3 (model), M9 (UI) | B |
| CC-G07 | Enforcement state labels | Every rule, capability and integration path shows exactly one label from the table above; a fixture with a removed enforcement point displays UNVERIFIED (CC-SEC-18) | M6 | B |
| CC-G08 | Confirmation, preview and rollback | Every mutating operation has a preview (dry-run) API; destructive operations require typed confirmation of the object name; versioned objects support rollback; CC-UX-06 passes | M6 to M9 | B |
| CC-G09 | Accessibility | WCAG 2.2 AA: automated scan has zero serious or critical violations on every screen; manual keyboard-only and screen-reader pass for every guided workflow (CC-UX-01, CC-UX-02) | M9 | B |
| CC-G10 | Responsive layout | Every screen usable without horizontal scrolling at 360, 768 and 1280 px widths (CC-UX-07) | M9 | B |
| CC-G11 | Helpful error states | Every API error code has a user message, probable cause and recovery step; no stack traces or internal paths in responses (CC-UX-05) | M2 (codes), M9 (UI) | B |
| CC-G12 | Minimal local configuration | Local install reaches the Overview screen with zero configuration files edited, in under 5 minutes on reference hardware; first administrator created by a local CLI bootstrap (CC-UX-04) | M9 | B |
| CC-G13 | Headless independence | Kernel, broker, adapters and their test suites run with the management service and UI absent; CI job proves it (CC-SEC-01) | M2 onward | B |
| CC-G14 | CLI and API parity | Every operation in the essential set ([management API](management-api.md#essential-operations)) is available via `writ` CLI and API; parity test enumerates both (CC-UX-08) | M9 | B |

### 1. Overview

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-O01 | Health of kernel, broker, adapters, indexes, graph | Each component shows HEALTHY, DEGRADED, UNAVAILABLE or UNKNOWN with last-check time; a check older than twice its interval shows UNKNOWN; fault injection changes the displayed state within 10 s | M9 (local), M5 (adapters) | B |
| CC-O02 | Activity, errors, latency, resource usage | Decisions per minute, error rate, decision p95, broker outcomes, RSS and disk per component over 1 h, 24 h, 7 d; sourced from metrics, labelled best-effort | M9 | B |
| CC-O03 | Pending approvals and warnings | Counts link to filtered lists; warnings include expiring keys, stale indexes, UNKNOWN effects awaiting reconciliation, unverified integrations | M4/M9 | B |

### 2. Activity & Audit

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-A01 | Search decisions, effects, approvals, policy evaluations, errors, logs, traces | Filter by time, tenant, project, principal, action kind, status, policy version, trace id; results paginated and bounded | M3 (query model), M9 (UI) | B |
| CC-A02 | Explain permitted, denied, failed, unavailable | CC-G06 applied to each record type | M3/M9 | B |
| CC-A03 | Cross-component correlation | Given a trace id or request id, the timeline shows adapter, kernel, approval, broker and receipt events in causal order; fixture with all trace stages passes | M3 | B |
| CC-A04 | Attempted versus executed effects | Effect states INTENT, DISPATCHED, COMPLETED, FAILED, UNKNOWN_RECONCILE shown distinctly; crash fixture shows DISPATCHED without receipt as UNKNOWN_RECONCILE, never as COMPLETED | M3 | B |
| CC-A05 | Unmediated operations | Host coverage data lists UNMEDIATED capabilities per integration; observed unmediated activity (where a host reports it) is labelled and never counted as governed | M5 | B |
| CC-A06 | Audit export with integrity proof | Export of a filtered range includes chain verification data; an offline verifier detects a modified, removed or reordered record | M9 | B |
| CC-A07 | SIEM streaming, legal hold, long-term archive | Configurable export targets; legal hold blocks retention expiry for selected scopes | M10 | E |

### 3. Rules & Policies (including Contracts)

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-R01 | Create, edit, clone, validate, simulate, compare, version, publish, activate, deactivate, retire, roll back | Each operation exists in API and UI with the semantics in [lifecycle semantics](lifecycle-semantics.md#policies); published versions immutable (CC-SEC-12) | M6 | B |
| CC-R02 | Distinguish enforced policy, approval requirements, advisory guidance, unverified requirements | Separate registries and labels (CC-G07); an advisory rule cannot be published into the enforced bundle without an enforcement binding and tests (INV-20) | M6 | B |
| CC-R03 | No toggle bypasses mandatory invariants | Deactivating, editing or reordering a mandatory policy is refused by the management service and the policy registry, not merely hidden in the UI (CC-SEC-04) | M6 | B |
| CC-R04 | Simulation | A draft is evaluated against recorded or sample requests; output lists every decision that would change versus the active bundle; no effect executes | M6 | B |
| CC-R05 | Publish controls | Publishing requires an authenticated publisher role, passing tests, conflict analysis and a signed bundle (M6 signing); single-user local installs record an explicit self-publish acknowledgment | M6 | B |
| CC-R06 | Staged rollout, shadow comparison, separation of duties | Author and publisher must differ; shadow mode reports divergence before activation | M10 | E |
| CC-C01 | Browse contracts, inspect fields and versions | Every schema in the contract registry browsable with field docs and version history | M6 | B |
| CC-C02 | Validate example requests | User pastes or uploads an example; validation result lists each error with JSON pointer | M6 | B |
| CC-C03 | Register custom contracts and domain extensions | Custom contracts live in a tenant or pack namespace; they cannot shadow core contract ids | M6 | B |
| CC-C04 | Breaking-change detection | A new version that removes a field, narrows a type or adds a required field is flagged breaking and requires a major version | M6 | B |
| CC-C05 | Dependencies and safe deprecation | Deprecation shows dependent packs, policies and integrations; retiring a contract with active dependents is refused | M6 | B |
| CC-C06 | Core security contracts protected | Core contracts (ActionRequest, Decision, ApprovalGrant, BrokerPermit, EffectReceipt, PrincipalProof, PolicyBundle, AuditEvent) are read-only through the management plane; changes only through a source release with an ADR (CC-SEC-13) | M6 | B |

### 4. Knowledge & Memory

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-K01 | Upload and import documents, repositories and supported sources | Import job per source with progress, bounded size, file-type allowlist; content treated as untrusted data (INV-11) | M7 | B |
| CC-K02 | Browse and search knowledge | Search shows the same scoped results retrieval would use, with scores in Advanced view | M7 | B |
| CC-K03 | Provenance | Each chunk links to source, version, import job, importer principal and content digest | M7 | B |
| CC-K04 | Edit metadata | Title, tags, authority level, owner; each edit versioned and audited | M7 | B |
| CC-K05 | Refresh, reindex, archive, delete eligible sources | Semantics and eligibility in [lifecycle semantics](lifecycle-semantics.md#knowledge-sources); deletion reconciliation verified (CC-SEC-10) | M7 | B |
| CC-K06 | Conflicts, duplicates, stale information, index failures | Dashboard lists each class with counts and drill-down; index failure shows the failing source and error | M7 | B |
| CC-K07 | Retrieval observability | For a query, show candidates, filters applied (scope, authority, staleness), final ranking and abstention reason | M7 | B |
| CC-M01 | Explore memories, decisions, experiences, relationships | Graph and list views scoped to tenant and project; bounded neighborhood (default depth 2, max 200 nodes) | M8 | B |
| CC-M02 | Sources and confidence | Each memory shows origin, evidence, confidence, consent status and retention | M8 | B |
| CC-M03 | Correct, supersede, archive, remove eligible records | Correction creates a new version with a supersedes link; semantics in [lifecycle semantics](lifecycle-semantics.md#memories) | M8 | B |
| CC-M04 | Retention, provenance, consent, isolation | Enforced by the memory service, not the UI; cross-tenant access attempts refused (CC-SEC-03) | M8 | B |

### 5. Integrations

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-I01 | Configure Claude Code, Codex and future host adapters | Guided install and configuration for each supported adapter; configuration versioned and audited | M5 | B |
| CC-I02 | Coverage, installation, errors, compatibility | Per adapter: version, host version, registered events, coverage per capability with label, last error, compatibility status | M5 | B |
| CC-I03 | Capability Pack management | Install, verify, enable, disable, upgrade; enabling a pack never adds a permission (CC-SEC-16, INV-23) | M6 | B |
| CC-I04 | Verified, advisory and unmediated behaviors distinguished | Labels per CC-G07; "verified" requires live-host evidence of the SR-06 class for that host version | M5 | B |
| CC-I05 | Fleet view of adapters across hosts | Many installations reporting to one management service | M10 | E |

### 6. Administration

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-AD01 | Projects and tenants | Create, rename, archive; deletion follows retention rules; local beta supports one tenant with many projects | M2 (contracts), M9 | B (single tenant), E (multi-tenant) |
| CC-AD02 | Users, roles, permissions | Built-in roles from [trust and authorization](trust-and-authorization.md#roles); local accounts | M9 | B |
| CC-AD03 | Keys | Metadata only (id, purpose, created, expires, last used, status); rotate and revoke; secret values never returned (CC-SEC-05) | M3/M9 | B |
| CC-AD04 | Authenticated approvals | Approver assignment, step-up policy, expiry defaults | M4 | B |
| CC-AD05 | Settings | Versioned, validated, audited; preview before apply | M9 | B |
| CC-AD06 | Backup and restore | Scheduled and on-demand backup; restore drill shows integrity check and audit continuity | M9 | B |
| CC-AD07 | Migrations | Shows pending schema and data migrations, preview, apply, rollback point | M9 | B |
| CC-AD08 | Logging configuration | Levels and sinks per component; cannot disable security audit | M9 | B |
| CC-AD09 | Security posture | Shows enforcement profile, unverified items, key expiry, open risk acceptances | M9 | B |
| CC-AD10 | Diagnostics | Redacted support bundle; canary-secret test proves redaction (CC-SEC-05) | M9 | B |
| CC-AD11 | SSO (OIDC, SAML), SCIM provisioning, custom roles, delegated tenant admins, quotas | Enterprise identity and administration | M10 | E |

### Approval experience (M4)

| ID | Requirement | Definition of done | Milestone | Tier |
|---|---|---|---|---|
| CC-AP01 | Approval inbox | Each request shows principal, action, canonical target, operation digest, policy version, risk notes and expiry; content rendered as untrusted text | M4 | B |
| CC-AP02 | Approve or deny with step-up authentication | Approval requires a fresh human authentication; the grant binds exactly the displayed digest; a swapped target or changed content is refused (CC-SEC-07) | M4 | B |
| CC-AP03 | Revoke and audit | Unused grants revocable; every approval action audited | M4 | B |
| CC-AP04 | Quorum, delegation, out-of-band channels | Multi-approver policies, mobile or chat approval with the same binding | M10 | E |

## Minimum usable Control Center (first local V2 beta)

Every requirement with tier **B** above. In summary: Overview health and activity; Activity & Audit search, correlation, effect states and explanations; approval inbox with step-up; Rules & Policies lifecycle with simulation and guarded publish; contract browsing and validation; knowledge import, search, provenance, reindex and safe deletion; memory exploration and correction; adapter and pack status with honest labels; single-tenant administration, keys metadata, backup and restore, diagnostics; CLI parity for the essential operations.

Complete by the M9 gate. The beta release itself still requires the release gate (REL-01, M5-SR06, M6-RULES and the release-standard decision); a finished Control Center does not authorize a release.

## Enterprise feature set (M10+, where justified)

Every requirement with tier **E**: multi-tenant administration, SSO and SCIM, custom roles and delegated admins, separation of duties for publishing, staged rollout and shadow policies, approval quorum and out-of-band channels, SIEM streaming, legal hold, fleet adapter view, quotas. Each enters the plan only with a workload or customer requirement recorded in an ADR.

## Relationship to M1

The Control Center is **not** a prerequisite for M1. M1 builds only the Rust and Go kernel prototypes. The language decision in ADR-0002 informs, but does not dictate, the management-service language, because the management plane is outside the hot path ([ADR-0007](../decisions/ADR-0007-control-center-management-plane.md)).
