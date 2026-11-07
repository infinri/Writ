# M1-04: Go prototype (`proto`)

The Go side of the M1 language bake-off (ADR-0002). It implements the JSON Lines protocol in [M1-PARITY-SUITE](../../docs/implementation/M1-PARITY-SUITE.md) ("Prototype interface") for `canonical`, `decide` (and `decide-batch`, same handler), `check-acceptance`, `external-refusal`, `transition-scenario` and `broker-scenario`. It is a prototype, not the production kernel. It was written from the M1 fixture specifications (acceptance SEMANTICS, transition SCENARIOS, BROKER, AMENDMENT-1..3, D-006, D-008..D-012, D-017..D-019, the contract schemas). `m0/writ_m0/decision.py` and `canonical.py` were consulted only for the D-008 evaluation order and output fields.

## Build and run

Toolchain: Go 1.27.3 (pinned, user space, `GOTOOLCHAIN=local`). Standard library only; no third-party modules.

```sh
source m1/tools/env.sh
cd m1/go && go build -o proto ./cmd/proto && cd ../..
python3 m1/driver/parity.py --proto "$PWD/m1/go/proto" --manifest m1/fixtures/manifest.json --repeat 2
```

Checks: `cd m1/go && go vet ./... && go test -race -count=1 ./...`. The binary `m1/go/proto` is gitignored.

## Layout

| Package | Role | I/O |
|---|---|---|
| `internal/sjson` | Strict JSON parser (duplicate keys including escape-equivalent ones, integer-only numbers within ±(2^53-1), no `NaN`/`Infinity`, lone surrogates, D-006 traversal depth 64) and the D-006 canonical serializer and digest. `encoding/json` is not used anywhere. | none |
| `internal/kernel` | Pure decision function: hand-written ActionRequest and PolicyBundle schema validation, bundle load (including the M1 transition and protection rules and the bypassable-edge search), D-008 order, transition structure and protection, statement precedence. Time is an input; no clock reads, no I/O, no model SDK. | none |
| `internal/acceptance` | Pure `check_acceptance` in the normative order of SEMANTICS.md. | none |
| `internal/refusal` | Internal code to agent-facing code mapping (D-018 A7). | none |
| `internal/transition` | `transition-scenario` broker: promotion transaction, effects, reconciliation, concurrency. Calls `acceptance.Check`. | in-memory only |
| `internal/broker` | `broker-scenario` broker: sealed ingress, use-time `kernel.Decide`, approval grants, permits and adapter, idempotent receipts, faults, reconciliation. | in-memory only |
| `cmd/proto` | Envelope handling and command dispatch. | stdin, stdout |

Kernel, broker and acceptance are separate packages; the brokers depend on the kernel and acceptance checker, never the reverse.

## Simulated, not verified

- **Signatures:** ingress seals and permit MACs are HMAC-SHA256 under fixture keys taken from scenario state (hex-decoded). This simulates authenticated ingress (D-010) and permit integrity (SR-07); there is no key management, rotation or adapter registration.
- **Storage and crashes:** grants, receipts, permits, promotion receipts and the effect store are in-memory maps guarded by one mutex per scenario; each operation runs as one serializable transaction. `fault` values inject crashes at named points. Nothing is durable; real crash recovery and multi-process locking are not exercised.
- **Humans:** an authenticated human is a credential match on a fixture channel (simulates the approval service).
- **Effects:** dispatch increments a counter or updates the in-memory effect store; no real side effect occurs.
- **Acceptance records:** `record_digest` is an unkeyed consistency check; evaluator identity and record issuance are not verified (boundary 2).

## Concurrency

`concurrent` and `promote_concurrent` run each operation on its own goroutine; operations take the scenario mutex for their whole duration, so the outcome equals some serialization. Where the spec allows several serializations (`sc-64`, compare mode `one_of`), either may occur from run to run.

## Choices where the documents are silent

- Envelope lines are parsed with the same strict parser without a depth limit; the payload's depth is then checked from its own root, so `input` and `input_text` are equivalent.
- `decide` with a missing or malformed `now` or `state_revision` returns `invalid` / `REQUEST_SCHEMA_INVALID`. A `workflow.transition` request against a bundle that has no `workflow_protection` (only possible when no statement covers transitions) fails closed as `unavailable` / `POLICY_BUNDLE_REJECTED`.
- Strict-parse failures of `broker-scenario` input return `{"input_error": "INPUT_JSON_INVALID", "json_error": ...}` as for `transition-scenario`; structurally unusable scenario input returns `{"input_error": "INPUT_MALFORMED"}`. An unknown operation is a refusal `OPERATION_UNKNOWN`.
- Permit and grant expiry are exclusive (`now` at or after expiry refuses), like record expiry.
- The broker permit MAC covers `{permit_id, request_fingerprint, policy_bundle_sha, expires_at}` in canonical form; `tamper: "mac"` flips one hex digit, `tamper: "expiry"` adds one hour without re-signing.
- For transitions the approval request id also binds `from_state` and `to_state` (SCENARIOS.md "Approval binding").
