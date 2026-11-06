# Control Center: security and usability acceptance tests

**Status: PLANNED.** No test here has been written or run. Each test becomes a task when its milestone starts and is recorded in [TEST-EVIDENCE](../implementation/TEST-EVIDENCE.md) only after it runs.

## Security

| ID | Test | Pass condition | Invariant | Milestone |
|---|---|---|---|---|
| CC-SEC-01 | Hot-path independence: stop, crash and overload the management service while the parity and broker suites run | Identical decisions and receipts; kernel decision p95 within run-to-run variance of the baseline | INV-27, CC-G13 | M2 onward |
| CC-SEC-02 | Agent-realm credential (adapter, agent, subagent, AgentRunner) calls every management endpoint | Every call refused; refusal audited | INV-28 | M2/M3 |
| CC-SEC-03 | Cross-tenant and cross-project access through search, lists, deep links, timeline joins, graph exploration | Zero foreign results; not-found responses indistinguishable from nonexistent ids | INV-10 | M3, M7, M8 |
| CC-SEC-04 | Deactivate, edit, reorder or omit a mandatory policy through UI, API and a crafted bundle sent directly to the registry | All refused by the registry; active bundle unchanged | INV-29 | M6 |
| CC-SEC-05 | Plant canary secrets in keys, settings, environment and logs; call every endpoint and produce a diagnostics bundle | No canary value appears in any response, export or bundle | INV-31 | M3, M9 |
| CC-SEC-06 | CSRF, cross-origin requests and DNS rebinding against the local service | Refused (Origin, Host, SameSite, CSRF token) | INV-28 | M9 |
| CC-SEC-07 | Approval swap: change target, content or digest between display and approve; approve without step-up; approve own request | All refused; no grant created | INV-06, P-03 | M4 |
| CC-SEC-08 | Audit store unavailable during a management mutation | Mutation refused; no partial change | INV-30, INV-15 | M3 |
| CC-SEC-09 | Delete a knowledge source and a memory referenced by decisions | Security audit chain verifies and still contains the referencing records | INV-30 | M7, M8 |
| CC-SEC-10 | Knowledge deletion reconciliation | Zero index and graph residue for the source (by id, chunk id, digest); dependent memories flagged `needs_review` | INV-30 | M7 |
| CC-SEC-11 | Explanations for all decision statuses and effect states with the model provider absent and with injected model text in records | Explanations produced; none cites model text as cause | INV-25, INV-21 | M3 |
| CC-SEC-12 | Modify a published bundle in storage; roll back | Modified bundle rejected on sha mismatch; rollback creates a new activation event; history unchanged | INV-12 | M6 |
| CC-SEC-13 | Edit or delete a core security contract through the management plane | Refused | INV-29 | M6 |
| CC-SEC-14 | Unbounded requests: page size over max, deep graph expansion, slow query | Capped or refused within the timeout; service stays responsive | INV-18 | M2/M3 |
| CC-SEC-15 | Stored XSS and markup injection through knowledge content, memory text, request arguments and log lines | Rendered as text; CSP blocks inline script; no script executes | INV-11 | M9 |
| CC-SEC-16 | Enable a pack requesting forbidden capabilities | No decision changes (differential test across the golden set) | INV-23 | M6 |
| CC-SEC-17 | Switch Basic and Advanced views and scope selector while requesting restricted data | Authorization outcomes identical | INV-28 | M9 |
| CC-SEC-18 | Remove an enforcement implementation behind an ENFORCED rule | Label becomes UNVERIFIED; publish of a bundle claiming enforcement fails | INV-20, INV-16 | M6 |

## Usability

| ID | Test | Pass condition | Milestone |
|---|---|---|---|
| CC-UX-01 | Automated accessibility scan (axe-core or equivalent) on every screen in light and dark themes | Zero serious or critical violations | M9 |
| CC-UX-02 | Manual keyboard-only and screen-reader walkthrough of the ten guided workflows | Each workflow completable; findings fixed or recorded with severity | M9 |
| CC-UX-03 | Moderated task study, at least 5 participants not on the core team, 8 tasks drawn from the [user journeys](journeys-and-navigation.md#user-journeys) | At least 80% task success without documentation; median time per task recorded; System Usability Scale at least 70 | M9 |
| CC-UX-04 | Fresh local install on reference hardware | Overview reached with zero configuration files edited in under 5 minutes | M9 |
| CC-UX-05 | Error catalogue coverage | 100% of API error codes have message, cause, recovery step and help link | M9 |
| CC-UX-06 | Destructive operations | Each shows preview of impact and requires typed confirmation; undo available during grace windows | M9 |
| CC-UX-07 | Responsive layouts at 360, 768, 1280 px | No horizontal scrolling; status labels visible | M9 |
| CC-UX-08 | CLI parity | Script enumerates the essential operations and finds each in both CLI and API | M9 |
| CC-UX-09 | "Why did this happen?" on the 26 M0 golden decisions | Each explanation names the deciding rule or missing condition consistent with the golden reason code | M3 (service), M9 (UI) |

Usability results are measurements, not verdicts: report participant counts, tasks and raw numbers.
