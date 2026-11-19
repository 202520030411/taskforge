"""Difficulty Repairer (spec section 5, stage 7): one change per round, in one area of the package."""
from __future__ import annotations

import json
from typing import Mapping, Sequence

from ...core.errors import ModelOutputError
from .. import package as pkg
from .llm import RoleLLM

LEGAL_SOURCES = ("add a hidden test case", "require reasoning across files", "add a problem only found by checking "
                 "one's own work", "add a step that requires recovering from an intermediate failure")


def repair(llm: RoleLLM, files: Mapping[str, str], diagnosis: str, evidence: str,
           examples: Sequence[str] = (), *, passes: tuple[int, int] = (0, 0),
           target: tuple[int, int, int] = (1, 4, 5)) -> tuple[str, str, dict[str, str | None]]:
    """Returns (mutation, area, changes). Raises ModelOutputError if the change breaks the one-change rule.
    `examples`: how people fixed similar tasks before (from the knowledge base).
    `passes`: (passed, attempts) now; `target`: (low, high, out of n) - the range a repaired task should land in."""
    reply = llm.ask_json("repairer", required=("mutation", "area", "files"), max_tokens=32000, diagnosis=diagnosis,
                         passed=passes[0], attempts=passes[1], low=target[0], high=target[1], out_of=target[2],
                         examples="\n".join(f"- {e}" for e in examples) or "(none yet)",
                         evidence=evidence[:20000], legal="\n".join(f"- {s}" for s in LEGAL_SOURCES),
                         package=json.dumps(dict(files), indent=1)[:60000])
    area = str(reply["area"])
    changes = reply["files"]
    if area not in pkg.AREAS:
        raise ModelOutputError(f"unknown area {area!r}")
    if not isinstance(changes, dict) or not changes:
        raise ModelOutputError("a repair must change at least one file")
    changes = {str(p): (None if c is None else str(c)) for p, c in changes.items()}
    wrong = [p for p in changes if pkg.area(p) != area]
    if wrong:
        raise ModelOutputError(f"one change per round: {wrong} are outside the declared area {area!r}")
    return str(reply["mutation"]).strip(), area, changes
