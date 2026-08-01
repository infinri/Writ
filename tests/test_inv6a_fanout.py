"""INV-6a: self-sizing hierarchical audit fan-out -- the deterministic brain + doctrine.

Writ provides the math the lead sub-agent needs to fan out smartly (size estimate,
scope partition to a context budget, coverage roll-up) plus the PBK-PROC-AUDIT-FANOUT-001
doctrine. The agent does the spawning via the Agent tool; workers inherit hooks/RAG via
the existing writ-subagent-start. These tests pin the deterministic commands + the node.

Loads writ-session.py as a module (mirrors tests/test_inv4_coverage_map.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
from pathlib import Path

import pytest

from writ.graph.ingest import (


    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)

from tests._bible_guard import requires_bible

pytestmark = requires_bible


HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_inv6a", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SKILL_DIR = Path(__file__).resolve().parent.parent
METHODOLOGY = SKILL_DIR / "bible" / "methodology"
FANOUT = "PBK-PROC-AUDIT-FANOUT-001"
FANOUT_PATH = METHODOLOGY / f"{FANOUT}.md"
RESEARCH_PATH = METHODOLOGY / "PBK-PROC-RESEARCH-001.md"
SID = "test-inv6a-fanout"


def _seed(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump({
            "session_id": SID, "mode": "investigate", "current_phase": None,
            "citation_log": [], "pretool_queried_files": [], "coverage_scope": None,
        }, f)


def _mkfile(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("x\n" * lines)
    return str(p)


def _freeze(files, **extra):
    payload = {"files": files}
    payload.update(extra)
    writ_session.cmd_update(SID, ["--freeze-scope", json.dumps(payload)])


def _capture(capsys, fn, *args, **kwargs):
    capsys.readouterr()
    fn(*args, **kwargs)
    return json.loads(capsys.readouterr().out)


def _coverage_map(scope_total, examined_in_scope):
    return {
        "status": "coverage_map", "scope_total": scope_total,
        "examined_in_scope": examined_in_scope,
        "coverage_pct": round(examined_in_scope / scope_total * 100) if scope_total else 0,
    }


class TestScopeEstimate:
    def test_counts_and_recommends_workers(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        files = [_mkfile(tmp_path, f"f{i}.py", 10) for i in range(3)]  # 30 LOC total
        _freeze(files)
        report = _capture(capsys, writ_session.cmd_scope_estimate, SID, budget_loc=20)
        assert report["file_count"] == 3
        assert report["total_loc"] == 30
        assert report["recommended_workers"] == math.ceil(30 / 20)  # 2

    def test_no_scope_status(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        report = _capture(capsys, writ_session.cmd_scope_estimate, SID)
        assert report["status"] == "no_scope"


class TestPartitionScope:
    def test_partitions_tile_the_scope(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        files = [_mkfile(tmp_path, f"f{i}.py", 10) for i in range(5)]
        _freeze(files)
        report = _capture(capsys, writ_session.cmd_partition_scope, SID, max_loc=10_000, max_files=2)
        parts = report["partitions"]
        seen = [f for p in parts for f in p["files"]]
        assert sorted(seen) == sorted(files), "every scope file in exactly one partition"
        assert len(seen) == len(set(seen)), "no file appears in two partitions"
        assert all(p["count"] <= 2 for p in parts), "file-count budget respected"

    def test_loc_budget_respected(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        files = [_mkfile(tmp_path, f"f{i}.py", 10) for i in range(5)]  # 10 LOC each
        _freeze(files)
        report = _capture(capsys, writ_session.cmd_partition_scope, SID, max_loc=25, max_files=99)
        # 10+10=20<=25 fits two; a third (30) would exceed -> at most 2 per partition.
        assert all(p["loc"] <= 25 for p in report["partitions"])

    def test_deterministic(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        files = [_mkfile(tmp_path, f"f{i}.py", (i % 3) + 1) for i in range(6)]
        _freeze(files)
        r1 = _capture(capsys, writ_session.cmd_partition_scope, SID, max_loc=5, max_files=3)
        r2 = _capture(capsys, writ_session.cmd_partition_scope, SID, max_loc=5, max_files=3)
        assert r1["partitions"] == r2["partitions"]

    def test_oversized_file_gets_own_flagged_partition(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        big = _mkfile(tmp_path, "big.py", 100)
        small = _mkfile(tmp_path, "small.py", 5)
        _freeze([big, small])
        report = _capture(capsys, writ_session.cmd_partition_scope, SID, max_loc=50, max_files=99)
        oversized = [p for p in report["partitions"] if p.get("oversized")]
        assert oversized, "a file over the LOC budget must be flagged oversized"
        assert oversized[0]["files"] == [big]


class TestCoverageRollup:
    def test_sums_and_reconciles(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _freeze(["a", "b", "c", "d", "e"])  # lead scope total = 5
        maps = [_coverage_map(3, 2), _coverage_map(2, 2)]  # 5 total, 4 examined
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(maps)))
        report = _capture(capsys, writ_session.cmd_coverage_rollup, SID)
        assert report["global_scope_total"] == 5
        assert report["global_examined_in_scope"] == 4
        assert report["global_coverage_pct"] == 80
        assert report["partitions_reported"] == 2
        assert report["reconciled"] is True

    def test_not_reconciled_on_mismatch(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _freeze(["a", "b", "c", "d", "e"])  # lead total = 5
        maps = [_coverage_map(2, 1), _coverage_map(2, 2)]  # sums to 4 != 5
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(maps)))
        report = _capture(capsys, writ_session.cmd_coverage_rollup, SID)
        assert report["reconciled"] is False


class TestFanoutDoctrine:
    def test_node_is_valid_playbook(self) -> None:
        assert FANOUT_PATH.exists(), f"{FANOUT_PATH} does not exist yet"
        nodes = parse_nodes_from_file(FANOUT_PATH)
        assert len(nodes) == 1
        assert nodes[0].get("node_type") == "Playbook"
        assert nodes[0].get("playbook_id") == FANOUT
        validate_parsed_node(nodes[0])

    def test_research_invokes_fanout(self) -> None:
        # 1.3b: AUDIT-FANOUT is a Playbook (not a SubagentRole), so RESEARCH
        # INVOKES it (applies inline, one level) rather than DISPATCHES.
        assert RESEARCH_PATH.exists(), f"{RESEARCH_PATH} missing"
        edges = parse_edges_from_file(RESEARCH_PATH)
        invoke = {e["target"] for e in edges if e.get("type") == "INVOKES"}
        assert FANOUT in invoke, f"PBK-PROC-RESEARCH-001 must INVOKES {FANOUT}; got {sorted(invoke)}"

    def test_doctrine_names_the_commands(self) -> None:
        assert FANOUT_PATH.exists(), f"{FANOUT_PATH} does not exist yet"
        body = FANOUT_PATH.read_text(encoding="utf-8")
        for cmd in ("scope-estimate", "partition-scope", "coverage-rollup"):
            assert cmd in body, f"the fan-out doctrine must name `{cmd}`"
