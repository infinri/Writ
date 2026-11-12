# M3 plan: authorization broker, transition service and mechanical runner (Rust)

**Status:** AUTHORIZED, IN PROGRESS (owner 2026-11-12, bounded). Baseline: M2 accepted at `eecab4d` (D-031, E-057). ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO. **Outside this authorization:** PostgreSQL, enterprise services, Control Center (including its M3-tagged management items below), real-host integration.

## Scope (MASTER-IMPLEMENTATION-PLAN sections 10 and 18; acceptance-gate-integrity section 13)

**Deliverable:** trusted identity binding at ingress, effect mediation, canonical resource and digest binding, receipts; the protected transition service (CAS, single-use acceptance records, use-time recomputation, audit events); the mechanical runner as a sandboxed broker effect. **Gate:** no effect without valid authorization; TOCTOU, replay and idempotency tests; plus the fixture and risk obligations mapped below. Exactly-once execution is **not** claimed for arbitrary external systems: the broker guarantees at most one dispatch per idempotency key and never redispatches after uncertainty; effects on systems outside the controlled adapter are at-most-once with reconciliation.

## Component boundaries

| Component | Crate (workspace member) | Holds | Never |
|---|---|---|---|
| Kernel | `kernel/` `writ-kernel` (unchanged authority) | pure decisions and structure checks | keys, I/O, clocks, persistence |
| Broker | `broker/` `writ-broker` | sealed ingress verification, use-time re-decision through `writ-kernel`, approval grant consumption, permit issue and verification (full request fingerprint and bundle binding), dispatch intent, receipts, idempotency, reconciliation, transition service, effect-adapter and runner interfaces, trusted clock interface | SQL or storage details; model SDKs |
| Ledger store interface | `broker::store` (trait) | logical transactions (reserve, dispatch, adapter, observe, approve, reconcile, audit, transition CAS) | backend specifics |
| SQLite local profile | `persistence/sqlite/` `writ-ledger-sqlite` | the store trait on SQLite (D-023 engine and settings), single-owner coordination and fencing | network filesystems (unsupported) |
| Controlled effect adapter | `broker::effects` test adapter, then real adapters later | independently observable effects for tests (a separate store the broker can only reach through the adapter) | agent-supplied credentials |
| Mechanical runner | `broker::runner` (sandboxed broker effect) | launches registered commands on digest-identified inputs, signs attestations with a runner key | agent working copies or credentials |

Kernel dependency restrictions stay as they are (`kernel/DEPENDENCIES.md`); the broker and the SQLite crate get their own reviewed lists, and the gate checks each crate's dependency closure separately.

## Coordination boundary (single-owner SQLite profile)

The D-029 coordination gate is process-local and is never presented as cross-process protection. The initial local profile is **single-owner**:

1. **Exclusive ownership lock:** on open, the store takes an exclusive OS lock (`File::try_lock`) on `<ledger>.owner`, held for the store's lifetime. A second broker or a separate reconciler on the same ledger fails closed at startup (`LEDGER_UNAVAILABLE`, operator category `ownership`); there is no waiting or stealing. The OS releases the lock when the owning process exits or is killed, so a restart can take ownership.
2. **Fencing epoch:** after taking the lock, the owner increments `owner_epoch` in the ledger in one `BEGIN IMMEDIATE` transaction and keeps its value; **every** write transaction checks inside the transaction that the stored epoch still equals its own and otherwise rolls back fail closed. A stale instance (for example one whose lock was lost through an unsupported filesystem or manual interference) therefore cannot commit after another owner started.
3. **Startup order:** lock, then schema verification and epoch increment, then serve; reconciliation runs only inside the owning process (exclusive D-029 gate).
4. **Assumptions:** local filesystem with working `flock` semantics; network filesystems are unsupported and not detected; the agent has no write access to the ledger or lock file (real-host enforcement is M5). Multi-owner coordination (leases across hosts) belongs to the PostgreSQL profile (M10) and is not designed here.

No new kernel semantics are introduced. The new broker-level outcome (instance refused because another owner holds the ledger) is reported as `LEDGER_UNAVAILABLE`; if review prefers a distinct code, that is a decision before M3-02.

## Fixture and invariant map (127 M1 cases plus carried obligations)

| Source | Cases | Component | Acceptance |
|---|---|---|---|
| broker-scenarios | 46 | broker (ingress, decision, grants, permits, receipts, reconciliation) on SQLite and in-memory test store | exact expected outputs through a broker command layer and dev-only line adapter |
| protocol-broker | 9 | broker command layer (line protocol through the dev adapter) | exact |
| transition-scenarios | 66 | transition service (CAS, single-use records, revision conflicts) | exact |
| protocol-transition | 6 | transition command layer | exact |
| m1/ledger_tests (D-024..D-029, storage failures, timing integrity where applicable, chaos) | 45 tests | SQLite store and broker | language-neutral tests run against the production adapter |
| M1 property campaigns `broker`, `transition` (memory and SQLite, both concurrency profiles) | generated | broker, transition service | differential against the M1 Rust prototype with an exact-exception gate like M2-06 |

## Tasks (dependency order)

| ID | Task | Acceptance evidence |
|---|---|---|
| M3-00 | Workspace members `broker/`, `persistence/sqlite/`; broker and SQLite dependency review (`rusqlite` =0.41.1 with the D-023 pinned SQLite library and `bundled` forbidden, after RUSTSEC-2026-0459; `hmac` and `sha2` in the broker only, from M3-02); per-crate dependency gate with the kernel closure unchanged; CI with the pinned SQLite | gate passes; kernel closure still exactly the 10 reviewed packages; CI green |
| M3-01 | Store interface and SQLite local profile: open and initialization (D-024 rules), ownership lock, fencing epoch | cross-process tests: concurrent startup (exactly one owner, others fail closed), owner killed by SIGKILL then restart takes ownership with a higher epoch, stale-epoch writes refused, reconciler process refused while a broker owns the ledger |
| M3-02 | Trusted ingress and use-time authorization: seal verification (test keys only, separated from production key loading), re-decision through `writ-kernel`, complete request and permit bindings | broker-scenarios ingress cases; forged, replayed and altered seals refused; TOCTOU tests (decision versus use) |
| M3-03 | Approvals and grants: authenticated human channel interface, atomic single-use consumption, binding comparison | grant cases; concurrent consumption with 64 workers; replayed approvals refused |
| M3-04 | Dispatch intent, permits, receipts, idempotency, reconciliation; controlled effect adapter with an independently observable effect store | lifecycle cases; crash at every boundary (real process kills); ambiguous outcomes never redispatched; storage failures fail closed (including injected I/O errors, S-7) |
| M3-05 | Transition service: CAS on state revision, single-use acceptance records, use-time recomputation, audit events | transition-scenarios and protocol-transition; INV-32..INV-40 transition subset |
| M3-06 | Prerequisite contracts not delivered in M2 (acceptance-gate-integrity section 13, M2 row): evaluator registry entry, runner attestation, EvaluationRecord scope fields | contract tests; owner review of contract shapes before M3-07 |
| M3-07 | Mechanical runner as a sandboxed broker effect: registered commands, digest-identified inputs, no agent credentials, signed attestations (test runner key) | runner attestations verified; tampered input or command refused; sandbox limits tested on disposable resources |
| M3-08 | Differential and property gate for broker and transition (exact exceptions), 127-case closure, chaos and storage-failure suites against the production broker | 127/127; 0 unreconciled differential problems; chaos 0 invariant violations |

## Carried obligations mapped to M3 tasks

| Obligation | Source | Task |
|---|---|---|
| Cross-process reconciliation | D-029 limitation | M3-01 (single-owner enforcement); multi-owner deferred to M10 |
| Ingress and permit keys (risk 8, SR-01, SR-07) | M0 sign-off | M3-02 (test keys, rotation tests); production key management M5 and M9 |
| Composite multi-effect requests (risk 11, RM-01) | M0 sign-off | M3-02 (refused as `COMPOSITE_NOT_SUPPORTED`, swapped sub-effect test) |
| SQLite durability limits (risk 12) | M0 sign-off | M3-04 (crash during commit, kills); power loss M9 |
| Use-time TOCTOU (risk 13, INV-14) | M0 sign-off | M3-02 (decision versus use); OS-level path mediation M5 |
| Trusted clocks (risk 14) | D-018 A5, M1-02 review | M3-02 (clock interface, rollback cannot extend a grant or deadline) |
| Reservation lease or expiry (C-3) | M1 open assumptions | M3-04 |
| Linearizability and kills under load (C-6) | M1 open assumptions | M3-08 |
| Production logging policy (S-5) | M1 open assumptions | M3-04 (sanitized operator diagnostics) |
| Disk full and I/O fault injection (S-7) | M1 open assumptions | M3-04 |
| Control Center M3 items (management service skeleton, audit query model, CC-SEC-02/03/05/08/14, CC-A01/A03/A04, CC-UX-09) | control-center plan | **not in this authorization** (owner 2026-11-12); remain open for a later decision |

## Owner decisions that may be needed

1. Whether an ownership refusal should keep `LEDGER_UNAVAILABLE` or get a distinct code (before M3-02).
2. Contract shapes for M3-06 before the runner (M3-07).
3. Any change to existing broker semantics found while implementing (stop and report).
