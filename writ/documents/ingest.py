"""`writ docs ingest`: a repo's markdown into Document records with Chunk children.

A writer, not a retrieval collector (the chunk read is DOCUMENT_COLLECTOR in
writ/retrieval/pipeline.py). Each eligible file is hashed; an unchanged hash is skipped, a new
or changed file is split and replaced in one statement, and the documents of vanished files
are deleted, but only under the kinds this run ingested (docs/adr/ADR-document-retrieval.md).
Chunk text is stored raw: sanitizing happens once, at render.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from writ.documents.splitter import split_markdown
from writ.graph.schema import Chunk, Document
from writ.session.project_boundary import is_contained

DEFAULT_KINDS = frozenset({"docs", "adr", "readme"})
MAX_DOCUMENT_BYTES = 1024 * 1024
PRUNED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "vendor", "dist", "build", "__pycache__", "site-packages",
})
_ADR_DIRS = frozenset({"adr", "adrs"})


@dataclass
class IngestReport:
    scanned: int = 0
    unchanged: int = 0
    written: int = 0
    chunks: int = 0
    deleted: int = 0
    skipped_large: int = 0
    skipped_outside: int = 0


def _never_read(parts: tuple[str, ...]) -> bool:
    return parts[-1].lower().startswith("credentials") or "secrets" in parts[:-1]


def _kind(parts: tuple[str, ...]) -> str | None:
    name = parts[-1].lower()
    dirs = {d.lower() for d in parts[:-1]}
    if name == "claude.md":
        return "claude_md"
    if name == "readme.md":
        return "readme"
    if parts[0] == "docs" and len(parts) > 1:
        return "adr" if dirs & _ADR_DIRS or parts[-1].startswith("ADR-") else "docs"
    if dirs & _ADR_DIRS:
        return "adr"
    return None


def _discover(root: Path, kinds: frozenset[str]) -> list[tuple[str, str, Path]]:
    """(doc_id, kind, path) for every eligible markdown file under root, in walk order."""
    found: list[tuple[str, str, Path]] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in PRUNED_DIRS and d != "secrets")
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            path = Path(current) / name
            parts = path.relative_to(root).parts
            if _never_read(parts):
                continue
            kind = _kind(parts)
            if kind in kinds:
                found.append(("/".join(parts), kind, path))
    return found


def _memory_doc_id(path: Path) -> str:
    """memory/<short hash of the memory dir>/<file>: two memory dirs never share an id."""
    directory = hashlib.sha256(str(path.parent.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"memory/{directory}/{path.name}"


def _display_path(path: Path) -> str:
    """The stored (and printed) path of a memory file: home-relative when under home."""
    home = os.path.expanduser("~")
    return "~/" + os.path.relpath(path, home) if is_contained(str(path), home) else str(path)


async def ingest_documents(db, project: str, repo_root, kinds, memory_files) -> IngestReport:
    report = IngestReport()
    stored = await db.get_document_hashes(project)
    root = Path(repo_root).resolve()
    sources = _discover(root, frozenset(kinds))
    if "memory" in kinds:
        sources += [(_memory_doc_id(Path(f)), "memory", Path(f)) for f in memory_files
                    if not _never_read(Path(f).parts)]
    seen: set[str] = set()
    for doc_id, kind, path in sources:
        # Seen before any skip: a file still present but skipped keeps its stored document.
        seen.add(doc_id)
        resolved = path.resolve()
        if kind != "memory" and not is_contained(str(resolved), str(root)):
            report.skipped_outside += 1
            continue
        if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
            report.skipped_large += 1
            continue
        data = resolved.read_bytes()
        source_hash = hashlib.sha256(data).hexdigest()
        report.scanned += 1
        if (stored.get(doc_id) or {}).get("source_hash") == source_hash:
            report.unchanged += 1
            continue
        split = split_markdown(data.decode("utf-8", errors="replace"), fallback_title=path.stem)
        document = Document(
            doc_id=doc_id, project=project, path=_display_path(path) if kind == "memory" else doc_id,
            kind=kind, title=split.title, source_hash=source_hash, chunk_count=len(split.chunks),
            ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        chunks = [
            Chunk(chunk_id=f"{project}:{doc_id}#{draft.ordinal:04d}", project=project, doc_id=doc_id,
                  ordinal=draft.ordinal, breadcrumb=draft.breadcrumb, text=draft.text,
                  est_tokens=draft.est_tokens, source_hash=source_hash)
            for draft in split.chunks
        ]
        await db.replace_document(project, document, chunks)
        report.written += 1
        report.chunks += len(chunks)
    gone = sorted(doc_id for doc_id, row in stored.items()
                  if row.get("kind") in kinds and doc_id not in seen)
    if gone:
        await db.delete_documents(project, gone)
        report.deleted = len(gone)
    return report
