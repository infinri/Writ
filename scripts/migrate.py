"""One-time migration of existing Markdown rules into the graph.

Discovers all rules in bible/, validates against schema, and ingests into Neo4j.
Cross-references become RELATED_TO skeleton edges.
Script is idempotent -- uses MERGE, not CREATE.

Usage: python scripts/migrate.py [--bible-dir bible/] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path for imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from writ.graph.db import Neo4jConnection
from writ.graph.ingest import (
    discover_rule_files,
    parse_rules_from_file,
    validate_parsed_rule,
)

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "writdevpass"


async def run_migration(bible_dir: Path, dry_run: bool = False) -> None:
    """Execute the full migration pipeline."""
    files = discover_rule_files(bible_dir)
    print(f"Discovered {len(files)} Markdown files in {bible_dir}")

    all_rules: list[dict] = []
    skipped_files = 0
    parse_errors: list[str] = []

    for filepath in files:
        parsed = parse_rules_from_file(filepath)
        if not parsed:
            skipped_files += 1
            continue
        for rule_data in parsed:
            try:
                validate_parsed_rule(rule_data)
                all_rules.append(rule_data)
            except ValueError as e:
                parse_errors.append(str(e))

    print(f"Parsed {len(all_rules)} rules ({skipped_files} files skipped, {len(parse_errors)} errors)")

    if parse_errors:
        print("\nValidation errors:")
        for err in parse_errors:
            print(f"  - {err}")

    if dry_run:
        print("\n[DRY RUN] Would insert the following rules:")
        for rule in all_rules:
            mandatory = "MANDATORY" if rule.get("mandatory") else "domain"
            refs = rule.get("_cross_references", [])
            print(f"  {rule['rule_id']} ({mandatory}) [{len(refs)} cross-refs]")
        return

    db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        # Phase 1a: apply uniqueness constraint and performance indexes.
        await db.apply_constraints()
        print("Applied Neo4j constraints and indexes")

        created = 0
        for rule_data in all_rules:
            clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
            await db.create_rule(clean)
            created += 1

        print(f"Inserted/updated {created} rule nodes")

        # Create RELATED_TO skeleton edges from cross-references.
        edge_count = 0
        rule_ids_in_graph = {r["rule_id"] for r in all_rules}
        for rule_data in all_rules:
            for ref_id in rule_data.get("_cross_references", []):
                if ref_id in rule_ids_in_graph:
                    await db.create_edge("RELATED_TO", rule_data["rule_id"], ref_id)
                    edge_count += 1

        print(f"Created {edge_count} RELATED_TO skeleton edges")

        # Verify.
        count = await db.count_rules()
        print(f"\nVerification: {count} Rule nodes in graph")

        mandatory_count = 0
        for rule in all_rules:
            if rule.get("mandatory"):
                mandatory_count += 1
        print(f"  Mandatory (ENF-*): {mandatory_count}")
        print(f"  Domain rules: {count - mandatory_count}")

    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Markdown rules into Neo4j graph.")
    parser.add_argument(
        "--bible-dir",
        type=Path,
        default=Path("bible/"),
        help="Path to rule source directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without writing to database.",
    )
    args = parser.parse_args()

    if not args.bible_dir.exists():
        print(f"Error: bible directory not found: {args.bible_dir}")
        sys.exit(1)

    asyncio.run(run_migration(args.bible_dir, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
