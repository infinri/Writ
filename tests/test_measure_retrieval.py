"""scripts/measure_retrieval.py: the pure halves, intervals and the comparison record.

The measuring half needs the isolated graph and is run by hand (plan tasks 0 and 4);
these tests pin what turns its numbers into a decision.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_retrieval.py"


@pytest.fixture()
def m():
    spec = importlib.util.spec_from_file_location("measure_retrieval_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BOOTSTRAP_RESAMPLES = 200
    return mod


def _record(m, per_query, rate=0.0, limit=10):
    arm = {"threshold": 0.0, "per_query": per_query, "summary": m.summarize(per_query),
           "false_injection_rate": rate}
    return {"vector_candidate_limit": limit,
            "arms": {"ungated": arm, "gated": dict(arm, threshold=0.30)}}


class TestBootstrapInterval:
    def test_deterministic_and_brackets_the_mean(self, m):
        values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
        lo, hi = m.bootstrap_ci(values)
        assert (lo, hi) == m.bootstrap_ci(values)
        assert 0.0 <= lo <= sum(values) / len(values) <= hi <= 1.0

    def test_constant_values_have_zero_width(self, m):
        assert m.bootstrap_ci([0.5] * 20) == (0.5, 0.5)

    def test_empty_values_are_zero(self, m):
        assert m.bootstrap_ci([]) == (0.0, 0.0)


class TestComparisonRecord:
    def test_identical_runs_are_no_decision_everywhere(self, m):
        values = {name: [1.0, 0.0] * 50 for name in m.METRICS}
        doc = m.render_comparison(_record(m, values), _record(m, values, limit=50))
        assert doc.count("| no decision |") == len(m.METRICS) * len(m.ARMS)
        assert "| improved |" not in doc and "| regressed |" not in doc
        assert "before 10, after 50" in doc

    def test_disjoint_intervals_are_called_in_the_right_direction(self, m):
        low = {name: [0.0] * 100 for name in m.METRICS}
        high = {name: [1.0] * 100 for name in m.METRICS}
        assert "| improved |" in m.render_comparison(_record(m, low), _record(m, high))
        assert "| regressed |" in m.render_comparison(_record(m, high), _record(m, low))


class TestTarget:
    def test_measures_only_the_isolated_graph(self):
        src = SCRIPT.read_text()
        assert "ISOLATED_NEO4J_URI" in src
        assert "get_neo4j_uri" not in src
