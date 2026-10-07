"""BM25 index persistence: warm cold-starts skip the rebuild, like HNSW.

The helper under test is writ.retrieval.pipeline._load_or_build_keyword_index:
hash-keyed sidecar, open-on-hit, rebuild-into-fresh-dir on miss (tantivy
appends, so a dirty dir would duplicate documents), in-memory fallback when
persistence is unavailable. No Neo4j needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from writ.retrieval.pipeline import (
    BM25_PRUNE_GRACE_SECONDS,
    _BM25_SIDECAR,
    _bm25_current_dir,
    _compute_bm25_hash,
    _load_or_build_keyword_index,
    _prune_bm25_generations,
)


# _compute_bm25_hash(_candidates()) as computed before program item 6 touched the key.
PRE_ITEM6_FIXTURE_HASH = "ad650a15a33d7d6569f9d7abfe3135f6b7c8b8069ded45d4567a16a90e9502e6"


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
        live = _bm25_current_dir(tmp_path / "bm25")
        assert live is not None, "no CURRENT generation after a build"
        sidecar = live / _BM25_SIDECAR
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

    def test_superseded_flip_changes_the_hash(self) -> None:
        # Program item 6: a superseded rule leaves the BM25 index, so the key moves
        # when a rule's index membership moves.
        a = _candidates()
        b = _candidates()
        b[0]["superseded"] = True
        assert _compute_bm25_hash(a) != _compute_bm25_hash(b)

    def test_corpus_with_no_superseded_rule_keeps_its_hash(self) -> None:
        # No forced rebuild on upgrade: an explicit superseded=False, or a missing
        # key, hashes exactly as the pre-change corpus did.
        legacy = _candidates()
        explicit = _candidates()
        for row in explicit:
            row["superseded"] = False
        assert _compute_bm25_hash(legacy) == _compute_bm25_hash(explicit)
        # Pinned digest of the exclusion-element-aware key for the fixture corpus:
        # computed from the unchanged implementation before item 6 landed.
        assert _compute_bm25_hash(legacy) == PRE_ITEM6_FIXTURE_HASH

    def test_superseded_mandatory_rule_hashes_like_a_mandatory_rule(self) -> None:
        # The exclusion element is bool(mandatory or superseded): a rule that is both
        # is excluded once, not differently from a mandatory-only rule.
        a = _candidates()
        b = _candidates()
        b[2]["superseded"] = True
        assert _compute_bm25_hash(a) == _compute_bm25_hash(b)

    def test_candidate_order_does_not_change_the_hash(self) -> None:
        a = _candidates()
        assert _compute_bm25_hash(a) == _compute_bm25_hash(list(reversed(a)))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class TestGenerationDirectories:
    """Program item 2: a rebuild never writes into the directory a reader has open."""

    def test_a_rebuild_never_touches_the_live_generation(self, tmp_path: Path) -> None:
        live_index, _ = _load_or_build_keyword_index(_candidates(), tmp_path)
        live_dir = _bm25_current_dir(tmp_path / "bm25")
        assert live_dir is not None
        before = _tree_digest(live_dir)

        changed = _candidates(statement="Bind parameters through named placeholders.")
        _new_index, outcome = _load_or_build_keyword_index(changed, tmp_path)

        assert outcome == "miss"
        assert live_dir.is_dir(), "the live generation directory was removed"
        assert _tree_digest(live_dir) == before, "the live generation's bytes changed"
        assert any(
            r["rule_id"] == "T-BM25-001"
            for r in live_index.search("parameterized queries", limit=5)
        ), "an index opened on the previous generation stopped answering"
        assert _bm25_current_dir(tmp_path / "bm25") != live_dir

    def test_legacy_flat_files_are_pruned_once_a_generation_is_current(self, tmp_path: Path) -> None:
        legacy = tmp_path / "bm25"
        legacy.mkdir()
        for name in (_BM25_SIDECAR, "meta.json", ".managed.json", "abc123.idx", "abc123.store"):
            (legacy / name).write_text("{}")
        unrelated = legacy / "notes.txt"
        unrelated.write_text("kept")

        _load_or_build_keyword_index(_candidates(), tmp_path)

        leftovers = sorted(p.name for p in legacy.iterdir() if p.is_file())
        assert leftovers == ["CURRENT", "notes.txt"], leftovers
        assert _bm25_current_dir(legacy) is not None

    def test_current_names_a_generation_and_leaves_no_staged_pointer(self, tmp_path: Path) -> None:
        _load_or_build_keyword_index(_candidates(), tmp_path)
        root = tmp_path / "bm25"
        name = (root / "CURRENT").read_text()
        assert name.startswith("gen-")
        assert (root / name / _BM25_SIDECAR).is_file()
        assert not list(root.glob(".CURRENT.*")), "a staged pointer file was left behind"

    def test_a_pointer_escaping_the_cache_root_reads_as_a_miss(self, tmp_path: Path) -> None:
        root = tmp_path / "bm25"
        root.mkdir()
        (root / "CURRENT").write_text("../../etc")
        _index, outcome = _load_or_build_keyword_index(_candidates(), tmp_path)
        assert outcome == "miss"
        assert _bm25_current_dir(root).parent == root


class TestGenerationPruning:
    @staticmethod
    def _gen(root: Path, name: str, age_seconds: float) -> Path:
        path = root / name
        path.mkdir(parents=True)
        (path / "segment").write_text("x")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_only_stale_superseded_generations_are_removed(self, tmp_path: Path) -> None:
        root = tmp_path / "bm25"
        stale = self._gen(root, "gen-stale-1", BM25_PRUNE_GRACE_SECONDS + 60)
        previous = self._gen(root, "gen-prev-1", BM25_PRUNE_GRACE_SECONDS + 60)
        live = self._gen(root, "gen-live-1", BM25_PRUNE_GRACE_SECONDS + 60)
        building = self._gen(root, "gen-building-1", 5)

        removed = _prune_bm25_generations(root, keep={live.name, previous.name})

        assert removed == [stale.name]
        assert previous.is_dir() and live.is_dir()
        assert building.is_dir(), "a concurrent builder's fresh directory was pruned"

    def test_directories_that_are_not_generations_are_never_pruned(self, tmp_path: Path) -> None:
        root = tmp_path / "bm25"
        other = root / "segments-dir"
        other.mkdir(parents=True)
        stamp = time.time() - BM25_PRUNE_GRACE_SECONDS - 60
        os.utime(other, (stamp, stamp))
        assert _prune_bm25_generations(root, keep=set()) == []
        assert other.is_dir()
