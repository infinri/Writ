# M1-02 amendment 7: declared broker dependencies required (D-025 DEP-1)

**Date:** 2026-11-10. **Basis:** owner decision [D-025](../../docs/implementation/DECISION-LOG.md) DEP-1. All cases remain **PENDING INDEPENDENT REVIEW**.

- **Changed expected results:** none (every existing broker scenario lists both `ledger` and `policy`).
- **Added:** 3 cases in `protocol-broker` built from `b-01-allow-path` with `initial.dependencies` lacking `policy`, lacking `ledger`, or lacking both: each exactly `{"input_error": "INPUT_MALFORMED"}`. Rationale: an omitted dependency status is missing trusted configuration; defaulting it to available was fail-open.
- **Builder:** `initial_dependencies_drop`.
- **Totals:** 12 sets, **447 cases**.
- **Prototype results before the change:** Rust and Go each failed all 3 (both defaulted to `up`).
- **Language-neutral SQLite tests added with D-025** (not fixtures): stored-grant corruption (C-4), durable state unchanged by a resumed retry (A), no effect or consumed id for a refused permit (B), S-1 (rejected non-WAL file unchanged), S-2 (created file removed, pre-existing file kept), stderr free of paths and credentials.
