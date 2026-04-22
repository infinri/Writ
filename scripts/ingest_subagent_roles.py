"""Ingest SubagentRole nodes into Neo4j from .claude/agents/*.md.

Phase 3 deliverable 2: the graph becomes the canonical source of subagent
definitions. scripts/export_subagent_roles.py regenerates the .md files
from the graph (the reverse direction).

Each .md has YAML front-matter (name, description, model, tools) plus a
markdown body containing the system prompt. This script parses both and
creates a SubagentRole node per file, idempotent via MERGE.

Usage: .venv/bin/python scripts/ingest_subagent_roles.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from writ.graph.db import Neo4jConnection

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"
AGENTS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "agents"
FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)

# Dispatched-by map: which Playbooks invoke each role. Per plan Section 8
# Subagent-Driven Development (PBK-PROC-SDD-001) dispatches spec-reviewer
# and code-reviewer; writ-implementer is also dispatched by SDD.
DISPATCHED_BY: dict[str, list[str]] = {
    "writ-explorer":              [],
    "writ-planner":               [],
    "writ-test-writer":           [],
    "writ-implementer":           ["PBK-PROC-SDD-001"],
    "writ-spec-reviewer":         ["PBK-PROC-SDD-001"],
    "writ-code-quality-reviewer": ["PBK-PROC-SDD-001", "PBK-PROC-REVREQ-001"],
}


def parse_agent_file(path: Path) -> dict | None:
    text = path.read_text()
    m = FRONT_MATTER.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    body = m.group(2).strip()
    name = fm.get("name") or path.stem
    return {
        "role_id": f"ROL-{name.upper().replace('WRIT-', '')}-001",
        "name": name,
        "description": fm.get("description") or "",
        "model": fm.get("model"),
        "tools": fm.get("tools"),
        "prompt_template": body,
    }


def build_node(agent: dict) -> dict:
    """Assemble SubagentRole node dict with all required base fields."""
    today = date.today().isoformat()
    return {
        "role_id": agent["role_id"],
        "domain": "process",
        "scope": "task",
        "trigger": f"When a workflow dispatches the {agent['name']} subagent.",
        "statement": agent["description"] or f"{agent['name']} subagent role template.",
        "rationale": "Graph-canonical subagent definition per plan Section 8 deliverable 2.",
        "tags": sorted({"process", "subagent", "template"}),
        "confidence": "peer-reviewed",
        "authority": "human",
        "last_validated": today,
        "staleness_window": 365,
        "evidence": "doc:superpowers",
        "source_attribution": "superpowers@5.0.7",
        "name": agent["name"],
        "prompt_template": agent["prompt_template"],
        "dispatched_by": DISPATCHED_BY.get(agent["name"], []),
        "model_preference": agent.get("model"),
    }


async def main(dry_run: bool) -> int:
    files = sorted(AGENTS_DIR.glob("*.md"))
    print(f"Parsing {len(files)} agent definitions from {AGENTS_DIR}")

    nodes: list[dict] = []
    for f in files:
        parsed = parse_agent_file(f)
        if not parsed:
            print(f"  skip (no front-matter): {f.name}")
            continue
        nodes.append(build_node(parsed))

    if dry_run:
        for n in nodes:
            print(f"  [DRY RUN] would ingest: {n['role_id']} ({n['name']})  "
                  f"model={n.get('model_preference')}  "
                  f"prompt_chars={len(n['prompt_template'])}")
        return 0

    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        created = 0
        for n in nodes:
            await db.create_methodology_node("SubagentRole", n)
            created += 1
            print(f"  ingested: {n['role_id']} ({n['name']})")
        print(f"\nTotal: {created} SubagentRole nodes in graph.")

        # Verification: all 6 present + model_preference populated.
        async with db._driver.session(database=db._database) as session:
            q = """
                MATCH (r:SubagentRole)
                RETURN r.role_id AS rid, r.name AS name, r.model_preference AS model
                ORDER BY r.role_id
            """
            result = await session.run(q)
            rows = [rec.data() async for rec in result]
            print("\nSubagentRole nodes in graph:")
            for r in rows:
                print(f"  {r['rid']:<30} name={r['name']}  model={r['model']}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
