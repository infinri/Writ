# M1-04: Go prototype (`proto`)

The Go side of the M1 language bake-off (ADR-0002). It implements the JSON Lines protocol in [M1-PARITY-SUITE](../../docs/implementation/M1-PARITY-SUITE.md) ("Prototype interface") for `canonical`, `decide` (and `decide-batch`, same handler), `check-acceptance`, `external-refusal`, `transition-scenario` and `broker-scenario`. It is a prototype, not the production kernel. It was written from the M1 fixture specifications (acceptance SEMANTICS, transition SCENARIOS, BROKER, AMENDMENT-1..4, D-006, D-008..D-012, D-017..D-020, the contract schemas). `m0/writ_m0/decision.py` and `canonical.py` were consulted only for the D-008 evaluation order and output fields.

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
| `internal/refusal` | Internal code to agent-facing code mapping (D-018 A7, D-020). | none |
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

## D-020 behavior

- **Transition support.** A bundle declares support when any statement covers `workflow.transition` or it carries `workflow_protection` (`Bundle.TransitionSupported`). Declared support without a schema-valid `workflow_protection` rejects the bundle at load (`unavailable` / `POLICY_BUNDLE_REJECTED`). A transition request against a bundle that lists `workflow.transition` as known but declares no support is `deny` / `UNSUPPORTED_CAPABILITY`, checked after scope and before resource shape; not known stays `UNKNOWN_CAPABILITY`. Other capabilities of such a bundle are unaffected. Valid support and protection with no authorizing statement is `DEFAULT_DENY`.
- **External refusal.** The pass-through list is exactly the one in acceptance SEMANTICS.md ("Internal codes versus external responses"), now including `REQUEST_SCHEMA_INVALID` and `DEADLINE_EXCEEDED`. `REVISION_CONFLICT` and `STATE_REVISION_CONFLICT` map to `PROMOTION_REFUSED`, as do unknown codes.
- **`restart_broker`.** Drops the loaded policy cache and reloads it from the active bundle document. The ledger persists: grants and their uses, receipts and their status, consumed permit ids, the permit sequence (a new permit never reuses a consumed id) and the audited refusal count. Permits already handed to the client remain presentable. Keys, channels, dependency status and the injected clock are trusted configuration outside the process and are unchanged. Result `{"result": "restarted"}`. In-memory simulation only.

Regression tests: `internal/kernel/support_test.go`, `internal/refusal/refusal_test.go`, `internal/broker/restart_test.go`.

## M1-05 fixes and protocol clarifications

Defects found by the M1-05 property harness (`m1/props/`) and the protocol clarifications in M1-PARITY-SUITE ("Protocol clarifications (M1-05 findings ...)"):

- **Numbers.** A leading zero followed by a digit (`01`, `-00`, `-01`) is `INVALID_JSON` in every command; only `-0` itself is `NUMBER_NEGATIVE_ZERO`.
- **No crash.** Each line runs behind a recover boundary (`cmd/proto`, answered `{"error": "INTERNAL_ERROR"}` and logged to stderr); panics in `concurrent`/`promote_concurrent` worker goroutines are carried back to it. Inputs are validated first, so `promote_concurrent` with `count` -1 (previously a panic, exit 2) is `INPUT_MALFORMED`.
- **Envelope.** Each non-blank line is parsed strictly (`NaN` anywhere is invalid); whitespace-only lines are ignored. A valid envelope is an object with a string `id` and exactly one of `input` or `input_text` (a string). Otherwise, if the `id` is readable (`sjson.ReadID`: exactly one top-level `id` member, decoded strictly, whose value is a well-formed string; other values skipped tolerantly) the line is answered `{"error": "ENVELOPE_INVALID"}`; if not, it is skipped and the process exits 1 after answering every other line.
- **Scenarios.** `INPUT_MALFORMED` for the whole scenario, checked before anything runs: missing or non-array `steps`; missing or non-object `initial`; a missing or mistyped required state field (transition: the identity strings, `state`, `revision` (integer >= 0), snapshot and digest, acceptance policy id, version and bundle sha, `acceptance_requirements` present, `authorization_bundle_sha`, `now`; optional `consumed_record_ids`/`revoked_evaluator_ids` string arrays, `effect_prerequisites` object, `last_promotion` object or null, `protection` free since absent protection is `PROTECTION_UNAVAILABLE`; broker: valid `now`, `state_revision` (integer >= 0), `bundle` present, `permit_key` string, `ingress_keys` and `channels` objects, optional `dependencies` with statuses `up`/`down`); `count` not a positive integer; a non-object `promote` template; an unknown fault or dependency status; a step or nested `concurrent` operation that is not an object. An unknown `op` is refused `OPERATION_UNKNOWN`; `approve` with a `grant_ref` already in use is refused `GRANT_REF_IN_USE` (uses never reset); `present_permit` naming a step that issued no permit is `PERMIT_MISSING`.

Regression tests: `internal/sjson/sjson_test.go` (numbers, `ReadID`), `cmd/proto/main_test.go` (envelope, recover boundary, negative count), `internal/transition/validate_test.go`, `internal/broker/validate_test.go`.

## Concurrency

`concurrent` and `promote_concurrent` run each operation on its own goroutine; operations take the scenario mutex for their whole duration, so the outcome equals some serialization. Where the spec allows several serializations (`sc-64`, compare mode `one_of`), either may occur from run to run.

## Choices where the documents are silent

- Envelope lines are parsed with the same strict parser without a depth limit; the payload's depth is then checked from its own root, so `input` and `input_text` are equivalent.
- An envelope with members other than `id` and one of `input`/`input_text` is `ENVELOPE_INVALID` (read of "exactly one of" in the protocol clarifications).
- `promote_concurrent` `count` above 65536 is `INPUT_MALFORMED` (implementation limit, so a huge count cannot exhaust memory).
- `transition-scenario`: a `fault` on `promote` or `effect` must be a name from the SCENARIOS.md vocabulary (`crash_before_commit`, `crash_before_reserve`, `crash_after_dispatch`); a known name at a point the operation does not have (for example `crash_after_dispatch` on `promote`) never fires. `broker-scenario`: an `execute` fault must be one of the four BROKER.md names. A `fault` on any other operation is ignored.
- `set_dependency` (and `initial.dependencies`) check only the status (`up`/`down`); an unknown dependency name is accepted and has no effect.
- `approve` checks `GRANT_REF_IN_USE` last, after `HUMAN_UNAUTHENTICATED`, `NOT_APPROVABLE` and `SELF_APPROVAL_FORBIDDEN`, at the point the grant would be issued. `present_permit` with a missing or non-integer `of_step` is `PERMIT_MISSING`.
- `decide` with a missing or malformed `now` or `state_revision` returns `invalid` / `REQUEST_SCHEMA_INVALID`.
- Strict-parse failures of `broker-scenario` input return `{"input_error": "INPUT_JSON_INVALID", "json_error": ...}` as for `transition-scenario`.
- Permit and grant expiry are exclusive (`now` at or after expiry refuses), like record expiry.
- The broker permit MAC covers `{permit_id, request_fingerprint, policy_bundle_sha, expires_at}` in canonical form; `tamper: "mac"` flips one hex digit, `tamper: "expiry"` adds one hour without re-signing.
- For transitions the approval request id also binds `from_state` and `to_state` (SCENARIOS.md "Approval binding").
