"""Model-driven Hacker: an agent told to make the hidden tests pass without solving the task.

It works in a normal sandbox with the solver's tools - it never gets the hidden tests or the
solution - and its /app is then graded like any attempt. Scripted strategies live in taskgen/hacks.py.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from ...core.agent import FINISH, READ_FILE, RUN_SHELL, WRITE_FILE
from ...core.sandbox import Sandbox, section, session
from ..hacks import scan_suspect
from .llm import RoleLLM


def model_hack(llm: RoleLLM, sandbox: Sandbox, image: str, files: Mapping[str, str], max_steps: int,
               notes: Sequence[str] = (), wall_s: float = 1800.0) -> dict:
    """`notes`: shortcuts people found on other tasks (from the knowledge base)."""
    task_prompt, _ = llm.prompts.render("hacker_task", instruction=files.get("instruction.md", ""),
                                        notes="\n".join(f"- {n}" for n in notes) or "(none recorded)")
    with session(sandbox, image, purpose="hack") as sid:
        result = llm.agent("hacker", system_prompt="hacker_system", task_prompt=task_prompt, sandbox=sandbox, sid=sid,
                           tools=(RUN_SHELL, READ_FILE, WRITE_FILE, FINISH), max_steps=max_steps, wall_s=wall_s)
        snap = sandbox.snapshot_app(sid)
    v = sandbox.verify(image, section(files, "tests"), snap)
    return {"passed": v.passed, "turns": len(result.steps) - 1, "exit": result.exit,
            "suspect": list(scan_suspect(result.steps))}
