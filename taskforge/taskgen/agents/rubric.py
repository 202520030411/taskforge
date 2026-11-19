"""Rubric Writer, Rubric Scorer, the partial-order check, and pattern clustering (spec section 5, stages 2-3)."""
from __future__ import annotations

from typing import Sequence

from ...core.errors import ModelOutputError
from ..schemas import RubricItem, Trajectory
from ..views import clip_middle, render_for_judge
from .llm import RoleLLM

MAX_TASK_TEXT = 6000


def write_items(llm: RoleLLM, strong: Trajectory, weak: Trajectory, n: int, avoid: Sequence[str] = ()) -> list[dict]:
    """3-6 yes/no items contrasting what the strong run did with what the weak run did at the same point.
    `avoid`: items a reviewer deleted earlier (from the knowledge base)."""
    reply = llm.ask_json("rubric_writer", required=("items",), n=n,
                         avoid="\n".join(f"- {a}" for a in avoid) or "(none)",
                         task=clip_middle(strong.task_prompt, MAX_TASK_TEXT),
                         strong=render_for_judge(strong).text, weak=render_for_judge(weak).text)
    items = [i for i in reply["items"] if isinstance(i, dict) and i.get("description") and i.get("scoring_rule")]
    if not items:
        raise ModelOutputError("the rubric writer returned no usable items")
    return [{"description": str(i["description"]), "scoring_rule": str(i["scoring_rule"])} for i in items[:n]]


def score_trajectory(llm: RoleLLM, items: Sequence[RubricItem], traj: Trajectory) -> dict[str, int]:
    """Score every item 0/1 on one trajectory. The scorer sees commands and outputs only, never the reward."""
    listing = "\n".join(f"{i + 1}. {it.description}\n   Score 1 if: {it.scoring_rule}" for i, it in enumerate(items))
    reply = llm.ask_json("rubric_scorer", required=("scores",), items=listing,
                         task=clip_middle(traj.task_prompt, MAX_TASK_TEXT), trajectory=render_for_judge(traj).text)
    scores = reply["scores"] if isinstance(reply["scores"], dict) else {}
    return {it.id: 1 if scores.get(str(i + 1)) in (1, "1", True) else 0 for i, it in enumerate(items)}


def partial_order(strong: Sequence[int], weak: Sequence[int], *, margin: float, floor: float) -> tuple[bool, float, float]:
    """Keep an item only if strong runs score systematically higher than weak runs:
    strong_mean - weak_mean >= margin, and strong_mean > floor (so 'both low, weak lower' is dropped)."""
    if not strong or not weak:
        return False, 0.0, 0.0
    s, w = sum(strong) / len(strong), sum(weak) / len(weak)
    return (s - w >= margin and s > floor), round(s, 4), round(w, 4)


def cluster(llm: RoleLLM, items: Sequence[RubricItem]) -> list[dict]:
    """Group validated items into bad patterns; each item index belongs to at most one pattern."""
    listing = "\n".join(f"{i + 1}. {it.description}" for i, it in enumerate(items))
    reply = llm.ask_json("pattern_clusterer", required=("patterns",), items=listing)
    used: set[int] = set()
    out = []
    for p in reply["patterns"]:
        if not isinstance(p, dict) or not p.get("category") or not p.get("description"):
            continue
        idx = [int(i) - 1 for i in p.get("items", []) if str(i).isdigit() and 0 < int(i) <= len(items)]
        idx = [i for i in idx if i not in used]
        if idx:
            used.update(idx)
            out.append({"category": str(p["category"]), "description": str(p["description"]),
                        "item_ids": [items[i].id for i in idx]})
    return out
