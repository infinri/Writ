"""Phase 5.1: source-derived count regression gate.

A pure (no-Neo4j) test that pins stable counts derived from SOURCE (schema,
config, hooks.json, route decorators). Documentation is intentionally not
asserted against here: doc prose can go stale independently of whether the
code is correct, so a test suite failure must never hinge on doc content.

Four source-derived counts:
  node types  -- len(NODE_ID_FIELDS)  == 13
  edge types  -- len(ALLOWED_EDGE_TYPES) == 24
  modes       -- len(MODE_CONFIG)     == 5
  hooks       -- json.load hooks/hooks.json, count "command" leaves == 44
  endpoints   -- regex @app/@router route decorators across writ/server/**.py == 46
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from writ.graph.db import ALLOWED_EDGE_TYPES
from writ.graph.schema import NODE_ID_FIELDS
from writ.session.mode_engine import MODE_CONFIG

from tests.conftest import writ_server_source

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

# ---------------------------------------------------------------------------
# Source-derived counts (computed once at import time -- always run, never skip)
# ---------------------------------------------------------------------------
SOURCE_NODE_TYPE_COUNT: int = len(NODE_ID_FIELDS)
SOURCE_EDGE_TYPE_COUNT: int = len(ALLOWED_EDGE_TYPES)
SOURCE_MODE_COUNT: int = len(MODE_CONFIG)


def _count_hooks_json_entries() -> int:
    """Count JSON objects that carry a 'command' key in hooks/hooks.json."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    # The file is a list of objects, each representing one hook registration.
    # Count entries with a "command" key (leaf hook objects).
    if isinstance(data, list):
        return sum(1 for entry in data if "command" in entry)
    # Nested format: {event: [{command: ...}, ...], ...}
    count = 0
    def _walk(obj):
        nonlocal count
        if isinstance(obj, dict):
            if "command" in obj:
                count += 1
            else:
                for v in obj.values():
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
    _walk(data)
    return count


def _count_server_endpoints() -> int:
    """Count `@app.<verb>` and `@router.<verb>` route decorators across the
    writ.server module/package (layout-agnostic; see writ_server_source())."""
    text = writ_server_source()
    return len(re.findall(r"^@(?:app|router)\.(get|post|put|delete|patch)", text, re.MULTILINE))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDocCounts:
    """Regression gate: each stable count derived from source must match the doc claim."""

    # --- node types ---------------------------------------------------------

    def test_node_types_source_count(self) -> None:
        assert SOURCE_NODE_TYPE_COUNT == 13, (
            f"NODE_ID_FIELDS has {SOURCE_NODE_TYPE_COUNT} entries; "
            "update this test and the docs if the schema changed"
        )

    # --- edge types ---------------------------------------------------------

    def test_edge_types_source_count(self) -> None:
        # APPLIES_TO and JUSTIFIED_BY were retired (19 -> 17); later features
        # added edge types, so the current source-of-truth count is 24.
        assert SOURCE_EDGE_TYPE_COUNT == 24, (
            f"ALLOWED_EDGE_TYPES has {SOURCE_EDGE_TYPE_COUNT} entries; expected 24. "
            "If this changed, update writ/graph/db/_common.py ALLOWED_EDGE_TYPES."
        )

    # --- modes --------------------------------------------------------------

    def test_modes_source_count(self) -> None:
        assert SOURCE_MODE_COUNT == 5, (
            f"MODE_CONFIG has {SOURCE_MODE_COUNT} entries; "
            "update this test if a mode was added or removed"
        )

    def test_all_mode_names_importable(self) -> None:
        expected_names = {"work", "debug", "review", "conversation", "investigate"}
        assert set(MODE_CONFIG.keys()) == expected_names, (
            f"MODE_CONFIG keys differ: got {set(MODE_CONFIG.keys())}"
        )

    # --- hooks --------------------------------------------------------------

    def test_hooks_json_entry_count(self) -> None:
        # 44 = the 41 long-standing registrations + writ-manual-test-grant.sh +
        # writ-state-write-gate.sh + writ-memory-capture.sh (the auto-memory mirror).
        source_count = _count_hooks_json_entries()
        assert source_count == 44, (
            f"hooks/hooks.json has {source_count} 'command' entries; expected 44. "
            "Bump this (and HANDBOOK 'registers **N hook scripts**') when adding or "
            "removing a registration."
        )

    # --- endpoints ----------------------------------------------------------

    def test_server_endpoint_count(self) -> None:
        # source_count is derived from writ_server_source(), which scans
        # writ/server/**/*.py and matches both @app.<verb> and @router.<verb>
        # decorators. Bump this when adding/removing a route.
        # 46 = 45 + POST /memory-record (the auto-memory graph mirror).
        source_count = _count_server_endpoints()
        assert source_count == 46, (
            f"writ.server has {source_count} @app/@router route decorators; expected 46"
        )
