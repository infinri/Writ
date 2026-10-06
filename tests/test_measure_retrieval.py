"""scripts/measure_retrieval.py: the pure halves.

The measuring half needs the isolated graph and is run by hand (plan steps 7 and 10). These
tests pin what turns its numbers into a decision: the intervals, the delivery record, the
replay the gated arm and the threshold sweep are scored from, the digest, and the two
markdown records. Channel membership and the graph reads are the shared helpers', pinned in
tests/test_retrieval_scoring.py and exercised on the real graph by the benchmark.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from writ.retrieval.embeddings import ScoredResult
from writ.retrieval.pipeline import RetrievalPipeline, _compute_corpus_hash_from_text
from writ.retrieval.traversal import AdjacencyCache

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_retrieval.py"


@pytest.fixture()
def m():
    spec = importlib.util.spec_from_file_location("measure_retrieval_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BOOTSTRAP_RESAMPLES = 200
    return mod


def _gold(qid, expected, top, signal, qset="ambiguous"):
    return {"id": qid, "set": qset, "query": f"query {qid}", "expected_rule_id": expected,
            "top": top, "signal": signal}


def _neg(nid, signal, flavor="A"):
    return {"id": nid, "flavor": flavor, "query": f"negative {nid}", "top": ["X-001"],
            "signal": signal}


RECORDED = {
    "gold": [
        _gold("G1", "R-001", ["R-001", "X-001"], 0.50),
        _gold("G2", "R-002", ["X-001", "R-002"], 0.28),
        _gold("G3", "R-003", ["X-001"], 0.40, qset="direct"),
        _gold("G4", "M-001", ["X-001"], 0.60),
        _gold("G5", "P-001", [], 0.30),
    ],
    "negatives": [_neg("N1", 0.10), _neg("N2", 0.40, flavor="B")],
}
CHANNELS = {"eligible": ["G1", "G2", "G3"], "always_on": ["G4"], "routed": ["G5"]}
DELIVERY = {"always_on": {"queries": 1, "rules": 1, "undelivered": []},
            "routed": {"queries": 1, "rules": 1, "orphans": [], "channelless": []}}


def _record(m, recorded=RECORDED, channels=CHANNELS, limit=50, digest="a" * 64):
    return m.build_record(recorded, channels, DELIVERY, {"ungated": 0.0, "gated": 0.30},
                          limit, digest)


def _uniform(m, hit, n=40):
    gold = [_gold(f"U{i}", "R-001", ["R-001"] if hit else ["X-001"], 0.9) for i in range(n)]
    recorded = {"gold": gold, "negatives": [_neg("N1", 0.1)]}
    channels = {"eligible": [g["id"] for g in gold], "always_on": [], "routed": []}
    return _record(m, recorded, channels)


def _live_pipeline(score):
    keyword = MagicMock()
    keyword.search.return_value = []
    encoder = MagicMock()
    encoder.encode.return_value = np.zeros(384, dtype=np.float32)
    store = MagicMock()
    store.search.return_value = [ScoredResult(rule_id="SEC-A-001", score=score)]
    meta = {"node_type": "Rule", "routes": ["semantic"], "domain": "security",
            "severity": "high", "confidence": "production-validated",
            "statement": "s.", "trigger": "t.", "project": "writ"}
    return RetrievalPipeline(
        keyword_index=keyword, vector_store=store, adjacency_cache=AdjacencyCache(),
        embedding_model=encoder, rule_metadata={"SEC-A-001": meta},
    )


class TestIntervals:
    def test_bootstrap_is_deterministic_and_brackets_the_mean(self, m):
        values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
        lo, hi = m.bootstrap_ci(values)
        assert (lo, hi) == m.bootstrap_ci(values)
        assert 0.0 <= lo <= sum(values) / len(values) <= hi <= 1.0

    def test_bootstrap_of_constant_values_has_zero_width(self, m):
        assert m.bootstrap_ci([0.5] * 20) == (0.5, 0.5)

    def test_bootstrap_of_nothing_is_zero(self, m):
        assert m.bootstrap_ci([]) == (0.0, 0.0)

    @pytest.mark.parametrize("k,lo,hi", [(4, 0.0807, 0.4160), (8, 0.2188, 0.6134)])
    def test_wilson_matches_the_recorded_sweep_intervals(self, m, k, lo, hi):
        got = m.wilson_interval(k, 20)
        assert got[0] == pytest.approx(lo, abs=1e-3)
        assert got[1] == pytest.approx(hi, abs=1e-3)

    def test_wilson_edges(self, m):
        assert m.wilson_interval(0, 20)[0] == pytest.approx(0.0, abs=1e-12)
        assert m.wilson_interval(20, 20)[1] == pytest.approx(1.0, abs=1e-12)
        assert m.wilson_interval(0, 0) == (0.0, 0.0)

    def test_paired_bootstrap_of_identical_runs_is_zero(self, m):
        values = [1.0, 0.0, 0.5] * 10
        assert m.paired_bootstrap_ci(values, list(values)) == (0.0, 0.0)

    def test_paired_bootstrap_of_a_constant_shift_is_the_shift(self, m):
        before = [0.0, 0.25, 0.5] * 10
        lo, hi = m.paired_bootstrap_ci([v + 0.5 for v in before], before)
        assert lo == pytest.approx(0.5)
        assert hi == pytest.approx(0.5)

    def test_paired_bootstrap_refuses_unpaired_lists(self, m):
        with pytest.raises(ValueError):
            m.paired_bootstrap_ci([1.0, 0.0], [1.0])


class TestDeliveryRecord:
    def test_stranded_always_on_targets_and_routed_findings_are_reported(self, m):
        always_on = [{"id": "Q1", "expected_rule_id": "M-001"},
                     {"id": "Q2", "expected_rule_id": "M-002"},
                     {"id": "Q3", "expected_rule_id": "M-001"}]
        routed = [{"id": "Q4", "expected_rule_id": "P-001"}]
        got = m.delivery_record(always_on, routed, stranded={"M-002", "M-999"},
                                routed_findings={"orphans": ["P-001"], "channelless": []})
        assert got["always_on"] == {"queries": 3, "rules": 2, "undelivered": ["M-002"]}
        assert got["routed"] == {"queries": 1, "rules": 1, "orphans": ["P-001"],
                                 "channelless": []}

    def test_clean_when_everything_arrives(self, m):
        got = m.delivery_record([{"id": "Q1", "expected_rule_id": "M-001"}], [], stranded=set(),
                                routed_findings={"orphans": [], "channelless": []})
        assert got["always_on"]["undelivered"] == []
        assert got["routed"] == {"queries": 0, "rules": 0, "orphans": [], "channelless": []}


class TestReplay:
    def test_serves_the_recorded_result_at_or_above_the_threshold(self, m):
        recorded = {"gold": [_gold("E", "R-001", ["R-001"], 0.30)], "negatives": []}
        replay = m.ReplayPipeline(recorded, 0.30)
        assert replay.query("query E")["rules"] == [{"rule_id": "R-001"}]

    def test_abstains_below_the_threshold(self, m):
        replay = m.ReplayPipeline(RECORDED, 0.30)
        assert replay.query("query G2")["rules"] == []

    def test_threshold_zero_never_abstains(self, m):
        recorded = {"gold": [_gold("E", "R-001", ["R-001"], 0.0)], "negatives": []}
        assert m.ReplayPipeline(recorded, 0.0).query("query E")["rules"] == [{"rule_id": "R-001"}]

    @pytest.mark.parametrize("score", [0.60, 0.20])
    def test_replay_equals_the_live_gate(self, m, score):
        pipe = _live_pipeline(score)
        recorded = m.record_run(
            pipe,
            [{"id": "Q1", "set": "ambiguous", "query": "q", "expected_rule_id": "SEC-A-001"}],
            [{"id": "N1", "flavor": "A", "query": "n"}],
        )
        assert recorded["gold"][0]["signal"] == pytest.approx(score)
        assert m.check_replay(pipe, recorded, 0.30) == []
        assert pipe._abstention_threshold == 0.0

    def test_a_disagreeing_pipeline_is_named(self, m):
        class _Disagrees:
            _abstention_threshold = 0.0

            def query(self, text):
                return {"rules": [{"rule_id": "OTHER-001"}], "abstain_signal": 0.9}

        recorded = {"gold": [_gold("E", "R-001", ["R-001"], 0.9)], "negatives": []}
        pipe = _Disagrees()
        assert m.check_replay(pipe, recorded, 0.30) == ["E"]
        assert pipe._abstention_threshold == 0.0


class TestScoredRecord:
    def test_eligible_pool_excludes_the_other_channels(self, m):
        pools = _record(m)["arms"]["ungated"]["pools"]
        assert pools["eligible"]["ids"]["hit@5"] == ["G1", "G2", "G3"]
        assert pools["all"]["ids"]["hit@5"] == ["G1", "G2", "G3", "G4", "G5"]
        assert pools["eligible"]["per_query"]["hit@5"] == [1.0, 1.0, 0.0]
        assert pools["eligible"]["ids"]["mrr@5_ambiguous"] == ["G1", "G2"]
        assert pools["eligible"]["per_query"]["mrr@5_ambiguous"] == [1.0, 0.5]

    def test_gated_arm_drops_queries_below_the_threshold(self, m):
        pool = _record(m)["arms"]["gated"]["pools"]["eligible"]
        assert pool["per_query"]["hit@5"] == [1.0, 0.0, 0.0]
        assert pool["per_query"]["mrr@5_ambiguous"] == [1.0, 0.0]

    def test_false_injection_counts_negatives_that_clear_the_gate(self, m):
        arms = _record(m)["arms"]
        assert arms["ungated"]["false_injection"]["injected"] == 2
        gated = arms["gated"]["false_injection"]
        assert (gated["injected"], gated["n"], gated["rate"]) == (1, 2, 0.5)
        assert gated["ci95"] == list(m.wilson_interval(1, 2))

    def test_record_carries_the_definition_and_the_digest(self, m):
        record = _record(m)
        assert record["definition"] == m.DEFINITION_NOTE
        assert "Definitional change" in m.DEFINITION_NOTE
        assert record["corpus_digest"] == "a" * 64


class TestCorpusDigest:
    def test_is_the_pipeline_corpus_hash_over_trigger_and_statement(self, m):
        metadata = {"R-002": {"trigger": "t2", "statement": "s2"},
                    "R-001": {"trigger": "t", "statement": "s"}}
        assert m.corpus_digest(metadata) == _compute_corpus_hash_from_text(
            ["R-002", "R-001"], ["t2 s2", "t s"],
        )

    def test_moves_with_the_rule_text(self, m):
        a = m.corpus_digest({"R-001": {"trigger": "t", "statement": "s"}})
        assert a != m.corpus_digest({"R-001": {"trigger": "t2", "statement": "s"}})


class TestComparisonRecord:
    def test_identical_runs_are_no_decision_everywhere(self, m):
        doc = m.render_comparison(_record(m, limit=10), _record(m, limit=50))
        assert doc.count("| no decision |") == len(m.METRICS) * len(m.ARMS) * len(m.POOLS)
        assert "| improved |" not in doc and "| regressed |" not in doc
        assert "before 10, after 50" in doc
        assert "Definitional change" in doc
        assert "[0.0000, 0.0000]" in doc
        assert doc.rstrip().endswith("None.")

    def test_disjoint_intervals_are_called_in_the_right_direction(self, m):
        low, high = _uniform(m, hit=False), _uniform(m, hit=True)
        assert "| improved |" in m.render_comparison(low, high)
        assert "| regressed |" in m.render_comparison(high, low)

    def test_runs_over_different_queries_are_refused(self, m):
        with pytest.raises(ValueError):
            m.render_comparison(_uniform(m, hit=True, n=40), _uniform(m, hit=True, n=39))

    def test_changed_queries_are_listed(self, m):
        moved = json.loads(json.dumps(RECORDED))
        moved["gold"][0]["top"] = ["X-001", "R-001"]
        doc = m.render_comparison(_record(m), _record(m, recorded=moved))
        assert "| ungated | G1 | mrr@5_ambiguous | 1.0000 | 0.5000 |" in doc


class TestSweep:
    def test_one_row_per_threshold_per_pool(self, m):
        doc = m.render_sweep(_record(m), (0.0, 0.30))
        assert doc.count("| 0.00 |") == len(m.POOLS)
        assert doc.count("| 0.30 |") == len(m.POOLS)
        assert "Definitional change" in doc

    def test_rows_name_lost_queries_and_false_injection(self, m):
        rows = [line for line in m.render_sweep(_record(m), (0.0, 0.30)).splitlines()
                if line.startswith("| 0.")]
        assert all(" 2/2 " in line for line in rows if line.startswith("| 0.00 |"))
        assert all(" 1/2 " in line and "G2" in line for line in rows if line.startswith("| 0.30 |"))

    def test_signals_section(self, m):
        doc = m.render_sweep(_record(m), (0.0,))
        assert doc.index("N2 (B) 0.400") < doc.index("N1 (A) 0.100")
        assert "G2 0.280" in doc

    def test_docstring_names_the_superseded_diagnostic(self, m):
        assert "scripts/measure_false_injection.py" in m.sweep.__doc__


class TestCli:
    def test_unknown_arguments_print_usage_and_return_2(self, m):
        assert m.main(["measure_retrieval.py"]) == 2

    def test_sweep_writes_the_document_for_the_given_thresholds(self, m, tmp_path):
        run, doc = tmp_path / "run.json", tmp_path / "sweep.md"
        run.write_text(json.dumps(_record(m)))
        assert m.main(["measure_retrieval.py", "sweep", str(run), str(doc), "0.3"]) == 0
        text = doc.read_text()
        assert "| 0.30 |" in text and "| 0.00 |" not in text


class TestTarget:
    def test_measures_only_the_isolated_graph(self):
        src = SCRIPT.read_text()
        assert "ISOLATED_NEO4J_URI" in src
        assert "get_neo4j_uri" not in src

    def test_reuses_the_shared_helpers_instead_of_copies(self):
        src = SCRIPT.read_text()
        for name in ("split_by_channel", "routed_target_findings", "mandatory_rule_ids",
                     "detect_stranded_mandatory", "_compute_corpus_hash_from_text"):
            assert name in src, name
        assert "def split_by_channel" not in src
        assert "BELONGS_TO" not in src
        assert "mandatory = true" not in src and "mandatory: true" not in src
