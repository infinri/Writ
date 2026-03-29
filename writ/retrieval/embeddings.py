"""Vector search and embedding abstraction layer.

Per PY-PROTO-001: VectorStore and EmbeddingModel use Protocol, not ABC.

Per handbook Section 3.5: the swap is a single-file change. The pipeline,
ranking, and all upstream/downstream stages are unchanged.

Mandatory rules (mandatory: true) are excluded at index build time.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Protocol

import hnswlib
import numpy as np
from pydantic import BaseModel

# Per ARCH-CONST-001: named constants for HNSW defaults.
DEFAULT_EF_CONSTRUCTION = 200
DEFAULT_M = 16
DEFAULT_EF_SEARCH = 50

# LRU cache size for query-time embeddings. ~1.5MB at 1024 entries (384-dim float32).
EMBEDDING_CACHE_SIZE = 1024

# Default ONNX model path.
DEFAULT_ONNX_DIR = Path.home() / ".cache" / "writ" / "models" / "onnx"


class ScoredResult(BaseModel):
    rule_id: str
    score: float


class EmbeddingModel(Protocol):
    """Abstraction for text embedding models.

    Implementations:
    - SentenceTransformer (PyTorch, fallback)
    - OnnxEmbeddingModel (ONNX Runtime, preferred -- no PyTorch dependency)
    """

    def encode(self, text: str) -> np.ndarray: ...


class OnnxEmbeddingModel:
    """ONNX Runtime embedding model for MiniLM-style transformers.

    Uses optimum-exported ONNX graph. Tokenizer loaded from Rust-backed
    tokenizers library (no PyTorch/transformers import).
    Satisfies EmbeddingModel protocol structurally (PY-PROTO-001).
    """

    MAX_LENGTH = 128

    def __init__(self, model_dir: Path = DEFAULT_ONNX_DIR) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        if not model_path.exists():
            raise FileNotFoundError(f"ONNX model not found at {model_path}")
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"Tokenizer not found at {tokenizer_path}")

        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=self.MAX_LENGTH)
        self._tokenizer.enable_padding(length=self.MAX_LENGTH)
        # Note: onnxruntime emits a GPU discovery warning at import time on
        # CPU-only machines. This is a C++ log from device_discovery.cc, not
        # suppressible from Python. CPUExecutionProvider works correctly.
        self._session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )

    def _tokenize(self, texts: list[str]) -> dict[str, np.ndarray]:
        """Tokenize texts into numpy arrays for ONNX inference."""
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)
        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

    def _pool_and_normalize(
        self, token_embeddings: np.ndarray, attention_mask: np.ndarray,
    ) -> np.ndarray:
        """Mean pooling + L2 normalization."""
        mask = attention_mask[..., np.newaxis].astype(np.float32)
        pooled = (token_embeddings * mask).sum(axis=1) / mask.sum(axis=1)
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        return pooled / np.maximum(norm, 1e-12)

    def encode(self, text: str) -> np.ndarray:
        """Encode a single text to a normalized embedding vector."""
        inputs = self._tokenize([text])
        outputs = self._session.run(None, inputs)
        normalized = self._pool_and_normalize(outputs[0], inputs["attention_mask"])
        return normalized[0]

    def encode_batch(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """Encode a batch of texts. Used for build-time bulk encoding.

        Processes in chunks of batch_size to avoid OOM on large corpora.
        """
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            inputs = self._tokenize(chunk)
            outputs = self._session.run(None, inputs)
            normalized = self._pool_and_normalize(outputs[0], inputs["attention_mask"])
            all_embeddings.extend(normalized.tolist())
        return all_embeddings


class CachedEncoder:
    """LRU-cached wrapper around any embedding model.

    Returns independent copies on every call to prevent cache corruption
    from in-place mutation of returned arrays.

    Per ARCH-CONST-001: maxsize is EMBEDDING_CACHE_SIZE (1024).
    """

    def __init__(self, model: object, maxsize: int = EMBEDDING_CACHE_SIZE) -> None:
        self._model = model

        @functools.lru_cache(maxsize=maxsize)
        def _cached_encode(text: str) -> np.ndarray:
            result = self._model.encode(text)
            if isinstance(result, np.ndarray):
                return result
            return np.array(result, dtype=np.float32)

        self._cached_encode = _cached_encode

    def encode(self, text: str) -> np.ndarray:
        """Return embedding for text. Cache hit returns a copy to prevent mutation."""
        return self._cached_encode(text).copy()

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch encode -- delegates to underlying model. Does not use cache."""
        if hasattr(self._model, "encode_batch"):
            return self._model.encode_batch(texts)
        return self._model.encode(texts).tolist()

    def cache_info(self):
        """Return cache statistics."""
        return self._cached_encode.cache_info()

    def cache_clear(self) -> None:
        """Clear the cache."""
        self._cached_encode.cache_clear()


class VectorStore(Protocol):
    """Abstraction layer for vector search backends.

    Implementations:
    - HnswlibStore (Phases 1-5, in-process)
    - QdrantStore (at scale, gRPC/REST)
    """

    def build_index(self, rule_ids: list[str], vectors: list[list[float]]) -> None: ...
    def search(self, vector: list[float], k: int) -> list[ScoredResult]: ...


class HnswlibStore:
    """In-process HNSW vector search via hnswlib.

    Satisfies VectorStore protocol structurally (PY-PROTO-001).
    """

    def __init__(
        self,
        dimensions: int,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        m: int = DEFAULT_M,
        ef_search: int = DEFAULT_EF_SEARCH,
    ) -> None:
        self._dimensions = dimensions
        self._ef_construction = ef_construction
        self._m = m
        self._ef_search = ef_search
        self._index: hnswlib.Index | None = None
        self._id_to_rule: dict[int, str] = {}

    def build_index(self, rule_ids: list[str], vectors: list[list[float]]) -> None:
        """Build HNSW index from rule_ids and their embedding vectors.

        Caller is responsible for excluding mandatory rules before calling.
        """
        if not rule_ids:
            return
        count = len(rule_ids)
        self._index = hnswlib.Index(space="cosine", dim=self._dimensions)
        self._index.init_index(
            max_elements=count,
            ef_construction=self._ef_construction,
            M=self._m,
        )
        self._index.set_ef(self._ef_search)
        self._id_to_rule = {i: rid for i, rid in enumerate(rule_ids)}
        self._index.add_items(vectors, list(range(count)))

    def search(self, vector: list[float], k: int) -> list[ScoredResult]:
        """Return top-k nearest neighbors by cosine similarity."""
        if self._index is None or self._index.get_current_count() == 0:
            return []
        actual_k = min(k, self._index.get_current_count())
        labels, distances = self._index.knn_query([vector], k=actual_k)
        results: list[ScoredResult] = []
        for label, distance in zip(labels[0], distances[0]):
            rule_id = self._id_to_rule.get(int(label), "")
            # hnswlib cosine distance = 1 - cosine_similarity
            score = 1.0 - float(distance)
            results.append(ScoredResult(rule_id=rule_id, score=score))
        return results
