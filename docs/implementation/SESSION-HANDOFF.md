# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-10 America/Chicago; after M1 measurements and enterprise readiness direction)

**This is the authoritative recovery entry point. It is not authorization to do anything beyond what is listed under "Authorized next task"; re-establish other authorizations from the owner's newest message.**

**After compaction (checkpoint 2026-11-12, M3 in progress, NOT complete):**

- **Repository:** branch `v2/architecture-rebuild`; verified published baseline `aff6ce33e2ec9543321be3224a2a92ee9e27db12` (docs checkpoint), CI https://github.com/infinri/Writ/actions/runs/43343727179 **success, all 6 jobs**; last verified code baseline `8ead392`, CI run 43340059204, all 6 jobs. D-035 commit `5fdcd306acc814bcb713d784591d189aa6f01877`, CI run https://github.com/infinri/Writ/actions/runs/43346923290 **success, all 6 jobs**. The docs commit after it (M3-05 design note) has CI pending until verified. Working tree: only untracked `docs/audit/m0/hook-audit/` (private owner material: never stage, publish or summarize).
- **Done in M3 (each CI green):** M3-PLAN; M3-00 workspace and per-crate dependency gate with standalone and combined views; M3-01 single-owner SQLite ledger (canonical identity, exclusive lock plus fencing epoch, create versus open with exact schema verification); M3-02 sealed ingress, use-time re-decision through `writ-kernel`, guard-bound policy gate (`ActivePolicy`), permits with `PERMIT_STALE`, semantic contract validation (`broker::contracts`), trusted time (`rustix` `CLOCK_BOOTTIME`, persisted floor, D-034); M3-03 authenticated approvals and atomic grant consumption (E-064); M3-04 execute lifecycle with a separate effect store, D-026 stage evidence, fault injection at every stage, real SIGKILL tests (E-065). D-035 (E-067, `5fdcd30`, CI green). Schemas: ledger `writ-ledger-4`, effects `writ-effects-2`.
- **Fork and inherited-lock finding (E-066):** a fork duplicates every open descriptor, including `.owner` lock descriptors, until the child execs; CI failed once because a parallel test reopened its ledger during another test's spawn. **CI isolation fix (done):** process-kill tests run alone in a dedicated CI step; ownership tests serialized. **Production obligation (open, M3-07):** the broker and mechanical runner must handle inherited descriptors and coordinate broker and ledger shutdown or restart with child spawning in production code and tests; the CI fix does not resolve it.
- **D-035 implemented (E-067, CI green at `5fdcd30`):** reservation revalidation inside T-dispatch, permit expiry capped by grant expiry, no refunds; effect store with its own identity-safe owner lock, bound to the ledger's `ledger_id`; `present_permit` never applies an effect; reconciliation records outcomes only from the adapter's evidence for the receipt's own permit; `outcome_unknown` for failed commits and `failure_records`; approver provenance (`writ-provenance-1`) in schema `writ-ledger-4`, explicit `upgrade_from_v3`, legacy grants non-executable. Effects schema `writ-effects-2`. **Owner item:** M1 fixture b-36 needs an exact M3-08 exception (reconcile now records `observed_success` from evidence).
- **Next action after recovery:** verify the latest docs commit's CI; M3-05 is designed but **blocked on the owner questions** in [M3-05-TRANSITION-SERVICE](M3-05-TRANSITION-SERVICE.md) section 3 (record authentication, promotion permits, transition reconciliation, b-36 exception, trusted workflow inputs); after the answers implement M3-05 transition service, M3-06/M3-07 integration (contracts approved with semantic validation; use-time MAC, registry, revocation, replay, immutable-snapshot and atomic-consumption tests mandatory; M3-07 inherited descriptors and restart coordination), M3-08 closure (127 cases, differential, chaos). Do not re-request these approvals.
- **Out of scope:** PostgreSQL, enterprise services, Control Center, real-host integration (M5 real-host clock security, SR-06). ADR-0002 ACCEPTED (Rust, scoped); ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Enterprise planning update:** owner delegated the readiness approach after discussing Rust, PostgreSQL and SOC 2. [ENTERPRISE-READINESS](../security/ENTERPRISE-READINESS.md), ENT-OWNER-01 and ENT-00..ENT-08 allocate controls across M2..M12. SQLite stays local/offline; PostgreSQL targets enterprise state with a separate future coordination/recovery gate. Controls and examinations remain planned. Self-hosted customer audit responsibilities do not replace any examination of a Writ-operated service. ADR-0008 stays APPROVED IN PRINCIPLE; release and production NO-GO. No paid engagement or production action is authorized.

### A. Repository state (verify with the commands in section H)

| Item | State |
|---|---|
| Branch | `v2/architecture-rebuild` (never `main`) |
| Verified published baseline | `aff6ce3` docs checkpoint, CI run 43343727179 **success, all 6 jobs**; code `8ead392`, CI run 43340059204 **success, all 6 jobs** (incl. isolated process-kill step); D-035 `5fdcd30`, CI run 43346923290 **success, all 6 jobs** |
| Last code commit | `8ead392` (test isolation fix); M3-04 `3c92d42`, M3-03 `844407e`, trusted time `1e193cc` |
| This update | M3 authorized (bounded); [M3-START-PLAN](M3-START-PLAN.md); use git and CI to verify publication |
| Working tree | untracked `docs/audit/m0/hook-audit/` (private owner material: never stage, publish or summarize); backup branch `backup/m1-08-pre-rebase-567e088` is local only |
| Milestone | **M3 in progress, not complete** (M3-00..M3-04 and D-035 implemented; M3-05 next). Release and production **NO-GO** |

Commit chain since M1-05 (all published): `9e6f9f1` M1-06a harness; `a44c9cc` D-022; `935e4e3` D-023 SQLite ledger; `e1dc2e1` M1-07 report (CI failed: system SQLite linked); `3012940` D-024 plus CI fix; `6b7eab1` M1-06d broker timing; `68f4593` D-025; `8d1915c` docs; `d9815f8` guard refuses discharging battery (CI failed: busy timeouts, E-034); `1994b67` docs; `7a5a366` D-026; `988a840` docs; `d9054c9` docs checkpoint; `52b31fe` D-027; `41529d1` docs (CI red, E-039); `a0769b6` docs (CI red, same cause); `c5effec` D-028, D-029; `d99faa2` docs (CI green, E-044); `612e1f4` docs; `138163d` readiness docs; `a91af1d` official results and scorecard.

### B. Architecture in one page

- **Kernel:** pure, deterministic, key-free authorization (no I/O, injected time); strict JSON (D-017), D-006 canonical form; decides allow, deny, require_approval, invalid or unavailable with matched rule ids ([V2-ARCHITECTURE](../../V2-ARCHITECTURE.md)).
- **Broker:** the only path to governed effects. Adapter-sealed ingress (D-010), use-time re-decision, approval grants from authenticated humans on a separate channel (single use, bound to the full request, D-009), permits bound to the full request fingerprint and bundle (D-012), idempotency receipts and reconciliation; effect adapter simulated in M1.
- **SQLite ledger (D-023..D-027):** one pinned SQLite 3.54.1 static library linked by both prototypes; WAL, `synchronous=FULL`, `busy_timeout=5000`, `BEGIN IMMEDIATE` logical transactions (reserve, dispatch, adapter, observe, approve, reconcile, audit), per-key locks, durable receipts, grant uses, refusal audit, simulated adapter tables; fail closed on storage failures with one sanitized response ([LEDGER-SQLITE](../../m1/fixtures/broker/LEDGER-SQLITE.md), [BROKER](../../m1/fixtures/broker/BROKER.md)).
- **Acceptance Gate Integrity:** protected workflow transitions need a trusted, version-bound acceptance record bound to tenant, project, task, workflow, transition, artifact snapshot id and digest, policy version and state revision, checked by the broker plus kernel structure checks; promotion separate from publish or deploy; model-assisted findings advisory only ([spec](../architecture/acceptance-gate-integrity.md), ADR-0008 **APPROVED IN PRINCIPLE**).
- **Model independence:** model output is untrusted; kernel and broker import no model SDK; packs never grant permissions.
- **Bake-off:** equivalent Rust and Go prototypes from shared language-neutral specifications, implemented by separate agents that never read each other's code; ADR-0002 **OPEN** until final owner decision after targeted M1-08 confirmation; Rust is the provisional leader and preferred direction.
- **Writ Control Center:** roadmap (D-015, ADR-0007, `docs/control-center/`); not started.

### C. Completed work (do not repeat)

M1-00 toolchains; M1-01 parity driver; M1-02 fixtures (amendments 1..7, 12 sets, **447 cases**, all PENDING INDEPENDENT REVIEW); M1-03 Rust and M1-04 Go prototypes; M1-05 property, metamorphic, concurrency and differential harness; M1-06a guarded benchmark harness; M1-06b D-022; M1-06c D-023 SQLite ledger; M1-06e D-024 storage failures; M1-06d broker timing (`bench-broker-scenario`); M1-06f D-025 corrections; M1-06g D-026 concurrency oracle; D-027..D-029 implemented and CI green (E-044); official M1-06 and M1-07 measurements recorded (E-046/E-047). M1-07 facts collected ([report](M1-07-BUILD-ASSESSMENT.md)); newline-only incremental timings are invalid for comparison and require replacement.

### D. Decisions in force ([DECISION-LOG](DECISION-LOG.md))

| Decision | Status |
|---|---|
| D-001..D-013 | accepted (M0) |
| D-014 | M0 development GO (16 risks open) |
| D-015 Control Center, D-016 Acceptance Gate Integrity | accepted; ADR-0008 APPROVED IN PRINCIPLE (not ACCEPTED) |
| D-017 strict JSON, identifiers, exact transitions, permits, completeness | implemented, tested |
| D-018 final semantics A1..A8; D-019 protection in bundle, effect prerequisites; D-020 refusal mapping, transition support | implemented, tested |
| D-021 strict protocol responses | implemented, tested |
| D-022 decide input validation | implemented, tested, approved (D-024) |
| D-023 SQLite ledger required for workload B | implemented, tested |
| D-024 trimpath, Linux x86_64 only, storage failures fail closed, `ReadID` removed | implemented, tested |
| D-025 DEP-1, C-1..C-6, C-4 grant integrity, adapter precedence, S-1, S-2, sanitized stderr | implemented, tested |
| D-026 verified lock-contention aborts, availability separate from correctness | implemented, tested, CI green at `7a5a366` |
| D-027 standardized concurrent-worker start | implemented (`52b31fe`), refined by D-028 |
| D-028 equivalent release protocol | implemented (`c5effec`), CI green E-044 |
| D-029 process-local coordination gate, `STATE_CONFLICT`, verified contention acceptance | implemented (`c5effec`), CI green E-044; process-local only |

### E. Historical and current evidence ([TEST-EVIDENCE](TEST-EVIDENCE.md))

Current runtime/measurement records: E-042..E-047; published baseline CI `a91af1d`, run 42943984397 green. ENT-E01 records only readiness documentation checks. The next paragraph is historical evidence at `7a5a366`, not a current benchmark status.

At `7a5a366` (CI run 42898828550, success, all 5 jobs; and locally, E-035): Rust 58 unit + 3 integration tests; Go vet clean, race tests ok in 6 packages; both link SQLite 3.54.1 (source id `2026-10-16 14:45:20 555e07ef...`), identical settings; fixtures **447/447** in memory and on SQLite for each prototype; regressions 11/11; `m1/ledger_tests` **30** OK (13 storage failures, 3 timing integrity, 14 D-025); D-026 oracle negative tests **14** OK; memory property CI profile seeds 20261107 and 1; Rust-SQLite versus Go-SQLite broker campaigns with 64-way groups PASS (CI 348 cases, 6,780 checks; local 2,584 cases, 52,920 checks; 0 mismatches); mutation smoke 9 of 9 mutants caught for each (including `masked-effect`); harness self-tests 18 OK; benchmark dry run 0 correctness failures; M0 281 passed; drift, driver and docs checks OK. Availability (preliminary, not comparable before D-027): E-034 Rust 89 / Go 6 timeouts of 12,800; E-036 CI Rust 2 of 1,408 at 64-way. Guarantee level: **Tested (unit or reference) only**; no test result is a real-host security certification. **At that historical checkpoint no official measurement existed; E-046/E-047 now record the official measurements.**

### F. Next task: M2 completion (see the top of this checkpoint and TASKS rows M2-00..M2-06)

Plan: [M2-START-PLAN](M2-START-PLAN.md). The completed M1-08 confirmation record follows for reference.

**Status (2026-11-12): ALL ITEMS DONE** (2: E-052; 4: E-053; 7: scorecard final draft with recommendation). Earlier status (2026-11-11): item 1 evidence bundle DONE (`m1/bench/evidence/2026-11-11-seed-20261111/`); item 3 DONE (E-048: instrumentation boundary, Go fixed); item 5 DONE (E-049: real SIGKILL campaign, 0 violations); item 4 tooling in place, edit must change retained compiled code before official timings (owner 2026-11-11); items 2 and 4 timings and item 7 pending: approved seeds 20261121 and 20261131, guard must pass before each run.

The owner supplied an explicit M1-08 review directive in session, then preferred Rust and delegated enterprise readiness planning. Preserve its bounded scope:

1. `a91af1d` is already published and all 5 jobs passed on that exact SHA; do not push it again or force-push. Inspect the original local tree and remote before any integration or later push. Preserve the full original evidence; inspect sizes/content/sensitivity and create a separate sanitized reproducibility bundle or archive/manifest while keeping raw local output ignored. Retain seed 20261111, raw samples/distributions, commit/binary/input/fixture hashes, toolchains/flags, environment, commands, run order, correctness and refusal counts. Never discard the only complete original copy.
2. Repeat the controlled comparison with at least two additional recorded seeds and independent run sets, alternating Rust/Go order. Retain raw data and run-to-run variation for kernel latency/throughput, memory, cold start, process throughput and SQLite broker metrics. Useful throughput counts successful operations separately from refusals.
3. Investigate the Go duplicates-workload process overhead with equivalent instrumentation and unchanged workloads. Distinguish serialization/buffering, startup/shutdown, instrumentation boundaries, GC, connection cleanup, defects and harness cost; report a verified cause or explicit unresolved limitation.
4. Replace newline-only build timings with a reproducible behavior-preserving, compiler-relevant edit in disposable clean source trees and isolated caches. Verify actual recompilation/relinking and retain repeated timings and commands.
5. Implement targeted M1 chaos/interruption coverage using disposable resources and real child-process termination/restart where feasible. Verify no grant overuse, unauthorized effect or duplicate dispatch after uncertainty; durable receipt/reconciliation and fail-closed storage behavior. A simulated early return is not a process kill. Keep the process-local reconciliation limitation unresolved and do not claim real-host certification.
6. Every new official comparative run requires full environmental readiness at its start: temperature, clocks/throttling indicators, load, power/battery and governor, plus clean recorded source/toolchains/binaries. Stop if any guard fails. Never bypass safeguards, change privileged settings or stop user applications. The existing owner directive authorizes these bounded runs conditional on the guard; no repeated permission request is needed within that scope. Do not run them on a replacement cloud host as if they reproduced the original environment.
7. Update the scorecard with correctness/security, repeated measurements, resources, dependencies, reproducibility, maintenance, broker behavior and explicit limitations. Separate measurements, facts and judgments; recommend Rust versus Go once targeted evidence is complete. Await the final owner decision before accepting ADR-0002. ADR-0008 remains APPROVED IN PRINCIPLE and production/release NO-GO.

Enterprise control implementation (ENT-01..ENT-08) belongs to later milestones and does not replace these investigations. Update handoff/evidence before compaction. This documentation checkout has not performed new comparative measurements, real process kills or preservation of the original ignored evidence.

### G. Unverified security obligations and blockers (never claim otherwise)

Independent human review of the 26 golden cases and all M1 fixtures (REL-01); review of the 288 V1 rules (M6-RULES); SR-06 real-host verification (M5-SR06; adversarial trials only on a VM or separate machine); 16 D-014 risks open; authenticated approval service and evaluator issuance, attestation and signatures; real snapshot immutability and credential isolation; trusted production clock and key management; real process-kill and power-loss recovery (ledger crash tests are early returns); real external exactly-once effects (adapter simulated); remaining adversarial and host-bypass tests; real-host enforcement. ADR-0002 OPEN; ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.

**Historical pre-measurement environment ([M1-06-READINESS](M1-06-READINESS.md)), not current readiness:** reading 2026-11-10: full-power charger not connected (battery discharging on a 5 V / 3 A USB-C supply, about 15 W); governor back to `powersave` (owner restored it); 1-minute load 3.52 (IntelliJ, 22 Docker containers, Chrome, Claude sessions); CPU 60 C (needs a fresh check). The guard (`m1/bench/wbench/envinfo.py`) must pass; never bypass or relax it; never change CPU settings or stop the owner's processes.

### H. Commands

```bash
# state
git branch --show-current && git fetch -q origin && git status --short
git log --oneline -8 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
# build and unit tests
source m1/tools/env.sh
(cd m1/rust && cargo build --release --locked && cargo test --release --locked)
(cd m1/go && T="libsqlite3 sqlite_omit_load_extension" && go vet -tags "$T" ./... && go build -trimpath -tags "$T" -o proto ./cmd/proto && go test -race -count=1 -tags "$T" ./...)
R=$PWD/m1/rust/target/release/proto; G=$PWD/m1/go/proto
# fixtures, both ledgers
for b in $R $G; do python3 m1/driver/parity.py --proto $b --manifest m1/fixtures/manifest.json --repeat 2; WRIT_PROTO=$b python3 m1/driver/parity.py --proto $PWD/m1/tools/sqlite-ledger-proto.sh --manifest m1/fixtures/manifest.json --repeat 2; python3 m1/driver/parity.py --proto $b --manifest m1/props/regressions/manifest.json; done
# language-neutral and oracle tests
WRIT_RUST=$R WRIT_GO=$G python3 -m unittest discover -s m1/ledger_tests -v
python3 -m unittest discover -s m1/props/tests -v
# properties (memory and SQLite) and mutation smoke
python3 m1/props/run.py --profile ci --seed 20261107 --rust $R --go $G --out /tmp/p.json
python3 m1/props/run.py --campaign broker --ledger sqlite --seed 20261107 --cases 300 --repeats 2 --rust $R --go $G --out /tmp/ps.json
python3 m1/props/mutants/smoke.py --real $R --seed 7 --cases 300 --out /tmp/sr.json; python3 m1/props/mutants/smoke.py --real $G --seed 7 --cases 300 --out /tmp/sg.json
# harness and other checks
WRIT_BENCH_RUST=$R WRIT_BENCH_GO=$G python3 -m unittest discover -s m1/bench/tests
python3 m1/bench/bench.py --dry-run --seed 20261107 --rust $R --go $G --out /tmp/dry.json
(cd m0 && .venv/bin/python -m pytest -q); python3 m1/fixtures/export_m0_fixtures.py --check; python3 -m unittest discover -s m1/driver/tests; python3 scripts/check-v2-docs.py
# environment (read only)
cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor | sort | uniq -c; cat /sys/class/power_supply/ACAD/online /sys/class/power_supply/BAT1/status /proc/loadavg
```

### I. Requires owner approval

Every push (fast-forward only, never force); deleting files; changing frozen fixture expectations; any new decision or change to D-017..D-027; benchmark or build-time measurement runs outside the bounded M1-08 directive (guard must always pass); CPU or system settings; stopping the owner's processes; choosing Rust or Go; marking ADR-0002 or ADR-0008 ACCEPTED; anything touching `main`, V1 trees, the M0 harness or `hook-audit/`.

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
