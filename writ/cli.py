"""Writ CLI -- typer entrypoint for all writ commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

DEFAULT_BIBLE_DIR = "bible/"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8765

app = typer.Typer(
    name="writ",
    help="Hybrid RAG knowledge retrieval service for AI coding rule enforcement.",
)


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, help="Port to bind the service to."),
    host: str = typer.Option(DEFAULT_HOST, help="Host to bind the service to."),
) -> None:
    """Start Writ service. Pre-warms indexes into memory."""
    import uvicorn

    from writ.server import app as fastapi_app

    typer.echo(f"Starting Writ service on {host}:{port}")
    typer.echo("Pre-warming indexes...")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command()
def ingest(
    path: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Path to rule source directory."),
) -> None:
    """Parse Markdown rules and ingest into graph. Validates schema. Triggers export."""
    from writ.graph.db import Neo4jConnection
    from writ.graph.ingest import discover_rule_files, parse_rules_from_file, validate_parsed_rule

    async def _run() -> None:
        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            files = discover_rule_files(path)
            count = 0
            errors = 0
            for f in files:
                for rule_data in parse_rules_from_file(f):
                    try:
                        validate_parsed_rule(rule_data)
                        clean = {k: v for k, v in rule_data.items() if not k.startswith("_")}
                        await db.create_rule(clean)
                        count += 1
                    except ValueError as e:
                        typer.echo(f"  Error: {e}")
                        errors += 1
            typer.echo(f"Ingested {count} rules ({errors} errors)")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def validate(
    review_confidence: bool = typer.Option(
        False, "--review-confidence", help="List rules at migration default confidence."
    ),
    benchmark: bool = typer.Option(False, "--benchmark", help="Report integrity check duration."),
) -> None:
    """Run integrity checks: conflicts, orphans, staleness, redundancy."""
    import time

    from writ.graph.db import Neo4jConnection
    from writ.graph.integrity import IntegrityChecker

    async def _run() -> int:
        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            checker = IntegrityChecker(db._driver, db._database)
            start = time.perf_counter()
            findings = await checker.run_all_checks()
            elapsed_ms = (time.perf_counter() - start) * 1000

            if findings["conflicts"]:
                typer.echo(f"\nConflicts ({len(findings['conflicts'])}):")
                for c in findings["conflicts"]:
                    typer.echo(f"  {c['rule_a']} <-> {c['rule_b']}")

            if findings["orphans"]:
                typer.echo(f"\nOrphans ({len(findings['orphans'])}):")
                for o in findings["orphans"]:
                    typer.echo(f"  {o}")

            if findings["stale"]:
                typer.echo(f"\nStale ({len(findings['stale'])}):")
                for s in findings["stale"]:
                    typer.echo(f"  {s['rule_id']} (expired {s['expired_on']})")

            if findings["redundant"]:
                typer.echo(f"\nRedundant ({len(findings['redundant'])}):")
                for r in findings["redundant"]:
                    typer.echo(f"  {r['rule_a']} ~ {r['rule_b']} ({r['similarity']})")

            if review_confidence:
                defaults = await checker.detect_confidence_defaults()
                typer.echo(f"\nRules at default confidence ({len(defaults)}):")
                for d in defaults:
                    typer.echo(f"  {d}")

            if benchmark:
                typer.echo(f"\nIntegrity check completed in {elapsed_ms:.1f}ms")

            if findings["exit_code"] == 0:
                typer.echo("\nAll checks passed.")
            else:
                typer.echo("\nFindings detected.")

            return findings["exit_code"]
        finally:
            await db.close()

    code = asyncio.run(_run())
    raise typer.Exit(code=code)


@app.command()
def add() -> None:
    """Add a new rule to the graph with relationship suggestion and validation."""
    from datetime import date

    from writ.authoring import check_conflicts, check_redundancy, suggest_relationships
    from writ.graph.db import Neo4jConnection
    from writ.graph.schema import Rule
    from writ.retrieval.pipeline import build_pipeline
    from writ.retrieval.traversal import AdjacencyCache

    async def _run() -> None:
        # Collect required fields.
        rule_id = typer.prompt("rule_id (e.g., ARCH-NEW-001)")
        domain = typer.prompt("domain")
        severity = typer.prompt("severity (critical/high/medium/low)")
        scope = typer.prompt("scope (file/module/slice/pr/session)")
        trigger = typer.prompt("trigger")
        statement = typer.prompt("statement")
        violation = typer.prompt("violation")
        pass_example = typer.prompt("pass_example")
        enforcement = typer.prompt("enforcement")
        rationale = typer.prompt("rationale")

        rule_data = {
            "rule_id": rule_id,
            "domain": domain,
            "severity": severity,
            "scope": scope,
            "trigger": trigger,
            "statement": statement,
            "violation": violation,
            "pass_example": pass_example,
            "enforcement": enforcement,
            "rationale": rationale,
            "last_validated": date.today().isoformat(),
        }

        # INV-6: Validate against schema before any graph write.
        try:
            Rule(**rule_data)
        except Exception as e:
            typer.echo(f"Validation error: {e}")
            raise typer.Exit(code=1)

        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            typer.echo("Building pipeline for relationship analysis...")
            pipeline = await build_pipeline(db)
            cache = AdjacencyCache()
            await cache.build_from_db(db)

            # Redundancy check.
            redundant = check_redundancy(rule_data, pipeline)
            if redundant:
                typer.echo("\nRedundancy warning (>= 0.95 cosine similarity):")
                for r in redundant:
                    typer.echo(f"  {r['rule_id']} (similarity: {r['similarity']})")
                    typer.echo(f"    {r['statement'][:100]}")

            # Relationship suggestions.
            suggestions = suggest_relationships(rule_data, pipeline)
            if suggestions:
                typer.echo("\nSuggested relationships:")
                for i, s in enumerate(suggestions, 1):
                    typer.echo(f"  {i}. {s['rule_id']} (score: {s['score']})")
                    typer.echo(f"     {s['statement'][:100]}")

            # Write rule to graph.
            await db.create_rule(rule_data)
            typer.echo(f"\nCreated rule: {rule_id}")

            # Offer to create edges for accepted suggestions.
            if suggestions:
                for s in suggestions:
                    edge_types = ["DEPENDS_ON", "SUPPLEMENTS", "CONFLICTS_WITH", "RELATED_TO"]
                    create = typer.confirm(f"Create edge to {s['rule_id']}?", default=False)
                    if create:
                        edge_type = typer.prompt(
                            f"Edge type ({'/'.join(edge_types)})",
                            default="RELATED_TO",
                        )
                        if edge_type in edge_types:
                            await db.create_edge(edge_type, rule_id, s["rule_id"])
                            typer.echo(f"  Created {edge_type} -> {s['rule_id']}")
                        else:
                            typer.echo(f"  Unknown edge type: {edge_type}, skipped.")

            # Conflict check after edges are created.
            await cache.build_from_db(db)
            conflicts = check_conflicts(rule_id, cache)
            if conflicts:
                typer.echo("\nConflict warning:")
                for c in conflicts:
                    typer.echo(f"  CONFLICTS_WITH {c['rule_id']}")

            # Phase 7 stub.
            typer.echo("\nExport stub -- will auto-export in Phase 7.")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def edit(
    rule_id: str = typer.Argument(..., help="ID of the rule to edit."),
) -> None:
    """Edit an existing rule in the graph."""
    from writ.authoring import check_conflicts, check_redundancy, suggest_relationships
    from writ.graph.db import Neo4jConnection
    from writ.graph.schema import Rule
    from writ.retrieval.pipeline import build_pipeline
    from writ.retrieval.traversal import AdjacencyCache

    async def _run() -> None:
        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            existing = await db.get_rule(rule_id)
            if existing is None:
                typer.echo(f"Rule not found: {rule_id}")
                raise typer.Exit(code=1)

            typer.echo(f"Editing rule: {rule_id}")
            typer.echo("Press Enter to keep current value.\n")

            fields = ["domain", "severity", "scope", "trigger", "statement",
                       "violation", "pass_example", "enforcement", "rationale"]
            updated = dict(existing)
            for field in fields:
                current = existing.get(field, "")
                display = str(current)[:80] if current else "(empty)"
                new_val = typer.prompt(f"{field} [{display}]", default=str(current))
                updated[field] = new_val

            # INV-6: Validate before write.
            try:
                Rule(**updated)
            except Exception as e:
                typer.echo(f"Validation error: {e}")
                raise typer.Exit(code=1)

            typer.echo("Building pipeline for relationship analysis...")
            pipeline = await build_pipeline(db)
            cache = AdjacencyCache()
            await cache.build_from_db(db)

            # Redundancy check on updated text.
            redundant = check_redundancy(updated, pipeline)
            # Filter out self from redundancy results.
            redundant = [r for r in redundant if r["rule_id"] != rule_id]
            if redundant:
                typer.echo("\nRedundancy warning:")
                for r in redundant:
                    typer.echo(f"  {r['rule_id']} (similarity: {r['similarity']})")

            # Re-suggest relationships.
            suggestions = suggest_relationships(updated, pipeline)
            if suggestions:
                typer.echo("\nSuggested relationships:")
                for i, s in enumerate(suggestions, 1):
                    typer.echo(f"  {i}. {s['rule_id']} (score: {s['score']})")

            # INV-7: MERGE = idempotent update.
            await db.create_rule(updated)
            typer.echo(f"\nUpdated rule: {rule_id}")

            # Offer edges.
            if suggestions:
                for s in suggestions:
                    create = typer.confirm(f"Create edge to {s['rule_id']}?", default=False)
                    if create:
                        edge_type = typer.prompt("Edge type", default="RELATED_TO")
                        await db.create_edge(edge_type, rule_id, s["rule_id"])
                        typer.echo(f"  Created {edge_type} -> {s['rule_id']}")

            # Conflict check.
            await cache.build_from_db(db)
            conflicts = check_conflicts(rule_id, cache)
            if conflicts:
                typer.echo("\nConflict warning:")
                for c in conflicts:
                    typer.echo(f"  CONFLICTS_WITH {c['rule_id']}")

            typer.echo("\nExport stub -- will auto-export in Phase 7.")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def export(
    output: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Output directory for generated Markdown."),
) -> None:
    """Regenerate Markdown from graph. Overwrites output directory."""
    typer.echo("Not implemented -- Phase 7")
    raise typer.Exit(code=1)


@app.command()
def migrate() -> None:
    """One-time migration of existing rules into graph."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/migrate.py"],
        capture_output=False,
    )
    raise typer.Exit(code=result.returncode)


@app.command()
def query(
    query_text: str = typer.Argument(..., help="Natural language query for rule retrieval."),
    domain: str | None = typer.Option(None, help="Filter by domain."),
    budget: int | None = typer.Option(None, help="Context budget in tokens."),
) -> None:
    """CLI rule query for testing retrieval quality."""
    from writ.graph.db import Neo4jConnection
    from writ.retrieval.pipeline import build_pipeline

    async def _run() -> None:
        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            typer.echo("Building pipeline (loading indexes)...")
            pipeline = await build_pipeline(db)
            typer.echo(f"Querying: {query_text}\n")
            result = pipeline.query(
                query_text=query_text,
                domain=domain,
                budget_tokens=budget,
            )
            typer.echo(f"Mode: {result['mode']} | Candidates: {result['total_candidates']} | Latency: {result['latency_ms']}ms\n")
            for i, rule in enumerate(result["rules"], 1):
                typer.echo(f"  {i}. [{rule['score']}] {rule['rule_id']}")
                if "statement" in rule:
                    typer.echo(f"     {rule['statement'][:100]}")
                typer.echo()
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def status() -> None:
    """Health check: rule count, index status, last ingestion, stale rules."""
    import httpx

    try:
        resp = httpx.get(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/health", timeout=5.0)
        data = resp.json()
        typer.echo(json.dumps(data, indent=2))
    except httpx.ConnectError:
        typer.echo("Service not running. Start with: writ serve")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
