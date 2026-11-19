"""Anthropic provider, through the official `anthropic` SDK (DESIGN.md section 6).

Rules this file follows for Claude Opus 5.5 (from Anthropic's migration notes):
  - thinking is always on; the `thinking` field is omitted and `output_config.effort` sets how much
  - every reply is streamed (long replies would otherwise hit request time limits); max_tokens covers
    the thinking as well as the visible reply
  - an assistant turn is sent back exactly as it came, every block in order, including the hidden
    reasoning blocks. ModelResponse.raw carries those blocks; the agent loop stores them on the message
  - forced tool choice is not allowed; requests leave tool_choice at its default ("auto")
  - a safety refusal (stop_reason "refusal") is a result, not an exception. One-shot calls opt into
    Anthropic's server-side fallback, which re-runs a refused request on another model; agent loops
    do not, because replaying a conversation after a fallback has its own rules
Retries happen in the Gateway, so the SDK's own retries are switched off.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import BudgetExceeded
from .types import ModelRequest, ModelResponse, ProviderError, ToolCall, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES = {
    "claude-opus-5-5": Price(4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": Price(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": Price(5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5-5": Price(2.0, 10.0, 2.5, 0.20),
}


def _price(model: str) -> Price:
    return PRICES.get(model) or PRICES["claude-opus-5-5"]


def cost_of(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    p = _price(model)
    return (input_tokens * p.input + output_tokens * p.output + cache_write * p.cache_write
            + cache_read * p.cache_read) / 1e6


def to_anthropic_messages(messages) -> list[dict]:
    """Neutral format -> Anthropic format. Consecutive tool results become one user message of
    tool_result blocks; an assistant turn with `raw` blocks is sent back exactly as received."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            raw = m.get("raw")
            if raw:
                out.append({"role": "assistant", "content": list(raw)})
            else:
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": dict(c["arguments"])}
                           for c in m.get("tool_calls") or []]
                out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            raise ValueError(f"unknown message role {role!r}")
    return out


def _block_dict(block) -> dict:
    return block.to_dict(mode="json", exclude_none=True)


class AnthropicProvider:
    def __init__(self, api_key: str | None = None, *, client=None, timeout_s: float = 900.0,
                 fallback_for_one_shot: bool = True):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)
        self.client = client
        self.fallback_for_one_shot = fallback_for_one_shot

    def estimate_cost(self, request: ModelRequest) -> float:
        chars = len(request.system) + sum(len(json.dumps(m, default=str)) for m in request.messages)
        return cost_of(request.model_name, chars // 3, min(request.max_tokens, 12000))

    def call(self, request: ModelRequest, *, role: str) -> ModelResponse:
        import anthropic
        params = dict(request.params)
        kw: dict[str, Any] = {
            "model": request.model_name,
            "max_tokens": request.max_tokens,
            "messages": to_anthropic_messages(request.messages),
            "output_config": {"effort": params.pop("effort", "medium")},
        }
        if request.system:
            kw["system"] = request.system
        if request.tools:
            kw["tools"] = [{"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                           for t in request.tools]
        if params.pop("cache", False):
            kw["cache_control"] = {"type": "ephemeral"}      # agent loops re-send a growing prefix every turn
        one_shot = not request.tools and len(request.messages) <= 3
        use_fallback = self.fallback_for_one_shot and one_shot
        try:
            if use_fallback:
                with self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kw) as stream:
                    message = stream.get_final_message()
            else:
                with self.client.messages.stream(**kw) as stream:
                    message = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise ProviderError(f"{type(e).__name__}: {e}", retryable=True, rate_limited=True) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError) as e:
            raise ProviderError(f"{type(e).__name__}: {e}", retryable=True) from e
        except anthropic.APIStatusError as e:
            text = str(getattr(e, "message", e))
            if "credit balance" in text.lower():
                raise BudgetExceeded("the Anthropic account balance is too low - top up, then raise the budget "
                                     "to resume") from e
            retryable = e.status_code in (408, 409, 429) or e.status_code >= 500
            raise ProviderError(f"{e.status_code}: {text[:500]}", retryable=retryable,
                                rate_limited=e.status_code == 429) from e
        return self._response(request, message)

    def _response(self, request: ModelRequest, message) -> ModelResponse:
        blocks = list(message.content)
        text = "".join(b.text for b in blocks if b.type == "text")
        calls = tuple(ToolCall(b.id, b.name, dict(b.input)) for b in blocks if b.type == "tool_use")
        u = message.usage
        iterations = getattr(u, "iterations", None) or []
        attempts = [it for it in iterations if getattr(it, "model", None)] or [None]
        cost = 0.0
        for it in attempts:
            src = it or u
            model = getattr(it, "model", None) or message.model or request.model_name
            cost += cost_of(model, src.input_tokens or 0, src.output_tokens or 0,
                            src.cache_creation_input_tokens or 0, src.cache_read_input_tokens or 0)
        refusal = None
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            refusal = getattr(details, "category", None) or "unspecified"
        return ModelResponse(
            text=text, tool_calls=calls, stop_reason=message.stop_reason or "",
            usage=Usage(input_tokens=(u.input_tokens or 0) + (u.cache_creation_input_tokens or 0)
                        + (u.cache_read_input_tokens or 0),
                        output_tokens=u.output_tokens or 0,
                        cache_read_tokens=u.cache_read_input_tokens or 0,
                        cache_write_tokens=u.cache_creation_input_tokens or 0),
            cost_usd=round(cost, 8), raw=tuple(_block_dict(b) for b in blocks),
            served_by=message.model or request.model_name, refusal=refusal)


def quick_check(provider: AnthropicProvider, model: str) -> Mapping[str, Any]:
    """Used by the smoke test: one tiny call."""
    r = provider.call(ModelRequest(model=f"anthropic/{model}", max_tokens=2000, params={"effort": "low"},
                                   messages=({"role": "user", "content": 'Reply with exactly {"ok": true}'},)),
                      role="smoke")
    return {"text": r.text, "cost_usd": r.cost_usd, "usage": r.usage, "served_by": r.served_by}
