"""Program item 6, workstream G: authoring the trust metadata in markdown (capabilities 6-11).

Four props are AUTHORED in markdown (layer, basis, deliberate, verify_interval_days):
managed, so reconcile clears one removed from source. Five are GRAPH-ONLY
(approved_at, approval_via, last_verified, disputed, superseded): written only by the
graph, runtime-exempt, never read from or written to markdown.

Capability map
  Cap 6  -- RULE-START, NODE-START and front-matter Rules parse the four props alike
  Cap 7  -- absent props are not written; invalid values are an IngestError naming the
            field and the rule is not written
  Cap 8  -- export renders the four only when present, round-trips them, and never
            writes a graph-only prop (RULE-START and front-matter paths)
  Cap 9  -- a front-matter file declaring a graph-only prop has it dropped at parse
  Cap 10 -- managed versus exempt boundary; graph-only props survive import-markdown
            and reconcile, a removed authored prop is cleared (real graph)
  Cap 11 -- a live ingest seeds last_verified only where it is missing; a later ingest
            leaves seeded values; a dry run seeds nothing (real graph)

Real-graph claims run on the isolated instance under project "test-trust-auth" and
never touch the corpus.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_asyncio

from tests._graph import connection

PROJECT = "test-trust-auth"
AUTHORED = ("layer", "basis", "deliberate", "verify_interval_days")
GRAPH_ONLY = ("approved_at", "approval_via", "last_verified", "disputed", "superseded")

_BODY = """
### Trigger
When a thing happens.

### Statement
A statement for {rid}.

### Violation
```python
x = 1
```

### Pass
```python
y = 2
```

### Enforcement
Code review.

### Rationale
A rationale for {rid}.
"""


def _rule_start_md(rid: str, meta: str = "", edges: str = "") -> str:
    edge_block = f"\n### Edges\n{edges}\n" if edges else ""
    return (
        f"<!-- RULE START: {rid} -->\n## Rule {rid}\n\n"
        f"**Domain**: security\n**Severity**: Medium\n**Scope**: Component\n{meta}"
        + _BODY.format(rid=rid) + edge_block + f"\n<!-- RULE END: {rid} -->\n"
    )


def _node_start_md(rid: str, meta: str = "") -> str:
    return (
        f"<!-- NODE START type=Rule id={rid} -->\n"
        f"**Domain**: security\n**Severity**: Medium\n**Scope**: Component\n{meta}"
        + _BODY.format(rid=rid) + f"\n<!-- NODE END: {rid} -->\n"
    )


def _front_matter_md(rid: str, extra_yaml: str = "") -> str:
    return (
        "---\n"
        f"rule_id: {rid}\nnode_type: Rule\ndomain: security\nseverity: medium\n"
        f"scope: component\ntrigger: When a thing happens.\nstatement: A statement.\n"
        f"violation: v\npass_example: p\nenforcement: e\nrationale: r\n"
        f"category: CAT-TEST-001\n{extra_yaml}---\nbody text\n"
    )


_VALID_META = (
    "**Layer**: Observed\n**Basis**: code\n**Deliberate**: true\n**Verify_Interval_Days**: 90\n"
)
_VALID_YAML = "layer: observed\nbasis: code\ndeliberate: true\nverify_interval_days: 90\n"
_EXPECTED = {"layer": "observed", "basis": "code", "deliberate": True, "verify_interval_days": 90}


def _write(tmp_path: Path, name: str, text: str) -> Path:
    d = tmp_path / "bible"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _picked(node: dict) -> dict:
    return {k: node[k] for k in AUTHORED if k in node}


# ---------------------------------------------------------------------------
# Capability 6 (pure): three markdown formats, one parse result
# ---------------------------------------------------------------------------


class TestTrustMetadataParsesInAllThreeFormats:
    def test_rule_start_lines_parse_to_the_four_props(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_rules_from_file

        path = _write(tmp_path, "rs.md", _rule_start_md("TA-RS-001", _VALID_META))
        (rule,) = parse_rules_from_file(path)
        assert _picked(rule) == _EXPECTED

    def test_node_start_rule_block_parses_to_the_same_values(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        path = _write(tmp_path, "ns.md", _node_start_md("TA-NS-001", _VALID_META))
        (node,) = parse_nodes_from_file(path)
        assert node["node_type"] == "Rule"
        assert _picked(node) == _EXPECTED

    def test_front_matter_rule_parses_to_the_same_values(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        path = _write(tmp_path, "fm.md", _front_matter_md("TA-FM-001", _VALID_YAML))
        (node,) = parse_nodes_from_file(path)
        assert _picked(node) == _EXPECTED

    def test_the_three_formats_agree_with_each_other(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        parsed = [
            _picked(parse_nodes_from_file(_write(tmp_path, "a.md", _rule_start_md("TA-AG-001", _VALID_META)))[0]),
            _picked(parse_nodes_from_file(_write(tmp_path, "b.md", _node_start_md("TA-AG-002", _VALID_META)))[0]),
            _picked(parse_nodes_from_file(_write(tmp_path, "c.md", _front_matter_md("TA-AG-003", _VALID_YAML)))[0]),
        ]
        assert parsed[0] == parsed[1] == parsed[2]

    def test_the_underscore_less_spellings_are_accepted_like_other_metadata(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.ingest import parse_rules_from_file

        meta = "**VerifyIntervalDays**: 45\n"
        (rule,) = parse_rules_from_file(_write(tmp_path, "u.md", _rule_start_md("TA-US-001", meta)))
        assert rule["verify_interval_days"] == 45

    @pytest.mark.parametrize("layer", ["observed", "inferred", "unknown"])
    def test_every_layer_value_parses(self, tmp_path: Path, layer: str) -> None:
        from writ.graph.ingest import parse_rules_from_file

        (rule,) = parse_rules_from_file(
            _write(tmp_path, "l.md", _rule_start_md("TA-LY-001", f"**Layer**: {layer}\n")))
        assert rule["layer"] == layer

    @pytest.mark.parametrize("basis", ["code", "document", "ticket", "testimony"])
    def test_every_basis_value_parses(self, tmp_path: Path, basis: str) -> None:
        from writ.graph.ingest import parse_rules_from_file

        (rule,) = parse_rules_from_file(
            _write(tmp_path, "b.md", _rule_start_md("TA-BS-001", f"**Basis**: {basis}\n")))
        assert rule["basis"] == basis

    def test_layer_and_basis_are_lowercased(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_rules_from_file

        meta = "**Layer**: INFERRED\n**Basis**: Ticket\n"
        (rule,) = parse_rules_from_file(_write(tmp_path, "c.md", _rule_start_md("TA-LC-001", meta)))
        assert (rule["layer"], rule["basis"]) == ("inferred", "ticket")

    @pytest.mark.parametrize("raw,expected", [("true", True), ("True", True), ("false", False)])
    def test_deliberate_is_a_bool_from_true_or_false(
        self, tmp_path: Path, raw: str, expected: bool
    ) -> None:
        from writ.graph.ingest import parse_rules_from_file

        (rule,) = parse_rules_from_file(
            _write(tmp_path, "d.md", _rule_start_md("TA-DL-001", f"**Deliberate**: {raw}\n")))
        assert rule["deliberate"] is expected


# ---------------------------------------------------------------------------
# Capability 7: absent props stay absent; invalid values are named IngestErrors
# ---------------------------------------------------------------------------


class TestAbsentTrustPropsAreNotWritten:
    def test_a_rule_declaring_none_parses_without_any_of_the_four(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_rules_from_file

        (rule,) = parse_rules_from_file(_write(tmp_path, "n.md", _rule_start_md("TA-NO-001")))
        for key in AUTHORED + GRAPH_ONLY:
            assert key not in rule, f"{key} must not be invented by the parser"

    def test_the_other_two_formats_also_omit_them(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        ns = parse_nodes_from_file(_write(tmp_path, "n1.md", _node_start_md("TA-NO-002")))[0]
        fm = parse_nodes_from_file(_write(tmp_path, "n2.md", _front_matter_md("TA-NO-003")))[0]
        for node in (ns, fm):
            for key in AUTHORED + GRAPH_ONLY:
                assert key not in node, f"{key} must not be invented by the parser"

    @pytest.mark.asyncio
    async def test_the_written_rule_node_carries_none_of_the_four(self, db, tmp_path: Path) -> None:
        from writ.graph.methodology_ingest import ingest_path

        _write(tmp_path, "w.md", _rule_start_md("TA-NOW-001"))
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        props = await _props(db, "TA-NOW-001")
        assert props is not None, "the rule must be written"
        for key in AUTHORED:
            assert key not in props, f"{key} was persisted from a model default"


class TestInvalidTrustPropsAreIngestErrors:
    @pytest.mark.parametrize(
        "meta,field",
        [
            ("**Layer**: guessed\n", "layer"),
            ("**Basis**: rumor\n", "basis"),
            ("**Verify_Interval_Days**: soon\n", "verify_interval_days"),
            ("**Verify_Interval_Days**: 0\n", "verify_interval_days"),
            ("**Verify_Interval_Days**: -30\n", "verify_interval_days"),
            ("**Verify_Interval_Days**: 7.5\n", "verify_interval_days"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_invalid_value_is_reported_by_field_and_the_rule_is_not_written(
        self, db, tmp_path: Path, meta: str, field: str
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        rid = "TA-BAD-001"
        _write(tmp_path, "bad.md", _rule_start_md(rid, meta))
        report = await ingest_path(tmp_path / "bible", db, project=PROJECT)
        named = [e for e in report.errors if e.field == field]
        assert named, f"no IngestError names {field!r}; got {[str(e) for e in report.errors]}"
        assert named[0].node_id == rid
        assert await _props(db, rid) is None, "an invalid rule must not be written"

    @pytest.mark.parametrize(
        "yaml_lines,field",
        [("layer: guessed\n", "layer"), ("basis: rumor\n", "basis"),
         ("verify_interval_days: 0\n", "verify_interval_days")],
    )
    @pytest.mark.asyncio
    async def test_front_matter_validates_the_same_way(
        self, db, tmp_path: Path, yaml_lines: str, field: str
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        _write(tmp_path, "fmbad.md", _front_matter_md("TA-BADFM-001", yaml_lines))
        report = await ingest_path(tmp_path / "bible", db, project=PROJECT, dry_run=True)
        assert [e for e in report.errors if e.field == field], [str(e) for e in report.errors]

    @pytest.mark.asyncio
    async def test_a_valid_neighbour_is_still_written(self, db, tmp_path: Path) -> None:
        from writ.graph.methodology_ingest import ingest_path

        text = (_rule_start_md("TA-BADMIX-001", "**Layer**: guessed\n")
                + _rule_start_md("TA-GOODMIX-001", _VALID_META))
        _write(tmp_path, "mix.md", text)
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        assert await _props(db, "TA-BADMIX-001") is None
        good = await _props(db, "TA-GOODMIX-001")
        assert good is not None
        assert {k: good[k] for k in AUTHORED} == _EXPECTED


# ---------------------------------------------------------------------------
# Capability 8 (pure): export renders only what is present and round-trips it
# ---------------------------------------------------------------------------


def _exportable(**extra) -> dict:
    base = {
        "rule_id": "TA-EXP-001", "domain": "security", "severity": "medium",
        "scope": "component", "mandatory": False, "trigger": "When a thing happens.",
        "statement": "A statement.", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r",
    }
    return {**base, **extra}


class TestExportRendersAuthoredProps:
    def test_a_rule_with_none_of_the_four_renders_no_trust_line(self) -> None:
        from writ.export import rule_to_markdown

        text = rule_to_markdown(_exportable())
        for label in ("**Layer**", "**Basis**", "**Deliberate**", "**Verify_Interval_Days**"):
            assert label not in text

    def test_each_present_prop_renders_one_metadata_line(self) -> None:
        from writ.export import rule_to_markdown

        text = rule_to_markdown(_exportable(
            layer="observed", basis="code", deliberate=True, verify_interval_days=90))
        assert "**Layer**: observed" in text
        assert "**Basis**: code" in text
        assert "**Deliberate**: true" in text
        assert "**Verify_Interval_Days**: 90" in text

    def test_only_the_present_props_render(self) -> None:
        from writ.export import rule_to_markdown

        text = rule_to_markdown(_exportable(layer="inferred"))
        assert "**Layer**: inferred" in text
        assert "**Basis**" not in text and "**Deliberate**" not in text
        assert "**Verify_Interval_Days**" not in text

    def test_the_lines_sit_in_the_metadata_block_after_trigger_keywords(self) -> None:
        from writ.export import rule_to_markdown

        text = rule_to_markdown(_exportable(
            trigger_keywords=["alpha", "beta"], layer="observed", basis="code"))
        lines = text.splitlines()
        kw = next(i for i, l in enumerate(lines) if l.startswith("**Trigger_Keywords**"))
        layer_at = [i for i, l in enumerate(lines) if l.startswith("**Layer**")]
        trigger_heading = next(i for i, l in enumerate(lines) if l.startswith("### Trigger"))
        assert layer_at, "no **Layer** line was rendered"
        assert kw < layer_at[0] < trigger_heading

    def test_a_declared_false_deliberate_is_rendered_because_presence_is_declaration(self) -> None:
        from writ.export import rule_to_markdown

        assert "**Deliberate**: false" in rule_to_markdown(_exportable(deliberate=False))

    def test_the_four_props_round_trip_through_export_then_ingest_parse(
        self, tmp_path: Path
    ) -> None:
        from writ.export import rule_to_markdown
        from writ.graph.ingest import parse_rules_from_file

        text = rule_to_markdown(_exportable(
            layer="inferred", basis="testimony", deliberate=True, verify_interval_days=30))
        (rule,) = parse_rules_from_file(_write(tmp_path, "rt.md", text))
        assert _picked(rule) == {
            "layer": "inferred", "basis": "testimony", "deliberate": True,
            "verify_interval_days": 30}

    def test_a_rule_with_none_of_the_four_round_trips_to_none_of_the_four(
        self, tmp_path: Path
    ) -> None:
        from writ.export import rule_to_markdown
        from writ.graph.ingest import parse_rules_from_file

        (rule,) = parse_rules_from_file(_write(tmp_path, "rt0.md", rule_to_markdown(_exportable())))
        assert _picked(rule) == {}


class TestExportNeverWritesGraphOnlyProps:
    _STATE = {
        "approved_at": "2026-10-06T00:00:00+00:00", "approval_via": "review_promote",
        "last_verified": "2026-10-06", "disputed": True, "superseded": True,
    }

    def test_graph_only_fields_covers_the_five(self) -> None:
        from writ.export import GRAPH_ONLY_FIELDS

        assert set(GRAPH_ONLY) <= set(GRAPH_ONLY_FIELDS)

    def test_rule_start_export_writes_none_of_them(self) -> None:
        from writ.export import rule_to_markdown

        text = rule_to_markdown(_exportable(layer="observed", **self._STATE))
        for name in GRAPH_ONLY:
            assert name not in text, name
        assert "2026-10-06" not in text and "review_promote" not in text
        assert "**Layer**: observed" in text

    def test_front_matter_export_strips_them_and_keeps_the_authored_four(self) -> None:
        import yaml

        from writ.export import node_to_yaml_frontmatter

        text = node_to_yaml_frontmatter(
            _exportable(layer="observed", basis="code", deliberate=True,
                        verify_interval_days=90, **self._STATE),
            node_type="Rule")
        front = yaml.safe_load(text.split("---\n")[1])
        for name in GRAPH_ONLY:
            assert name not in front, f"front-matter export wrote graph-only {name}"
        assert {k: front[k] for k in AUTHORED} == _EXPECTED

    def test_front_matter_export_then_parse_round_trips_the_authored_four(
        self, tmp_path: Path
    ) -> None:
        from writ.export import node_to_yaml_frontmatter
        from writ.graph.ingest import parse_nodes_from_file

        text = node_to_yaml_frontmatter(
            _exportable(category="CAT-TEST-001", layer="observed", basis="code",
                        deliberate=True, verify_interval_days=90, **self._STATE),
            node_type="Rule")
        (node,) = parse_nodes_from_file(_write(tmp_path, "fmrt.md", text))
        assert _picked(node) == _EXPECTED
        assert not (set(GRAPH_ONLY) & set(node))


# ---------------------------------------------------------------------------
# Capability 9: a front-matter file can never author a graph-only value
# ---------------------------------------------------------------------------


class TestFrontMatterDropsGraphOnlyProps:
    _DECLARED = (
        "approved_at: '2020-01-01T00:00:00+00:00'\napproval_via: review_promote\n"
        "last_verified: '2020-01-01'\ndisputed: false\nsuperseded: true\n"
    )

    def test_each_graph_only_key_is_dropped_at_parse(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        (node,) = parse_nodes_from_file(
            _write(tmp_path, "g.md", _front_matter_md("TA-GO-001", self._DECLARED)))
        for name in GRAPH_ONLY:
            assert name not in node, f"front matter authored the graph-only prop {name}"

    def test_dropping_graph_only_keys_leaves_authored_keys_alone(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        (node,) = parse_nodes_from_file(
            _write(tmp_path, "g2.md", _front_matter_md("TA-GO-002", _VALID_YAML + self._DECLARED)))
        assert _picked(node) == _EXPECTED

    def test_a_single_declared_key_is_enough_to_be_dropped(self, tmp_path: Path) -> None:
        from writ.graph.ingest import parse_nodes_from_file

        (node,) = parse_nodes_from_file(
            _write(tmp_path, "g3.md", _front_matter_md("TA-GO-003", "disputed: false\n")))
        assert "disputed" not in node

    @pytest.mark.asyncio
    async def test_ingest_never_writes_a_graph_only_value_from_markdown(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        _write(tmp_path, "g4.md", _front_matter_md("TA-GO-004", self._DECLARED))
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        props = await _props(db, "TA-GO-004")
        assert props is not None
        assert props.get("approved_at") is None
        assert props.get("approval_via") is None
        assert props.get("disputed") is None, "markdown must not author a disputed value"
        assert props.get("last_verified") != "2020-01-01", "markdown must not set the clock"
        assert props.get("superseded") is None or props.get("superseded") is False


# ---------------------------------------------------------------------------
# Capability 10: managed versus exempt boundary on the real graph
# ---------------------------------------------------------------------------


class TestManagedVersusExemptBoundary:
    def test_the_authored_four_are_managed(self) -> None:
        from writ.graph.schema import MANAGED_PROP_NAMES

        assert set(AUTHORED) <= set(MANAGED_PROP_NAMES)

    def test_the_graph_only_five_are_exempt_and_not_managed(self) -> None:
        from writ.graph.schema import MANAGED_PROP_NAMES, RUNTIME_EXEMPT_PROPS

        assert set(GRAPH_ONLY) <= set(RUNTIME_EXEMPT_PROPS)
        assert not (set(GRAPH_ONLY) & set(MANAGED_PROP_NAMES))

    @pytest.mark.asyncio
    async def test_reconcile_clears_an_authored_prop_removed_from_source(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        rid = "TA-CLR-001"
        path = _write(tmp_path, "clr.md", _rule_start_md(rid, _VALID_META))
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        assert (await _props(db, rid))["layer"] == "observed"

        path.write_text(_rule_start_md(rid, "**Basis**: code\n"), encoding="utf-8")
        result = await reconcile(tmp_path / "bible", db, project=PROJECT)

        props = await _props(db, rid)
        assert "layer" not in props, "a removed authored prop must be cleared"
        assert "deliberate" not in props and "verify_interval_days" not in props
        assert props["basis"] == "code", "the still-declared prop must stay"
        assert "layer" in result["cleared_props"].get(rid, []), result["cleared_props"]

    @pytest.mark.asyncio
    async def test_graph_only_props_survive_import_markdown_and_reconcile(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path, reconcile

        rid = "TA-SURV-001"
        _write(tmp_path, "surv.md", _rule_start_md(rid, _VALID_META))
        bible = tmp_path / "bible"
        await ingest_path(bible, db, project=PROJECT)
        written = {
            "approved_at": "2026-10-01T00:00:00+00:00", "approval_via": "review_promote",
            "last_verified": "2026-10-01", "disputed": True,
        }
        await db.set_rule_trust_props(rid, written)

        await ingest_path(bible, db, project=PROJECT)
        after_import = await _props(db, rid)
        assert {k: after_import[k] for k in written} == written

        result = await reconcile(bible, db, project=PROJECT)
        after_reconcile = await _props(db, rid)
        assert {k: after_reconcile[k] for k in written} == written
        cleared = set(result["cleared_props"].get(rid, []))
        assert not (cleared & set(GRAPH_ONLY)), cleared


# ---------------------------------------------------------------------------
# Capability 11: seeding last_verified
# ---------------------------------------------------------------------------


class TestSeedLastVerified:
    @pytest.mark.asyncio
    async def test_a_live_ingest_seeds_today_on_every_new_rule(self, db, tmp_path: Path) -> None:
        from writ.graph.methodology_ingest import ingest_path

        _write(tmp_path, "s.md", _rule_start_md("TA-SEED-001") + _rule_start_md("TA-SEED-002"))
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        today = date.today().isoformat()
        assert (await _props(db, "TA-SEED-001"))["last_verified"] == today
        assert (await _props(db, "TA-SEED-002"))["last_verified"] == today

    @pytest.mark.asyncio
    async def test_a_rule_that_already_has_a_value_keeps_it_and_a_new_one_gets_today(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = tmp_path / "bible"
        _write(tmp_path, "s2.md", _rule_start_md("TA-SEED-010"))
        await ingest_path(bible, db, project=PROJECT)
        await db.set_rule_trust_props("TA-SEED-010", {"last_verified": "2025-01-01"})

        _write(tmp_path, "s2.md", _rule_start_md("TA-SEED-010") + _rule_start_md("TA-SEED-011"))
        await ingest_path(bible, db, project=PROJECT)

        assert (await _props(db, "TA-SEED-010"))["last_verified"] == "2025-01-01"
        assert (await _props(db, "TA-SEED-011"))["last_verified"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_a_second_ingest_on_a_later_date_leaves_seeded_values_unchanged(
        self, db, tmp_path: Path
    ) -> None:
        from writ.graph.methodology_ingest import ingest_path

        bible = tmp_path / "bible"
        _write(tmp_path, "s3.md", _rule_start_md("TA-SEED-020"))
        await ingest_path(bible, db, project=PROJECT)
        seeded = (await _props(db, "TA-SEED-020"))["last_verified"]

        await db.seed_last_verified(PROJECT, "2099-12-31")

        assert (await _props(db, "TA-SEED-020"))["last_verified"] == seeded
        await ingest_path(bible, db, project=PROJECT)
        assert (await _props(db, "TA-SEED-020"))["last_verified"] == seeded

    @pytest.mark.asyncio
    async def test_the_store_seed_fills_only_rules_that_lack_the_value(self, db) -> None:
        await db.create_rule(_rule_dict("TA-SEED-030"))
        await db.create_rule(_rule_dict("TA-SEED-031"))
        await db.set_rule_trust_props("TA-SEED-031", {"last_verified": "2024-02-02"})

        await db.seed_last_verified(PROJECT, "2026-10-06")

        assert (await _props(db, "TA-SEED-030"))["last_verified"] == "2026-10-06"
        assert (await _props(db, "TA-SEED-031"))["last_verified"] == "2024-02-02"

    @pytest.mark.asyncio
    async def test_the_seed_is_scoped_to_one_project(self, db) -> None:
        other = PROJECT + "-other"
        await db.create_rule(_rule_dict("TA-SEED-040", project=other))
        try:
            await db.seed_last_verified(PROJECT, "2026-10-06")
            assert (await _props(db, "TA-SEED-040", other)).get("last_verified") is None
        finally:
            await db.clear_project(other)

    @pytest.mark.asyncio
    async def test_a_live_ingest_runs_the_seed_exactly_once(
        self, db, tmp_path: Path, monkeypatch
    ) -> None:
        from writ.graph.db import Neo4jConnection
        from writ.graph.methodology_ingest import ingest_path

        calls: list[tuple] = []
        original = Neo4jConnection.seed_last_verified

        async def spy(self, *args, **kwargs):
            calls.append((args, kwargs))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(Neo4jConnection, "seed_last_verified", spy)
        _write(tmp_path, "s4.md", _rule_start_md("TA-SEED-050"))
        await ingest_path(tmp_path / "bible", db, project=PROJECT)
        assert len(calls) == 1, calls

    @pytest.mark.asyncio
    async def test_a_dry_run_seeds_nothing_and_does_not_call_the_seed(
        self, db, tmp_path: Path, monkeypatch
    ) -> None:
        from writ.graph.db import Neo4jConnection
        from writ.graph.methodology_ingest import ingest_path

        await db.create_rule(_rule_dict("TA-SEED-060"))
        calls: list[tuple] = []
        original = Neo4jConnection.seed_last_verified

        async def spy(self, *args, **kwargs):
            calls.append((args, kwargs))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(Neo4jConnection, "seed_last_verified", spy)
        _write(tmp_path, "s5.md", _rule_start_md("TA-SEED-060"))
        report = await ingest_path(tmp_path / "bible", db, project=PROJECT, dry_run=True)

        assert report.dry_run is True
        assert calls == []
        assert (await _props(db, "TA-SEED-060")).get("last_verified") is None


# ---------------------------------------------------------------------------
# Fixtures and helpers (real graph, project-scoped)
# ---------------------------------------------------------------------------


def _rule_dict(rule_id: str, project: str = PROJECT) -> dict:
    return {
        "rule_id": rule_id, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": "t", "statement": "s", "violation": "v", "pass_example": "p",
        "enforcement": "e", "rationale": "r", "last_validated": "2026-03-15",
        "project": project,
    }


async def _props(db, rule_id: str, project: str = PROJECT) -> dict | None:
    rows = list(await db._run(
        "MATCH (r:Rule {rule_id: $id, project: $p}) RETURN properties(r) AS props",
        id=rule_id, p=project))
    return dict(rows[0]["props"]) if rows else None


@pytest_asyncio.fixture()
async def db():
    conn = connection()
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("isolated test Neo4j (bolt://localhost:7688) unreachable; run "
                    "`bash scripts/test-graph.sh up`")
    await conn.clear_project(PROJECT)
    yield conn
    await conn.clear_project(PROJECT)
    await conn.close()
