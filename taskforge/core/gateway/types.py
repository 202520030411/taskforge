"""Provider-neutral request and response shapes.

Messages use one neutral format; provider adapters (P3) translate it:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
  {"role": "tool", "tool_call_id": str, "content": str}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]   # JSON schema of the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    model: str                                  # "<provider>/<model id>", e.g. "anthropic/claude-opus-5-5"
    messages: tuple[Mapping[str, Any], ...]
    system: str = ""
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 16000
    params: Mapping[str, Any] = field(default_factory=dict)   # sent to the provider (temperature, effort ...)
    tags: Mapping[str, Any] = field(default_factory=dict)     # never sent; for logs and fake providers
    prompt_version: str = ""

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        return self.model.split("/", 1)[1] if "/" in self.model else self.model


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0            # all input tokens, cached or not
    output_tokens: int = 0           # includes hidden thinking
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    usage: Usage = Usage()
    cost_usd: float = 0.0
    raw: tuple[Mapping[str, Any], ...] | None = None   # provider blocks to send back unchanged (Anthropic)
    served_by: str = ""                                 # the model that actually answered (fallbacks)
    refusal: str | None = None                          # refusal category, when the model declined


class ProviderError(Exception):
    """retryable: try again later. unavailable: the provider cannot serve this model at all right now (for
    example the pinned host vanished); callers wait for it without using up attempts. rate_limited: the
    provider said "too many requests" (HTTP 429); callers slow down without using up attempts either."""

    def __init__(self, message: str, *, retryable: bool, unavailable: bool = False, rate_limited: bool = False):
        super().__init__(message)
        self.retryable = retryable or unavailable or rate_limited
        self.unavailable = unavailable
        self.rate_limited = rate_limited


class Provider(Protocol):
    def estimate_cost(self, request: ModelRequest) -> float: ...
    def call(self, request: ModelRequest, *, role: str) -> ModelResponse: ...


class BudgetGuard(Protocol):
    def reserve(self, scope, estimate_usd: float) -> Any: ...      # returns a token for settle()
    def settle(self, token: Any, actual_usd: float) -> None: ...
