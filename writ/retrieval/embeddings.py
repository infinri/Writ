"""Vector search abstraction layer.

Per PY-PROTO-001: VectorStore uses Protocol, not ABC. The hnswlib and Qdrant
implementations satisfy the interface structurally without inheritance.

Per handbook Section 3.5: the swap is a single-file change. The pipeline,
ranking, and all upstream/downstream stages are unchanged.

Mandatory rules (mandatory: true) are excluded at index build time.
"""

from __future__ import annotations

from typing import Protocol

import hnswlib
from pydantic import BaseModel

# Per ARCH-CONST-001: named constants for HNSW defaults.
DEFAULT_EF_CONSTRUCTION = 200
DEFAULT_M = 16
DEFAULT_EF_SEARCH = 50


class ScoredResult(BaseModel):
    rule_id: str
    score: float


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
