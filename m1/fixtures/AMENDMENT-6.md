# M1-02 amendment 6: decide input validation (D-022)

**Date:** 2026-11-07. **Basis:** owner decision [D-022](../../docs/implementation/DECISION-LOG.md). All cases remain **PENDING INDEPENDENT REVIEW**.

- **Changed expected results:** none.
- **Added:** 30 cases `p22-01` to `p22-30` in `protocol-decide` (built from `transition-decisions` `tx-01-accept-permitted` by structural edits, from raw text edits, or as literal inputs): malformed `now` (absent, number, null, empty, 30 February, 29 February in a non-leap year, leap second, hour 24, zero date, offset, fraction, lowercase, date only); `state_revision` -1, -(2^53-1), absent, string, null, boolean; `request` absent; input array and number: all exactly `invalid`/`REQUEST_SCHEMA_INVALID` with three members. `state_revision` 2^53, `7.0`, `7e0`, `07`, `-0`: exactly `invalid`/`REQUEST_JSON_INVALID` (D-017 strict parsing). Valid boundaries (leap day `2024-02-29T23:59:59Z`, revision 0, revision 2^53-1 with a matching expected revision) keep the base case's `allow`/`ACCEPT_TRANSITION_PERMITTED` (fields compare).
- **Builder:** `export_m0_fixtures.py` protocol builder gained `set_input`, `set_request`, `edit_text`, `input_literal` and per-case `compare_fields`.
- **Totals:** 12 sets, **444 cases**.
- **Property coverage:** `m1/props` decide campaign invariant `decide.d022_harness_fields_validated_before_decision` (mutated timestamps, wrong types, negative and boundary revisions, absent members).
- **Prototype results before the fix:** Rust failed 5 (offset, fraction, lowercase `now`; revisions -1 and -(2^53-1)); Go failed 22 (every malformed `now`, `state_revision` or `request` case returned a full Decision, several with `decided_at` `0001-01-01T00:00:00Z`). Both fixed; both 444/444.
