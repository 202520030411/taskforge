"""Funnel and measures for one run (DESIGN.md section 13). Definitions are fixed here, before any run."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from ..core.review import ReviewRecord
from .schemas import GateResult, TaskPackage


def _stage(reasons: Sequence[str]) -> str:
    """The first reason names where a proposal's branch ended, e.g. 'gate:oracle' or 'contamination:proposal'."""
    return reasons[0] if reasons else "accepted"


def _lineage_tasks(svc, task_id: str | None) -> list[str]:
    if not task_id:
        return []
    return [task_id] + [r.id for r in svc.store.ancestors(task_id) if isinstance(r, TaskPackage)]


def build_report(svc, run_id: str, outcomes: Sequence[Mapping[str, Any]], failed_branches: Sequence) -> dict:
    by_pattern: dict[str, Counter] = defaultdict(Counter)
    overall: Counter = Counter()
    oracle_passed = oracle_passed_rejected = 0
    for o in outcomes:
        end = "accepted" if o["decision"] == "ACCEPT" else _stage(o["reasons"])
        by_pattern[o["pattern_id"]]["proposals"] += 1
        by_pattern[o["pattern_id"]][end] += 1
        overall[end] += 1
        tasks = _lineage_tasks(svc, o.get("task_id"))
        if any(g.passed for t in tasks for g in svc.store.find(GateResult, task_id=t, gate="oracle")):
            oracle_passed += 1
            oracle_passed_rejected += o["decision"] != "ACCEPT"
    accepted = overall["accepted"]
    reviews = [r for r in svc.store.all(ReviewRecord) if r.run_id == run_id]
    spent = svc.gateway.spent(run_id)
    minutes = sum(r.minutes_spent for r in reviews)
    return {
        "funnel": {
            "proposals": len(outcomes),
            "accepted": accepted,
            "ended_at": dict(sorted(overall.items())),
            "by_pattern": {p: dict(sorted(c.items())) for p, c in sorted(by_pattern.items())},
            "failed_branches": len(failed_branches),
        },
        "measures": {
            # of proposals whose task passed the oracle gate at least once, the share that still ended rejected
            "extra_rejections_beyond_oracle": {
                "n_oracle_passed": oracle_passed, "rejected_later": oracle_passed_rejected,
                "share": round(oracle_passed_rejected / oracle_passed, 4) if oracle_passed else None},
            "cost_usd": round(spent, 4),
            "cost_per_accepted_usd": round(spent / accepted, 4) if accepted else None,
            "human_minutes_per_accepted": round(minutes / accepted, 2) if accepted else None,
            "reviews_by_stage": dict(sorted(Counter(r.stage for r in reviews).items())),
            "double_charges": svc.gateway.double_charges(),
        },
    }
