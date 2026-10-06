"""Live reload of the daemon's retrieval state (program item 2).

The daemon built its pipeline and trigger index once, in the lifespan, so an edited rule,
a new edge or a promoted node reached retrieval only after a restart. This module owns
the rebuild and the swap:

- RetrievalHandle is ONE generation: the pipeline and the trigger index, built from the
  same graph read and installed together, never one without the other.
- build_retrieval_handle reads the graph on the event loop (the driver is bound to it),
  fingerprints the result, and runs the CPU and disk build in a worker thread only when
  the fingerprint moved.
- RetrievalReloader serializes rebuilds behind one asyncio.Lock and coalesces requests
  that arrive while one runs. A failed rebuild leaves the previous generation serving
  and is reported in the outcome, never raised to the requester.

Nothing here imports writ.server. The package facade passes in the callables that read
and install the live handle, which keeps the module-global monkeypatch seam
(server._pipeline, server._trigger_index) intact for every route and test.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

from writ.retrieval.pipeline import (
    RetrievalPipeline,
    assemble_pipeline,
    fingerprint_digest,
    load_pipeline_inputs,
    pipeline_fingerprint,
)
from writ.retrieval.trigger_index import MethodologyTriggerIndex
from writ.shared.logging import emit, emit_exception

if TYPE_CHECKING:
    from writ.graph.db import GraphConnection

ReloadStatus = Literal["swapped", "unchanged", "failed"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


@dataclass(frozen=True)
class RetrievalHandle:
    """One served generation of retrieval state."""

    generation: int
    pipeline: RetrievalPipeline
    trigger_index: MethodologyTriggerIndex
    fingerprint: dict[str, str]
    built_at: str


@dataclass(frozen=True)
class ReloadOutcome:
    """What one reload did. `error` is set only when status is "failed"."""

    status: ReloadStatus
    generation: int
    previous_generation: int
    duration_ms: float
    at: str
    changed: tuple[str, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. The `error` key appears only on failure: the daemon's callers
        read a body carrying `error` as a failure (docs/reference/http-api.md)."""
        body: dict[str, Any] = {
            "status": self.status,
            "generation": self.generation,
            "previous_generation": self.previous_generation,
            "duration_ms": self.duration_ms,
            "at": self.at,
            "changed": list(self.changed),
        }
        if self.error is not None:
            body["error"] = self.error
        return body


class HandleBuilder(Protocol):
    """Builds the next generation, or returns None when the graph content is unchanged."""

    async def __call__(
        self, previous: RetrievalHandle | None, generation: int,
    ) -> RetrievalHandle | None: ...


async def build_retrieval_handle(
    db: GraphConnection,
    previous: RetrievalHandle | None,
    generation: int,
    *,
    abstention_threshold: float,
    authority_preference_threshold: float,
) -> RetrievalHandle | None:
    """Read, fingerprint, and build generation `generation`, or None if nothing changed.

    The graph reads stay on the event loop that owns the driver. Only assemble_pipeline
    (BM25 and HNSW load-or-build, the encoder) runs in a worker thread, so queries keep
    being served from `previous` while it runs. The running encoder is reused: the model
    is loaded once per process and its query cache survives the swap.
    """
    inputs = await load_pipeline_inputs(db)
    trigger_index = await MethodologyTriggerIndex.build_from_db(db)
    fingerprint = {
        **pipeline_fingerprint(inputs),
        "triggers": fingerprint_digest(trigger_index.snapshot()),
    }
    if previous is not None and previous.fingerprint == fingerprint:
        return None
    pipeline = await asyncio.to_thread(
        assemble_pipeline,
        inputs,
        embedding_model=previous.pipeline.encoder if previous is not None else None,
        abstention_threshold=abstention_threshold,
        authority_preference_threshold=authority_preference_threshold,
    )
    return RetrievalHandle(
        generation=generation,
        pipeline=pipeline,
        trigger_index=trigger_index,
        fingerprint=fingerprint,
        built_at=_now(),
    )


class RetrievalReloader:
    """Serializes rebuilds, coalesces the requests that queue behind one, keeps the last
    good generation when a rebuild fails.

    Coalescing: every request takes a ticket. A rebuild that starts inside the lock covers
    every ticket issued before it started, because it reads the graph after all of them.
    A waiter whose ticket is already covered returns that rebuild's outcome instead of
    building again, so N requests during one rebuild cost at most one more.
    """

    def __init__(
        self,
        builder: HandleBuilder,
        current: Callable[[], RetrievalHandle | None],
        install: Callable[[RetrievalHandle], None],
    ) -> None:
        self._builder = builder
        self._current = current
        self._install = install
        self._lock = asyncio.Lock()
        self._requested = 0
        self._covered = 0
        self._last: ReloadOutcome | None = None
        self._followups: set[asyncio.Task] = set()

    @property
    def in_progress(self) -> bool:
        return self._lock.locked()

    @property
    def last_outcome(self) -> ReloadOutcome | None:
        return self._last

    async def start(self) -> RetrievalHandle:
        """Build and install generation 1. Raises: a daemon that cannot build once must
        not start, exactly as before this module existed."""
        started = time.perf_counter()
        handle = await self._builder(None, 1)
        if handle is None:
            raise RuntimeError("the first retrieval build returned no handle")
        self._install(handle)
        self._last = ReloadOutcome(
            "swapped", handle.generation, 0, _elapsed_ms(started), _now(),
            changed=tuple(sorted(handle.fingerprint)),
        )
        return handle

    async def request(self) -> ReloadOutcome:
        """Reload now, or join the reload that will cover this request."""
        self._requested += 1
        ticket = self._requested
        async with self._lock:
            if self._covered >= ticket and self._last is not None:
                return self._last
            covers = self._requested
            outcome = await self._run()
            self._covered = covers
            self._last = outcome
            return outcome

    def request_soon(self) -> None:
        """Queue one coalesced reload behind a running one. A write that landed while a
        rebuild was reading the graph may be missing from what it swaps in."""
        if not self.in_progress:
            return
        task = asyncio.get_running_loop().create_task(self.request())
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    def cancel_pending(self) -> None:
        """Cancel queued follow-ups (daemon shutdown)."""
        for task in list(self._followups):
            task.cancel()

    async def _run(self) -> ReloadOutcome:
        started = time.perf_counter()
        previous = self._current()
        previous_generation = previous.generation if previous is not None else 0
        try:
            handle = await self._builder(previous, previous_generation + 1)
        except Exception as exc:
            emit_exception(
                "server.retrieval_reload", exc, "", None, generation=previous_generation,
            )
            outcome = ReloadOutcome(
                "failed", previous_generation, previous_generation,
                _elapsed_ms(started), _now(), error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if handle is None:
                outcome = ReloadOutcome(
                    "unchanged", previous_generation, previous_generation,
                    _elapsed_ms(started), _now(),
                )
            else:
                self._install(handle)
                before = previous.fingerprint if previous is not None else {}
                changed = tuple(sorted(
                    key for key, value in handle.fingerprint.items() if before.get(key) != value
                ))
                outcome = ReloadOutcome(
                    "swapped", handle.generation, previous_generation,
                    _elapsed_ms(started), _now(), changed=changed,
                )
        emit("metrics", "retrieval_reload", "", None, **outcome.as_dict())
        return outcome
