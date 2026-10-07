"""Program item 6 (attribution and trust records), capabilities 18 to 23: the STALE and
DELIBERATE tags, from the shared staleness rule to the lines the agent reads.

Five seams, one definition each:

  * writ/shared/trust.py: VERIFY_INTERVAL_DAYS_DEFAULT, is_verify_stale and trust_tags, the
    ONE staleness rule and the ONE tag spelling the ranked header, the /always-on route and
    both always-on renderers share (capabilities 18).
  * writ/shared/injection_text.py: always_on_head_line, the one spelling of an always-on
    rule's head line, called by both always-on renderers so their byte parity cannot drift
    (capability 21).
  * the /always-on route, which replaces the raw clock fields with a `stale` boolean per
    Rule row and passes `deliberate` through, with the statement count it had before
    (capability 21).
  * render_always_on and render_always_on_section, byte-identical to each other and to
    today when nothing is tagged, and inside the character limit when everything is
    (capabilities 21 and 22).
  * `writ review --verify`, after which a rebuilt pipeline's header carries no STALE tag
    (capability 23).

The ranked-header chain (metadata -> query -> projection -> cmd_format, every budget mode,
the _summary_with_abstractions fallback) lives in tests/test_ranked_header_fields.py, where
the anti-recurrence discipline for that chain already is.

RED today: writ.shared.trust does not exist, always_on_head_line does not exist, the
/always-on rows carry neither `stale` nor `deliberate`, the always-on renderers print no
tags, and `writ review` has no --verify.

REAL GRAPH (ENF-SYS-005), on the isolated instance: the /always-on statement count and the
verify-then-rebuild chain cannot be proven with a mock, because both are claims about what
the graph returns. The pure pieces (is_verify_stale, trust_tags, the renderers) need none.
Dates are relative to the process clock with a margin of days, so nothing here depends on
the instant the suite runs.
"""
from __future__ import annotations

import ast
import asyncio
import os
from datetime import date, timedelta

import pytest
import pytest_asyncio
from typer.testing import CliRunner

from tests.fixtures.server_routes import always_on, route_db  # noqa: F401  (fixtures)
from tests.test_review_promote_authority import (  # noqa: F401  (autouse leak guard + helpers)
    _mint_cleanup,
    _no_leaked_gate_tokens,
    _sid,
)
from writ.cli import app

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
runner = CliRunner()


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Capability 18: is_verify_stale
# ---------------------------------------------------------------------------


class TestIsVerifyStale:
    LAST = "2026-01-01"
    LAST_DATE = date(2026, 1, 1)

    def test_the_default_interval_is_one_hundred_eighty_days(self):
        from writ.shared.trust import VERIFY_INTERVAL_DAYS_DEFAULT

        assert VERIFY_INTERVAL_DAYS_DEFAULT == 180

    def test_not_stale_on_the_last_day_of_the_interval(self):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(self.LAST, 30, self.LAST_DATE + timedelta(days=30)) is False

    def test_stale_one_day_after_the_interval(self):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(self.LAST, 30, self.LAST_DATE + timedelta(days=31)) is True

    def test_stale_well_after_the_interval(self):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(self.LAST, 30, self.LAST_DATE + timedelta(days=400)) is True

    def test_not_stale_on_the_day_it_was_verified(self):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(self.LAST, 30, self.LAST_DATE) is False

    def test_an_absent_interval_uses_one_hundred_eighty_days(self):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(self.LAST, None, self.LAST_DATE + timedelta(days=180)) is False
        assert is_verify_stale(self.LAST, None, self.LAST_DATE + timedelta(days=181)) is True

    def test_a_custom_interval_replaces_the_default(self):
        from writ.shared.trust import is_verify_stale

        today = self.LAST_DATE + timedelta(days=45)
        assert is_verify_stale(self.LAST, 30, today) is True
        assert is_verify_stale(self.LAST, 90, today) is False

    def test_a_missing_last_verified_is_never_stale(self):
        """A rule created by `writ add` has no clock until the next ingest: fail open."""
        from writ.shared.trust import is_verify_stale

        far_future = date(2099, 1, 1)
        assert is_verify_stale(None, 30, far_future) is False
        assert is_verify_stale("", 30, far_future) is False

    @pytest.mark.parametrize("garbage", ["not-a-date", "2026-13-45", "01/02/2026", "   "])
    def test_an_unparseable_last_verified_is_never_stale(self, garbage):
        from writ.shared.trust import is_verify_stale

        assert is_verify_stale(garbage, 30, date(2099, 1, 1)) is False

    def test_the_result_is_a_real_boolean(self):
        from writ.shared.trust import is_verify_stale

        assert isinstance(is_verify_stale(self.LAST, 30, date(2099, 1, 1)), bool)
        assert isinstance(is_verify_stale(None, 30, date(2099, 1, 1)), bool)


class TestTrustTags:
    def test_stale_alone(self):
        from writ.shared.trust import trust_tags

        assert list(trust_tags({"stale": True})) == ["STALE"]

    def test_deliberate_alone(self):
        from writ.shared.trust import trust_tags

        assert list(trust_tags({"deliberate": True})) == ["DELIBERATE"]

    def test_both_render_stale_first(self):
        from writ.shared.trust import trust_tags

        assert list(trust_tags({"stale": True, "deliberate": True})) == ["STALE", "DELIBERATE"]

    @pytest.mark.parametrize("entry", [
        {}, {"stale": False}, {"deliberate": False}, {"stale": False, "deliberate": False},
        {"stale": None, "deliberate": None},
    ])
    def test_a_rule_that_is_neither_has_no_tags(self, entry):
        from writ.shared.trust import trust_tags

        assert list(trust_tags(entry)) == []

    def test_it_reads_only_the_computed_flags_not_the_raw_clock(self):
        """The entry carries `stale` and `deliberate` already computed; a raw last_verified
        far in the past on the same dict must not produce a tag by itself."""
        from writ.shared.trust import trust_tags

        assert list(trust_tags({"last_verified": "2001-01-01"})) == []

    def test_the_module_sits_below_retrieval_session_and_server(self):
        """Lowest layer, importable from all three, as writ/shared/injection_text.py is:
        it may import none of them."""
        path = os.path.join(SKILL_ROOT, "writ", "shared", "trust.py")
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        forbidden = ("writ.retrieval", "writ.session", "writ.server", "writ.graph")
        offenders = sorted(m for m in imported if m.startswith(forbidden))
        assert offenders == [], f"writ/shared/trust.py imports upward: {offenders}"


# ---------------------------------------------------------------------------
# Capability 21 (pure half): the head line and both always-on renderers
# ---------------------------------------------------------------------------

UNTAGGED_GOLDEN = (
    "=== ALWAYS-ACTIVE RULES ===\n"
    "[ENF-A-001] WHEN: trigger a\n"
    "  statement a\n"
    "[ENF-B-001] WHEN: trigger b\n"
    "  statement b\n"
    "=== END ALWAYS-ACTIVE RULES ==="
)


def _ao(*rows: dict) -> dict:
    return {"rules": list(rows), "total_tokens": 40, "cap": 5000}


def _row(rule_id: str, trigger: str, statement: str, **flags) -> dict:
    return {"rule_id": rule_id, "trigger": trigger, "statement": statement, **flags}


class TestAlwaysOnHeadLine:
    def test_an_untagged_rule_keeps_the_head_line_it_always_had(self):
        from writ.shared.injection_text import always_on_head_line, pointer_line

        assert always_on_head_line("ENF-X-001", "the trigger", []) == "[ENF-X-001] WHEN: the trigger"
        assert always_on_head_line("ENF-X-001", "the trigger", []) == pointer_line(
            "ENF-X-001", "the trigger",
        )

    def test_a_stale_rule_carries_the_tag_before_when(self):
        from writ.shared.injection_text import always_on_head_line

        assert always_on_head_line("ENF-X-001", "the trigger", ["STALE"]) == (
            "[ENF-X-001] (STALE) WHEN: the trigger"
        )

    def test_two_tags_share_one_parenthesis_in_the_slot_spelling(self):
        from writ.shared.injection_text import always_on_head_line

        assert always_on_head_line("ENF-X-001", "the trigger", ["STALE", "DELIBERATE"]) == (
            "[ENF-X-001] (STALE, DELIBERATE) WHEN: the trigger"
        )

    def test_the_pointer_line_is_unchanged_for_a_collapsed_rule(self):
        from writ.shared.injection_text import pointer_line

        assert pointer_line("ENF-X-001", "  the trigger  ") == "[ENF-X-001] WHEN: the trigger"


class TestAlwaysOnRenderersWithoutTags:
    """With no tagged rule both renderers produce the bytes they produced before."""

    def _untagged(self):
        return _ao(
            _row("ENF-A-001", "trigger a", "statement a"),
            _row("ENF-B-001", "trigger b", "statement b"),
        )

    def test_render_always_on_is_byte_identical_to_the_golden_text(self):
        from writ.retrieval.prompt_bundle import render_always_on

        text, _tokens, count = render_always_on(self._untagged())
        assert text == UNTAGGED_GOLDEN
        assert count == 2

    def test_render_always_on_section_is_byte_identical_to_the_golden_text(self):
        from writ.retrieval.injection_ceiling import render_always_on_section

        assert render_always_on_section(self._untagged(), set(), 10_000).text == UNTAGGED_GOLDEN

    def test_explicit_false_flags_render_the_same_bytes_as_no_flags(self):
        from writ.retrieval.injection_ceiling import render_always_on_section
        from writ.retrieval.prompt_bundle import render_always_on

        flagged = _ao(
            _row("ENF-A-001", "trigger a", "statement a", stale=False, deliberate=False),
            _row("ENF-B-001", "trigger b", "statement b", stale=False, deliberate=False),
        )
        assert render_always_on(flagged)[0] == UNTAGGED_GOLDEN
        assert render_always_on_section(flagged, set(), 10_000).text == UNTAGGED_GOLDEN


class TestAlwaysOnRenderersWithTags:
    def _tagged(self):
        return _ao(
            _row("ENF-A-001", "trigger a", "statement a", stale=True, deliberate=False),
            _row("ENF-B-001", "trigger b", "statement b", stale=False, deliberate=True),
            _row("ENF-C-001", "trigger c", "statement c", stale=True, deliberate=True),
            _row("ENF-D-001", "trigger d", "statement d", stale=False, deliberate=False),
        )

    def test_render_always_on_prints_each_tag_before_when(self):
        from writ.retrieval.prompt_bundle import render_always_on

        text, _tokens, _count = render_always_on(self._tagged())
        assert "[ENF-A-001] (STALE) WHEN: trigger a\n  statement a" in text
        assert "[ENF-B-001] (DELIBERATE) WHEN: trigger b\n  statement b" in text
        assert "[ENF-C-001] (STALE, DELIBERATE) WHEN: trigger c\n  statement c" in text
        assert "[ENF-D-001] WHEN: trigger d\n  statement d" in text

    def test_the_two_renderers_stay_byte_identical_to_each_other_when_tagged(self):
        from writ.retrieval.injection_ceiling import render_always_on_section
        from writ.retrieval.prompt_bundle import render_always_on

        ao = self._tagged()
        assert render_always_on_section(ao, set(), 100_000).text == render_always_on(ao)[0]

    def test_a_collapsed_rule_prints_a_pointer_with_no_tag(self):
        """pointer_line is unchanged: a rule shown in full earlier collapses to one WHEN
        line, tagged or not."""
        from writ.retrieval.injection_ceiling import render_always_on_section

        text = render_always_on_section(self._tagged(), {"ENF-A-001"}, 100_000).text
        assert "[ENF-A-001] WHEN: trigger a" in text
        assert "[ENF-A-001] (STALE)" not in text
        assert "[ENF-B-001] (DELIBERATE) WHEN: trigger b" in text

    def test_renderable_rows_carry_the_tags_and_the_recorded_ids_are_unchanged(self):
        from writ.retrieval.prompt_bundle import _renderable_always_on, always_on_rule_ids

        rows = _renderable_always_on(self._tagged())
        assert [list(r["tags"]) for r in rows] == [
            ["STALE"], ["DELIBERATE"], ["STALE", "DELIBERATE"], [],
        ]
        assert always_on_rule_ids(self._tagged()) == [
            "ENF-A-001", "ENF-B-001", "ENF-C-001", "ENF-D-001",
        ]

    def test_a_row_missing_its_trigger_is_still_dropped_with_its_tag(self):
        from writ.retrieval.prompt_bundle import render_always_on

        ao = _ao(_row("ENF-A-001", "", "statement a", stale=True),
                 _row("ENF-B-001", "trigger b", "statement b"))
        text, _tokens, _count = render_always_on(ao)
        assert "ENF-A-001" not in text
        assert "[ENF-B-001] WHEN: trigger b" in text


# ---------------------------------------------------------------------------
# Capability 22: the character limit counts the tags
# ---------------------------------------------------------------------------


class TestAlwaysOnCharacterLimitCountsTags:
    def _ao(self, *, tagged: bool) -> dict:
        flags = {"stale": True, "deliberate": True} if tagged else {}
        return _ao(*[
            _row(f"ENF-LIM-{n:03d}", f"trigger number {n} " + "w" * 40, f"statement {n} " + "s" * 60, **flags)
            for n in range(1, 7)
        ])

    def test_tags_are_inside_the_measured_text_so_a_limit_that_fits_untagged_collapses_tagged(self):
        from writ.retrieval.injection_ceiling import render_always_on_section

        untagged_full = render_always_on_section(self._ao(tagged=False), set(), 1_000_000)
        limit = len(untagged_full.text)
        assert len(untagged_full.full_ids) == 6

        same_limit_untagged = render_always_on_section(self._ao(tagged=False), set(), limit)
        assert len(same_limit_untagged.full_ids) == 6

        tagged = render_always_on_section(self._ao(tagged=True), set(), limit)
        assert len(tagged.text) <= limit
        assert len(tagged.full_ids) < 6, (
            "every rule is tagged, so the tags add characters to each full block and "
            "the same limit must now collapse or drop some"
        )

    def test_a_tagged_block_never_exceeds_its_limit_across_a_sweep_of_limits(self):
        from writ.retrieval.injection_ceiling import render_always_on_section

        ao = self._ao(tagged=True)
        full_len = len(render_always_on_section(ao, set(), 1_000_000).text)
        assert "(STALE, DELIBERATE)" in render_always_on_section(ao, set(), 1_000_000).text
        for limit in range(150, full_len + 1, 53):
            rendered = render_always_on_section(ao, set(), limit)
            assert len(rendered.text) <= limit, (limit, len(rendered.text))

    def test_collapsed_rules_in_a_tagged_block_keep_their_ids_in_the_record(self):
        from writ.retrieval.injection_ceiling import render_always_on_section

        ao = self._ao(tagged=True)
        full_len = len(render_always_on_section(ao, set(), 1_000_000).text)
        rendered = render_always_on_section(ao, set(), full_len - 120)
        assert rendered.text
        assert set(rendered.full_ids) <= set(rendered.rule_ids)
        for rid in rendered.rule_ids:
            assert f"[{rid}]" in rendered.text


# ---------------------------------------------------------------------------
# Capability 21 (real graph half): /always-on rows and statement count
# ---------------------------------------------------------------------------


def _always_on_rule_data(rule_id: str, trigger: str, always_on: bool = True) -> dict:
    return {
        "rule_id": rule_id, "domain": "Testing", "severity": "high", "scope": "slice",
        "trigger": trigger,
        "statement": "A fixture always-on statement for the trust tag route test.",
        "violation": "A fixture violation.", "pass_example": "A fixture pass example.",
        "enforcement": "Reviewed in the per-slice findings table.",
        "rationale": "A fixture rationale.", "last_validated": "2026-03-15",
        "always_on": always_on, "mandatory": False,
    }


class _CountedSession:
    def __init__(self, inner, statements):
        self._inner = inner
        self._statements = statements
        self._session = None

    async def __aenter__(self):
        self._session = await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def run(self, query, *args, **kwargs):
        self._statements.append(query)
        return await self._session.run(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


class _CountedDriver:
    def __init__(self, inner, statements):
        self._inner = inner
        self._statements = statements

    def session(self, **kwargs):
        return _CountedSession(self._inner.session(**kwargs), self._statements)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def _seed_always_on(
    conn, rule_id: str, trigger: str, *, always_on: bool = True, **graph_props
) -> None:
    await conn.create_rule(
        _always_on_rule_data(rule_id, trigger, always_on), source_origin="graph-authored",
    )
    if graph_props:
        await conn._run(
            "MATCH (r:Rule {rule_id: $rid}) SET r += $props RETURN count(r) AS c",
            rid=rule_id, props=graph_props,
        )


async def _drop_rules(conn, *rule_ids: str) -> None:
    for rid in rule_ids:
        await conn._run("MATCH (r:Rule {rule_id: $rid}) DETACH DELETE r", rid=rid)


class TestAlwaysOnRouteOnTheRealGraph:
    STALE_ID = "TRUSTTAG-STALE-001"
    DELIB_ID = "TRUSTTAG-DELIB-001"
    FRESH_ID = "TRUSTTAG-FRESH-001"
    STALE_TRIGGER = "When the stale always-on trust tag fixture is read."
    DELIB_TRIGGER = "When the deliberate always-on trust tag fixture is read."
    FRESH_TRIGGER = "When the fresh always-on trust tag fixture is read."

    @pytest_asyncio.fixture()
    async def seeded(self, route_db):
        ids = (self.STALE_ID, self.DELIB_ID, self.FRESH_ID)
        await _drop_rules(route_db, *ids)
        await _seed_always_on(route_db, self.STALE_ID, self.STALE_TRIGGER, last_verified=_days_ago(400))
        await _seed_always_on(
            route_db, self.DELIB_ID, self.DELIB_TRIGGER,
            last_verified=_days_ago(1), deliberate=True,
        )
        await _seed_always_on(route_db, self.FRESH_ID, self.FRESH_TRIGGER, last_verified=_days_ago(1))
        yield ids
        await _drop_rules(route_db, *ids)

    @staticmethod
    def _row(data: dict, rule_id: str) -> dict:
        rows = [r for r in data["rules"] if r.get("rule_id") == rule_id]
        assert len(rows) == 1, f"expected one /always-on row for {rule_id}, got {rows}"
        return rows[0]

    @pytest.mark.asyncio
    async def test_each_rule_row_carries_stale_and_deliberate(self, seeded, always_on):
        data = await always_on()
        stale = self._row(data, self.STALE_ID)
        delib = self._row(data, self.DELIB_ID)
        fresh = self._row(data, self.FRESH_ID)
        assert (stale["stale"], stale["deliberate"]) == (True, False)
        assert (delib["stale"], delib["deliberate"]) == (False, True)
        assert (fresh["stale"], fresh["deliberate"]) == (False, False)

    @pytest.mark.asyncio
    async def test_the_raw_clock_fields_are_replaced_by_the_boolean(self, seeded, always_on):
        data = await always_on()
        for rid in seeded:
            row = self._row(data, rid)
            assert "last_verified" not in row, row
            assert "verify_interval_days" not in row, row

    @pytest.mark.asyncio
    async def test_the_route_issues_the_same_two_statements_it_did_before(
        self, seeded, always_on, route_db,
    ):
        """Capability 21: three more RETURN columns, zero more statements. The Rule query
        and the ForbiddenResponse query are the two."""
        statements: list[str] = []
        real_driver = route_db._driver
        route_db._driver = _CountedDriver(real_driver, statements)
        try:
            await always_on()
        finally:
            route_db._driver = real_driver
        assert len(statements) == 2, statements

    @pytest.mark.asyncio
    async def test_a_tagged_rule_renders_its_tag_through_both_renderers(self, seeded, always_on):
        from writ.retrieval.injection_ceiling import render_always_on_section
        from writ.retrieval.prompt_bundle import render_always_on

        data = await always_on()
        text, _tokens, _count = render_always_on(data)
        assert f"[{self.STALE_ID}] (STALE) WHEN: {self.STALE_TRIGGER}" in text
        assert f"[{self.DELIB_ID}] (DELIBERATE) WHEN: {self.DELIB_TRIGGER}" in text
        assert f"[{self.FRESH_ID}] WHEN: {self.FRESH_TRIGGER}" in text

        section = render_always_on_section(data, set(), 1_000_000).text
        assert section == text, "the two renderers must agree byte for byte on the real bundle"


# ---------------------------------------------------------------------------
# Capability 23: verify clears the tag
# ---------------------------------------------------------------------------


def _graph(work):
    from tests._graph import connection

    async def _go():
        db = connection()
        try:
            return await work(db)
        finally:
            await db.close()

    return asyncio.run(_go())


class TestVerifyClearsTheStaleTag:
    RULE_ID = "TRUSTTAG-VERIFY-001"

    @pytest.fixture()
    def stale_rule(self):
        async def _seed(db):
            await _drop_rules(db, self.RULE_ID)
            await db._run(
                "MATCH (e:TrustEvent {rule_id: $rid}) DETACH DELETE e", rid=self.RULE_ID,
            )
            await _seed_always_on(
                db, self.RULE_ID, "When the verify chain fixture is read.",
                always_on=False, last_verified=_days_ago(400),
            )

        async def _drop(db):
            await _drop_rules(db, self.RULE_ID)
            await db._run(
                "MATCH (e:TrustEvent {rule_id: $rid}) DETACH DELETE e", rid=self.RULE_ID,
            )

        _graph(_seed)
        yield self.RULE_ID
        _graph(_drop)

    def _ranked_header(self, monkeypatch, capsys) -> str:
        """A rebuild of the pipeline's metadata from the graph (the loader the daemon uses),
        then the same query -> projection -> cmd_format chain the agent's header comes from."""
        import io
        import json

        from tests.test_ranked_header_fields import _stub_pipeline
        from writ.retrieval import prompt_bundle as pb
        from writ.retrieval.pipeline import _load_candidates
        from writ.session import budget_tracking as bt

        async def _load(db):
            _candidates, metadata = await _load_candidates(db)
            return metadata

        metadata = _graph(_load)
        assert self.RULE_ID in metadata, "the seeded rule must be in the loaded pool"
        meta = dict(metadata[self.RULE_ID])
        meta.setdefault("node_type", "Rule")
        pipeline = _stub_pipeline({self.RULE_ID: meta})
        response = pipeline.query("verify chain fixture", budget_tokens=5000)
        tagged = pb.tag_overlap(response["rules"], set())
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"rules": tagged, "mode": response["mode"]})),
        )
        bt.cmd_format()
        text, _meta = pb.split_format(capsys.readouterr().out)
        lines = [ln for ln in text.splitlines() if ln.startswith(f"[{self.RULE_ID}] (")]
        assert len(lines) == 1, text
        return lines[0]

    def test_a_stale_rule_is_tagged_then_verified_then_untagged_after_a_rebuild(
        self, stale_rule, tmp_path, monkeypatch, capsys,
    ):
        from writ.session.gate_token import mint_gate_token

        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
        (tmp_path / "cache").mkdir()
        monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
        monkeypatch.setenv("WRIT_LOG_PROJECT", "trust-tags-verify")

        before = self._ranked_header(monkeypatch, capsys)
        assert before.startswith(f"[{stale_rule}] (high, STALE) score="), before

        sid = _sid("tagverify")
        with _mint_cleanup(sid):
            token = mint_gate_token(
                sid, gate="", plan_hash="", rule_id=f"verify:{stale_rule}",
                os_login="alice-fixed", git_name="Alice Fixed Name",
            )
            result = runner.invoke(
                app, ["review", stale_rule, "--verify", "--session-id", sid, "--token", token],
            )
        assert result.exit_code == 0, result.output

        after = self._ranked_header(monkeypatch, capsys)
        assert after.startswith(f"[{stale_rule}] (high) score="), after
        assert "STALE" not in after
