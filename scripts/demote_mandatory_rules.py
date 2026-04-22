"""Demote mandatory rules flagged in the Section 17 retroactive audit.

Usage:
  .venv/bin/python scripts/demote_mandatory_rules.py [--dry-run]

Reads the list of rule_ids from docs/mandatory-rule-audit.md's
"Recommended demotions" section. For each rule, flips mandatory: true →
false in Neo4j. Idempotent — re-running is safe.

Per plan Section 17.4: maintainer has spot-checked the audit BEFORE running
this script. If spot-check fails, re-draft the audit instead of running.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from writ.graph.db import Neo4jConnection

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"
AUDIT_PATH = Path(__file__).resolve().parent.parent / "docs" / "mandatory-rule-audit.md"


def parse_demotion_ids(audit_path: Path) -> list[str]:
    """Extract rule IDs from the audit's 'Recommended demotions' code block."""
    text = audit_path.read_text()
    section = re.search(
        r"## Recommended demotions.*?```\n(.*?)```", text, re.DOTALL
    )
    if not section:
        raise ValueError("Could not locate 'Recommended demotions' code block in audit")
    ids: list[str] = []
    for line in section.group(1).splitlines():
        m = re.match(r"^(ENF-[A-Z0-9]+(?:-[A-Z0-9]+)*)", line.strip())
        if m:
            ids.append(m.group(1))
    return ids


async def demote(rule_id: str, db: Neo4jConnection, dry_run: bool) -> tuple[str, bool]:
    """Return (rule_id, was_changed). was_changed=False if already advisory or absent."""
    async with db._driver.session(database=db._database) as session:
        check = await session.run(
            "MATCH (r:Rule {rule_id: $rid}) RETURN r.mandatory AS m", rid=rule_id
        )
        rec = await check.single()
        if rec is None:
            return rule_id, False  # not in graph
        if not rec["m"]:
            return rule_id, False  # already advisory
        if dry_run:
            return rule_id, True
        await session.run(
            "MATCH (r:Rule {rule_id: $rid}) SET r.mandatory = false", rid=rule_id
        )
        return rule_id, True


async def main(dry_run: bool) -> int:
    ids = parse_demotion_ids(AUDIT_PATH)
    print(f"Found {len(ids)} rules flagged for demotion in audit.")
    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        changed = 0
        skipped = 0
        for rid in ids:
            _, was_changed = await demote(rid, db, dry_run)
            label = "[DRY RUN] would demote" if dry_run else "demoted"
            if was_changed:
                changed += 1
                print(f"  {label}: {rid}")
            else:
                skipped += 1
                print(f"  skip (already advisory or not in graph): {rid}")
        print()
        print(f"Demoted: {changed}  Skipped: {skipped}")
        if not dry_run:
            # Verify blocker: zero mandatory rules without mechanical_enforcement_path.
            async with db._driver.session(database=db._database) as session:
                q = """
                    MATCH (r:Rule)
                    WHERE r.mandatory = true
                    AND (r.mechanical_enforcement_path IS NULL
                         OR r.mechanical_enforcement_path = '')
                    RETURN count(r) AS c
                """
                result = await session.run(q)
                rec = await result.single()
                violations = rec["c"]
                print(f"\nPost-demotion audit: {violations} mandatory rules still lack mechanical_enforcement_path.")
                if violations:
                    print("Phase 2 release-blocker 'zero mandatory without mechanical' is NOT MET.")
                    return 1
                print("Phase 2 release-blocker 'zero mandatory without mechanical': MET.")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
