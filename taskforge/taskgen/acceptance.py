"""The one function that decides a task (invariants 1 and 6).

Executed gates are a hard veto: if any gate failed or never ran, the answer is REJECT
and neither the judge nor a person is consulted. Otherwise a person's rejection wins;
a person may also keep a task the judge flagged (the judge is a signal, not a gate).
"""
from __future__ import annotations

from typing import Mapping

from .schemas import GATES


def decide(gates: Mapping[str, bool], judge_verdict: str | None, human: str | None = None) -> tuple[str, tuple[str, ...]]:
    failed = [g for g in GATES if gates.get(g) is not True]
    if failed:
        return "REJECT", tuple(f"gate:{g}" for g in failed)
    if human == "reject":
        return "REJECT", ("human:reject",)
    if human == "keep":
        return "ACCEPT", ("human:keep",)
    if judge_verdict == "reasonable":
        return "ACCEPT", ()
    return "REVIEW", (f"judge:{judge_verdict or 'missing'}",)
