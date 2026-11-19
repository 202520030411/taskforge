"""A scripted stand-in for a model provider. Used in tests and in every P1 run: no money is spent."""
from __future__ import annotations

import dataclasses
import json
import random
import time
from typing import Any, Callable, Mapping

from .types import ModelRequest, ModelResponse, ProviderError, ToolCall, Usage

Script = Callable[[str, ModelRequest], "ModelResponse | Mapping[str, Any] | str"]


class FakeProvider:
    """`script(role, request)` decides the reply. A dict is returned to the caller as JSON text."""

    def __init__(self, script: Script, *, cost_per_call: float = 0.0, latency_s: tuple[float, float] | None = None,
                 faults: Callable[[], Mapping[str, Any]] | None = None, record: bool = True):
        """latency_s: each call waits a random time in this range, like a real API (load tests).
        faults(): the faults to inject right now, e.g. {"rate_limit_share": 0.5} refuses half of all calls
        with a retryable "429 too many requests" error (failure drills).
        record=False keeps no list of calls (long load tests would otherwise fill memory)."""
        self.script = script
        self.cost_per_call = cost_per_call
        self.latency_s = latency_s
        self.faults = faults
        self.record = record
        self.calls: list[tuple[str, ModelRequest]] = []

    def estimate_cost(self, request: ModelRequest) -> float:
        return self.cost_per_call

    def call(self, request: ModelRequest, *, role: str) -> ModelResponse:
        if self.latency_s:
            time.sleep(random.uniform(*self.latency_s))
        share = float((self.faults() if self.faults else {}).get("rate_limit_share", 0.0))
        if share and random.random() < share:
            raise ProviderError("429: too many requests (injected fault)", retryable=True, rate_limited=True)
        if self.record:
            self.calls.append((role, request))
        out = self.script(role, request)
        if isinstance(out, ModelResponse):
            resp = out
        elif isinstance(out, str):
            resp = ModelResponse(text=out)
        else:
            resp = ModelResponse(text=json.dumps(out))
        if resp.usage == Usage():
            text_len = sum(len(str(m.get("content", ""))) for m in request.messages) + len(request.system)
            resp = dataclasses.replace(resp, usage=Usage(text_len // 4, len(resp.text) // 4 + 20 * len(resp.tool_calls)))
        return dataclasses.replace(resp, cost_usd=self.cost_per_call)


def tool_reply(*calls: tuple[str, Mapping[str, Any]], text: str = "") -> ModelResponse:
    """Build a reply that calls tools, e.g. tool_reply(("run_shell", {"command": "ls"}))."""
    return ModelResponse(text=text, tool_calls=tuple(ToolCall(f"call_{i}", name, dict(args))
                                                     for i, (name, args) in enumerate(calls)),
                         stop_reason="tool_use")


def turn_number(request: ModelRequest) -> int:
    """How many assistant turns came before this request (0 for the first call of an agent loop)."""
    return sum(1 for m in request.messages if m.get("role") == "assistant")
