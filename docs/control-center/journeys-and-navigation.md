# Control Center: user journeys and navigation architecture

**Status: PLANNED.** Design target; no UI exists. Requirement ids refer to the [product specification](README.md).

## Information architecture

~~~text
Writ Control Center
├── Overview                      health, activity, errors, latency, resources, approvals, warnings
├── Activity & Audit
│   ├── Timeline                  correlated events by trace or request id
│   ├── Decisions                 status, policy version, matched rules, explanation
│   ├── Effects                   intent, dispatched, completed, failed, unknown (reconcile)
│   ├── Approvals                 inbox, history, revocations
│   ├── Logs and traces           operational logs, OpenTelemetry traces (best effort)
│   └── Unmediated activity       per integration coverage gaps and observed events
├── Rules & Policies
│   ├── Enforced policies         bundles, versions, simulation, publish, rollback
│   ├── Approval requirements
│   ├── Advisory guidance         separate registry; never shown as enforced
│   ├── Unverified requirements
│   └── Contracts                 core (read-only), custom, versions, validation, dependencies
├── Knowledge & Memory
│   ├── Knowledge Base            sources, imports, search, provenance, health (stale, duplicate, conflict, failed)
│   ├── Retrieval inspector       candidates, filters, ranking, abstention for a query
│   └── Memory                    memories, decisions, experiences, relationships, consent, retention
├── Integrations
│   ├── Host adapters             Claude Code, Codex, future hosts: install, coverage, errors, compatibility
│   └── Capability Packs          install, verify, enable, disable, upgrade
└── Administration
    ├── Projects and tenants
    ├── Users and roles
    ├── Keys                      metadata only
    ├── Approval settings
    ├── Settings
    ├── Backup, restore, migrations
    ├── Logging
    ├── Security posture
    └── Diagnostics
~~~

## Global UX elements

| Element | Behavior |
|---|---|
| Scope selector | Tenant and project; every query is filtered server-side by the selected scope intersected with the caller's grants. Changing scope never widens authorization |
| Global search | Typed results grouped by entity class; keyboard shortcut `/`; results respect scope (CC-G01) |
| Filters | Saved per user; encoded in the URL (CC-G03) |
| Breadcrumbs | Area > list > entity > sub-view; every crumb is a link |
| Contextual help | `?` on each screen and next to each error; links to the help topic for that screen or error code (CC-G04) |
| Basic / Advanced | Per-user preference; Basic uses plain language and hides ids and digests; Advanced shows raw records read-only (CC-G05) |
| "Why did this happen?" | Button on every decision, effect, failure and UNAVAILABLE status; opens an explanation panel ([audit model](audit-and-observability.md#why-did-this-happen)) |
| State badges | ENFORCED, APPROVAL REQUIRED, HOOK-INTEGRATED (ADVISORY), ADVISORY, UNMEDIATED, UNVERIFIED; component health HEALTHY, DEGRADED, UNAVAILABLE, UNKNOWN. Text plus icon, never color alone |
| Confirmation | Destructive actions show a preview of impact and require typing the object name (CC-G08) |
| Empty and error states | Explain what is missing and the next step (CC-G11) |

## Guided workflows (beta)

Each is a step-by-step flow with preview, confirmation and a link to the resulting audit record.

1. **First run:** bootstrap the first administrator through the local CLI, open the Overview, connect the first host adapter.
2. **Connect a host adapter:** choose host, install, verify registration, view coverage labels including UNMEDIATED capabilities.
3. **Review and decide an approval:** open the request, inspect target and digest, step-up authenticate, approve or deny.
4. **Investigate a denied or failed action:** search by time or trace id, open the timeline, use "Why did this happen?", follow the link to the rule or missing approval.
5. **Change a policy safely:** clone the active policy, edit, validate, simulate against recent requests, review the diff of changed decisions, publish, verify, roll back if needed.
6. **Add knowledge:** import a source, watch the job, inspect provenance and index health, run a test retrieval.
7. **Correct a memory:** find the memory, view sources and confidence, supersede with a correction, confirm retrieval returns the new version.
8. **Delete a knowledge source:** preview the impact (chunks, graph nodes, edges, dependent memories), confirm, verify reconciliation completed.
9. **Rotate a key:** view metadata, rotate, confirm dependents picked up the new key, revoke the old one.
10. **Back up and restore:** run a backup, run a restore drill into a scratch location, view the integrity report.

## User journeys

| Persona | Goal | Journey | Requirements |
|---|---|---|---|
| Developer using Claude Code locally | Understand why an edit was blocked | Overview warning > Activity > decision > "Why?" shows rule, policy version and that the path is protected; help explains how to request approval | CC-A01, CC-G06 |
| Approver | Approve a deployment command safely | Inbox > request shows exact command digest and target > step-up > approve; later sees the receipt linked to the grant | CC-AP01, CC-AP02, CC-A04 |
| Policy author | Tighten network egress rules | Clone > edit > simulate shows 3 decisions would change > publish request > publisher approves > active; rollback available | CC-R01, CC-R04, CC-R05 |
| Knowledge curator | Remove an outdated runbook | Knowledge > source > delete preview lists 120 chunks and 14 graph edges > confirm > reconciliation report shows zero remaining | CC-K05, CC-SEC-10 |
| Memory steward | Fix a wrong project decision recorded by an agent | Memory > record > sources and confidence > supersede with corrected text > old version archived and linked | CC-M02, CC-M03 |
| Security auditor | Prove no effect ran without authorization last month | Activity > Effects > filter > export with integrity proof > offline verifier passes | CC-A04, CC-A06 |
| Installation admin | Recover after disk failure | Administration > Backup > restore drill > integrity report > Overview healthy | CC-AD06 |
| Integration engineer | See what Codex integration does not cover | Integrations > Codex > coverage table lists UNMEDIATED capabilities and the evidence for each label | CC-I02, CC-I04 |

## Accessibility and responsiveness

Target WCAG 2.2 AA (CC-G09): keyboard reachability of every control with visible focus; target size at least 24 by 24 CSS px; no information by color alone; text contrast 4.5:1; status changes announced through live regions; no time limits on approval review other than the grant expiry, which is displayed. Layouts adapt at 360, 768 and 1280 px (CC-G10); dense tables collapse to cards on narrow screens without hiding status labels.
