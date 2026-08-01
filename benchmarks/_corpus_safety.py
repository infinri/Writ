"""Data-safety helpers for the destructive benchmarks.

The scale/traversal benchmarks call ``db.clear_all()`` against the LIVE Neo4j to build a
synthetic corpus. Two hazards follow, both fixed here:

1. **Permanent loss.** Graph-first nodes (``provenance`` in ``proposed`` /
   ``graduation_pending``) have NO markdown home -- ``clear_all()`` destroys them with no
   way to rebuild. ``assert_safe_to_wipe`` refuses to run while any exist.
2. **Lossy restore.** Rebuilding from ``bible/`` after the wipe is NOT a faithful restore:
   markdown ingest carries no Abstraction nodes (they live in the compression pipeline),
   derives RELATED_TO edges differently, and inherits any source-vs-graph flag drift
   (measured 2026-08-01: a bible/ rebuild left 400 nodes / 1060 edges and 32 mandatory
   where the live graph had 464 / 732 / 33). ``snapshot_graph`` dumps the EXACT live
   graph to a Cypher replay file before the first wipe; ``restore_full_corpus`` replays
   it, so the post-benchmark graph is byte-equivalent to the pre-benchmark graph.

Use all three: guard, then snapshot, before the first wipe; restore in the ``finally``.
The snapshot file survives a crashed run -- replay it manually with
``writ import-cypher <snapshot>`` if a benchmark dies between wipe and restore.
"""
from __future__ import annotations

from pathlib import Path

from writ.graph.dump import import_cypher_dump, render_cypher_dump

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "var" / "benchmark-graph-snapshot.cypher"

# Graph-first provenance states have no markdown home; clearing them is irreversible.
_GRAPH_FIRST = ("proposed", "graduation_pending")


async def assert_safe_to_wipe(db) -> None:
    """Raise if the live graph holds graph-first nodes that clear_all() would destroy
    permanently. Call ONCE before the first destructive clear_all()."""
    async with db._driver.session(database=db._database) as session:
        result = await session.run(
            "MATCH (n) WHERE n.provenance IN $states RETURN count(n) AS c",
            states=list(_GRAPH_FIRST),
        )
        count = (await result.single())["c"]
    if count:
        raise RuntimeError(
            f"Refusing to run a destructive benchmark: {count} graph-first node(s) "
            f"(provenance in {_GRAPH_FIRST}) have no markdown home, so clear_all() would "
            "destroy them permanently. Promote (writ promote-candidate) or remove them first."
        )


async def snapshot_graph(db) -> Path:
    """Dump the exact live graph to a Cypher replay file. Call BEFORE the first
    clear_all(); the file is the restore source and the crash-recovery artifact."""
    nodes = await db.get_all_nodes_for_dump()
    edges = await db.get_all_edges_cross_type()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(render_cypher_dump(nodes, edges), encoding="utf-8")
    return SNAPSHOT_PATH


async def restore_full_corpus(db, snapshot: Path = SNAPSHOT_PATH) -> None:
    """Replay the pre-benchmark snapshot (wipes first), restoring the graph exactly."""
    await import_cypher_dump(db, snapshot.read_text(encoding="utf-8"))
