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
    NODE_ID_FIELDS,
    discover_rule_files,
    parse_edges_from_file,
    parse_nodes_from_file,
    parse_rules_from_file,
    validate_parsed_node,
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


async def run_methodology_migration(
    fixtures_dir: Path,
    dry_run: bool = False,
    db: Neo4jConnection | None = None,
) -> None:
    """Ingest methodology nodes and edges from a fixtures directory.

    Creates nodes under the correct Phase-1 label (Skill, Playbook, etc.) and
    methodology edges (TEACHES, GATES, etc.). Idempotent via MERGE. Safe to
    re-run.
    """
    files = sorted(fixtures_dir.glob("*.md"))
    print(f"\n[methodology] Discovered {len(files)} files in {fixtures_dir}")

    parsed_nodes: list[dict] = []
    parsed_edges: list[dict] = []
    errors: list[str] = []

    for filepath in files:
        try:
            for node in parse_nodes_from_file(filepath):
                validate_parsed_node(node)
                parsed_nodes.append(node)
            parsed_edges.extend(parse_edges_from_file(filepath))
        except Exception as e:
            errors.append(f"{filepath.name}: {type(e).__name__}: {e}")

    print(f"[methodology] Parsed {len(parsed_nodes)} methodology nodes, {len(parsed_edges)} edges")
    if errors:
        print(f"[methodology] {len(errors)} errors:")
        for err in errors[:10]:
            print(f"  - {err}")

    if dry_run:
        by_type: dict[str, int] = {}
        for n in parsed_nodes:
            by_type[n["node_type"]] = by_type.get(n["node_type"], 0) + 1
        print("[methodology DRY RUN] By type:", dict(sorted(by_type.items())))
        return

    owned_db = db is None
    if owned_db:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    try:
        created = 0
        rule_created = 0
        for node in parsed_nodes:
            node_type = node["node_type"]
            clean = {
                k: v for k, v in node.items()
                if k != "node_type" and not k.startswith("_") and k != "edges"
            }
            if node_type == "Rule":
                # Methodology rules (ENF-PROC-*, META-AUTH-*) use the existing
                # Rule label; route through create_rule for schema consistency.
                await db.create_rule(clean)
                rule_created += 1
            else:
                await db.create_methodology_node(node_type, clean)
                created += 1
        print(f"[methodology] Inserted/updated {created} new-type nodes + {rule_created} methodology Rule nodes")

        # Edges: filter to those whose source+target both exist in the parsed set
        # (or in the pre-existing Rule graph). Skip dangling references.
        parsed_ids = {
            n[NODE_ID_FIELDS[n["node_type"]]]
            for n in parsed_nodes
        }
        existing_rule_ids = {r["rule_id"] for r in await db.get_all_rules()}
        known_ids = parsed_ids | existing_rule_ids

        edge_count = 0
        edge_dangling = 0
        for e in parsed_edges:
            if e["source"] not in known_ids or e["target"] not in known_ids:
                edge_dangling += 1
                continue
            try:
                await db.create_edge(e["type"], e["source"], e["target"])
                edge_count += 1
            except ValueError:
                edge_dangling += 1
        print(f"[methodology] Created {edge_count} edges ({edge_dangling} skipped: dangling or unknown type)")
    finally:
        if owned_db:
            await db.close()


async def run_combined_migration(
    bible_dir: Path,
    methodology_dir: Path | None,
    dry_run: bool,
) -> None:
    """Run rule migration and optional methodology migration against one DB session."""
    if dry_run:
        await run_migration(bible_dir, dry_run=True)
        if methodology_dir is not None:
            await run_methodology_migration(methodology_dir, dry_run=True)
        return

    await run_migration(bible_dir, dry_run=False)
    if methodology_dir is not None:
        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            await run_methodology_migration(methodology_dir, dry_run=False, db=db)
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
        "--methodology-dir",
        type=Path,
        default=None,
        help="Path to methodology fixtures directory. If set, also ingests new-node-type content.",
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

    if args.methodology_dir is not None and not args.methodology_dir.exists():
        print(f"Error: methodology directory not found: {args.methodology_dir}")
        sys.exit(1)

    asyncio.run(run_combined_migration(args.bible_dir, args.methodology_dir, args.dry_run))


if __name__ == "__main__":
    main()
