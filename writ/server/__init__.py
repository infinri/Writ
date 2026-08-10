"""Writ HTTP API -- FastAPI service (package facade).

Per PY-ASYNC-001: all endpoints are async.
Per PERF-IO-001: no sync I/O in request handlers. Pipeline uses pre-warmed indexes.
Per PY-PYDANTIC-001: request/response bodies validated through Pydantic models.

W2 split (branch refactor/w2-server-split): this file IS `writ.server`
(sys.modules["writ.server"]). It owns the shared daemon state, the app lifecycle,
the single writ_session load, the shared format-stream lock, and re-exports the
request models. All 45 `@app` routes moved to per-domain APIRouter modules under
writ/server/routes/, wired in at the BOTTOM via `app.include_router(...)`.
Router handlers read the mutable/monkeypatched names below via live
`server.<attr>` access, so the lifespan reassignment and every test monkeypatch
are observed (the monkeypatch seam).
"""

# writ-auth-scan: internal-service localhost-only
# The Writ session daemon binds to 127.0.0.1:8765 and is reached only by local
# Claude Code hooks; it has no external network surface and intentionally runs
# without per-route auth. This marker suppresses the route-auth heuristic for
# this file in bin/run-analysis.sh; all other security analyzers still apply.

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from writ.analysis.friction import log_friction_event, resolve_log_path
from writ.analysis.instrumentation import Instrumentation
from writ.analysis.llm import LlmAnalyzer
from writ.config import (
    get_authority_preference_threshold,
    get_neo4j_uri,
    get_neo4j_user,
    get_neo4j_password,
)
from writ.graph.db import Neo4jConnection
from writ.graph.predicates import INJECTION_RULE_WHERE
from writ.retrieval.pipeline import (
    RULE_INJECTION_ABSTENTION_THRESHOLD,
    RetrievalPipeline,
    build_pipeline,
)
from writ.retrieval.trigger_index import MethodologyTriggerIndex
from writ.shared.logging import emit
from writ.shared.tokens import estimate_tokens
# POL-6-A3: single source the mode vocabulary. mode_engine.VALID_MODES is a real, normally-
# imported module, so this binds a real set even when route tests mock the path-loaded
# writ_session (patch("writ.server.writ_session", ...)) -- which never exposed VALID_MODES.
from writ.session.approval_workflow import _validate_phase_a, apply_phase_advance
from writ.session.commit_capture import capture_commit
from writ.session.decision_capture import capture_decision_at_approve
from writ.session.gate_token import (
    # _claim_token_mutex is deliberately NOT re-exported here. This import list is the
    # route layer's monkeypatch seam, and the advance route's unbound-token fallback (the
    # only thing that ever reached the bare mutex through it) is gone: every route claim
    # now goes through claim_gate_token, binding and all. Leaving the primitive on the
    # seam would advertise a route-level claim that skips the binding check, which is the
    # fail-open shape this cycle removed. It stays public within writ.session.gate_token,
    # where claim_gate_token is built on it and its own tests point at it directly.
    claim_gate_token,
    consume_gate_token,
    gate_binding_refusal,
    gate_token_valid,
    read_gate_binding,
    read_gate_token,
)
from writ.session.locators import _find_plan_md, plan_md_hash
from writ.session.mode_engine import (
    MODE_CONFIG,
    VALID_MODES,
    _next_pending_gate,
)

# Package-location anchors, computed ONCE. writ/server/__init__.py -> writ/server
# -> writ -> repo root. These re-derive the __file__-relative paths that changed
# depth when server.py became the writ/server/ package.
_PKG_DIR: Path = Path(__file__).resolve().parent          # writ/server
_WRIT_DIR: Path = _PKG_DIR.parent                          # writ
_REPO_ROOT: Path = _WRIT_DIR.parent                        # repo root

# 6.3c: canonical bible source root (repo_root/bible) -- where the human promotion gate
# writes a graduated node's markdown home.
_BIBLE_DIR: Path = _REPO_ROOT / "bible"

# The interactive-showcase SPA lives in a separate file so the page can be tweaked
# without restarting the daemon (routes/explorer.py reads it at request time).
_EXPLORE_HTML_PATH: Path = _WRIT_DIR / "static" / "explore.html"

# Load writ-session.py as a module for session route handlers. SINGLE load site;
# handlers read it as server.writ_session (live) so the monkeypatch seam holds.
_WRIT_SESSION_PATH = _REPO_ROOT / "bin" / "lib" / "writ-session.py"
if _WRIT_SESSION_PATH.exists():
    _spec = importlib.util.spec_from_file_location("writ_session", str(_WRIT_SESSION_PATH))
    writ_session = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(writ_session)  # type: ignore[union-attr]
else:
    writ_session = None  # type: ignore[assignment]


def _validate_context_window_env() -> None:
    """Log a warning when WRIT_CONTEXT_WINDOW_TOKENS is unset or out of range.

    Range: [1000, 10000000]. Does not block boot; the watcher hook applies the
    200000 default independently. This helps operators detect typos in their
    env config without breaking startup.
    """
    import logging
    raw = os.environ.get("WRIT_CONTEXT_WINDOW_TOKENS")
    logger = logging.getLogger("writ.server")
    if raw is None or raw == "":
        logger.warning(
            "WRIT_CONTEXT_WINDOW_TOKENS not set; context-watcher hook will "
            "use default window of 200000 tokens."
        )
        return
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "WRIT_CONTEXT_WINDOW_TOKENS=%r is not a valid integer; "
            "watcher hook will use default window of 200000 tokens.",
            raw,
        )
        return
    if value < 1000 or value > 10_000_000:
        logger.warning(
            "WRIT_CONTEXT_WINDOW_TOKENS=%d is outside [1000, 10000000]; "
            "watcher hook will use default window of 200000 tokens.",
            value,
        )


# Module-level state set during lifespan.
_pipeline: RetrievalPipeline | None = None
_db: Neo4jConnection | None = None
_startup_time: datetime | None = None
_llm_client: LlmAnalyzer | None = None
_instrumentation: Instrumentation | None = None
# 1.6 methodology-trigger index (CHANNEL 2: methodology by workflow-state).
_trigger_index: MethodologyTriggerIndex | None = None

# Serializes the global sys.stdin/sys.stdout swap in /session/format. Without it,
# two concurrent format calls running in the asyncio.to_thread worker pool would
# race on the process-global streams (interleaved swap + wrong restoration).
_FORMAT_STREAM_LOCK = threading.Lock()


def _run_cmd_format_locked(payload: Any) -> str:
    """Run writ_session.cmd_format() with a serialized stdin/stdout swap; return raw output.

    cmd_format() reads sys.stdin / writes sys.stdout, which are process-global, so the
    swap MUST hold _FORMAT_STREAM_LOCK: /session/format and /pre-write-check both run in
    the to_thread pool and would otherwise race and corrupt output (the C1 bug). The lock
    lives INSIDE this helper so no caller can hold/forget it -- never acquire it caller-side
    (the lock is non-reentrant; a caller holding it across this call would deadlock).

    There is exactly ONE instance of this helper (and _FORMAT_STREAM_LOCK): routers call
    server._run_cmd_format_locked so the concurrency guarantee is shared, not duplicated.
    The body reads `writ_session` from THIS namespace at call time, honoring the monkeypatch
    seam.
    """
    import io
    import json as json_mod
    with _FORMAT_STREAM_LOCK:
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json_mod.dumps(payload))
        try:
            old_stdout = sys.stdout
            sys.stdout = buf = io.StringIO()
            try:
                writ_session.cmd_format()
            except SystemExit:
                pass
            finally:
                sys.stdout = old_stdout
            raw = buf.getvalue()
        finally:
            sys.stdin = old_stdin
    return raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-warm all indexes at startup per PERF-IO-001."""
    global _pipeline, _db, _startup_time, _llm_client, _instrumentation, _trigger_index

    # WRIT_CONTEXT_WINDOW_TOKENS sanity check. Log-only warning; the watcher
    # hook defaults to 200000 when the env var is missing or unparseable.
    _validate_context_window_env()

    _db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    # Daemon is the production rule-injection path: enable the S4 abstention gate.
    _pipeline = await build_pipeline(
        _db, abstention_threshold=RULE_INJECTION_ABSTENTION_THRESHOLD,
        # Read once at startup, not per query: the hot /query path must not touch
        # the filesystem. Defaults to 0.0 (pass disabled) when unconfigured.
        authority_preference_threshold=get_authority_preference_threshold(),
    )
    _trigger_index = await MethodologyTriggerIndex.build_from_db(_db)
    _llm_client = LlmAnalyzer()
    _instrumentation = Instrumentation()
    _startup_time = datetime.now()
    yield
    if _db is not None:
        await _db.close()


app = FastAPI(
    title="Writ",
    description="Hybrid RAG knowledge retrieval service for AI coding rule enforcement.",
    lifespan=lifespan,
)


# Routes whose volume would drown the stream without telling anyone anything. /health is
# polled by ensure-server.sh, the SessionStart hook, `writ doctor` and the test harness, so
# it can outnumber real traffic by an order of magnitude; a liveness probe's latency is not
# a signal anyone reads. Everything else is recorded, including errors.
_REQUEST_TELEMETRY_SKIP = frozenset({"/health"})


@app.middleware("http")
async def _request_telemetry(request, call_next):
    """Emit one `daemon_request` metrics event per request.

    Audit item F: the daemon had 17 domain events (phase_advance, candidate_promoted and
    so on) but nothing per REQUEST, so latency, status and error rate per route were
    unobservable for all ~30 routes -- including /query and /pre-write-check, which every
    hook calls on every turn. A slow or 500-ing daemon was visible only as hooks
    mysteriously degrading, since they all fail open by design.

    Recorded on the way out, including when the handler raises, then the exception is
    re-raised untouched. The emit itself can never affect the response: writ.shared.logging
    .emit is documented never to raise, and it is wrapped anyway, because an observability
    concern must not be able to break the thing it observes.
    """
    start = time.perf_counter()
    status = 500          # what the client gets if the handler raises
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            path = request.url.path
            if path not in _REQUEST_TELEMETRY_SKIP:
                # The matched ROUTE, not the concrete path: /session/{session_id}/mode
                # keeps one identity instead of splintering into one per session id, which
                # is what makes the rows aggregatable. Set by the router, so it is absent
                # on a 404.
                route = request.scope.get("route")
                emit(
                    "metrics",
                    "daemon_request",
                    (request.scope.get("path_params") or {}).get("session_id", "") or "",
                    None,
                    route=getattr(route, "path_format", None) or path,
                    method=request.method,
                    status=status,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )
        except Exception:  # noqa: BLE001 - telemetry must never mask the real outcome
            pass


# ---------------------------------------------------------------------------
# Re-export the Pydantic request models so `from writ.server import QueryRequest`
# etc. keep working (models.py has zero writ.server deps -> no import cycle).
# ---------------------------------------------------------------------------
from writ.server.models import (  # noqa: E402
    CommitCaptureRequest,
    CompanionRequest,
    ConflictsRequest,
    ContextPercentRequest,
    FeedbackRequest,
    GitHooksAutoInstallRequest,
    PreWriteCheckRequest,
    PromptBundleRequest,
    ProposeRequest,
    QueryRequest,
    RecallRequest,
    SessionActivePlaybookRequest,
    SessionAddViolationRequest,
    SessionAdvancePhaseRequest,
    SessionAutoFeedbackRequest,
    SessionCanWriteRequest,
    SessionFormatRequest,
    SessionInvalidateGateRequest,
    SessionModeSetRequest,
    SessionPromoteCandidateRequest,
    SessionQualityJudgmentRequest,
    SessionUpdateRequest,
    SessionVerificationEvidenceRequest,
)

# ---------------------------------------------------------------------------
# Router wiring (LAST): the routers do `import writ.server as server` at module
# top and touch server.<attr> only inside handler bodies, so importing them here
# -- after all shared state, helpers, and model re-exports are defined -- is safe
# against the partially-initialized module. Then re-export the handler functions
# and helpers tests import by name from writ.server.
# ---------------------------------------------------------------------------
from writ.server.routes.query import router as query_router  # noqa: E402
from writ.server.routes.session_state import router as session_state_router  # noqa: E402
from writ.server.routes.gate import router as gate_router  # noqa: E402
from writ.server.routes.decision_memory import router as decision_memory_router  # noqa: E402
from writ.server.routes.git_hooks import router as git_hooks_router  # noqa: E402
from writ.server.routes.explorer import router as explorer_router  # noqa: E402

app.include_router(query_router)
app.include_router(session_state_router)
app.include_router(gate_router)
app.include_router(decision_memory_router)
app.include_router(git_hooks_router)
app.include_router(explorer_router)

from writ.server.routes.query import methodology_companion, _health_status  # noqa: E402,F401
from writ.server.routes.gate import session_advance_phase, pre_write_check  # noqa: E402,F401
