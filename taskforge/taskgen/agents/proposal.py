"""Proposal Writer (spec section 5, stage 4): sees only the pattern brief, never task text."""
from __future__ import annotations

from typing import Sequence

from ...core.errors import ModelOutputError
from ..schemas import PatternBrief
from .llm import RoleLLM


def propose(llm: RoleLLM, brief: PatternBrief, n: int, avoid: Sequence[str]) -> list[dict]:
    avoid_text = "\n".join(f"- {a}" for a in avoid) or "(none yet)"
    reply = llm.ask_json("proposal_writer", required=("proposals",), category=brief.category,
                         description=brief.description, n=n, avoid=avoid_text)
    out = [{"title": str(p.get("title", "")).strip() or "untitled", "statement": str(p["statement"]).strip()}
           for p in reply["proposals"] if isinstance(p, dict) and str(p.get("statement", "")).strip()]
    if not out:
        raise ModelOutputError("the proposal writer returned no proposals")
    return out[:n]
