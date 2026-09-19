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
   where the live graph had 464 / 731 / 33). ``snapshot_graph`` dumps the EXACT live
   graph to a Cypher replay file before the first wipe; ``restore_full_corpus`` replays
   it, so the post-benchmark graph is byte-equivalent to the pre-benchmark graph.

Use all three: guard, then snapshot, before the first wipe; restore in the ``finally``.
The snapshot file survives a crashed run -- replay it manually with
``writ import-cypher <snapshot>`` if a benchmark dies between wipe and restore.
"""
from __future__ import annotations

from pathlib import Path

from writ.graph.db._safety import full_wipe_allowed, how_to_run_safely
from writ.graph.dump import import_cypher_dump, render_cypher_dump

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "var" / "benchmark-graph-snapshot.cypher"

# Graph-first provenance states have no markdown home; clearing them is irreversible.
_GRAPH_FIRST = ("proposed", "graduation_pending")


async def assert_safe_to_wipe(db) -> None:
    """Raise unless this wipe is safe: a disposable instance, holding no graph-first
    nodes. Call ONCE before the first destructive clear_all().

    THE INSTANCE CHECK COMES FIRST, and it is new. `clear_all` enforces
    `assert_full_wipe_allowed` only when the preserve set is EMPTY
    (`maintenance_store.py:37-47`), and a bare `clear_all()` preserves
    `RECORD_LABELS`, which is not, so nothing stopped a destructive benchmark from
    wiping the corpus on whichever instance happened to be configured, the live one
    included. The snapshot below is real protection but it is RECOVERY; requiring
    isolation is prevention, and the two are not interchangeable after a crash
    between wipe and restore.

    The refusal reuses `how_to_run_safely` rather than writing its own instructions,
    so a developer who hits this and the pytest skip is not told two different
    things about the same requirement (DRY-DUP-001).
    """
    uri = getattr(db, "_uri", None)
    if not full_wipe_allowed(uri):
        raise RuntimeError(
            "Refusing to run a destructive benchmark: the connected graph is not "
            f"marked disposable (uri={uri!r}), and this benchmark wipes the corpus.\n"
            + how_to_run_safely()
        )
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
