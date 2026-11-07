# Writ V2 security and correctness invariants

**Normative.** Every invariant requires executable positive AND negative controls before a V2 release may claim it.

| ID | Invariant | Required negative control |
|---|---|---|
| INV-01 | No privileged effect without broker-authorized, current, scoped permit | Call effect adapter directly without permit |
| INV-02 | Agent/model text cannot mint or expand authorization | Forge transcript with 'approved' |
| INV-03 | Hard DENY never becomes approval/allow through retries or mode | Repeat denied action and switch workflow |
| INV-04 | Same normalized action/policy/state produces same decision | Cross-host differential fixture |
| INV-05 | Missing identity or unverifiable policy cannot permit privileged effect | Remove identity and damage bundle signature |
| INV-06 | Every approval binds tenant, principal, action, target, digest, policy version, nonce and expiry | Replay, swap target, change file after check |
| INV-07 | Approval consume is atomic and replay-safe | Two concurrent execution requests using same token |
| INV-08 | Every privileged effect attempt has durable intent and receipt/reconciliation record | Crash between dispatch and receipt |
| INV-09 | Duplicate idempotency key cannot produce different effects | Reuse key with changed operation |
| INV-10 | Cross-tenant and cross-project confidential memory is never returned | Shared term linking to foreign graph neighbor |
| INV-11 | Retrieved content has no effect on permission lattice | Insert malicious instruction in document |
| INV-12 | Policy changes are versioned, authorized and auditable | Agent attempts policy edit through ordinary file tool |
| INV-13 | Unknown tool/capability is not silently treated as covered | New host tool routed to old allow-default hook |
| INV-14 | Effect target is canonical at decision and use time | Symlink/path substitution or alternate separator |
| INV-15 | Failure posture is explicit and testable | Kill graph, daemon, ledger and telemetry independently |
| INV-16 | Advisory checks never claim to block a completed effect | Failing PostToolUse validator presented as prevention |
| INV-17 | State transitions are revision-controlled | Concurrent phase changes on stale revision |
| INV-18 | Local operation has bounded memory, startup, latency and input size | Oversized argument and malformed envelope |
| INV-19 | Trust boundary never relies on a mutable ambient 'current session' guess | Interleave two sessions/projects |
| INV-20 | All active enforced rules bind to real implementations and tests | Remove validator file and attempt policy publish |
| INV-21 | Kernel and broker cannot depend on model/provider SDK availability | Simulate model outage while evaluating a policy request |
| INV-22 | External-agent host mode requires no Writ-managed model calls | Run simulated host task with ModelAdapter absent |
| INV-23 | Capability Pack cannot grant permissions or modify organizational policy | Pack requests forbidden network/tool capability |
| INV-24 | Code, research and noncode review use same TaskSpec/evidence contract | Validate 3 task fixtures against identical schema |
| INV-25 | Model-based evaluation is never misrepresented as independent verification | Feed fabricated citations and unsupported source claims |
| INV-26 | Model routing/fallback respects tenant privacy and capability requirements | Force fallback to unavailable capability or forbidden region |
| INV-27 | The management plane (Control Center, management service) is outside the decision path; its absence or failure never changes a decision or effect outcome | Kill or overload the management service during the parity and broker suites (CC-SEC-01) |
| INV-28 | Management APIs accept only authenticated management-realm principals; agent-realm credentials are refused and UI state never widens authorization | Call every management endpoint with an adapter or agent credential (CC-SEC-02) |
| INV-29 | No management action can disable, weaken or reorder a mandatory policy or edit a core security contract | Crafted bundle omitting a mandatory policy sent directly to the registry (CC-SEC-04, CC-SEC-13) |
| INV-30 | Administrative mutations are audited before they take effect; knowledge and memory deletion never removes security audit records | Audit store down during a mutation; delete content referenced by decisions (CC-SEC-08, CC-SEC-09) |
| INV-31 | No management API, export or diagnostics bundle returns a secret value | Canary secrets planted in keys, settings and logs (CC-SEC-05) |
| INV-32 | (PROPOSED, ADR-0008) No protected workflow transition without a kernel ALLOW for `workflow.transition` and a current acceptance-service AcceptanceRecord; agents cannot write workflow state | Direct state-store write; promotion with no record (AG-ADV-12, AG-NEG-10) |
| INV-33 | (PROPOSED) An AcceptanceRecord binds tenant, project, task, workflow, transition, artifact digest, acceptance-policy version, evaluator identities, evidence digests and state revision; any mismatch at use time refuses | Swap artifact after evaluation; change policy version (AG-NEG-01, AG-NEG-09, AG-PROP-01) |
| INV-34 | (PROPOSED) Only registered evaluators of sufficient trust satisfy mandatory criteria; producer, agent and model-assisted claims never satisfy mechanical, evidence or human criteria | Producer "verified" evidence; model pass on a mechanical criterion (AG-NEG-04, AG-NEG-06) |
| INV-35 | (PROPOSED) Mechanical results count only when executed by the Writ-controlled runner on digest-identified inputs and attested | Agent-supplied test result file (AG-NEG-03) |
| INV-36 | (PROPOSED) Changes to artifact, evidence, acceptance policy, evaluator version or key, scope or state revision make an AcceptanceRecord unusable, enforced at use time | Promotion racing an artifact update (AG-CON-03) |
| INV-37 | (PROPOSED) Acceptance requirements are versioned policy; agents and packs cannot create, weaken or waive them; invariant-derived criteria are never waivable | Pack or agent edits acceptance policy (AG-ADV-09) |
| INV-38 | (PROPOSED) In Governed Execution, missing, failed, stale, unverifiable or malformed mandatory evidence, or an unavailable evaluator or acceptance service, refuses the protected transition | Evaluator timeout and malformed output (AG-FAIL-01..03) |
| INV-39 | (PROPOSED) Advisory Integration never reports a transition or effect as governed or verified; the mode is recorded on every record | Advisory-mode promotion displayed in the Control Center |

## Verification levels

**Unit:** pure authorization and reducer tests, typed boundary tests. **Integration:** real host tool interception and broker execution. **Adversarial:** bypasses, identity forgery, race conditions, process escapes and prompt injection. **Chaos:** interrupted storage, hot reload, unavailable graph. **Performance:** p50/p95/p99 and memory on named hardware. A passing unit test for an advisory hook does not satisfy INV-01 or INV-13.

## Release gate

No P0 invariant violations; full threat suite green; clear scope statement of what the host adapter can actually mediate; recorded risk acceptance for external capabilities outside the broker; rollback and recovery demonstrated; V1 historical behavior not confused with V2 certification.
