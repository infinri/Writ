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

Readings where the written specification is silent or generic, kept identical to both M1 prototypes and listed for owner confirmation:

1. `current.revoked_evaluator_ids` items must match the evaluator id pattern `^[a-z][a-z0-9_-]{0,63}$`. SEMANTICS.md says "array of ids"; Rust and Go both use the evaluator (name) pattern. Stricter reading; a non-matching entry fails closed as `CURRENT_STATE_MALFORMED`.
2. `external-refusal` input that fails strict parsing returns `{"input_error": "INPUT_JSON_INVALID", "json_error": <code>}`; a missing or non-string `internal_code` maps to `PROMOTION_REFUSED`. The parity suite defines only `{"internal_code"}` to `{"external"}`.
3. Extra members of the `decide` and `check-acceptance` command inputs are ignored (the inputs are harness objects, not closed contracts; D-021 closes only the line envelope).
