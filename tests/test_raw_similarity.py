"""Program item 5, workstream P, phase 0 part 2: the raw cosine on the ranked entry.

The printed `score=` is the composite compute_score. The raw cosine of the vector stage was
kept as vector_score in the candidate dict and dropped from the entry, so the agent could not
tell a strong semantic match from a weak one riding on keyword overlap. Item 5 puts it back:

  * _merge_and_normalize records each candidate's raw cosine as "similarity" (None when only
    the keyword stage found it) and _final_rank puts round(value, 4) on the entry (14)
  * ranking._HEADER_FIELDS carries it through summary, standard, full and the ungrouped
    summary fallback exactly as item 6 carries stale and deliberate (15)
  * cmd_format renders ` sim=0.612` after the score, ` sim=n/a` for None and nothing when the
    key is absent; methodology rows and abstraction summaries never show it (16)
  * ranking does not move: ids, order and scores are identical with and without the key, and
    _merge_and_normalize iterates in the same order (17)

No graph, no model: the pipeline is built in process with MagicMock indexes, the stub pattern of
tests/test_ranked_header_fields.py. The cosine against a real HNSW index is proven in
tests/test_item5_p_graph.py.

RED today: candidates carry no "similarity", _HEADER_FIELDS does not name it, injection_text
has no similarity_slot.

Run: flock <lock> .venv/bin/python3 -m pytest tests/test_raw_similarity.py
"""
from __future__ import annotations

import io
import json
import re
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.test_ranked_header_fields import _node_meta
from writ.retrieval import prompt_bundle as pb
from writ.retrieval import ranking
from writ.retrieval.embeddings import ScoredResult
from writ.retrieval.ranking import STANDARD_THRESHOLD, SUMMARY_THRESHOLD, apply_context_budget
from writ.session import budget_tracking as bt


def _pipeline(metadata: dict, bm25_ids, vector_scores: dict, *, abstention_threshold: float = 0.0):
    """A RetrievalPipeline whose keyword stage returns `bm25_ids` and whose vector stage
    returns `vector_scores` ({rule_id: raw cosine}); both are filtered by Stage 1 as in
    production."""
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

    keyword = MagicMock()
    keyword.search.return_value = [
        {"rule_id": rid, "score": 5.0 - i * 0.1} for i, rid in enumerate(bm25_ids)
    ]
    vector = MagicMock()
    vector.search.return_value = [ScoredResult(rule_id=r, score=s) for r, s in vector_scores.items()]
    encoder = MagicMock()
    encoder.encode.return_value = np.zeros(384, dtype=np.float32)
    return RetrievalPipeline(
        keyword_index=keyword, vector_store=vector, adjacency_cache=AdjacencyCache(),
        embedding_model=encoder, rule_metadata=metadata, abstention_threshold=abstention_threshold,
    )


def _metadata(*ids: str) -> dict:
    return {rid: _node_meta() for rid in ids}


def _entries(response: dict) -> dict[str, dict]:
    return {r["rule_id"]: r for r in response["rules"]}


def _render(monkeypatch, capsys, rules, mode="standard") -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rules": rules, "mode": mode})))
    bt.cmd_format()
    text, _meta = pb.split_format(capsys.readouterr().out)
    return text


# ===========================================================================
# 14. The entry carries similarity
# ===========================================================================


class TestEntrySimilarity:
    def test_a_vector_stage_candidate_carries_the_raw_cosine_rounded_to_four_places(self):
        pipe = _pipeline(_metadata("R-V"), ["R-V"], {"R-V": 0.612345})
        entry = _entries(pipe.query("some query", budget_tokens=5000))["R-V"]
        assert entry["similarity"] == round(0.612345, 4) == 0.6123

    def test_a_keyword_only_candidate_carries_none_not_zero(self):
        pipe = _pipeline(_metadata("R-V", "R-K"), ["R-V", "R-K"], {"R-V": 0.8})
        entries = _entries(pipe.query("some query", budget_tokens=5000))
        assert "similarity" in entries["R-K"] and entries["R-K"]["similarity"] is None
        assert entries["R-V"]["similarity"] == 0.8

    def test_a_vector_only_candidate_keeps_its_cosine(self):
        pipe = _pipeline(_metadata("R-A", "R-B"), ["R-A"], {"R-B": 0.4321, "R-A": 0.7})
        entries = _entries(pipe.query("some query", budget_tokens=5000))
        assert entries["R-B"]["similarity"] == 0.4321

    def test_the_value_is_the_unprojected_cosine_not_a_normalised_rank(self):
        pipe = _pipeline(_metadata("R-1", "R-2", "R-3"), ["R-1", "R-2", "R-3"],
                         {"R-1": 0.31, "R-2": 0.62, "R-3": 0.93})
        entries = _entries(pipe.query("some query", budget_tokens=5000))
        assert {k: v["similarity"] for k, v in entries.items()} == {"R-1": 0.31, "R-2": 0.62, "R-3": 0.93}

    def test_merge_and_normalize_records_the_raw_cosine_or_none_on_each_candidate(self):
        pipe = _pipeline({}, [], {})
        merged = pipe._merge_and_normalize(
            [{"rule_id": "A", "score": 2.0}, {"rule_id": "B", "score": 1.0}],
            [ScoredResult(rule_id="C", score=0.8), ScoredResult(rule_id="A", score=0.6)],
        )
        assert merged["A"]["similarity"] == 0.6
        assert merged["B"]["similarity"] is None
        assert merged["C"]["similarity"] == 0.8
        assert merged["B"]["vector_score"] == 0.0, "the ranking input keeps its 0.0 for a keyword-only hit"

    def test_an_abstained_response_is_unchanged(self):
        pipe = _pipeline(_metadata("R-V"), ["R-V"], {"R-V": 0.1}, abstention_threshold=0.5)
        response = pipe.query("some query", budget_tokens=5000)
        assert response["rules"] == []
        assert response["mode"] == "abstained"
        assert response["abstain_signal"] == 0.1
        assert set(response) == {"rules", "mode", "total_candidates", "latency_ms", "abstain_signal"}


# ===========================================================================
# 15. Through every projection
# ===========================================================================


def _source_rule(rule_id: str = "PROJ-SIM-001", **extra) -> dict:
    return {
        "rule_id": rule_id, "node_type": "Rule", "score": 0.834, "severity": "critical",
        "authority": "human", "similarity": 0.6123, "statement": "s.", "trigger": "t.",
        "violation": "v.", "pass_example": "p.", "rationale": "r.", "relationships": [], **extra,
    }


class TestSimilaritySurvivesProjection:
    def test_header_fields_name_similarity(self):
        assert ranking._HEADER_FIELDS == ("severity", "authority", "stale", "deliberate", "similarity")

    @pytest.mark.parametrize("budget,mode", [
        (SUMMARY_THRESHOLD - 1, "summary"),
        (SUMMARY_THRESHOLD, "standard"),
        (STANDARD_THRESHOLD, "standard"),
        (STANDARD_THRESHOLD + 1, "full"),
    ])
    def test_every_budget_mode_carries_a_number_and_a_none(self, budget, mode):
        rules = [_source_rule("PROJ-SIM-001"), _source_rule("PROJ-SIM-002", similarity=None)]
        trimmed, actual_mode = apply_context_budget(rules, budget)
        assert actual_mode == mode
        by_id = {r["rule_id"]: r for r in trimmed}
        assert by_id["PROJ-SIM-001"]["similarity"] == 0.6123
        assert "similarity" in by_id["PROJ-SIM-002"] and by_id["PROJ-SIM-002"]["similarity"] is None

    @pytest.mark.parametrize("budget", [SUMMARY_THRESHOLD - 1, STANDARD_THRESHOLD, STANDARD_THRESHOLD + 1])
    def test_a_source_without_the_key_projects_without_it(self, budget):
        rule = _source_rule()
        del rule["similarity"]
        trimmed, _mode = apply_context_budget([rule], budget)
        assert "similarity" not in trimmed[0]

    def test_the_ungrouped_summary_fallback_carries_it(self):
        abstraction = {"abstraction_id": "ABS-X", "rule_ids": ["OTHER-001"], "summary": "sum",
                       "domain": "d", "compression_ratio": 2.0}
        trimmed, mode = apply_context_budget(
            [_source_rule("UNGROUPED-001")], SUMMARY_THRESHOLD - 1, abstractions=[abstraction])
        assert mode == "summary"
        assert trimmed[0]["rule_id"] == "UNGROUPED-001"
        assert trimmed[0]["similarity"] == 0.6123

    def test_an_abstraction_summary_entry_carries_none_of_the_rule_header_fields(self):
        abstraction = {"abstraction_id": "ABS-X", "rule_ids": ["GROUPED-001"], "summary": "sum",
                       "domain": "d", "compression_ratio": 2.0}
        trimmed, _mode = apply_context_budget(
            [_source_rule("GROUPED-001")], SUMMARY_THRESHOLD - 1, abstractions=[abstraction])
        assert trimmed[0]["abstraction_id"] == "ABS-X"
        assert "similarity" not in trimmed[0]

    def test_the_value_survives_the_whole_pipeline_query(self):
        pipe = _pipeline(_metadata("R-V"), ["R-V"], {"R-V": 0.5})
        for budget in (SUMMARY_THRESHOLD - 1, STANDARD_THRESHOLD, STANDARD_THRESHOLD + 1):
            assert _entries(pipe.query("q", budget_tokens=budget))["R-V"]["similarity"] == 0.5


# ===========================================================================
# 16. The header slot
# ===========================================================================


class TestSimilaritySlot:
    @staticmethod
    def _slot(entry):
        from writ.shared.injection_text import similarity_slot

        return similarity_slot(entry)

    def test_a_number_renders_three_decimals_after_a_space(self):
        assert self._slot({"similarity": 0.612}) == " sim=0.612"
        assert self._slot({"similarity": 0.7346}) == " sim=0.735"
        assert self._slot({"similarity": 0.0}) == " sim=0.000"
        assert self._slot({"similarity": 1.0}) == " sim=1.000"

    def test_none_renders_n_a(self):
        assert self._slot({"similarity": None}) == " sim=n/a"

    def test_an_absent_key_renders_nothing(self):
        assert self._slot({}) == ""
        assert self._slot({"score": 0.9}) == ""


class TestHeaderLine:
    def _rule(self, **extra) -> dict:
        return {"rule_id": "SEC-INJ-SQL-002", "severity": "critical", "authority": "human",
                "score": 0.921, "trigger": "t.", "statement": "s.", **extra}

    def test_a_number_prints_after_the_score(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [self._rule(similarity=0.7341)])
        assert "[SEC-INJ-SQL-002] (critical) score=0.921 sim=0.734\n" in text + "\n"

    def test_none_prints_n_a(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [self._rule(similarity=None)])
        assert "[SEC-INJ-SQL-002] (critical) score=0.921 sim=n/a" in text.splitlines()

    def test_an_absent_key_prints_exactly_what_it_printed_before(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [self._rule()])
        assert "[SEC-INJ-SQL-002] (critical) score=0.921" in text.splitlines()
        assert "sim=" not in text

    def test_the_slot_follows_trust_tags_and_authority(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [
            self._rule(authority="ai-provisional", stale=True, deliberate=True, similarity=0.5)])
        assert "[SEC-INJ-SQL-002] (critical, ai-provisional, STALE, DELIBERATE) score=0.921 sim=0.500" in text.splitlines()

    @pytest.mark.parametrize("mode", ["summary", "standard", "full"])
    def test_every_mode_renders_the_slot_on_the_header_line(self, monkeypatch, capsys, mode):
        text = _render(monkeypatch, capsys, [self._rule(similarity=0.25)], mode=mode)
        assert "[SEC-INJ-SQL-002] (critical) score=0.921 sim=0.250" in text.splitlines()

    def test_an_abstraction_summary_never_shows_it(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [{
            "abstraction_id": "ABS-1", "rule_ids": ["R-1", "R-2"], "summary": "Summary.",
            "domain": "d", "similarity": 0.9}], mode="summary")
        assert "[ABSTRACT: ABS-1] (covers 2 rules, d)" in text.splitlines()
        assert "sim=" not in text

    def test_a_methodology_companion_row_without_the_key_shows_none(self, monkeypatch, capsys):
        text = _render(monkeypatch, capsys, [
            {"rule_id": "PBK-X-001", "node_type": "Playbook", "channel": "floor", "severity": "high",
             "authority": "human", "score": 1.0, "trigger": "t.", "statement": "s."},
            {"rule_id": "FL-1", "pointer_only": True, "trigger": "floor"},
        ], mode="summary")
        assert "sim=" not in text

    def test_the_chain_from_metadata_to_the_rendered_line(self, monkeypatch, capsys):
        pipe = _pipeline(_metadata("R-V", "R-K"), ["R-V", "R-K"], {"R-V": 0.612345})
        response = pipe.query("chain query", budget_tokens=5000)
        tagged = pb.tag_overlap(response["rules"], set())
        text = _render(monkeypatch, capsys, tagged, mode=response["mode"])
        headers = [ln for ln in text.splitlines() if ln.startswith("[R-")]
        by_id = {re.match(r"\[(R-\w)\]", ln).group(1): ln for ln in headers}
        assert re.fullmatch(r"\[R-V\] \(critical\) score=\d\.\d{3} sim=0\.612", by_id["R-V"]), by_id["R-V"]
        assert re.fullmatch(r"\[R-K\] \(critical\) score=\d\.\d{3} sim=n/a", by_id["R-K"]), by_id["R-K"]


# ===========================================================================
# 17. Ranking does not move
# ===========================================================================


def _without_similarity(pipe):
    """The same pipeline with _merge_and_normalize stripped of the new key: what a query
    computed before the change."""
    original = pipe._merge_and_normalize

    def stripped(bm25_results, vector_results):
        candidates = original(bm25_results, vector_results)
        for candidate in candidates.values():
            candidate.pop("similarity", None)
        return candidates

    pipe._merge_and_normalize = stripped
    return pipe


class TestRankingUnchanged:
    IDS = ("R-1", "R-2", "R-3", "R-4", "R-5", "R-6")
    VECTORS = {"R-1": 0.31, "R-3": 0.93, "R-4": 0.62, "R-6": 0.62, "R-7": 0.2}

    def _ids_order_scores(self, pipe, budget=5000):
        response = pipe.query("determinism query", budget_tokens=budget)
        return [(r["rule_id"], r["score"]) for r in response["rules"]]

    @pytest.mark.parametrize("budget", [SUMMARY_THRESHOLD - 1, STANDARD_THRESHOLD, STANDARD_THRESHOLD + 1])
    def test_ids_order_and_scores_are_identical_with_and_without_the_key(self, budget):
        metadata = _metadata(*self.IDS, "R-7")
        with_key = _pipeline(metadata, list(self.IDS), self.VECTORS)
        without_key = _without_similarity(_pipeline(metadata, list(self.IDS), self.VECTORS))
        got = self._ids_order_scores(with_key, budget)
        assert got and got == self._ids_order_scores(without_key, budget)

    def test_a_stripped_pipeline_really_lacks_the_key_or_holds_none(self):
        metadata = _metadata(*self.IDS, "R-7")
        stripped = _without_similarity(_pipeline(metadata, list(self.IDS), self.VECTORS))
        merged = stripped._merge_and_normalize(
            [{"rule_id": "R-1", "score": 1.0}], [ScoredResult(rule_id="R-1", score=0.5)])
        assert "similarity" not in merged["R-1"], "the control must differ from the live merge"

    def test_merge_and_normalize_iterates_bm25_order_then_vector_only_order(self):
        pipe = _pipeline({}, [], {})
        merged = pipe._merge_and_normalize(
            [{"rule_id": "B", "score": 3.0}, {"rule_id": "A", "score": 2.0}, {"rule_id": "D", "score": 1.0}],
            [ScoredResult(rule_id="E", score=0.9), ScoredResult(rule_id="A", score=0.8),
             ScoredResult(rule_id="C", score=0.7)],
        )
        assert list(merged) == ["B", "A", "D", "E", "C"]

    def test_the_normalised_ranks_are_unchanged(self):
        from writ.retrieval.ranking import normalize_ranks

        pipe = _pipeline({}, [], {})
        bm25 = [{"rule_id": "A", "score": 3.0}, {"rule_id": "B", "score": 1.0}]
        vec = [ScoredResult(rule_id="B", score=0.9), ScoredResult(rule_id="C", score=0.4)]
        merged = pipe._merge_and_normalize(bm25, vec)
        ids = list(merged)
        assert [merged[i]["bm25_norm"] for i in ids] == normalize_ranks([merged[i]["bm25_score"] for i in ids])
        assert [merged[i]["vector_norm"] for i in ids] == normalize_ranks([merged[i]["vector_score"] for i in ids])
