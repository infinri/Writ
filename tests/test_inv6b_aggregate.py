"""INV-6b: the lead's aggregation layer over worker reports.

aggregate-findings merges workers' structured findings (dedup), surfaces where
workers CONTRADICT each other (same subject, opposing stance), and ranks where
attention is owed -- coverage-aware (a barely-examined clean region ranks HIGH,
not low). Deterministic, stdin-driven; no agent.

Loads writ-session.py as a module (mirrors tests/test_inv6a_fanout.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

from tests._bible_guard import requires_bible

pytestmark = requires_bible


HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_inv6b", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SKILL_DIR = Path(__file__).resolve().parent.parent
FANOUT_PATH = SKILL_DIR / "bible" / "methodology" / "PBK-PROC-AUDIT-FANOUT-001.md"
SID = "test-inv6b-aggregate"


def _seed(monkeypatch, tmp_path):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump({"session_id": SID, "mode": "investigate"}, f)


def _finding(ref, rule="R1", severity="error", message="m", subject=None, stance=None):
    f = {"ref": ref, "rule": rule, "severity": severity, "message": message}
    if subject is not None:
        f["subject"] = subject
    if stance is not None:
        f["stance"] = stance
    return f


def _report(index, coverage_pct, findings):
    return {
        "partition_index": index,
        "coverage_map": {"scope_total": 1, "examined_in_scope": 1, "coverage_pct": coverage_pct},
        "findings": findings,
    }


def _aggregate(monkeypatch, capsys, reports):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(reports)))
    capsys.readouterr()
    writ_session.cmd_aggregate_findings(SID)
    return json.loads(capsys.readouterr().out)


class TestDedup:
    def test_duplicate_finding_counted_once(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        dup = _finding("a.py:10", "R1", "error", "boom")
        agg = _aggregate(monkeypatch, capsys, [_report(0, 100, [dup]), _report(1, 100, [dict(dup)])])
        assert agg["total_findings"] == 2
        assert agg["deduped_findings"] == 1
        assert agg["by_severity"].get("error") == 1
        assert agg["by_rule"].get("R1") == 1

    def test_distinct_findings_both_kept(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        agg = _aggregate(monkeypatch, capsys, [
            _report(0, 100, [_finding("a.py:10", "R1", "error", "x"),
                             _finding("b.py:20", "R2", "warning", "y")]),
        ])
        assert agg["deduped_findings"] == 2
        assert agg["by_severity"].get("error") == 1
        assert agg["by_severity"].get("warning") == 1


class TestContradiction:
    def test_opposing_stance_on_same_subject_is_flagged(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        agg = _aggregate(monkeypatch, capsys, [
            _report(0, 100, [_finding("a.py:10", subject="auth", stance="safe")]),
            _report(1, 100, [_finding("a.py:10", subject="auth", stance="unsafe")]),
        ])
        assert len(agg["contradictions"]) == 1
        c = agg["contradictions"][0]
        assert c["subject"] == "auth"
        assert set(c["stances"]) == {"safe", "unsafe"}

    def test_same_stance_is_not_a_contradiction(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        agg = _aggregate(monkeypatch, capsys, [
            _report(0, 100, [_finding("a.py:10", subject="auth", stance="safe")]),
            _report(1, 100, [_finding("b.py:20", subject="auth", stance="safe")]),
        ])
        assert agg["contradictions"] == []

    def test_stanceless_findings_never_contradict(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        agg = _aggregate(monkeypatch, capsys, [
            _report(0, 100, [_finding("a.py:10", "R1", "error", "x")]),
            _report(1, 100, [_finding("a.py:10", "R2", "error", "y")]),
        ])
        assert agg["contradictions"] == []


class TestCoverageAwareRanking:
    def test_low_coverage_high_error_ranks_first(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        clean = _report(0, 100, [])  # fully covered, no findings
        thin = _report(1, 40, [_finding("c.py:1", "R1", "error", "a"),
                               _finding("c.py:2", "R1", "error", "b")])  # barely covered, 2 errors
        agg = _aggregate(monkeypatch, capsys, [clean, thin])
        ranking = agg["attention_ranking"]
        assert ranking[0]["partition_index"] == 1, "low-coverage + high-error must rank first"
        assert ranking[0]["attention_score"] > ranking[1]["attention_score"]

    def test_ranking_is_deterministic(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        reports = [_report(0, 100, []), _report(1, 40, [_finding("c.py:1")]), _report(2, 70, [])]
        a1 = _aggregate(monkeypatch, capsys, reports)
        a2 = _aggregate(monkeypatch, capsys, reports)
        assert a1["attention_ranking"] == a2["attention_ranking"]


class TestEmptyAndDoctrine:
    def test_empty_input_is_well_formed(self, tmp_path, monkeypatch, capsys) -> None:
        _seed(monkeypatch, tmp_path)
        agg = _aggregate(monkeypatch, capsys, [])
        assert agg["workers"] == 0
        assert agg["total_findings"] == 0
        assert agg["contradictions"] == []
        assert agg["attention_ranking"] == []

    def test_doctrine_names_aggregate_findings(self) -> None:
        assert FANOUT_PATH.exists(), f"{FANOUT_PATH} missing"
        body = FANOUT_PATH.read_text(encoding="utf-8")
        assert "aggregate-findings" in body, "the fan-out doctrine must name the aggregate-findings step"
