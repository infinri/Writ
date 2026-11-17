# M5a gate-review package (M5a-14; D-045, D-046, D-047)

Status: **PREPARED; NOT READY FOR A GATE DECISION.** Every M5a component is implemented and verified in user space with required CI green, but D-045 M5-D15 makes the containment gate depend on an isolated disposable VM runner, and the privileged and host-boundary checks can only run on the designated reference VM. Both are external blockers (owner provisioning). Nothing here claims host enforcement or production readiness. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

## 1. What the owner is asked for

1. **Provide the designated disposable reference VM** (D-045 M5-D12): Ubuntu 24.04 LTS, rollback capability, namespace and cgroup v2 support, a root-owned marker `/etc/writ-disposable-vm` naming it (see `m5/reference-vm/README.md`). Local VM preferred; a cloud VM needs separately approved provisioning and cost.
2. **Provide the isolated containment CI runner** (M5a-12, M5-D15): a disposable VM runner with no exposure to unreviewed fork code or secrets. See the [draft workflow and provisioning procedure](../../m5/containment-ci/README.md).
3. When both exist, the lead follows the [reference-VM runbook](../../m5/reference-vm/README.md) to run `setup.sh`, `trial.sh`, `service-install.sh` and the verify-in-VM checklist, records the evidence, wires the required containment job, and presents this package again for the gate decision.

## 2. Scope delivered (TASKS M5a-00..M5a-14)

| Task | Status | Evidence |
|---|---|---|
| M5a-00 start gate, RSA recheck | DONE (RSA still blocked: `crypto-glue` has no release accepting `rsa` 0.10) | M5-DEPENDENCY-REVIEW |
| M5a-01 interfaces | DONE: M5-INTERFACES BINDING revision 6 after independent reviews (J-1/J-2/J-3/J-6/J-8/J-9; J-7 three rounds; J-11 two rounds) | M5-INTERFACES |
| M5a-02 dependency review | DONE: rustix features, Tokio `signal` (+ `signal-hook-registry` 1.4.8), source rules incl. spawn reachability; `rmcp` deferred to M5b | M5-DEPENDENCY-REVIEW |
| M5a-03 capability probes, VM scripts | IMPLEMENTED; VM run BLOCKED | E-080, E-081 |
| M5a-04 privilege separation | IMPLEMENTED (user space): `writ-rpc`, `writ-serviced`, ledger key registry and revocation, honest identity sealing | E-082 |
| M5a-05 key source | IMPLEMENTED (user space) | E-078, E-082, E-087 |
| M5a-06 snapshot store | IMPLEMENTED (user space), deployment proof with periodic recheck | E-079, E-087 |
| M5a-07 containment and job launcher | IMPLEMENTED (unprivileged part); privileged steps BLOCKED | E-083, E-087 |
| M5a-08 fs.write recovery contract | DONE (design, J-7 revision 3) | M5-INTERFACES |
| M5a-09 fs.write adapter | IMPLEMENTED (user space); service composition with M5b-03 | E-084 |
| M5a-10 CO-PROP-01 for fs.write | IMPLEMENTED (single worker) | E-086 |
| M5a-11 graceful shutdown | IMPLEMENTED (user space); CO-M4X-05 closed for user space | E-085 |
| M5a-12 containment CI runner | BLOCKED (external) | |
| M5a-13 independent security review | DONE: no CRITICAL; H1, M1..M7 and lows fixed or dispositioned | E-087 |
| M5a-14 this package | PREPARED | |

## 3. Verification

- Required CI on the published recovery SHA `482129d7a6b05a89451d576f94da31d4fe23f857`: [run 43951124726](https://github.com/infinri/Writ/actions/runs/43951124726), 6/6. This run supplies user-space evidence.
- Required CI on `4f4f92f`: run 43948789763, 6/6 (production workspace incl. browser suite, kill tests, fs.write SIGKILL property, closure and property gates, dependency gate with OSV and source rules).
- Local: 920 workspace tests passed, closure gate exact 17 / adapted 72 / approved differences 38 / deferred 0, property gate 19,796 checks 0 failed, parity 320/320.
- CI incidents investigated and corrected with preserved evidence: E-081 (test path rewrite), E-083 (directory order), E-084 (staging lock released late while a concurrent child held a duplicate; real production bug, fixed).

## 4. Verify-in-VM checklist (all open)

From J-11.10 and the reviews: bubblewrap 0.9.0 `--block-fd`, `--json-status-fd`, descriptor closing, pre-gate process count; `std` group clearing; AppArmor stacking and attachment under `no_new_privs`; `StandardInput=socket`, `TemporaryFileSystem`, `SystemCallFilter`, `ProtectKernelTunables`, `LimitNOFILE`, `--remount-ro /`; H1 termination timing and `cgroup.freeze`; `/proc/key-users` baseline; apport and `core_pattern`; service socket activation, `pidfd_getfd`, restart holding sockets, overflow-uid refusal, `KillMode` against the hard deadline; real cross-uid denial (P-AGENT-DENY, P-AGENT-DESKTOP, P-JOB-DENY, P-PEERCRED); fs.write real ENOSPC, `dm-error`/`dm-flakey` EIO, mount crossings, casefold, identity across reboot; the P-FD status-pipe control.

## 5. Limitations and carried obligations

- Trust boundary: in-process code linking the crates is trusted like ledger-file access; `LedgerCapability` scanner-confined.
- Identity facts from passwd and group files only; seal lifetime fixed at 30 s; inherited activation descriptors stay open (hence the enforced no-spawn rule); revocations sync into the key source only via `admin.sock`.
- Jobs serialized host-wide (shared job uid); concurrent jobs need a new owner decision (M10).
- Carried: CO-M4X-01 delegation chain (M5b), CO-M4X-02 real authenticator (M5b), CO-M4X-03 ledger growth and sustained local DoS (M9), CO-M4X-04 legacy-label recovery (M12), CO-DEP-01 `rsa`/`crypto-glue` dispositions (release gate), CO-M5M9-01 production key management (M9), CO-M6-01, CO-M6-02, CO-M7-01, CO-HR-01.

M5 remains in progress; M5b (host and model adapters, portability, delegation, SR-06, real-authenticator trials) follows under D-045.
