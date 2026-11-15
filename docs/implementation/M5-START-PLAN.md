# M5 plan: host enforcement, containment, snapshots, keys and adapters

**Status:** **AUTHORIZED under [D-045](DECISION-LOG.md)** (owner, 2026-11-15; answers M5-D1..M5-D17 in section 10). M5 is split into **M5a trusted foundations** and **M5b host integration**, each with its own evidence and gate review; M5 completes only when both gates pass. All M5a and M5b tasks are **NOT STARTED** unless a row in [TASKS](TASKS.md) says otherwise. Baseline: `8fbf26d` (D-045 recorded). Binding interface design: [M5-INTERFACES](M5-INTERFACES.md) (**DRAFT for lead review**; no consumer is integrated before the lead marks a section binding).

**Predecessor:** **M4 ACCEPTED** as scoped development scope under [D-044](DECISION-LOG.md) (conditions met: authenticated scoped reads, browser suite as a required CI step, required CI on the exact SHA `8b2057a`, run 43659759755, 6/6; E-077). The earlier text of this plan that called M4 "conditionally accepted" and made M5 wait for the D-044 conditions is superseded.

**Scope labels.** Accepted M3 and M4 scope is **development acceptance**: not production enforcement, not real-authenticator validation, not 127/127 exact parity. ADR-0008 stays **APPROVED IN PRINCIPLE**; release and production stay **NO-GO**, and M5 does not change that. The broker guarantees **at most one dispatch per (tenant, idempotency key)** (D-039); a real adapter adds its own effect and recovery contract (D-035 item 2, D-045 M5-D8) and never turns that into exactly-once external effects. No host-enforcement claim is allowed before M5-SR06 is executed (D-014 item 3, R-G3). Work is labelled **planned**, **implemented** or **verified**, never blurred; an externally blocked item is recorded as **blocked, not passed**.

## 1. What exists today (verified in code at `8fbf26d`)

| Area | Implemented today | Not implemented (M5 or later) |
|---|---|---|
| Runner sandbox | `Sandbox` trait and `SandboxGuarantees` in [runner.rs](../../broker/src/runner.rs); `RunnerRequirements::production()` requires every guarantee (filesystem, network, process tree, resources, closed descriptors, bounded execution), so production refuses every job. `LocalProcessSandbox` (`local-process-uncontained/1`, [process.rs](../../broker/src/process.rs)) gives bounded execution and closed descriptors only; accepted solely by the test-only `uncontained_for_disposable_tests` (feature `uncontained-test-runner`). The runner adapter already uses a two-transaction shape (T-start, job outside any transaction, T-finish; a `started` run is never run again) | A contained sandbox; descendant kill (direct child only today); sweep of leftover work directories; operator path for `started` runs ([M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md) "Mechanical runner") |
| Snapshots | `SnapshotStore::check` in [workflow.rs](../../broker/src/workflow.rs) (promotion, administration) and `SnapshotContent` with `immutable()` in runner.rs (runner input); a store not reporting `immutable()` gives `ARTIFACT_INTEGRITY_UNAVAILABLE` | One service-owned, content-addressed, agent-unwritable store behind both traits using the `writ-snapshot-manifest/1` digest (INV-40); only test stores exist |
| Keys | [keys.rs](../../broker/src/keys.rs): `IngressKey`, `PermitKey`, `AcceptanceRecordKey`, `RunnerAttestationKey` (distinct types, minimum 32 bytes, redacted `Debug`); `KeyRing` (ingress by adapter id), `AcceptanceKeyRing` and `RunnerKeyRing` (by key id, `Active`, `Revoked`, `Unknown`, `KeysUnavailable`); keys supplied explicitly only, no ambient source. Permits carry no key id | Provisioning, storage, rotation, revocation, isolation (CO-M5M9-01); in-memory test rings only |
| Effect adapters | `EffectAdapter` in [effects.rs](../../broker/src/effects.rs) (`apply`, `inspect`, `evidence`); `SqliteEffectStore` ([effects.rs](../../persistence/sqlite/src/effects.rs)) records effects as rows, owned and bound to one ledger (D-035 item 2); the broker routes only `runner.evaluate` to the runner and everything else to the one controlled adapter ([execute.rs](../../broker/src/execute.rs)) | Any adapter with an external side effect; routing by action kind to more than two adapters; a recovery contract for effects that cannot share the adapter's transaction |
| Host ingress | Sealed ingress `verify_ingress` in [ingress.rs](../../broker/src/ingress.rs) (HMAC under a registered adapter key, D-010); test line adapter `persistence/sqlite/examples/m3_line_adapter` | Any real HostAdapter; Claude Code integration; coverage matrix; trusted session binding; delegation chain (requests carry none; M4 records `delegation_chain: unavailable`) |
| Model side | Python M0 stubs (M0-02e: two stub ModelAdapters, one simulated host) | Rust ModelAdapter contract, two stub providers and AgentRunner stub (M5-D11) |
| Approval server | `writ-approval-server` (Hyper HTTP/1; Tokio `rt`, `net`, `time` in `writ-approval`); opens the ledger through the ownership path; runs until killed | Graceful shutdown (CO-M4X-05); real-authenticator runs (CO-M4X-02) |
| Ownership | `OwnerLock` (process lifecycle gate, unlock before close) for the ledger and every owned store; canonical-path identity, one hard link, owner epoch fencing ([lib.rs](../../persistence/sqlite/src/lib.rs)) | Path substitution between identity check and open is explicitly out of scope until the trusted directory is service-owned (M5a-04) |
| Live host | SR-06 sandbox prepared and torn down 2026-11-06; 0 of 69 experiments ([readiness record](../audit/m0/live-host/2026-11-06-sandbox-readiness/README.md)); no hypervisor on the workstation | M5-SR06 trials ([SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md) sections 6 and 9) |

Workspace constraint: `unsafe_code = "forbid"` ([Cargo.toml](../../Cargo.toml)) stays (D-045 M5-D4). Containment is external (bubblewrap and a cgroup-v2/systemd job unit); the `rustix` features that M5 needs (`fs`, `process`) and the Tokio `signal` feature go through the existing dependency review (D-041 process).

## 2. M5 scope and the M5a/M5b split (D-045 M5-D1)

**M5a trusted foundations:** privilege separation and the single-owner service composition; local key source; immutable snapshot store; external containment of runner jobs; the first real effect adapter (`fs.write`) with its corrected, independently reviewed recovery contract; graceful shutdown; capability probes and the reference environment; the required containment CI job on an isolated disposable VM runner.

**M5b host integration:** HostAdapter contract and coverage matrix; Claude Code adapter (advisory hooks plus a broker-executed MCP tool path); an independent generic CLI adapter and the portability differential; Rust ModelAdapter contract, two stub providers and an AgentRunner stub; trusted session binding and the delegation chain; SR-06 live trials; real-authenticator checklist runs.

**Concurrency of the phases.** Independent M5b contract work (schemas, coverage matrix format, model stub contracts, test plans and scripts) may proceed alongside M5a once [M5-INTERFACES](M5-INTERFACES.md) is binding. M5b **integration** (anything that seals a request, executes a governed effect or relies on service-held keys or snapshots) depends on the verified M5a foundations it uses.

**Gates.** Section 9: separate M5a and M5b gate reviews, each with its own evidence package and exact-SHA CI.

## 3. Carried obligations relevant to M5

| Obligation | Acceptance criterion (from [TASKS](TASKS.md)) | Owning gate | M5 task |
|---|---|---|---|
| CO-M5-01 contained runner | Contained sandbox verified on a real host; descendants killed with the job; leftover work directories swept; operator path for `started` runs; `RunnerRequirements::production()` accepts jobs only with containment | M5a | M5a-07, M5a-12 |
| CO-M5-02 host enforcement and snapshots | Production snapshot store the broker trusts; missing guarantees fail closed (INV-40); M5-SR06 trials executed before any host-enforcement claim | snapshot part M5a; SR-06 part M5b | M5a-06; M5b-07, M5b-08 |
| M5-SR06 / SR-06 / R-G3 | SR-06-TRIAL-PLAN section 9 met (69 priority, then full acceptance; adversarial only in the designated VM or a separate machine, D-045 M5-D12) | M5b | M5b-07, M5b-08 |
| CO-M5M9-01 key management | Keys never in repository or agent-reachable state; rotation and revocation tested; unknown, revoked or unavailable keys fail closed | M5a (local profile), M9 (operations) | M5a-05 |
| CO-PROP-01 effect-store faults | Concurrent groups inject adapter-store faults; per-key model distinguishes every operation; mutants rejected; recovery final state checked against a serialization | M5a with the first real adapter (single worker); multi-worker M10 (D-040) | M5a-10 |
| CO-M4X-01 delegation chain | Chain authenticated end to end; separation tests per member | M5b (D-045 M5-D14) | M5b-06 |
| CO-M4X-02 real authenticator and host boundary | Recorded real-device runs; loopback and host-binding assumptions verified on a real host | M5b (D-045 M5-D13) | M5b-09, M5b-10 |
| CO-M4X-03 ledger growth and sustained local DoS | Bounded growth under hostile load; retention policy enforced | M9 | none (M5 risk only; snapshot quotas in M5a-06) |
| CO-M4X-04 legacy-label collision recovery | Recovery path tested; no history rewritten | M12 (D-045 M5-D16) | none; stays an explicit fail-closed migration obligation |
| CO-M4X-05 graceful shutdown | Clean shutdown releases the ownership lock and finishes in-flight requests | M5a (D-045 M5-D16) | M5a-11 |
| CO-DEP-01 `rsa` and `crypto-glue` dispositions | Patched resolution or explicit release disposition; `check_deps.py --release` passes | release gate | rechecked at M5a-00; every M5 dependency repeats the D-041 review (M5a-02, M5b-02) |
| CO-M6-01, CO-M6-02, CO-M7-01, CO-HR-01 | unchanged | M6, M7, every closure edit | not M5 |

## 4. Trust and deployment model (decided: D-045 M5-D3, M5-D7)

The decisive local risk (M4-START-PLAN section 2, "Same-OS-user risk"): the agent runs as the human's OS user, so anything that user can read or write, the agent can too. D-045 M5-D3 settles it: a **dedicated service OS user** owns trusted state and services, the agent receives no trusted file access and no secret keys, and **one trusted process owns the ledger** (single-owner SQLite profile kept; never two independent ledger owners). Design: [M5-INTERFACES](M5-INTERFACES.md) J-1 and J-2.

| Component | Runs as | Holds | Agent can |
|---|---|---|---|
| `writ-serviced`: broker, ledger, effect and fs journals, snapshot store, approval listener, runner supervisor, sealing part of host adapters (one owner process) | service user | ledger and every store; permit, record, runner and ingress keys | connect to the host socket (peer credentials checked) and the loopback approval page; nothing else |
| Hook client and MCP tool shim (M5b) | agent's user | nothing secret | everything it can today; hook decisions are advisory; governed effects only through the broker-executed path |
| Runner jobs | sandboxed (bubblewrap) inside a per-job cgroup | a materialized snapshot copy only | nothing outside the job (no trusted socket, state, key, host process interface or cgroup control) |
| Operator procedures (install, key provisioning, readiness) | root or the service user on the disposable reference host only | key material during provisioning | nothing |

Host identity (M5-D7): service-side sealing with configured tenant and project bindings and socket peer credentials, which prove the **OS principal only**; host-asserted fields (agent, session, parent) are labelled and never authority. Agent-specific grant consumption and delegation-dependent authorization are refused or kept advisory until a trusted session binding exists (M5b-06). The hook never holds the ingress key. Design: M5-INTERFACES J-6.

No automatic user creation or permission change happens on the owner's workstation: install and teardown scripts are prepared for the disposable reference environment only (M5-D3, M5-D12).

## 5. First real effect adapter: corrected recovery contract requirement (D-045 M5-D8)

**Adapter (decided):** `fs.write` from the immutable snapshot store into a broker-owned target root, modes `create_only` and `replace_if` (expected prior content). Bytes never come from an agent path.

**Withdrawn inference.** The earlier version of this section proposed reconciling an interrupted write from content hashes ("target equal to the new digest is evidence of success; prior digest with the temporary file absent is evidence of failure"). **D-045 M5-D8 rejects this as insufficient proof**: matching destination bytes do not prove that this operation completed (another operation, a repeated identical write or an external writer can produce the same bytes), and prior bytes plus an absent temporary file do not prove that it never completed (the file may have been renamed and then replaced, or removed by someone else). The inference is not used anywhere in M5.

**Contract requirement (binding from D-045; design in [M5-INTERFACES](M5-INTERFACES.md) J-7, which must be independently reviewed before the adapter is integrated):**

1. **Explicit non-transactional adapter contract.** The filesystem effect and the journal commit cannot share one transaction. Durable intent and permit consumption are committed **before** the filesystem effect; durable outcome evidence is committed **after** it. The `EffectAdapter::apply` documentation ("consume and apply atomically") is amended for this adapter class, as the runner already does with T-start and T-finish.
2. **Durable operation identity.** Every operation has an identity recorded at intent (derived from the permit id) and operation-specific evidence that cannot be produced by another operation with the same bytes.
3. **Per-target ordering and blocking.** At most one unresolved operation per target; a target with an unresolved outcome refuses every conflicting later operation until it is resolved.
4. **Exclusive-writer assumptions.** Stated explicitly, enforced by OS ownership and the journal's ownership lock where possible, and detected (target drift) where enforcement is impossible; a detected violation keeps the outcome unknown and the target blocked.
5. **Trusted directory descriptors.** Operations are anchored to a root directory descriptor opened once and verified; mount crossings are forbidden and enforced explicitly (not only by beneath and no-symlink resolution).
6. **Outcome evidence versus desired state.** Evidence that a particular operation completed is recorded separately from verification that the target currently holds the desired content.
7. **Reconciliation decision table.** Every combination of journal state and observed filesystem state maps to `applied`, `not applied` or `unknown`; ambiguous evidence stays **unknown**; nothing is ever redispatched (D-035 item 3).
8. **Every crash boundary enumerated and tested**, with actual SIGKILL recovery.

**Tests (first):** every journal and filesystem boundary (SIGKILL at each enumerated point, then recovery); repeated identical content (same bytes written twice by different operations; replacement with the same bytes); later writes to the same target (blocked while unresolved, allowed after resolution); path races (symlink swap, directory rename, bind mount or mount over a parent between decision and use); permission and full-disk failures; ambiguous commits (journal commit with unknown outcome at intent, staged and outcome); replayed permit; external modification detected as drift; CO-PROP-01 groups with injected journal faults and a per-key model (M5a-10).

## 6. Task breakdown (tests first)

Every task is **NOT STARTED** at `8fbf26d`. TASKS rows carry acceptance criteria and evidence; this table adds dependencies, the tests that are written first and the done condition. "Blocked externally" items are in section 12.

### 6.1 M5a trusted foundations

| ID | Task | Deps | Tests written first | Done when |
|---|---|---|---|---|
| M5a-00 | Start gate: D-045 recorded (`8fbf26d`); baseline SHA; TASKS rows; `rsa` patched-resolution recheck (CO-DEP-01) with the result recorded | D-045 | none | Rows present; recheck recorded in TEST-EVIDENCE; release eligibility still blocked unless resolved |
| M5a-01 | Shared interfaces: lead review of [M5-INTERFACES](M5-INTERFACES.md); independent review of J-1 (privilege model), J-2 (RPC), J-6 (host identity); sections marked binding | M5a-00 | none | Lead marks the sections binding; review findings dispositioned |
| M5a-02 | Dependency review (D-041 process): `rustix` features `fs` and `process` (and `net` only if J-2 needs it), Tokio `signal`; exact allow-list entries per crate; new crates' closures registered in `check_deps.py` | M5a-01 | dependency gate fails on an unreviewed feature or crate | Reviewed closure recorded; gate green; nothing adopted for MCP (M5b-02) |
| M5a-03 | Capability probes and reference environment: `writ-hostsys` probe set, `writ-probe` binary, `writ-readiness/1` report; VM setup and teardown scripts prepared (never run on the workstation) | M5a-01, M5a-02 | each probe has a negative control that makes it fail (namespace creation denied, cgroup v1 or no delegation, missing `cgroup.kill`, unsupported filesystem type, missing `openat2` flag); readiness unaffected by a forged version string | Probe set and report implemented; reference-image run **blocked externally** until the VM exists (recorded, not passed) |
| M5a-04 | Privilege separation: service identity, directory layout and modes, `writ-serviced` as the single ledger owner composing broker, stores, approval listener and runner supervisor; local RPC over Unix sockets with peer credentials (J-2); install and teardown scripts for the disposable reference VM | M5a-01, M5a-02 | agent-user read and write of every trusted path refused at the OS layer (AG-ADV-18); canary key unreadable (INV-31); peer of an unlisted uid refused; oversized or malformed frame refused (INV-18, INV-41); second owner process refused (`OWNED_ELSEWHERE`); no secret in command line or environment | Implemented with tests in user space; agent-deny matrix verified on the reference VM (**blocked externally** until it exists) |
| M5a-05 | Local key source (J-3): service-owned key files, purpose separation, ids, validity windows, rotation with overlap, revocation, no-follow loading and permission checks; permit key id (contract amendment, lead-owned) | M5a-04 | forged seal and permit after rotation; revoked, unknown, expired, not-yet-valid, missing, symlinked, hard-linked, world- or group-readable key or directory refused; one key file used under two purposes refused; no key in logs, `Debug`, command line, environment or repository (canary scan) | CO-M5M9-01 local part met; access denial from the agent identity verified on the reference VM (**blocked externally** until it exists) |
| M5a-06 | Immutable snapshot store (J-4): `writ-snapshots-1` in `persistence/sqlite`, owned, bound to one ledger, behind `SnapshotStore` and `SnapshotContent`; byte-stream ingest (M5-D17) over the host socket | M5a-04 (parallel with M5a-05) | AG-NEG-11, AG-NEG-12, AG-ADV-17..19, AG-CON-04; crash during ingest; size, count and path bounds; duplicate, unsafe and prefix-conflicting paths; claimed digest mismatch; blob corruption detected at read; `immutable()` false without a verified deployment; no update or delete through the API; store bound to another ledger refused | `immutable()` true only on the separated deployment; the M3 limitation (two snapshot paths) removed |
| M5a-07 | Containment (J-5): bubblewrap profile, delegated cgroup-v2 subtree under the service's systemd unit, per-job cgroup entry before exec, termination verification, pinned runtime profiles, work-directory sweep at start, operator path for `started` runs | M5a-03, M5a-04, M5a-05, M5a-06 | write outside the work directory; read of service state and keys; connect to trusted sockets and the network; `setsid` and double-fork survivors; fork bomb; memory and CPU limits; descriptor leak; cgroup escape attempt (write to `cgroup.procs` of a parent); supervisor killed mid-job; broker killed mid-job; missing bubblewrap, missing controller or unpinned runtime refuses (no uncontained fallback) | `RunnerRequirements::production()` accepts jobs on the reference VM with every probe passing; CO-M5-01 met except the required CI part (M5a-12) |
| M5a-08 | `fs.write` recovery contract (J-7): design completed, **independently reviewed**, findings dispositioned; the `EffectAdapter` contract amendment for non-transactional adapters recorded | M5a-01 | none (design); the review checks the decision table against every enumerated boundary | Review recorded; J-7 marked binding by the lead |
| M5a-09 | `fs.write` adapter: journal `writ-fseffects-1` (owned, bound to one ledger), adapter crate, broker routing of effect adapters by action kind (lead-owned broker change), new refusal codes with external mapping (INV-44) | M5a-08 review passed, M5a-04, M5a-05, M5a-06 | section 5 tests; SIGKILL at every boundary of J-7 with real processes (`kill_tests` style) | Contract implemented; every row of the J-7 decision table covered by a test |
| M5a-10 | CO-PROP-01 for the `fs.write` adapter (single worker): concurrent groups with injected journal faults, per-key model distinguishing every operation, recovery final state checked against a serialization | M5a-09 | property mutants for each new property rejected | CO-PROP-01 met for one worker |
| M5a-11 | Graceful shutdown (J-8) of `writ-serviced` and `writ-approval-server` (CO-M4X-05) | M5a-02, M5a-04; job termination step after M5a-07 | SIGTERM during a ceremony, an ingest, an `fs.write` and a running job; lock released; in-flight request finished or refused cleanly; second signal and hard deadline; uncertain outcomes preserved | CO-M4X-05 met |
| M5a-12 | Required containment CI on an isolated, disposable VM runner (M5-D15): fails on missing prerequisites, no silent skip, no fork code or secrets on a trusted runner | M5a-07 | CI job fails on a runner without the capabilities | Required job green on the exact SHA; **blocked externally** until the runner exists (local VM evidence recorded but does not complete M5a) |
| M5a-13 | Independent security review of M5a-04..M5a-11 and remediation | M5a-04..M5a-11 | regression test per finding | Findings fixed or dispositioned by the owner |
| M5a-14 | M5a gate-review package | all M5a | none | Owner review (section 9) |

### 6.2 M5b host integration

| ID | Task | Deps | Tests written first | Done when |
|---|---|---|---|---|
| M5b-00 | Start: contract work may begin when M5a-01 is binding; integration tasks wait for the M5a items they use (listed per row) | M5a-01 | none | Recorded |
| M5b-01 | HostAdapter contract (proposed schemas in `docs/contracts/proposed/`): host event envelope, coverage matrix (event, tool variant, capability, interceptor, `can_prevent`, `can_observe`, test id, host version), decision mapping, advisory and governed labels, host-asserted identity labels (J-6) | M5b-00 | unknown tool DENY (governed) and labelled ungoverned (advisory) (INV-13, INV-39); malformed and oversized envelopes (INV-18, INV-41); no promotion shortcut; a host-asserted label never selects a grant | Schemas and fixtures pass the docs and contract checks; lead review |
| M5b-02 | Dependency review of an MCP implementation for the agent-side tool shim only (maintained implementation, pinned protocol version; no invented partial JSON-RPC dialect) | M5b-01, M5a-02 | dependency gate | Reviewed or rejected with the reason; nothing adopted before review |
| M5b-03 | Claude Code adapter: service-side normalizer and sealing (trusted), hook client (agent user, no keys, advisory), MCP tool shim (agent user) forwarding to the host socket; governed effects only through the broker's protected execution path | M5b-01, M5b-02, M5a-04, M5a-05, M5a-09 | forged session ids; interleaved sessions (INV-19); crash, timeout and unparseable hook output; subagent events; unknown tools; hook client with the ingress key absent | Coverage matrix published from observed behaviour on a pinned host version only, including bypasses and unmediated tools |
| M5b-04 | Generic CLI adapter with its own independent normalization; portability differential with the Claude Code adapter | M5b-01, M5a-04 | identical ActionRequest and Decision fixtures through both; Unicode paths, symlinks, huge payloads, host failures, nested subagents, permission-prompt semantics | Portability acceptance met; independence shown (no shared normalizer code) |
| M5b-05 | Rust ModelAdapter contract, two stub providers with dissimilar features, AgentRunner stub; coding, research and review fixtures; closure scan proving no model SDK in kernel, broker or service closures | M5b-01 | model outage during policy evaluation (INV-21); host task with no ModelAdapter (INV-22); fallback to a forbidden capability (INV-26); stub tool proposals never authorize | Master-plan M5 gate rows met |
| M5b-06 | Trusted session binding and delegation chain (M5-D14): chain from trusted session creation and lifecycle records with authenticated member bindings; versioned request, proof and fingerprint changes | M5b-03, M5a-05 | subagent approving its parent's request; truncation, reordering, substitution and missing links; forged member; parent grants never inherited (D-011); without a chain, `unavailable` kept and separation-dependent operations refused | CO-M4X-01 met for requests with an authenticated chain |
| M5b-07 | SR-06 trial preparation: VM setup, teardown and trial scripts, pinned Claude Code version, preflight repeated, evidence template | M5b-03, M5a-03 | scripts dry-run on a disposable container for syntax only (no adversarial trial) | Scripts and template reviewed |
| M5b-08 | M5-SR06 live trials in the designated VM: 69 priority experiments, then SR-06-TRIAL-PLAN section 9; UNMEDIATED-TOOLS updated | M5b-07 | the trial matrix | Section 9 met by a named operator; **blocked externally** (VM, revocable model credential) until provisioned |
| M5b-09 | Real-authenticator checklist and evidence template (M5-D13): enrol, approve, deny, signoff, revoke, read session, wrong origin, removed key, no-UV device refused; host-boundary checks (loopback binding, origin) | M5a-04 | checklist items as executable steps where possible | Checklist and template reviewed |
| M5b-10 | Owner-performed real-authenticator runs (roaming FIDO2 key and platform passkey, disposable state; device and browser versions recorded) | M5b-09 | the checklist | CO-M4X-02 met; **blocked externally** until the owner has the hardware (a named gate gap, never substituted by virtual authentication) |
| M5b-11 | Independent security review of the adapter boundary, session binding and delegation chain, and remediation | M5b-03..M5b-06 | regression test per finding | Findings fixed or dispositioned |
| M5b-12 | M5b gate-review package | all M5b | none | Owner review (section 9) |

### 6.3 Ordering and parallelism (D-045 "Execution and cleanup")

1. **M5a-00, M5a-01, M5a-02** first, by the lead (interfaces, dependency graphs).
2. **Privilege separation first: M5a-04** (with the probe framework M5a-03 alongside). Everything trusted depends on it.
3. **Keys and snapshots then independently, in parallel: M5a-05 and M5a-06** (disjoint ownership: key source crate versus `persistence/sqlite` snapshots module).
4. **Containment: M5a-07** (needs probes, separation, keys for the runner attestation key and snapshots for job input).
5. **`fs.write` only after its recovery contract is independently reviewed:** M5a-08 (design and review, may run in parallel with steps 2 to 4 because it is design only), then M5a-09 and M5a-10.
6. **Graceful shutdown: M5a-11** (signal handling after M5a-02; the job-termination step after M5a-07).
7. **M5a-12, M5a-13, M5a-14.**
8. M5b contract work (M5b-01, M5b-05, M5b-09) may run alongside steps 2 to 7; M5b-03, M5b-04, M5b-06 integrate after their M5a dependencies are verified.

Bounded parallel workers with disjoint ownership as in D-040..D-045; the lead owns shared contracts, migrations, dependency graphs, integration and CI; independent security review of the foundations (M5a-13), the recovery contract (M5a-08) and the adapter boundary (M5b-11).

## 7. What existing decisions already settle (not asked again)

| Topic | Settled by |
|---|---|
| Rust for kernel and broker; one production core | ADR-0002, D-030 |
| No host-enforcement claim before M5-SR06; adversarial trials only in the designated VM or a separate machine | D-014 item 3, R-G3, D-045 M5-D12 |
| Every production adapter has explicit concurrency, replay, authorization and recovery contracts; at-most-once dispatch is not exactly-once | D-035 item 2, D-039, D-045 M5-D8 |
| No client-asserted reconciliation; uncertain effects never redispatched; adapter storage failure precedence | D-035 items 3 and 4, D-036 |
| Runner fails closed without full containment; uncontained profile test-only | M3-07, D-035 item 6, D-045 M5-D4 |
| Content-addressed immutable snapshot is the default; any equivalent needs its own ADR | [acceptance-gate-integrity](../architecture/acceptance-gate-integrity.md) section 6a, INV-40, D-045 M5-D5 |
| Host-side transitions advisory; no host promotion shortcut | ADR-0008 section 13 item 6 |
| Pack and model output never grant authority; no model SDK in kernel or broker | ADR-0006, INV-21..INV-23, D-045 M5-D11 |
| Subagents never inherit parent grants | D-011, D-045 M5-D14 |
| Dependency adoption process | D-041 DR-1..DR-4, D-042 DR-5 |
| Approval channel loopback, WebAuthn local profile, enterprise OIDC is future work | D-040 O-1, O-12 |
| CO-PROP-01 at M5 with the first real adapter; multi-worker at M10 | D-040 |
| Ledger growth and sustained loopback DoS at M9 | D-044 item 5, CO-M4X-03 |
| Control Center CC-M5 separately authorized | ADR-0007, D-015 |

## 8. Risks

1. **Scope.** Addressed by the split (M5-D1); M5b integration still depends on several M5a items at once.
2. **Advisory profile mislabelled.** Same-user deployments and non-reference hosts keep failing closed for governed jobs and writes; any advisory functionality must be labelled accurately (M5-D2, INV-39).
3. **Host kernel features.** Unprivileged user namespaces, cgroup-v2 delegation and `openat2` flags vary by distribution and configuration; readiness comes only from probes (M5-D2). Some distributions restrict unprivileged user namespaces by policy (for example AppArmor restrictions on recent Ubuntu releases), which is one reason the reference image is Debian (M5-INTERFACES J-9).
4. **External trusted binaries.** bubblewrap and the per-job cgroup entry helper are in the trusted computing base; they are pinned and recorded with the reference image.
5. **Host drift.** Claude Code's hook contract and version change; SR-06 plan revision 3 was prepared against 2.1.318; preflight is repeated on the pinned version (M5b-07).
6. **Trial and CI environments.** No hypervisor on the workstation; VM, isolated CI runner, model credential and authenticator hardware are external (section 12).
7. **Non-transactional effects.** A filesystem effect leaves real uncertainty windows; the recovery contract makes them explicit and keeps ambiguity as unknown, which can block a target until an operator acts.
8. **Identity.** Socket peer credentials prove the OS principal only (M5-D7); agent identity and delegation stay host-asserted until M5b-06.
9. **Dependency growth.** `rustix` features, Tokio `signal`, an MCP implementation (agent side only); `rsa` (RUSTSEC-2023-0071) still blocks release (CO-DEP-01).
10. **Denial of service.** Sandboxed jobs hold the policy gate and workflow lock for their wall time (M3 limitation); blocked targets; snapshot quotas; loopback DoS (CO-M4X-03) remains until M9.

## 9. Gates (D-045: M5a and M5b each need their scoped gate review)

**M5a gate:** CO-M5-01 (including the required containment CI job on the isolated VM runner, M5-D15); CO-M5-02 snapshot part; CO-M5M9-01 local part; CO-PROP-01 for one worker; CO-M4X-05; the `fs.write` recovery contract independently reviewed and every decision-table row and crash boundary tested; agent-deny matrix verified on the reference VM; independent security review dispositioned; closure, property, dependency and browser gates still required and green; exact-SHA CI. Not claimed: host enforcement (needs M5-SR06), production enforcement, enterprise key management, multi-worker effects, release.

**M5b gate:** master-plan M5 gate (external host needs no model API; two stub providers and coding, research and review fixtures without policy divergence); AG-M5 (mediated host operations proven by live trials; host-side transitions advisory); portability acceptance (two independent adapters pass identical fixtures); CO-M5-02 SR-06 part (M5-SR06 section 9); CO-M4X-01; CO-M4X-02 with real devices; coverage matrix from observed behaviour only; independent security review dispositioned; exact-SHA CI.

M5 completes only when both gates pass. Release and production stay NO-GO.

## 10. Owner decisions (answered under D-045)

| ID | Decision | Answer (D-045) | Consequence in this plan |
|---|---|---|---|
| M5-D1 | Milestone shape | M5a trusted foundations, M5b host and model integration; separate evidence and gate reviews; M5b contract work alongside M5a, integration after verified foundations | Sections 2, 6, 9 |
| M5-D2 | Host OS and kernel scope | Linux governed profile only; one reference image with exact versions recorded; readiness from capability probes and tests, never version comparison; other hosts refuse governed jobs and writes; Landlock optional defence in depth | M5a-03; M5-INTERFACES J-9 |
| M5-D3 | Privilege separation | Dedicated service OS user; agent gets no trusted file access and no secret keys; single-owner SQLite profile, services composed around the owner or reached by authenticated RPC; scripts for the disposable reference environment only | M5a-04; J-1, J-2 |
| M5-D4 | Sandbox technology | bubblewrap plus a cgroup-v2/systemd job unit; `unsafe_code` stays forbidden; `rustix` features through review; complete cgroup and process-tree termination verified (setsid, double fork); restrictions established before untrusted execution; pinned profiles and runtimes; no uncontained fallback; supervisor and broker termination tested | M5a-07; J-5 |
| M5-D5 | Snapshot store | One service-owned SQLite content-addressed store bound to one ledger behind both interfaces; transactional bounded ingest, canonical manifest validation; bytes verified at ingest and consumption; immutability through the API; retention never invalidates an active reference; no caller-controlled `immutable=true` | M5a-06; J-4 |
| M5-D6 | Key storage | Service-owned key files behind a key-source interface; protected directories, restrictive permissions, no-follow loading, purpose separation, ids, validity, rotation, revocation; operator provisioning; never in arguments, logs, repository or agent-readable state; enterprise KMS/HSM later | M5a-05; J-3 |
| M5-D7 | Host identity | Service-side sealing, configured bindings, peer credentials as OS-principal proof only; host-asserted fields labelled, never authority; no agent-specific or delegation-dependent authorization on caller-selectable labels until trusted session binding; hook never holds the ingress key | M5b-01, M5b-03, M5b-06; J-6 |
| M5-D8 | First real effect adapter | `fs.write` from snapshots into a broker-owned root (`create_only`, expected-prior replacement); explicit non-transactional contract; content-hash inference rejected; operation identity, per-target ordering and blocking, exclusive-writer assumptions, reconciliation evidence independently reviewed before integration; trusted directory descriptors; explicit mount-crossing enforcement | Section 5; M5a-08..M5a-10; J-7 |
| M5-D9 | Claude Code integration | Advisory hooks plus a broker-executed MCP tool path through a reviewed, maintained MCP implementation with a pinned protocol version; coverage from observed behaviour on a pinned host version | M5b-02, M5b-03 |
| M5-D10 | Second adapter | Separate generic CLI adapter with independent normalization; wrapping the Claude adapter is not independence | M5b-04 |
| M5-D11 | Language and packaging | Separate Rust crates for trusted adapters, contracts and two stub providers; model SDKs out of kernel and broker closures; frozen M0/M1 evidence preserved | M5b-05; J-0 |
| M5-D12 | Trial environment | Owner-designated disposable Linux VM with rollback; scripts prepared now; no automatic hypervisor installation, paid provisioning or personal credential reuse; dedicated revocable model credential out of evidence and chat; trials recorded blocked until available | M5b-07, M5b-08; section 12 |
| M5-D13 | Real authenticator | Owner-performed checklist with a roaming FIDO2 key and a platform passkey on disposable state; missing hardware a named gate gap; never virtual substitution | M5b-09, M5b-10 |
| M5-D14 | Delegation chain | M5b; chain from trusted session creation and lifecycle records with authenticated member bindings; versioned contract and fingerprint changes; `unavailable` retained otherwise | M5b-06 |
| M5-D15 | Containment CI | Isolated disposable VM runner, failing on missing prerequisites; no hostile workloads on a persistent workstation runner; local VM evidence does not complete M5a | M5a-12 |
| M5-D16 | Small carry-overs | Tokio `signal` through feature review; bounded graceful shutdown; CO-M4X-04 at M12, fail closed | M5a-02, M5a-11; J-8 |
| M5-D17 | Snapshot ingest | Agent-submitted bytes over the authenticated socket; the store computes the digest; no agent paths reopened; a claimed digest is checked, never authority | M5a-06; J-4 |

New trust-boundary questions found while designing the interfaces are listed in [M5-INTERFACES](M5-INTERFACES.md) J-10; they are raised only when dependent work reaches them (D-045 "Execution and cleanup").

## 11. Recovery checkpoints

Each checkpoint is recorded in [SESSION-HANDOFF](SESSION-HANDOFF.md) and [WORK-STATE](WORK-STATE.md) with the exact commit, and its evidence in [TEST-EVIDENCE](TEST-EVIDENCE.md). A resumed session verifies the listed state before continuing.

| Checkpoint | Reached when | Verify on resume | Next |
|---|---|---|---|
| C5-0 | D-045 recorded, this plan and TASKS rows updated, M5-INTERFACES drafted | `git log` contains `8fbf26d` and the plan commit; `python3 scripts/check-v2-docs.py` passes | M5a-00 (RSA recheck), M5a-01 lead review |
| C5-1 | M5-INTERFACES sections binding; dependency review done | Interfaces status line says binding per section; `python3 kernel/tools/check_deps.py --osv` green with new entries | M5a-03, M5a-04 |
| C5-2 | Privilege separation implemented (user-space tests) | `cargo test --release --locked` green; `writ-serviced` refuses a second owner | M5a-05, M5a-06 in parallel; M5a-08 design review |
| C5-3 | Keys and snapshots implemented | key and snapshot suites green; `immutable()` false without verified deployment | M5a-07 |
| C5-4 | Recovery contract independently reviewed | review record in TEST-EVIDENCE; J-7 marked binding | M5a-09, M5a-10 |
| C5-5 | Containment, `fs.write`, shutdown implemented | local suites green; VM runs recorded or marked blocked | M5a-12, M5a-13 |
| C5-6 | M5a gate package presented | exact-SHA CI including the containment job | Owner M5a review |
| C5-7 | M5b contracts and adapters implemented | contract and portability suites green | M5b-07..M5b-11 |
| C5-8 | M5b gate package presented | exact-SHA CI; SR-06 and authenticator evidence, or recorded blocked gaps | Owner M5b review |

Rules at every checkpoint: the repository is the source of truth; a test is reported passing only if it ran in that session; blocked externally is never written as passed.

## 12. External blockers (recorded as blocked, not passed)

| Blocker | Needed by | Who resolves | Until then |
|---|---|---|---|
| Disposable Linux VM with rollback, namespace and cgroup capabilities (M5-D12) | M5a-03 reference run, M5a-04 and M5a-05 agent-deny verification, M5a-07 containment verification, M5b-08 trials | owner (local VM preferred; cloud VM needs separate approval of provisioning and cost) | scripts prepared; user-space tests run; VM-dependent results recorded **BLOCKED (external)** |
| Isolated disposable CI runner for the containment gate (M5-D15) | M5a-12, hence the M5a gate | owner | local VM evidence recorded accurately; does not satisfy the required CI gate or complete M5a |
| Dedicated revocable model credential through the approved secure procedure (M5-D12) | M5b-08 | owner | trials blocked; credential never in evidence, chat or repository |
| Roaming FIDO2 key and a platform passkey (M5-D13) | M5b-10 | owner | named gate gap; no hardware purchase or real-user enrollment automatically; virtual authentication never substitutes |
| Hypervisor on the workstation | only if the owner chooses a local VM there | owner | no automatic installation or privileged workstation change |

## 13. References

[DECISION-LOG](DECISION-LOG.md) (D-011, D-014, D-030, D-035, D-036, D-039..D-045), [M5-INTERFACES](M5-INTERFACES.md), [MASTER-IMPLEMENTATION-PLAN](MASTER-IMPLEMENTATION-PLAN.md) (sections 10, 14, 17, 18), [TASKS](TASKS.md) (M5a-*, M5b-*, CO-M5-01, CO-M5-02, CO-M5M9-01, CO-PROP-01, CO-M4X-01..05, CO-DEP-01, M5-SR06, CC-M5), [M4-GATE-REVIEW](M4-GATE-REVIEW.md), [M4-INTERFACES](M4-INTERFACES.md), [M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md), [invariants](../security/invariants.md) (INV-01, INV-08, INV-13..INV-19, INV-21..INV-23, INV-31, INV-39..INV-41, INV-44, INV-45), [threat model](../security/threat-model.md) (enforcement profiles, T-03..T-07, T-13, T-17), [ENTERPRISE-READINESS](../security/ENTERPRISE-READINESS.md) (ENT-R05), [host adapters](../architecture/host-adapters.md), [UNMEDIATED-TOOLS](../audit/m0/UNMEDIATED-TOOLS.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md), [ADR-0006](../decisions/ADR-0006-model-and-domain-independence.md), [ADR-0008](../decisions/ADR-0008-acceptance-gate-integrity.md).

Release and production remain NO-GO; ADR-0008 APPROVED IN PRINCIPLE.
