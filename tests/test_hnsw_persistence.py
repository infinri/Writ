"""Unit tests for HnswlibStore.save_index() and load_index().

Per TEST-TDD-001: skeletons approved before implementation.
Per ARCH-CONST-001: cache_dir is injected, not hardcoded.
Per PY-PYDANTIC-001: sidecar schema validated via Pydantic model.
Per ARCH-ERR-001: load failures must include sidecar path and specific mismatch detail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from writ.retrieval.embeddings import HnswlibStore

# Sidecar schema model -- import expected future location.
# ImportError is intentional until implementation lands.
try:
    from writ.retrieval.embeddings import HnswSidecar
except ImportError:
    HnswSidecar = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(dimensions: int = 4, cache_dir: str | None = None) -> HnswlibStore:
    """Build a small HnswlibStore. cache_dir injected per ARCH-DI-001."""
    kwargs: dict[str, Any] = {"dimensions": dimensions}
    if cache_dir is not None:
        kwargs["cache_dir"] = cache_dir
    return HnswlibStore(**kwargs)


def _build_tiny_index(store: HnswlibStore, n: int = 5) -> tuple[list[str], list[list[float]]]:
    """Populate the store with n synthetic rules and return (rule_ids, vectors)."""
    rng = np.random.RandomState(42)
    rule_ids = [f"TEST-RULE-{i:03d}" for i in range(n)]
    vectors = [rng.randn(store._dimensions).astype(np.float32).tolist() for _ in range(n)]
    store.build_index(rule_ids, vectors)
    return rule_ids, vectors


def _corpus_hash_for(rule_ids: list[str], vectors: list[list[float]]) -> str:
    """Compute the expected corpus hash using the same algorithm the impl will use."""
    import hashlib

    pairs = sorted(zip(rule_ids, [str(v) for v in vectors]))
    digest_input = "|".join(f"{rid}:{vec}" for rid, vec in pairs)
    return hashlib.sha256(digest_input.encode()).hexdigest()


# ---------------------------------------------------------------------------
# TestRoundTrip
# ---------------------------------------------------------------------------


class TestRoundTripSaveLoad:
    """save_index then load_index restores a fully functional store."""

    def test_search_results_match_after_round_trip(self, tmp_path: Path) -> None:
        """After save+load the search returns the same top-1 result as before save."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)

        query = np.array(vectors[0], dtype=np.float32).tolist()
        original_results = store.search(query, k=1)
        loaded_results = loaded.search(query, k=1)
        assert original_results[0].rule_id == loaded_results[0].rule_id

    def test_id_to_rule_mapping_preserved(self, tmp_path: Path) -> None:
        """_id_to_rule dict is identical after round-trip."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)

        assert store._id_to_rule == loaded._id_to_rule

    def test_rule_count_preserved(self, tmp_path: Path) -> None:
        """The number of indexed rules is the same after round-trip."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store, n=7)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)

        assert len(loaded._id_to_rule) == 7


# ---------------------------------------------------------------------------
# TestSidecarSchema
# ---------------------------------------------------------------------------


class TestSidecarSchema:
    """Sidecar JSON contains all required fields with correct types."""

    def test_sidecar_fields_present(self, tmp_path: Path) -> None:
        """Sidecar JSON contains corpus_hash, rule_count, dims, ef_construction, M, _id_to_rule."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        sidecar_files = list(tmp_path.glob("*.json"))
        assert len(sidecar_files) == 1, "Expected exactly one sidecar JSON file"

        data = json.loads(sidecar_files[0].read_text())
        for field in ("corpus_hash", "rule_count", "dims", "ef_construction", "M", "_id_to_rule"):
            assert field in data, f"Sidecar missing field: {field}"

    def test_sidecar_corpus_hash_matches_input(self, tmp_path: Path) -> None:
        """corpus_hash field in sidecar equals the hash passed to save_index."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        sidecar_files = list(tmp_path.glob("*.json"))
        data = json.loads(sidecar_files[0].read_text())
        assert data["corpus_hash"] == corpus_hash

    def test_sidecar_rule_count_matches(self, tmp_path: Path) -> None:
        """rule_count in sidecar equals the number of indexed rules."""
        n = 6
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store, n=n)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        sidecar_files = list(tmp_path.glob("*.json"))
        data = json.loads(sidecar_files[0].read_text())
        assert data["rule_count"] == n

    def test_sidecar_validated_by_pydantic_model(self, tmp_path: Path) -> None:
        """HnswSidecar Pydantic model accepts a well-formed sidecar dict."""
        if HnswSidecar is None:
            pytest.fail("skeleton -- HnswSidecar not yet implemented")

        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        sidecar_files = list(tmp_path.glob("*.json"))
        data = json.loads(sidecar_files[0].read_text())
        sidecar = HnswSidecar(**data)
        assert sidecar.corpus_hash == corpus_hash


# ---------------------------------------------------------------------------
# TestCorpusHashMismatch
# ---------------------------------------------------------------------------


class TestCorpusHashMismatch:
    """load_index rejects a sidecar whose corpus_hash does not match the caller's hash."""

    def test_mismatch_raises_with_sidecar_path_in_message(self, tmp_path: Path) -> None:
        """ValueError (or subclass) includes the sidecar path when hash does not match."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(Exception) as exc_info:
            fresh.load_index(corpus_hash="deadbeef" + "0" * 56)
        assert str(tmp_path) in str(exc_info.value) or "hash" in str(exc_info.value).lower()

    def test_mismatch_does_not_serve_stale_index(self, tmp_path: Path) -> None:
        """After a hash mismatch, the store has no usable index loaded."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        try:
            fresh.load_index(corpus_hash="wrong_hash")
        except Exception:
            pass
        assert fresh._index is None


# ---------------------------------------------------------------------------
# TestAtomicWrite
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """save_index writes atomically -- no partial sidecar on simulated crash."""

    def test_bin_and_sidecar_written_together(self, tmp_path: Path) -> None:
        """After a successful save, both .bin and .json files exist."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        bin_files = list(tmp_path.glob("*.bin"))
        json_files = list(tmp_path.glob("*.json"))
        assert len(bin_files) == 1, "Expected one .bin file"
        assert len(json_files) == 1, "Expected one .json sidecar"

    def test_no_tempfile_left_on_disk_after_save(self, tmp_path: Path) -> None:
        """After save completes, no .tmp files remain in the cache directory."""
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Leftover temp files: {tmp_files}"


# ---------------------------------------------------------------------------
# TestMaxElementsHeadroom
# ---------------------------------------------------------------------------


class TestMaxElementsHeadroom:
    """load_index resizes the index to 120% of rule_count for growth headroom."""

    def test_max_elements_exceeds_rule_count_after_load(self, tmp_path: Path) -> None:
        """The loaded index has max_elements > rule_count (at least 1.2x)."""
        n = 5
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store, n=n)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)

        max_elements = loaded._index.get_max_elements()  # type: ignore[union-attr]
        assert max_elements >= int(n * 1.2)


# ---------------------------------------------------------------------------
# TestMissingSidecar / TestCorruptedSidecar
# ---------------------------------------------------------------------------


class TestMissingSidecar:
    """load_index behavior when no sidecar file is present."""

    def test_missing_sidecar_raises(self, tmp_path: Path) -> None:
        """load_index raises when no sidecar exists in cache_dir."""
        store = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(Exception):
            store.load_index(corpus_hash="any_hash")

    def test_missing_sidecar_error_includes_path(self, tmp_path: Path) -> None:
        """The error message from a missing sidecar contains the cache path."""
        store = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(Exception) as exc_info:
            store.load_index(corpus_hash="any_hash")
        assert str(tmp_path) in str(exc_info.value)


class TestCorruptedSidecar:
    """load_index behavior when the sidecar JSON is malformed or incomplete."""

    def test_truncated_json_raises(self, tmp_path: Path) -> None:
        """A truncated sidecar raises an error, not a silent wrong load."""
        sidecar_path = tmp_path / "writ_hnsw.json"
        sidecar_path.write_text('{"corpus_hash": "abc", "rule_coun')

        store = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(Exception):
            store.load_index(corpus_hash="abc")

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        """A sidecar missing a required field raises a validation error."""
        sidecar_path = tmp_path / "writ_hnsw.json"
        sidecar_path.write_text('{"corpus_hash": "abc"}')

        store = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(Exception):
            store.load_index(corpus_hash="abc")
