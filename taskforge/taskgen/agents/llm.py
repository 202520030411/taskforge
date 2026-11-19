"""How agent roles talk to models: render a versioned prompt, call the gateway, parse JSON."""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Callable, Mapping

from ...core.agent import AgentBudget, AgentResult, run_agent
from ...core.errors import ModelOutputError
from ...core.gateway import Gateway, ModelRequest, PromptRegistry, ToolSpec
from ...core.sandbox import Sandbox
from ...core.scope import CallScope

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def parse_json_reply(text: str, required: tuple[str, ...] = ()) -> dict:
    t = text.strip()
    fenced = _FENCE.search(t)
    if fenced:
        t = fenced.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ModelOutputError("no JSON object found")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise ModelOutputError(f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ModelOutputError("the reply is not a JSON object")
    missing = [k for k in required if k not in obj]
    if missing:
        raise ModelOutputError(f"missing keys {missing}")
    return obj


class RoleLLM:
    def __init__(self, gateway: Gateway, prompts: PromptRegistry, models: Mapping[str, str],
                 next_scope: Callable[[], CallScope], role_params: Mapping[str, Mapping] | None = None,
                 max_tokens: Mapping[str, int] | None = None):
        self.gateway = gateway
        self.prompts = prompts
        self.models = dict(models)
        self.next_scope = next_scope
        self.role_params = {k: dict(v) for k, v in (role_params or {}).items()}
        self.max_tokens = dict(max_tokens or {})

    def ask_json(self, role: str, *, required: tuple[str, ...], prompt: str | None = None,
                 max_tokens: int = 16000, **values) -> dict:
        """One call; if the reply cannot be parsed, one more call that shows the model its bad reply."""
        text, version = self.prompts.render(prompt or role, **values)
        request = ModelRequest(model=self.models[role], messages=({"role": "user", "content": text},),
                               max_tokens=self.max_tokens.get(role, max_tokens), prompt_version=version,
                               params=self.role_params.get(role, {}))
        reply = self.gateway.complete(self.next_scope(), role, request)
        if reply.refusal is not None:
            raise ModelOutputError(f"{role}: the model declined (category {reply.refusal})")
        try:
            return parse_json_reply(reply.text, required)
        except ModelOutputError as e:
            again = dataclasses.replace(request, messages=request.messages + (
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": f"That reply could not be used ({e}). Reply again with only the JSON object."}))
            return parse_json_reply(self.gateway.complete(self.next_scope(), role, again).text, required)

    def agent(self, role: str, *, system_prompt: str, task_prompt: str, sandbox: Sandbox, sid: str,
              tools: tuple[ToolSpec, ...], max_steps: int, wall_s: float = 1800.0,
              params: Mapping | None = None, tags: Mapping | None = None) -> AgentResult:
        system, version = self.prompts.render(system_prompt)
        merged = {**self.role_params.get(role, {}), **dict(params or {})}
        return run_agent(gateway=self.gateway, next_scope=self.next_scope, role=role, model=self.models[role],
                         system=system, task_prompt=task_prompt, sandbox=sandbox, sid=sid,
                         budget=AgentBudget(max_steps=max_steps, wall_s=wall_s), tools=tools, params=merged,
                         tags=tags, prompt_version=version, max_tokens=self.max_tokens.get(role, 16000))
