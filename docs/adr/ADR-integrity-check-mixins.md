# ADR: IntegrityChecker composed from check mixins

Status: accepted (Wave 2 hygiene, 2026-07-08)

## Context
`writ/graph/integrity.py` was a 1489-line god-file: one `IntegrityChecker` class
with 42 methods (40 checks/helpers + `__init__` + the `run_all_checks` orchestrator)
spanning structural, frequency, parity/reconcile, edge-contract, content-lint,
routing, and artifact-freshness domains. It holds only `self._driver`,
`self._database`, and `self._default_bible_dir` (the last set directly by
`writ/cli.py`). The hygiene program targets splitting it, immediately after the
`writ/graph/db.py` split (ADR-db-store-mixins, PR #38).

## Decision
Convert `integrity.py` into a `writ/graph/integrity/` package. `IntegrityChecker`
is composed from seven per-domain check MIXINS (`StructuralChecksMixin`,
`FrequencyChecksMixin`, `ParityChecksMixin`, `EdgeContractChecksMixin`,
`ContentChecksMixin`, `RoutingChecksMixin`, `ArtifactChecksMixin`), each holding
its cluster's check methods MOVED VERBATIM. The facade `__init__.py` keeps the
class name, `__init__`, and `run_all_checks` (both moved verbatim), and re-exports
the module-level surface from `_common.py` via `__all__`.

Same pattern and rationale as ADR-db-store-mixins: mixins move method bodies
verbatim (no rewrite), which preserves `run_all_checks`'s fuzz-verified exit-code
aggregation and the `PARITY_EXEMPT_PROVENANCE` set-membership across the four
parity methods byte-identical.

## Alternatives considered
- Composition/delegation. Rejected for the same reasons as the db split: it
  rewrites method bodies (endangering the fuzz-verified aggregation and the parity
  keystones) and adds 42 wrapper methods of churn.
- Keep one file. Rejected: it is the god-file the program removes.

## Consequences
- Behavior preserved by construction; the live-Neo4j integrity suite (including
  `test_phase6_parity_provenance`, `test_phase010_reconcile`) passes unchanged.
- Inheritance depth stays 1 level (ARCH-COMP-001).
- `_common.py` is a deliberate import hub: integrity's shared surface pulls from
  five lower-layer modules (writ.graph.db, .schema, .ingest, .methodology_ingest,
  .predicates) once, and the mixins/facade pull from `_common`. An `__all__` marks
  the re-exports so the module is ruff-clean. This hub is more justified here than
  in the db split (five source modules vs the db split's two).
- The then-failing `test_phase6_sequencing_guard::test_integrity_imports_the_provenance_axis`
  was left as-is by this split (behavior-preserving) and later fixed in `9fb481a`:
  the test now asserts on `PARITY_EXEMPT_PROVENANCE`, the wider set integrity
  actually re-exports, and passes.
- One path-coupled test (`test_fix3_metrics.py`, which `read_text()` the old single
  file) was made layout-agnostic, mirroring `_db_source()` / `writ_server_source()`.
