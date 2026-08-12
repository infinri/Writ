"""Pure renderer for the `writ validate` findings dict.

`IntegrityChecker.run_all_checks` (writ/graph/integrity.py) produces a findings
dict; this module turns that dict into ordered output lines. It does no DB,
timing, or filesystem work, so it is trivially unit-testable and keeps the
`validate` command thin.

`render_findings(findings)` returns `(stdout_lines, stderr_lines)`: two lists of
strings, one element per line. Each element corresponds to exactly one original
`typer.echo(...)` call at writ/cli.py:623-913, transcribed verbatim (leading
"\n", spacing, `!r` repr, and alignment padding preserved) so that emitting each
line with a single `typer.echo(line)` reproduces byte-identical output. The
steps run in exact source order; findings keys are treated as optional so a
partial dict renders only its present sections.

Byte-identity is per-stream. The only stderr line is `redundancy_unavailable`
(emitted with `err=True`); the caller drains `stdout_lines` then `stderr_lines`,
so under a merged capture (`writ validate 2>&1`) that single line prints after
the stdout sections rather than inline where the pre-refactor code emitted it.
This is immaterial for the normal case (separate streams) and only reachable
when the redundancy check is unavailable (sentence-transformers absent).

`conflicts`, `orphans`, `stale`, and `redundant` were indexed directly (not via
`.get`) in the pre-refactor code; `run_all_checks` always populates them, so
treating every key as optional here is behavior-equivalent for real findings.
"""

from __future__ import annotations

from collections.abc import Callable


def _section(
    key: str,
    header: Callable[[object], str],
    item: Callable[[object], str],
    items: Callable[[object], list] | None = None,
    limit: int | None = None,
) -> Callable[[dict, list, list], None]:
    """Build a step for a regular "header + item loop (+ optional truncation)"
    block. The header count uses the full (untruncated) value; the item loop
    uses the truncated slice when ``limit`` is set.
    """

    def step(findings: dict, out: list, err: list) -> None:
        value = findings.get(key)
        if not value:
            return
        out.append(header(value))
        seq = items(value) if items is not None else value
        if limit is not None:
            seq = seq[:limit]
        for element in seq:
            out.append(item(element))

    return step


def _render_orphan_counts_by_type(findings: dict, out: list, err: list) -> None:
    nonzero_orphans = {
        t: c for t, c in (findings.get("orphan_counts_by_type") or {}).items() if c
    }
    if nonzero_orphans:
        out.append("\nOrphans by node type (all labels):")
        for t, c in sorted(nonzero_orphans.items()):
            out.append(f"  {t:18s} {c}")


def _render_dangling_dispatched_roles(findings: dict, out: list, err: list) -> None:
    dangling = findings.get("dangling_dispatched_roles")
    if not dangling:
        return
    out.append(f"\nDangling dispatched_roles ({len(dangling)}):")
    for x in dangling:
        arrow = (
            f" -> {x['resolved_target_id']}"
            if x["resolvable"]
            else " (unresolvable)"
        )
        out.append(f"  {x['from']} dispatched_roles='{x['ref']}'{arrow}")


def _render_redundancy_unavailable(findings: dict, out: list, err: list) -> None:
    if findings.get("redundancy_unavailable"):
        # Redundancy check could not run (missing optional dep).
        # Surface explicitly so the user does not read silence as
        # "no redundancies found." Same principle as the explicit
        # ONNX-fallback contract in writ/retrieval/pipeline.py
        # (commit dae679a): wire-format silence is indistinguishable
        # from a clean check; the gate must say which it is.
        err.append(
            f"\nRedundancy check skipped: {findings['redundancy_unavailable']}"
        )


def _render_unreviewed(findings: dict, out: list, err: list) -> None:
    u = findings.get("unreviewed")
    if not u:
        return
    out.append(f"\nUnreviewed AI-provisional: {u['message']}")


def _render_edge_parity(findings: dict, out: list, err: list) -> None:
    ep = findings.get("edge_parity")
    if not ep:
        return
    stale = ep.get("stale", [])
    missing = ep.get("missing", [])
    out.append(
        f"\nEdge parity drift -- run `writ reconcile` "
        f"(stale={len(stale)}, missing={len(missing)}):"
    )
    for etype, src, tgt in stale[:20]:
        out.append(f"  stale (graph, not in source):   {src} -{etype}-> {tgt}")
    for etype, src, tgt in missing[:20]:
        out.append(f"  missing (source, not in graph): {src} -{etype}-> {tgt}")


def _render_methodology_field_drift(findings: dict, out: list, err: list) -> None:
    fd = findings.get("methodology_field_drift")
    if not fd:
        return
    out.append(
        f"\nMethodology field drift -- graph value diverges from "
        f"bible/methodology/<id>.md (re-author the source) on "
        f"{len(fd)} node(s):"
    )
    for node_id, fields in sorted(fd.items())[:20]:
        for field, vals in sorted(fields.items()):
            out.append(
                f"  {node_id}.{field}: graph={vals.get('graph')!r} "
                f"source={vals.get('source')!r}"
            )


def _render_dispatch_invokes(findings: dict, out: list, err: list) -> None:
    di = findings.get("dispatch_invokes")
    if not di:
        return
    dnr = di.get("dispatch_to_non_role", [])
    itr = di.get("invokes_to_role", [])
    out.append(
        f"\nDispatch/invokes invariant ("
        f"DISPATCHES->non-role={len(dnr)}, INVOKES->role={len(itr)}):"
    )
    for src, tgt, label in dnr[:20]:
        out.append(f"  DISPATCHES to non-role (use INVOKES): {src} -> {tgt} [{label}]")
    for src, tgt, label in itr[:20]:
        out.append(f"  INVOKES to role (use DISPATCHES):     {src} -> {tgt} [{label}]")


def _render_category_reachability(findings: dict, out: list, err: list) -> None:
    reachability = findings.get("category_reachability") or {}
    if reachability.get("skipped"):
        out.append(f"\nCategory reachability skipped: {reachability.get('reason', '')}")
    else:
        nwc = reachability.get("nodes_without_category") or []
        if nwc:
            out.append(f"\nNodes without category ({len(nwc)}):")
            for n in nwc:
                out.append(f"  {n.get('type', '?')}: {n['id']}")
        cwr = reachability.get("categories_without_route") or []
        if cwr:
            out.append(f"\nCategories without route ({len(cwr)}):")
            for c in cwr:
                out.append(f"  {c}")


def _render_ranked_exclusion_mismatch(findings: dict, out: list, err: list) -> None:
    rem = findings.get("ranked_exclusion_mismatch")
    if not rem:
        return
    # Plain string literal (NOT an f-string): the braces stay un-interpolated.
    out.append("\nRanked-exclusion mismatch ({excluded-from-ranked} != {mandatory}):")
    for rid in rem.get("excluded_not_mandatory", []):
        out.append(f"  excluded-but-not-mandatory: {rid}")
    for rid in rem.get("mandatory_not_excluded", []):
        out.append(f"  mandatory-but-not-excluded: {rid}")


def _render_always_on_budget_breach(findings: dict, out: list, err: list) -> None:
    b = findings.get("always_on_budget_breach")
    if not b:
        return
    out.append(
        f"\nAlways-on budget breach: {b['rule_count']} rules render to "
        f"{b['total_tokens']} tokens (cap {b['cap']})."
    )


def _render_floor_completeness(findings: dict, out: list, err: list) -> None:
    fc = findings.get("floor_completeness")
    if not fc:
        return
    out.append(
        f"\nFloor completeness (Invariant A) -- node floor_modes != Appendix B "
        f"({len(fc)} modes):"
    )
    for mode, d in sorted(fc.items()):
        if d.get("missing"):
            out.append(f"  {mode} missing: {', '.join(d['missing'])}")
        if d.get("spurious"):
            out.append(f"  {mode} spurious: {', '.join(d['spurious'])}")


def _render_trigger_keyword_invariant(findings: dict, out: list, err: list) -> None:
    tk = findings.get("trigger_keyword_invariant")
    if not tk:
        return
    pv = tk.get("parity_violations", [])
    po = tk.get("pull_orphans", [])
    out.append(
        f"\nTrigger-keyword invariant (Invariant B) -- "
        f"parity={len(pv)}, pull_orphans={len(po)}:"
    )
    for v in pv[:20]:
        out.append(f"  parity: {v['id']} keyword '{v['keyword']}' not in node text")
    for oid in po[:20]:
        out.append(f"  pull orphan (no floor/action/keywords): {oid}")


def _render_push_reachability(findings: dict, out: list, err: list) -> None:
    pr = findings.get("push_reachability")
    if not pr:
        return
    ear = pr.get("empty_action_routes", [])
    po = pr.get("push_orphans", [])
    dat = pr.get("dead_action_tags", [])
    out.append(
        f"\nPush reachability (1.8) -- empty_action_routes={len(ear)}, "
        f"push_orphans={len(po)}, dead_action_tags={len(dat)}:"
    )
    for c in ear:
        out.append(f"  empty action route (routes 'action', no member action_triggers): {c}")
    for oid in po[:20]:
        out.append(f"  push orphan (no floor/action/keywords): {oid}")
    for oid in dat[:20]:
        out.append(f"  dead action tag (action_triggers on a non-methodology node): {oid}")


def _render_delivery_orphans(findings: dict, out: list, err: list) -> None:
    do = findings.get("delivery_orphans")
    if not do:
        return
    out.append(
        f"\nDelivery orphans (1.9) -- {len(do)} methodology node(s) no channel "
        f"can select (no floor_modes, no action_triggers, no trigger_keywords, "
        f"and no 'semantic'/'pull' category route):"
    )
    for node_id, ev in sorted(do.items())[:20]:
        out.append(
            f"  {node_id} ({ev.get('label', '?')}) in "
            f"{ev.get('category', '?')} routes={ev.get('routes') or []}"
        )


def _render_example_lint(findings: dict, out: list, err: list) -> None:
    el = findings.get("example_lint")
    if not el:
        return
    n = sum(len(v) for v in el.values())
    out.append(
        f"\nExample lint (3.2) -- {n} defect(s) in {len(el)} rule(s):"
    )
    for rid, items in sorted(el.items()):
        for it in items:
            out.append(
                f"  {rid} [{it['field']}] {it['kind']}: {it['detail']}"
            )


# Ordered render steps, matching writ/cli.py:623-913 source order exactly.
# Regular header+item-loop blocks are data-driven `_section` entries; the
# irregular blocks get dedicated functions. `orphans_all_labels` is
# intentionally absent (run_all_checks populates it but validate never renders
# it).
_RENDER_STEPS: tuple[Callable[[dict, list, list], None], ...] = (
    _section(
        "conflicts",
        lambda v: f"\nConflicts ({len(v)}):",
        lambda c: f"  {c['rule_a']} <-> {c['rule_b']}",
    ),
    _section(
        "orphans",
        lambda v: f"\nOrphans ({len(v)}):",
        lambda o: f"  {o}",
    ),
    _render_orphan_counts_by_type,
    _render_dangling_dispatched_roles,
    _section(
        "stale",
        lambda v: f"\nStale ({len(v)}):",
        lambda s: f"  {s['rule_id']} (expired {s['expired_on']})",
    ),
    _section(
        "redundant",
        lambda v: f"\nRedundant ({len(v)}):",
        lambda r: f"  {r['rule_a']} ~ {r['rule_b']} ({r['similarity']})",
    ),
    _render_redundancy_unavailable,
    _render_unreviewed,
    _section(
        "frequency_stale",
        lambda v: f"\nFrequency stale -- zero observations ({len(v)}):",
        lambda fs: f"  {fs['rule_id']}",
        limit=10,
    ),
    _section(
        "graduation_flags",
        lambda v: f"\nGraduation flags ({len(v)}):",
        lambda gf: f"  {gf['rule_id']} (ratio: {gf['ratio']}, n={gf['n']})",
    ),
    _section(
        "parity_violations",
        lambda v: f"\nParity violations -- graph nodes absent from markdown ({len(v)}):",
        lambda x: f"  {x.get('type', '?')}: {x['id']}",
    ),
    _render_edge_parity,
    _section(
        "prop_parity",
        lambda v: (
            f"\nProp parity drift -- managed props in graph absent from source "
            f"(run `writ reconcile`) on {len(v)} node(s):"
        ),
        lambda kv: f"  {kv[0]}: {', '.join(kv[1])}",
        items=lambda v: sorted(v.items()),
        limit=20,
    ),
    _render_methodology_field_drift,
    _render_dispatch_invokes,
    _section(
        "teaches_source",
        lambda v: (
            f"\nTEACHES convention violated -- a Rule is teaching ({len(v)}); "
            f"a Rule is taught, never the teacher:"
        ),
        lambda e: f"  {e['src']} -TEACHES-> {e['tgt']}",
        limit=20,
    ),
    _render_category_reachability,
    _section(
        "stranded_mandatory",
        lambda v: f"\nStranded mandatory -- reachable by NEITHER ranked nor injection ({len(v)}):",
        lambda rid: f"  {rid}",
    ),
    _render_ranked_exclusion_mismatch,
    _render_always_on_budget_breach,
    _section(
        "artifact_dangling_rule_ids",
        lambda v: (
            f"\nArtifact dangling rule_ids -- bible/abstractions.json references "
            f"rules absent from the graph ({len(v)}); regenerate with "
            f"`writ compress`:"
        ),
        lambda d: f"  {d['rule_id']} (in abstraction {d['abstraction_id']})",
    ),
    _render_floor_completeness,
    _render_trigger_keyword_invariant,
    _render_push_reachability,
    _section(
        "action_vocabulary",
        lambda v: f"\nAction vocabulary closure (1.8) -- unknown actions on {len(v)} node(s):",
        lambda kv: f"  {kv[0]}: {', '.join(kv[1])}",
        items=lambda v: sorted(v.items()),
    ),
    _section(
        "route_implementation_closure",
        lambda v: (
            f"\nRoute implementation closure (1.9) -- {len(v)} categor(y/ies) "
            f"declare a route no delivery channel implements; nothing reaches a "
            f"member BY that route, and when it is the category's only route the "
            f"member is undeliverable outright (see delivery orphans below):"
        ),
        lambda kv: f"  {kv[0]}: {', '.join(kv[1])}",
        items=lambda v: sorted(v.items()),
    ),
    _render_delivery_orphans,
    _render_example_lint,
    _section(
        "domain_enum",
        lambda v: f"\nDomain enum (3.5) -- {len(v)} node(s) with an invalid domain:",
        lambda d: f"  {d['node_id']} ({d['label']}): {d['domain']!r}",
    ),
    _section(
        "counter_nodes_parity",
        lambda v: (
            f"\nCounter-nodes parity (3.5b) -- counter_nodes drifted from edges "
            f"on {len(v)} node(s):"
        ),
        lambda d: (
            f"  {d['node_id']}: missing_from_field={d['missing_from_field']} "
            f"extra_in_field={d['extra_in_field']}"
        ),
    ),
    _section(
        "dispatched_by_parity",
        lambda v: (
            f"\nDispatched-by parity (3.5b) -- dispatched_by drifted from edges "
            f"on {len(v)} node(s):"
        ),
        lambda d: (
            f"  {d['node_id']}: missing_from_field={d['missing_from_field']} "
            f"extra_in_field={d['extra_in_field']}"
        ),
    ),
    _section(
        "enforceable_severity",
        lambda v: (
            f"\nEnforceable-severity coupling (3.1) -- {len(v)} enforceable "
            f"critical/high rule(s) left advisory:"
        ),
        lambda d: f"  {d['rule_id']} ({d['severity']}): has MEP but mandatory=false",
    ),
    _section(
        "forbidden_phrase_overlap",
        lambda v: f"\nForbidden-phrase overlap (3.3) -- {len(v)} phrase(s) in >1 FRB node:",
        lambda d: f"  {d['phrase']!r} -> {d['nodes']}",
    ),
    _section(
        "shared_code_example",
        lambda v: f"\nShared code example (3.3) -- {len(v)} block(s) in >1 rule:",
        lambda d: f"  {d['rules']}: {d['block']!r}",
    ),
)


def render_findings(findings: dict) -> tuple[list[str], list[str]]:
    """Render the findings dict into `(stdout_lines, stderr_lines)`.

    Covers only the findings-dict blocks (writ/cli.py:623-913) in exact source
    order. The epilogue, review_confidence, benchmark, and hook-delivery-lint
    branches stay in the command (they need live/timing/filesystem inputs).
    """
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    for step in _RENDER_STEPS:
        step(findings, stdout_lines, stderr_lines)
    return stdout_lines, stderr_lines
