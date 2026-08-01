"""Regenerate docs/architecture/assets/graph-data.js from the live Neo4j graph.

The architecture site's graph explorer (docs/architecture/knowledge-graph.html)
reads this static snapshot so the page works with no daemon or database running.
Re-run after the corpus changes:

    .venv/bin/python scripts/export-graph-snapshot.py

Prints the per-type censuses the page's legend/preset/table text is written
from, so the numbers in knowledge-graph.html can be updated in the same pass.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUT = REPO_ROOT / "docs" / "architecture" / "assets" / "graph-data.js"
TEXT_LIMIT = 180  # inspector preview length; matches the original export


async def main() -> None:
    from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
    from writ.graph.db import Neo4jConnection

    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    nodes, edges = await db.get_graph_nodes_and_edges()

    # Enrich with the inspector fields (statement preview + trigger) the census
    # read does not carry. Reuse the census's own id resolution so every node
    # type (rule_id, skill_id, abstraction_id, ...) joins correctly.
    from writ.graph.db._common import _GRAPH_ID_COALESCE

    nid = _GRAPH_ID_COALESCE.format(v="n")
    async with db._driver.session(database=db._database) as s:
        res = await s.run(
            f"MATCH (n) RETURN {nid} AS id, "
            "coalesce(n.statement, n.summary, '') AS text, "
            "coalesce(n.trigger, '') AS trigger"
        )
        detail = {r["id"]: (r["text"], r["trigger"]) async for r in res if r["id"]}
    await db.close()

    for n in nodes:
        text, trigger = detail.get(n["id"], ("", ""))
        n["text"] = (text or "")[:TEXT_LIMIT]
        n["trigger"] = trigger or ""

    payload = json.dumps({"nodes": nodes, "edges": edges}, separators=(",", ":"))
    OUT.write_text(
        "/* Snapshot of the writ-project knowledge graph, exported from Neo4j. */\n"
        f"window.WRIT_GRAPH = {payload};\n"
    )

    edge_types = Counter(e["type"] for e in edges)
    derived = {"RELATED_TO", "BELONGS_TO", "ABSTRACTS"}
    hand = sum(c for t, c in edge_types.items() if t not in derived)
    print(f"Wrote {OUT} ({len(nodes)} nodes, {len(edges)} edges)")
    print("node types:", dict(Counter(n["type"] for n in nodes).most_common()))
    print("edge types:", dict(edge_types.most_common()))
    print(f"hand-authored (declared) edges: {hand}")
    print(f"average degree: {2 * len(edges) / len(nodes):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
