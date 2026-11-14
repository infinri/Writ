# M5 plan: host enforcement, containment, snapshots, keys and adapters

**Status:** PLANNED, NOT AUTHORIZED. Nothing in this document is implemented unless a row says "implemented today" and cites the code. Prepared under [D-044](DECISION-LOG.md) item 5 ("Prepare M5-START-PLAN around the carried host, adapter, containment and snapshot obligations, identifying any genuinely new decisions before M5 implementation"). Baseline: `b44741d` (D-044 carry-over tasks recorded). M4 is **conditionally accepted** under D-044 item 1; M5 starts only after (1) the D-044 conditions hold (authenticated scoped reads, the complete browser suite as a required CI step, required CI green on the exact resulting SHA) and (2) the owner answers the decisions in section 10.

**Scope labels.** Accepted M3 and M4 scope is **development acceptance**: not production enforcement, not real-authenticator validation, not 127/127 exact parity. ADR-0008 stays **APPROVED IN PRINCIPLE**; release and production stay **NO-GO**, and M5 does not change that. The broker guarantees **at most one dispatch per (tenant, idempotency key)** (D-039); a real adapter adds its own effect and recovery contract (D-035 item 2) and never turns that into exactly-once external effects. No host-enforcement claim is allowed before M5-SR06 is executed (D-014 item 3, R-G3).

**Gate (MASTER-IMPLEMENTATION-PLAN section 10, M5):** external host needs no model API; two stub providers and coding, research and review fixtures work without policy divergence. Plus section 18 (AG-M5: mediated host operations proven; host-side transitions advisory), section 17 (CC-M5, separately authorized), [host adapters](../architecture/host-adapters.md) portability acceptance (two independent adapters pass identical ActionRequest and Decision fixtures), and the carried obligations in section 3.

## 1. What exists today (verified in code at `b44741d`)

| Area | Implemented today | Not implemented (M5 or later) |
|---|---|---|
| Runner sandbox | `Sandbox` trait and `SandboxGuarantees` in [runner.rs](../../broker/src/runner.rs); `RunnerRequirements::production()` requires every guarantee (filesystem, network, process tree, resources, closed descriptors, bounded execution), so production refuses every job. `LocalProcessSandbox` (`local-process-uncontained/1`) gives bounded execution and closed descriptors only; accepted solely by the test-only `uncontained_for_disposable_tests` (feature `uncontained-test-runner`) | A contained sandbox; descendant kill (direct child only today); sweep of leftover work directories; operator path for `started` runs ([M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md) "Mechanical runner") |
| Snapshots | `SnapshotStore::check` in [workflow.rs](../../broker/src/workflow.rs) (promotion, administration) and `SnapshotContent` with `immutable()` in runner.rs (runner input); a store not reporting `immutable()` gives `ARTIFACT_INTEGRITY_UNAVAILABLE` | One service-controlled, content-addressed, agent-unwritable store behind both traits using the `writ-snapshot-manifest/1` digest (INV-40); only test stores exist |
| Keys | [keys.rs](../../broker/src/keys.rs): `KeyRing` (ingress, permit), `AcceptanceKeyRing`, `RunnerKeyRing`; keys supplied explicitly only, no ambient source, redacted `Debug`, minimum 32 bytes | Provisioning, storage, rotation, revocation, isolation (CO-M5M9-01); in-memory test rings only |
| Effect adapters | `EffectAdapter` in [effects.rs](../../broker/src/effects.rs): one adapter transaction (verify, replay check, consume permit and apply atomically), `inspect`, `evidence` for reconciliation; `SqliteEffectStore` (`writ-effects-2`) records effects as rows, owned and bound to one ledger (D-035 item 2) | Any adapter with an external side effect; a contract for effects that cannot share the adapter's transaction |
| Host ingress | Sealed ingress `verify_ingress` in [ingress.rs](../../broker/src/ingress.rs) (HMAC under a registered adapter key, D-010); test line adapter `persistence/sqlite/examples/m3_line_adapter` | Any real HostAdapter; Claude Code integration; coverage matrix; delegation chain (requests carry none; M4 records `delegation_chain: unavailable`) |
| Model side | Python M0 stubs (M0-02e: two stub ModelAdapters, one simulated host) | Rust or other production-path ModelAdapter contract, stub providers and AgentRunner stub |
| Approval server | `writ-approval-server` (Hyper HTTP/1, Tokio `rt`, `net`, `time`) | Graceful shutdown (CO-M4X-05); real-authenticator runs (CO-M4X-02) |
| Live host | SR-06 sandbox prepared and torn down 2026-11-06; 0 of 69 experiments ([readiness record](../audit/m0/live-host/2026-11-06-sandbox-readiness/README.md)); no hypervisor on the workstation | M5-SR06 trials ([SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md) sections 6 and 9) |

Workspace constraint: `unsafe_code = "forbid"` ([Cargo.toml](../../Cargo.toml)). Namespace setup in a child (`pre_exec`) needs `unsafe`, so in-process containment needs either an external launcher or a scoped exception (decision M5-D4). `std::os::unix::process::CommandExt::process_group` is safe, and `rustix` (already a broker dependency, feature `time` only) offers process-group signalling behind its `process` feature.

## 2. M5 scope (master plan)

1. **Host adapters:** Claude Code adapter first; generic HostAdapter contract; machine-readable coverage matrix (event, tool variants, capability, interceptor, `can_prevent`, `can_observe`, test id, host version); unknown actions DENY in the governed profile and are labelled ungoverned in the advisory profile (INV-13, INV-16, INV-39).
2. **Model side:** generic ModelAdapter contract, two stub providers, optional AgentRunner stub; kernel and broker import no model SDK (INV-21); external host mode needs no ModelAdapter (INV-22, P-14).
3. **Portability:** a second independent adapter passes the same ActionRequest and Decision fixtures (Unicode paths, symlinks, huge payloads, host failures, nested subagents, permission-prompt semantics).
4. **Acceptance gate (AG-M5):** mediated host operations proven by live trials; host-side transitions advisory; no host adapter offers a promotion shortcut that writes workflow state ([acceptance-gate-integrity](../architecture/acceptance-gate-integrity.md) section 13 item 6).
5. **Carried obligations** assigned to M5 (section 3), which are the trusted foundations the adapters need: contained runner, immutable snapshots, local key management, first real effect adapter.

## 3. Carried obligations relevant to M5

| Obligation | Acceptance criterion (from [TASKS](TASKS.md)) | Owning gate | M5 task |
|---|---|---|---|
| CO-M5-01 contained runner | Contained sandbox verified on a real host; descendants killed with the job; leftover work directories swept; operator path for `started` runs; `RunnerRequirements::production()` accepts jobs only with containment | M5 | M5-04 |
| CO-M5-02 host enforcement and snapshots | Production snapshot store the broker trusts; missing guarantees fail closed (INV-40); M5-SR06 trials executed before any host-enforcement claim | M5 | M5-03, M5-10 |
| M5-SR06 / SR-06 / R-G3 | SR-06-TRIAL-PLAN section 9 met (69 priority, then full acceptance; adversarial only on a VM or separate machine, R-SB) | M5 | M5-10 |
| CO-M5M9-01 key management | Keys never in repository or agent-reachable state; rotation and revocation tested; unknown, revoked or unavailable keys fail closed | M5 (local profile), M9 (operations) | M5-02 |
| CO-PROP-01 effect-store faults | Concurrent groups inject adapter-store faults; per-key model distinguishes every operation; mutants rejected; recovery final state checked against a serialization | M5 with the first real adapter; multi-worker M10 (D-040) | M5-05 |
| CO-M4X-01 delegation chain | Chain authenticated end to end; separation tests per member | "M5 or later (gate named in M5-START-PLAN)" | M5-11 (proposed M5, decision M5-D14) |
| CO-M4X-02 real authenticator and host boundary | Recorded real-device runs; loopback and host-binding assumptions verified on a real host | M5 (SR-06 family) | M5-12 |
| CO-M4X-03 ledger growth and sustained local DoS | Bounded growth under hostile load; retention policy enforced | M9 | none (recorded as an M5 risk only) |
| CO-M4X-04 legacy-label collision recovery | Recovery path tested; no history rewritten | M5 or migration milestone | proposed M12 (decision M5-D16) |
| CO-M4X-05 graceful shutdown | Clean shutdown releases the ownership lock and finishes in-flight requests | M5 | M5-13 |
| CO-DEP-01 `rsa` and `crypto-glue` dispositions | Patched resolution or explicit release disposition; `check_deps.py --release` passes | release gate | none; every M5 dependency repeats the D-041 review, and the `rsa` patched-release check is re-run at M5-00 |
| CO-M6-01, CO-M6-02, CO-M7-01, CO-HR-01 | unchanged | M6, M7, every closure edit | not M5 |

## 4. Trust and deployment model (planned)

The decisive local risk (M4-START-PLAN section 2, "Same-OS-user risk"): the agent runs as the human's OS user, so anything that user can read or write, the agent can too. INV-40 requires **agent-unwritable** snapshot storage; CO-M5M9-01 requires keys outside agent-reachable state; a hook that holds the ingress key would let the agent forge sealed requests. M5 therefore needs an OS-level separation between the agent's user and the trusted processes (decision M5-D3). Planned shape, subject to that decision:

| Component | Runs as | Holds | Agent can |
|---|---|---|---|
| Broker, ledger, effect stores, snapshot store, runner | dedicated service user | ledger, permit, record, runner and adapter-ingress keys | reach a local socket only |
| Host adapter (sealing part) | service user | ingress key | send unsealed host events over a socket; never read the key |
| Hook or tool client in the host | agent's user | nothing secret | everything it can today; its decisions are advisory unless the effect itself is broker-executed |
| Runner jobs | sandboxed, no access to service-user state | snapshot copy only | nothing outside the job |
| Approval server | service user, loopback | ledger | reach the page; authentication still needs a user-verified passkey |

The local host identity proof is weak: a socket peer credential identifies the OS user, not the agent versus the human. The adapter can bind tenant, project and session from configuration and peer credentials, but cannot authenticate "the agent" beyond that (master plan section 14, open question 2). Decision M5-D7.

## 5. First real effect adapter

**Candidates:** (a) `fs.write` into a broker-owned target root, content always sourced from the immutable snapshot store by digest (covers a governed file write and publication of an accepted snapshot, INV-45); (b) git commit to a broker-owned local repository; (c) remote HTTP API (for example a code-host pull request); (d) the runner itself (already an effect, no external system). Recommendation in decision M5-D8: (a).

**Contract for (a) (proposed; D-035 item 2 requires all four parts):**

- **Authorization.** Action kind `fs.write`; canonical resource `file:<tenant>/<project>/<relative path>` under a configured root; action digest binds the content digest (snapshot id and digest), write mode (`create_only`, `replace_if` with expected prior digest) and target. The permit is verified inside the adapter transaction in the existing order (effects.rs). At use time the adapter re-resolves the target beneath the root refusing symlinks, `..` and mount crossings (`openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, `rustix` `fs` feature; INV-14, T-03, T-04), and re-verifies the snapshot digest (INV-40). Bytes never come from an agent path.
- **Concurrency.** One adapter journal (`writ-fseffects-1`, SQLite) per target root, bound to one ledger with the identity-safe ownership lock (D-035 item 2 pattern); one write per target at a time; `replace_if` is a compare-and-swap on the current content digest; a mismatch is a recorded failed effect, never a silent overwrite.
- **Replay.** The permit id is consumed in the journal; a second presentation is `PERMIT_REPLAYED`; the broker's per-(tenant, key) rule is unchanged.
- **Recovery.** A filesystem write and a SQLite commit cannot share one transaction, so the current `EffectAdapter::apply` promise ("consume and apply atomically") cannot hold literally. Proposed two-phase journal: (1) commit permit consumption plus intent {target, prior digest, new digest, temp name}; (2) write a temp file in the target directory, fsync, `renameat` (no-replace for `create_only`), fsync the directory; (3) commit `applied`. A crash between (1) and (3) returns `outcome_unknown`; the receipt stays `dispatched`, then reconciliation reads the intent and the target: content equal to the new digest with no later intent is evidence of success; content equal to the prior digest with the temp file absent is evidence of failure; anything else keeps `unknown_reconcile`. Nothing is ever redispatched (D-035 item 3). This needs an owner-approved contract amendment (decision M5-D8).
- **Tests (first):** crash at each step with real SIGKILL (`kill_tests` style); symlink swap and directory rename between decision and use; concurrent writers to one target; full disk and permission failures; replayed permit; CO-PROP-01 property groups with injected adapter-store faults and a per-key model.

## 6. Task breakdown (tests first)

| ID | Task | Deps | Tests written first | Done when |
|---|---|---|---|---|
| M5-00 | Start gate: D-044 conditions met; owner decisions M5-D1..M5-D17 recorded as a D-entry; baseline SHA; TASKS rows; `rsa` patched-release recheck | D-044 | none | Decision recorded |
| M5-01 | Privilege separation per M5-D3: service user, socket boundary with peer credentials, file ownership and modes for ledger, stores, keys, work directories; install and teardown scripts for a disposable host | M5-00 | agent-user read and write of every trusted path refused at the OS layer (AG-ADV-18); canary key unreadable (INV-31); socket peer of another user refused | Trusted state agent-unreachable on the reference host |
| M5-02 | Local key management per M5-D6: key ids, provisioning ceremony, rotation with overlap, revocation recorded in the ledger, load-time permission checks | M5-01 | forged seal and permit after rotation; revoked, unknown, missing, world-readable key refused; no key in logs, `Debug`, environment or repository | CO-M5M9-01 local part met |
| M5-03 | Immutable snapshot store per M5-D5 behind both `SnapshotStore` and `SnapshotContent`, `writ-snapshot-manifest/1` digest, bound to one ledger | M5-01 | AG-NEG-11, AG-NEG-12, AG-ADV-17..19, AG-CON-04; crash during ingest; size limits | `immutable()` true only on the separated deployment; M3 limitation (two snapshot paths) removed |
| M5-04 | Contained sandbox per M5-D4; descendant kill; sweep of work directories at start; operator path for `started` runs (records abandonment, keeps the receipt uncertain; no client-asserted reconciliation, D-035 item 3, D-036) | M5-01, M5-03 | write outside work directory, network connect, fork bomb, `setsid` double-fork survivor, memory and CPU limits, descriptor leak, ledger and key access from the job | `RunnerRequirements::production()` accepts jobs on the reference host; CO-M5-01 met |
| M5-05 | First real effect adapter per M5-D8 and its contract amendment | M5-02, M5-03 | section 5 tests; CO-PROP-01 properties and mutants | Contract documented; CO-PROP-01 met for one worker |
| M5-06 | HostAdapter contract (proposed schemas): host event envelope, coverage matrix, decision mapping, advisory and governed labels | M5-00 | unknown tool DENY (governed) and labelled ungoverned (advisory); malformed and oversized envelopes (INV-18); no promotion shortcut | Schemas in `docs/contracts/proposed/`, owner review |
| M5-07 | Claude Code adapter per M5-D9: hook bridge (advisory, fail-closed mapping derived from SR-06 observations) plus a broker-executed tool path for governed effects | M5-02, M5-05, M5-06 | forged session ids, interleaved sessions (INV-19), crash, timeout and unparseable hook output, subagent events, unknown tools | Coverage matrix published from observed behaviour only |
| M5-08 | Second independent adapter per M5-D10 and portability differential | M5-06 | identical fixtures through both adapters; Unicode, symlink, huge payload, host failure, nested subagent cases | Portability acceptance met |
| M5-09 | ModelAdapter contract, two stub providers, AgentRunner stub per M5-D11; coding, research and review fixtures | M5-06 | model outage during policy evaluation (INV-21); host task with no ModelAdapter (INV-22); fallback to forbidden capability (INV-26); stub tool proposals never authorize | Master-plan M5 gate rows met |
| M5-10 | M5-SR06 live trials per M5-D12: 69 priority experiments, then SR-06-TRIAL-PLAN section 9; UNMEDIATED-TOOLS updated; adapter requirement written from observed behaviour | M5-07 | the trial matrix | Section 9 met by a named operator; results in `docs/audit/m0/live-host/<run-id>/` and TEST-EVIDENCE |
| M5-11 | Trusted delegation chain per M5-D14: chain sealed by the adapter, member identities in request contexts and fingerprints, separation over every member | M5-07 | subagent approving parent's request; chain truncation and reordering; forged member; D-011 no inheritance preserved | `delegation_chain: unavailable` replaced for requests with an authenticated chain |
| M5-12 | Real-authenticator and host-boundary verification per M5-D13 (CO-M4X-02) | M5-01 | checklist runs: enrol, approve, deny, signoff, revoke, read session, wrong origin, removed key, no-UV device refused | Recorded runs; no real user in a non-disposable ledger |
| M5-13 | Graceful shutdown of `writ-approval-server` per M5-D16 (CO-M4X-05) | M5-00 | SIGTERM during a ceremony; lock released; in-flight request finished or refused cleanly | CO-M4X-05 met |
| M5-14 | M5 gate-review package: evidence, exact-SHA CI, limitations, carried obligations | all | none | Owner review |

Ordering: M5-01 first (everything trusted depends on it); M5-02, M5-03 in parallel; then M5-04 and M5-05; M5-06 can start at M5-00; M5-07 needs the foundations; M5-10 needs M5-07 and the trial environment. Bounded parallel workers with disjoint ownership as in D-040..D-044; the lead owns shared contracts, migrations, dependencies and CI; independent security review of M5-01..M5-05 and of the adapter boundary.

## 7. What existing decisions already settle (not asked again)

| Topic | Settled by |
|---|---|
| Rust for kernel and broker; one production core | ADR-0002, D-030 |
| No host-enforcement claim before M5-SR06; adversarial trials only on a VM or separate machine | D-014 item 3, R-G3, R-SB ([M0-GO-NO-GO](M0-GO-NO-GO.md)) |
| Every production adapter has explicit concurrency, replay, authorization and recovery contracts; at-most-once dispatch is not exactly-once | D-035 item 2, D-039 |
| No client-asserted reconciliation; uncertain effects never redispatched; adapter storage failure precedence | D-035 items 3 and 4, D-036 |
| Runner fails closed without full containment; uncontained profile test-only | M3-07, D-035 item 6, [M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md) |
| Content-addressed immutable snapshot is the default; any equivalent needs its own ADR | [acceptance-gate-integrity](../architecture/acceptance-gate-integrity.md) section 6a, INV-40 |
| Host-side transitions advisory; no host promotion shortcut | ADR-0008 section 13 item 6 |
| Pack and model output never grant authority; no model SDK in kernel or broker | ADR-0006, INV-21..INV-23 |
| Subagents never inherit parent grants | D-011 |
| Dependency adoption process (reproduced closure, licences, advisories, exact allow-list entries) | D-041 DR-1..DR-4, D-042 DR-5 |
| Approval channel loopback, WebAuthn local profile, enterprise OIDC is future work | D-040 O-1, O-12 |
| CO-PROP-01 at M5 with the first real adapter; multi-worker at M10 | D-040 |
| Ledger growth and sustained loopback DoS at M9 | D-044 item 5, CO-M4X-03 |
| Control Center CC-M5 separately authorized | ADR-0007, D-015 |

## 8. Risks

1. **Scope.** M5 combines four trust foundations with two adapters, model stubs and live trials; without a split (M5-D1) the gate review becomes very large.
2. **Same-user deployments.** If the owner keeps a single-user profile, INV-40 and key isolation cannot be met and the runner and promotions must keep failing closed; this must not be relabelled as enforcement.
3. **Host kernel features.** Unprivileged user namespaces are restricted by default on some distributions (for example AppArmor restrictions on recent Ubuntu); cgroup v2 delegation to user sessions varies; GitHub-hosted runners may not allow containment tests (E-071 precedent of host-specific CI failures).
4. **`unsafe` forbid.** In-process namespace setup is impossible without an exception; an external launcher moves trust to that binary.
5. **Host drift.** Claude Code's hook contract and version change; SR-06 plan revision 3 was prepared against 2.1.318; preflight must be repeated on the pinned version.
6. **Trial environment.** No hypervisor on the workstation; adversarial trials need a VM or separate machine and a dedicated revocable model credential.
7. **Non-transactional effects.** A filesystem or remote effect leaves real uncertainty windows; reconciliation evidence can be ambiguous if any unmediated writer can touch the target.
8. **Identity.** Local host events cannot prove agent identity beyond the OS user and configuration; delegation-chain claims depend on adapter-observed lifecycle events.
9. **Dependency growth.** Sandbox, MCP or signal-handling crates enlarge the trusted closure; each needs the D-041 review; `rsa` (RUSTSEC-2023-0071) still blocks release (CO-DEP-01).
10. **Denial of service.** Sandboxed jobs hold the policy gate and workflow lock for their wall time (M3 limitation); loopback DoS (CO-M4X-03) remains until M9.

## 9. Gate proposal for M5

All of: master-plan M5 gate; AG-M5; portability acceptance; CO-M5-01, CO-M5-02 (including M5-SR06 section 9), CO-M5M9-01 local part, CO-PROP-01 (single worker), CO-M4X-01, CO-M4X-02 and CO-M4X-05 as assigned by section 10; closure and property gates still required and green; exact-SHA CI. Not claimed: production enforcement, enterprise key management, multi-worker effects, release.

## 10. Owner decisions required (genuinely new)

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| M5-D1 | Milestone shape | (a) one M5; (b) M5a trusted foundations (M5-01..M5-05, M5-13) then M5b host and model adapters, SR-06, delegation, real authenticator (M5-06..M5-12) with separate gate reviews | (b): foundations are prerequisites and reviewable on their own |
| M5-D2 | Host OS and kernel scope | (a) Linux only, minimum kernel with cgroup v2, user namespaces, `openat2`, Landlock ABI 1 (5.13 or later), other platforms refuse runner jobs and governed writes; (b) Linux and macOS; (c) add Windows | (a); other platforms fail closed and are labelled advisory |
| M5-D3 | Privilege separation | (a) dedicated service OS user running broker and stores, agent reaches sockets only; (b) same user with documented limitation (INV-40 and key isolation not met, runner stays refused); (c) trusted side in a container or VM | (a) for the local governed profile; (b) remains the advisory profile, labelled as such |
| M5-D4 | Sandbox technology and new dependencies | (a) bubblewrap as an external launcher (namespaces, read-only binds, no network, `--die-with-parent`) inside a cgroup v2 transient unit for resources and tree kill; (b) in-crate namespaces, Landlock and seccomp via crates, requiring a reviewed `unsafe` exception for a small launcher crate; (c) rootless container runtime; (d) microVM | (a), with Landlock as optional defence in depth; the external binary is pinned and recorded in the dependency review; process-group kill via `process_group(0)` plus `rustix` `process` feature as a second layer |
| M5-D5 | Snapshot store design | (a) separate SQLite content-addressed store file (`writ-snapshots-1`) owned by the service user, bound to the ledger, digests checked at ingest and every read, size caps; (b) blobs inside the ledger; (c) directory of read-only content-addressed files with an index; (d) filesystem snapshots or reflinks (needs its own ADR per section 6a) | (a): reuses the owned-store pattern, transactional ingest, one digest path for runner and promotion |
| M5-D6 | Key storage backend | (a) key files owned by the service user, mode 0600, generated by an operator ceremony, ids and validity windows, revocation in the ledger; (b) OS keyring of the human's user (agent-readable, rejected as sole store); (c) TPM-sealed keys; (d) external KMS or HSM (enterprise, M10) | (a) behind a key-source trait so (c) and (d) can follow; never (b) |
| M5-D7 | Local host identity and ingress-key custody | (a) sealing adapter runs as the service user, binds tenant, project and principal from configuration plus socket peer credentials, and labels agent identity as host-asserted; (b) per-session tokens issued at session start by a human-authenticated ceremony; (c) hook holds the ingress key (rejected: agent-readable) | (a) for M5, with every host-asserted field labelled; (b) as a later option for governed sessions |
| M5-D8 | First real effect adapter and contract amendment | (a) `fs.write` from snapshot store into a broker-owned root (section 5), with a two-phase journal amendment to the `EffectAdapter` contract; (b) local git commit; (c) remote API; (d) none in M5 | (a), with the amendment recorded as a D-entry before implementation |
| M5-D9 | Claude Code integration shape | (a) hooks (advisory, fail-closed mapping) plus a broker-executed tool exposed over MCP (new protocol dependency or a minimal JSON-RPC over the strict kernel JSON parser); (b) hooks plus a CLI tool only; (c) hooks only (advisory only) | (a) with a minimal reviewed MCP implementation choice deferred to a dependency review; governed effects only through the broker-executed tool |
| M5-D10 | Second independent adapter | (a) generic MCP or CLI adapter usable by any host; (b) a second real host (Codex or Gemini CLI); (c) a simulated host as in M0 | (a): independent code path, no second vendor credential; (c) is not independent enough for the portability claim |
| M5-D11 | Language and packaging of host adapters, ModelAdapter and AgentRunner stubs (outside ADR-0002 scope) | (a) Rust crates separate from kernel and broker, source scanner proving no model SDK in their closures; (b) Python reusing M0 stubs; (c) mixed | (a) for the sealing adapter (trusted); stubs in Rust for one toolchain; no real provider SDK in M5 |
| M5-D12 | SR-06 trial environment | (a) local KVM VM (needs hypervisor install with sudo) with a dedicated revocable model credential provided by the owner; (b) separate physical machine; (c) disposable cloud VM; container use for probe-only experiments only if the owner accepts R-SB | (a) or (c); owner provisions and revokes the credential; Claude Code version pinned and preflight repeated |
| M5-D13 | Real-authenticator test plan | (a) owner-performed checklist with one roaming FIDO2 key and one platform passkey on a disposable ledger and RP id, results recorded; (b) automated hardware-in-the-loop; (c) defer to M9 | (a); never enrol a real user in a non-disposable ledger |
| M5-D14 | Delegation-chain gate | (a) M5 with the Claude Code adapter (subagent lifecycle events), new sealed request field and fingerprint change; (b) later milestone | (a): the adapter is where the chain is observable; separation stays refused where the chain is unavailable |
| M5-D15 | CI for containment and host tests | (a) self-hosted or VM runner for containment tests as a required job; (b) GitHub-hosted only with tests skipped when unsupported (rejected: silent skip, as D-044 item 2); (c) local evidence only | (a), failing when the host lacks the features; until it exists, containment evidence is local and recorded, not claimed as required CI |
| M5-D16 | Small carry-overs | CO-M4X-05: adopt Tokio `signal` feature (feature addition under review) or self-pipe via `rustix`; CO-M4X-04: M5 operator recovery operation or M12 migration tooling | Tokio `signal` in M5; CO-M4X-04 at M12 (no deployed pre-M4 ledgers; stays fail closed) |
| M5-D17 | Snapshot ingest source | (a) agent submits bytes over the socket and the store digests them; (b) service user copies from the agent's working copy path; (c) both | (a): no pathname from the agent is reopened (section 6a); (b) only with no-follow and post-copy digest check |

## 11. References

[DECISION-LOG](DECISION-LOG.md) (D-011, D-014, D-030, D-035, D-036, D-039..D-044), [MASTER-IMPLEMENTATION-PLAN](MASTER-IMPLEMENTATION-PLAN.md) (sections 10, 14, 17, 18), [TASKS](TASKS.md) (CO-M5-01, CO-M5-02, CO-M5M9-01, CO-PROP-01, CO-M4X-01..05, CO-DEP-01, M5-SR06, CC-M5), [M4-GATE-REVIEW](M4-GATE-REVIEW.md), [M4-INTERFACES](M4-INTERFACES.md), [M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md), [invariants](../security/invariants.md) (INV-01, INV-13..INV-16, INV-21..INV-23, INV-31, INV-39, INV-40, INV-45), [threat model](../security/threat-model.md) (enforcement profiles, T-03..T-07, T-13, T-17), [ENTERPRISE-READINESS](../security/ENTERPRISE-READINESS.md) (ENT-R05), [host adapters](../architecture/host-adapters.md), [UNMEDIATED-TOOLS](../audit/m0/UNMEDIATED-TOOLS.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md), [ADR-0006](../decisions/ADR-0006-model-and-domain-independence.md), [ADR-0008](../decisions/ADR-0008-acceptance-gate-integrity.md).

Release and production remain NO-GO; ADR-0008 APPROVED IN PRINCIPLE.
