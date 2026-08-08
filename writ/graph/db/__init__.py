"""writ.graph.db -- Neo4j access layer.

This package replaces the former single db.py module. Neo4jConnection is composed
from per-domain store mixins (rule/node/edge/abstraction/schema/project/record/
maintenance); each mixin holds its cluster's methods verbatim and reads
self._driver / self._database (set here in __init__). This module stays the import
+ monkeypatch seam: writ.graph.db.Neo4jConnection and every module-level name below
remain importable and patchable at this exact path.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from neo4j import AsyncGraphDatabase

from writ.graph.db._common import (
    ALLOWED_EDGE_TYPES,
    CORPUS_EDGE_TYPES,
    GraphConnection,
    METHODOLOGY_NODE_ID_FIELDS,
    METHODOLOGY_NODE_LABELS,
    ProjectIdentityConflict,
    RECORD_EDGE_TYPES,
    _GRAPH_ID_COALESCE,
    _RECORD_EDGE_ENDPOINTS,
    _coerce_neo4j_value,
    _id_field_for_label,
    _id_or_match,
    _node_write_spec,
    _now_iso,
    read_live_edges,
    read_live_nodes_with_keys,
)

if TYPE_CHECKING:
    from neo4j import AsyncDriver
from writ.graph.db._safety import FullWipeRefused
from writ.graph.db._query_runner import _QueryRunnerMixin
from writ.graph.db.rule_store import RuleStoreMixin
from writ.graph.db.node_store import NodeStoreMixin
from writ.graph.db.edge_store import EdgeStoreMixin
from writ.graph.db.abstraction_store import AbstractionStoreMixin
from writ.graph.db.schema_store import SchemaStoreMixin
from writ.graph.db.project_store import ProjectStoreMixin
from writ.graph.db.record_store import RecordStoreMixin
from writ.graph.db.maintenance_store import MaintenanceStoreMixin

# Public surface re-exported from _common so `from writ.graph.db import X` keeps
# resolving for external callers (the seam the old single-file module exposed).
__all__ = [
    "Neo4jConnection",
    "FullWipeRefused",
    "ProjectIdentityConflict",
    "GraphConnection",
    "ALLOWED_EDGE_TYPES",
    "CORPUS_EDGE_TYPES",
    "RECORD_EDGE_TYPES",
    "METHODOLOGY_NODE_ID_FIELDS",
    "METHODOLOGY_NODE_LABELS",
    "_GRAPH_ID_COALESCE",
    "_RECORD_EDGE_ENDPOINTS",
    "_coerce_neo4j_value",
    "_id_field_for_label",
    "_id_or_match",
    "_node_write_spec",
    "_now_iso",
    "read_live_edges",
    "read_live_nodes_with_keys",
]


class Neo4jConnection(
    _QueryRunnerMixin, RuleStoreMixin, NodeStoreMixin, EdgeStoreMixin, AbstractionStoreMixin, SchemaStoreMixin, ProjectStoreMixin, RecordStoreMixin, MaintenanceStoreMixin,
):
    """Neo4j graph connection. Behavior identical to the former db.py class; the
    methods now live on the store mixins above (see each module)."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        # Retained so clear_all can tell WHICH instance it is about to delete from.
        # The driver does not expose its target URI in a stable, public way, and the
        # whole-graph wipe guard (writ/graph/db/_safety.py) cannot make a decision it
        # has no way to observe. Never logged: it is inert connection metadata, but
        # the credentials that travel with it are not, so only the URI is kept.
        self._uri = uri

    async def close(self) -> None:
        """Close the driver connection pool."""
        await self._driver.close()
