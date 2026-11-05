"""Minimal optional AgentRunner for managed-agent mode. Non-production (D-001).

The runner is unprivileged: a model's tool proposal becomes an ActionRequest
through the same HostAdapter normalization an external host uses, then goes to
the broker. Model text has no authority; there is no code path from model
output to an approval grant (INV-02, P-19).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from . import contracts
from .broker import Broker, BrokerRefusal, EffectAdapter
from .hosts import HostAdapter, NormalizationError
from .providers import ModelAdapter, negotiate


@dataclass
class RunResult:
    text: str = ""
    events: list[dict] = field(default_factory=list)
    receipts: list[dict] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


class AgentRunner:
    def __init__(self, *, adapter: ModelAdapter, normalizer: HostAdapter, broker: Broker, effect: EffectAdapter):
        self._adapter = adapter
        self._normalizer = normalizer
        self._broker = broker
        self._effect = effect

    def run(self, *, task: dict, proof: dict, session_id: str, tools: list[str], now: datetime,
            required_features: list[str] = ()) -> RunResult:
        contracts.validate("task-spec", task)
        caps = self._adapter.describe()
        negotiate(caps, list(required_features) + (["tool_proposals"] if tools else []))
        limit = task["limits"].get("max_model_calls", 1)
        result = RunResult()
        request = {
            "contract_version": "2.0", "request_id": f"{task['task_id']}:model:1", "trace_id": session_id,
            "tenant_id": task["tenant_id"], "project_id": task["project_id"],
            "model": {"provider_id": caps["provider_id"], "model_id": caps["model_id"]},
            "messages": [{"role": "user", "parts": [{"kind": "text", "text": task["goal"]}]}],
            "tool_descriptions": [{"name": t, "input_schema_ref": f"writ://tools/{t}/1"} for t in tools],
            "budget": {"max_output_tokens": caps["limits"]["output_tokens"]},
        }
        if limit < 1:
            result.refusals.append("MODEL_CALL_BUDGET_EXHAUSTED")
            return result
        last_seq = -1
        for event in self._adapter.generate(request):
            if event["sequence"] <= last_seq and event["kind"] != "completed":
                raise ValueError("model events out of order")
            last_seq = event["sequence"]
            result.events.append(event)
            if event["kind"] == "message_delta":
                result.text += event.get("text") or ""
            elif event["kind"] == "tool_call_proposal":
                self._handle_proposal(event["call"], proof, session_id, tools, now, result)
        return result

    def _handle_proposal(self, call: dict, proof: dict, session_id: str, tools: list[str], now: datetime,
                         result: RunResult) -> None:
        if call["name"] not in tools:
            result.refusals.append(f"TOOL_NOT_OFFERED:{call['name']}")
            return
        try:
            args = json.loads(call["arguments_json"])
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            sealed = self._normalizer.normalize(tool=call["name"], args=args, call_id=call["call_id"],
                                                 session_id=session_id, proof=proof, now=now)
        except (ValueError, NormalizationError) as exc:
            result.refusals.append(f"INVALID_TOOL_ARGUMENTS:{type(exc).__name__}")
            return
        try:
            result.receipts.append(self._broker.execute(sealed, self._effect, now))
        except BrokerRefusal as refusal:
            result.refusals.append(refusal.code)
