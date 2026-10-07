"""Program item 4 (decision recall), workstream A: the recall core.

Recall ranks the project's decisions by the prompt: the decision behind a file the prompt
names first (FileChange -[MOTIVATED_BY]-> Decision, one batched read), then decisions and
memories whose text matches the prompt's terms (an in-memory BM25 over the corpus), then
recent decisions on the first brief only. Cards are costed by their rendered text inside the
real 500-token recall budget, and a shown-ids record replaces the recall_briefed bool.

What this file pins, each against the seam that owns it:

  * clip and first_sentence, the shared helpers (1), and the harvest title (2, 3)
  * prompt_path_tokens and path_candidates, the one prompt path extractor (4, 5)
  * ranking tiers and ties, the term tier, the score-floor fixture (8, 9, 10)
  * card shape, budget and eviction, slots and the memory cap (11, 12, 13)
  * memories, exclude_ids, matched_only, full mode, the corpus cache (14 to 18)
  * the /prompt-bundle cadence and clamp marking (19 to 25, 37)
  * the /recall forwarding and the CLI budgets (27, 28)

The graph claims (the traversal, project scoping, statement counts, the end to end chain and
concurrent shown-record writes) are in tests/test_decision_recall_graph.py on the isolated
instance. The fake database of tests/test_recall_compile.py is confined here to what has no
store behavior: ranking order, the score floor over a fixture corpus, card rendering,
eviction, the slot and memory caps, exclude and matched_only. The route cadence uses the
patched-seam pattern of tests/test_prompt_injection_ceiling.py; the real-cache concurrency
claims are deliberately NOT here.

RED today: writ.shared.injection_text has no clip or first_sentence; writ.session.recall has
no prompt ranking, display_title, extractor or corpus cache; injection_state does not track
recall; the route still reads recall_briefed; RecallRequest has no prompt; the CLI passes no
project_root.

Run: .venv/bin/python3 -m pytest tests/test_decision_recall.py
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import writ.server as server
import writ.server.routes.query as qroute
from tests.test_decision_memory_harvester import _FakeDB as _HarvestDB
from tests.test_recall_compile import (
    _FakeDB,
    _briefing_tokens,
    _decision_factory,
    _memory_factory,
    _path_hit,
)
from writ.server.models import PromptBundleRequest, RecallRequest
from writ.shared.tokens import PROMPT_SECTION_TOKENS

# Program item 5 (workstream P): the briefing is a fenced block, so its open line is the fence's.
HEADER = (
    "--- WRIT RECALL (recent decisions on this project, "
    "rule-grounded, read back from decision memory) ---"
)
CLOSE = "--- END WRIT RECALL ---"
PROJECT_ROOT = "/repo/proj"

runner = CliRunner()


def _recall_mod():
    return importlib.import_module("writ.session.recall")


# ---------------------------------------------------------------------------
# Fixture corpus (TEST-FIXTURE-001)
# ---------------------------------------------------------------------------

_GENERIC = "Continue with the next step, yes do that, and the plan stays ok."


def _corpus20(special: dict[int, dict] | None = None) -> list[dict]:
    """Twenty decisions, newest first, built for the score-floor fixture.

    Every rationale carries the same generic prose, so words such as "continue", "the",
    "next", "step", "yes", "do", "that" and "ok" occur in all twenty documents (their IDF is
    close to zero, which is what a real corpus of working decisions looks like). Each has a
    unique topic token and one planned file. `special` overrides fields of chosen indexes so
    a test can plant ONE distinctive term in ONE decision.
    """
    special = special or {}
    out = []
    for n in range(20):
        fields = dict(
            decision_id=f"DEC-C{n:02d}",
            title=f"stored title {n}",
            rationale=f"Settle topic{n:02d}. {_GENERIC}",
            planned_files=[{"path": f"pkg/mod{n:02d}.py", "reason": "adjust the module"}],
            governing_rule_ids=["PERF-BATCH-001"],
            ts=f"2026-06-{28 - n:02d}T10:00:00+00:00",
        )
        fields.update(special.get(n, {}))
        out.append(_decision_factory(**fields))
    return out


def _db_with(special: dict[int, dict] | None = None, **kwargs: Any) -> _FakeDB:
    return _FakeDB(decisions=_corpus20(special), **kwargs)


async def _recall(db: _FakeDB, project: str = "proj", **kwargs: Any) -> dict:
    return await _recall_mod().compile_recall(db, project, **kwargs)


def _ids(result: dict) -> list[str]:
    return [d["decision_id"] for d in result["decisions"]]


def _match_of(result: dict) -> dict[str, str]:
    return {d["decision_id"]: d["match"] for d in result["decisions"]}


# ===========================================================================
# 1. clip and first_sentence
# ===========================================================================


class TestClip:
    def _clip(self, text, limit):
        from writ.shared.injection_text import clip

        return clip(text, limit)

    def test_text_that_fits_is_returned_unchanged(self):
        assert self._clip("short text", 50) == "short text"

    def test_text_exactly_at_the_limit_is_unchanged(self):
        assert self._clip("a" * 20, 20) == "a" * 20

    def test_runs_of_whitespace_collapse_to_one_space_and_the_ends_are_stripped(self):
        assert self._clip("  a   b\n\n c\t d  ", 50) == "a b c d"

    def test_longer_text_is_cut_at_a_word_boundary_with_an_ellipsis(self):
        # The last space at or before limit - 3 (index 17) is at index 16.
        assert self._clip("alpha beta gamma delta epsilon", 20) == "alpha beta gamma..."

    def test_the_result_is_never_longer_than_the_limit(self):
        text = "word " * 100
        for limit in (10, 33, 160, 400):
            out = self._clip(text, limit)
            assert len(out) <= limit
            assert out.endswith("...")

    def test_text_one_over_the_limit_is_clipped(self):
        out = self._clip("a" * 21, 20)
        assert len(out) <= 20 and out.endswith("...")

    def test_text_with_no_space_is_cut_hard_at_the_limit_minus_three(self):
        assert self._clip("x" * 50, 20) == "x" * 17 + "..."

    def test_empty_and_none_return_empty(self):
        assert self._clip("", 20) == ""
        assert self._clip(None, 20) == ""


class TestFirstSentence:
    def _fs(self, text, limit=100):
        from writ.shared.injection_text import first_sentence

        return first_sentence(text, limit)

    def test_takes_the_text_up_to_the_first_sentence_end(self):
        out = self._fs("Fix the cache invalidation. Then explain why it broke.")
        assert out.rstrip(".") == "Fix the cache invalidation"
        assert "explain" not in out

    def test_question_and_exclamation_end_a_sentence_too(self):
        assert "Second" not in self._fs("Why does it fail? Second sentence.")
        assert "Second" not in self._fs("Stop the leak! Second sentence.")

    def test_a_newline_ends_the_title_without_a_period(self):
        out = self._fs("Plain first line\nSecond line here")
        assert out == "Plain first line"

    def test_strips_leading_heading_and_list_markers(self):
        assert self._fs("## Heading text here\nbody").rstrip(".") == "Heading text here"
        assert self._fs("- bullet text\nbody") == "bullet text"
        assert self._fs("* star text\nbody") == "star text"

    def test_uses_the_first_non_empty_line(self):
        assert self._fs("\n\n   \n  Real first line\nsecond") == "Real first line"

    def test_is_clipped_to_the_limit(self):
        out = self._fs("word " * 60, 40)
        assert len(out) <= 40
        assert out.endswith("...")

    def test_empty_and_none_return_empty(self):
        assert self._fs("") == ""
        assert self._fs(None) == ""


# ===========================================================================
# 2, 3. Titles: written at harvest, derived at read
# ===========================================================================


_PLAN = "## Files\n\n- `planned.py` (modify) -- the real per-file reason\n"


async def _harvest(monkeypatch, *, rationale: str, subject: str):
    from writ.session import harvester

    monkeypatch.setattr(
        harvester, "harvest_plan",
        lambda text: {
            "rationale": rationale, "cited_rules": [],
            "files": [{"path": "planned.py", "change_type": "modify", "reason": "why"}],
        },
    )
    db = _HarvestDB()
    await harvester.harvest_one_commit(
        db, "proj", commit_hash="t-001", subject=subject, author="a", branch="main",
        commit_ts="2026-06-30T10:00:00Z",
        files=[{"path": "planned.py", "change_type": "modify"}],
        plan_text=_PLAN, plan_ts="2026-06-30T09:00:00Z",
    )
    return db, harvester


class TestHarvestTitle:
    @pytest.mark.asyncio
    async def test_title_is_the_first_sentence_of_the_rationale(self, monkeypatch):
        rationale = "Cache the widget lookups. The daemon re-reads them on every prompt, which is slow."
        db, _ = await _harvest(monkeypatch, rationale=rationale, subject="feat: widgets")
        title = db.decisions[0]["title"]
        assert title.rstrip(".") == "Cache the widget lookups"

    @pytest.mark.asyncio
    async def test_title_is_at_most_100_characters(self, monkeypatch):
        db, _ = await _harvest(monkeypatch, rationale="word " * 80, subject="s")
        title = db.decisions[0]["title"]
        assert 0 < len(title) <= 100

    @pytest.mark.asyncio
    async def test_empty_rationale_falls_back_to_the_commit_subject(self, monkeypatch):
        db, _ = await _harvest(monkeypatch, rationale="", subject="feat: add the widget cache")
        assert db.decisions[0]["title"] == "feat: add the widget cache"

    @pytest.mark.asyncio
    async def test_harvested_plan_only_when_both_are_empty(self, monkeypatch):
        db, _ = await _harvest(monkeypatch, rationale="", subject="")
        assert db.decisions[0]["title"] == "harvested plan"

    @pytest.mark.asyncio
    async def test_the_decision_id_does_not_depend_on_the_title(self, monkeypatch):
        db_a, harvester = await _harvest(monkeypatch, rationale="First sentence. More.", subject="s")
        db_b, _ = await _harvest(monkeypatch, rationale="", subject="another subject")
        assert db_a.decisions[0]["decision_id"] == db_b.decisions[0]["decision_id"]
        assert db_a.decisions[0]["decision_id"] == harvester._decision_id("proj", _PLAN)


class TestDisplayTitle:
    def test_a_rationale_80_char_title_displays_the_first_sentence(self):
        rationale = "Make the cache honest about staleness. Because the daemon holds the index for hours."
        stored = rationale[:80]
        assert _recall_mod().display_title({"title": stored, "rationale": rationale}).rstrip(".") == (
            "Make the cache honest about staleness"
        )

    def test_an_empty_rationale_keeps_the_stored_title(self):
        assert _recall_mod().display_title({"title": "harvested plan", "rationale": ""}) == "harvested plan"

    def test_no_title_and_no_rationale_is_untitled(self):
        assert _recall_mod().display_title({"title": None, "rationale": ""}) == "(untitled)"

    def test_the_derived_title_is_at_most_100_characters(self):
        out = _recall_mod().display_title({"title": "x", "rationale": "word " * 80})
        assert len(out) <= 100

    def test_one_title_length_constant_serves_recall_and_the_harvester(self):
        from writ.shared import injection_text

        assert injection_text.TITLE_CHARS == 100
        harvester = importlib.import_module("writ.session.harvester")
        for mod in (_recall_mod(), harvester):
            assert not hasattr(mod, "_TITLE_CHARS")
            assert mod.TITLE_CHARS is injection_text.TITLE_CHARS

    @pytest.mark.asyncio
    async def test_cards_and_payload_show_the_derived_title_not_the_stored_one(self):
        rationale = "Make the cache honest about staleness. Because the daemon holds the index for hours."
        decision = _decision_factory(decision_id="DEC-OLD", title=rationale[:80], rationale=rationale)
        result = await _recall(_FakeDB(decisions=[decision]))
        head = result["briefing"].splitlines()[1]
        assert head.startswith("- Make the cache honest about staleness")
        assert "Because" not in head
        assert result["decisions"][0]["title"].rstrip(".") == "Make the cache honest about staleness"

    @pytest.mark.asyncio
    async def test_a_decision_with_no_rationale_shows_its_stored_title(self):
        decision = _decision_factory(decision_id="DEC-HP", title="harvested plan", rationale="")
        result = await _recall(_FakeDB(decisions=[decision]))
        assert result["briefing"].splitlines()[1].startswith("- harvested plan [")


# ===========================================================================
# 4, 5. The one prompt path extractor
# ===========================================================================


class TestPromptPathTokens:
    def _tokens(self, prompt):
        return _recall_mod().prompt_path_tokens(prompt)

    def test_keeps_slash_bearing_and_dotted_extension_tokens(self):
        assert self._tokens("edit writ/session/recall.py and README.md now") == [
            "writ/session/recall.py", "README.md",
        ]

    def test_a_slash_without_an_extension_is_kept(self):
        assert self._tokens("look in the writ/session directory") == ["writ/session"]

    def test_plain_words_are_dropped(self):
        assert self._tokens("ok, continue with the next step") == []

    def test_strips_surrounding_quotes_and_brackets(self):
        out = self._tokens("see `a/b.py`, 'c/d.py', \"e/f.py\" (g/h.py) [i/j.py] <k/l.py>")
        assert out == ["a/b.py", "c/d.py", "e/f.py", "g/h.py", "i/j.py", "k/l.py"]

    def test_strips_trailing_punctuation(self):
        assert self._tokens("fix src/a.py, then src/b.py. Also src/c.py! Why src/d.py?") == [
            "src/a.py", "src/b.py", "src/c.py", "src/d.py",
        ]

    def test_strips_a_line_and_column_suffix(self):
        assert self._tokens("see src/a.py:120 and src/b.py:7:13 and bare.py:9") == [
            "src/a.py", "src/b.py", "bare.py",
        ]

    def test_drops_urls(self):
        assert self._tokens("read https://example.com/docs/page.html then docs/x.md") == ["docs/x.md"]

    def test_returns_at_most_ten_tokens_in_first_seen_order(self):
        prompt = " ".join(f"dir/file{i:02d}.py" for i in range(15))
        out = self._tokens(prompt)
        assert out == [f"dir/file{i:02d}.py" for i in range(10)]

    def test_an_empty_prompt_yields_nothing(self):
        assert self._tokens("") == []


class TestPathCandidates:
    def _cands(self, path, root=PROJECT_ROOT):
        return _recall_mod().path_candidates(path, root)

    def test_an_absolute_path_under_the_root_yields_the_repo_relative_path_first(self):
        out = self._cands("/repo/proj/writ/session/recall.py")
        assert out[0] == "writ/session/recall.py"

    def test_an_ancestor_of_the_root_yields_the_path_relative_to_it(self):
        out = self._cands("/repo/proj/writ/x.py")
        assert "proj/writ/x.py" in out

    def test_a_root_below_the_repo_root_still_reaches_the_repo_relative_path(self):
        out = self._cands("/repo/pkg/sub/a.py", "/repo/pkg/sub")
        assert out[0] == "a.py"
        assert "sub/a.py" in out
        assert "pkg/sub/a.py" in out

    def test_at_most_four_ancestors_are_tried(self):
        out = self._cands("/a/b/c/d/e/f/g/x.py", "/a/b/c/d/e/f/g")
        assert 1 <= len(out) <= 5

    def test_a_relative_token_yields_itself_first_then_its_expansion(self):
        out = self._cands("writ/session/recall.py")
        assert out[0] == "writ/session/recall.py"
        assert "proj/writ/session/recall.py" in out

    def test_a_leading_dot_slash_is_normalized_away(self):
        out = self._cands("./writ/x.py")
        assert out[0] == "writ/x.py"

    def test_every_candidate_is_normalized_and_the_list_is_deduped(self):
        out = self._cands("/repo/proj/writ/x.py")
        assert all(not c.startswith("/") and not c.startswith("./") for c in out)
        assert len(out) == len(set(out))

    def test_a_path_under_none_of_the_directories_yields_nothing(self):
        assert self._cands("/x/elsewhere.py", "/repo/proj-a") == []

    def test_no_project_root_yields_nothing_for_an_absolute_path(self):
        assert self._cands("/repo/proj/writ/x.py", "") == []


# ===========================================================================
# 8, 9, 10. Ranking: tiers, ties, the term tier and the score floor
# ===========================================================================


def _path_decision(decision_id="DEC-P", rationale="Route the writes through the gate. Detail.", **kw):
    return _decision_factory(
        decision_id=decision_id, rationale=rationale,
        planned_files=kw.pop("planned_files", [{"path": "writ/foo.py", "reason": "planned foo reason"}]),
        ts=kw.pop("ts", "2026-05-01T10:00:00+00:00"), **kw,
    )


class TestRankingTiers:
    @pytest.mark.asyncio
    async def test_path_then_term_then_recent(self):
        d_path = _path_decision()
        db = _db_with(
            {5: {"rationale": f"Settle topic05. Handle glimmerwisp carefully. {_GENERIC}"}},
            path_hits={"writ/foo.py": [_path_hit(d_path)]},
        )
        result = await _recall(
            db, prompt="please glimmerwisp writ/foo.py", project_root=PROJECT_ROOT
        )
        ids = _ids(result)
        assert ids[0] == "DEC-P" and ids[1] == "DEC-C05", ids
        assert ids[2] == "DEC-C00", "recency fill starts at the newest decision"
        match = _match_of(result)
        assert match["DEC-P"] == "path"
        assert match["DEC-C05"] == "term"
        assert match["DEC-C00"] == "recent"

    @pytest.mark.asyncio
    async def test_a_path_decision_older_than_the_corpus_window_still_renders(self):
        d_path = _path_decision(decision_id="DEC-ANCIENT", ts="2020-01-01T00:00:00+00:00")
        db = _db_with(path_hits={"writ/foo.py": [_path_hit(d_path)]})
        result = await _recall(db, prompt="look at writ/foo.py", project_root=PROJECT_ROOT)
        assert "DEC-ANCIENT" in _ids(result)
        assert "Route the writes through the gate" in result["briefing"]

    @pytest.mark.asyncio
    async def test_a_decision_in_both_the_path_and_term_tiers_appears_once_as_path(self):
        d_path = _path_decision(decision_id="DEC-C04")
        db = _db_with(
            {4: {"rationale": f"Route the writes through the gate. Handle glimmerwisp. {_GENERIC}"}},
            path_hits={"writ/foo.py": [_path_hit(d_path)]},
        )
        result = await _recall(db, prompt="glimmerwisp in writ/foo.py", project_root=PROJECT_ROOT)
        assert _ids(result).count("DEC-C04") == 1
        assert _match_of(result)["DEC-C04"] == "path"

    @pytest.mark.asyncio
    async def test_path_ties_break_on_the_most_recent_matched_change(self):
        older = _path_decision("DEC-P-OLD", ts="2026-05-02T00:00:00+00:00")
        newer = _path_decision("DEC-P-NEW", ts="2026-05-01T00:00:00+00:00")
        db = _db_with(path_hits={
            "writ/a.py": [_path_hit(older, change_ts="2026-06-01T00:00:00+00:00")],
            "writ/b.py": [_path_hit(newer, change_ts="2026-06-20T00:00:00+00:00")],
        })
        result = await _recall(db, prompt="edit writ/a.py and writ/b.py", project_root=PROJECT_ROOT)
        assert _ids(result)[:2] == ["DEC-P-NEW", "DEC-P-OLD"]

    @pytest.mark.asyncio
    async def test_term_ties_break_newest_first(self):
        same = {"rationale": f"Settle topic90. Handle zorblat carefully. {_GENERIC}"}
        db = _db_with({9: dict(same), 2: dict(same)})
        result = await _recall(db, prompt="zorblat", matched_only=True)
        assert _ids(result) == ["DEC-C02", "DEC-C09"], "C02 is newer than C09"

    @pytest.mark.asyncio
    async def test_recency_fill_is_newest_first(self):
        result = await _recall(_db_with())
        assert _ids(result) == [f"DEC-C{n:02d}" for n in range(5)]
        assert all(m == "recent" for m in _match_of(result).values())


class TestPathRead:
    @pytest.mark.asyncio
    async def test_the_path_read_gets_normalized_candidates_and_three_per_path(self):
        db = _db_with()
        await _recall(db, prompt="update writ/session/recall.py:44", project_root=PROJECT_ROOT)
        assert len(db.path_calls) == 1
        project, paths, per_path = db.path_calls[0]
        assert project == "proj"
        assert "writ/session/recall.py" in paths
        assert per_path == 3
        assert len(paths) <= 40

    @pytest.mark.asyncio
    async def test_no_path_read_without_a_project_root(self):
        db = _db_with()
        await _recall(db, prompt="update writ/session/recall.py")
        assert db.path_calls == []

    @pytest.mark.asyncio
    async def test_no_path_read_when_the_prompt_names_no_path(self):
        db = _db_with()
        await _recall(db, prompt="ok, continue with the next step", project_root=PROJECT_ROOT)
        assert db.path_calls == []

    @pytest.mark.asyncio
    async def test_a_matched_file_carries_the_change_reason_then_the_planned_reason(self):
        d = _path_decision(planned_files=[
            {"path": "writ/foo.py", "reason": "PLANNED-FOO-REASON"},
            {"path": "writ/bar.py", "reason": "PLANNED-BAR-REASON"},
        ])
        db = _db_with(path_hits={
            "writ/foo.py": [_path_hit(d, reason="CHANGE-FOO-REASON")],
            "writ/bar.py": [_path_hit(d, reason="")],
        })
        result = await _recall(db, prompt="writ/foo.py writ/bar.py", project_root=PROJECT_ROOT)
        files = {f["path"]: f["reason"] for f in result["decisions"][0]["matched_files"]}
        assert files == {"writ/foo.py": "CHANGE-FOO-REASON", "writ/bar.py": "PLANNED-BAR-REASON"}


class TestTermTier:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("planted", [
        pytest.param(
            {3: {"rationale": f"Quibblefrost the cache. {_GENERIC}"}}, id="title-word"),
        pytest.param(
            {3: {"rationale": f"Settle topic03. Handle quibblefrost carefully. {_GENERIC}"}},
            id="rationale-word"),
        pytest.param(
            {3: {"planned_files": [{"path": "src/gizmos/quibblefrost.py", "reason": "adjust the module"}]}},
            id="planned-path-word"),
        pytest.param(
            {3: {"planned_files": [{"path": "pkg/mod03.py", "reason": "quibblefrost handler for imports"}]}},
            id="planned-reason-word"),
    ])
    async def test_a_distinctive_word_in_any_field_ranks_that_decision_in_the_term_tier(self, planted):
        result = await _recall(_db_with(planted), prompt="please look into quibblefrost", matched_only=True)
        assert _ids(result) == ["DEC-C03"]
        assert _match_of(result)["DEC-C03"] == "term"

    @pytest.mark.asyncio
    async def test_a_bare_filename_matches_the_decision_whose_planned_paths_contain_it(self):
        planted = {8: {"planned_files": [{"path": "src/gizmos/quuxwidget.py", "reason": "adjust the module"}]}}
        db = _db_with(planted)
        result = await _recall(db, prompt="tweak quuxwidget.py", project_root=PROJECT_ROOT, matched_only=True)
        assert _ids(result) == ["DEC-C08"]
        assert _match_of(result)["DEC-C08"] == "term"

    def test_the_floor_fraction_is_set_so_a_twenty_document_corpus_keeps_the_old_floor(self):
        recall = _recall_mod()
        assert recall.RECALL_TERM_FLOOR_FRACTION == pytest.approx(0.18946, abs=1e-5)
        assert recall.term_score_floor(20) == pytest.approx(0.5)

    @pytest.mark.parametrize("n", [1, 3, 20, 200])
    def test_the_floor_is_the_fraction_of_the_score_the_index_gives_a_one_document_term(self, n):
        from writ.retrieval.keyword import KeywordIndex

        index = KeywordIndex()
        index.build([{"rule_id": f"d{i}", "statement": "alpha " + ("zorblat" if i == 0 else "filler")}
                     for i in range(n)])
        idf1 = index.search("zorblat")[0]["score"]
        recall = _recall_mod()
        assert recall.term_score_floor(n) == pytest.approx(recall.RECALL_TERM_FLOOR_FRACTION * idf1, rel=1e-5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt", [
        "ok, continue with the next step",
        "yes do that",
        "ok",
        "thanks, that looks good, continue",
    ])
    async def test_generic_prompts_produce_no_term_tier_item(self, prompt):
        db = _db_with()
        matched = await _recall(db, prompt=prompt, matched_only=True)
        assert matched["decisions"] == [] and matched["briefing"] == ""
        full = await _recall(db, prompt=prompt)
        assert set(_match_of(full).values()) == {"recent"}

    @pytest.mark.asyncio
    async def test_a_distinctive_term_produces_exactly_that_decision(self):
        planted = {11: {"rationale": f"Settle topic11. Handle zorblat carefully. {_GENERIC}"}}
        db = _db_with(planted)
        only = await _recall(db, prompt="what about zorblat", matched_only=True)
        assert _ids(only) == ["DEC-C11"]
        with_fill = await _recall(db, prompt="what about zorblat")
        assert [i for i, m in _match_of(with_fill).items() if m == "term"] == ["DEC-C11"]
        assert _ids(with_fill)[0] == "DEC-C11"

    @pytest.mark.asyncio
    async def test_the_query_is_the_prompt_clipped_to_2000_characters(self):
        planted = {11: {"rationale": f"Settle topic11. Handle zorblat carefully. {_GENERIC}"}}
        far_term = "filler " * 400 + "zorblat"
        result = await _recall(_db_with(planted), prompt=far_term, matched_only=True)
        assert result["decisions"] == [], "a term past the 2000-character clip is not searched"


def _corpus_of(n: int, planted: int | None = None) -> _FakeDB:
    """`n` decisions sharing the generic prose; `planted` carries the one word "zorblat"."""
    return _FakeDB(decisions=[
        _decision_factory(
            decision_id=f"DEC-S{i:03d}",
            rationale=f"Settle topic{i:03d}. {'Handle zorblat. ' if i == planted else ''}{_GENERIC}",
            planned_files=[{"path": f"pkg/mod{i:03d}.py", "reason": "adjust the module"}],
            ts=f"2026-06-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
        ) for i in range(n)
    ])


class TestSizeAwareFloor:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 3, 20, 200])
    async def test_one_distinctive_word_shared_with_one_decision_clears_the_floor(self, n):
        result = await _recall(_corpus_of(n, planted=n // 2), prompt="what about zorblat", matched_only=True)
        assert _ids(result) == [f"DEC-S{n // 2:03d}"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n, prompt", [
        (3, "continue"),
        (20, "ok, continue with the next step"),
        (200, "ok, continue with the next step"),
        (200, "thanks, that looks good, continue"),
    ])
    async def test_generic_words_present_in_every_document_do_not_clear_the_floor(self, n, prompt):
        result = await _recall(_corpus_of(n), prompt=prompt, matched_only=True)
        assert result["decisions"] == []


def _floor_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "recall_term_floor"]


class TestTermFloorLog:
    @pytest.fixture()
    def log_path(self, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(path))
        return path

    @pytest.mark.asyncio
    async def test_a_compile_that_runs_the_term_tier_writes_one_row_with_the_tuning_fields(self, log_path):
        db = _corpus_of(20, planted=4)
        await _recall(db, prompt="what about zorblat Quibblesecret")
        rows = _floor_rows(log_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["project"] == "proj"
        assert row["corpus_size"] == 20
        assert row["floor"] == pytest.approx(0.5)
        assert row["top_score"] > row["floor"]
        assert row["cleared"] == 1

    @pytest.mark.asyncio
    async def test_the_row_carries_no_prompt_text_and_no_decision_text(self, log_path):
        await _recall(_corpus_of(20, planted=4), prompt="what about zorblat Quibblesecret")
        raw = log_path.read_text()
        assert "recall_term_floor" in raw
        for text in ("zorblat", "Quibblesecret", "Settle", "topic", "Continue"):
            assert text not in raw

    @pytest.mark.asyncio
    async def test_no_hit_records_a_zero_top_score_and_none_cleared(self, log_path):
        await _recall(_corpus_of(3), prompt="nothingmatchesthis")
        (row,) = _floor_rows(log_path)
        assert row["corpus_size"] == 3
        assert row["top_score"] == 0
        assert row["cleared"] == 0

    @pytest.mark.asyncio
    async def test_a_compile_without_a_prompt_writes_no_row(self, log_path):
        await _recall(_corpus_of(3))
        assert _floor_rows(log_path) == []

    @pytest.mark.asyncio
    async def test_a_failing_log_write_never_reaches_recall(self, log_path, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("log down")

        monkeypatch.setattr(_recall_mod(), "emit", _boom)
        result = await _recall(_corpus_of(20, planted=4), prompt="what about zorblat", matched_only=True)
        assert _ids(result) == ["DEC-S004"]

    def test_the_row_is_stated_in_the_stream_map_as_metrics(self):
        from writ.shared.logging import STREAM_MAP

        assert STREAM_MAP["recall_term_floor"] == "metrics"


# ===========================================================================
# 11. Cards
# ===========================================================================


def _card_lines(briefing: str) -> list[str]:
    lines = briefing.splitlines()[1:]
    return lines[:-1] if lines and lines[-1] == CLOSE else lines


class TestCards:
    @pytest.mark.asyncio
    async def test_the_briefing_opens_with_the_fenced_recall_line_and_ends_with_its_close_line(self):
        result = await _recall(_db_with())
        assert result["briefing"].splitlines()[0] == HEADER
        assert result["briefing"].splitlines()[-1] == CLOSE

    @pytest.mark.asyncio
    async def test_card_head_is_title_and_rule_ids(self):
        d = _decision_factory(
            decision_id="DEC-1", rationale="Gate the writes. More.", governing_rule_ids=["RULE-A", "RULE-B"],
        )
        result = await _recall(_FakeDB(decisions=[d]))
        assert _card_lines(result["briefing"])[0].rstrip(".").startswith("- Gate the writes")
        assert "[RULE-A, RULE-B]" in _card_lines(result["briefing"])[0]
        assert result["cards"] == [{"id": "DEC-1", "head": _card_lines(result["briefing"])[0]}]

    @pytest.mark.asyncio
    async def test_a_decision_without_rules_renders_no_rules_cited(self):
        d = _decision_factory(decision_id="DEC-1", governing_rule_ids=[])
        result = await _recall(_FakeDB(decisions=[d]))
        assert "[no rules cited]" in _card_lines(result["briefing"])[0]

    @pytest.mark.asyncio
    async def test_the_why_line_clips_the_rationale_to_160_characters(self):
        d = _decision_factory(decision_id="DEC-1", rationale="Alpha " * 60)
        result = await _recall(_FakeDB(decisions=[d]))
        why = next(ln for ln in _card_lines(result["briefing"]) if ln.startswith("  why: "))
        body = why[len("  why: "):]
        assert len(body) <= 160
        assert body.endswith("...")

    @pytest.mark.asyncio
    async def test_a_matched_file_line_clips_the_reason_to_120_characters(self):
        d = _path_decision()
        db = _db_with(path_hits={"writ/foo.py": [_path_hit(d, reason="word " * 40)]})
        result = await _recall(db, prompt="writ/foo.py", project_root=PROJECT_ROOT)
        line = next(ln for ln in _card_lines(result["briefing"]) if ln.startswith("  writ/foo.py: "))
        body = line[len("  writ/foo.py: "):]
        assert len(body) <= 120 and body.endswith("...")

    @pytest.mark.asyncio
    async def test_at_most_two_matched_file_lines_render(self):
        d = _path_decision(planned_files=[
            {"path": f"writ/f{i}.py", "reason": "r"} for i in range(3)
        ])
        db = _db_with(path_hits={
            f"writ/f{i}.py": [_path_hit(d, reason=f"reason {i}")] for i in range(3)
        })
        result = await _recall(db, prompt="writ/f0.py writ/f1.py writ/f2.py", project_root=PROJECT_ROOT)
        lines = [ln for ln in result["briefing"].splitlines() if ln.startswith("  writ/f")]
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_unmatched_planned_files_are_not_rendered(self):
        d = _path_decision(planned_files=[
            {"path": "writ/foo.py", "reason": "planned foo reason"},
            {"path": "other/unmatched.py", "reason": "UNMATCHED-REASON"},
        ])
        db = _db_with(path_hits={"writ/foo.py": [_path_hit(d)]})
        result = await _recall(db, prompt="writ/foo.py", project_root=PROJECT_ROOT)
        assert "other/unmatched.py" not in result["briefing"]
        assert "UNMATCHED-REASON" not in result["briefing"]

    @pytest.mark.asyncio
    async def test_a_recency_card_shows_no_file_lines(self):
        result = await _recall(_db_with())
        assert not [ln for ln in result["briefing"].splitlines() if ln.startswith("  pkg/")]


# ===========================================================================
# 12, 13. Budget, eviction and slots
# ===========================================================================


class TestBudgetAndSlots:
    def test_the_default_budget_is_the_recall_section_budget(self):
        default = inspect.signature(_recall_mod().compile_recall).parameters["budget"].default
        assert default == PROMPT_SECTION_TOKENS["recall"] == 500

    def test_the_full_listing_budget_is_a_named_constant(self):
        assert _recall_mod().RECALL_FULL_BUDGET == 20000

    @pytest.mark.asyncio
    async def test_the_default_briefing_fits_the_section_budget(self):
        rich = {n: {"rationale": f"Settle topic{n:02d}. " + "Long rationale words. " * 30} for n in range(20)}
        result = await _recall(_db_with(rich))
        assert _briefing_tokens(result) <= PROMPT_SECTION_TOKENS["recall"]

    @pytest.mark.asyncio
    async def test_at_most_five_cards_even_when_more_match(self):
        result = await _recall(_db_with(), budget=200_000)
        assert len(result["decisions"]) == 5
        assert len(result["cards"]) == 5

    @pytest.mark.asyncio
    async def test_at_most_two_of_the_five_cards_are_memories(self):
        special = {n: {"rationale": f"Settle topic{n:02d}. Handle zorblat carefully. {_GENERIC}"}
                   for n in (1, 2, 3, 4)}
        memories = [
            _memory_factory(name=f"note_{i}", description=f"Zorblat guidance number {i}.",
                            updated_at=f"2026-06-2{i}T00:00:00+00:00")
            for i in range(4)
        ]
        db = _db_with(special, memories=memories)
        result = await _recall(db, prompt="zorblat", budget=200_000)
        assert len(result["cards"]) == 5
        assert len(result["memories"]) == 2
        assert len(result["decisions"]) == 3
        memory_lines = [ln for ln in result["briefing"].splitlines() if ln.startswith("- memory ")]
        assert len(memory_lines) == 2


# ===========================================================================
# 14. Memories
# ===========================================================================


class TestMemories:
    def _memories(self):
        return [
            _memory_factory(name="reference_lemonsqueezer",
                            description="Lemonsqueezer notes live in the wiki."),
            _memory_factory(name="feedback_unrelated", description="Prefer short answers."),
        ]

    @pytest.mark.asyncio
    async def test_a_matching_memory_renders_as_memory_name_description(self):
        db = _db_with(memories=self._memories())
        result = await _recall(db, prompt="check lemonsqueezer")
        assert "- memory reference_lemonsqueezer: Lemonsqueezer notes live in the wiki." in (
            result["briefing"].splitlines()
        )
        assert [m["name"] for m in result["memories"]] == ["reference_lemonsqueezer"]
        assert {"id": "memory:reference_lemonsqueezer",
                "head": "- memory reference_lemonsqueezer: Lemonsqueezer notes live in the wiki."} in result["cards"]

    @pytest.mark.asyncio
    async def test_a_memory_matches_on_its_name(self):
        db = _db_with(memories=[_memory_factory(name="snazzleberry_setup", description="Setup steps.")])
        result = await _recall(db, prompt="remember the snazzleberry setup")
        assert [m["name"] for m in result["memories"]] == ["snazzleberry_setup"]

    @pytest.mark.asyncio
    async def test_memories_never_fill_by_recency(self):
        db = _db_with(memories=self._memories())
        result = await _recall(db)
        assert result["memories"] == []
        assert "- memory " not in result["briefing"]

    @pytest.mark.asyncio
    async def test_a_memory_with_no_term_match_is_absent(self):
        db = _db_with(memories=self._memories())
        result = await _recall(db, prompt="check lemonsqueezer", matched_only=True)
        assert "feedback_unrelated" not in result["briefing"]

    @pytest.mark.asyncio
    async def test_the_memory_read_is_list_memories_unchanged(self):
        db = _db_with(memories=self._memories())
        await _recall(db, prompt="check lemonsqueezer")
        assert db.memory_calls == [("proj", False)], "no include_deleted: tombstones stay hidden"

    @pytest.mark.asyncio
    async def test_a_memory_description_is_evicted_before_its_name(self):
        db = _db_with(memories=[_memory_factory(
            name="reference_lemonsqueezer", description="Lemonsqueezer notes " + "live in the wiki " * 8)])
        ample = await _recall(db, prompt="check lemonsqueezer", matched_only=True, budget=200_000)
        tight = await _recall(db, prompt="check lemonsqueezer", matched_only=True,
                              budget=_briefing_tokens(ample) - 1)
        assert tight["memories"][0]["name"] == "reference_lemonsqueezer"
        assert tight["memories"][0]["description"] == ""
        assert "reference_lemonsqueezer" in tight["briefing"]
        assert "wiki" not in tight["briefing"]


# ===========================================================================
# 15, 16. exclude_ids and matched_only
# ===========================================================================


class TestExcludeAndMatchedOnly:
    @pytest.mark.asyncio
    async def test_excluded_ids_appear_nowhere(self):
        d_path = _path_decision(rationale="Route the writes through the gate. Detail.")
        db = _db_with(
            {5: {"rationale": f"Settle topic05. Handle glimmerwisp carefully. {_GENERIC}"}},
            memories=[_memory_factory(name="reference_lemonsqueezer",
                                      description="Lemonsqueezer notes live in the wiki.")],
            path_hits={"writ/foo.py": [_path_hit(d_path)]},
        )
        prompt = "glimmerwisp lemonsqueezer writ/foo.py"
        base = await _recall(db, prompt=prompt, project_root=PROJECT_ROOT)
        assert {"DEC-P", "DEC-C05"} <= set(_ids(base)) and base["memories"]

        excluded = ["DEC-P", "DEC-C05", "DEC-C00", "memory:reference_lemonsqueezer"]
        result = await _recall(db, prompt=prompt, project_root=PROJECT_ROOT, exclude_ids=excluded)

        assert not set(excluded) & set(_ids(result))
        assert result["memories"] == []
        assert not {c["id"] for c in result["cards"]} & set(excluded)
        assert "Route the writes through the gate" not in result["briefing"]
        assert "reference_lemonsqueezer" not in result["briefing"]

    @pytest.mark.asyncio
    async def test_recency_fill_skips_excluded_ids(self):
        result = await _recall(_db_with(), exclude_ids=["DEC-C00", "DEC-C01"])
        assert _ids(result) == [f"DEC-C{n:02d}" for n in range(2, 7)]

    @pytest.mark.asyncio
    async def test_matched_only_does_no_recency_fill(self):
        d_path = _path_decision()
        db = _db_with(path_hits={"writ/foo.py": [_path_hit(d_path)]})
        result = await _recall(db, prompt="writ/foo.py", project_root=PROJECT_ROOT, matched_only=True)
        assert _ids(result) == ["DEC-P"]

    @pytest.mark.asyncio
    async def test_matched_only_with_nothing_unseen_returns_an_empty_briefing(self):
        d_path = _path_decision()
        db = _db_with(path_hits={"writ/foo.py": [_path_hit(d_path)]})
        result = await _recall(
            db, prompt="writ/foo.py", project_root=PROJECT_ROOT, matched_only=True, exclude_ids=["DEC-P"],
        )
        assert result["briefing"] == ""
        assert result["cards"] == [] and result["decisions"] == [] and result["memories"] == []

    @pytest.mark.asyncio
    async def test_a_prompt_that_matches_nothing_returns_nothing_under_matched_only(self):
        result = await _recall(_db_with(), prompt="ok, continue with the next step", matched_only=True)
        assert result["briefing"] == "" and result["cards"] == []


# ===========================================================================
# 17. Full mode
# ===========================================================================


class TestFullMode:
    @pytest.mark.asyncio
    async def test_one_batched_statement_read_over_the_deduped_union_of_kept_rule_ids(self):
        decisions = [
            _decision_factory(decision_id=f"DEC-{i}", ts=f"2026-06-{20 - i:02d}T10:00:00+00:00",
                              governing_rule_ids=["SHARED-RULE", f"RULE-{i}"])
            for i in range(7)
        ]
        db = _FakeDB(decisions=decisions, statements={"SHARED-RULE": "Shared statement."})
        result = await _recall(db, full=True, budget=_recall_mod().RECALL_FULL_BUDGET)

        assert len(result["decisions"]) == 7, "full mode is not slot capped"
        assert len(db.get_rule_statements_calls) == 1
        fetched = db.get_rule_statements_calls[0]
        assert sorted(fetched) == sorted({"SHARED-RULE", *[f"RULE-{i}" for i in range(7)]})
        assert fetched.count("SHARED-RULE") == 1
        assert result["decisions"][0]["rule_statements"]["SHARED-RULE"] == "Shared statement."

    @pytest.mark.asyncio
    async def test_full_decisions_carry_the_full_rationale(self):
        long = "This rationale is deliberately far longer than the card's clip. " * 12
        d = _decision_factory(decision_id="DEC-LONG", rationale=long.strip())
        result = await _recall(_FakeDB(decisions=[d]), full=True, budget=_recall_mod().RECALL_FULL_BUDGET)
        assert result["decisions"][0]["rationale"] == long.strip()

    @pytest.mark.asyncio
    async def test_without_full_no_statement_read_happens_and_the_key_is_empty(self):
        db = _db_with()
        result = await _recall(db)
        assert db.get_rule_statements_calls == []
        assert all(d["rule_statements"] == {} for d in result["decisions"])


# ===========================================================================
# 18. The corpus cache
# ===========================================================================


class TestCorpusCache:
    @pytest.mark.asyncio
    async def test_the_corpus_is_read_with_a_limit_of_200_decisions_and_the_project_memories(self):
        db = _db_with()
        await _recall(db)
        assert db.recent_calls == [("proj", 200)]
        assert db.memory_calls == [("proj", False)]

    @pytest.mark.asyncio
    async def test_a_second_compile_within_the_ttl_reads_nothing(self):
        db = _db_with()
        await _recall(db)
        await _recall(db, prompt="zorblat")
        assert len(db.recent_calls) == 1
        assert len(db.memory_calls) == 1

    @pytest.mark.asyncio
    async def test_the_path_read_is_never_cached(self):
        db = _db_with()
        await _recall(db, prompt="writ/foo.py", project_root=PROJECT_ROOT)
        await _recall(db, prompt="writ/foo.py", project_root=PROJECT_ROOT)
        assert len(db.path_calls) == 2

    @pytest.mark.asyncio
    async def test_after_the_ttl_the_corpus_is_read_again(self, monkeypatch):
        db = _db_with()
        await _recall(db)
        monkeypatch.setattr(_recall_mod(), "RECALL_CORPUS_TTL_S", 0)
        await _recall(db)
        assert len(db.recent_calls) == 2
        assert len(db.memory_calls) == 2

    def test_the_ttl_is_sixty_seconds(self):
        assert _recall_mod().RECALL_CORPUS_TTL_S == 60

    @pytest.mark.asyncio
    async def test_two_db_objects_never_share_a_cached_corpus(self):
        db_a, db_b = _db_with(), _db_with()
        await _recall(db_a)
        await _recall(db_b)
        assert len(db_a.recent_calls) == 1 and len(db_b.recent_calls) == 1

    @pytest.mark.asyncio
    async def test_two_projects_on_one_db_do_not_share_a_cached_corpus(self):
        db = _db_with()
        await _recall(db, "proj-one")
        await _recall(db, "proj-two")
        assert [p for p, _ in db.recent_calls] == ["proj-one", "proj-two"]

    @pytest.mark.asyncio
    async def test_invalidating_a_project_reloads_only_that_project(self):
        db = _db_with()
        await _recall(db, "proj-one")
        await _recall(db, "proj-two")
        _recall_mod().invalidate_corpus(db, "proj-one")
        await _recall(db, "proj-one")
        await _recall(db, "proj-two")
        assert [p for p, _ in db.recent_calls] == ["proj-one", "proj-two", "proj-one"]

    @pytest.mark.asyncio
    async def test_invalidating_without_a_project_reloads_every_project_of_that_db(self):
        db, other = _db_with(), _db_with()
        await _recall(db, "proj-one")
        await _recall(db, "proj-two")
        await _recall(other, "proj-one")
        _recall_mod().invalidate_corpus(db)
        await _recall(db, "proj-one")
        await _recall(db, "proj-two")
        await _recall(other, "proj-one")
        assert len(db.recent_calls) == 4
        assert len(other.recent_calls) == 1

    def test_invalidating_a_db_never_cached_is_a_no_op(self):
        _recall_mod().invalidate_corpus(_db_with(), "proj")


_NEW_DECISION = _decision_factory(
    decision_id="DEC-NEW", rationale="Handle zorblat carefully.", ts="2026-07-01T10:00:00+00:00",
)


class _CaptureDB(_FakeDB):
    async def create_memory(self, **fields) -> str:
        self._memories.append({"name": fields["name"], "description": fields["description"],
                               "type": "", "updated_at": ""})
        return fields["name"]


class TestCaptureInvalidatesTheCorpus:
    def _primed(self, monkeypatch) -> _CaptureDB:
        db = _CaptureDB(decisions=_corpus20())
        asyncio.run(_recall(db))
        monkeypatch.setattr(server, "_db", db)
        return db

    def test_a_successful_commit_capture_reloads_the_corpus_on_the_next_compile(self, client, monkeypatch):
        db = self._primed(monkeypatch)

        async def _capture(*a, **k):
            db._decisions.insert(0, _NEW_DECISION)
            return "proj"

        monkeypatch.setattr(server, "capture_commit", _capture)
        assert client.post("/commit/capture", json={"project_root": "/repo/proj", "commit_hash": "abc"}).json() == {"ok": True}
        result = asyncio.run(_recall(db, prompt="what about zorblat", matched_only=True))
        assert len(db.recent_calls) == 2
        assert _ids(result) == ["DEC-NEW"]

    def test_a_failed_commit_capture_returns_ok_and_keeps_the_cached_corpus(self, client, monkeypatch):
        db = self._primed(monkeypatch)

        async def _capture(*a, **k):
            raise RuntimeError("graph down")

        monkeypatch.setattr(server, "capture_commit", _capture)
        assert client.post("/commit/capture", json={"project_root": "/repo/proj", "commit_hash": "abc"}).json() == {"ok": True}
        asyncio.run(_recall(db))
        assert len(db.recent_calls) == 1

    def test_a_memory_record_reloads_the_corpus_on_the_next_compile(self, client, monkeypatch):
        db = self._primed(monkeypatch)
        body = {"path": "/x/proj/memory/zorblat_notes.md", "name": "zorblat_notes",
                "project": "proj", "description": "Zorblat notes"}
        assert client.post("/memory-record", json=body).json() == {"ok": True, "name": "zorblat_notes"}
        result = asyncio.run(_recall(db, prompt="what about zorblat", matched_only=True))
        assert len(db.memory_calls) == 2
        assert [m["name"] for m in result["memories"]] == ["zorblat_notes"]

    def test_an_approval_that_captures_a_decision_reloads_the_corpus(self, client, monkeypatch, tmp_path):
        from tests.test_decision_memory_capture import (
            _VALID_PLAN,
            _advance_post,
            _seed_planning_phase,
            _write_gate_token,
        )

        db = self._primed(monkeypatch)

        async def _capture(*a, **k):
            db._decisions.insert(0, _NEW_DECISION)
            return "DEC-NEW"

        monkeypatch.setattr(server, "capture_decision_at_approve", _capture)
        sid = f"recall-inv-{uuid.uuid4().hex[:8]}"
        _seed_planning_phase(tmp_path / "cache", sid)
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_VALID_PLAN)
        _write_gate_token(sid, "tok-" + sid)
        assert _advance_post(client, sid, "tok-" + sid, project_root=str(plan_dir)).get("phase") == "testing"
        result = asyncio.run(_recall(db, prompt="what about zorblat", matched_only=True))
        assert _ids(result) == ["DEC-NEW"]


# ===========================================================================
# 19 to 25, 37. /prompt-bundle: the shown-ids cadence
# ===========================================================================


def _cache(**overrides) -> dict:
    cache = {
        "loaded_rule_ids_by_phase": {}, "current_phase": "", "loaded_rule_ids": [],
        "remaining_budget": 8000, "last_injected_rule_ids": [], "detected_domain": "",
        "context_percent": 0, "is_subagent": False, "compaction_epoch": 0, "injection_shown": {},
    }
    cache.update(overrides)
    return cache


def _recall_payload(briefing="", cards=()):
    return {"ok": True, "briefing": briefing, "decisions": [], "memories": [], "cards": list(cards)}


def _install(monkeypatch, *, cache=None, recall_payload=None):
    monkeypatch.setattr(server, "_pipeline", object())
    monkeypatch.setattr(server, "_trigger_index", SimpleNamespace(floor_ids=lambda mode: set()))
    snapshot = cache if cache is not None else _cache()
    monkeypatch.setattr(server.writ_session, "_read_cache", lambda sid: dict(snapshot))
    spies = SimpleNamespace(
        recall=AsyncMock(return_value=recall_payload if recall_payload is not None else _recall_payload()),
        update=MagicMock(),
        exception=MagicMock(),
    )
    monkeypatch.setattr(server.writ_session, "cmd_update", spies.update)
    monkeypatch.setattr(qroute, "emit_exception", spies.exception)
    decision_memory = importlib.import_module("writ.server.routes.decision_memory")
    monkeypatch.setattr(decision_memory, "recall", spies.recall)
    return spies


async def _turn(prompt="please change writ/foo.py", **kw):
    return await qroute.prompt_bundle(PromptBundleRequest(
        session_id="s1", prompt=prompt, mode="work", sections=["recall"],
        project_root=kw.pop("project_root", PROJECT_ROOT), **kw))


def _marks(spies) -> list[tuple[str, str, list]]:
    """(section, epoch, ids) for every --mark-shown the route wrote."""
    out = []
    for call in spies.update.call_args_list:
        args = call.args[-1]
        if "--mark-shown" in args:
            i = args.index("--mark-shown")
            out.append((args[i + 1], args[i + 2], json.loads(args[i + 3])))
    return out


def _request_sent(spies) -> RecallRequest:
    return spies.recall.await_args.args[0]


def _briefed_cache(*ids, **overrides) -> dict:
    return _cache(injection_shown={"epoch": "0|", "recall": list(ids)}, **overrides)


CARD_A = {"id": "DEC-A", "head": "- Card A title [RULE-A]"}
CARD_B = {"id": "DEC-B", "head": "- Card B title [RULE-B]"}


def _briefing_of(*cards) -> str:
    return "\n".join([HEADER, *(c["head"] for c in cards)])


class TestFirstBriefOfAnEpoch:
    @pytest.mark.asyncio
    async def test_asks_recall_with_the_section_budget_the_prompt_and_no_excludes(self, monkeypatch):
        spies = _install(monkeypatch, recall_payload=_recall_payload(_briefing_of(CARD_A), [CARD_A]))
        out = await _turn("please change writ/foo.py")
        req = _request_sent(spies)
        assert req.budget == PROMPT_SECTION_TOKENS["recall"] == 500
        assert req.prompt == "please change writ/foo.py"
        assert req.matched_only is False
        assert list(req.exclude_ids) == []
        assert req.project_root == PROJECT_ROOT
        assert out["recall_block"] == _briefing_of(CARD_A)

    @pytest.mark.asyncio
    async def test_records_the_shown_ids_under_the_current_epoch(self, monkeypatch):
        spies = _install(monkeypatch, recall_payload=_recall_payload(_briefing_of(CARD_A, CARD_B), [CARD_A, CARD_B]))
        await _turn()
        assert _marks(spies) == [("recall", "0|", ["DEC-A", "DEC-B"])]

    @pytest.mark.asyncio
    async def test_marks_the_epoch_briefed_even_with_nothing_to_show(self, monkeypatch):
        spies = _install(monkeypatch, recall_payload=_recall_payload(""))
        out = await _turn()
        assert out["recall_block"] == ""
        assert _marks(spies) == [("recall", "0|", [])]

    @pytest.mark.asyncio
    async def test_the_block_is_clamped_to_1998_characters(self, monkeypatch):
        lines = [HEADER] + [f"- Card {i} title [R]\n  why: " + "w" * 400 for i in range(20)]
        spies = _install(monkeypatch, recall_payload=_recall_payload("\n".join(lines), [CARD_A]))
        out = await _turn()
        assert 0 < len(out["recall_block"]) <= 1998
        assert spies.recall.await_count == 1

    @pytest.mark.asyncio
    async def test_a_card_whose_head_the_clamp_removed_is_not_recorded_as_shown(self, monkeypatch):
        briefing = "\n".join([
            HEADER, CARD_A["head"], "  why: " + "w" * 1500, "  src/a.py: " + "r" * 600,
            CARD_B["head"], "  why: short",
        ])
        spies = _install(monkeypatch, recall_payload=_recall_payload(briefing, [CARD_A, CARD_B]))
        out = await _turn()
        assert CARD_A["head"] in out["recall_block"]
        assert CARD_B["head"] not in out["recall_block"]
        assert _marks(spies) == [("recall", "0|", ["DEC-A"])]

    @pytest.mark.asyncio
    async def test_a_recall_failure_returns_a_clean_response_and_still_marks_the_epoch(self, monkeypatch):
        spies = _install(monkeypatch)
        spies.recall.side_effect = RuntimeError("decision store unavailable")
        out = await _turn()
        assert out["error"] is False
        assert out["recall_block"] == ""
        assert spies.exception.call_args.args[0] == "server.prompt_bundle.recall"
        assert _marks(spies) == [("recall", "0|", [])]

    @pytest.mark.asyncio
    async def test_a_cache_still_holding_recall_briefed_true_does_not_suppress_the_brief(self, monkeypatch):
        spies = _install(
            monkeypatch,
            cache=_cache(recall_briefed=True),
            recall_payload=_recall_payload(_briefing_of(CARD_A), [CARD_A]),
        )
        out = await _turn()
        assert spies.recall.await_count == 1
        assert out["recall_block"] != ""


class TestLaterPromptsInTheSameEpoch:
    @pytest.mark.asyncio
    async def test_asks_for_matched_cards_only_and_excludes_what_was_shown(self, monkeypatch):
        spies = _install(monkeypatch, cache=_briefed_cache("DEC-B", "DEC-A"))
        await _turn("what about writ/bar.py")
        req = _request_sent(spies)
        assert req.matched_only is True
        assert sorted(req.exclude_ids) == ["DEC-A", "DEC-B"]
        assert req.budget == PROMPT_SECTION_TOKENS["recall"]

    @pytest.mark.asyncio
    async def test_a_prompt_that_matches_nothing_unseen_is_silent_and_writes_no_cache(self, monkeypatch):
        spies = _install(monkeypatch, cache=_briefed_cache("DEC-A"), recall_payload=_recall_payload(""))
        out = await _turn("ok, continue")
        assert out["recall_block"] == ""
        assert spies.update.call_count == 0

    @pytest.mark.asyncio
    async def test_a_newly_matched_decision_shows_alone_and_its_id_is_added(self, monkeypatch):
        spies = _install(
            monkeypatch, cache=_briefed_cache("DEC-A"),
            recall_payload=_recall_payload(_briefing_of(CARD_B), [CARD_B]),
        )
        out = await _turn("what about writ/bar.py")
        assert CARD_B["head"] in out["recall_block"]
        assert CARD_A["head"] not in out["recall_block"]
        assert _marks(spies) == [("recall", "0|", ["DEC-B"])]

    @pytest.mark.asyncio
    async def test_a_recall_failure_on_a_later_prompt_writes_nothing(self, monkeypatch):
        spies = _install(monkeypatch, cache=_briefed_cache("DEC-A"))
        spies.recall.side_effect = RuntimeError("boom")
        out = await _turn()
        assert out["recall_block"] == ""
        assert spies.update.call_count == 0

    @pytest.mark.asyncio
    async def test_the_section_records_marks_only_under_the_recall_section(self, monkeypatch):
        spies = _install(monkeypatch, recall_payload=_recall_payload(_briefing_of(CARD_A), [CARD_A]))
        await _turn()
        assert {section for section, _, _ in _marks(spies)} == {"recall"}
        flags = {a for c in spies.update.call_args_list for a in c.args[-1] if str(a).startswith("--")}
        assert "--set-recall-briefed" not in flags


class TestANewEpochBriefsAgain:
    @pytest.mark.asyncio
    async def test_after_a_compaction_the_next_prompt_briefs_with_recency_fill(self, monkeypatch):
        cache = _cache(compaction_epoch=1, injection_shown={"epoch": "0|", "recall": ["DEC-A"]})
        spies = _install(monkeypatch, cache=cache, recall_payload=_recall_payload(_briefing_of(CARD_A), [CARD_A]))
        out = await _turn("ok, continue")
        req = _request_sent(spies)
        assert req.matched_only is False
        assert list(req.exclude_ids) == []
        assert CARD_A["head"] in out["recall_block"]
        assert _marks(spies) == [("recall", "1|", ["DEC-A"])]

    @pytest.mark.asyncio
    async def test_after_a_phase_change_the_next_prompt_briefs_again(self, monkeypatch):
        cache = _cache(current_phase="planning", injection_shown={"epoch": "0|", "recall": ["DEC-A"]})
        spies = _install(monkeypatch, cache=cache, recall_payload=_recall_payload(_briefing_of(CARD_A), [CARD_A]))
        await _turn("ok, continue")
        assert _request_sent(spies).matched_only is False
        assert _marks(spies) == [("recall", "0|planning", ["DEC-A"])]


class TestRecallStateRetired:
    def test_recall_and_pre_write_decision_are_collapsible_sections(self):
        from writ.session.injection_state import COLLAPSIBLE_SECTIONS

        assert "recall" in COLLAPSIBLE_SECTIONS
        assert "pre_write_decision" in COLLAPSIBLE_SECTIONS
        assert {"always_on", "floor"} <= set(COLLAPSIBLE_SECTIONS)

    def test_apply_mark_shown_records_recall_and_pre_write_decision_ids(self):
        from writ.session.injection_state import apply_mark_shown, shown_ids

        cache = _cache()
        apply_mark_shown(cache, "recall", "0|", ["DEC-A"])
        apply_mark_shown(cache, "pre_write_decision", "0|", ["a.py#DEC-A"])
        assert shown_ids(cache, "recall") == {"DEC-A"}
        assert shown_ids(cache, "pre_write_decision") == {"a.py#DEC-A"}

    def test_marked_this_epoch_is_true_only_for_the_current_epoch_with_the_section_key(self):
        from writ.session.injection_state import apply_mark_shown, marked_this_epoch

        cache = _cache()
        assert marked_this_epoch(cache, "recall") is False
        apply_mark_shown(cache, "recall", "0|", [])
        assert marked_this_epoch(cache, "recall") is True, "an empty mark still records the epoch"
        assert marked_this_epoch(cache, "pre_write_decision") is False
        cache["compaction_epoch"] = 1
        assert marked_this_epoch(cache, "recall") is False
        cache["compaction_epoch"] = 0
        cache["current_phase"] = "planning"
        assert marked_this_epoch(cache, "recall") is False

    def test_set_recall_briefed_is_no_longer_registered(self):
        from writ.session.budget_tracking import _UPDATE_HANDLERS

        assert "--set-recall-briefed" not in _UPDATE_HANDLERS
        assert "--mark-shown" in _UPDATE_HANDLERS

    def test_a_stale_set_recall_briefed_caller_is_harmless(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.budget_tracking import cmd_update
        from writ.session.cache import _read_cache

        cmd_update("stale-caller", ["--set-recall-briefed"])
        assert not _read_cache("stale-caller").get("recall_briefed"), (
            "the retired flag is skipped as unknown, so it can no longer set the bool"
        )

    def test_the_route_never_reads_or_writes_recall_briefed(self):
        assert "recall_briefed" not in inspect.getsource(qroute)
        budget_tracking = importlib.import_module("writ.session.budget_tracking")
        assert not hasattr(budget_tracking, "_upd_set_recall_briefed")


# ===========================================================================
# 27. /recall
# ===========================================================================


class _ResolverDB:
    def __init__(self, project: str = "proj") -> None:
        self._project = project

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        return self._project


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "friction.log"))
    return TestClient(server.app)


_CANNED = {
    "briefing": "B", "decisions": [{"decision_id": "D1"}],
    "memories": [{"name": "m"}], "cards": [{"id": "D1", "head": "- h"}],
}


class TestRecallRoute:
    def test_request_defaults_the_budget_to_the_section_budget(self):
        req = RecallRequest(project_root="/x")
        assert req.budget == PROMPT_SECTION_TOKENS["recall"]
        assert req.prompt == ""
        assert req.exclude_ids == []
        assert req.matched_only is False

    def test_forwards_every_field_to_compile_recall_and_returns_all_five_keys(self, client, monkeypatch):
        monkeypatch.setattr(server, "_db", _ResolverDB("proj"))
        seen: dict = {}

        async def _compile(db, project, **kwargs):
            seen["project"] = project
            seen.update(kwargs)
            return _CANNED

        with patch("writ.session.recall.compile_recall", new=_compile):
            resp = client.post("/recall", json={
                "project_root": "/repo/proj", "budget": 123, "full": True,
                "prompt": "writ/foo.py", "exclude_ids": ["D9"], "matched_only": True,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data == {"ok": True, **_CANNED}
        assert seen["project"] == "proj"
        assert seen["budget"] == 123 and seen["full"] is True
        assert seen["prompt"] == "writ/foo.py"
        assert list(seen["exclude_ids"]) == ["D9"]
        assert seen["matched_only"] is True
        assert seen["project_root"] == "/repo/proj"

    def test_the_default_budget_forwarded_is_the_section_budget(self, client, monkeypatch):
        monkeypatch.setattr(server, "_db", _ResolverDB("proj"))
        seen: dict = {}

        async def _compile(db, project, **kwargs):
            seen.update(kwargs)
            return _CANNED

        with patch("writ.session.recall.compile_recall", new=_compile):
            client.post("/recall", json={"project_root": "/repo/proj"})
        assert seen["budget"] == PROMPT_SECTION_TOKENS["recall"]

    def test_unresolved_project_returns_the_empty_payload_and_logs_the_same_row(self, client, monkeypatch):
        monkeypatch.setattr(server, "_db", _ResolverDB(""))
        log = MagicMock()
        with patch("writ.server.log_friction_event", new=log):
            data = client.post("/recall", json={"project_root": "/nowhere"}).json()
        assert data == {"ok": True, "briefing": "", "decisions": [], "memories": [], "cards": []}
        assert log.call_args.kwargs["event"] == "recall_project_unresolved"

    def test_a_compile_failure_returns_the_empty_payload_and_logs_the_same_row(self, client, monkeypatch):
        monkeypatch.setattr(server, "_db", _ResolverDB("proj"))
        log = MagicMock()

        async def _raise(*a, **k):
            raise RuntimeError("boom")

        with patch("writ.session.recall.compile_recall", new=_raise), \
                patch("writ.server.log_friction_event", new=log):
            data = client.post("/recall", json={"project_root": "/repo/proj"}).json()
        assert data == {"ok": True, "briefing": "", "decisions": [], "memories": [], "cards": []}
        assert log.call_args.kwargs["event"] == "recall_failed"

    def test_the_docstring_first_line_names_the_ranked_briefing(self):
        decision_memory = importlib.import_module("writ.server.routes.decision_memory")
        first = (decision_memory.recall.__doc__ or "").strip().splitlines()[0]
        assert "ranked" in first.lower() and "briefing" in first.lower()


# ===========================================================================
# 28. The CLI
# ===========================================================================


def _cli_db(project: str = "proj"):
    @asynccontextmanager
    async def _ctx():
        yield _ResolverDB(project)

    return _ctx


def _run_cli(args: list[str], payload: dict, tmp_path: Path):
    seen: dict = {}

    async def _compile(db, project, *a, **kwargs):
        seen["project"] = project
        seen.update(kwargs)
        return payload

    with patch("writ.cli._writ_db", new=_cli_db()), patch("writ.session.recall.compile_recall", new=_compile):
        from writ.cli import app

        result = runner.invoke(app, ["recall", "--repo", str(tmp_path), *args])
    return result, seen


class TestRecallCli:
    def test_without_full_it_uses_the_section_budget_and_passes_the_project_root(self, tmp_path):
        result, seen = _run_cli([], {"briefing": "BRIEF", "decisions": []}, tmp_path)
        assert result.exit_code == 0, result.output
        assert "BRIEF" in result.output
        assert seen["project_root"] == os.path.abspath(str(tmp_path))
        assert seen.get("budget", PROMPT_SECTION_TOKENS["recall"]) == PROMPT_SECTION_TOKENS["recall"]
        assert seen["full"] is False
        assert not seen.get("prompt")

    def test_with_full_it_passes_the_full_listing_budget(self, tmp_path):
        result, seen = _run_cli(["--full"], {"briefing": "BRIEF", "decisions": []}, tmp_path)
        assert result.exit_code == 0, result.output
        assert seen["budget"] == _recall_mod().RECALL_FULL_BUDGET == 20000
        assert seen["full"] is True

    def test_full_prints_the_rationale_and_statements_for_up_to_limit_decisions(self, tmp_path):
        decisions = [
            {"decision_id": f"DEC-{i}", "title": f"Title {i}", "rationale": f"Rationale {i}.",
             "rule_statements": {"RULE-A": f"Statement {i}"}}
            for i in range(3)
        ]
        result, _ = _run_cli(["--full", "--limit", "2"], {"briefing": "BRIEF", "decisions": decisions}, tmp_path)
        assert "# Title 0 (DEC-0)" in result.output and "# Title 1 (DEC-1)" in result.output
        assert "# Title 2" not in result.output
        assert "rationale: Rationale 0." in result.output
        assert "RULE-A: Statement 1" in result.output

    def test_the_empty_message_is_unchanged(self, tmp_path):
        result, _ = _run_cli([], {"briefing": "", "decisions": []}, tmp_path)
        assert "[Writ recall: no decisions captured for this project yet.]" in result.output
