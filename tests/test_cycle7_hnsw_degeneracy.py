"""Unit tests for the cycle 7 HNSW degeneracy gate (plan.md, "Defect 3:
verify the payload, not just the label on the box", plan.md:573-694).

Covers:
  - `_degeneracy_sample_ids` (capability 17): a pure helper that picks
    `DEGENERACY_SAMPLE_SIZE` evenly spaced, sorted, distinct ids across
    0..count-1 (plan.md:616-648). No driver, no hnswlib.
  - The third gate in `HnswlibStore.load_index` (capabilities 18-20,
    plan.md:650-684): it samples stored vectors via
    `idx.get_items(sample_ids, return_type="numpy")` and rejects an index
    whose sampled rows are ALL zero-norm, warns and loads when only SOME
    are zero, and leaves a healthy round trip untouched.

None of this exists yet. `_degeneracy_sample_ids` and
`DEGENERACY_SAMPLE_SIZE` are imported inside a guarded try/except (mirrors
tests/test_hnsw_persistence.py's `HnswSidecar` import) so the module still
collects; tests that need those symbols call `pytest.fail` with a
"skeleton" message when the guard tripped, per TEST-REGRESSION-001 style
skeleton-failure reporting. The gate tests (18) do not need the guard: on
today's code `load_index` simply loads a zero-vector index, so
`pytest.raises(ValueError)` fails with "DID NOT RAISE" -- itself a correct,
legible red for the missing behavior.

Per TEST-TDD-001: skeletons approved before implementation.
Per ARCH-CONST-001: cache_dir is ALWAYS injected as tmp_path in this file,
never ~/.cache/writ -- that is the live index the running daemon serves
from, and a corrupted one already caused six days of degraded retrieval
(plan.md:580-582).
Per ARCH-ERR-001: load failures must include the sidecar/.bin path and the
specific mismatch detail; that is exactly what capability 18 pins.
No `clear_all()` and no Neo4j: this file is pure HnswlibStore + hnswlib.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from writ.retrieval.embeddings import HnswlibStore

# Cycle 7 additions -- do not exist yet. Guarded so this module still
# collects; tests that need these symbols fail loudly via pytest.fail
# rather than erroring at import/collection time.
try:
    from writ.retrieval.embeddings import DEGENERACY_SAMPLE_SIZE
except ImportError:
    DEGENERACY_SAMPLE_SIZE = None  # type: ignore[assignment]

try:
    from writ.retrieval.embeddings import _degeneracy_sample_ids
except ImportError:
    _degeneracy_sample_ids = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(dimensions: int = 4, cache_dir: str | None = None) -> HnswlibStore:
    """Build a small HnswlibStore. cache_dir injected per ARCH-DI-001."""
    kwargs: dict[str, Any] = {"dimensions": dimensions}
    if cache_dir is not None:
        kwargs["cache_dir"] = cache_dir
    return HnswlibStore(**kwargs)


def _zero_vectors(n: int, dims: int) -> list[list[float]]:
    """n rows that all encode to zero -- the degenerate fixture. Measured on
    the installed hnswlib 0.8.0: a raw zero row reads back from
    get_items(..., return_type="numpy") at norm 0.0 for space="cosine".
    """
    return [[0.0] * dims for _ in range(n)]


def _random_vectors(n: int, dims: int, seed: int = 42) -> list[list[float]]:
    """n healthy, non-zero rows -- same construction as
    tests/test_hnsw_persistence.py's _build_tiny_index. Measured: a raw
    scaled row reads back normalized at norm 1.0 for space="cosine", so
    these can never look degenerate.
    """
    rng = np.random.RandomState(seed)
    return [rng.randn(dims).astype(np.float32).tolist() for _ in range(n)]


def _build_and_save(
    store: HnswlibStore,
    rule_ids: list[str],
    vectors: list[list[float]],
    corpus_hash: str,
) -> None:
    """Build a real index through the real HnswlibStore API and persist it."""
    store.build_index(rule_ids, vectors)
    store.save_index(corpus_hash=corpus_hash)


# ---------------------------------------------------------------------------
# Capability 17: _degeneracy_sample_ids
# ---------------------------------------------------------------------------


class TestDegeneracySampleIds:
    """Evenly spaced, sorted, distinct ids across 0..count-1 (plan.md:633-647).

    Per plan.md:701-703 this helper is pure and unit-tested with no driver.
    """

    def test_zero_count_returns_empty_list(self) -> None:
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        assert _degeneracy_sample_ids(0) == []

    def test_negative_count_returns_empty_list(self) -> None:
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        assert _degeneracy_sample_ids(-5) == []

    def test_count_below_sample_size_returns_every_id(self) -> None:
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        assert _degeneracy_sample_ids(5, sample_size=16) == [0, 1, 2, 3, 4]

    def test_count_equal_to_sample_size_returns_every_id(self) -> None:
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        assert _degeneracy_sample_ids(16, sample_size=16) == list(range(16))

    def test_count_above_sample_size_returns_exactly_sample_size_distinct_sorted_ids(
        self,
    ) -> None:
        """A naive rounding implementation emits duplicate ids for a large
        count; a set-then-sort implementation can silently collapse to
        fewer than sample_size distinct ids. Both are wrong: pin the count
        exactly, not just "no duplicates".
        """
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        ids = _degeneracy_sample_ids(10_000, sample_size=16)
        assert len(ids) == 16, f"expected exactly 16 ids, got {len(ids)}: {ids}"
        assert len(set(ids)) == 16, f"expected 16 distinct ids, found duplicates: {ids}"
        assert ids == sorted(ids), f"ids must be sorted: {ids}"
        assert all(0 <= i < 10_000 for i in ids), f"id out of range 0..9999: {ids}"

    def test_custom_sample_size_is_respected(self) -> None:
        if _degeneracy_sample_ids is None:
            pytest.fail("skeleton -- _degeneracy_sample_ids not yet implemented")
        ids = _degeneracy_sample_ids(1000, sample_size=4)
        assert len(ids) == 4, f"expected exactly 4 ids, got {len(ids)}: {ids}"
        assert len(set(ids)) == 4, f"expected 4 distinct ids, found duplicates: {ids}"
        assert ids == sorted(ids)
        assert all(0 <= i < 1000 for i in ids)

    def test_default_sample_size_is_used_when_unspecified(self) -> None:
        if _degeneracy_sample_ids is None or DEGENERACY_SAMPLE_SIZE is None:
            pytest.fail(
                "skeleton -- _degeneracy_sample_ids/DEGENERACY_SAMPLE_SIZE not "
                "yet implemented"
            )
        ids = _degeneracy_sample_ids(10_000)
        assert len(ids) == DEGENERACY_SAMPLE_SIZE

    def test_degeneracy_sample_size_constant_equals_16(self) -> None:
        if DEGENERACY_SAMPLE_SIZE is None:
            pytest.fail("skeleton -- DEGENERACY_SAMPLE_SIZE not yet implemented")
        assert DEGENERACY_SAMPLE_SIZE == 16


# ---------------------------------------------------------------------------
# Capability 18: load_index rejects an all-zero sample
# ---------------------------------------------------------------------------


class TestLoadIndexRejectsAllZeroSample:
    """load_index raises when EVERY sampled stored vector has zero norm
    (plan.md:663-677): self._index is left None first, and the message
    names the .bin path, the number of sampled vectors, the rule_count,
    and the corpus_hash.

    Getting this backwards (loading a zero index) is exactly the six-day
    incident plan.md describes: knn_query never raises on an all-zero
    index, it just returns distance 1.0 for every neighbour.
    """

    def test_raises_valueerror_naming_bin_path_sample_size_rule_count_and_hash(
        self, tmp_path: Path
    ) -> None:
        n = 5
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        corpus_hash = "hash-all-zero"
        _build_and_save(store, rule_ids, _zero_vectors(n, store._dimensions), corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(ValueError) as exc_info:
            fresh.load_index(corpus_hash=corpus_hash)

        bin_path = tmp_path / "writ_hnsw.bin"
        message = str(exc_info.value)
        assert str(bin_path) in message, message
        assert f"{n} sampled vectors have zero norm" in message, message
        assert f"rule_count={n}" in message, message
        assert f"corpus_hash={corpus_hash}" in message, message

    def test_index_left_none_after_all_zero_rejection(self, tmp_path: Path) -> None:
        n = 5
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        corpus_hash = "hash-all-zero-index-none"
        _build_and_save(store, rule_ids, _zero_vectors(n, store._dimensions), corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(ValueError):
            fresh.load_index(corpus_hash=corpus_hash)

        assert fresh._index is None

    def test_message_uses_sampled_count_not_rule_count_for_large_corpus(
        self, tmp_path: Path
    ) -> None:
        """rule_count (30) and the sampled count (16, the fixed
        DEGENERACY_SAMPLE_SIZE) must both appear, and must differ, proving
        the message reports the SAMPLE size, not the corpus size it was
        drawn from.
        """
        n = 30  # comfortably above the fixed sample size of 16
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        corpus_hash = "hash-all-zero-large"
        _build_and_save(store, rule_ids, _zero_vectors(n, store._dimensions), corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with pytest.raises(ValueError) as exc_info:
            fresh.load_index(corpus_hash=corpus_hash)

        message = str(exc_info.value)
        assert "16 sampled vectors have zero norm" in message, message
        assert f"rule_count={n}" in message, message


# ---------------------------------------------------------------------------
# Capability 19: load_index warns (does not raise) on a partial-zero sample
# ---------------------------------------------------------------------------


class TestLoadIndexWarnsOnPartialZeroSample:
    """load_index logs a warning and still loads when only SOME sampled
    vectors are zero-norm (plan.md:678-683): one degenerate row must not
    disable the cache and start a rebuild loop (plan.md:595-599 -- any-zero
    rejection would re-encode the same zero row, save it, and reject again
    on every cold start, forever).
    """

    def test_loads_successfully_and_logs_warning_naming_zero_count_sample_size_and_hash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        n = 5
        zero_row = 2
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        vectors = _random_vectors(n, store._dimensions, seed=11)
        vectors[zero_row] = [0.0] * store._dimensions
        corpus_hash = "hash-partial-zero"
        _build_and_save(store, rule_ids, vectors, corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="writ.retrieval.embeddings"):
            fresh.load_index(corpus_hash=corpus_hash)  # must NOT raise

        assert fresh._index is not None, "one zero row must not leave the index unset"
        assert len(fresh._id_to_rule) == n

        bin_path = tmp_path / "writ_hnsw.bin"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING log record for a partially-zero sample"
        message = warnings[0].getMessage()
        assert str(bin_path) in message, message
        assert "1 zero-norm vector(s) in a sample of 5" in message, message
        assert f"corpus_hash={corpus_hash}" in message, message

    def test_two_zero_rows_out_of_seven_still_loads_and_warns_with_correct_counts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """SOME can mean more than one row: the counts in the warning must
        track the actual zero_count and sample size, not be hardcoded to 1.
        Also confirms the loaded index is still fully usable: a query near
        a healthy row finds it.
        """
        n = 7
        zero_rows = (1, 4)
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        vectors = _random_vectors(n, store._dimensions, seed=13)
        for row in zero_rows:
            vectors[row] = [0.0] * store._dimensions
        corpus_hash = "hash-partial-zero-two-rows"
        _build_and_save(store, rule_ids, vectors, corpus_hash)

        fresh = _make_store(cache_dir=str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="writ.retrieval.embeddings"):
            fresh.load_index(corpus_hash=corpus_hash)  # must NOT raise

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING log record for a partially-zero sample"
        message = warnings[0].getMessage()
        assert "2 zero-norm vector(s) in a sample of 7" in message, message

        healthy_query = vectors[0]
        results = fresh.search(healthy_query, k=1)
        assert results and results[0].rule_id == rule_ids[0]


# ---------------------------------------------------------------------------
# Capability 20: a healthy round trip is untouched by the third gate
# ---------------------------------------------------------------------------


class TestLoadIndexHealthyRoundTripUnaffectedByDegeneracyGate:
    """A healthy save/load round trip -- no zero rows anywhere in the
    sample -- still loads and returns the same top-1 search result as
    before the third gate existed. n is sized above the fixed sample size
    so the gate's real sampling path is exercised, not the count<=16
    passthrough, proving the gate is silent on real, healthy data.
    """

    def test_top1_search_result_matches_before_and_after_round_trip(
        self, tmp_path: Path
    ) -> None:
        if DEGENERACY_SAMPLE_SIZE is None:
            pytest.fail("skeleton -- DEGENERACY_SAMPLE_SIZE not yet implemented")

        n = DEGENERACY_SAMPLE_SIZE + 9  # above the sample size: real sampling, not passthrough
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        vectors = _random_vectors(n, store._dimensions, seed=42)
        corpus_hash = "hash-healthy-round-trip"
        _build_and_save(store, rule_ids, vectors, corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        loaded.load_index(corpus_hash=corpus_hash)  # must not raise

        query = vectors[0]
        original_top1 = store.search(query, k=1)[0].rule_id
        loaded_top1 = loaded.search(query, k=1)[0].rule_id
        assert loaded_top1 == original_top1

    def test_no_degeneracy_warning_logged_for_a_fully_healthy_corpus(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        if DEGENERACY_SAMPLE_SIZE is None:
            pytest.fail("skeleton -- DEGENERACY_SAMPLE_SIZE not yet implemented")

        n = DEGENERACY_SAMPLE_SIZE + 9
        store = _make_store(cache_dir=str(tmp_path))
        rule_ids = [f"RULE-{i}" for i in range(n)]
        vectors = _random_vectors(n, store._dimensions, seed=7)
        corpus_hash = "hash-healthy-no-warning"
        _build_and_save(store, rule_ids, vectors, corpus_hash)

        loaded = _make_store(cache_dir=str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="writ.retrieval.embeddings"):
            loaded.load_index(corpus_hash=corpus_hash)

        degeneracy_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "zero-norm" in r.getMessage()
        ]
        assert degeneracy_warnings == [], (
            "a fully healthy corpus must not log a degeneracy warning, got: "
            f"{[r.getMessage() for r in degeneracy_warnings]}"
        )
