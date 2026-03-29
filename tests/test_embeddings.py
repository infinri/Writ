"""Embedding model tests: ONNX Runtime, CachedEncoder, ranking stability.

Per TEST-ISO-001: each test owns its data.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from writ.retrieval.embeddings import (
    DEFAULT_ONNX_DIR,
    CachedEncoder,
    OnnxEmbeddingModel,
)


# --- Fixtures ---


@pytest.fixture()
def onnx_model():
    """Load ONNX model if available, skip if not exported."""
    try:
        return OnnxEmbeddingModel(DEFAULT_ONNX_DIR)
    except (FileNotFoundError, ImportError):
        pytest.skip("ONNX model not exported. Run: python scripts/export_onnx.py")


def _make_mock_model():
    """Mock model that returns a deterministic 384-dim vector."""
    model = MagicMock()
    call_count = [0]

    def mock_encode(text):
        call_count[0] += 1
        rng = np.random.RandomState(hash(text) % 2**31)
        return rng.randn(384).astype(np.float32)

    model.encode = mock_encode
    model._call_count = call_count
    return model


# --- OnnxEmbeddingModel ---


class TestOnnxEmbeddingModel:
    """ONNX Runtime embedding model."""

    def test_encode_returns_correct_dimensions(self, onnx_model) -> None:
        vector = onnx_model.encode("test query")
        assert vector.shape == (384,)

    def test_encode_deterministic(self, onnx_model) -> None:
        v1 = onnx_model.encode("test query")
        v2 = onnx_model.encode("test query")
        assert np.allclose(v1, v2)

    def test_encode_different_texts_differ(self, onnx_model) -> None:
        v1 = onnx_model.encode("async blocking event loop")
        v2 = onnx_model.encode("SQL injection prevention")
        assert not np.allclose(v1, v2)

    def test_encode_batch_matches_single(self, onnx_model) -> None:
        texts = ["query one", "query two"]
        batch = onnx_model.encode_batch(texts)
        single_0 = onnx_model.encode(texts[0])
        single_1 = onnx_model.encode(texts[1])
        assert np.allclose(batch[0], single_0, atol=1e-5)
        assert np.allclose(batch[1], single_1, atol=1e-5)


# --- CachedEncoder ---


class TestCachedEncoder:
    """LRU cache on encode calls with mutation safety."""

    def test_cache_hit_returns_same_values(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=128)
        v1 = encoder.encode("test query")
        v2 = encoder.encode("test query")
        assert np.array_equal(v1, v2)

    def test_cache_miss_calls_model(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=128)
        encoder.encode("query 1")
        encoder.encode("query 2")
        assert model._call_count[0] == 2

    def test_cache_hit_does_not_call_model(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=128)
        encoder.encode("query 1")
        encoder.encode("query 1")
        assert model._call_count[0] == 1

    def test_maxsize_evicts_oldest(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=2)
        encoder.encode("a")
        encoder.encode("b")
        encoder.encode("c")  # evicts "a"
        encoder.encode("a")  # cache miss, re-encodes
        assert model._call_count[0] == 4

    def test_cache_info_reports_stats(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=128)
        encoder.encode("q1")
        encoder.encode("q1")
        info = encoder.cache_info()
        assert info.hits == 1
        assert info.misses == 1

    def test_cache_returns_independent_copies(self) -> None:
        model = _make_mock_model()
        encoder = CachedEncoder(model, maxsize=128)
        v1 = encoder.encode("test query")
        v1[0] = 999.0  # mutate returned array
        v2 = encoder.encode("test query")  # cache hit
        assert v2[0] != 999.0  # cached value not corrupted


# --- Ranking Stability (Integration) ---


class TestOnnxRankingStability:
    """ONNX produces identical ranking output to PyTorch."""

    @pytest.mark.asyncio
    async def test_top5_identical_on_ground_truth(self, onnx_model) -> None:
        """All 83 queries produce the same top-5 rule IDs with ONNX vs PyTorch."""
        import json

        from writ.graph.db import Neo4jConnection
        from writ.retrieval.pipeline import build_pipeline

        gt_path = Path("tests/fixtures/ground_truth_queries.json")
        if not gt_path.exists():
            pytest.skip("Ground truth queries not found")

        with open(gt_path) as f:
            ground_truth = json.load(f)["queries"]

        db = Neo4jConnection("bolt://localhost:7687", "neo4j", "writdevpass")
        try:
            # Build PyTorch pipeline (lazy import).
            from sentence_transformers import SentenceTransformer

            pt_model = SentenceTransformer("all-MiniLM-L6-v2")
            pt_pipeline = await build_pipeline(db, embedding_model=pt_model)

            # Build ONNX pipeline.
            onnx_encoder = CachedEncoder(onnx_model)
            onnx_pipeline = await build_pipeline(db, embedding_model=onnx_encoder)

            mismatches = []
            for query_data in ground_truth:
                qt = query_data["query"]
                pt_result = pt_pipeline.query(qt)
                onnx_result = onnx_pipeline.query(qt)
                pt_ids = [r["rule_id"] for r in pt_result["rules"][:5]]
                onnx_ids = [r["rule_id"] for r in onnx_result["rules"][:5]]
                if pt_ids != onnx_ids:
                    severity = "TOP-RANK" if pt_ids[0] != onnx_ids[0] else "ADJACENT-SWAP"
                    mismatches.append({
                        "query": qt,
                        "severity": severity,
                        "pytorch": pt_ids,
                        "onnx": onnx_ids,
                    })

            for m in mismatches:
                print(f"  [{m['severity']}] {m['query']}: PT={m['pytorch'][:3]} ONNX={m['onnx'][:3]}")

            assert mismatches == [], f"{len(mismatches)} queries diverged"
        finally:
            await db.close()
