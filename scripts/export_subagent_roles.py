"""Export SubagentRole nodes from Neo4j to .claude/agents/*.md.

Phase 3b (plan Section 8.1 deliverable 2): inverse of ingest_subagent_roles.py.
The graph is the canonical source of subagent definitions; this script
regenerates .claude/agents/*.md from SubagentRole nodes.

Usage:
    .venv/bin/python scripts/export_subagent_roles.py              # write files
    .venv/bin/python scripts/export_subagent_roles.py --dry-run    # print only
    .venv/bin/python scripts/export_subagent_roles.py --check      # exit 1 if drift
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from writ.graph.db import Neo4jConnection

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"
AGENTS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "agents"


def render_agent_md(row: dict) -> str:
    """Render a SubagentRole row to the canonical .md file format.

    YAML front-matter fields in insertion order matching the original files:
    name, description, model, tools. prompt_template is the body.
    """
    lines = ["---", f"name: {row['name']}"]
    description = row.get("description") or row.get("statement") or ""
    if description:
        lines.append(f"description: {description}")
    if row.get("model_preference"):
        lines.append(f"model: {row['model_preference']}")
    if row.get("tools"):
        lines.append(f"tools: {row['tools']}")
    lines.append("---")
    lines.append("")
    body = (row.get("prompt_template") or "").rstrip()
    lines.append(body)
    lines.append("")
    return "\n".join(lines)


async def fetch_roles(db: Neo4jConnection) -> list[dict]:
    async with db._driver.session(database=db._database) as session:
        query = """
            MATCH (r:SubagentRole)
            RETURN r.role_id         AS role_id,
                   r.name            AS name,
                   r.description     AS description,
                   r.statement       AS statement,
                   r.prompt_template AS prompt_template,
                   r.model_preference AS model_preference,
                   r.tools           AS tools,
                   r.dispatched_by   AS dispatched_by
            ORDER BY r.name
        """
        result = await session.run(query)
        return [rec.data() async for rec in result]


async def main(dry_run: bool, check: bool) -> int:
    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        rows = await fetch_roles(db)
    finally:
        await db.close()

    if not rows:
        print("No SubagentRole nodes found. Run scripts/ingest_subagent_roles.py first.", file=sys.stderr)
        return 1

    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    drift = 0
    wrote = 0
    for row in rows:
        target = AGENTS_DIR / f"{row['name']}.md"
        rendered = render_agent_md(row)

        if check:
            current = target.read_text() if target.exists() else ""
            if current != rendered:
                drift += 1
                print(f"  DRIFT: {target.name}")
            continue

        if dry_run:
            print(f"  [DRY RUN] would write: {target} ({len(rendered)} bytes)")
            continue

        target.write_text(rendered)
        wrote += 1
        print(f"  wrote: {target.name} ({len(rendered)} bytes)")

    if check:
        if drift:
            print(f"\n{drift} file(s) drift from graph. Run without --check to regenerate.", file=sys.stderr)
            return 1
        print(f"OK: {len(rows)} file(s) match graph.")
        return 0

    if not dry_run:
        print(f"\nTotal: {wrote} file(s) written from {len(rows)} SubagentRole nodes.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print actions without writing")
    p.add_argument("--check", action="store_true", help="exit 1 if files drift from graph")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run, args.check)))
