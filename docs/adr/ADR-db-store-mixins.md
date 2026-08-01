# ADR: Neo4jConnection composed from store mixins

Status: accepted (Wave 2 hygiene, 2026-07-08)

## Context
`writ/graph/db.py` was a 1578-line god-file: one `Neo4jConnection` class with 60
methods spanning rules, nodes, edges, abstractions, schema, project registry,
decision-memory records, and maintenance. It holds only `self._driver` +
`self._database`; every method opens its own session. The hygiene program targets
splitting it. The seam is load-bearing: `writ.graph.db.Neo4jConnection` is
monkeypatched by 2 tests, `_driver`/`_database` are read by external modules, all
60 method names are duck-typed by fake-DB test doubles, and 11 module-level names
are imported from `writ.graph.db`.

## Decision
Convert `db.py` into a `writ/graph/db/` package. `Neo4jConnection` is composed from
eight per-domain store MIXINS (`RuleStoreMixin`, `NodeStoreMixin`, `EdgeStoreMixin`,
`AbstractionStoreMixin`, `SchemaStoreMixin`, `ProjectStoreMixin`, `RecordStoreMixin`,
`MaintenanceStoreMixin`), each holding its cluster's methods MOVED VERBATIM. The
facade `__init__.py` keeps the class name, `__init__`/`close`, and re-exports the
module-level surface from `_common.py` (the shared helpers/constants hub).

## Alternatives considered
- Composition/delegation (stores as injected objects, methods delegate). Rejected:
  it rewrites every method body into `return await self._store.x(...)`, which (a)
  breaks `tests/test_phaseM2b_data_integrity.py` (it asserts the token `_id_or_match`
  appears in `inspect.getsource(batch_create_edges)`), (b) risks perturbing the
  byte-identical concurrency keystones (`create_project`, `project_name_unique`),
  and (c) adds 60 wrapper methods of churn for no behavioral gain.
- Keep one file. Rejected: the god-file is the thing the program exists to remove.

## Consequences
- Behavior is preserved by construction (methods move, they are not rewritten), so
  the live-Neo4j suite (incl. the real-race `TestCreateProjectConcurrency`) and the
  getsource test pass unchanged.
- Inheritance depth stays 1 level (`Neo4jConnection` <- store mixins; mixins do not
  inherit each other), within ARCH-COMP-001's 2-level limit. This is composition of
  behavior via mixins, not a deep hierarchy.
- Novel for this repo (no prior mixin/store pattern; the earlier `server.py` split
  was a function/route split). `_common.py` is a single import hub: the stores
  import shared helpers/constants from it, so cross-module names route through one
  place.
- Dead code (`get_all_edges`) moved as-is at split time; the W4 sweep later removed it (`tests/test_w4_db_dead_methods.py` pins the absence).
