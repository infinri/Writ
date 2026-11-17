# SR-06 live-host evidence template

Copy this template for each run. Keep full raw logs, with access limited to the operator, outside the repository. Review exported evidence for credentials, cookie values, source paths tied to a personal account, prompts containing private content and other identifying values. This template records planning status until a named operator performs the live trials.

## Run record

| Field | Value |
|---|---|
| Run ID, named operator, start/end with timezone | Pending |
| Reviewed Writ SHA and V1 source SHA | Pending |
| Guest name, designation confirmed by owner, rollback reference | Pending |
| Ubuntu build, image SHA-256, kernel, QEMU/KVM version | Pending |
| Claude Code exact version at start and end, official artifact URL and SHA-256 | Pending |
| Node, Python, jq and Bash versions | Pending |
| Exact synthetic fixture revision and local Git remote check | Pending |
| Effective isolation and network policy revision | Pending |
| PF-1 through PF-8 results and sanitized evidence references | Pending |
| Non-secret credential issuance reference and revocation time | Pending |
| Evidence archive digest, teardown time and hypervisor verification | Pending |

## Per-experiment record

Repeat for each planned case. Record both outcomes for a rerun under separate attempt numbers. Preserve a sanitized copy of the loaded hook registration and settings where safe, or an immutable digest and restricted evidence reference.

| Field | Value |
|---|---|
| Experiment ID, priority group, arm, attempt, timestamp | Pending |
| Fault or condition, tool variant, permission mode | Pending |
| `onFailure`, timeout, matcher, loaded hook SHA-256, settings SHA-256 | Pending |
| Hook ran and independent observation source | Pending |
| Tool attempted, actual control involved and effect evidence | Pending |
| Exit code and sanitized stdout/stderr references | Pending |
| Classification (`BLOCKED`, `EXECUTED`, `ERROR-EXECUTED`, `ERROR-BLOCKED`, `VOID`) | Pending |
| Blocked by (hook, permission rule, prompt, other, n/a) | Pending |
| Documented expectation confirmed and follow-up | Pending |

## Coverage and closure

| Section 6 group | Planned | Executed | Conclusive | Open |
|---|---:|---:|---:|---:|
| P0 | 2 | 0 | 0 | 2 |
| P1 | 18 | 0 | 0 | 18 |
| P1-NC | 2 | 0 | 0 | 2 |
| P2 | 4 | 0 | 0 | 4 |
| P3 | 3 | 0 | 0 | 3 |
| P4 | 2 | 0 | 0 | 2 |
| P5 | 12 | 0 | 0 | 12 |
| P6 | 26 | 0 | 0 | 26 |
| **Priority total** | **69** | **0** | **0** | **69** |

Track the conditional instrumentation checks separately. List any VOID reruns and remaining inconclusive cases. Record a result and evidence reference for each of the six [section 9 requirements](../../docs/audit/m0/SR-06-TRIAL-PLAN.md#9-full-sr-06-acceptance-unchanged-requirement-not-met-by-the-priority-run), including the version-specific UNMEDIATED-TOOLS and adapter requirement updates. Operator disposition remains pending.

| Section 9 requirement | Evidence or missing case | Status |
|---|---|---|
| Preventive registrations and workflow gates under the required fault cases and arms | Pending | Open |
| H-1..H-20 and C1..C10 by observed result | Pending | Open |
| Deferred targets listed in section 9 | Pending | Open |
| Arm A executed effects recorded in UNMEDIATED-TOOLS | Pending | Open |
| Version-specific V2 Claude Code adapter requirement updated | Pending | Open |
| Named operator results and TEST-EVIDENCE entries | Pending | Open |
