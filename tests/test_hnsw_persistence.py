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


# ---------------------------------------------------------------------------
# TestBinFingerprintIntegrity (#84 -- torn-pair cache integrity fix)
# ---------------------------------------------------------------------------


class TestBinFingerprintIntegrity:
    """load_index verifies the on-disk .bin's sha256 against the sidecar's
    recorded bin_sha256, so a torn pair (sidecar from one build, .bin from
    another) or an unverifiable old-format sidecar is a detected cache-miss,
    never a silent wrong-vector load.

    Per TEST-REGRESSION-001: test_load_rejects_torn_bin_from_different_build
    reproduces bug #84 directly and must fail against today's code (which
    loads whatever .bin is on disk once corpus_hash matches).

    Per ENF-SYS-005: a true inter-process race that tears the .bin/.json
    pair cannot be forced deterministically in a unit test, so this
    simulates the torn pair directly (overwrite the .bin bytes after save)
    rather than claiming to reproduce the race itself. The guarantee under
    test is detect-and-raise, not prevention of the underlying race.
    """

    def test_load_rejects_torn_bin_from_different_build(self, tmp_path: Path) -> None:
        """A sidecar for build A (corpus_hash="hash-A") paired with build B's
        .bin (same dims and rule_count, different vectors) must raise, not
        silently serve B's vectors under A's corpus_hash.

        Uses a valid-but-wrong .bin (a real save_index output from a second
        store), not garbage bytes: garbage would make hnswlib itself throw
        on load, which would pass for the wrong reason. A valid .bin from a
        different build is the actual torn-pair failure mode -- today's
        code accepts it silently because only corpus_hash is checked.
        """
        dir_a = tmp_path / "store_a"
        dir_b = tmp_path / "store_b"
        dir_a.mkdir()
        dir_b.mkdir()

        store_a = _make_store(cache_dir=str(dir_a))
        _build_tiny_index(store_a, n=5)
        store_a.save_index(corpus_hash="hash-A")

        store_b = _make_store(cache_dir=str(dir_b))
        rng = np.random.RandomState(99)
        rule_ids_b = [f"OTHER-RULE-{i:03d}" for i in range(5)]
        vectors_b = [rng.randn(store_b._dimensions).astype(np.float32).tolist() for _ in range(5)]
        store_b.build_index(rule_ids_b, vectors_b)
        store_b.save_index(corpus_hash="hash-B")

        # Torn pair: leave A's sidecar (corpus_hash="hash-A") untouched, but
        # overwrite A's .bin with B's -- a valid, differently-built index
        # with the same dims/rule_count so it loads without an hnswlib error.
        bin_a = dir_a / "writ_hnsw.bin"
        bin_b = dir_b / "writ_hnsw.bin"
        bin_a.write_bytes(bin_b.read_bytes())

        fresh = _make_store(cache_dir=str(dir_a))
        with pytest.raises(ValueError):
            fresh.load_index(corpus_hash="hash-A")

    def test_load_rejects_old_format_sidecar_without_fingerprint(self, tmp_path: Path) -> None:
        """A sidecar with no bin_sha256 field (or bin_sha256=="") cannot be
        verified against its .bin and must raise so the caller rebuilds,
        rather than silently accepting an unverifiable .bin (old-format
        back-compat: fail-loud once, not a silent wrong load forever).
        """
        store = _make_store(cache_dir=str(tmp_path))
        _build_tiny_index(store)
        store.save_index(corpus_hash="hash-old")

        sidecar_path = tmp_path / "writ_hnsw.json"
        data = json.loads(sidecar_path.read_text())
        data.pop("bin_sha256", None)
        sidecar_path.write_text(json.dumps(data))

        fresh = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(ValueError):
            fresh.load_index(corpus_hash="hash-old")

    def test_roundtrip_with_fingerprint_still_loads(self, tmp_path: Path) -> None:
        """Once bin_sha256 is recorded on save, a load with a matching
        corpus_hash still succeeds and returns the same results as before
        save -- the fingerprint check does not break the happy path.
        """
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids, vectors = _build_tiny_index(store)
        corpus_hash = _corpus_hash_for(rule_ids, vectors)
        store.save_index(corpus_hash=corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)

        assert loaded._id_to_rule == store._id_to_rule
        query = np.array(vectors[0], dtype=np.float32).tolist()
        assert loaded.search(query, k=1)[0].rule_id == store.search(query, k=1)[0].rule_id

    def test_save_cleans_tempfiles_and_leaves_no_pair_on_rename_failure(self, tmp_path: Path, monkeypatch) -> None:
        """If a rename fails mid-save (the crash-between-writes scenario #84 is
        about), save_index raises, cleans up its tempfiles, and does not leave a
        half-written cache pair. Payload-before-pointer: the .bin is renamed
        first, so a failure there must leave neither final file nor any .tmp.
        """
        store = _make_store(cache_dir=str(tmp_path))
        _build_tiny_index(store)

        real_rename = os.rename

        def boom(src: Any, dst: Any) -> None:
            if str(dst).endswith("writ_hnsw.bin"):
                raise OSError("simulated .bin rename failure")
            return real_rename(src, dst)

        monkeypatch.setattr(os, "rename", boom)
        with pytest.raises(OSError):
            store.save_index(corpus_hash="hash-fail")

        assert not list(tmp_path.glob("*.tmp")), "tempfiles leaked on rename failure"
        assert not (tmp_path / "writ_hnsw.bin").exists()
        assert not (tmp_path / "writ_hnsw.json").exists()


# ---------------------------------------------------------------------------
# TestDeterministicBuild (F4)
# ---------------------------------------------------------------------------


class TestDeterministicBuild:
    """Insertion order across threads decides the graph links, so two builds of the same
    vectors can return different neighbour tails. A single insertion thread removes that."""

    def test_build_index_inserts_with_a_single_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deterministic: records the keyword arguments build_index passes to add_items."""
        from types import SimpleNamespace

        import hnswlib

        import writ.retrieval.embeddings as embeddings

        recorded: list[dict[str, Any]] = []

        class _RecordingIndex:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._real = hnswlib.Index(*args, **kwargs)

            def add_items(self, *args: Any, **kwargs: Any) -> Any:
                recorded.append(dict(kwargs))
                return self._real.add_items(*args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        monkeypatch.setattr(embeddings, "hnswlib", SimpleNamespace(Index=_RecordingIndex))
        store = _make_store()
        _build_tiny_index(store, n=5)

        assert len(recorded) == 1, recorded
        assert recorded[0].get("num_threads") == 1, recorded

    def test_two_builds_of_the_same_vectors_agree_on_neighbours_and_bytes(self, tmp_path: Path) -> None:
        """Behavioural proof. On a multi-core machine this is expected to fail before the
        fix, but a lucky thread schedule can pass it; the test above is the deterministic pin."""
        dims, count, queries, k = 384, 2000, 50, 10
        rng = np.random.RandomState(7)
        vectors = rng.randn(count, dims).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        vector_lists = vectors.tolist()
        rule_ids = [f"DET-RULE-{i:05d}" for i in range(count)]
        query_vectors = rng.randn(queries, dims).astype(np.float32).tolist()

        stores = []
        for label in ("a", "b"):
            store = HnswlibStore(dimensions=dims, cache_dir=str(tmp_path / label))
            store.build_index(rule_ids, vector_lists)
            store.save_index(corpus_hash="same-corpus")
            stores.append(store)

        for query in query_vectors:
            first = [r.rule_id for r in stores[0].search(query, k=k)]
            second = [r.rule_id for r in stores[1].search(query, k=k)]
            assert first == second, (first, second)

        digests = [json.loads((tmp_path / label / "writ_hnsw.json").read_text())["bin_sha256"]
                   for label in ("a", "b")]
        assert digests[0] == digests[1], digests
