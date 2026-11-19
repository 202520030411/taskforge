"""OpenRouter provider (OpenAI-style chat completions) for the weak model under test (DESIGN.md section 6).

  - one host, no fallback to others: every attempt in a run comes from the same serving setup
  - the cost of each call is read from OpenRouter's reply (`usage.cost`), else computed from the price
  - an empty reply (no text and no tool call) is treated as a fault to retry; an earlier project lost
    attempts to a host that silently returned nothing
  - HTTP 402 (no credit left) pauses the run instead of failing steps
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..errors import BudgetExceeded
from .types import ModelRequest, ModelResponse, ProviderError, ToolCall, Usage

URL = "https://openrouter.ai/api/v1/chat/completions"


_BAD_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def clean_name(name: str) -> str:
    """Hosts such as Amazon Bedrock reject a whole request if any tool name in the history has other
    characters. A model sometimes glues markup to a name (`finish</function`); the call already failed as
    an unknown tool, so only the name sent back as history is cleaned."""
    return _BAD_NAME.sub("_", name or "")[:64] or "unknown_tool"


def to_openai_messages(system: str, messages) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": clean_name(c["name"]), "arguments": json.dumps(dict(c["arguments"]))}}
                                     for c in m["tool_calls"]]
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            raise ValueError(f"unknown message role {role!r}")
    return out


class OpenRouterProvider:
    def __init__(self, api_key: str, *, host: str, price_in: float, price_out: float, timeout_s: float = 600.0,
                 transport: httpx.BaseTransport | None = None):
        self.host = host                                       # e.g. "siliconflow/fp8"
        self.price_in, self.price_out = price_in, price_out   # dollars per million tokens, for estimates
        self.http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=15.0), transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}", "X-Title": "taskforge"})

    def estimate_cost(self, request: ModelRequest) -> float:
        chars = len(request.system) + sum(len(json.dumps(m, default=str)) for m in request.messages)
        return (chars // 3 * self.price_in + min(request.max_tokens, 4000) * self.price_out) / 1e6

    def call(self, request: ModelRequest, *, role: str) -> ModelResponse:
        body: dict[str, Any] = {
            "model": request.model_name,
            "messages": to_openai_messages(request.system, request.messages),
            "max_tokens": request.max_tokens,
            "provider": {"order": [self.host], "allow_fallbacks": False},
            "usage": {"include": True},
            **{k: v for k, v in request.params.items() if k in ("temperature", "top_p", "top_k")},
        }
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": dict(t.parameters)}} for t in request.tools]
        try:
            r = self.http.post(URL, json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"{type(e).__name__}: {e}", retryable=True) from e
        if r.status_code == 402:
            raise BudgetExceeded("the OpenRouter balance is too low - top up, then raise the budget to resume")
        if r.status_code == 404 and "No endpoints found" in r.text:
            raise ProviderError(f"host {self.host} is not serving this model: {r.text[:300]}", retryable=True,
                                unavailable=True)
        if r.status_code >= 400:
            raise ProviderError(f"{r.status_code}: {r.text[:500]}", retryable=r.status_code in (408, 429) or r.status_code >= 500,
                                rate_limited=r.status_code == 429)
        data = r.json()
        if "error" in data:                                   # errors can also arrive with HTTP 200
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            if "No endpoints found" in str(err):
                raise ProviderError(f"host {self.host} is not serving this model", retryable=True, unavailable=True)
            raise ProviderError(f"{code}: {str(err)[:500]}", retryable=code in (408, 429, 502, 503) or code is None,
                                rate_limited=code == 429)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"_raw": fn.get("arguments")}
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}            # the tool then answers "missing argument"
            calls.append(ToolCall(c.get("id") or f"call_{len(calls)}", fn.get("name") or "", args))
        text = msg.get("content") or ""
        if not text.strip() and not calls:
            raise ProviderError(f"empty reply from {data.get('provider', self.host)} "
                                f"(finish_reason={choice.get('finish_reason')})", retryable=True)
        usage = data.get("usage") or {}
        p_in, p_out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        cost = usage.get("cost")
        if cost is None:
            cost = (p_in * self.price_in + p_out * self.price_out) / 1e6
        return ModelResponse(text=text, tool_calls=tuple(calls), stop_reason=choice.get("finish_reason") or "",
                             usage=Usage(p_in, p_out), cost_usd=float(cost),
                             served_by=f"{data.get('provider', '?')}/{data.get('model', request.model_name)}")
