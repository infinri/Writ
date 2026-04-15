"""Vector search and embedding abstraction layer.

Per PY-PROTO-001: VectorStore and EmbeddingModel use Protocol, not ABC.

Per handbook Section 3.5: the swap is a single-file change. The pipeline,
ranking, and all upstream/downstream stages are unchanged.

Mandatory rules (mandatory: true) are excluded at index build time.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

import hnswlib
import numpy as np
from pydantic import BaseModel, Field

# Per ARCH-CONST-001: named constants for HNSW defaults.
DEFAULT_EF_CONSTRUCTION = 200
DEFAULT_M = 16
DEFAULT_EF_SEARCH = 50

DEFAULT_HNSW_CACHE_DIR = str(Path.home() / ".cache" / "writ" / "hnsw")

# LRU cache size for query-time embeddings. ~1.5MB at 1024 entries (384-dim float32).
EMBEDDING_CACHE_SIZE = 1024

# Default ONNX model path.
DEFAULT_ONNX_DIR = Path.home() / ".cache" / "writ" / "models" / "onnx"


class ScoredResult(BaseModel):
    rule_id: str
    score: float


class HnswSidecar(BaseModel):
    """Pydantic model for the HNSW sidecar JSON file.

    Per PY-PYDANTIC-001: validates sidecar schema at load time.
    """

    model_config = {"populate_by_name": True}

    corpus_hash: str
    rule_count: int
    dims: int
    ef_construction: int
    M: int
    id_to_rule: dict[str, str] = Field(alias="_id_to_rule")


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
    Per ARCH-DI-001: cache_dir injected via constructor parameter.
    """

    def __init__(
        self,
        dimensions: int,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        m: int = DEFAULT_M,
        ef_search: int = DEFAULT_EF_SEARCH,
        cache_dir: str | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._ef_construction = ef_construction
        self._m = m
        self._ef_search = ef_search
        self._cache_dir = cache_dir or DEFAULT_HNSW_CACHE_DIR
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

    def save_index(self, corpus_hash: str) -> None:
        """Save HNSW index to disk with a JSON sidecar.

        Writes atomically via tempfile + rename to prevent partial files on crash.
        Per ARCH-ERR-001: errors include the cache path for context.
        """
        if self._index is None:
            return

        cache_dir = Path(self._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        bin_path = cache_dir / "writ_hnsw.bin"
        sidecar_path = cache_dir / "writ_hnsw.json"

        # Build sidecar data
        sidecar_data = {
            "corpus_hash": corpus_hash,
            "rule_count": len(self._id_to_rule),
            "dims": self._dimensions,
            "ef_construction": self._ef_construction,
            "M": self._m,
            "_id_to_rule": {str(k): v for k, v in self._id_to_rule.items()},
        }

        # Atomic write: sidecar JSON via tempfile + rename
        fd, tmp_sidecar = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(sidecar_data, f)
            os.rename(tmp_sidecar, str(sidecar_path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_sidecar)
            except OSError:
                pass
            raise

        # Atomic write: binary index via tempfile + rename
        fd2, tmp_bin = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
        os.close(fd2)
        try:
            self._index.save_index(tmp_bin)
            os.rename(tmp_bin, str(bin_path))
        except Exception:
            try:
                os.unlink(tmp_bin)
            except OSError:
                pass
            raise

    def load_index(self, corpus_hash: str) -> None:
        """Load HNSW index from disk, verifying corpus hash.

        Per ARCH-ERR-001: errors include sidecar path and specific mismatch.
        Per plan: resize_index to count * 1.2 for growth headroom.
        """
        cache_dir = Path(self._cache_dir)
        sidecar_path = cache_dir / "writ_hnsw.json"
        bin_path = cache_dir / "writ_hnsw.bin"

        if not sidecar_path.exists():
            raise FileNotFoundError(
                f"HNSW sidecar not found at {sidecar_path} "
                f"(cache_dir={self._cache_dir})"
            )

        try:
            raw = sidecar_path.read_text()
            sidecar_data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                f"Corrupted HNSW sidecar at {sidecar_path}: {exc}"
            ) from exc

        # Validate via Pydantic model
        try:
            HnswSidecar(**sidecar_data)
        except Exception as exc:
            raise ValueError(
                f"Invalid HNSW sidecar schema at {sidecar_path}: {exc}"
            ) from exc

        # Verify corpus hash
        stored_hash = sidecar_data.get("corpus_hash", "")
        if stored_hash != corpus_hash:
            self._index = None
            raise ValueError(
                f"HNSW corpus hash mismatch at {sidecar_path}: "
                f"stored={stored_hash!r}, expected={corpus_hash!r}"
            )

        if not bin_path.exists():
            self._index = None
            raise FileNotFoundError(
                f"HNSW binary index not found at {bin_path} "
                f"(cache_dir={self._cache_dir})"
            )

        # Load the index
        rule_count = sidecar_data["rule_count"]
        dims = sidecar_data["dims"]

        idx = hnswlib.Index(space="cosine", dim=dims)
        idx.load_index(str(bin_path), max_elements=rule_count)
        idx.set_ef(self._ef_search)

        # Resize for growth headroom (1.2x)
        new_max = max(int(rule_count * 1.2), rule_count + 1)
        idx.resize_index(new_max)

        self._index = idx
        self._dimensions = dims
        self._ef_construction = sidecar_data.get("ef_construction", self._ef_construction)
        self._m = sidecar_data.get("M", self._m)
        self._id_to_rule = {int(k): v for k, v in sidecar_data["_id_to_rule"].items()}
