"""Writ CLI -- typer entrypoint for all writ commands."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def _writ_db():
    """Open a Neo4j connection from config and always close it. Single source for
    the per-command connection lifecycle (was duplicated across ~14 commands).
    Neo4jConnection import stays deferred so DB-free commands (e.g. serve) don't
    pull the driver at CLI startup."""
    from writ.graph.db import Neo4jConnection
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        yield db
    finally:
        await db.close()


@app.command(name="analyze-friction")
def analyze_friction(
    log: Path | None = typer.Option(
        None,
        help="Path to a single friction log. Default: the project's split "
             "audit+friction+metrics streams, including rotated archives.",
    ),
    since: int = typer.Option(0, help="Only include events from the last N days (0 = all)."),
    top: int = typer.Option(10, help="Cap top-N rankings."),
    rotate: bool = typer.Option(False, help="Rotate log to .1 if it exceeds 5MB, then exit."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON instead of text."),
    rule: str | None = typer.Option(None, "--rule", help="Filter events to a single rule_id."),
    rule_effectiveness: bool = typer.Option(False, "--rule-effectiveness", help="Per-rule denial-stick-rate (Phase 5)."),
    skill_usage: bool = typer.Option(False, "--skill-usage", help="Skill loads vs playbook completion (Phase 5)."),
    playbook_compliance: bool = typer.Option(False, "--playbook-compliance", help="Per-playbook in-order compliance (Phase 5)."),
    graduation_candidates: bool = typer.Option(False, "--graduation-candidates", help="Rules ready to graduate (Phase 5)."),
    trim_candidates: bool = typer.Option(False, "--trim-candidates", help="Rules / skills with low activation (Phase 5)."),
    quality_judge_false_positives: bool = typer.Option(False, "--quality-judge-false-positives", help="Per-rubric override rates (Phase 5)."),
) -> None:
    """Summarize workflow-friction.log: event counts, hook p95s, top rules, gate activity.

    Phase 5 flags select a single analyzer and emit a focused report. They
    are mutually exclusive with each other and with the default summary.
    """
    from writ.analysis.friction import (
        load_events, resolve_log_path, summarize, format_report, rotate_if_needed,
    )

    if rotate:
        # --rotate acts on ONE file, so it keeps the legacy single-log resolution
        # (explicit --log, then WRIT_FRICTION_LOG, then ./workflow-friction.log)
        # rather than the split streams the analysis paths now read. The stream
        # tree has its own sweep: `writ logs rotate`.
        target = resolve_log_path(log)
        rotated = rotate_if_needed(target)
        typer.echo(f"{'rotated' if rotated else 'no rotation needed'}: {target}")
        return

    # Phase 5: mutual exclusion of analyzer flags.
    phase5_flags = {
        "--rule-effectiveness": rule_effectiveness,
        "--skill-usage": skill_usage,
        "--playbook-compliance": playbook_compliance,
        "--graduation-candidates": graduation_candidates,
        "--trim-candidates": trim_candidates,
        "--quality-judge-false-positives": quality_judge_false_positives,
    }
    active = [name for name, on in phase5_flags.items() if on]
    if len(active) > 1:
        typer.echo(
            f"Error: flags {', '.join(active)} are mutually exclusive. Use one at a time.",
            err=True,
        )
        raise typer.Exit(code=2)

    if active:
        _run_phase5_report(active[0], log, since, top, json_output)
        return

    if json_output or rule:
        _render_phase4_summary(log, rule, json_output)
        return

    events = load_events(log)
    since_days = since if since > 0 else None
    summary = summarize(events, top=top, since_days=since_days)
    typer.echo(format_report(summary))


def _run_phase5_report(flag: str, log: Path | None, since: int, top: int, json_output: bool) -> None:
    """Phase 5: run the single analyzer selected by `flag` and emit its focused report.

    D-CLI-REGISTRY: per-flag (analyzer, default_since_days, headers, row->cells) live in one
    table, replacing a 6-way if/elif. default_since_days=None means the analyzer takes no
    since_days arg (graduation-candidates); `since or default` preserves the per-flag default
    when --since is 0.
    """
    from writ.analysis.friction import (
        parse_log,
        analyze_rule_effectiveness, analyze_skill_usage,
        analyze_playbook_compliance, analyze_graduation_candidates,
        analyze_trim_candidates, analyze_quality_judge_false_positives,
    )
    events = parse_log(log)
    since_days = since if since > 0 else 0
    analyzers = {
        "--rule-effectiveness": (
            analyze_rule_effectiveness, 30,
            ["rule_id", "activations", "stuck", "stick_rate", "rationalizations"],
            lambda r: [r.rule_id, r.activations, r.stuck_denials,
                       f"{r.denial_stick_rate:.2f}", r.rationalizations],
        ),
        "--skill-usage": (
            analyze_skill_usage, 60,
            ["skill_id", "loads", "completions", "completion_rate"],
            lambda r: [r.skill_id, r.loads, r.completions, f"{r.completion_rate:.2f}"],
        ),
        "--playbook-compliance": (
            analyze_playbook_compliance, 30,
            ["playbook_id", "runs", "compliant", "skip_points"],
            lambda r: [r.playbook_id, r.runs, r.compliant_runs,
                       ", ".join(r.common_skip_points)],
        ),
        "--graduation-candidates": (
            analyze_graduation_candidates, None,
            ["rule_id", "days_stable", "current", "recommended", "stick_rate"],
            lambda r: [r.rule_id, r.days_stable, r.current_tier, r.recommended_tier,
                       f"{r.denial_stick_rate:.2f}"],
        ),
        "--trim-candidates": (
            analyze_trim_candidates, 90,
            ["entity", "type", "activations", "last_seen", "recommendation"],
            lambda r: [r.entity_id, r.entity_type, r.activations_in_window,
                       r.last_activation or "-", r.recommendation],
        ),
        "--quality-judge-false-positives": (
            analyze_quality_judge_false_positives, 30,
            ["rubric", "fails", "overrides", "override_rate"],
            lambda r: [r.rubric, r.total_fails, r.overrides, f"{r.override_rate:.2f}"],
        ),
    }
    analyzer, default_days, headers, extract = analyzers[flag]
    if default_days is None:
        rows: list = analyzer(events, top=top)
    else:
        rows = analyzer(events, since_days=since_days or default_days, top=top)
    cells = [extract(r) for r in rows]

    if json_output:
        typer.echo(json.dumps([r.model_dump() for r in rows]))
    else:
        typer.echo(" | ".join(str(h) for h in headers))
        typer.echo("-" * 60)
        for row in cells:
            typer.echo(" | ".join(str(c) for c in row))


def _render_phase4_summary(log: Path | None, rule: str | None, json_output: bool) -> None:
    """Phase 4 path: Pydantic-validated events with --json / --rule filters."""
    from writ.analysis.friction import parse_log, aggregate_by_rule, aggregate_by_event
    events = parse_log(log)
    if rule:
        events = [e for e in events if e.rule_id == rule]
    payload = {
        "by_rule": aggregate_by_rule(events),
        "by_event": aggregate_by_event(events),
        "total": len(events),
    }
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"Events matching rule={rule}: {payload['total']}")
        for rid, n in payload["by_rule"].items():
            typer.echo(f"  {rid}: {n}")
        for evt, n in payload["by_event"].items():
            typer.echo(f"  event={evt}: {n}")


@app.command(name="audit-session")
def audit_session(
    session_id: str = typer.Argument(..., help="The session id to audit."),
    log: Path | None = typer.Option(
        None,
        help="Path to a single friction log. Default: the project's split "
             "audit+friction+metrics streams, including rotated archives.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON instead of text."),
) -> None:
    """Per-session timeline + summary from workflow-friction.log.

    Filters the friction log to one session_id and presents:
      - Phase progression (planning -> testing -> implementation -> complete)
      - Mode + orchestrator state
      - Rule / skill / playbook loads with counts
      - Gate denials with the offending rule_id
      - Subagent dispatches
      - Always-on bundle injections
      - Token consumption breakdown by query_source

    Structured per-session audit -- filterable, machine-parseable,
    persists across compactions and session boundaries.
    """
    import json as _json

    from writ.analysis.friction import (
        aggregate_session,
        load_events,
        render_audit_json,
        render_audit_text,
    )

    events = load_events(log)
    session_events = [e for e in events if e.get("session") == session_id]

    if not session_events:
        if json_output:
            typer.echo(_json.dumps({
                "session": session_id,
                "event_count": 0,
                "message": "no events found for this session",
            }))
        else:
            typer.echo(f"No events found for session {session_id} in {log}.")
        return

    agg = aggregate_session(session_events)
    if json_output:
        typer.echo(render_audit_json(session_id, session_events, agg))
    else:
        typer.echo(render_audit_text(session_id, session_events, agg))


@app.command(name="token-audit")
def token_audit(
    transcript: str = typer.Argument(..., help="Path to a Claude Code transcript jsonl."),
    friction: str = typer.Option(None, help="Optional workflow-friction.log for Writ attribution."),
    model: str = typer.Option("claude-opus-4-8", help="Model id for USD conversion / weights."),
    as_json: bool = typer.Option(False, "--json", help="Emit the scorecard as JSON."),
) -> None:
    """FOOTPRINT observer (WRIT-TOKEN-BLUEPRINT P0): per-session token COST from a CC transcript.

    Denominator only -- silent on trajectory/efficacy (that is the P0.5 A/B harness). Fails loud
    (exit 2) if the transcript usage schema is unrecognized, rather than emit a wrong number."""
    from writ.analysis.token_audit import (
        TokenAuditSchemaError, render_json, render_text, scorecard,
    )
    try:
        card = scorecard(transcript, friction, model)
    except TokenAuditSchemaError as e:
        typer.echo(f"[token-audit] SCHEMA CANARY FAILED: {e}", err=True)
        raise typer.Exit(2)
    except OSError as e:
        typer.echo(f"[token-audit] cannot read transcript: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(render_json(card) if as_json else render_text(card))


@app.command(name="corpus-footprint")
def corpus_footprint(
    bible_dir: str = typer.Argument(None, help="Bible dir (default: the skill's bible/)."),
    top: int = typer.Option(20, help="Top-N bloat cut-candidates to rank."),
    domain: str = typer.Option(None, help="Restrict to one domain."),
    include_methodology: bool = typer.Option(False, "--include-methodology", help="Also scan methodology/."),
    as_json: bool = typer.Option(False, "--json", help="Emit the scorecard as JSON."),
) -> None:
    """No-API corpus footprint: rank per-rule bloat (WASTE) cut-candidates. Proposes, never applies."""
    import os
    from writ.analysis import corpus_footprint as cf
    from writ.analysis.token_audit import render_json
    if not bible_dir:
        bible_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bible")
    try:
        card = cf.scorecard(bible_dir, top=top, domain=domain, include_methodology=include_methodology)
        typer.echo(render_json(card) if as_json else cf.render_text(card))
    except cf.CorpusFootprintError as e:
        typer.echo(f"CORPUS CANARY FAILED: {e}", err=True)
        raise typer.Exit(2)
    except OSError as e:
        typer.echo(f"corpus-footprint: {e}", err=True)
        raise typer.Exit(1)


@app.command(name="efficacy-ab")
def efficacy_ab(
    suite_dir: str = typer.Argument(..., help="Task-suite dir (e.g. tests/efficacy_suite)."),
    variant_a: str = typer.Option("writ-on", help="Variant A profile name."),
    variant_b: str = typer.Option("writ-off", help="Variant B profile name."),
    reps: int = typer.Option(1, help="Reps per (task,variant). reps=1 is a single draw, no verdict."),
    model: str = typer.Option("claude-opus-4-8", help="Model id for cost weighting."),
    judge: bool = typer.Option(False, "--judge", help="Enable the LLM-judge fallback (spends)."),
    live: bool = typer.Option(False, "--live", help="Actually spawn real claude runs (SPENDS API budget)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """NUMERATOR harness: run a matched-task A/B and score cost + defect-caught.
    Default is a DRY RUN (no spawn, prints the plan + an estimate). --live opts into real spend."""
    import tempfile

    from writ.analysis import efficacy_ab as ab
    from writ.analysis import variants as V
    from writ.analysis.token_audit import TokenAuditSchemaError, render_json
    try:
        tasks = ab.load_suite(suite_dir)
        plan = [(t, vn) for t in tasks for vn in (variant_a, variant_b) for _ in range(reps)]
        if not live:
            typer.echo(f"DRY RUN: {len(plan)} runs ({len(tasks)} tasks x 2 variants x {reps} reps). "
                       f"Est ~${0.30 * len(plan):.2f}+ (trivial-run floor). Re-run with --live to spend.")
            raise typer.Exit(0)
        judge_fn = ab.make_judge(model) if judge else None
        scored = []
        for t, vn in plan:
            prof = V.materialize_variant(vn, tempfile.mkdtemp(prefix="writ-ab-"))
            with tempfile.TemporaryDirectory(prefix="writ-ab-run-") as wd:
                res = ab.run_task(t, prof, wd)
                scored.append(ab.score_run(t, res, vn, prof["friction_log"], judge_fn, model))
        report = ab.compare_arms(scored, reps_floor=5)
        typer.echo(render_json(report) if as_json else ab.render_text(report))
    except TokenAuditSchemaError as e:
        typer.echo(f"SCHEMA CANARY FAILED: {e}", err=True)
        raise typer.Exit(2)
    except OSError as e:
        typer.echo(f"efficacy-ab: {e}", err=True)
        raise typer.Exit(1)
    except ab.EfficacyError as e:
        typer.echo(f"efficacy-ab harness error: {e}", err=True)
        raise typer.Exit(3)


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
    path: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Path to Markdown source directory."),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated node_types to import (e.g. 'Rule', 'Skill,Playbook'). Default: import all.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse and validate without writing to the database.",
    ),
    no_export: bool = typer.Option(
        False,
        "--no-export",
        help="Skip the auto-export round-trip (regenerating rules.md from the graph). "
        "For setup/fixture imports that only need the graph populated, not the source rewritten.",
    ),
    compress: bool = typer.Option(
        False,
        "--compress/--no-compress",
        help="After a successful ingest, regenerate the graph-only Abstraction layer "
        "(clusters non-mandatory rules). Maintainer-only: needs the [fallback] "
        "sentence-transformers dep; if absent, warns and continues (ingest still succeeds). "
        "Default OFF (the abstraction layer is a regenerable materialized view).",
    ),
) -> None:
    """Import bible content (Rules + methodology) into the graph. Validates schema. Triggers export."""
    from writ.graph.methodology_ingest import (
        KNOWN_NODE_TYPES,
        finish_import,
        ingest_path,
    )

    # Parse --only CSV.
    parsed_only: set[str] | None = None
    if only is not None:
        parsed_only = {tok.strip() for tok in only.split(",") if tok.strip()}
        if not parsed_only:
            parsed_only = None

    # Validate every --only token before any DB work; surface a clean error
    # with the unknown token AND the sorted list of valid types (API-ERROR-002).
    if parsed_only:
        unknown = sorted(parsed_only - KNOWN_NODE_TYPES)
        if unknown:
            valid = sorted(KNOWN_NODE_TYPES)
            typer.echo(
                f"Error: unknown --only type(s): {', '.join(unknown)}. "
                f"Valid types: {', '.join(valid)}.",
                err=True,
            )
            raise typer.Exit(code=2)

    async def _run() -> int:
        async with _writ_db() as db:
            report = await ingest_path(
                path, db, only=parsed_only, dry_run=dry_run,
            )
            typer.echo(report.render())
            for err in report.errors:
                typer.echo(str(err), err=True)

            # is_default_root is computed here (from the CLI default constant)
            # and passed to finish_import so the ingest library stays free of
            # the CLI default. It gates the auto-export in finish_import: on a
            # subdirectory import the exporter would write the WHOLE graph back
            # through a file-location lookup scoped to that subdir and create
            # bogus duplicates.
            is_default_root = (
                path.resolve() == Path(DEFAULT_BIBLE_DIR).resolve()
            )
            result = await finish_import(
                report,
                db,
                path,
                dry_run=dry_run,
                no_export=no_export,
                compress=compress,
                parsed_only=parsed_only,
                is_default_root=is_default_root,
            )
            if result["exported"] is not None:
                typer.echo(
                    f"Exported {result['exported']['rules_exported']} rules to {path}"
                )
            if result["compress_import_error"] is not None:
                typer.echo(
                    _compress_dep_error_message(
                        "import-markdown --compress",
                        result["compress_import_error"],
                        prefix="WARNING",
                    ),
                    err=True,
                )
            elif result["compressed"] is not None:
                typer.echo(
                    f"Compressed: created {len(result['compressed']['abstractions'])} "
                    f"abstractions"
                )
            elif result["materialized"] is not None:
                typer.echo(
                    f"Materialized {result['materialized']} abstractions from artifact"
                )
            return 1 if report.errors else 0
    exit_code = asyncio.run(_run())
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command()
def prune(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List parity-violation candidates (graph nodes absent from markdown) without deleting.",
    ),
    bible_dir: Path = typer.Option(
        Path(DEFAULT_BIBLE_DIR),
        "--bible-dir",
        help="Markdown corpus to compare the graph against.",
    ),
) -> None:
    """Detect graph nodes absent from the bible markdown (parity violations).

    With --dry-run, prints each flagged node id and issues no DELETE. The
    dry-run path never mutates the graph.
    """
    from writ.graph.integrity import IntegrityChecker

    async def _run() -> None:
        async with _writ_db() as db:
            checker = IntegrityChecker(db._driver, db._database)
            checker._default_bible_dir = bible_dir
            violations = await checker.detect_parity_violations()

            if not violations:
                typer.echo("No parity violations found.")
                return

            typer.echo(f"Parity violations ({len(violations)}):")
            for v in violations:
                node_id = v["id"] if isinstance(v, dict) else v
                typer.echo(f"  {node_id}")

            if dry_run:
                typer.echo("\nDry run: no nodes removed.")
    asyncio.run(_run())


@app.command(name="reconcile")
def reconcile(
    bible_dir: Path = typer.Option(
        Path(DEFAULT_BIBLE_DIR),
        "--bible-dir",
        help="Markdown corpus (source-of-truth) to reconcile the graph against.",
    ),
    project: str = typer.Option(
        "writ",
        "--project",
        help="Project scope to reconcile (M.1: never touches another project's nodes/edges).",
    ),
) -> None:
    """Make the graph match the source-of-truth: delete stale nodes/edges and clear stale props.

    Thin wrapper over the library `reconcile`. Imports the bible (upsert), then
    removes graph nodes/edges ABSENT from the source (exempting graph-first
    proposed/graduation_pending nodes) and clears stale managed props. This is the
    command `writ validate` points at when it reports edge-parity or graph-only drift.
    """
    from writ.graph.methodology_ingest import reconcile as _reconcile

    async def _run() -> None:
        async with _writ_db() as db:
            try:
                result = await _reconcile(bible_dir, db, project=project)
            except ValueError as exc:
                # The library reconcile refuses to run against an empty oracle
                # (would wipe the corpus). Surface it as a clean exit, not a
                # traceback -- mirrors add/edit/propose error handling.
                typer.echo(f"ERROR: reconcile aborted -- {exc}", err=True)
                raise typer.Exit(code=1) from exc
            deleted_nodes = result["deleted_nodes"]
            deleted_edges = result["deleted_edges"]
            cleared_props = result["cleared_props"]
            cleared_count = sum(len(v) for v in cleared_props.values())
            typer.echo(
                f"Reconciled {bible_dir} (project={project}): "
                f"deleted {len(deleted_nodes)} node(s), "
                f"deleted {len(deleted_edges)} edge(s), "
                f"cleared {cleared_count} prop(s) on {len(cleared_props)} node(s)."
            )
            for nid in deleted_nodes:
                typer.echo(f"  - node {nid}")
            for etype, src, tgt in deleted_edges:
                typer.echo(f"  - edge {etype} {src} -> {tgt}")
            for nid, props in cleared_props.items():
                typer.echo(f"  - props {nid}: {', '.join(props)}")
    asyncio.run(_run())


@app.command()
def validate(
    review_confidence: bool = typer.Option(
        False, "--review-confidence", help="List rules at migration default confidence."
    ),
    benchmark: bool = typer.Option(False, "--benchmark", help="Report integrity check duration."),
    bible_dir: Path | None = typer.Option(
        None,
        "--bible-dir",
        help="Markdown corpus to check graph/markdown parity against.",
    ),
) -> None:
    """Run integrity checks: conflicts, orphans, staleness, redundancy."""
    import time

    from writ.graph.integrity import IntegrityChecker

    async def _run() -> int:
        async with _writ_db() as db:
            checker = IntegrityChecker(db._driver, db._database)
            start = time.perf_counter()
            findings = await checker.run_all_checks(bible_dir=bible_dir)
            elapsed_ms = (time.perf_counter() - start) * 1000

            from writ.graph.validate_report import render_findings

            stdout_lines, stderr_lines = render_findings(findings)
            for line in stdout_lines:
                typer.echo(line)
            for line in stderr_lines:
                typer.echo(line, err=True)

            if review_confidence:
                defaults = await checker.detect_confidence_defaults()
                typer.echo(f"\nRules at default confidence ({len(defaults)}):")
                for d in defaults:
                    typer.echo(f"  {d}")

            if benchmark:
                typer.echo(f"\nIntegrity check completed in {elapsed_ms:.1f}ms")

            # #7C: static hook-delivery lint (not graph-based). WARNING-only --
            # surfaces injectors whose rules cannot reach the model; does not
            # affect the integrity exit code (the C1 lesson: prove clean first).
            from writ.hooks_lint import lint_hooks
            plugin_root = Path(__file__).resolve().parents[1]
            hooks_json = plugin_root / "hooks" / "hooks.json"
            if hooks_json.exists():
                hl = lint_hooks(hooks_json, plugin_root)
                inert = [f for f in hl if f["severity"] == "inert"]
                review = [f for f in hl if f["severity"] == "review"]
                if hl:
                    typer.echo(
                        f"\nHook delivery lint (#7C, WARNING) -- "
                        f"{len(inert)} inert, {len(review)} review:"
                    )
                    for f in hl:
                        typer.echo(
                            f"  [{f['severity']}] {f['event']}:{f['matcher'] or '*'} "
                            f"{f['script']}"
                        )
                        typer.echo(f"      {f['detail']}")

            if findings["exit_code"] == 0:
                typer.echo("\nAll checks passed.")
            else:
                typer.echo("\nFindings detected.")

            return findings["exit_code"]
    code = asyncio.run(_run())
    raise typer.Exit(code=code)


@app.command()
def add() -> None:
    """Add a new rule to the graph with relationship suggestion and validation."""
    from writ.authoring import (
        RuleIdCollisionError,
        build_rule_dict,
        check_id_collision,
        check_redundancy,
        finalize_conflict_and_export,
        suggest_relationships,
    )
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

        rule_data = build_rule_dict(
            rule_id=rule_id, domain=domain, severity=severity, scope=scope,
            trigger=trigger, statement=statement, violation=violation,
            pass_example=pass_example, enforcement=enforcement, rationale=rationale,
        )

        async with _writ_db() as db:
            # ID collision check runs before schema validation so an author
            # re-using an existing rule_id fails fast without spending time
            # on the rest of the gate. MERGE in create_rule would silently
            # update the existing node otherwise.
            try:
                await check_id_collision(rule_id, db)
            except RuleIdCollisionError as e:
                typer.echo(f"rule_id already exists: {e.rule_id}")
                typer.echo(f"  existing statement: {e.existing.get('statement', '')[:100]}")
                typer.echo("Use `writ edit` to modify, or choose a different rule_id.")
                raise typer.Exit(code=1)

            # INV-6: Validate against schema before any graph write.
            try:
                Rule(**rule_data)
            except Exception as e:
                typer.echo(f"Validation error: {e}")
                raise typer.Exit(code=1)

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

            # Write rule to graph. A cli-add rule is graph-first with no markdown home yet
            # (0.10), so reconcile must not delete it: source_origin='graph-authored'.
            await db.create_rule(rule_data, source_origin="graph-authored")
            typer.echo(f"\nCreated rule: {rule_id}")

            # Offer to create edges for accepted suggestions.
            if suggestions:
                from writ.graph.db import CORPUS_EDGE_TYPES
                edge_types = sorted(CORPUS_EDGE_TYPES)
                for s in suggestions:
                    create = typer.confirm(f"Create edge to {s['rule_id']}?", default=False)
                    if create:
                        edge_type = typer.prompt(
                            f"Edge type ({'/'.join(edge_types)})",
                            default="RELATED_TO",
                        )
                        if edge_type in CORPUS_EDGE_TYPES:
                            await db.create_edge(edge_type, rule_id, s["rule_id"])
                            typer.echo(f"  Created {edge_type} -> {s['rule_id']}")
                        else:
                            typer.echo(f"  Unknown edge type: {edge_type}, skipped.")

            # Conflict check (after edges created) + auto-export (Phase 7).
            result = await finalize_conflict_and_export(
                db, cache, rule_id, bible_dir=DEFAULT_BIBLE_DIR
            )
            if result["conflicts"]:
                typer.echo("\nConflict warning:")
                for c in result["conflicts"]:
                    typer.echo(f"  CONFLICTS_WITH {c['rule_id']}")
            typer.echo(f"\nExported {result['rules_exported']} rules to {result['export_dir']}")
    asyncio.run(_run())


@app.command()
def edit(
    rule_id: str = typer.Argument(..., help="ID of the rule to edit."),
) -> None:
    """Edit an existing rule in the graph."""
    from writ.authoring import (
        check_redundancy,
        finalize_conflict_and_export,
        suggest_relationships,
    )
    from writ.graph.schema import Rule
    from writ.retrieval.pipeline import build_pipeline
    from writ.retrieval.traversal import AdjacencyCache

    async def _run() -> None:
        async with _writ_db() as db:
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

            # INV-7: MERGE = idempotent update. PRESERVE the node's existing source_origin
            # (0.10): editing a node must NOT change how it entered the graph -- forcing
            # 'ingest' here would flip a graph-authored node and re-introduce the deletion
            # race. updated carries it from `existing`; default 'ingest' for pre-0.10 nodes.
            await db.create_rule(updated, source_origin=updated.get("source_origin", "ingest"))
            typer.echo(f"\nUpdated rule: {rule_id}")

            # Offer edges.
            if suggestions:
                for s in suggestions:
                    create = typer.confirm(f"Create edge to {s['rule_id']}?", default=False)
                    if create:
                        edge_type = typer.prompt("Edge type", default="RELATED_TO")
                        await db.create_edge(edge_type, rule_id, s["rule_id"])
                        typer.echo(f"  Created {edge_type} -> {s['rule_id']}")
                        # 0.10-F: an edge created here lives only in the graph, not
                        # in the markdown source. For a source-backed node,
                        # `writ reconcile` will delete it (live-minus-oracle). Warn
                        # so the author declares it in the source to persist it.
                        if updated.get("source_origin", "ingest") != "graph-authored":
                            typer.echo(
                                f"  WARNING: {rule_id} is source-backed; this edge is "
                                f"graph-only and `writ reconcile` will remove it. Declare "
                                f"it in the bible source (front-matter `edges:`) to persist."
                            )

            # Conflict check + auto-export (Phase 7).
            result = await finalize_conflict_and_export(
                db, cache, rule_id, bible_dir=DEFAULT_BIBLE_DIR
            )
            if result["conflicts"]:
                typer.echo("\nConflict warning:")
                for c in result["conflicts"]:
                    typer.echo(f"  CONFLICTS_WITH {c['rule_id']}")
            typer.echo(f"\nExported {result['rules_exported']} rules to {result['export_dir']}")
    asyncio.run(_run())


@app.command()
def export(
    output: Path = typer.Argument(Path(DEFAULT_BIBLE_DIR), help="Output directory for generated Markdown."),
) -> None:
    """Regenerate Markdown from graph. Overwrites output directory."""
    from writ.export import export_rules_to_markdown

    async def _run() -> None:
        async with _writ_db() as db:
            result = await export_rules_to_markdown(db, output)
            typer.echo(f"Exported {result['rules_exported']} rules to {output}")
    asyncio.run(_run())


def _compress_dep_error_message(context: str, exc: Exception, prefix: str = "ERROR") -> str:
    """Actionable message when sentence-transformers (the [fallback] dep the
    compression pipeline needs) is missing. Shared by `writ compress` (prefix
    ERROR, fatal) and the `import-markdown --compress` graceful-degradation
    path (prefix WARNING, non-fatal)."""
    return (
        f"{prefix}: {context} requires the sentence-transformers "
        f"library, which could not be imported ({type(exc).__name__}: "
        f"{exc}).\n"
        "\n"
        "Production installs deliberately exclude this library (see "
        "pyproject.toml: it lives in the [fallback] extras group, "
        "not core dependencies). Install it with:\n"
        "    pip install -e '.[fallback]'\n"
        "Then re-run with the abstraction step."
    )


@app.command(name="export-cypher")
def export_cypher(
    output: Path = typer.Argument(
        Path("writ-corpus.cypher"), help="Output file for the Cypher dump script."
    ),
) -> None:
    """Dump the whole graph as a portable Cypher replay script."""
    from writ.graph.dump import render_cypher_dump

    async def _run() -> None:
        async with _writ_db() as db:
            nodes = await db.get_all_nodes_for_dump()
            edges = await db.get_all_edges_cross_type()
            script = render_cypher_dump(nodes, edges)
            output.write_text(script)
            typer.echo(f"Exported {len(nodes)} nodes and {len(edges)} edges to {output}")

    asyncio.run(_run())


@app.command(name="import-cypher")
def import_cypher(
    input_file: Path = typer.Argument(
        Path("writ-corpus.cypher"), help="Cypher dump script to replay."
    ),
) -> None:
    """Rebuild the graph from a Cypher dump script produced by export-cypher."""
    from writ.graph.dump import import_cypher_dump

    async def _run() -> None:
        async with _writ_db() as db:
            result = await import_cypher_dump(db, input_file.read_text())
            typer.echo(f"Ran {result['statements_run']} statements from {input_file}")

    asyncio.run(_run())


@app.command()
def compress() -> None:
    """Cluster rules into abstraction nodes for compressed retrieval."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError as exc:
        # Approach C (Finding D): sentence-transformers moved out of
        # core deps because the production daemon does not import it.
        # `writ compress` is a maintainer-only command that regenerates
        # abstraction nodes via clustering, and that pipeline uses the
        # SentenceTransformer model. Surface the missing dep as an
        # actionable typer-style error rather than a bare ImportError
        # traceback; mirrors the shape of the ONNX-unavailable error
        # in writ/retrieval/pipeline.py (commit dae679a).
        typer.echo(_compress_dep_error_message("writ compress", exc), err=True)
        raise typer.Exit(code=1) from exc

    from writ.compression.abstractions import run_compression

    async def _run() -> None:
        async with _writ_db() as db:
            result = await run_compression(db)
            if result["chosen"] is None:
                typer.echo("No domain rules to cluster.")
                raise typer.Exit(code=0)

            typer.echo(f"\nCreated {len(result['abstractions'])} abstractions")
            typer.echo(f"Ungrouped rules: {len(result['ungrouped'])}")
            typer.echo(f"Average compression ratio: {result['avg_ratio']:.1f}x")
    asyncio.run(_run())


@app.command(name="role-prompt")
def role_prompt(
    role: str = typer.Argument(..., help="Subagent role name (writ-explorer, writ-planner, etc.) or ROL-* id."),
) -> None:
    """Print the graph-canonical prompt template for a SubagentRole.

    Phase 3 Section 8.2 release blocker: `writ review prompt <role>` returns
    graph-canonical text. Implemented as `writ role-prompt <role>` to avoid
    collision with the existing `writ review <rule_id>` command. The graph
    is the canonical source (plan Section 8.1 deliverable 2); .claude/agents
    files are exported from the graph.
    """
    import asyncio

    async def _fetch() -> None:
        async with _writ_db() as db:
            rec = await db.get_subagent_role(role)
        if rec is None:
            typer.echo(f"SubagentRole '{role}' not found in graph.", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"# {rec['role_id']}  (name={rec['name']}, model={rec['model_preference']})")
        typer.echo("")
        typer.echo(rec["prompt_template"])
    asyncio.run(_fetch())


@app.command()
def migrate() -> None:
    """One-time migration of existing rules into graph.

    Backward-compat shim: delegates to `writ import-markdown` with default args.
    """
    from writ.graph.methodology_ingest import ingest_path

    path = Path(DEFAULT_BIBLE_DIR)

    async def _run() -> int:
        async with _writ_db() as db:
            report = await ingest_path(path, db, only=None, dry_run=False)
            typer.echo(report.render())
            for err in report.errors:
                typer.echo(str(err), err=True)
            return 1 if report.errors else 0
    code = asyncio.run(_run())
    if code:
        raise typer.Exit(code=code)


@app.command()
def query(
    query_text: str = typer.Argument(..., help="Natural language query for rule retrieval."),
    domain: str | None = typer.Option(None, help="Filter by domain."),
    budget: int | None = typer.Option(None, help="Context budget in tokens."),
    local: bool = typer.Option(False, "--local", help="Bypass the server and build the pipeline in-process."),
) -> None:
    """CLI rule query for testing retrieval quality.

    Routes through the running `writ serve` instance when available, so repeat
    queries hit the warm encoder cache. Falls back to in-process pipeline build
    if the server is unreachable, or when --local is passed.
    """
    import httpx

    def _render(result: dict) -> None:
        typer.echo(f"Mode: {result['mode']} | Candidates: {result['total_candidates']} | Latency: {result['latency_ms']}ms\n")
        for i, rule in enumerate(result["rules"], 1):
            typer.echo(f"  {i}. [{rule['score']}] {rule['rule_id']}")
            if "statement" in rule:
                typer.echo(f"     {rule['statement'][:100]}")
            typer.echo()

    if not local:
        payload: dict[str, object] = {"query": query_text}
        if domain is not None:
            payload["domain"] = domain
        if budget is not None:
            payload["budget_tokens"] = budget
        try:
            resp = httpx.post(
                f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/query",
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            typer.echo(f"Querying via server: {query_text}\n")
            _render(resp.json())
            return
        except httpx.HTTPError as exc:
            # Any server-path failure (unreachable, timeout, or an error status
            # from raise_for_status) falls back to the in-process pipeline.
            typer.echo(
                f"Server query failed ({type(exc).__name__}); falling back to "
                "in-process pipeline. Start the server with: writ serve\n"
            )

    from writ.retrieval.pipeline import (
        RULE_INJECTION_ABSTENTION_THRESHOLD,
        build_pipeline,
    )

    async def _run() -> None:
        async with _writ_db() as db:
            typer.echo("Building pipeline (loading indexes)...")
            # Mirror the daemon rule-injection path: enable the S4 abstention gate.
            pipeline = await build_pipeline(
                db, abstention_threshold=RULE_INJECTION_ABSTENTION_THRESHOLD,
            )
            typer.echo(f"Querying: {query_text}\n")
            result = pipeline.query(
                query_text=query_text,
                domain=domain,
                budget_tokens=budget,
            )
            _render(result)
    asyncio.run(_run())


@app.command()
def feedback(
    rule_id: str = typer.Argument(..., help="Rule ID to record feedback for."),
    signal: str = typer.Argument(..., help="Signal: 'positive' or 'negative'."),
) -> None:
    """Record positive or negative feedback for a rule (hook integration)."""

    if signal not in ("positive", "negative"):
        typer.echo(f"Invalid signal: {signal}. Must be 'positive' or 'negative'.")
        raise typer.Exit(code=1)

    async def _run() -> None:
        async with _writ_db() as db:
            if signal == "positive":
                found = await db.increment_positive(rule_id)
            else:
                found = await db.increment_negative(rule_id)

            if not found:
                typer.echo(f"Rule not found: {rule_id}")
                raise typer.Exit(code=1)
            typer.echo(f"Recorded {signal} feedback for {rule_id}")
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
    from writ.authoring import build_rule_dict
    from writ.gate import propose_rule
    from writ.origin_context import DEFAULT_DB_PATH
    from writ.retrieval.pipeline import build_pipeline

    candidate = build_rule_dict(
        rule_id=rule_id, domain=domain, severity=severity, scope=scope,
        trigger=trigger, statement=statement, violation=violation,
        pass_example=pass_example, enforcement=enforcement, rationale=rationale,
    )

    async def _run() -> None:
        async with _writ_db() as db:
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
    from writ import authoring

    async def _run() -> None:
        async with _writ_db() as db:
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
                # Guard before the confirm prompt so a non-provisional rule
                # errors + exits(1) WITHOUT prompting. assert_ai_provisional is
                # the single-source legality check (DRY-DUP-002); we keep the
                # exact 'human'-fallback message + prompt ordering here.
                try:
                    authoring.assert_ai_provisional(existing, rule_id, "promote")
                except authoring.IllegalAuthorityTransitionError:
                    typer.echo(f"Cannot promote: {rule_id} has authority '{existing.get('authority', 'human')}'")
                    raise typer.Exit(code=1)
                confirm = typer.confirm(f"Promote {rule_id} to ai-promoted?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await authoring.promote(db, rule_id, existing)
                typer.echo(f"Promoted: {rule_id} (authority: ai-promoted, confidence: peer-reviewed)")
                return

            if reject:
                try:
                    authoring.assert_ai_provisional(existing, rule_id, "reject")
                except authoring.IllegalAuthorityTransitionError:
                    typer.echo(f"Cannot reject: {rule_id} has authority '{existing.get('authority', 'human')}'")
                    raise typer.Exit(code=1)
                confirm = typer.confirm(f"Delete {rule_id} from graph?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await authoring.reject(db, rule_id, existing)
                typer.echo(f"Rejected and deleted: {rule_id}")
                return

            if downweight:
                confirm = typer.confirm(f"Downweight {rule_id} to speculative confidence?")
                if not confirm:
                    typer.echo("Cancelled.")
                    return
                await authoring.downweight(db, rule_id)
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


# --- Decision-memory Phase 1d: git-hooks sub-app -------------------------------
# Explicit control + opt-out + repair for the git hooks (auto-install is the
# default via the CwdChanged seam). `install` / `uninstall` drive the installer
# against a repo; `bootstrap` registers the writ project bound to its remote_url
# so the first live auto-register REUSEs the name 'writ'.

git_hooks_app = typer.Typer(
    name="git-hooks",
    help="Install, uninstall, or bootstrap the Writ git hooks for a repo.",
)
app.add_typer(git_hooks_app, name="git-hooks")


@git_hooks_app.command(name="install")
def git_hooks_install(
    repo: str = typer.Option(".", "--repo", help="Repo to install the hooks into."),
) -> None:
    """Install the Writ post-commit git hook into a repo (removes the retired prepare-commit-msg block)."""
    from writ.session.git_hooks import install_git_hooks
    install_git_hooks(repo)
    typer.echo(f"Installed Writ git hooks in {repo}")


@git_hooks_app.command(name="uninstall")
def git_hooks_uninstall(
    repo: str = typer.Option(".", "--repo", help="Repo to remove the hooks from."),
) -> None:
    """Remove the Writ git-hook block from a repo (preserving other content)."""
    from writ.session.git_hooks import uninstall_git_hooks
    uninstall_git_hooks(repo)
    typer.echo(f"Removed Writ git hooks from {repo}")


@git_hooks_app.command(name="bootstrap")
def git_hooks_bootstrap(
    repo: str = typer.Option(".", "--repo", help="The writ repo to bootstrap."),
) -> None:
    """Register the writ project bound to its remote_url before first auto-register.

    Binds the name 'writ' to the repo's origin remote_url so the
    ensure_project_registered REUSE lookup keeps the writ project named 'writ'
    rather than minting a normalized-remote name on first auto-register.
    """
    from writ.session.git_identity import derive_project_identity

    repo_root, remote_url, _ = derive_project_identity(repo)

    async def _run() -> None:
        async with _writ_db() as db:
            await db.create_project("writ", repo_root, DEFAULT_BIBLE_DIR, remote_url)
            typer.echo(
                f"Bootstrapped writ project at {repo_root} "
                f"(remote_url={remote_url})"
            )
    asyncio.run(_run())


# --- Decision-memory Phase 1e: pr sub-app --------------------------------------
# `writ pr sync` is the single trigger that surfaces the captured per-file reasons
# as one file-level comment inside the Bitbucket PR review. It runs synchronously
# (asyncio.run), reads neither local git diffs nor commit hashes for its file set
# (the PR diffstat is the source), and posts exactly one sync per command.

from writ.config import get_bitbucket_email, get_bitbucket_token
from writ.session.bitbucket_client import BitbucketClient
from writ.session.git_identity import derive_project_identity
from writ.session.pr_comments import (
    sync_pr_comments,
    render_commit_notes as _render_commit_notes,
    write_commit_notes as _write_commit_notes,
)
from writ.session.registration import ensure_project_registered
from writ.session.remote_parse import parse_bitbucket_remote

pr_app = typer.Typer(
    name="pr",
    help="Surface captured per-file reasons as comments in the PR review.",
)
app.add_typer(pr_app, name="pr")


@app.command(name="harvest")
def harvest_cmd(
    repo: str = typer.Option(".", "--repo", help="Repo to harvest."),
    since: str | None = typer.Option(
        None, "--since",
        help="Harvest commits in <since>..HEAD (default: all reachable from HEAD).",
    ),
) -> None:
    """Harvest git commits + transcript plans into decision-memory records."""
    from writ.session.harvester import _resolve_rev, harvest as run_harvest

    # Validate --since up front (tight scope): only the rev-check's ValueError maps to
    # a --since parameter error, so an internal ValueError from harvest is never
    # mislabeled as a bad --since. Fails fast before the db is opened.
    if since:
        try:
            _resolve_rev(repo, since)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--since") from exc

    async def _run() -> None:
        async with _writ_db() as db:
            stats = await run_harvest(db, repo, since=since)
            if stats["project"] is None:
                typer.echo("Not a git repo; nothing harvested.")
                raise typer.Exit(code=1)
            typer.echo(
                f"Harvested project={stats['project']}: "
                f"commits={stats['commits']} filechanges={stats['filechanges']} "
                f"decisions={stats['decisions']} "
                f"(plan_reason={stats['with_plan_reason']} "
                f"fallback={stats['fallback_reason']})"
            )

    asyncio.run(_run())


@app.command(name="recall")
def recall_cmd(
    branch: str = typer.Argument("", help="Branch (accepted for symmetry; recall is project-scoped)."),
    repo: str = typer.Option(".", "--repo", help="Repo whose project to recall."),
    full: bool = typer.Option(False, "--full/--no-full", help="Print full decisions, not just the briefing."),
    limit: int = typer.Option(20, "--limit", help="Max decisions to display under --full."),
) -> None:
    """Read back the project's recent rule-grounded decisions from memory."""
    import os
    from writ.session.recall import compile_recall

    async def _run() -> None:
        async with _writ_db() as db:
            project = await db.resolve_project_for_cwd(os.path.abspath(repo))
            if not project:
                # No project, not "writ": the resolver stopped defaulting an
                # unregistered repo to this one. Say so plainly instead of printing
                # "no decisions captured", which is a different fact and would send
                # someone hunting for missing decisions in a repo Writ has never
                # been told about. Exit 0: nothing failed, there is just nothing
                # registered to recall.
                typer.echo(
                    f"[Writ recall: {os.path.abspath(repo)} is not registered as a "
                    f"project, so there are no decisions to recall. Register it by "
                    f"running a Writ session in it (or `writ hooks install`) and "
                    f"capturing a commit.]"
                )
                return
            payload = await compile_recall(db, project, full=full)
            briefing = payload.get("briefing") or ""
            if briefing:
                typer.echo(briefing)
            else:
                typer.echo("[Writ recall: no decisions captured for this project yet.]")
            if full:
                for d in payload.get("decisions", [])[:limit]:
                    typer.echo("")
                    typer.echo(f"# {d.get('title', '(untitled)')} ({d.get('decision_id')})")
                    if d.get("rationale"):
                        typer.echo(f"  rationale: {d['rationale']}")
                    for rid, stmt in (d.get("rule_statements") or {}).items():
                        typer.echo(f"  {rid}: {stmt}")

    asyncio.run(_run())


def _current_branch(repo: str) -> str:
    """Return the current branch via `git rev-parse --abbrev-ref HEAD` for `repo`.

    Returns "" when git cannot resolve a branch (no repo, detached HEAD). The
    caller surfaces a clean "no open PR" message rather than a traceback.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@pr_app.command(name="sync")
def pr_sync(
    repo: str = typer.Option(".", "--repo", help="Repo whose PR to sync."),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch to find the open PR for (default: current)."
    ),
    pr_id: int | None = typer.Option(
        None, "--pr-id", help="Target a specific PR id and skip find_open_pr."
    ),
) -> None:
    """Post the captured per-file reasons as file-level comments on the PR review."""
    email = get_bitbucket_email()
    token = get_bitbucket_token()
    if not email or not token:
        typer.echo(
            "Bitbucket email/token not set in writ.toml [bitbucket]; "
            "cannot post PR comments."
        )
        raise typer.Exit(code=1)

    repo_root, remote_url, _ = derive_project_identity(repo)
    parsed = parse_bitbucket_remote(remote_url)
    if parsed is None:
        typer.echo(
            "No Bitbucket remote found for this repo; cannot post PR comments."
        )
        raise typer.Exit(code=1)
    workspace, repo_slug = parsed

    resolved_branch = branch or _current_branch(repo_root)

    async def _run() -> None:
        async with _writ_db() as db:
            project = await ensure_project_registered(db, cwd=repo_root)
            host = BitbucketClient(email, token)
            try:
                target_pr = pr_id
                if target_pr is None:
                    target_pr = await host.find_open_pr(
                        workspace, repo_slug, resolved_branch
                    )
                if target_pr is None:
                    typer.echo(f"No open PR found for branch {resolved_branch}.")
                    raise typer.Exit(code=1)
                counts = await sync_pr_comments(
                    host, db, workspace, repo_slug, project, target_pr
                )
                typer.echo(
                    f"PR sync complete: created={counts.get('created', 0)} "
                    f"updated={counts.get('updated', 0)} "
                    f"unchanged={counts.get('unchanged', 0)} "
                    f"skipped_no_reason={counts.get('skipped_no_reason', 0)}"
                )
                notes_by_commit = await _render_commit_notes(
                    db, host, workspace, repo_slug, project, target_pr
                )
                notes_written = _write_commit_notes(repo_root, notes_by_commit)
                if notes_written:
                    typer.echo(
                        f"Notes: wrote {notes_written} commit notes (ref writ-decisions), "
                        f"pushed to origin"
                    )
                    typer.echo(
                        "Read them: git log --notes=writ-decisions  |  "
                        "Fetch on another clone: "
                        "git fetch origin "
                        "\"refs/notes/writ-decisions:refs/notes/writ-decisions\""
                    )
            finally:
                await host.close()

    asyncio.run(_run())


@app.command(name="doctor")
def doctor(
    fix: bool = typer.Option(
        False, "--fix/--no-fix", help="Apply the safe auto-repairs for fixable, non-ok checks."
    ),
    net: bool = typer.Option(
        False, "--net/--no-net", help="Allow the single outbound call (Bitbucket live auth)."
    ),
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit a machine-readable JSON list of results."
    ),
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help=(
            "Read this session's cache in the mode/gate check instead of the current "
            "session's ($CLAUDE_SESSION_ID, then basename($CLAUDE_JOB_DIR)). Without it, "
            "an unresolvable session reports 'no session'; nothing is scanned or guessed."
        ),
    ),
    repo: str = typer.Option(
        ".", "--repo", help="Repo whose post-commit hook is checked."
    ),
) -> None:
    """Run the operability self-diagnostic; exit non-zero if any check fails.

    A battery of 10 read-only checks against the live Writ install. `--fix`
    applies only the safe auto-repairs (restart daemon, kill stale orphan,
    install git hook, recreate PATH symlink, reconcile corpus drift). `--net`
    gates the single outbound Bitbucket auth ping.
    """
    from writ.session import doctor as doctor_mod

    opts = doctor_mod.DoctorOptions(net=net, session_id=session_id, repo=repo)
    results = doctor_mod.run_all_checks(opts)

    if fix:
        for r in results:
            if r.status != "ok" and r.fixable and r.fix is not None:
                r.fix()
                if not json_output:
                    typer.echo(f"repaired: {r.name}")
        results = doctor_mod.run_all_checks(opts)

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "status": r.status,
                        "detail": r.detail,
                        "fixable": r.fixable,
                    }
                    for r in results
                ]
            )
        )
    else:
        name_width = max(len("NAME"), max((len(r.name) for r in results), default=0))
        status_width = max(len("STATUS"), max((len(r.status) for r in results), default=0))
        header = f"{'STATUS':<{status_width}}  {'NAME':<{name_width}}  DETAIL"
        typer.echo(header)
        for r in results:
            typer.echo(f"{r.status:<{status_width}}  {r.name:<{name_width}}  {r.detail}")

    if any(r.status == "fail" for r in results):
        raise typer.Exit(code=1)


# --- P2 logging lifecycle: logs sub-app ----------------------------------------
# `writ logs rotate` is the manual/timer entrypoint for the scheduled sweep that
# bounds the typed audit/friction/metrics streams (rotate -> gzip -> prune ->
# scratch cleanup). Registered like the git-hooks / pr sub-apps.

# Single source of truth for the journald hint, reused by the `logs` sub-app help
# text and `writ logs list` (DRY-DUP-001).
_JOURNALD_HINT = "journalctl --user -u writ-server"

# The `\b` marker keeps the journald hint on one line in `writ logs --help`
# (Click no-rewrap paragraph): the daemon's own stdout already rotates under
# systemd, so P3 only surfaces where to read it rather than re-plumbing it.
logs_app = typer.Typer(
    name="logs",
    help=(
        "Manage the Writ log lifecycle (rotate, retention, compression, backup, "
        f"inspect).\n\n\b\nDaemon stdout: {_JOURNALD_HINT}"
    ),
)
app.add_typer(logs_app, name="logs")


@logs_app.command("rotate")
def logs_rotate() -> None:
    """Rotate, compress, prune, and sweep the Writ log streams (the P2 backstop).

    Runs the sweep once and prints a one-line summary. Idempotent: a run with
    nothing to do reports all-zero counts and exits 0.
    """
    from writ.session.log_rotation import rotate_logs

    summary = rotate_logs()
    typer.echo(
        f"rotated={summary['rotated']} gzipped={summary['gzipped']} "
        f"pruned={summary['pruned']} scratch_cleaned={summary['scratch_cleaned']}"
    )


@logs_app.command("backup")
def logs_backup(
    dest: str = typer.Option(
        None, "--dest", help="Destination directory for the archive backup."
    ),
) -> None:
    r"""Copy the compressed archive generations to an off-root destination.

    Dest resolution: `--dest` -> `writ.toml \[logs] backup_dest`. Copies only
    `*.jsonl.gz` archive generations (never live streams, never moves), is
    idempotent, and is fail-soft: a partial-failure run still exits 0 and prints
    a `copied / skipped / bytes / errors` summary. With no destination at all it
    exits non-zero with a clear error.
    """
    from writ import config
    from writ.session.log_backup import backup_archives

    target = dest if dest is not None else config.get_logs_backup_dest()
    if not target:
        typer.echo(
            "error: no --dest given and no [logs] backup_dest configured in writ.toml",
            err=True,
        )
        raise typer.Exit(code=1)

    summary = backup_archives(target)
    typer.echo(
        f"copied={summary['copied']} skipped={summary['skipped']} "
        f"bytes={summary['bytes']} errors={summary['errors']}"
    )


@logs_app.command("tail")
def logs_tail(
    stream: str = typer.Option("audit", "--stream", help="Stream name to tail."),
    project: str = typer.Option(
        None, "--project", help="Project scope (defaults to the resolved project)."
    ),
    lines: int = typer.Option(20, "-n", "--lines", help="Number of lines to show."),
) -> None:
    """Print the last N events of a stream (newest last), fail-open on missing."""
    from writ.session.log_read import tail_stream
    from writ.shared.logging import resolve_project

    proj = project if project is not None else resolve_project()
    for line in tail_stream(project=proj, stream=stream, n=lines):
        typer.echo(line)


@logs_app.command("stats")
def logs_stats(
    project: str = typer.Option(
        None, "--project", help="Project scope (defaults to the resolved project)."
    ),
) -> None:
    """Print per-stream live line/byte counts, archive count, and ts range."""
    from writ.session.log_read import stream_stats
    from writ.shared.logging import resolve_project

    proj = project if project is not None else resolve_project()
    stats = stream_stats(project=proj)
    if not stats:
        typer.echo("(no streams)")
        return
    header = (
        f"{'STREAM':<10}  {'LINES':>8}  {'BYTES':>12}  {'ARCHIVES':>8}  "
        f"OLDEST -> NEWEST"
    )
    typer.echo(header)
    for name in sorted(stats):
        s = stats[name]
        typer.echo(
            f"{name:<10}  {s['live_lines']:>8}  {s['live_bytes']:>12}  "
            f"{s['archive_count']:>8}  {s['oldest_ts']} -> {s['newest_ts']}"
        )


@logs_app.command("list")
def logs_list(
    project: str = typer.Option(
        None, "--project", help="Filter to a single project scope."
    ),
) -> None:
    """List projects under the log root with their streams and archive counts."""
    from writ.session.log_read import list_projects

    for entry in list_projects(project=project):
        stream_summary = ", ".join(
            f"{s['stream']}={s['bytes']}" for s in entry["streams"]
        )
        typer.echo(
            f"{entry['project']}  streams=[{stream_summary}]  "
            f"archives={entry['archive_count']}"
        )
    typer.echo(f"hint: daemon stdout -> {_JOURNALD_HINT}")


# --- Auto-memory graph mirror: memory sub-app ----------------------------------
# The mirror hook covers memories written from now on; `backfill` covers the ones
# already on disk and doubles as the DELETION reconciler (a memory file the user
# deleted must stop reading as live in the graph). Registered like the git-hooks /
# pr / logs sub-apps.

DEFAULT_PROJECTS_ROOT = "~/.claude/projects"

memory_app = typer.Typer(
    name="memory",
    help="Mirror Claude Code's auto-memory files into the Writ graph, and read them back.",
)
app.add_typer(memory_app, name="memory")


def _memory_capture():
    """Load bin/lib/memory_capture.py -- the parser the mirror hook also binds to.

    It is a flat stdlib-only script (it has to run from a hook with no virtualenv),
    not an importable package, so it is loaded by path -- the same idiom
    writ/server/__init__.py uses for bin/lib/writ-session.py. Binding here rather
    than re-implementing the parse is what keeps the two writers from drifting.
    """
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "bin" / "lib" / "memory_capture.py"
    spec = importlib.util.spec_from_file_location("writ_memory_capture", str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contained(candidate: Path, root: Path) -> bool:
    """True when `candidate` resolves to `root` or somewhere beneath it.

    SEC-INJ-PATH-001: the walk resolves symlinks and verifies containment BEFORE
    reading, so a project directory (or a note) symlinked outside --projects-root
    cannot make the backfill read arbitrary files.
    """
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


@memory_app.command("backfill")
def memory_backfill(
    projects_root: str = typer.Option(
        DEFAULT_PROJECTS_ROOT,
        "--projects-root",
        help="Root holding one directory per project, each with a memory/ subdir.",
    ),
) -> None:
    """Upsert every existing memory file, then tombstone the ones whose file is gone.

    Walks `<projects-root>/*/memory/*.md`, upserts each memory (idempotent MERGE, so
    re-running is safe), and per project tombstones the Memory records whose file no
    longer exists -- status='deleted', never a hard delete, so the audit trail
    survives. MEMORY.md (the index) is skipped. Prints the counts.
    """
    root_path = Path(projects_root).expanduser()
    try:
        root = root_path.resolve()
    except OSError as exc:
        typer.echo(f"Cannot resolve --projects-root {projects_root!r}: {exc}", err=True)
        raise typer.Exit(code=1)
    if not root.is_dir():
        typer.echo(f"No projects root at {root} (nothing to backfill).", err=True)
        raise typer.Exit(code=1)

    mc = _memory_capture()

    async def _run() -> None:
        projects = upserted = tombstoned = skipped = unreadable = outside = 0

        async with _writ_db() as db:
            for project_dir in sorted(root.iterdir()):
                if not project_dir.is_dir():
                    continue
                if not _contained(project_dir, root):
                    outside += 1
                    continue
                memory_dir = project_dir / "memory"
                if not memory_dir.is_dir() or not _contained(memory_dir, root):
                    continue

                projects += 1
                present_names: list[str] = []
                for note in sorted(memory_dir.glob("*.md")):
                    if mc.is_memory_index_file(str(note)):
                        continue
                    if not _contained(note, root):
                        outside += 1
                        continue
                    try:
                        content = note.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        # The file EXISTS; a transient read failure must not become a
                        # tombstone. Protect its record via the naming convention
                        # (name == filename stem); the next run re-covers the content.
                        present_names.append(note.stem)
                        unreadable += 1
                        continue
                    payload = mc.build_memory_payload(str(note), content)
                    if payload is None:
                        present_names.append(note.stem)
                        skipped += 1
                        continue
                    await db.create_memory(**payload)
                    present_names.append(payload["name"])
                    upserted += 1

                # Only ever reached with a directory we actually listed, so an empty
                # present_names means "every memory file here is gone", not "the read
                # failed" -- the difference between a correct sweep and a mass tombstone.
                tombstoned += await db.tombstone_missing_memories(
                    project=project_dir.name, existing_names=present_names
                )

        typer.echo(
            f"Memory backfill: projects={projects} upserted={upserted} "
            f"tombstoned={tombstoned} skipped={skipped} unreadable={unreadable} "
            f"outside_root={outside}"
        )

    asyncio.run(_run())


@memory_app.command("list")
def memory_list(
    project: str = typer.Option(
        ..., "--project", help="Project scope (the encoded project directory name)."
    ),
    include_deleted: bool = typer.Option(
        False, "--include-deleted/--no-include-deleted",
        help="Also show tombstoned memories (status=deleted).",
    ),
) -> None:
    """List a project's mirrored memories, most-recently-updated first."""

    async def _run() -> None:
        async with _writ_db() as db:
            memories = await db.list_memories(project, include_deleted=include_deleted)
            if not memories:
                typer.echo(f"(no memories mirrored for project {project})")
                return
            for memory in memories:
                mem_type = memory.get("type") or "-"
                status = memory.get("status") or "live"
                typer.echo(
                    f"{memory.get('name', '(unnamed)')}  [{mem_type}/{status}]  "
                    f"{memory.get('description', '')}"
                )
            typer.echo(f"({len(memories)} memories)")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
