# Writ V2 M1 Rust prototype (`proto`, M1-03)

Bake-off prototype for ADR-0002, not the production kernel. It implements the JSON Lines protocol in `docs/implementation/M1-PARITY-SUITE.md` ("Prototype interface"). It was written from the M1 fixture specifications (`m1/fixtures/acceptance/SEMANTICS.md`, `m1/fixtures/transition/SCENARIOS.md`, `m1/fixtures/broker/BROKER.md`, AMENDMENT-1..4, DECISION-LOG D-006, D-008..D-012, D-017..D-020, `docs/contracts/*.schema.json`). Only `m0/writ_m0/decision.py` and `canonical.py` were consulted, as a reference for D-008 and D-006.

## Build and run

```sh
source m1/tools/env.sh                      # pinned Rust 1.99.1 (user space)
cd m1/rust && cargo build --release --locked
cargo test --release --locked               # parser, canonical form, time, envelope, D-020, M1-05 and D-021 regression tests
cd ../.. && python3 m1/driver/parity.py --proto "$PWD/m1/rust/target/release/proto" \
    --manifest m1/fixtures/manifest.json --repeat 2 --out /tmp/rust-result.json
```

Commands: `proto canonical | decide | check-acceptance | external-refusal | transition-scenario | broker-scenario`. `proto broker-scenario --ledger sqlite --ledger-dir DIR` uses a new SQLite file in `DIR` per scenario line, `--ledger-db PATH` one file for the whole process (created if absent, never reset); other commands reject these flags (exit 2). `proto sqlite-info` (no stdin) prints the engine version, source id, compile options and connection settings read back from a new temporary database. stdin carries one `{"id", "input"}` or `{"id", "input_text"}` per line; stdout carries one `{"id", "output"}` per line, in input order. Whitespace-only lines are ignored. The envelope is parsed with the strict rules and must be an object with a string `id` and exactly one of `input` or `input_text` (a string), with no other member (D-021, closed structure). A strictly parsed line with exactly one string `id` that breaks this is answered `{"id": "<id>", "output": {"error": "ENVELOPE_INVALID"}}`. Any other line (not strict JSON, including a strict failure inside `input` other than depth; root not an object; `id` missing, repeated or not a string) is answered `{"id": null, "output": {"error": "ENVELOPE_INVALID"}}`: no id is recovered by a non-strict scan or fabricated. No command runs for an invalid envelope. Every non-blank line is answered, processing continues, and the exit status is 0 when stdin was read to the end (1 only on a read or write failure).

## Dependencies

| Crate | Version | Use |
|---|---|---|
| `sha2` (RustCrypto) | =0.10.8 | SHA-256 digests (D-006) |
| `hmac` (RustCrypto) | =0.12.1 | simulated ingress seals and permit MACs |
| `rusqlite` | =0.40.2, `default-features = false` | SQLite broker ledger (D-023); links the pinned static SQLite 3.54.1 from `m1/tools/install-sqlite.sh` through `libsqlite3-sys` 0.38.2 (`SQLITE3_LIB_DIR`, `SQLITE3_INCLUDE_DIR`, `SQLITE3_STATIC=1` from `m1/tools/env.sh`); no bundled copy |

Their transitive dependencies are pinned in `Cargo.lock`: `digest` 0.10.7, `block-buffer` 0.10.4, `crypto-common` 0.1.7, `generic-array` 0.14.7, `typenum` 1.20.1, `version_check` 0.9.5, `subtle` 2.6.1, `cfg-if` 1.0.5 and `cpufeatures` 0.2.17. `rusqlite` adds `bitflags` 2.13.2, `fallible-iterator` 0.3.0, `fallible-streaming-iterator` 0.1.9, `smallvec` 1.16.5 and `libsqlite3-sys` 0.38.2 (build-time `pkg-config` 0.3.34 and `vcpkg` 0.2.15). There is no serde. The JSON parser and serializer are our own code.

## Architecture

| Module | Role | Purity |
|---|---|---|
| `json.rs` | Strict parser (D-017/D-018) and D-006 canonical serializer and digest | pure |
| `time.rs` | Strict `YYYY-MM-DDTHH:MM:SSZ` and RFC 3339 parsing; instants come only from input | pure, never reads a clock |
| `ids.rs` | Identifier and code patterns, matched byte for byte | pure |
| `kernel.rs` | Contract schema checks (ActionRequest, PolicyBundle), bundle load (fail closed, including bypassable protected edges), `decide` | pure: no I/O, no clock, no keys, no model SDK |
| `acceptance.rs` | `check_acceptance(record, current, requirements)` in the SEMANTICS order | pure |
| `refusal.rs` | Internal-to-external refusal mapping (D-018 A7) | pure |
| `promotion.rs` | Broker side of promotion and governed effects (`transition-scenario`) | stateful, simulated ledger |
| `broker.rs` | Sealed ingress, use-time re-decision, grants, receipts, permits, adapter (`broker-scenario`); in-memory ledger | stateful, simulated keys |
| `sqlite.rs` | SQLite ledger (`m1/fixtures/broker/LEDGER-SQLITE.md`): connection settings, embedded `ledger.sql`, one function per logical transaction, `sqlite-info` | file I/O |
| `main.rs` | Protocol envelope and command dispatch | I/O only here |

The kernel and the acceptance check are separate. The broker modules call `kernel::decide` and `acceptance::check` as pure functions and never trust a decision or protection flag supplied by the client. The kernel is key-free (D-010). Seals and permits are verified only in `broker.rs`.

### Input path

Every command parses its input with `json::parse`. For an `input` member, the envelope parser records the raw bytes of that member without interpreting them and then parses them as an independent document. Depth is counted from the input root for both `input` and `input_text`. When several problems are present, the first one met reading left to right is reported (for example, a depth violation is reported when the parser reaches the too-deep value). A strict failure inside `input_text` is the command's own error (`INPUT_JSON_INVALID`, `REQUEST_JSON_INVALID`, ...). Inside `input` the envelope line itself is not strict JSON, so any strict failure there is `ENVELOPE_INVALID` with a `null` id (D-021), except a depth violation, which stays the command's `DEPTH_EXCEEDED` because D-021 measures depth with `input` as the root.

### JSON equality

`Value` implements `PartialEq` by hand: objects compare as unordered maps (RFC 8259, D-006), arrays stay ordered. Every `==` on JSON values (acceptance `same()` over transition objects and policy fields, record attributes against request attributes, grant bindings, uniqueness checks in schema validation, replay fingerprints) therefore ignores member order. Fingerprints and MACs are computed over the D-006 canonical form, which sorts members.

### Concurrency

`concurrent` and `promote_concurrent` (transition scenarios), and `concurrent` (broker scenarios), run each operation on its own OS thread (`std::thread::scope`). All threads are released together by a `Barrier`. With the in-memory ledger each operation holds the configuration lock exclusively for its whole run, so every outcome equals some serial order. With SQLite each operation of a group opens its own connection; operations on one (tenant, idempotency key) are serialized by an in-process key lock, and different keys contend only in SQLite (`BEGIN IMMEDIATE`, `busy_timeout` 5000 ms). Operations that change trusted configuration or restart the broker run alone. Results come back in submission order. When several serial orders are valid, the observed result can vary between runs. `sc-64` is such a case: both outputs it allows were observed.

## Simulated, not verified

- **Signatures:** ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in scenario state. There is no key management, rotation or adapter registration.
- **Storage and crashes:** the default ledger is an in-memory model; `--ledger sqlite` is a real file-backed SQLite ledger (WAL, `synchronous=FULL`), tested for atomicity, restart, a second process and contention in `src/broker.rs` and `tests/ledger_persistence.rs`. `fault` injects a crash at a named point (nothing is killed); power-loss durability is not tested. The SQLite effect adapter (consumed permits, effect count) is still simulated in the same file. Storage failures follow D-024 (LEDGER-SQLITE.md "Storage failures"): before any step the ledger is opened, configured and initialized in one `BEGIN IMMEDIATE` transaction (schema created in an empty database, otherwise `meta.schema_version = '1'` and every ledger.sql table verified); any failure, including a read-only connection, answers the whole scenario `{"error": "LEDGER_UNAVAILABLE"}` without modifying the file. Diagnostics go to stderr only. Tested in `src/broker.rs` and `m1/ledger_tests`.
- **Humans:** an "authenticated human" is a credential match on a fixture channel.
- **Effects:** "dispatch" increments a counter or records a state. No external side effect occurs.
- **Acceptance records:** `record_digest` is an unkeyed consistency check. Trust labels are claims, not evaluator identity (boundary 2 is not covered).

## D-020 behavior

- **Transition support.** A bundle declares support when any statement covers `workflow.transition` or it carries `workflow_protection`. A declaring bundle without a schema-valid `workflow_protection` is rejected at load (`unavailable` / `POLICY_BUNDLE_REJECTED`). A transition request against a loaded bundle that lists `workflow.transition` as known but declares no support is `deny` / `UNSUPPORTED_CAPABILITY`, checked after scope and before resource shape; if the capability is not known, `UNKNOWN_CAPABILITY` applies as before. Other capabilities of such a bundle are unaffected. `PROTECTION_UNAVAILABLE` in `decide` is now unreachable (fail-closed guard only).
- **External refusal.** The pass-through list is exactly the one in `m1/fixtures/acceptance/SEMANTICS.md` (12 codes, including `REQUEST_SCHEMA_INVALID` and `DEADLINE_EXCEEDED`). `REVISION_CONFLICT`, `STATE_REVISION_CONFLICT` and unknown codes map to `PROMOTION_REFUSED`.
- **`restart_broker`** (broker scenarios) returns `{"result": "restarted"}`. Grants and uses, receipts and status, consumed permit ids and the permit id counter persist; issued permits stay presentable. The model holds no other volatile state (locks and in-flight values exist only for the duration of one operation), so the restart drops nothing from the broker struct; an exhaustive pattern in `Broker::restart` makes any new field require an explicit classification.

## Choices where the documents are silent

- In promotion, the permit's `record_digest` is compared with the presented record's own `record_digest` member. The recomputed digest is then checked by `check_acceptance`.
- The broker issues each permit an opaque id (`permit-<n>`) inside its MAC-covered body. A tampered expiry therefore fails the MAC check.

## M1-05 protocol clarifications

Implemented from `docs/implementation/M1-PARITY-SUITE.md`, "Protocol clarifications (M1-05 findings ...)":

- **Scenario input.** `transition-scenario` and `broker-scenario` return `{"input_error": "INPUT_MALFORMED"}` for the whole scenario when the input is not structurally usable: `steps` missing or not an array; `initial` missing, not an object or missing a required state field (transition: every field in the SCENARIOS.md State list except `protection`, whose absence fails closed as `PROTECTION_UNAVAILABLE`; broker: every field in the BROKER.md State list); a step that is not an object; a `fault` that is not one of that command's named faults (transition: `crash_before_commit`, `crash_before_reserve`, `crash_after_dispatch`; broker: `crash_before_reserve`, `crash_after_reserve`, `crash_after_dispatch`, `effect_fails`); a dependency status (in `initial.dependencies` or `set_dependency`) other than `up` or `down`; `promote_concurrent` with `count` not a positive integer. Our readings where the text is silent: the operations of a `concurrent` group are steps (so `operations` must be an array of usable steps), the `promote` template of `promote_concurrent` must be an object, a known fault on an operation it does not apply to is not malformed (it has no effect), and only presence of required state fields is checked, not their types.
- **Unknown operation** is refused `OPERATION_UNKNOWN` (was `UNKNOWN_OPERATION`) in both scenario commands and counted as a refusal.
- **`approve`** with a `grant_ref` already naming a grant is refused `GRANT_REF_IN_USE` and the existing grant is unchanged (a label never resets a grant's uses). The check runs after channel authentication, approvability and self-approval, just before the grant would be issued.
- **`present_permit`** naming a step that issued no permit (including a non-integer, negative or out-of-range `of_step`) is `PERMIT_MISSING`.

## D-021 protocol semantics

Conformance to `docs/implementation/M1-PARITY-SUITE.md`, "Protocol semantics (D-021)", and DECISION-LOG D-021. Changes from the M1-05 behavior above:

- **Correlation.** The id is kept only from a strictly parsed line with exactly one string `id`; every other invalid line is answered with `"id": null` (previously a best-effort scan recovered an id, and lines without one were left unanswered with exit status 1). The scan (`json::recover_string_member`) is removed.
- **Broker dependencies.** The declared dependencies are exactly `policy` and `ledger`. Any other name as a key of `initial.dependencies`, or a `set_dependency` (including inside `concurrent`) whose `name` is missing or not one of them, makes the whole scenario `{"input_error": "INPUT_MALFORMED"}` before any step runs. Absence of a declared dependency in `initial.dependencies` is not malformed; it starts `up` (our reading: D-021 restricts names, it does not require both).
- **Unchanged and checked against D-021:** the decide strict-parse failure has exactly `status`, `reason_code` and `matched_rule_ids`; `OPERATION_UNKNOWN`, `GRANT_REF_IN_USE` (the existing grant is never reset), `PERMIT_MISSING` and structural `INPUT_MALFORMED` are as described under M1-05; JSON equality ignores member order. Transition scenarios have no dependency model, so the dependency-name rule applies to broker scenarios only.
