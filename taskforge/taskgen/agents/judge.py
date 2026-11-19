"""Blind Judge (spec section 5, stage 8) and the model half of the leak check.

The Blind Judge reads JudgeViews: commands and outputs, no rewards, no model reasoning.
Its verdict is an audit signal and never a gate (invariant 1).
"""
from __future__ import annotations

import json
from typing import Mapping, Sequence

from ..views import JudgeView, clip_middle
from .llm import RoleLLM

VERDICTS = ("reasonable", "unreasonable", "cannot_judge", "no_good_solution")


def blind_judge(llm: RoleLLM, instruction: str, views: Sequence[JudgeView]) -> tuple[str, str]:
    if not all(isinstance(v, JudgeView) for v in views):
        raise TypeError("the blind judge only accepts JudgeView objects")
    attempts = "\n\n".join(f"### Attempt {i + 1}\n{v.text}" for i, v in enumerate(views))
    reply = llm.ask_json("blind_judge", required=("verdict", "evidence"), instruction=instruction,
                         attempts=clip_middle(attempts, 60000))
    verdict = str(reply["verdict"]).strip().lower()
    return (verdict if verdict in VERDICTS else "cannot_judge"), str(reply["evidence"])[:2000]


def leak_suspicion(llm: RoleLLM, files: Mapping[str, str]) -> tuple[bool, str]:
    visible = {p: c for p, c in files.items() if p == "instruction.md" or p.startswith("environment/app/")}
    hidden = {p: c for p, c in files.items() if p.startswith(("tests/", "solution/"))}
    reply = llm.ask_json("leak_checker", required=("suspect", "reason"),
                         visible=clip_middle(json.dumps(visible, indent=1), 30000),
                         hidden=clip_middle(json.dumps(hidden, indent=1), 30000))
    return bool(reply["suspect"]), str(reply["reason"])[:1000]
