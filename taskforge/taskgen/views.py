"""What a judge may see of a trajectory: commands and their outputs only (invariant 4).

The reward and the model's own text (its reasoning) are dropped: the reasoning is a
claim, not evidence. `JudgeView` refuses to be built anywhere except `render_for_judge`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from ..core.agent import AgentStep, ToolCallRecord

MAX_OUTPUT = 1500
MAX_WRITE = 800
MAX_TOTAL = 40000      # whole trajectory; the first and last parts are kept (longest ~10% of stored attempts)
_ORIGIN = object()


@dataclass(frozen=True)
class JudgeView:
    task_ref: str
    text: str
    origin: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.origin is not _ORIGIN:
            raise TypeError("JudgeView can only be built by render_for_judge()")


def clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + f"\n...[{len(text) - limit} characters omitted]...\n" + text[-(limit - head):]


def render_call(call: ToolCallRecord) -> str:
    args = call.arguments
    if call.name == "run_shell":
        return f"$ {args.get('command', '')}"
    if call.name == "bash_command":                       # Terminus-style imported trajectories
        return f"$ {str(args.get('keystrokes', '')).rstrip()}"
    if call.name == "write_file":
        return f"[write_file {args.get('path')}]\n{clip_middle(str(args.get('content', '')), MAX_WRITE)}"
    if call.name == "read_file":
        return f"[read_file {args.get('path')}]"
    if call.name == "finish":
        return "[finish]"
    return f"[{call.name} {json.dumps(args, default=str)[:400]}]"


def render_steps(steps: Iterable[AgentStep]) -> str:
    lines = []
    for i, step in enumerate(s for s in steps if s.source == "agent"):
        if not step.tool_calls:
            continue
        calls = "\n".join(render_call(c) for c in step.tool_calls)
        out = clip_middle(step.observation or "", MAX_OUTPUT)
        lines.append(f"--- turn {i + 1}\n{calls}\n>>> output:\n{out}")
    return "\n".join(lines) if lines else "(the agent ran no commands)"


def render_for_judge(trajectory) -> JudgeView:
    """trajectory: schemas.Trajectory. Only `task_ref` and the tool calls with outputs are used."""
    return JudgeView(trajectory.task_ref, clip_middle(render_steps(trajectory.steps), MAX_TOTAL), _ORIGIN)
