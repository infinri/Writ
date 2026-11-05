# Threat model — Writ V2

## Assets

Authority to perform tool effects; user/organization approvals; source code and secrets; graph and Recall memories; tenant boundaries; audit receipts; policy bundles; evidence lineage; enterprise availability.

## Adversaries

- An agent instructed by malicious or compromised context (including prompt injection).
- A local process or extension with more host privileges than a normal tool event.
- A malicious tenant attempting cross-scope graph retrieval or policy targeting.
- A confused human approving wrong content due to UI ambiguity or swapped operation.
- A compromised plugin/host adapter, replayed event or forged session identifier.
- Ordinary faults: storage crash, network loss, concurrent retries, partial write, out-of-date policy, stale index.

## Boundary analysis

| Entry point | Risk | Control |
|---|---|---|
| Prompt/retrieved document | Instructions promoted to authority | Data-only tagging; separate policy plane |
| Tool argument / shell | Alternate syntax, indirection, path alias | Capability broker + sandbox; avoid universal parser claims |
| Host event envelope | Forged principal/session ID | Verified host-origin identity, typed canonicalization |
| Human approval | Replay/mismatch/scope broadening | Bound digest/expiry/nonce, trusted UI, atomic consume |
| Policy publish | Agent self-approval or malicious update | Signed bundles, publisher RBAC, tests and change audit |
| Neo4j query | Cross-tenant hop, stale evidence | Scoped graph API, row/edge filtering, generated query guards |
| Memory ingestion | Poisoned persistent instruction | Provenance, confidence, consent and separate enforcement plane |
| Broker execution | TOCTOU or duplicate side effect | Revalidate target at use, idempotency, receipts, reconciliation |
| Local state | Symlink tampering, cached session confusion | Secure directories, scoped keys, revisioned storage |
| Extension | Code execution, data egress | Optional WASM with explicit capabilities/fuel/limits; host OS isolation where needed |
| Audit pipeline | Lost/forged history | Append-only durable linkage, integrity verification, independent permissions |

## Enforcement profiles

**Broker-enforced:** Writ owns every declared privileged effect path, or the host supplies independently validated sandbox enforcement. Claims of prevention apply only to these declared actions and boundaries.

**Hook-integrated/advisory:** Writ controls selected hook events only. Agents with independent shell/OS access can perform outside-Writ effects; the product reports those limitations honestly. A hook deny is not an OS sandbox.

## Abuse-case tests

T-01 forged 'approved' transcript; T-02 state-file tamper; T-03 symlink/reparse point; T-04 URI/path normalization; T-05 shell indirection; T-06 Bash wrapper; T-07 pre-/post-effect race; T-08 replayed approvals; T-09 policy downgrade; T-10 foreign graph neighbor; T-11 evidence poisoning; T-12 giant payload over argv limits; T-13 missing interpreter; T-14 concurrent session cross-contamination; T-15 audit failure; T-16 crash at each effect stage; T-17 unsupported host tool.

Threat tests must be executed in an isolated, disposable environment; do not run payloads against unrelated repositories or production hosts.

## Residual risks

Kernel authorization cannot compensate for a compromised host OS or an unmediated privileged process. Neo4j access controls and transactional authorization stores have their own deployment/licensing limits. A permissionless process running outside Writ is not governed by Writ. Record these clearly in release notes.
