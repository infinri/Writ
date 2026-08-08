"""Retrieval must return the same rules for the same query in every process.

Pins every checkbox in capabilities.md, item for item.

The defect: `_merge_and_normalize` built its candidate set with
`set(bm25_scores) | set(vector_scores)` and iterated it. Python randomizes string
hashing per process, so that iteration order changed between daemon starts, and
the order was load-bearing twice over. `normalize_ranks` breaks ties with a STABLE
sort, so a rule's bm25_norm/vector_norm actually changed; then `_final_rank` and
the final sort inherited the same order, and `apply_context_budget` trims by list
position. Measured across the 193-query gold set at PYTHONHASHSEED 0 vs 7:
30 queries returned a DIFFERENT SET of top-5 rules, 185 had a shared rule whose
score moved, and the first divergence was at position 1 or 2 on ten of them.

The headline test drives the REAL pipeline in two subprocesses under different
seeds, rather than unit-testing the merge helper in isolation: the helper in
isolation cannot reproduce the failure, and a test bound to one line would not
notice a new source of ordering nondeterminism somewhere else in the pipeline.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD = REPO_ROOT / "tests" / "fixtures" / "ground_truth_queries.json"

# Runs the live pipeline and prints {query_id: [[rule_id, score], ...]} as JSON.
# Kept as a string and executed by a subprocess because PYTHONHASHSEED only takes
# effect at interpreter startup: it cannot be varied inside the running process.
_PROBE = r"""
import asyncio, json, sys
sys.path.insert(0, %(repo)r)
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.retrieval.pipeline import build_pipeline

async def main():
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        p = await build_pipeline(db)
        queries = json.load(open(%(gold)r))["queries"]
        out = {}
        for q in queries:
            r = p.query(q["query"], budget_tokens=100000)
            out[q["id"]] = [[x["rule_id"], x["score"]] for x in r["rules"]]
        print("@@RESULT@@" + json.dumps(out))
    finally:
        await db.close()

asyncio.run(main())
"""


def _run_under_seed(seed: str) -> dict:
    """Run the probe in a fresh interpreter with PYTHONHASHSEED=<seed>."""
    src = _PROBE % {"repo": str(REPO_ROOT), "gold": str(GOLD)}
    env = dict(os.environ, PYTHONHASHSEED=seed)
    proc = subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True,
        env=env, cwd=str(REPO_ROOT), timeout=600,
    )
    marker = "@@RESULT@@"
    if marker not in proc.stdout:
        # FAIL, never skip. The caller has already confirmed Neo4j is reachable and
        # the corpus complete, so a probe that produces nothing means the pipeline
        # crashed under this hash seed, which is exactly the class of bug this file
        # exists to catch. tests/_corpus.py states the same convention: unreachable
        # is the only legitimate skip; reachable-but-broken must fail.
        pytest.fail(
            f"probe produced no result under PYTHONHASHSEED={seed} "
            f"(exit {proc.returncode}); stderr tail:\n{proc.stderr[-800:]}"
        )
    return json.loads(proc.stdout.split(marker, 1)[1].strip())


# Anchored on the value, not just the label. An earlier version matched
# `MRR@5[^\n]*?[\d.]+`, whose lazy run stops at the first digits AFTER the label,
# which are the constant `n=47` and `all 193`: two runs with different MRR@5 and
# nDCG@10 extracted byte-identical tuples, so the stability test silently compared
# only the hit rate. TestMetricExtraction below pins that it now discriminates.
_BENCH_METRIC_PATTERNS = (
    r"MRR@5 \(ambiguous, n=\d+\): ([\d.]+)",
    r"Hit rate \(all \d+ queries\): (\d+/\d+ = [\d.]+%)",
    r"Hit rate \(index-eligible, n=\d+\): (\d+/\d+ = [\d.]+%)",
    r"nDCG@10 \(all \d+ queries\): ([\d.]+)",
)


def _extract_bench_metrics(stdout: str) -> tuple[str, ...]:
    """The four published metrics, positionally. Empty when a pattern misses, so a
    changed bench output format fails loudly instead of comparing empty tuples."""
    import re

    out: list[str] = []
    for pattern in _BENCH_METRIC_PATTERNS:
        found = re.findall(pattern, stdout)
        if not found:
            return ()
        out.extend(found)
    return tuple(out)


@pytest.fixture(scope="module")
def two_seed_runs() -> tuple[dict, dict]:
    """The same gold set retrieved twice, in processes with different hash seeds.

    Module-scoped because each probe rebuilds the whole pipeline. The corpus check
    is inlined rather than taken from the function-scoped `corpus_ready` fixture,
    which a module-scoped fixture cannot request. Same contract as that fixture:
    skip only when Neo4j is genuinely unreachable, self-heal a wiped graph.
    """
    from tests._corpus import ensure_corpus, neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")
    ensure_corpus()
    return _run_under_seed("0"), _run_under_seed("7")


# --------------------------------------------------------------------------- #
# 1. Determinism across processes
# --------------------------------------------------------------------------- #
class TestCrossProcessDeterminism:
    def test_every_query_returns_identical_rule_order(self, two_seed_runs) -> None:
        a, b = two_seed_runs
        differing = [
            qid for qid in a
            if [r for r, _ in a[qid]] != [r for r, _ in b.get(qid, [])]
        ]
        assert differing == [], (
            f"{len(differing)} of {len(a)} queries returned a different rule order "
            f"under a different hash seed; first few: {differing[:8]}"
        )

    def test_every_query_returns_identical_scores(self, two_seed_runs) -> None:
        """Ordering alone is not enough: the reciprocal-rank tie-break made the
        SCORE itself seed-dependent, which is a different rule, not a display nit."""
        a, b = two_seed_runs
        moved = []
        for qid in a:
            sa, sb = dict(a[qid]), dict(b.get(qid, []))
            if any(rid in sb and sa[rid] != sb[rid] for rid in sa):
                moved.append(qid)
        assert moved == [], (
            f"{len(moved)} of {len(a)} queries scored a shared rule differently "
            f"under a different hash seed; first few: {moved[:8]}"
        )

    def test_top5_set_is_identical(self, two_seed_runs) -> None:
        """The user-visible consequence: which rules actually reach the model at
        the standard budget (STANDARD_LIMIT = 5)."""
        a, b = two_seed_runs
        changed = [
            qid for qid in a
            if {r for r, _ in a[qid][:5]} != {r for r, _ in b.get(qid, [])[:5]}
        ]
        assert changed == [], (
            f"{len(changed)} of {len(a)} queries would inject a DIFFERENT SET of "
            f"top-5 rules after a restart; first few: {changed[:8]}"
        )


# --------------------------------------------------------------------------- #
# 2. The ordered union keeps the candidate set intact
# --------------------------------------------------------------------------- #
class TestOrderedUnion:
    """Fast, no Neo4j: `_merge_and_normalize` over synthetic stage output."""

    def _merge(self, bm25_ids, vector_ids):
        from unittest.mock import MagicMock

        from writ.retrieval.embeddings import ScoredResult
        from writ.retrieval.pipeline import RetrievalPipeline

        pipe = RetrievalPipeline(
            keyword_index=MagicMock(), vector_store=MagicMock(),
            adjacency_cache=MagicMock(), embedding_model=MagicMock(),
            rule_metadata={},
        )
        bm25 = [{"rule_id": r, "score": s} for r, s in bm25_ids]
        vec = [ScoredResult(rule_id=r, score=s) for r, s in vector_ids]
        return pipe._merge_and_normalize(bm25, vec)

    def test_candidate_found_by_both_stages_appears_once(self) -> None:
        merged = self._merge([("A", 0.9), ("B", 0.8)], [("A", 0.7), ("C", 0.6)])
        assert list(merged).count("A") == 1
        assert set(merged) == {"A", "B", "C"}

    def test_vector_only_candidate_survives(self) -> None:
        merged = self._merge([("A", 0.9)], [("Z", 0.5)])
        assert "Z" in merged
        assert merged["Z"]["vector_score"] == 0.5
        assert merged["Z"]["bm25_score"] == 0.0

    def test_order_is_bm25_rank_then_vector_only_rank(self) -> None:
        """Discovery order, not alphabetical: a tie resolves toward the candidate
        the keyword stage surfaced first."""
        merged = self._merge(
            [("B", 0.9), ("A", 0.8)],
            [("A", 0.7), ("Z", 0.6), ("C", 0.5)],
        )
        assert list(merged) == ["B", "A", "Z", "C"]

    def test_empty_stages_produce_empty_candidates(self) -> None:
        assert self._merge([], []) == {}

    def test_bm25_only_and_vector_only_both_normalize(self) -> None:
        """Every candidate carries both normalized fields regardless of origin."""
        merged = self._merge([("A", 0.9)], [("Z", 0.5)])
        for rid in ("A", "Z"):
            assert "bm25_norm" in merged[rid]
            assert "vector_norm" in merged[rid]


# --------------------------------------------------------------------------- #
# 3. The benchmark stops drifting as a consequence
# --------------------------------------------------------------------------- #
class TestBenchmarkStability:
    def test_five_consecutive_bench_runs_agree(self, corpus_ready) -> None:
        """The symptom that started this cycle: five identical runs previously
        spread 3 queries on hit@5 and 0.0174 on MRR@5."""
        seen: list[tuple[str, ...]] = []
        for _ in range(5):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "benchmarks/bench_targets.py",
                 "-q", "-s", "-k", "hit_rate or mrr5 or ndcg"],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=900,
            )
            metrics = _extract_bench_metrics(proc.stdout)
            assert metrics, (
                "no metrics parsed from bench output; the output format changed and "
                f"this test would otherwise compare empty tuples:\n{proc.stdout[-600:]}"
            )
            seen.append(metrics)
        assert len(set(seen)) == 1, (
            "bench metrics differ across identical runs:\n"
            + "\n".join(str(s) for s in sorted(set(seen)))
        )


class TestMetricExtraction:
    """The stability test is only as good as its parser, so pin the parser."""

    _SAMPLE = (
        "MRR@5 (ambiguous, n=47): 0.6082 (floor: 0.45)\n"
        "Hit rate (all 193 queries): 156/193 = 80.83% (floor: 75%)\n"
        "Hit rate (index-eligible, n=169): 156/169 = 92.31% (floor: 90%)\n"
        "nDCG@10 (all 193 queries): 0.7323 (floor: 0.65)\n"
    )

    def test_extracts_all_four_metric_values(self) -> None:
        assert _extract_bench_metrics(self._SAMPLE) == (
            "0.6082", "156/193 = 80.83%", "156/169 = 92.31%", "0.7323",
        )

    def test_detects_a_changed_mrr(self) -> None:
        """The exact blind spot the review found: the old pattern captured the
        constant label prefix, so an MRR change was invisible."""
        changed = self._SAMPLE.replace("0.6082", "0.9999")
        assert _extract_bench_metrics(changed) != _extract_bench_metrics(self._SAMPLE)

    def test_detects_a_changed_ndcg(self) -> None:
        changed = self._SAMPLE.replace("0.7323", "0.1111")
        assert _extract_bench_metrics(changed) != _extract_bench_metrics(self._SAMPLE)

    def test_detects_a_changed_hit_rate(self) -> None:
        changed = self._SAMPLE.replace("156/193 = 80.83%", "159/193 = 82.38%")
        assert _extract_bench_metrics(changed) != _extract_bench_metrics(self._SAMPLE)

    def test_missing_metric_yields_empty_so_the_caller_fails_loudly(self) -> None:
        assert _extract_bench_metrics("nothing useful here") == ()
