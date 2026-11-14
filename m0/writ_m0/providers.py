"""Stub ModelAdapters for managed-agent mode and capability negotiation. Non-production (D-001).

Two stubs with deliberately different feature sets exercise the ModelEvent
contract without credentials or network. Negotiation is explicit: a missing
feature or a region outside the tenant's allowance raises UnsupportedCapability
rather than silently degrading (P-18, INV-26).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from . import contracts

STREAM_CHUNK_CHARS = 8


class UnsupportedCapability(Exception):
    def __init__(self, code: str, missing: list[str]):
        super().__init__(f"{code}: {missing}")
        self.code = code
        self.missing = missing


class ModelAdapter(Protocol):
    def describe(self) -> dict: ...
    def generate(self, request: dict, cancel: threading.Event | None = None) -> Iterator[dict]: ...


@dataclass
class ScriptedTurn:
    text: str = ""
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)  # (call_id, name, arguments_json)


def negotiate(capabilities: dict, required_features: list[str], allowed_regions: list[str] | None = None) -> None:
    features = capabilities["features"]
    missing = sorted(f for f in required_features if features.get(f) is not True)
    if missing:
        raise UnsupportedCapability("UNSUPPORTED_FEATURE", missing)
    if allowed_regions is not None and not set(capabilities.get("regions", [])) & set(allowed_regions):
        raise UnsupportedCapability("REGION_NOT_ALLOWED", sorted(allowed_regions))


def select_adapter(adapters: list[ModelAdapter], required_features: list[str],
                   allowed_regions: list[str] | None = None) -> ModelAdapter:
    """First adapter satisfying every requirement; never a weaker fallback."""
    reasons = []
    for adapter in adapters:
        try:
            negotiate(adapter.describe(), required_features, allowed_regions)
            return adapter
        except UnsupportedCapability as exc:
            reasons.extend(exc.missing)
    raise UnsupportedCapability("NO_COMPATIBLE_PROVIDER", sorted(set(reasons)))


class _StubBase:
    fixture: str

    def __init__(self, script: list[ScriptedTurn]):
        self._script = list(script)
        self._capabilities = contracts.validate("model-capabilities",
                                                contracts.load_fixture(f"{self.fixture}.capabilities.json"))
        self.calls = 0

    def describe(self) -> dict:
        return self._capabilities

    def _event(self, request: dict, seq: int, kind: str, **fields) -> dict:
        event = {"contract_version": "2.0", "request_id": request["request_id"],
                 "event_id": f"{request['request_id']}:{seq}", "sequence": seq, "kind": kind} | fields
        return contracts.validate("model-event", event)

    def _next_turn(self, request: dict) -> ScriptedTurn:
        contracts.validate("model-request", request)
        if request.get("tool_descriptions"):
            negotiate(self._capabilities, ["tool_proposals"])
        self.calls += 1
        return self._script.pop(0) if self._script else ScriptedTurn(text="")


class StubStreamingAdapter(_StubBase):
    """Streams text in chunks, proposes tool calls, supports cancellation."""
    fixture = "model-stub-streaming"

    def generate(self, request: dict, cancel: threading.Event | None = None) -> Iterator[dict]:
        turn = self._next_turn(request)
        seq = 0
        for start in range(0, len(turn.text), STREAM_CHUNK_CHARS):
            if cancel is not None and cancel.is_set():
                yield self._event(request, seq, "provider_error", error_code="CANCELLED", retryable=False)
                return
            yield self._event(request, seq, "message_delta", text=turn.text[start:start + STREAM_CHUNK_CHARS])
            seq += 1
        for call_id, name, arguments in turn.tool_calls:
            yield self._event(request, seq, "tool_call_proposal",
                              call={"call_id": call_id, "name": name, "arguments_json": arguments})
            seq += 1
        yield self._event(request, seq, "usage", usage={"input_tokens": 10, "output_tokens": len(turn.text),
                                                        "estimated_cost_usd": 0})
        yield self._event(request, seq + 1, "completed", finish_reason="stop")


class StubLimitedAdapter(_StubBase):
    """No streaming, no tool proposals, no structured output: one message then completion."""
    fixture = "model-stub-limited"

    def generate(self, request: dict, cancel: threading.Event | None = None) -> Iterator[dict]:
        turn = self._next_turn(request)
        if turn.tool_calls:
            raise UnsupportedCapability("UNSUPPORTED_FEATURE", ["tool_proposals"])
        yield self._event(request, 0, "message_delta", text=turn.text)
        yield self._event(request, 1, "completed", finish_reason="stop")
