"""RED characterization tests for `writ.graph.validate_report.render_findings`.

Wave 2 Cycle 3 (branch refactor/w2-cli-validate) extracts the ~31 findings-dict
render blocks of the `validate` command (writ/cli.py:623-913) into a pure
function: `render_findings(findings: dict) -> tuple[list[str], list[str]]`
returning `(stdout_lines, stderr_lines)`.

This rendering has ZERO pre-existing test coverage, so this file is the
byte-identical safety net for the extraction. Every expected literal below is
transcribed verbatim from the CURRENT `typer.echo(...)` f-strings at
writ/cli.py:623-913 (confirmed against `repr()` of the raw source lines, not
just a visual read, to lock exact spacing/emoji/`!r`/padding). Each list
element in the expected output corresponds to exactly one current
`typer.echo(...)` call.

RED: `writ.graph.validate_report` does not exist yet, so every test below
fails at import time. GREEN once `render_findings` is implemented per the
approved plan (see ~/.claude/skills/writ/plan.md).

Per TEST-TDD-001 / PBK-PROC-TDD-001: this skeleton is authored as an
executable spec before the implementation lands. Per TEST-ISOLATE-001, every
case is a synthetic findings dict -- no daemon, no Neo4j.

1.9 addition: route_implementation_closure and delivery_orphans (the route
half of the reachability class -- a Category declaring an unwired route, and
a methodology node no channel can select) were transcribed against the
shipped writ/graph/validate_report.py `_section("route_implementation_closure",
...)` and `_render_delivery_orphans` blocks the same way every other key here
was: verbatim, not derived.

Cycle E / E4 addition: dispatch_prose_parity (the fourth rendering of
dispatched_roles -- the playbook's own prose, checked against the
DISPATCHES edge) is transcribed verbatim from plan.md's
`_render_dispatch_prose_parity` block (a dedicated irregular renderer, like
dispatch_invokes, not a generic `_section` entry -- the finding is a
two-key dict: declared_but_unnamed / named_but_undeclared).

Key coverage map (33 findings keys total; every key has >=1 dedicated test
somewhere in this file):
    TestRenderFindingsPerKey: conflicts, orphans, orphan_counts_by_type,
        dangling_dispatched_roles, stale, redundant, unreviewed,
        frequency_stale, graduation_flags, parity_violations, edge_parity,
        prop_parity, methodology_field_drift, dispatch_invokes,
        teaches_source, stranded_mandatory, always_on_budget_breach,
        artifact_dangling_rule_ids, floor_completeness,
        trigger_keyword_invariant, push_reachability, action_vocabulary,
        route_implementation_closure, delivery_orphans, example_lint,
        domain_enum, counter_nodes_parity,
        dispatched_by_parity, enforceable_severity,
        forbidden_phrase_overlap, shared_code_example, dispatch_prose_parity.
    TestSpecialCases: ranked_exclusion_mismatch, category_reachability,
        orphan_counts_by_type (zero-filter/sort quirk),
        dangling_dispatched_roles (unresolvable arrow).
    TestStderrSplit: redundancy_unavailable.
    TestSilentKeys: orphans_all_labels.
    TestTruncation / TestCountExpressions: cross-cutting quirks (not new
        keys) layered on frequency_stale, edge_parity, push_reachability,
        example_lint.
"""

from __future__ import annotations

from writ.graph.validate_report import render_findings


def make_findings(**overrides: object) -> dict:
    """Build a findings dict containing only the key(s) under test.

    `render_findings` treats every findings key as optional (the plan's
    generic table entry does `if not findings.get(key): skip`), so a test
    only needs to populate the key(s) it exercises -- no kitchen-sink setup
    of the other ~31 keys (TEST-FIXTURE-002).
    """
    return dict(overrides)


class TestEmpty:
    """All-empty findings -> render_findings returns ([], [])."""

    def test_empty_dict_returns_empty_tuples(self) -> None:
        stdout_lines, stderr_lines = render_findings({})
        assert stdout_lines == []
        assert stderr_lines == []

    def test_all_falsy_values_return_empty_tuples(self) -> None:
        findings = make_findings(
            conflicts=[],
            orphans=[],
            stale=[],
            redundant=[],
            orphan_counts_by_type={},
            dangling_dispatched_roles=[],
            unreviewed={},
            frequency_stale=[],
            graduation_flags=[],
            parity_violations=[],
            edge_parity={},
            prop_parity={},
            methodology_field_drift={},
            dispatch_invokes={},
            teaches_source=[],
            category_reachability={},
            stranded_mandatory=[],
            ranked_exclusion_mismatch={},
            always_on_budget_breach={},
            artifact_dangling_rule_ids=[],
            floor_completeness={},
            trigger_keyword_invariant={},
            push_reachability={},
            action_vocabulary={},
            route_implementation_closure={},
            delivery_orphans={},
            example_lint={},
            domain_enum=[],
            counter_nodes_parity=[],
            dispatched_by_parity=[],
            enforceable_severity=[],
            forbidden_phrase_overlap=[],
            shared_code_example=[],
            dispatch_prose_parity={},
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == []
        assert stderr_lines == []


class TestRenderFindingsPerKey:
    """One case per findings key: minimal truthy value -> exact header +
    item line(s), transcribed verbatim from writ/cli.py:623-913."""

    def test_conflicts(self) -> None:
        findings = make_findings(
            conflicts=[{"rule_a": "RULE-A", "rule_b": "RULE-B"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nConflicts (1):",
            "  RULE-A <-> RULE-B",
        ]
        assert stderr_lines == []

    def test_orphans(self) -> None:
        findings = make_findings(orphans=["NODE-ORPHAN-1"])
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nOrphans (1):",
            "  NODE-ORPHAN-1",
        ]
        assert stderr_lines == []

    def test_orphan_counts_by_type_minimal(self) -> None:
        findings = make_findings(orphan_counts_by_type={"Rule": 3})
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nOrphans by node type (all labels):",
            f"  {'Rule':18s} 3",
        ]
        assert stderr_lines == []

    def test_dangling_dispatched_roles_resolvable(self) -> None:
        findings = make_findings(
            dangling_dispatched_roles=[
                {
                    "from": "writ-server",
                    "ref": "writ-explorer",
                    "resolvable": True,
                    "resolved_target_id": "writ-explorer-role",
                }
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDangling dispatched_roles (1):",
            "  writ-server dispatched_roles='writ-explorer' -> writ-explorer-role",
        ]
        assert stderr_lines == []

    def test_stale(self) -> None:
        findings = make_findings(
            stale=[{"rule_id": "RULE-STALE-1", "expired_on": "2026-01-01"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nStale (1):",
            "  RULE-STALE-1 (expired 2026-01-01)",
        ]
        assert stderr_lines == []

    def test_redundant(self) -> None:
        findings = make_findings(
            redundant=[
                {"rule_a": "RULE-A", "rule_b": "RULE-B", "similarity": 0.97}
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nRedundant (1):",
            "  RULE-A ~ RULE-B (0.97)",
        ]
        assert stderr_lines == []

    def test_unreviewed(self) -> None:
        findings = make_findings(
            unreviewed={"message": "4 AI-provisional rules pending human review"}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nUnreviewed AI-provisional: 4 AI-provisional rules pending human review",
        ]
        assert stderr_lines == []

    def test_frequency_stale_minimal(self) -> None:
        findings = make_findings(
            frequency_stale=[{"rule_id": "RULE-FREQ-1"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nFrequency stale -- zero observations (1):",
            "  RULE-FREQ-1",
        ]
        assert stderr_lines == []

    def test_graduation_flags(self) -> None:
        findings = make_findings(
            graduation_flags=[{"rule_id": "RULE-GRAD-1", "ratio": 0.8, "n": 12}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nGraduation flags (1):",
            "  RULE-GRAD-1 (ratio: 0.8, n=12)",
        ]
        assert stderr_lines == []

    def test_parity_violations(self) -> None:
        findings = make_findings(
            parity_violations=[{"type": "Rule", "id": "RULE-PARITY-1"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nParity violations -- graph nodes absent from markdown (1):",
            "  Rule: RULE-PARITY-1",
        ]
        assert stderr_lines == []

    def test_parity_violations_missing_type_defaults_to_question_mark(self) -> None:
        findings = make_findings(parity_violations=[{"id": "RULE-PARITY-2"}])
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nParity violations -- graph nodes absent from markdown (1):",
            "  ?: RULE-PARITY-2",
        ]
        assert stderr_lines == []

    def test_edge_parity_minimal(self) -> None:
        findings = make_findings(
            edge_parity={
                "stale": [("RELATED_TO", "RULE-A", "RULE-B")],
                "missing": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nEdge parity drift -- run `writ reconcile` (stale=1, missing=0):",
            "  stale (graph, not in source):   RULE-A -RELATED_TO-> RULE-B",
        ]
        assert stderr_lines == []

    def test_edge_parity_missing_only(self) -> None:
        findings = make_findings(
            edge_parity={
                "stale": [],
                "missing": [("TEACHES", "RULE-C", "RULE-D")],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nEdge parity drift -- run `writ reconcile` (stale=0, missing=1):",
            "  missing (source, not in graph): RULE-C -TEACHES-> RULE-D",
        ]
        assert stderr_lines == []

    def test_prop_parity(self) -> None:
        findings = make_findings(
            prop_parity={"NODE-PROP-1": ["confidence", "severity"]}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nProp parity drift -- managed props in graph absent from source "
            "(run `writ reconcile`) on 1 node(s):",
            "  NODE-PROP-1: confidence, severity",
        ]
        assert stderr_lines == []

    def test_methodology_field_drift(self) -> None:
        findings = make_findings(
            methodology_field_drift={
                "NODE-DRIFT-1": {
                    "severity": {"graph": "high", "source": "critical"}
                }
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nMethodology field drift -- graph value diverges from "
            "bible/methodology/<id>.md (re-author the source) on 1 node(s):",
            "  NODE-DRIFT-1.severity: graph='high' source='critical'",
        ]
        assert stderr_lines == []

    def test_methodology_field_drift_multiple_fields_sorted(self) -> None:
        findings = make_findings(
            methodology_field_drift={
                "NODE-DRIFT-1": {
                    "severity": {"graph": "high", "source": "critical"},
                    "confidence": {"graph": 0.5, "source": 0.9},
                }
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nMethodology field drift -- graph value diverges from "
            "bible/methodology/<id>.md (re-author the source) on 1 node(s):",
            "  NODE-DRIFT-1.confidence: graph=0.5 source=0.9",
            "  NODE-DRIFT-1.severity: graph='high' source='critical'",
        ]
        assert stderr_lines == []

    def test_dispatch_invokes_minimal(self) -> None:
        findings = make_findings(
            dispatch_invokes={
                "dispatch_to_non_role": [
                    ("writ-server", "RULE-X", "DISPATCHES")
                ],
                "invokes_to_role": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatch/invokes invariant (DISPATCHES->non-role=1, INVOKES->role=0):",
            "  DISPATCHES to non-role (use INVOKES): writ-server -> RULE-X [DISPATCHES]",
        ]
        assert stderr_lines == []

    def test_dispatch_invokes_invokes_to_role_only(self) -> None:
        findings = make_findings(
            dispatch_invokes={
                "dispatch_to_non_role": [],
                "invokes_to_role": [
                    ("writ-server", "writ-explorer", "INVOKES")
                ],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatch/invokes invariant (DISPATCHES->non-role=0, INVOKES->role=1):",
            "  INVOKES to role (use DISPATCHES):     writ-server -> writ-explorer [INVOKES]",
        ]
        assert stderr_lines == []

    def test_teaches_source(self) -> None:
        findings = make_findings(
            teaches_source=[{"src": "RULE-TEACH-1", "tgt": "RULE-TEACH-2"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nTEACHES convention violated -- a Rule is teaching (1); "
            "a Rule is taught, never the teacher:",
            "  RULE-TEACH-1 -TEACHES-> RULE-TEACH-2",
        ]
        assert stderr_lines == []

    def test_stranded_mandatory(self) -> None:
        findings = make_findings(stranded_mandatory=["RULE-STRANDED-1"])
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nStranded mandatory -- reachable by NEITHER ranked nor injection (1):",
            "  RULE-STRANDED-1",
        ]
        assert stderr_lines == []

    def test_always_on_budget_breach(self) -> None:
        findings = make_findings(
            always_on_budget_breach={
                "rule_count": 42,
                "total_tokens": 5000,
                "cap": 4000,
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nAlways-on budget breach: 42 rules render to 5000 tokens (cap 4000).",
        ]
        assert stderr_lines == []

    def test_artifact_dangling_rule_ids(self) -> None:
        findings = make_findings(
            artifact_dangling_rule_ids=[
                {"rule_id": "RULE-GONE-1", "abstraction_id": "ABS-1"}
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nArtifact dangling rule_ids -- bible/abstractions.json references "
            "rules absent from the graph (1); regenerate with `writ compress`:",
            "  RULE-GONE-1 (in abstraction ABS-1)",
        ]
        assert stderr_lines == []

    def test_floor_completeness_missing_and_spurious(self) -> None:
        findings = make_findings(
            floor_completeness={
                "work": {
                    "missing": ["gate-phase-a"],
                    "spurious": ["gate-old"],
                }
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nFloor completeness (Invariant A) -- node floor_modes != Appendix B (1 modes):",
            "  work missing: gate-phase-a",
            "  work spurious: gate-old",
        ]
        assert stderr_lines == []

    def test_floor_completeness_missing_only_omits_spurious_line(self) -> None:
        findings = make_findings(
            floor_completeness={"work": {"missing": ["gate-phase-a"], "spurious": []}}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nFloor completeness (Invariant A) -- node floor_modes != Appendix B (1 modes):",
            "  work missing: gate-phase-a",
        ]
        assert stderr_lines == []

    def test_trigger_keyword_invariant(self) -> None:
        findings = make_findings(
            trigger_keyword_invariant={
                "parity_violations": [
                    {"id": "RULE-TRIG-1", "keyword": "gate"}
                ],
                "pull_orphans": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nTrigger-keyword invariant (Invariant B) -- parity=1, pull_orphans=0:",
            "  parity: RULE-TRIG-1 keyword 'gate' not in node text",
        ]
        assert stderr_lines == []

    def test_trigger_keyword_invariant_pull_orphans_only(self) -> None:
        findings = make_findings(
            trigger_keyword_invariant={
                "parity_violations": [],
                "pull_orphans": ["RULE-ORPHAN-1"],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nTrigger-keyword invariant (Invariant B) -- parity=0, pull_orphans=1:",
            "  pull orphan (no floor/action/keywords): RULE-ORPHAN-1",
        ]
        assert stderr_lines == []

    def test_push_reachability_minimal(self) -> None:
        findings = make_findings(
            push_reachability={
                "empty_action_routes": ["writ-explorer"],
                "push_orphans": [],
                "dead_action_tags": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nPush reachability (1.8) -- empty_action_routes=1, "
            "push_orphans=0, dead_action_tags=0:",
            "  empty action route (routes 'action', no member action_triggers): writ-explorer",
        ]
        assert stderr_lines == []

    def test_action_vocabulary(self) -> None:
        findings = make_findings(action_vocabulary={"NODE-ACT-1": ["foo", "bar"]})
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nAction vocabulary closure (1.8) -- unknown actions on 1 node(s):",
            "  NODE-ACT-1: foo, bar",
        ]
        assert stderr_lines == []

    def test_route_implementation_closure(self) -> None:
        # NOT a full verbatim transcription like the keys above: this
        # header's wording was observed changing mid-authoring-session while
        # cycle 6a's implementer iterated (derivation -> hand-listed
        # WIRED_ROUTES, "serves"/"undeliverable" phrasing -> "implements"/
        # "nothing reaches" phrasing). Pinning content (the section fires,
        # names 1.9, and renders the category/route) rather than the exact
        # sentence keeps this test meaningful without re-churning on every
        # wording pass.
        findings = make_findings(
            route_implementation_closure={"CAT-DISC-001": ["ride_along"]}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert len(stdout_lines) == 2
        assert "Route implementation closure" in stdout_lines[0]
        assert "(1.9)" in stdout_lines[0]
        assert stdout_lines[1] == "  CAT-DISC-001: ride_along"
        assert stderr_lines == []

    def test_route_implementation_closure_multiple_routes_joined(self) -> None:
        # The item-line format (comma-joined routes) is stable across both
        # observed header revisions -- pin that specifically.
        findings = make_findings(
            route_implementation_closure={"CAT-MULTI-1": ["bogus_a", "bogus_b"]}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines[1] == "  CAT-MULTI-1: bogus_a, bogus_b"
        assert stderr_lines == []

    def test_delivery_orphans(self) -> None:
        # Transcribed from validate_report.py's _render_delivery_orphans.
        # The value shape ({node_id: {label, category, routes}}) matches
        # detect_delivery_orphans' documented return.
        findings = make_findings(
            delivery_orphans={
                "ANT-PROC-DEBUG-001": {
                    "label": "AntiPattern",
                    "category": "CAT-DISC-001",
                    "routes": ["ride_along"],
                }
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDelivery orphans (1.9) -- 1 methodology node(s) no channel can "
            "select (no floor_modes, no action_triggers, no trigger_keywords, "
            "and no 'semantic'/'pull' category route):",
            "  ANT-PROC-DEBUG-001 (AntiPattern) in CAT-DISC-001 routes=['ride_along']",
        ]
        assert stderr_lines == []

    def test_delivery_orphans_missing_routes_renders_empty_list(self) -> None:
        # ev.get('routes') or [] -- a category with no routes at all (falsy)
        # must render as an empty list, not None or a KeyError.
        findings = make_findings(
            delivery_orphans={
                "SKL-NOROUTE-1": {
                    "label": "Skill",
                    "category": "CAT-NOROUTE-1",
                    "routes": [],
                }
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDelivery orphans (1.9) -- 1 methodology node(s) no channel can "
            "select (no floor_modes, no action_triggers, no trigger_keywords, "
            "and no 'semantic'/'pull' category route):",
            "  SKL-NOROUTE-1 (Skill) in CAT-NOROUTE-1 routes=[]",
        ]
        assert stderr_lines == []

    def test_example_lint_minimal(self) -> None:
        findings = make_findings(
            example_lint={
                "RULE-LINT-1": [
                    {
                        "field": "example",
                        "kind": "missing",
                        "detail": "no code block present",
                    }
                ]
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nExample lint (3.2) -- 1 defect(s) in 1 rule(s):",
            "  RULE-LINT-1 [example] missing: no code block present",
        ]
        assert stderr_lines == []

    def test_domain_enum(self) -> None:
        findings = make_findings(
            domain_enum=[
                {"node_id": "NODE-DOM-1", "label": "Rule", "domain": "bogus-domain"}
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDomain enum (3.5) -- 1 node(s) with an invalid domain:",
            "  NODE-DOM-1 (Rule): 'bogus-domain'",
        ]
        assert stderr_lines == []

    def test_counter_nodes_parity(self) -> None:
        findings = make_findings(
            counter_nodes_parity=[
                {
                    "node_id": "NODE-CNT-1",
                    "missing_from_field": ["X"],
                    "extra_in_field": [],
                }
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nCounter-nodes parity (3.5b) -- counter_nodes drifted from edges on 1 node(s):",
            "  NODE-CNT-1: missing_from_field=['X'] extra_in_field=[]",
        ]
        assert stderr_lines == []

    def test_dispatched_by_parity(self) -> None:
        findings = make_findings(
            dispatched_by_parity=[
                {
                    "node_id": "NODE-DBP-1",
                    "missing_from_field": [],
                    "extra_in_field": ["Y"],
                }
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatched-by parity (3.5b) -- dispatched_by drifted from edges on 1 node(s):",
            "  NODE-DBP-1: missing_from_field=[] extra_in_field=['Y']",
        ]
        assert stderr_lines == []

    def test_counter_nodes_parity_and_dispatched_by_parity_both_present_preserve_order(
        self,
    ) -> None:
        # Both entries of the shared cli.py:875-888 tuple loop present at
        # once: counter_nodes_parity's block must render before
        # dispatched_by_parity's, matching source order of the tuple.
        findings = make_findings(
            counter_nodes_parity=[
                {
                    "node_id": "NODE-CNT-1",
                    "missing_from_field": ["X"],
                    "extra_in_field": [],
                }
            ],
            dispatched_by_parity=[
                {
                    "node_id": "NODE-DBP-1",
                    "missing_from_field": [],
                    "extra_in_field": ["Y"],
                }
            ],
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nCounter-nodes parity (3.5b) -- counter_nodes drifted from edges on 1 node(s):",
            "  NODE-CNT-1: missing_from_field=['X'] extra_in_field=[]",
            "\nDispatched-by parity (3.5b) -- dispatched_by drifted from edges on 1 node(s):",
            "  NODE-DBP-1: missing_from_field=[] extra_in_field=['Y']",
        ]
        assert stderr_lines == []

    def test_enforceable_severity(self) -> None:
        findings = make_findings(
            enforceable_severity=[{"rule_id": "RULE-ENF-1", "severity": "critical"}]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nEnforceable-severity coupling (3.1) -- 1 enforceable "
            "critical/high rule(s) left advisory:",
            "  RULE-ENF-1 (critical): has MEP but mandatory=false",
        ]
        assert stderr_lines == []

    def test_forbidden_phrase_overlap(self) -> None:
        findings = make_findings(
            forbidden_phrase_overlap=[
                {"phrase": "always automate", "nodes": ["RULE-A", "RULE-B"]}
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nForbidden-phrase overlap (3.3) -- 1 phrase(s) in >1 FRB node:",
            "  'always automate' -> ['RULE-A', 'RULE-B']",
        ]
        assert stderr_lines == []

    def test_shared_code_example(self) -> None:
        findings = make_findings(
            shared_code_example=[
                {"rules": ["RULE-A", "RULE-B"], "block": "def foo(): pass"}
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nShared code example (3.3) -- 1 block(s) in >1 rule:",
            "  ['RULE-A', 'RULE-B']: 'def foo(): pass'",
        ]
        assert stderr_lines == []

    def test_dispatch_prose_parity_declared_but_unnamed_only(self) -> None:
        # Transcribed verbatim from plan.md's E4 `_render_dispatch_prose_parity`
        # block: a dedicated irregular renderer (like dispatch_invokes), not a
        # generic `_section` entry, keyed on `dispatch_prose_parity` and shaped
        # {declared_but_unnamed: [...], named_but_undeclared: [...]}.
        findings = make_findings(
            dispatch_prose_parity={
                "declared_but_unnamed": [
                    {"playbook": "PBK-PROC-AUDIT-FANOUT-001", "role": "ROL-EXPLORER-001"}
                ],
                "named_but_undeclared": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatch prose parity (1):",
            "  PBK-PROC-AUDIT-FANOUT-001 dispatches ROL-EXPLORER-001 but "
            "never names it in its own text",
        ]
        assert stderr_lines == []

    def test_dispatch_prose_parity_named_but_undeclared_only(self) -> None:
        findings = make_findings(
            dispatch_prose_parity={
                "declared_but_unnamed": [],
                "named_but_undeclared": [
                    {"playbook": "PBK-PROC-SDD-001", "role": "ROL-REVIEWER-001"}
                ],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatch prose parity (1):",
            "  PBK-PROC-SDD-001 names ROL-REVIEWER-001 in its text with no "
            "DISPATCHES edge",
        ]
        assert stderr_lines == []

    def test_dispatch_prose_parity_both_kinds_header_counts_the_sum_and_unnamed_renders_first(
        self,
    ) -> None:
        # Header count is len(unnamed) + len(undeclared), not either alone,
        # and declared_but_unnamed rows render before named_but_undeclared
        # rows (source order of plan.md's renderer).
        findings = make_findings(
            dispatch_prose_parity={
                "declared_but_unnamed": [{"playbook": "PBK-A", "role": "ROL-A"}],
                "named_but_undeclared": [{"playbook": "PBK-B", "role": "ROL-B"}],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDispatch prose parity (2):",
            "  PBK-A dispatches ROL-A but never names it in its own text",
            "  PBK-B names ROL-B in its text with no DISPATCHES edge",
        ]
        assert stderr_lines == []


class TestTruncation:
    """Per-entry truncation limits (cli.py:679, 701/703, 838/840/842) --
    header count vs. rendered-item count diverge on purpose."""

    def test_frequency_stale_truncates_at_10_header_shows_full_count(self) -> None:
        findings = make_findings(
            frequency_stale=[{"rule_id": f"RULE-FREQ-{i}"} for i in range(12)]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines[0] == "\nFrequency stale -- zero observations (12):"
        item_lines = stdout_lines[1:]
        assert len(item_lines) == 10
        assert item_lines == [f"  RULE-FREQ-{i}" for i in range(10)]
        assert stderr_lines == []

    def test_edge_parity_stale_and_missing_truncate_at_20_header_shows_full_counts(
        self,
    ) -> None:
        stale = [("RELATED_TO", f"SRC-{i}", f"TGT-{i}") for i in range(25)]
        missing = [("TEACHES", f"MSRC-{i}", f"MTGT-{i}") for i in range(25)]
        findings = make_findings(edge_parity={"stale": stale, "missing": missing})
        stdout_lines, stderr_lines = render_findings(findings)

        assert stdout_lines[0] == (
            "\nEdge parity drift -- run `writ reconcile` (stale=25, missing=25):"
        )
        stale_lines = stdout_lines[1:21]
        missing_lines = stdout_lines[21:41]
        assert len(stdout_lines) == 41
        assert stale_lines == [
            f"  stale (graph, not in source):   SRC-{i} -RELATED_TO-> TGT-{i}"
            for i in range(20)
        ]
        assert missing_lines == [
            f"  missing (source, not in graph): MSRC-{i} -TEACHES-> MTGT-{i}"
            for i in range(20)
        ]
        assert stderr_lines == []

    def test_push_reachability_truncates_orphans_and_dead_tags_but_not_empty_routes(
        self,
    ) -> None:
        ear = [f"CAT-{i}" for i in range(25)]
        po = [f"PORPHAN-{i}" for i in range(25)]
        dat = [f"DEADTAG-{i}" for i in range(25)]
        findings = make_findings(
            push_reachability={
                "empty_action_routes": ear,
                "push_orphans": po,
                "dead_action_tags": dat,
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)

        assert stdout_lines[0] == (
            "\nPush reachability (1.8) -- empty_action_routes=25, "
            "push_orphans=25, dead_action_tags=25:"
        )
        ear_lines = stdout_lines[1:26]
        po_lines = stdout_lines[26:46]
        dat_lines = stdout_lines[46:66]
        assert len(stdout_lines) == 66
        # empty_action_routes: NOT truncated -- all 25 render.
        assert ear_lines == [
            f"  empty action route (routes 'action', no member action_triggers): CAT-{i}"
            for i in range(25)
        ]
        # push_orphans: truncated to 20.
        assert po_lines == [
            f"  push orphan (no floor/action/keywords): PORPHAN-{i}" for i in range(20)
        ]
        # dead_action_tags: truncated to 20.
        assert dat_lines == [
            f"  dead action tag (action_triggers on a non-methodology node): DEADTAG-{i}"
            for i in range(20)
        ]
        assert stderr_lines == []

    def test_delivery_orphans_truncates_at_20_header_shows_full_count(self) -> None:
        do = {
            f"ANT-{i:03d}": {"label": "AntiPattern", "category": "CAT-X", "routes": []}
            for i in range(25)
        }
        findings = make_findings(delivery_orphans=do)
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines[0] == (
            "\nDelivery orphans (1.9) -- 25 methodology node(s) no channel can "
            "select (no floor_modes, no action_triggers, no trigger_keywords, "
            "and no 'semantic'/'pull' category route):"
        )
        item_lines = stdout_lines[1:]
        assert len(item_lines) == 20
        assert item_lines == [
            f"  ANT-{i:03d} (AntiPattern) in CAT-X routes=[]" for i in range(20)
        ]
        assert stderr_lines == []


class TestCountExpressions:
    """Header count expressions that differ from len(rendered items) or
    len(the top-level dict) -- cli.py:855-857 (example_lint)."""

    def test_example_lint_header_uses_total_defects_not_rule_count(self) -> None:
        findings = make_findings(
            example_lint={
                "RULE-A": [
                    {"field": "example", "kind": "missing", "detail": "no code block"},
                    {"field": "example", "kind": "stale", "detail": "output drifted"},
                ],
                "RULE-B": [
                    {"field": "detail", "kind": "vague", "detail": "too generic"}
                ],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        # n = sum(len(v) for v in el.values()) = 2 + 1 = 3 defects.
        # len(el) = 2 rules. The two numbers must NOT be conflated.
        assert stdout_lines[0] == "\nExample lint (3.2) -- 3 defect(s) in 2 rule(s):"
        assert stdout_lines[1:] == [
            "  RULE-A [example] missing: no code block",
            "  RULE-A [example] stale: output drifted",
            "  RULE-B [detail] vague: too generic",
        ]
        assert stderr_lines == []


class TestSpecialCases:
    """Irregular blocks with dedicated render logic: ranked_exclusion_mismatch
    (literal, not f-string, header), category_reachability (3 output
    shapes), orphan_counts_by_type (zero-filter + sort), and
    dangling_dispatched_roles' unresolvable arrow branch."""

    def test_ranked_exclusion_mismatch_header_is_literal_not_interpolated(self) -> None:
        findings = make_findings(
            ranked_exclusion_mismatch={
                "excluded_not_mandatory": ["RULE-EXCL-1"],
                "mandatory_not_excluded": ["RULE-MAND-1"],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        # cli.py:776 is a plain string literal, NOT an f-string: the braces
        # around excluded-from-ranked/mandatory must stay un-interpolated.
        assert stdout_lines[0] == (
            "\nRanked-exclusion mismatch ({excluded-from-ranked} != {mandatory}):"
        )
        assert "{excluded-from-ranked}" in stdout_lines[0]
        assert "{mandatory}" in stdout_lines[0]
        assert stdout_lines[1:] == [
            "  excluded-but-not-mandatory: RULE-EXCL-1",
            "  mandatory-but-not-excluded: RULE-MAND-1",
        ]
        assert stderr_lines == []

    def test_category_reachability_skipped(self) -> None:
        findings = make_findings(
            category_reachability={
                "skipped": True,
                "reason": "no bible_dir configured",
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nCategory reachability skipped: no bible_dir configured",
        ]
        assert stderr_lines == []

    def test_category_reachability_nodes_without_category_only(self) -> None:
        findings = make_findings(
            category_reachability={
                "nodes_without_category": [{"type": "Rule", "id": "RULE-NOCAT-1"}],
                "categories_without_route": [],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nNodes without category (1):",
            "  Rule: RULE-NOCAT-1",
        ]
        assert stderr_lines == []

    def test_category_reachability_categories_without_route_only(self) -> None:
        findings = make_findings(
            category_reachability={
                "nodes_without_category": [],
                "categories_without_route": ["orphan-category"],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nCategories without route (1):",
            "  orphan-category",
        ]
        assert stderr_lines == []

    def test_category_reachability_both_sections_present(self) -> None:
        findings = make_findings(
            category_reachability={
                "nodes_without_category": [{"type": "Rule", "id": "RULE-NOCAT-1"}],
                "categories_without_route": ["orphan-category"],
            }
        )
        stdout_lines, stderr_lines = render_findings(findings)
        # Source order: nodes_without_category section, THEN
        # categories_without_route section (cli.py:756-764).
        assert stdout_lines == [
            "\nNodes without category (1):",
            "  Rule: RULE-NOCAT-1",
            "\nCategories without route (1):",
            "  orphan-category",
        ]
        assert stderr_lines == []

    def test_orphan_counts_by_type_filters_zero_and_sorts_by_type(self) -> None:
        findings = make_findings(
            orphan_counts_by_type={"Rule": 3, "Concept": 0, "Playbook": 5}
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nOrphans by node type (all labels):",
            f"  {'Playbook':18s} 5",
            f"  {'Rule':18s} 3",
        ]
        assert stderr_lines == []

    def test_orphan_counts_by_type_all_zero_renders_nothing(self) -> None:
        findings = make_findings(orphan_counts_by_type={"Rule": 0, "Concept": 0})
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == []
        assert stderr_lines == []

    def test_dangling_dispatched_roles_unresolvable_uses_unresolvable_suffix(
        self,
    ) -> None:
        findings = make_findings(
            dangling_dispatched_roles=[
                {
                    "from": "writ-server",
                    "ref": "bogus-role",
                    "resolvable": False,
                }
            ]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nDangling dispatched_roles (1):",
            "  writ-server dispatched_roles='bogus-role' (unresolvable)",
        ]
        assert stderr_lines == []


class TestStderrSplit:
    """redundancy_unavailable is the ONLY err=True block (cli.py:668-671):
    its line must land in stderr_lines and never in stdout_lines."""

    def test_redundancy_unavailable_goes_to_stderr_only(self) -> None:
        findings = make_findings(
            redundancy_unavailable="sentence-transformers not installed"
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == []
        assert stderr_lines == [
            "\nRedundancy check skipped: sentence-transformers not installed",
        ]

    def test_redundancy_unavailable_does_not_leak_into_stdout_alongside_other_keys(
        self,
    ) -> None:
        findings = make_findings(
            redundancy_unavailable="onnxruntime not installed",
            conflicts=[{"rule_a": "RULE-A", "rule_b": "RULE-B"}],
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nConflicts (1):",
            "  RULE-A <-> RULE-B",
        ]
        assert stderr_lines == [
            "\nRedundancy check skipped: onnxruntime not installed",
        ]
        assert "Redundancy check skipped" not in "\n".join(stdout_lines)


class TestSilentKeys:
    """orphans_all_labels is populated by run_all_checks (integrity.py:1423)
    but was NEVER rendered by validate. The renderer must stay silent on it
    (pinned so a future table entry doesn't accidentally add output)."""

    def test_orphans_all_labels_present_produces_no_lines(self) -> None:
        findings = make_findings(
            orphans_all_labels=["NODE-1", "NODE-2", "NODE-3"]
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == []
        assert stderr_lines == []

    def test_orphans_all_labels_alongside_other_keys_still_silent(self) -> None:
        findings = make_findings(
            orphans_all_labels=["NODE-1"],
            stale=[{"rule_id": "RULE-STALE-1", "expired_on": "2026-01-01"}],
        )
        stdout_lines, stderr_lines = render_findings(findings)
        assert stdout_lines == [
            "\nStale (1):",
            "  RULE-STALE-1 (expired 2026-01-01)",
        ]
        assert not any("NODE-1" in line for line in stdout_lines)
        assert stderr_lines == []
