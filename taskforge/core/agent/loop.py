"""The tool-calling loop every sandbox role uses (DESIGN.md section 8).

    repeat until `finish`, a budget runs out, or the model stops calling tools:
        ask the model -> run each tool call in the sandbox -> append the results

A failed tool call becomes text the model can read. Sandbox infrastructure faults
(SandboxError) are not caught: the whole rollout is retried in a fresh sandbox (decision D-006).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from ..gateway import Gateway, ModelRequest, ToolCall, ToolSpec
from ..sandbox import Sandbox
from ..scope import CallScope
from ..tools.registry import (DEFAULT_REGISTRY, FINISH, READ_FILE, RUN_SHELL, SOLVER_TOOLS, WRITE_FILE, ToolContext,
                              ToolRegistry)
from .trajectory import AgentStep, ToolCallRecord

__all__ = ["AgentBudget", "AgentResult", "FINISH", "READ_FILE", "RUN_SHELL", "SOLVER_TOOLS", "WRITE_FILE",
           "execute_tool", "run_agent"]


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 40
    wall_s: float = 1800.0
    cmd_timeout_s: float = 120.0


@dataclass
class AgentResult:
    steps: list[AgentStep] = field(default_factory=list)
    exit: str = ""          # finished | step_limit | time_limit | model_stopped | refused
    cost_usd: float = 0.0


def execute_tool(sandbox: Sandbox, sid: str, call: ToolCall, budget: AgentBudget,
                 allowed: Mapping[str, ToolSpec], registry: ToolRegistry = DEFAULT_REGISTRY) -> str:
    """Run one tool call through the registry. Only tools offered to the model (`allowed`) can run."""
    return registry.call(call.name, call.arguments, ToolContext(sandbox=sandbox, sid=sid,
                                                                cmd_timeout_s=budget.cmd_timeout_s), allowed=allowed)


def run_agent(*, gateway: Gateway, next_scope: Callable[[], CallScope], role: str, model: str, system: str,
              task_prompt: str, sandbox: Sandbox, sid: str, budget: AgentBudget = AgentBudget(),
              tools: tuple[ToolSpec, ...] = SOLVER_TOOLS, params: Mapping | None = None,
              tags: Mapping | None = None, prompt_version: str = "", max_tokens: int = 16000) -> AgentResult:
    allowed = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": task_prompt}]
    result = AgentResult(steps=[AgentStep(source="user", message=task_prompt)])
    started = time.monotonic()
    for _ in range(budget.max_steps):
        if time.monotonic() - started > budget.wall_s:
            result.exit = "time_limit"
            return result
        request = ModelRequest(model=model, system=system, messages=tuple(messages), tools=tools,
                               params=dict(params or {}), tags=dict(tags or {}), prompt_version=prompt_version,
                               max_tokens=max_tokens)
        reply = gateway.complete(next_scope(), role, request)
        result.cost_usd += reply.cost_usd
        assistant = {"role": "assistant", "content": reply.text,
                     "tool_calls": [{"id": c.id, "name": c.name, "arguments": dict(c.arguments)} for c in reply.tool_calls]}
        if reply.raw is not None:
            assistant["raw"] = [dict(b) for b in reply.raw]   # sent back exactly as received (Anthropic)
        messages.append(assistant)
        if reply.refusal is not None:
            result.steps.append(AgentStep(source="agent", message=f"[refused: {reply.refusal}]"))
            result.exit = "refused"
            return result
        if not reply.tool_calls:
            result.steps.append(AgentStep(source="agent", message=reply.text))
            result.exit = "model_stopped"
            return result
        observations = []
        finished = False
        for call in reply.tool_calls:
            # zero bytes (e.g. from cat on a binary file) cannot be stored in Postgres JSON; show them visibly
            obs = execute_tool(sandbox, sid, call, budget, allowed).replace("\x00", "\\0")
            observations.append(obs)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": obs})
            finished = finished or call.name == "finish"
        result.steps.append(AgentStep(
            source="agent", message=reply.text,
            tool_calls=tuple(ToolCallRecord(name=c.name, arguments=json.loads(json.dumps(dict(c.arguments))))
                             for c in reply.tool_calls),
            observation="\n".join(observations)))
        if finished:
            result.exit = "finished"
            return result
    result.exit = "step_limit"
    return result
