"""BM25 index persistence: warm cold-starts skip the rebuild, like HNSW.

The helper under test is writ.retrieval.pipeline._load_or_build_keyword_index:
hash-keyed sidecar, open-on-hit, rebuild-into-fresh-dir on miss (tantivy
appends, so a dirty dir would duplicate documents), in-memory fallback when
persistence is unavailable. No Neo4j needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from writ.retrieval.pipeline import (
    _BM25_SIDECAR,
    _compute_bm25_hash,
    _load_or_build_keyword_index,
)


def _candidates(statement: str = "Parameterized queries only.") -> list[dict]:
    return [
        {"rule_id": "T-BM25-001", "trigger": "When writing SQL strings.",
         "statement": statement, "tags": "security sql", "mandatory": False},
        {"rule_id": "T-BM25-002", "trigger": "When catching exceptions.",
         "statement": "Errors propagate with context.", "tags": "errors",
         "mandatory": False},
        {"rule_id": "T-BM25-MAND", "trigger": "Always.", "statement": "Mandatory.",
         "tags": "", "mandatory": True},
    ]


class TestLoadOrBuildKeywordIndex:
    def test_first_build_is_a_miss_and_writes_the_sidecar(self, tmp_path: Path) -> None:
        index, outcome = _load_or_build_keyword_index(_candidates(), tmp_path)
        assert outcome == "miss"
        sidecar = tmp_path / "bm25" / _BM25_SIDECAR
        assert sidecar.is_file()
        assert json.loads(sidecar.read_text())["corpus_hash"] == _compute_bm25_hash(_candidates())
        assert any(r["rule_id"] == "T-BM25-001" for r in index.search("parameterized queries", limit=5))

    def test_second_call_hits_the_cache_and_serves_the_same_results(self, tmp_path: Path) -> None:
        _load_or_build_keyword_index(_candidates(), tmp_path)
        index, outcome = _load_or_build_keyword_index(_candidates(), tmp_path)
        assert outcome == "hit"
        hits = [r["rule_id"] for r in index.search("parameterized queries", limit=5)]
        assert "T-BM25-001" in hits

    def test_cache_hit_does_not_duplicate_documents(self, tmp_path: Path) -> None:
        _load_or_build_keyword_index(_candidates(), tmp_path)
        index, outcome = _load_or_build_keyword_index(_candidates(), tmp_path)
        assert outcome == "hit"
        hits = [r["rule_id"] for r in index.search("parameterized", limit=10)]
        assert hits.count("T-BM25-001") == 1, (
            f"a cache hit must not re-add documents; got {hits!r}"
        )

    def test_changed_statement_misses_and_serves_fresh_content(self, tmp_path: Path) -> None:
        _load_or_build_keyword_index(_candidates(), tmp_path)
        changed = _candidates(statement="Bind parameters through named placeholders.")
        index, outcome = _load_or_build_keyword_index(changed, tmp_path)
        assert outcome == "miss"
        assert any(r["rule_id"] == "T-BM25-001" for r in index.search("named placeholders", limit=5))
        assert not any(
            r["rule_id"] == "T-BM25-001"
            for r in index.search("parameterized queries", limit=5)
        ), "the rebuilt index must not serve the pre-change statement"

    def test_unwritable_cache_root_degrades_to_in_memory_build(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        root = tmp_path / "sealed"
        root.mkdir()
        root.chmod(0o500)
        try:
            index, outcome = _load_or_build_keyword_index(_candidates(), root)
        finally:
            root.chmod(0o700)
        assert outcome == "nocache"
        assert any(r["rule_id"] == "T-BM25-001" for r in index.search("parameterized queries", limit=5))


class TestBm25HashCoversWhatBm25Indexes:
    def test_tags_change_flips_the_hash(self) -> None:
        a = _candidates()
        b = _candidates()
        b[0]["tags"] = "security sql injection placeholder"
        assert _compute_bm25_hash(a) != _compute_bm25_hash(b)

    def test_mandatory_flip_changes_the_hash(self) -> None:
        a = _candidates()
        b = _candidates()
        b[0]["mandatory"] = True
        assert _compute_bm25_hash(a) != _compute_bm25_hash(b)

    def test_candidate_order_does_not_change_the_hash(self) -> None:
        a = _candidates()
        assert _compute_bm25_hash(a) == _compute_bm25_hash(list(reversed(a)))
