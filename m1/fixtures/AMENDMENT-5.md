# M1-02 amendment 5: protocol semantics (D-021)

**Date:** 2026-11-07. **Basis:** owner decision [D-021](../../docs/implementation/DECISION-LOG.md). All cases remain **PENDING INDEPENDENT REVIEW**.

- **Changed expected results:** none.
- **Added:** 16 cases in four new per-command sets built from `protocol/protocol-semantics.cases.json`: `protocol-decide` 3 (exact three-member failed-parse output), `protocol-acceptance` 1 (member order), `protocol-transition` 6 (missing steps, missing state field, count 0, unknown fault, unknown op, member order), `protocol-broker` 6 (unknown op, grant label reuse never replenishes, permit never issued, undeclared dependency in `set_dependency` and in `initial`, unknown fault). Envelope rules are checked by `m1/props` (invariant `json.d021_envelope_correlation`), since the driver only sends valid envelopes.
- **Totals:** 12 sets, **414 cases**.
- **Recorded conflict:** the failed-parse response is a protocol refusal, not a `decision.schema.json` instance (D-021).
- **Open reading (not settled by D-021):** a declared dependency absent from `initial.dependencies` starts `up` in both prototypes; the Rust agent chose this explicitly, the Go behavior predates D-021. Agreement only; owner decision pending.
