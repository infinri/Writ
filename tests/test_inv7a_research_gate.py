"""INV-7a: research lens enforcement core -- url-citation hashing + the hard
triangulation gate (fail-closed) + excerpt-hash staleness drift.

Promotes RESEARCH-CORROBORATE-001 from advisory to enforced: a web synthesis is
blocked until >=2 INDEPENDENT source domains are captured. Deterministic, hash-based
staleness (no clock -> no time-bomb). url citations are the INV-2 citation_log rows.

Loads writ-session.py as a module (mirrors tests/test_inv4_coverage_map.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from tests._bible_guard import requires_bible

pytestmark = requires_bible


HELPER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
_spec = importlib.util.spec_from_file_location("writ_session_inv7a", HELPER_PATH)
writ_session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writ_session)

SKILL_DIR = Path(__file__).resolve().parent.parent
RESEARCH_RULES = SKILL_DIR / "bible" / "research" / "rules.md"
SID = "test-inv7a-research"


def _seed(monkeypatch, tmp_path):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    with open(writ_session._cache_path(SID), "w") as f:
        json.dump({"session_id": SID, "mode": "investigate", "citation_log": []}, f)


def _add_url(ref, excerpt="content"):
    writ_session.cmd_update(SID, ["--add-citation", json.dumps(
        {"artifact_type": "url", "ref": ref, "excerpt": excerpt})])


def _url_rows():
    return [r for r in writ_session._read_cache(SID).get("citation_log", [])
            if r.get("artifact_type") == "url"]


def _gate(capsys):
    capsys.readouterr()
    writ_session.cmd_triangulation_gate(SID)
    return json.loads(capsys.readouterr().out)


def _staleness(capsys):
    capsys.readouterr()
    writ_session.cmd_staleness_check(SID)
    return json.loads(capsys.readouterr().out)


class TestExcerptHash:
    def test_hash_stamped_on_citation(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x", "some content")
        rows = _url_rows()
        assert rows and rows[0].get("excerpt_hash"), "url citation must carry a non-empty excerpt_hash"

    def test_equal_excerpts_hash_equally_and_differ_otherwise(self, tmp_path, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x", "same text")
        _add_url("https://b.com/y", "same text")
        _add_url("https://c.com/z", "different text")
        rows = _url_rows()
        h = {r["ref"]: r["excerpt_hash"] for r in rows}
        assert h["https://a.com/x"] == h["https://b.com/y"]
        assert h["https://c.com/z"] != h["https://a.com/x"]


class TestTriangulationGate:
    def test_two_independent_domains_triangulated(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x")
        _add_url("https://b.org/y")
        gate = _gate(capsys)
        assert gate["status"] == "triangulation_gate"
        assert gate["triangulated"] is True
        assert gate["blocked"] is False
        assert len(gate["independent_domains"]) == 2

    def test_same_site_collapses_and_blocks(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://docs.python.org/3/library")
        _add_url("https://www.python.org/downloads")
        gate = _gate(capsys)
        assert len(gate["independent_domains"]) == 1, "sub/www of one site is one independent domain"
        assert gate["blocked"] is True

    def test_fail_closed_with_no_sources(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        gate = _gate(capsys)
        assert gate["blocked"] is True
        assert gate["url_citations"] == 0

    def test_three_independent_triangulated(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        for d in ("a.com", "b.org", "c.net"):
            _add_url(f"https://{d}/p")
        gate = _gate(capsys)
        assert gate["triangulated"] is True
        assert len(gate["independent_domains"]) == 3


class TestStalenessCheck:
    def test_changed_excerpt_flags_drift(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x", "version one")
        _add_url("https://a.com/x", "version two")  # same ref, content changed
        report = _staleness(capsys)
        drifted_refs = [d["ref"] for d in report["drifted"]]
        assert "https://a.com/x" in drifted_refs

    def test_same_excerpt_no_drift(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x", "stable")
        _add_url("https://a.com/x", "stable")
        report = _staleness(capsys)
        assert report["drifted"] == []

    def test_distinct_refs_no_drift(self, tmp_path, capsys, monkeypatch) -> None:
        _seed(monkeypatch, tmp_path)
        _add_url("https://a.com/x", "one")
        _add_url("https://b.com/y", "two")
        report = _staleness(capsys)
        assert report["drifted"] == []


class TestPromotionText:
    def test_corroborate_enforcement_names_triangulation_gate(self) -> None:
        assert RESEARCH_RULES.exists(), f"{RESEARCH_RULES} missing"
        body = RESEARCH_RULES.read_text(encoding="utf-8")
        assert "triangulation-gate" in body, "RESEARCH-CORROBORATE-001 must name the triangulation-gate enforcement"

    def test_staleness_enforcement_names_staleness_check(self) -> None:
        assert RESEARCH_RULES.exists(), f"{RESEARCH_RULES} missing"
        body = RESEARCH_RULES.read_text(encoding="utf-8")
        assert "staleness-check" in body, "RESEARCH-STALENESS-001 must name the staleness-check enforcement"
