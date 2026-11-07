# Writ V2 M1 Rust prototype (`proto`, M1-03)

Bake-off prototype for ADR-0002, not the production kernel. It implements the JSON Lines protocol in `docs/implementation/M1-PARITY-SUITE.md` ("Prototype interface"). It was written from the M1 fixture specifications (`m1/fixtures/acceptance/SEMANTICS.md`, `m1/fixtures/transition/SCENARIOS.md`, `m1/fixtures/broker/BROKER.md`, AMENDMENT-1..3, DECISION-LOG D-006, D-008..D-012, D-017..D-019, `docs/contracts/*.schema.json`). Only `m0/writ_m0/decision.py` and `canonical.py` were consulted, as a reference for D-008 and D-006.

## Build and run

```sh
source m1/tools/env.sh                      # pinned Rust 1.99.1 (user space)
cd m1/rust && cargo build --release --locked
cargo test --release --locked               # parser, canonical form and time unit tests
cd ../.. && python3 m1/driver/parity.py --proto "$PWD/m1/rust/target/release/proto" \
    --manifest m1/fixtures/manifest.json --repeat 2 --out /tmp/rust-result.json
```

Commands: `proto canonical | decide | check-acceptance | external-refusal | transition-scenario | broker-scenario`. stdin carries one `{"id", "input"}` or `{"id", "input_text"}` per line; stdout carries one `{"id", "output"}` per line, in input order. The exit status is 0 when every line was answered. A line without a parseable envelope or id cannot be answered: it is reported on stderr and the exit status becomes 1.

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

Every command parses its input with `json::parse`. For an `input` member, the envelope parser records the raw bytes of that member without interpreting them and then parses them as an independent document. As a result, `input` and `input_text` follow identical strict rules, and depth is counted from the input root. When several problems are present, the first one met reading left to right is reported (for example, a depth violation is reported when the parser reaches the too-deep value).

### Concurrency

`concurrent` and `promote_concurrent` (transition scenarios), and `concurrent` (broker scenarios), run each operation on its own OS thread (`std::thread::scope`). All threads are released together by a `Barrier`. Each operation holds the single ledger `Mutex` for its whole transaction, so every outcome equals some serial order. Results come back in submission order. When several serial orders are valid, the observed result can vary between runs. `sc-64` is such a case: both outputs it allows were observed.

## Simulated, not verified

- **Signatures:** ingress seals and permit MACs are HMAC-SHA256 under fixture keys held in scenario state. There is no key management, rotation or adapter registration.
- **Storage and crashes:** the ledger is an in-memory model. `fault` injects a crash at a named point. There is no durability, crash recovery or multi-process locking.
- **Humans:** an "authenticated human" is a credential match on a fixture channel.
- **Effects:** "dispatch" increments a counter or records a state. No external side effect occurs.
- **Acceptance records:** `record_digest` is an unkeyed consistency check. Trust labels are claims, not evaluator identity (boundary 2 is not covered).

## Choices where the documents are silent

- A `workflow.transition` request under a bundle without `workflow_protection` returns `unavailable` / `PROTECTION_UNAVAILABLE`, which fails closed. Such a bundle loads only if no statement covers transitions.
- The external mapping also passes through `REQUEST_SCHEMA_INVALID`, `DEADLINE_EXCEEDED` and `STATE_REVISION_CONFLICT`, because they describe the caller's own input, deadline or expected revision. Every other unknown code maps to `PROMOTION_REFUSED`.
- `final.receipts` in broker scenarios counts receipts per status (`{"observed_success": 1}`), as every fixture expects. BROKER.md says `{key: status}`.
- In promotion, the permit's `record_digest` is compared with the presented record's own `record_digest` member. The recomputed digest is then checked by `check_acceptance`.
- The broker issues each permit an opaque id (`permit-<n>`) inside its MAC-covered body. A tampered expiry therefore fails the MAC check.
