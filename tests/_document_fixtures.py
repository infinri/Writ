"""Shared builders for the item 5 document tests (workstream D). Not a test module.

BagEncoder is the deterministic stand-in encoder the plan asks for in the in-memory tests: a
384-dimension hashed bag of words, unit length, so two texts that share words have a high
cosine and two that share none have about zero. It is a stand-in for the MODEL only. The
indexes built over it are the real KeywordIndex and HnswlibStore, and the encoder object the
pipelines hold is the real CachedEncoder wrapper, because "the injected encoder is reused" is
a claim about that object's identity.

The tests that need the real model (calibration) use the session fixture
`methodology_model` from tests/conftest.py, never a second OnnxEmbeddingModel.
"""
from __future__ import annotations

import re
import zlib

import numpy as np

DIMENSIONS = 384
_WORD = re.compile(r"[a-z0-9]+")


class BagEncoder:
    """Deterministic hashed bag-of-words embedding with call counters."""

    def __init__(self) -> None:
        self.single_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        vec = np.zeros(DIMENSIONS, dtype=np.float32)
        for word in _WORD.findall(text.lower()):
            vec[zlib.crc32(word.encode("utf-8")) % DIMENSIONS] += 1.0
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    def encode(self, text):
        if isinstance(text, str):
            self.single_calls.append(text)
            return self._vector(text)
        self.batch_calls.append(list(text))
        return np.stack([self._vector(t) for t in text]) if text else np.zeros((0, DIMENSIONS), np.float32)

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [self._vector(t).tolist() for t in texts]

    @property
    def encoded_anything(self) -> bool:
        return bool(self.single_calls or self.batch_calls)


def cosine(encoder: BagEncoder, a: str, b: str) -> float:
    va, vb = encoder._vector(a), encoder._vector(b)
    return float(np.dot(va, vb))


def chunk_id(project: str, path: str, ordinal: int) -> str:
    return f"{project}:{path}#{ordinal:04d}"


def make_document(project: str, path: str, sections: list[tuple[str, str]], *,
                  title: str | None = None, kind: str = "docs") -> list[dict]:
    """The candidate dicts _collect_chunks yields for one document: one per (breadcrumb, text),
    linked by next_id and prev_id, KeywordIndex's aliases filled (rule_id = chunk_id, trigger =
    breadcrumb, statement = text, tags = the path with separators as spaces)."""
    title = title or sections[0][0].split(" > ")[0]
    ids = [chunk_id(project, path, i) for i in range(len(sections))]
    chunks = []
    for i, (breadcrumb, text) in enumerate(sections):
        chunks.append({
            "rule_id": ids[i], "chunk_id": ids[i], "trigger": breadcrumb, "statement": text,
            "tags": re.sub(r"[/._-]+", " ", path), "body": "", "mandatory": False,
            "node_type": "Chunk", "project": project, "doc_id": path, "ordinal": i,
            "breadcrumb": breadcrumb, "text": text, "title": title, "path": path, "kind": kind,
            "next_id": ids[i + 1] if i + 1 < len(ids) else None,
            "prev_id": ids[i - 1] if i > 0 else None,
        })
    return chunks
