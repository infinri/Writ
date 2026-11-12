# writ-kernel fixture map (M2-02 to M2-05)

Which M1 parity fixtures the production kernel serves, how they are run, and which remain obligations of later milestones. Expectations in `m1/fixtures/` and `docs/contracts/fixtures/` are frozen inputs: the kernel tests read them and never write them.

**Run:** `source m1/tools/env.sh && cargo test --workspace --locked --test m1_fixtures` (per-set counts: add `-- --nocapture`). The test drives the pure command layer (`writ_kernel::commands::run`) with the comparison rules of `m1/driver/parity.py`: `input` is passed as its raw JSON text, `input_text` as the decoded string's bytes; `exact`, `fields` and `one_of` compare JSON values (member order insignificant); every case is run twice and must give byte-identical output.

## Kernel sets: 8 sets, 320 cases

| Set | Command | Cases | Kernel entry point | Task |
|---|---|---|---|---|
| canonical-vector | `canonical` | 1 | `json::parse`, `json::canonical`, `json::digest` | M2-02 |
| strict-json | `canonical` | 37 | as above | M2-02 |
| golden-decisions | `decide` | 26 | `policy::load_bundle`, `decision::decide` | M2-03, M2-04 |
| transition-decisions | `decide` | 62 | `decision::decide`, `transition::check_transition` | M2-04, M2-05 |
| protocol-decide | `decide` | 33 | `commands::decide` (D-021, D-022 three-member refusals) | M2-04 |
| acceptance-binding | `check-acceptance` | 97 | `acceptance::check_values` | M2-05 |
| protocol-acceptance | `check-acceptance` | 1 | as above | M2-05 |
| external-refusal | `external-refusal` | 63 | `refusal::external_code` | M2-03 |

Also run as contract tests (not counted above): `docs/contracts/fixtures/policy/m0-reference.bundle.json` loads, does not declare transition support (D-020) and has the digest of the bundle embedded in the golden set; the nine `m1/fixtures/transition/*.bundle.json` documents load or are rejected as their fixture descriptions state.

## M3 obligations: 4 sets, 127 cases (not served by the kernel)

| Set | Command | Cases | Why not here |
|---|---|---|---|
| broker-scenarios | `broker-scenario` | 46 | broker, ingress seals, permits, grants, ledger (M3) |
| protocol-broker | `broker-scenario` | 9 | as above |
| transition-scenarios | `transition-scenario` | 66 | simulated promotion ledger and effects (M3 transition service) |
| protocol-transition | `transition-scenario` | 6 | as above |

`commands::run` returns `None` for these commands, and `manifest_partition_is_explicit` checks that the manifest holds exactly these 8 plus 4 sets with these sizes. The kernel does not claim the 447-case suite. The JSON Lines envelope (D-021 items 2 and 8) is also not part of the kernel; it belongs to the process protocol.

## Property-style tests (`kernel/tests/properties.rs`, fixed seed 20261112 + n)

Canonical round trip and invariance under member order, whitespace and equivalent escapes (3,000 values); single injected defects (number spellings and range, `NaN`/`Infinity`, lone surrogates, depth, duplicate and escape-equivalent duplicate names) rejected with their code (3,000 documents); decide and check-acceptance outputs byte-identical under input permutations (every parseable fixture input, 10 permutations each); a matching forbid always wins, also against broad permits and approvals (12 variants per fixture case that reached statements); statement order never changes status, reason or matched rules; requests never reach another tenant's or project's resource (1,000 cases); approval ids bind `to_state` and the bundle digest (1,000 cases); any single bound-field mutation of a resealed record, of current state or of an unsealed record refuses with the specified code (3,000 cases); malformed acceptance inputs refuse in trust order (1,500 cases). These are deterministic generators, not shrinking property tests; the M1 generators (`m1/props`) and the differential harness are M2-06.

## Semantic differences and readings

No expected value was changed. Differences from the reference implementations, none of which any fixture compares:

| Topic | M2 kernel | M1 Rust | M0 |
|---|---|---|---|
| `decision_id` for a request without a usable `request_id` | binds `unknown` (the reported `request_id`) | same | binds the Python `str()` of the raw value (`None`) |
| `expires_at` with a fractional or offset `deadline_at` | UTC `Z`, fraction without trailing zeros | same | Python `isoformat` (microseconds, offset kept) |
| Record whose digest cannot be computed | `ACCEPTANCE_RECORD_MALFORMED` | `ACCEPTANCE_RECORD_INVALID` | n/a |

The last row is unreachable from strictly parsed input (every parsed value is inside the D-006 domain); it only fixes the typed constructor's failure mode.

M0 differences, verified against the live M0 reference by `kernel/tools/m0_compare.py` (26 golden cases identical in every Decision field; targeted probes show each difference is limited to the field listed):

| Probe | Differing fields |
|---|---|
| `request_id` missing or `null` | `decision_id` only (row 1 above) |
| `deadline_at` with a fraction or an offset inside the expiry window | `expires_at` only (row 2 above) |
| `now` with a fraction or an offset | kernel `REQUEST_SCHEMA_INVALID` (D-022 strict `now`, approved); M0 predates D-022 and decides |

Readings approved by the owner as **D-031** (2026-11-12; tests in `kernel/tests/readings.rs`):

1. `current.revoked_evaluator_ids` entries must be valid evaluator names (`^[a-z][a-z0-9_-]{0,63}$`); any malformed entry or a non-array value is `CURRENT_STATE_MALFORMED`.
2. `external-refusal`: strict-parse failure keeps `{"input_error": "INPUT_JSON_INVALID", "json_error": <code>}`; a missing, non-string or unknown `internal_code` maps to `PROMOTION_REFUSED`.
3. Extra members are ignored only at the top level of the `decide` and `check-acceptance` command wrappers; ignored values must still be strict JSON; closed envelopes and nested contracts stay strict.

**Protection reachability:** `PROTECTION_UNAVAILABLE` is reachable through the public `transition::check_transition` API (no classification) and tested there; it is unreachable through validated command inputs (a transition-covering bundle without `workflow_protection` is rejected at load time; tested).

## Differential harness (M2-06)

`kernel/examples/m1_line_adapter.rs` (development-only Cargo example, never shipped) wraps the pure command layer in the M1 line protocol so the M1 parity driver and property generators drive `writ-kernel` unchanged. `kernel/tools/differential.py` runs the M1 campaigns `json`, `decide`, `acceptance` and `refusal` (CI profile) with the kernel against an M1 prototype and fails on any unclassified mismatch or failed kernel invariant (negative tests: `kernel/tools/test_differential.py`).

The gate (hardened 2026-11-12) requires a fresh report for the requested seed with exactly the four campaigns and positive case counts, captures the runner's exit status, and reconciles **every** failed check, mismatch cluster and protocol problem with an exact documented exception; anything else fails. Negative tests: `kernel/tools/test_differential.py`. Documented exceptions (exact):

- **M3 commands:** the JSON campaign's cross-command checks on `transition-scenario` and `broker-scenario`: for the N generated cases the kernel side answers none (adapter usage exit 2, "N ids unanswered"), the reference passes its own `json.cross_command.transition-scenario` check for all N, and each command contributes exactly N whole-output mismatches (2N `differential.exact_output` failures).
- **Adapter limitation (json campaign, command `canonical`, case `envelope-deep_envelope`, count 1, kernel `{null: ENVELOPE_INVALID}` versus reference `{"e": DEPTH_EXCEEDED}`):** an object `input` member that is itself deeper than the limit makes the whole envelope line fail strict parsing in the adapter (`ENVELOPE_INVALID` with a null id), while the M1 prototypes re-parse the raw `input` bytes and answer `DEPTH_EXCEEDED`. This is protocol handling, not kernel behaviour: the same document passed as `input_text` gives identical kernel and M1 answers at depths 64, 65, 100 and 200.

M0 is not run in the generated differential; it is compared live on the golden cases and probes by `kernel/tools/m0_compare.py` (above). Full Decision fields: the `decide` campaign compares whole outputs (every Decision field) with M1 Rust and Go (0 mismatches), and `kernel/tools/decision_schema_check.py` validates every full Decision for the decide fixture inputs against `docs/contracts/decision.schema.json`.
