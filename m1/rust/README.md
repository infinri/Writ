# Writ V2 M1 Rust prototype (`proto`, M1-03)

Bake-off prototype for ADR-0002, not the production kernel. It implements the JSON Lines protocol in `docs/implementation/M1-PARITY-SUITE.md` ("Prototype interface"). It was written from the M1 fixture specifications (`m1/fixtures/acceptance/SEMANTICS.md`, `m1/fixtures/transition/SCENARIOS.md`, `m1/fixtures/broker/BROKER.md`, AMENDMENT-1..4, DECISION-LOG D-006, D-008..D-012, D-017..D-020, `docs/contracts/*.schema.json`). Only `m0/writ_m0/decision.py` and `canonical.py` were consulted, as a reference for D-008 and D-006.

## Build and run

```sh
source m1/tools/env.sh                      # pinned Rust 1.99.1 (user space)
cd m1/rust && cargo build --release --locked
cargo test --release --locked               # parser, canonical form, time, envelope, D-020 and M1-05 regression tests
cd ../.. && python3 m1/driver/parity.py --proto "$PWD/m1/rust/target/release/proto" \
    --manifest m1/fixtures/manifest.json --repeat 2 --out /tmp/rust-result.json
```

Commands: `proto canonical | decide | check-acceptance | external-refusal | transition-scenario | broker-scenario`. stdin carries one `{"id", "input"}` or `{"id", "input_text"}` per line; stdout carries one `{"id", "output"}` per line, in input order. Whitespace-only lines are ignored. The envelope is parsed with the strict rules and must be an object with a string `id` and exactly one of `input` or `input_text` (a string), with no other member. A line that violates this but has a readable string `id` (recovered by a best-effort scan; a repeated or non-string `id` is not readable) is answered `{"id", "output": {"error": "ENVELOPE_INVALID"}}`. A line with no readable id is reported on stderr and not answered; the other lines are still answered and the exit status becomes 1. Otherwise the exit status is 0.

## Dependencies

| Crate | Version | Use |
|---|---|---|
| `sha2` (RustCrypto) | =0.10.8 | SHA-256 digests (D-006) |
| `hmac` (RustCrypto) | =0.12.1 | simulated ingress seals and permit MACs |

Their transitive dependencies are pinned in `Cargo.lock`: `digest` 0.10.7, `block-buffer` 0.10.4, `crypto-common` 0.1.7, `generic-array` 0.14.7, `typenum` 1.20.1, `version_check` 0.9.5, `subtle` 2.6.1, `cfg-if` 1.0.5 and `cpufeatures` 0.2.17. There is no serde. The JSON parser and serializer are our own code.

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
| `broker.rs` | Sealed ingress, use-time re-decision, grants, receipts, permits, adapter (`broker-scenario`) | stateful, simulated ledger and keys |
| `main.rs` | Protocol envelope and command dispatch | I/O only here |

The kernel and the acceptance check are separate. The broker modules call `kernel::decide` and `acceptance::check` as pure functions and never trust a decision or protection flag supplied by the client. The kernel is key-free (D-010). Seals and permits are verified only in `broker.rs`.

### Input path

Every command parses its input with `json::parse`. For an `input` member, the envelope parser records the raw bytes of that member without interpreting them and then parses them as an independent document. Depth is counted from the input root for both `input` and `input_text`. When several problems are present, the first one met reading left to right is reported (for example, a depth violation is reported when the parser reaches the too-deep value). A strict failure inside `input_text` is the command's own error (`INPUT_JSON_INVALID`, `REQUEST_JSON_INVALID`, ...). Inside `input` the envelope line itself is not strict JSON, so any strict failure there is `ENVELOPE_INVALID`, except a depth violation, which stays the command's `DEPTH_EXCEEDED` so that `input` and `input_text` agree on deep documents (our reading; the clarification does not say how depth inside `input` is counted).

### JSON equality

`Value` implements `PartialEq` by hand: objects compare as unordered maps (RFC 8259, D-006), arrays stay ordered. Every `==` on JSON values (acceptance `same()` over transition objects and policy fields, record attributes against request attributes, grant bindings, uniqueness checks in schema validation, replay fingerprints) therefore ignores member order. Fingerprints and MACs are computed over the D-006 canonical form, which sorts members.

### Concurrency

`concurrent` and `promote_concurrent` (transition scenarios), and `concurrent` (broker scenarios), run each operation on its own OS thread (`std::thread::scope`). All threads are released together by a `Barrier`. Each operation holds the single ledger `Mutex` for its whole transaction, so every outcome equals some serial order. Results come back in submission order. When several serial orders are valid, the observed result can vary between runs. `sc-64` is such a case: both outputs it allows were observed.

## Simulated, not verified

- **Signatures:** ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in scenario state. There is no key management, rotation or adapter registration.
- **Storage and crashes:** the ledger is an in-memory model. `fault` injects a crash at a named point. There is no durability, crash recovery or multi-process locking.
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
