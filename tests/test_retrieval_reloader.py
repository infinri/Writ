"""Program item 2: the reloader's ordering contract, with an injected builder.

The builder is the HandleBuilder protocol, so a plain async function stands in for the
graph. Proven here: start, swap, unchanged, keep-old-on-failure, coalescing, the queued
follow-up, and the metrics row. Whether a real rebuild sees a real edit is
tests/test_live_reload.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from writ.server.reload import RetrievalHandle, RetrievalReloader


def _handle(generation: int, tag: str = "a") -> RetrievalHandle:
    return RetrievalHandle(
        generation=generation, pipeline=object(), trigger_index=object(),
        fingerprint={"bm25": tag, "metadata": tag, "edges": "same"}, built_at="t",
    )


class _Slot:
    def __init__(self) -> None:
        self.handle: RetrievalHandle | None = None

    def current(self) -> RetrievalHandle | None:
        return self.handle

    def install(self, handle: RetrievalHandle) -> None:
        self.handle = handle


def _reloader(builder, slot: _Slot) -> RetrievalReloader:
    return RetrievalReloader(builder, current=slot.current, install=slot.install)


async def _until(predicate, limit: int = 200) -> None:
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_start_installs_generation_one() -> None:
    slot = _Slot()

    async def build(previous, generation):
        return _handle(generation)

    reloader = _reloader(build, slot)
    handle = await reloader.start()
    assert slot.handle is handle and handle.generation == 1
    assert reloader.last_outcome.status == "swapped"


@pytest.mark.asyncio
async def test_start_raises_when_the_first_build_fails() -> None:
    async def build(previous, generation):
        raise RuntimeError("no graph")

    with pytest.raises(RuntimeError):
        await _reloader(build, _Slot()).start()


@pytest.mark.asyncio
async def test_a_changed_build_is_swapped_in_and_names_what_moved() -> None:
    slot = _Slot()

    async def build(previous, generation):
        return _handle(generation, tag="a" if previous is None else "b")

    reloader = _reloader(build, slot)
    await reloader.start()
    outcome = await reloader.request()
    assert (outcome.status, outcome.generation, outcome.previous_generation) == ("swapped", 2, 1)
    assert outcome.changed == ("bm25", "metadata")
    assert slot.handle.generation == 2
    assert "error" not in outcome.as_dict()


@pytest.mark.asyncio
async def test_an_unchanged_graph_keeps_the_same_handle() -> None:
    slot = _Slot()

    async def build(previous, generation):
        return None if previous is not None else _handle(generation)

    reloader = _reloader(build, slot)
    first = await reloader.start()
    outcome = await reloader.request()
    assert (outcome.status, outcome.generation) == ("unchanged", 1)
    assert slot.handle is first


@pytest.mark.asyncio
async def test_a_failed_build_keeps_the_previous_handle_and_reports() -> None:
    slot = _Slot()

    async def build(previous, generation):
        if previous is not None:
            raise RuntimeError("boom")
        return _handle(generation)

    reloader = _reloader(build, slot)
    first = await reloader.start()
    outcome = await reloader.request()
    assert (outcome.status, outcome.generation) == ("failed", 1)
    assert "boom" in outcome.as_dict()["error"]
    assert slot.handle is first
    assert reloader.last_outcome is outcome


@pytest.mark.asyncio
async def test_requests_during_a_build_coalesce_into_one_more_build() -> None:
    slot = _Slot()
    gate = asyncio.Event()
    calls: list[int] = []

    async def build(previous, generation):
        calls.append(generation)
        if len(calls) == 2:
            await gate.wait()
        return _handle(generation, tag=str(generation))

    reloader = _reloader(build, slot)
    await reloader.start()
    first = asyncio.create_task(reloader.request())
    await _until(lambda: len(calls) == 2)
    rest = [asyncio.create_task(reloader.request()) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(first, *rest)

    assert calls == [1, 2, 3], "expected start, the blocked build, and ONE coalesced build"
    assert outcomes[0].generation == 2
    assert {o.generation for o in outcomes[1:]} == {3}
    assert slot.handle.generation == 3


@pytest.mark.asyncio
async def test_request_soon_is_a_no_op_when_idle() -> None:
    calls: list[int] = []

    async def build(previous, generation):
        calls.append(generation)
        return _handle(generation)

    reloader = _reloader(build, _Slot())
    await reloader.start()
    reloader.request_soon()
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == [1]


@pytest.mark.asyncio
async def test_request_soon_queues_one_follow_up_behind_a_running_build() -> None:
    gate = asyncio.Event()
    calls: list[int] = []

    async def build(previous, generation):
        calls.append(generation)
        if len(calls) == 2:
            await gate.wait()
        return _handle(generation, tag=str(generation))

    slot = _Slot()
    reloader = _reloader(build, slot)
    await reloader.start()
    running = asyncio.create_task(reloader.request())
    await _until(lambda: len(calls) == 2)
    reloader.request_soon()
    gate.set()
    await running
    await _until(lambda: len(calls) == 3 and not reloader.in_progress)
    assert slot.handle.generation == 3


@pytest.mark.asyncio
async def test_every_run_writes_one_metrics_row(tmp_path, monkeypatch) -> None:
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))

    async def build(previous, generation):
        return None if previous is not None else _handle(generation)

    reloader = _reloader(build, _Slot())
    await reloader.start()
    await reloader.request()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    reload_rows = [r for r in rows if r.get("event") == "retrieval_reload"]
    assert len(reload_rows) == 1
    assert reload_rows[0].get("status") == "unchanged"
