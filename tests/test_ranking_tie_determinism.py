"""Ties in the ranking stage are ordered by rule id, in every process.

Plan: .claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md (capabilities 1 and 2).

The ranking stage sorts by score alone (writ/retrieval/pipeline.py `scored_rules.sort` and
`first_pass_scores.sort`). Python's sort is stable, so tied scores keep their arrival order, and
that order is a property of whatever produced the candidates, not of the corpus. The fix breaks
ties by rule id.

The fixture is a real RetrievalPipeline (real merge, normalize, compute_score, sorts, budget)
with stub keyword and vector stages and no Neo4j. The BM25 list and the vector list hold the
same five ids in opposite order, in literal mode (equal BM25 and vector weights), so reciprocal
ranks make position i tie with position n-1-i. The BM25 order is deliberately not alphabetical,
so arrival order and rule-id order disagree on every tie.

What this file can and cannot prove: the stubs return a fixed order, so on code that has no
other hash-ordered step the cross-seed test is green before and after the fix. It exists to
catch any hash-ordered iteration that does reach a tie; the rule-id tests are the ones that are
red on the current code.

Run: .venv/bin/python3 -m pytest -q tests/test_ranking_tie_determinism.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# BM25 arrival order (best first). Not alphabetical on purpose.
ARRIVAL = ["RULE-E", "RULE-D", "RULE-X", "RULE-B", "RULE-A"]
# Position i ties with position n-1-i, so the tie groups are {E, A} and {D, B}; X is alone.
EXPECTED_BY_ID = ["RULE-A", "RULE-E", "RULE-B", "RULE-D", "RULE-X"]


def _pipeline():
    import numpy as np

    from writ.retrieval.embeddings import ScoredResult
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

    n = len(ARRIVAL)
    keyword = MagicMock()
    keyword.search.return_value = [
        {"rule_id": rid, "score": float(n - i)} for i, rid in enumerate(ARRIVAL)
    ]
    vector = MagicMock()
    vector.search.return_value = [
        ScoredResult(rule_id=rid, score=0.9 - 0.01 * i) for i, rid in enumerate(reversed(ARRIVAL))
    ]
    model = MagicMock()
    model.encode.return_value = np.zeros(4)
    metadata = {
        rid: {"node_type": "Rule", "domain": "d", "project": "writ", "severity": "medium",
              "confidence": "production-validated", "authority": "human"}
        for rid in ARRIVAL
    }
    return RetrievalPipeline(
        keyword_index=keyword, vector_store=vector, adjacency_cache=AdjacencyCache(),
        embedding_model=model, rule_metadata=metadata,
    )


def rank_ties() -> list[list]:
    """The ranked [rule_id, score] pairs for the tied candidate set. Used in-process and by
    the subprocess probe."""
    result = _pipeline().query("tie probe", retrieval_mode="literal", budget_tokens=100000)
    return [[r["rule_id"], r["score"]] for r in result["rules"]]


_PROBE = (
    "import json, sys\n"
    "sys.path.insert(0, {repo!r})\n"
    "from tests.test_ranking_tie_determinism import rank_ties\n"
    "print('@@RESULT@@' + json.dumps(rank_ties()))\n"
)


def _run_under_seed(seed: str) -> list[list]:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=str(REPO_ROOT))],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=180,
    )
    marker = "@@RESULT@@"
    if marker not in proc.stdout:
        pytest.fail(f"probe produced no result under PYTHONHASHSEED={seed} "
                    f"(exit {proc.returncode}); stderr tail:\n{proc.stderr[-800:]}")
    return json.loads(proc.stdout.split(marker, 1)[1].strip())


class TestTieFixtureIsReallyTied:
    def test_the_symmetric_positions_have_equal_scores(self):
        ranked = dict(map(tuple, rank_ties()))
        assert ranked["RULE-E"] == ranked["RULE-A"]
        assert ranked["RULE-D"] == ranked["RULE-B"]
        assert ranked["RULE-E"] > ranked["RULE-D"] > ranked["RULE-X"]


class TestTiesAreOrderedByRuleId:
    def test_final_ranking_orders_tied_scores_by_rule_id(self):
        assert [rid for rid, _ in rank_ties()] == EXPECTED_BY_ID

    def test_first_pass_ranking_orders_tied_scores_by_rule_id(self):
        from writ.retrieval.ranking import RankingWeights

        pipe = _pipeline()
        candidates = {rid: {"bm25_norm": 0.5, "vector_norm": 0.5} for rid in ARRIVAL}
        ranked = pipe._first_pass_rank(candidates, RankingWeights.literal())
        assert len({score for _, score in ranked}) == 1
        assert [rid for rid, _ in ranked] == sorted(ARRIVAL)

    def test_the_tie_order_does_not_depend_on_arrival_order(self):
        from writ.retrieval.ranking import RankingWeights

        pipe = _pipeline()
        weights = RankingWeights.literal()
        forward = pipe._first_pass_rank(
            {rid: {"bm25_norm": 0.5, "vector_norm": 0.5} for rid in ARRIVAL}, weights)
        backward = pipe._first_pass_rank(
            {rid: {"bm25_norm": 0.5, "vector_norm": 0.5} for rid in reversed(ARRIVAL)}, weights)
        assert forward == backward

    def test_a_higher_score_still_beats_a_lower_rule_id(self):
        ids = [rid for rid, _ in rank_ties()]
        assert ids.index("RULE-E") < ids.index("RULE-X")
        assert ids.index("RULE-D") < ids.index("RULE-X")


class TestCrossProcessDeterminism:
    def test_two_hash_seeds_and_a_random_one_give_the_identical_order_and_scores(self):
        runs = [_run_under_seed(s) for s in ("0", "1", "random")]
        assert runs[0] == runs[1] == runs[2]

    def test_every_process_orders_ties_by_rule_id(self):
        for seed in ("0", "1", "random"):
            assert [rid for rid, _ in _run_under_seed(seed)] == EXPECTED_BY_ID, seed
