"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (2c): `coverage-rollup` computes
`examined` from recorded evidence (the lead's own cache), not from the counts workers
claim on stdin.

RED at HEAD: `cmd_coverage_rollup` (writ/session/investigations.py) sums stdin claims
straight into `global_examined_in_scope` and never reads the lead's evidence at all
(the 950/950 defect `tests/test_inv6a_fanout.py::TestCoverageRollup.test_sums_and_
reconciles` pins). Every assertion below that reads a NEW key (`global_claimed_
examined_in_scope`, `unbacked_claims`, `unbacked_partitions`, `tiling_held`, a
per-map `partitions` list with `evidenced_examined_in_scope`) is red until the
production change lands; assertions on `reconciled`/`ready` are red where the claim
and the evidence diverge.

NOT bible-gated (unlike tests/test_inv6a_fanout.py, which loads `bible/` and skips on
a clean checkout): `writ-session.py` is loaded the same way that file does, but nothing
here reads bible/, so this module always runs.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HELPER_PATH = REPO / "bin" / "lib" / "writ-session.py"

_spec = importlib.util.spec_from_file_location("writ_session_cov_evidence", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SID = "test-coverage-rollup-evidence"


def _seed(tmp_path, monkeypatch, *, pretool_queried_files=None, citation_log=None) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump({
            "session_id": SID, "mode": "investigate", "current_phase": None,
            "citation_log": list(citation_log or []),
            "pretool_queried_files": list(pretool_queried_files or []),
            "coverage_scope": None,
        }, f)


def _freeze(files, **extra) -> None:
    payload = {"files": files}
    payload.update(extra)
    writ_session.cmd_update(SID, ["--freeze-scope", json.dumps(payload)])


def _capture(capsys, fn, *args, **kwargs) -> dict:
    capsys.readouterr()
    fn(*args, **kwargs)
    out, _ = capsys.readouterr()
    return json.loads(out)


def _rollup(capsys, monkeypatch, maps) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(maps)))
    return _capture(capsys, writ_session.cmd_coverage_rollup, SID)


def _map(scope_total, examined_in_scope, **extra) -> dict:
    row = {"status": "coverage_map", "scope_total": scope_total,
           "examined_in_scope": examined_in_scope,
           "coverage_pct": round(examined_in_scope / scope_total * 100) if scope_total else 0}
    row.update(extra)
    return row


class TestUnbackedClaimsAreReportedNotTrusted:
    """No recorded reads in the lead's cache: every claimed file is unbacked."""

    def test_no_evidence_reports_zero_evidenced_and_full_unbacked(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch)
        _freeze(["a", "b", "c", "d", "e"])
        maps = [_map(3, 2), _map(2, 2)]  # claim sums to 4
        report = _rollup(capsys, monkeypatch, maps)
        assert report["global_examined_in_scope"] == 0, report
        assert report["global_claimed_examined_in_scope"] == 4, report
        assert report["unbacked_claims"] == 4, report
        assert report["reconciled"] is False
        assert report["ready"] is False


class TestBackedClaimsReconcile:
    def test_matching_claim_and_evidence_reconciles(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a", "b", "c", "d"])
        _freeze(["a", "b", "c", "d", "e"])
        maps = [_map(3, 3), _map(2, 1)]  # claim sums to 4, matches evidence
        report = _rollup(capsys, monkeypatch, maps)
        assert report["global_claimed_examined_in_scope"] == 4
        assert report["global_examined_in_scope"] == 4
        assert report["unbacked_claims"] == 0
        assert report["reconciled"] is True
        assert report["global_coverage_pct"] == 80, report


class TestPerPartitionEvidenceCatchesLocalUnbackedClaims:
    def test_a_partition_over_claiming_its_own_files_is_flagged(self, tmp_path, capsys, monkeypatch) -> None:
        # Lead evidence: a, b, d, e (c and the fifth scope file are not evidenced).
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a", "b", "d", "e"])
        _freeze(["a", "b", "c", "d", "e"])
        maps = [
            _map(3, 3, files=["a", "b", "c"]),   # claims 3, only a,b evidenced -> unbacked
            _map(2, 1, files=["d", "e"]),        # claims 1, both evidenced -> under-claims, fine
        ]
        report = _rollup(capsys, monkeypatch, maps)
        # Global totals match (3 + 1 == 4 == |S evidenced| == 4) yet partition 0 is unbacked.
        assert report["global_claimed_examined_in_scope"] == 4
        assert report["global_examined_in_scope"] == 4
        assert report["unbacked_claims"] == 0
        assert report["unbacked_partitions"] == [0], report
        assert report["reconciled"] is False, (
            "a per-partition unbacked claim must refuse reconciliation even when "
            "the global totals happen to match"
        )
        partitions = report["partitions"]
        assert partitions[0]["evidenced_examined_in_scope"] == 2
        assert partitions[0]["claimed_examined_in_scope"] == 3
        assert partitions[1]["evidenced_examined_in_scope"] == 2
        assert partitions[1]["claimed_examined_in_scope"] == 1

    def test_a_map_with_no_files_key_gets_a_null_evidenced_count(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a"])
        _freeze(["a", "b"])
        maps = [_map(2, 1)]  # no "files" key: cannot be checked per partition
        report = _rollup(capsys, monkeypatch, maps)
        assert report["partitions"][0]["evidenced_examined_in_scope"] is None
        assert report["worst_partition_pct"] is None, (
            "worst_partition_pct is only computed over maps that carry files"
        )


class TestOutOfScopeAndCitationLogAndUnderClaiming:
    def test_reads_outside_frozen_scope_are_not_counted(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a", "b", "outside-the-scope.py"])
        _freeze(["a", "b"])
        maps = [_map(2, 2)]
        report = _rollup(capsys, monkeypatch, maps)
        assert report["global_examined_in_scope"] == 2

    def test_citation_log_file_rows_are_counted_as_evidence(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, citation_log=[
            {"artifact_type": "file", "ref": "a"}, {"artifact_type": "file", "ref": "b"},
        ])
        _freeze(["a", "b"])
        maps = [_map(2, 2)]
        report = _rollup(capsys, monkeypatch, maps)
        assert report["global_examined_in_scope"] == 2

    def test_underclaiming_still_reconciles(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a", "b", "c"])
        _freeze(["a", "b", "c"])
        maps = [_map(3, 1)]  # claims 1, evidence covers all 3 -- honest under-claim
        report = _rollup(capsys, monkeypatch, maps)
        assert report["global_claimed_examined_in_scope"] == 1
        assert report["global_examined_in_scope"] == 3
        assert report["unbacked_claims"] == 0
        assert report["reconciled"] is True


class TestRealFanoutSubprocessAgreesWithSynthesisGate:
    """A real subprocess rollup-subagent, then coverage-rollup, then synthesis-gate,
    for the SAME lead: the plan's headline property (950 vs 0 closed by construction)."""

    def _run(self, cache_dir: Path, *args, stdin: str | None = None) -> str:
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(cache_dir)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), *args],
            input=stdin, env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_rollup_subagent_then_coverage_rollup_matches_synthesis_gate(self, tmp_path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        lead = "cov-fanout-lead"
        child = "cov-fanout-child"
        (cache_dir / f"writ-session-{lead}.json").write_text(json.dumps({
            "session_id": lead, "mode": "investigate",
            "coverage_scope": {"files": ["a.py", "b.py", "c.py"], "frozen_at": "now"},
            "pretool_queried_files": [], "citation_log": [],
        }))
        (cache_dir / f"writ-session-{child}.json").write_text(json.dumps({
            "session_id": child, "parent_session_id": lead,
            "pretool_queried_files": ["a.py", "b.py"], "citation_log": [],
        }))
        self._run(cache_dir, "rollup-subagent", child, lead)
        rollup = json.loads(self._run(
            cache_dir, "coverage-rollup", lead,
            stdin=json.dumps([_map(3, 2, files=["a.py", "b.py", "c.py"])]),
        ))
        gate = json.loads(self._run(cache_dir, "synthesis-gate", lead))
        assert rollup["global_examined_in_scope"] == gate["examined_in_scope"] == 2


class TestMalformedInputAndNoFrozenScopeAndTilingMismatch:
    def test_malformed_stdin_gives_zero_partitions_but_still_computes_evidenced(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a"])
        _freeze(["a", "b"])
        monkeypatch.setattr("sys.stdin", io.StringIO("not json {{{"))
        report = _capture(capsys, writ_session.cmd_coverage_rollup, SID)
        assert report["partitions_reported"] == 0
        assert report["global_examined_in_scope"] == 1

    def test_no_frozen_scope_gives_null_lead_total_and_not_reconciled_not_ready(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a"])
        report = _rollup(capsys, monkeypatch, [_map(2, 2)])
        assert report["lead_scope_total"] is None
        assert report["reconciled"] is False
        assert report["ready"] is False

    def test_a_tiling_mismatch_is_still_not_reconciled(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, pretool_queried_files=["a", "b", "c", "d"])
        _freeze(["a", "b", "c", "d", "e"])  # lead total 5
        maps = [_map(2, 2), _map(2, 2)]     # sums to 4 != 5
        report = _rollup(capsys, monkeypatch, maps)
        assert report["tiling_held"] is False
        assert report["reconciled"] is False
