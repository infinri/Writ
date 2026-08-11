# writ-auth-scan: internal-service
"""Decision-memory routes: commit capture + memory mirror + recall.

3 routes: /commit/capture, /memory-record, /recall.

_db, capture_commit and log_friction_event are read via live `server.<attr>`
access inside handler bodies (the monkeypatch seam).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

import writ.server as server
from writ.server.models import (
    CommitCaptureRequest,
    MemoryRecordRequest,
    RecallRequest,
)

router = APIRouter()


def _project_from_memory_path(path: str) -> str:
    """The project scope of a memory path: the dir CONTAINING its memory dir.

    Both callers send `project` (computed by bin/lib/memory_capture.py, the
    stdlib-only parser the daemon deliberately does not import -- it belongs to the
    hook's no-virtualenv world). This is the fallback for a caller that sends only
    the path, and it must resolve the SAME segment that module does.
    """
    parts = [part for part in (path or "").replace("\\", "/").split("/") if part]
    if len(parts) < 3 or parts[-2] != "memory":
        return ""
    return parts[-3]


@router.post("/commit/capture")
async def commit_capture(request: CommitCaptureRequest) -> dict[str, Any]:
    """Create the Commit + FileChange records for a landed commit (Phase 1d).

    The post-commit hook curls this. The capture runs inside a try/except so a
    failure (including the DB raising) is logged and the route still returns
    (fail-open): a commit is never blocked by a graph-write failure. _db None
    returns the error shape without raising.
    """
    if server._db is None:
        return {"error": "Database not connected."}

    try:
        await server.capture_commit(
            server._db,
            cwd=request.project_root,
            commit_hash=request.commit_hash,
            subject=request.subject,
            author=request.author,
            branch=request.branch,
            files=request.files,
            session_id=request.session_id,
        )
    except Exception as exc:
        await asyncio.to_thread(
            server.log_friction_event,
            session_id=request.commit_hash,
            mode=None,
            event="commit_capture_failed",
            error=str(exc),
        )
        return {"ok": True}
    return {"ok": True}


@router.post("/memory-record")
async def memory_record(request: MemoryRecordRequest) -> dict[str, Any]:
    """Upsert one auto-memory file as a Memory record (the graph mirror).

    hooks/scripts/writ-memory-capture.sh curls this after a memory write LANDS, so
    the file already exists whatever happens here. Fail-open in the same shape as
    /commit/capture: _db None returns the error shape, and any failure is logged as
    memory_capture_failed and still returns 200 -- a graph-write failure must never
    surface as a hook error on a write that already succeeded. `writ memory backfill`
    re-covers every miss. The exception detail goes to the log, never to the caller.

    Access boundary: localhost-only daemon route, single-user, no auth tier; writes
    project-scoped Memory records on the caller's own machine.
    """
    if server._db is None:
        return {"error": "Database not connected."}

    try:
        name = await server._db.create_memory(
            name=request.name,
            project=request.project or _project_from_memory_path(request.path),
            description=request.description,
            type=request.type,
            body=request.body,
            links=request.links,
            path=request.path,
            session_id=request.session_id,
            updated_at=request.updated_at,
            status=request.status,
        )
    except Exception as exc:
        try:
            await asyncio.to_thread(
                server.log_friction_event,
                session_id=request.session_id,
                mode=None,
                event="memory_capture_failed",
                error=str(exc),
                file_path=request.path,
            )
        except Exception:
            pass
        return {"ok": True}
    return {"ok": True, "name": name}


@router.post("/recall")
async def recall(request: RecallRequest) -> dict[str, Any]:
    """Compile the project's recent rule-grounded Decisions (Phase 2 recall).

    The first-prompt briefing hook and `writ recall` both reach this route.
    Recall is a SEPARATE project-scoped read (Decision is excluded from the RAG
    pipeline), scoped to the project resolved from the caller's cwd. Fail-open:
    _db None returns the error shape, and any failure is logged and returns an
    empty-but-valid payload so a recall failure never blocks a prompt.

    Access boundary: localhost-only daemon route, single-user, no auth tier;
    reads project-scoped Decision records on the caller's own machine.
    """
    if server._db is None:
        return {"error": "Database not connected."}

    try:
        from writ.session.recall import compile_recall

        project = await server._db.resolve_project_for_cwd(request.project_root)
        if not project:
            # An unregistered project_root resolves to NO project now that the
            # resolver's "writ" default is gone. Querying for the empty string would
            # either return nothing or, worse, match records mis-filed under no
            # project at all, and reading back another project's decisions is the
            # exact confusion this cycle removes. Return the empty-but-valid payload
            # and say why, because a silent empty briefing looks like "no decisions
            # captured yet" and sends an operator looking in the wrong place.
            await asyncio.to_thread(
                server.log_friction_event,
                session_id=request.project_root,
                mode=None,
                event="recall_project_unresolved",
                project_root=request.project_root,
            )
            return {"ok": True, "briefing": "", "decisions": []}
        payload = await compile_recall(
            server._db, project, budget=request.budget, full=request.full
        )
    except Exception as exc:
        try:
            await asyncio.to_thread(
                server.log_friction_event,
                session_id=request.project_root,
                mode=None,
                event="recall_failed",
                error=str(exc),
            )
        except Exception:
            pass
        return {"ok": True, "briefing": "", "decisions": []}
    return {"ok": True, "briefing": payload["briefing"], "decisions": payload["decisions"]}
