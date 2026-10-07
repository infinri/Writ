"""Chunk retrieval for the per-prompt documents section (program item 5, phases 3 and 4).

DocumentPipeline is a hybrid BM25 plus vector query over the chunk indexes, built by the same
build_search_indexes as the rule indexes, in their own cache dirs, with the rule pipeline's
encoder injected. It scopes chunks through is_visible, drops chunks shown this epoch, abstains
on its own threshold, and expands each primary hit with its neighbours from the loaded
snapshot. fit_documents_block renders the fenced block (docs/adr/ADR-document-retrieval.md).
"""
from __future__ import annotations

from pathlib import Path

from writ.retrieval import pipeline
from writ.retrieval.embeddings import CachedEncoder, HnswlibStore, OnnxEmbeddingModel
from writ.retrieval.injection_ceiling import clamp_fenced
from writ.retrieval.keyword import KeywordIndex
from writ.retrieval.node_scope import is_visible
from writ.retrieval.pipeline import IndexLocation, merge_ranked_hits
from writ.retrieval.ranking import RankingWeights
from writ.shared.injection_text import fence, sanitize_retrieved, similarity_slot
from writ.shared.tokens import CHARS_PER_TOKEN

DOCUMENT_TOP_K = 3
DOCUMENT_CANDIDATE_LIMIT = 50
DOCUMENT_NEIGHBOUR_HOPS = 1
# Calibrated on the isolated graph over this repo's docs/ (docs/adr/ADR-document-retrieval.md).
DOCUMENT_ABSTENTION_THRESHOLD = 0.40
# The tokenizer truncates at MAX_LENGTH tokens; slicing first ties the HNSW hash to what is embedded.
CHUNK_EMBED_HEAD_CHARS = OnnxEmbeddingModel.MAX_LENGTH * CHARS_PER_TOKEN

_CHUNK_FIELDS = ("project", "doc_id", "ordinal", "breadcrumb", "title", "path", "kind", "text")


def document_vector_text(chunk: dict) -> str:
    return f"{chunk.get('breadcrumb', '')}\n{chunk.get('text', '')[:CHUNK_EMBED_HEAD_CHARS]}"


def document_index_location() -> IndexLocation:
    base = Path(pipeline.get_hnsw_cache_dir()).parent / "documents"
    return IndexLocation(base, str(base / "hnsw"), "documents_")


class DocumentPipeline:
    def __init__(self, keyword_index: KeywordIndex | None, vector_store: HnswlibStore | None,
                 encoder: CachedEncoder | None, chunks: list[dict]) -> None:
        self._keyword = keyword_index
        self._vector = vector_store
        self._model = encoder
        self._chunks = {c["chunk_id"]: c for c in chunks}

    @classmethod
    def empty(cls) -> DocumentPipeline:
        return cls(None, None, None, [])

    @property
    def encoder(self) -> CachedEncoder | None:
        return self._model

    def _entry(self, rid: str, score: float) -> dict:
        chunk = self._chunks[rid]
        return {"id": rid, "score": round(score, 4), **{k: chunk.get(k) for k in _CHUNK_FIELDS}}

    def _admitted_hits(self, search, rid_of, admitted) -> list:
        """Up to DOCUMENT_CANDIDATE_LIMIT admitted hits. The indexes hold every project's
        chunks, so the search widens (bounded by the corpus size) until enough hits survive
        the scope and exclusion filter, and another project's chunks cannot crowd these out."""
        k = DOCUMENT_CANDIDATE_LIMIT
        while True:
            hits = search(k)
            kept = [h for h in hits if admitted(rid_of(h))]
            if len(kept) >= DOCUMENT_CANDIDATE_LIMIT or len(hits) < k or k >= len(self._chunks):
                return kept[:DOCUMENT_CANDIDATE_LIMIT]
            k *= 4

    def query(self, text: str, *, project: str | None, exclude_ids, top_k: int = DOCUMENT_TOP_K) -> dict:
        if not self._chunks:
            return {"chunks": [], "mode": "abstained", "abstain_signal": 0.0}
        exclude = set(exclude_ids or ())

        def admitted(rid: str) -> bool:
            chunk = self._chunks.get(rid)
            return (chunk is not None and rid not in exclude
                    and is_visible("Chunk", chunk.get("project"), project))

        query_vector = self._model.encode(text).tolist()
        bm25 = self._admitted_hits(lambda k: self._keyword.search(text, limit=k),
                                   lambda r: r["rule_id"], admitted)
        vector = self._admitted_hits(lambda k: self._vector.search(query_vector, k=k),
                                     lambda r: r.rule_id, admitted)
        top = max((r.score for r in vector), default=0.0)
        if top < DOCUMENT_ABSTENTION_THRESHOLD:
            return {"chunks": [], "mode": "abstained", "abstain_signal": round(top, 6)}

        merged = merge_ranked_hits(bm25, vector)
        w = RankingWeights()
        scores = {rid: (w.w_bm25 * e["bm25_norm"] + w.w_vector * e["vector_norm"]) / (w.w_bm25 + w.w_vector)
                  for rid, e in merged.items()}
        ranked = sorted(merged, key=lambda rid: scores[rid], reverse=True)
        primaries = [rid for rid in ranked
                     if merged[rid]["similarity"] is not None
                     and merged[rid]["similarity"] >= DOCUMENT_ABSTENTION_THRESHOLD][:top_k]

        chunks = [{**self._entry(rid, scores[rid]), "similarity": round(merged[rid]["similarity"], 4)}
                  for rid in primaries]
        placed = set(primaries)
        for rid in primaries:
            doc = (self._chunks[rid]["project"], self._chunks[rid]["doc_id"])
            for link in ("next_id", "prev_id"):
                nid = rid
                for _ in range(DOCUMENT_NEIGHBOUR_HOPS):
                    nid = self._chunks.get(nid, {}).get(link)
                    neighbour = self._chunks.get(nid)
                    if neighbour is None or (neighbour["project"], neighbour["doc_id"]) != doc:
                        break
                    if nid not in placed and nid not in exclude:
                        placed.add(nid)
                        chunks.append(self._entry(nid, scores.get(nid, 0.0)))
        return {"chunks": chunks, "mode": "ranked", "abstain_signal": round(top, 6)}


def assemble_document_pipeline(chunks: list[dict], *, embedding_model) -> DocumentPipeline:
    if not chunks:
        return DocumentPipeline.empty()
    indexes = pipeline.build_search_indexes(
        chunks, document_index_location(), document_vector_text, embedding_model=embedding_model,
    )
    return DocumentPipeline(indexes.keyword_index, indexes.vector_store, indexes.encoder, chunks)


def _head(chunk: dict) -> str:
    slot = similarity_slot(chunk) if "similarity" in chunk else " (context)"
    return (f"[{sanitize_retrieved(chunk.get('path'))} #{chunk.get('ordinal', 0):04d}] "
            f"{sanitize_retrieved(chunk.get('breadcrumb'))}{slot}")


def _fenced(placed: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = {}
    for chunk in placed:
        groups.setdefault((chunk.get("project"), chunk.get("doc_id")), []).append(chunk)
    lines: list[str] = []
    for members in groups.values():
        first = members[0]
        lines.append(f"## {sanitize_retrieved(first.get('title'))} ({sanitize_retrieved(first.get('path'))})")
        for chunk in sorted(members, key=lambda c: c.get("ordinal", 0)):
            lines += [_head(chunk), sanitize_retrieved(chunk.get("text"))]
    return fence("DOCUMENTS", "\n".join(lines), f"{len(placed)} chunks")


def fit_documents_block(result: dict, limit: int) -> tuple[str, list[dict]]:
    """The fenced documents block within `limit`, and [{id, head}] for every placed chunk."""
    placed: list[dict] = []
    for chunk in result.get("chunks") or []:
        if len(_fenced([*placed, chunk])) <= limit:
            placed.append(chunk)
    if not placed:
        return "", []
    block = clamp_fenced(_fenced(placed), limit)
    return block, [{"id": c["id"], "head": _head(c)} for c in placed]
