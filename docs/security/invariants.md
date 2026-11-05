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

## Verification levels

**Unit:** pure authorization and reducer tests, typed boundary tests. **Integration:** real host tool interception and broker execution. **Adversarial:** bypasses, identity forgery, race conditions, process escapes and prompt injection. **Chaos:** interrupted storage, hot reload, unavailable graph. **Performance:** p50/p95/p99 and memory on named hardware. A passing unit test for an advisory hook does not satisfy INV-01 or INV-13.

## Release gate

No P0 invariant violations; full threat suite green; clear scope statement of what the host adapter can actually mediate; recorded risk acceptance for external capabilities outside the broker; rollback and recovery demonstrated; V1 historical behavior not confused with V2 certification.
