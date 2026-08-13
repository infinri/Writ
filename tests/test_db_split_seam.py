"""Seam guard for the Wave 2 db.py -> writ/graph/db/ package split (mixins).

The split moves Neo4jConnection's 60 methods verbatim onto per-domain store
mixins, but MUST preserve the import + monkeypatch seam callers depend on:
`writ.graph.db.Neo4jConnection` stays a plain patchable class, `_driver`/
`_database` stay instance attrs, all method names/signatures stay stable, and the
module-level names stay importable from `writ.graph.db`.

RED today: test_neo4jconnection_composed_from_store_mixins imports the new store
submodules, which do not exist until the split lands. The other tests are
regression guards (green before AND after) that catch a method or re-export lost
in the move.
"""
from __future__ import annotations

import asyncio

import pytest

# The full method surface of Neo4jConnection at HEAD 6206af2 (introspected from the
# live class). Every name must still resolve on the post-split class.
EXPECTED_METHODS = [
    "__init__", "_create_record", "_parse_planned_files", "_record_endpoint_clause", "_run",
    "_run_single", "apply_constraints",
    "batch_create_edges", "batch_create_nodes", "clear_all", "clear_project", "close",
    "count_by_authority", "count_rules", "create_abstraction", "create_abstracts_edge",
    "create_commit", "create_decision", "create_edge",
    "create_filechange", "create_memory", "create_methodology_node", "create_project",
    "create_record_edge",
    "create_rule", "delete_abstractions", "delete_rule", "evaluate_and_flip_graduation",
    "get_abstraction", "get_all_abstractions", "get_all_edges_cross_type",
    # Added after the split by later programs and never folded into this frozen
    # list, so it sat permanently RED: `execute` and `get_all_nodes_for_dump` from
    # the Cypher graph-dump feature, `get_category_routes_by_node` from Phase-0
    # category routing. The list stays exhaustive on purpose -- an unexpected EXTRA
    # method must fail here too -- so adding one is a deliberate act that edits this.
    # `execute_many` (cycle 9) replays a whole dump as ONE transaction. The
    # per-statement `execute` loop it replaced turned a 1714-line dump into 1714
    # independent transactions right after a mass delete, which is how a replay
    # ended up chasing node ids the delete had just freed
    # (Neo.ClientError.Statement.EntityNotFound). `execute` stays: it is still the
    # single-statement helper, and narrowing its contract is not this change.
    "execute", "execute_many", "get_all_nodes", "get_all_nodes_by_type",
    "get_all_nodes_for_dump",
    "get_all_rules", "get_category_routes_by_node", "get_graph_nodes_and_edges",
    "get_latest_filechange_per_path", "get_node_with_neighbors", "get_nodes_by_category",
    "get_open_decisions_for_path", "get_projects", "get_recent_decisions", "get_rule",
    "get_rule_abstraction", "get_rule_statements", "get_rules_by_authority",
    "get_subagent_role", "increment_negative", "increment_positive",
    # Part 5 (isolation cycle): the audit's read-only cross-project Memory listing
    # (writ/graph/db/record_store.py). Added deliberately, same reasoning as above.
    "list_all_memories", "list_constraints",
    "list_indexes", "list_memories", "resolve_file_claims", "resolve_project_for_cwd",
    "tombstone_missing_memories", "traverse_neighbors",
    "update_rule_authority", "update_rule_confidence", "wire_governed_by", "wire_has_change",
    "wire_has_commit", "wire_has_decision", "wire_includes", "wire_motivated_by",
    "wire_realizes",
]

# (submodule, mixin class name) the facade composes Neo4jConnection from.
EXPECTED_MIXINS = [
    ("writ.graph.db._query_runner", "_QueryRunnerMixin"),
    ("writ.graph.db.rule_store", "RuleStoreMixin"),
    ("writ.graph.db.node_store", "NodeStoreMixin"),
    ("writ.graph.db.edge_store", "EdgeStoreMixin"),
    ("writ.graph.db.abstraction_store", "AbstractionStoreMixin"),
    ("writ.graph.db.schema_store", "SchemaStoreMixin"),
    ("writ.graph.db.project_store", "ProjectStoreMixin"),
    ("writ.graph.db.record_store", "RecordStoreMixin"),
    ("writ.graph.db.maintenance_store", "MaintenanceStoreMixin"),
]

# Module-level names other modules import from `writ.graph.db` -- must stay there.
EXPECTED_MODULE_NAMES = [
    "Neo4jConnection", "ProjectIdentityConflict", "ALLOWED_EDGE_TYPES",
    "RECORD_EDGE_TYPES", "CORPUS_EDGE_TYPES", "GraphConnection", "_GRAPH_ID_COALESCE",
    "_id_or_match", "_coerce_neo4j_value", "read_live_edges", "read_live_nodes_with_keys",
]


class TestMixinComposition:
    def test_neo4jconnection_composed_from_store_mixins(self) -> None:
        import importlib

        from writ.graph.db import Neo4jConnection

        mro = Neo4jConnection.__mro__
        for module_path, cls_name in EXPECTED_MIXINS:
            mod = importlib.import_module(module_path)  # RED today: submodule absent
            mixin = getattr(mod, cls_name)
            assert mixin in mro, (
                f"{cls_name} from {module_path} must be a base of Neo4jConnection; "
                f"MRO is {[c.__name__ for c in mro]}"
            )


class TestMethodSurfaceParity:
    @pytest.mark.parametrize("name", EXPECTED_METHODS)
    def test_method_present(self, name: str) -> None:
        from writ.graph.db import Neo4jConnection

        assert callable(getattr(Neo4jConnection, name, None)), (
            f"Neo4jConnection.{name} was lost in the split -- every method must "
            f"resolve on the composed class"
        )

    def test_no_extra_or_missing_methods(self) -> None:
        from writ.graph.db import Neo4jConnection

        # Full resolved surface (app methods only: single-underscore helpers +
        # __init__, excluding inherited dunders). NOT pre-filtered to the expected
        # list, so an unexpected EXTRA method fails here too, not just a missing one.
        resolved = {
            n for n in dir(Neo4jConnection)
            if callable(getattr(Neo4jConnection, n))
            and (not n.startswith("__") or n == "__init__")
        }
        assert resolved == set(EXPECTED_METHODS), (
            "resolved method surface diverged from the frozen list; "
            f"extras: {sorted(resolved - set(EXPECTED_METHODS))}; "
            f"missing: {sorted(set(EXPECTED_METHODS) - resolved)}"
        )
        # The facade class body itself defines ONLY __init__ and close; every other
        # method must come from a store mixin.
        facade_defined = {
            n for n, v in vars(Neo4jConnection).items()
            if (callable(v) or isinstance(v, staticmethod))
            and (not n.startswith("__") or n == "__init__")
        }
        assert facade_defined == {"__init__", "close"}, (
            f"facade must define only __init__/close; found {sorted(facade_defined)}"
        )


class TestSeamPreserved:
    def test_module_level_names_importable(self) -> None:
        import writ.graph.db as db

        missing = [n for n in EXPECTED_MODULE_NAMES if not hasattr(db, n)]
        assert missing == [], f"module-level names not re-exported from writ.graph.db: {missing}"

    def test_neo4jconnection_is_patchable_attr(self, monkeypatch) -> None:
        import writ.graph.db as db

        sentinel = object()
        monkeypatch.setattr("writ.graph.db.Neo4jConnection", sentinel, raising=True)
        assert db.Neo4jConnection is sentinel, (
            "writ.graph.db.Neo4jConnection must stay a settable module attribute "
            "(two live monkeypatch sites depend on it)"
        )

    def test_construct_sets_driver_and_database(self) -> None:
        from writ.graph.db import Neo4jConnection

        # Driver creation does not open a connection, so this is offline-safe.
        # A deliberately non-production port. This asserts on constructor wiring and never
        # connects, but naming the real instance made it indistinguishable from a test that
        # genuinely reaches for production, which is what the isolated-run guard in
        # conftest.py looks for. A literal that cannot be the live graph keeps the guard
        # meaningful instead of teaching people to exempt it.
        conn = Neo4jConnection("bolt://localhost:7699", "u", "p", database="seamtest")
        try:
            assert conn._database == "seamtest"
            assert conn._driver is not None
        finally:
            asyncio.run(conn.close())
