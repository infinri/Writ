"""Writ CLI -- typer entrypoint for all writ commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from writ.config import get_neo4j_uri, get_neo4j_user, get_neo4j_password

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


@app.command(name="import-markdown")
def import_markdown(
    path: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Path to Markdown rule source directory."),
) -> None:
    """Import rules from Markdown files into the graph. Validates schema. Triggers export."""
    from writ.graph.db import Neo4jConnection
    from writ.graph.ingest import discover_rule_files, parse_rules_from_file, validate_parsed_rule

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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
            typer.echo(f"Imported {count} rules ({errors} errors)")

            # Auto-export after successful ingest (Phase 7).
            if count > 0 and errors == 0:
                from writ.export import export_rules_to_markdown

                export_result = await export_rules_to_markdown(db, path)
                typer.echo(f"Exported {export_result['rules_exported']} rules to {path}")
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
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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

            if findings.get("unreviewed"):
                u = findings["unreviewed"]
                typer.echo(f"\nUnreviewed AI-provisional: {u['message']}")

            if findings.get("frequency_stale"):
                typer.echo(f"\nFrequency stale ({len(findings['frequency_stale'])}):")
                for fs in findings["frequency_stale"][:10]:
                    typer.echo(f"  {fs['rule_id']} (last_seen: {fs.get('last_seen', 'never')})")

            if findings.get("graduation_flags"):
                typer.echo(f"\nGraduation flags ({len(findings['graduation_flags'])}):")
                for gf in findings["graduation_flags"]:
                    typer.echo(f"  {gf['rule_id']} (ratio: {gf['ratio']}, n={gf['n']})")

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

        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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

            # Auto-export after add (Phase 7).
            from writ.export import export_rules_to_markdown

            export_result = await export_rules_to_markdown(db, Path(DEFAULT_BIBLE_DIR))
            typer.echo(f"\nExported {export_result['rules_exported']} rules to {DEFAULT_BIBLE_DIR}")
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
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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

            # Auto-export after edit (Phase 7).
            from writ.export import export_rules_to_markdown

            export_result = await export_rules_to_markdown(db, Path(DEFAULT_BIBLE_DIR))
            typer.echo(f"\nExported {export_result['rules_exported']} rules to {DEFAULT_BIBLE_DIR}")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def export(
    output: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Output directory for generated Markdown."),
) -> None:
    """Regenerate Markdown from graph. Overwrites output directory."""
    from writ.export import export_rules_to_markdown
    from writ.graph.db import Neo4jConnection

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            result = await export_rules_to_markdown(db, output)
            typer.echo(f"Exported {result['rules_exported']} rules to {output}")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def compress() -> None:
    """Cluster rules into abstraction nodes for compressed retrieval."""
    import numpy as np
    from sentence_transformers import SentenceTransformer

    from writ.compression.abstractions import generate_abstractions, write_abstractions_to_graph
    from writ.compression.clusters import evaluate_both
    from writ.graph.db import Neo4jConnection

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            # Load non-mandatory rules.
            all_rules = await db.get_all_rules()
            domain_rules = [r for r in all_rules if not r.get("mandatory", False)]
            if not domain_rules:
                typer.echo("No domain rules to cluster.")
                raise typer.Exit(code=0)

            typer.echo(f"Clustering {len(domain_rules)} domain rules...")
            model = SentenceTransformer("all-MiniLM-L6-v2")
            texts = [f"{r.get('trigger', '')} {r.get('statement', '')}" for r in domain_rules]
            embeddings = np.array(model.encode(texts), dtype=np.float32)

            comparison = evaluate_both(
                [r["rule_id"] for r in domain_rules], embeddings,
            )
            typer.echo(f"\nHDBSCAN: {len(comparison.hdbscan.clusters)} clusters, "
                       f"silhouette={comparison.hdbscan.silhouette:.3f}")
            typer.echo(f"k-means: {len(comparison.kmeans.clusters)} clusters, "
                       f"silhouette={comparison.kmeans.silhouette:.3f}")
            typer.echo(f"Chosen: {comparison.chosen} ({comparison.reason})")

            chosen_result = (
                comparison.hdbscan if comparison.chosen == "hdbscan" else comparison.kmeans
            )

            abstractions = generate_abstractions(chosen_result, domain_rules)
            count = await write_abstractions_to_graph(db, abstractions)

            avg_ratio = 0.0
            if abstractions:
                avg_ratio = sum(a["compression_ratio"] for a in abstractions) / len(abstractions)

            typer.echo(f"\nCreated {count} abstractions")
            typer.echo(f"Ungrouped rules: {len(chosen_result.ungrouped)}")
            typer.echo(f"Average compression ratio: {avg_ratio:.1f}x")
        finally:
            await db.close()

    asyncio.run(_run())


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
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
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
def feedback(
    rule_id: str = typer.Argument(..., help="Rule ID to record feedback for."),
    signal: str = typer.Argument(..., help="Signal: 'positive' or 'negative'."),
) -> None:
    """Record positive or negative feedback for a rule (hook integration)."""
    from writ.graph.db import Neo4jConnection

    if signal not in ("positive", "negative"):
        typer.echo(f"Invalid signal: {signal}. Must be 'positive' or 'negative'.")
        raise typer.Exit(code=1)

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            if signal == "positive":
                found = await db.increment_positive(rule_id)
            else:
                found = await db.increment_negative(rule_id)

            if not found:
                typer.echo(f"Rule not found: {rule_id}")
                raise typer.Exit(code=1)
            typer.echo(f"Recorded {signal} feedback for {rule_id}")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def propose(
    rule_id: str = typer.Option(..., help="Rule ID for the proposed rule."),
    domain: str = typer.Option(..., help="Domain of the rule."),
    severity: str = typer.Option(..., help="Severity (critical/high/medium/low)."),
    scope: str = typer.Option(..., help="Scope of the rule."),
    trigger: str = typer.Option(..., help="When this rule applies."),
    statement: str = typer.Option(..., help="What the rule requires."),
    violation: str = typer.Option(..., help="Example violation."),
    pass_example: str = typer.Option(..., help="Example of passing."),
    enforcement: str = typer.Option(..., help="How the rule is enforced."),
    rationale: str = typer.Option(..., help="Why this rule exists."),
    task_description: str = typer.Option("", help="What the AI was doing when it proposed this rule."),
) -> None:
    """Propose an AI-generated rule. Runs structural gate before ingestion."""
    from datetime import date

    from writ.gate import propose_rule
    from writ.graph.db import Neo4jConnection
    from writ.origin_context import DEFAULT_DB_PATH
    from writ.retrieval.pipeline import build_pipeline

    candidate = {
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

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            typer.echo("Building pipeline...")
            pipeline = await build_pipeline(db)

            result = await propose_rule(
                candidate,
                pipeline,
                db,
                origin_db_path=DEFAULT_DB_PATH,
                task_description=task_description,
            )

            if result["accepted"]:
                typer.echo(f"Accepted: {result['rule_id']} (authority: ai-provisional)")
            else:
                typer.echo(f"Rejected: {result['rule_id']}")
                for reason in result.get("reasons", []):
                    typer.echo(f"  - {reason}")
        finally:
            await db.close()

    asyncio.run(_run())


@app.command()
def review(
    rule_id: str = typer.Argument(None, help="Rule ID to inspect. Omit to list all unreviewed."),
    promote: bool = typer.Option(False, "--promote", help="Promote AI-provisional to ai-promoted."),
    reject: bool = typer.Option(False, "--reject", help="Delete AI-provisional rule from graph."),
    downweight: bool = typer.Option(False, "--downweight", help="Set confidence floor (speculative)."),
    stats: bool = typer.Option(False, "--stats", help="Show review queue statistics."),
) -> None:
    """Review AI-proposed rules. List, inspect, promote, reject, or downweight."""
    from writ.graph.db import Neo4jConnection

    async def _run() -> None:
        db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        try:
            if stats:
                counts = await db.count_by_authority()
                total = sum(counts.values())
                typer.echo("Review queue statistics:")
                for authority, count in sorted(counts.items()):
                    typer.echo(f"  {authority}: {count}")
                typer.echo(f"  total: {total}")
                return

            if rule_id is None:
                # List all ai-provisional rules.
                rules = await db.get_rules_by_authority("ai-provisional")
                if not rules:
                    typer.echo("No AI-provisional rules in queue.")
                    return
                typer.echo(f"Unreviewed AI-provisional rules ({len(rules)}):\n")
                for r in rules:
                    typer.echo(f"  {r.get('rule_id', '?')}")
                    typer.echo(f"    trigger: {str(r.get('trigger', ''))[:80]}")
                    typer.echo(f"    statement: {str(r.get('statement', ''))[:80]}")
                    typer.echo()
                return

            # Inspect or act on a specific rule.
            existing = await db.get_rule(rule_id)
            if existing is None:
                typer.echo(f"Rule not found: {rule_id}")
                raise typer.Exit(code=1)

            if promote:
                if existing.get("authority") != "ai-provisional":
                    typer.echo(f"Cannot promote: {rule_id} has authority '{existing.get('authority', 'human')}'")
                    raise typer.Exit(code=1)
                confirm = typer.confirm(f"Promote {rule_id} to ai-promoted?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await db.update_rule_authority(rule_id, "ai-promoted")
                await db.update_rule_confidence(rule_id, "peer-reviewed")
                typer.echo(f"Promoted: {rule_id} (authority: ai-promoted, confidence: peer-reviewed)")
                return

            if reject:
                if existing.get("authority") != "ai-provisional":
                    typer.echo(f"Cannot reject: {rule_id} has authority '{existing.get('authority', 'human')}'")
                    raise typer.Exit(code=1)
                confirm = typer.confirm(f"Delete {rule_id} from graph?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await db.delete_rule(rule_id)
                typer.echo(f"Rejected and deleted: {rule_id}")
                return

            if downweight:
                confirm = typer.confirm(f"Downweight {rule_id} to speculative confidence?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await db.update_rule_confidence(rule_id, "speculative")
                typer.echo(f"Downweighted: {rule_id} (confidence: speculative)")
                return

            # Default: inspect the rule.
            typer.echo(f"Rule: {rule_id}")
            typer.echo(f"  authority: {existing.get('authority', 'human')}")
            typer.echo(f"  domain: {existing.get('domain', '')}")
            typer.echo(f"  severity: {existing.get('severity', '')}")
            typer.echo(f"  confidence: {existing.get('confidence', '')}")
            typer.echo(f"  trigger: {existing.get('trigger', '')}")
            typer.echo(f"  statement: {existing.get('statement', '')}")

            # Show origin context if available.
            try:
                from writ.origin_context import OriginContextStore

                store = OriginContextStore()
                ctx = store.get(rule_id)
                store.close()
                if ctx:
                    typer.echo("\n  Origin context:")
                    typer.echo(f"    task: {ctx['task_description']}")
                    typer.echo(f"    query: {ctx.get('query_that_triggered', 'N/A')}")
                    typer.echo(f"    consulted: {', '.join(ctx.get('existing_rules_consulted', []))}")
                    typer.echo(f"    created: {ctx['created_at']}")
                else:
                    typer.echo("\n  Origin context: not recorded")
            except Exception:
                typer.echo("\n  Origin context: not available")

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
