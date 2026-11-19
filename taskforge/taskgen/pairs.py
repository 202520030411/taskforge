"""Import stored Harbor trials and pair them (spec section 5, stage 1).

A trial folder holds `result.json` (task name, reward) and `agent/trajectory.json` (ATIF v1.8).
A pair is one task where the strong model passed and the weak model failed; v1 keeps one
pair per task (DESIGN.md section 12.4). Rubric items are later scored on *all* trajectories
of that task, not only the paired two.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from ..core.agent import AgentStep, ToolCallRecord
from .schemas import Trajectory, TrajectoryPair


def _observation_text(obs) -> str | None:
    if obs is None:
        return None
    if isinstance(obs, str):
        return obs
    if isinstance(obs, dict) and isinstance(obs.get("results"), list):
        return "\n".join(str(r.get("content", "")) for r in obs["results"])
    return json.dumps(obs, default=str)


def load_harbor_trial(trial_dir: Path, cohort: str) -> Trajectory | None:
    """Returns None for trials that crashed before grading (no reward) or have no trajectory file."""
    trial_dir = Path(trial_dir)
    try:
        result = json.loads((trial_dir / "result.json").read_text())
        atif = json.loads((trial_dir / "agent" / "trajectory.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if reward is None:
        return None
    steps = []
    for s in atif.get("steps", []):
        calls = s.get("tool_calls") or []
        if isinstance(calls, str):
            calls = json.loads(calls)
        steps.append(AgentStep(
            source=s.get("source", "agent") if s.get("source") in ("system", "user", "agent") else "agent",
            message=str(s.get("message") or ""),
            tool_calls=tuple(ToolCallRecord(name=str(c.get("function_name") or c.get("name")),
                                            arguments=dict(c.get("arguments") or {})) for c in calls),
            observation=_observation_text(s.get("observation"))))
    info = result.get("agent_info") or {}
    model_info = info.get("model_info") or {}
    first_user = next((st.message for st in steps if st.source == "user"), "")
    return Trajectory(
        task_ref=str(result.get("task_name", trial_dir.name)).split("/")[-1],
        cohort=cohort,  # type: ignore[arg-type]
        model=str(model_info.get("name") or (atif.get("agent") or {}).get("model_name") or ""),
        provider=str(model_info.get("provider") or ""),
        harness=f"{info.get('name', '')} {info.get('version', '')}".strip(),
        source=f"import:{trial_dir.parent.name}/{trial_dir.name}",
        task_prompt=first_user,
        steps=tuple(steps),
        reward=float(reward),
    )


def load_job(job_dir: Path, cohort: str) -> list[Trajectory]:
    out = []
    for trial in sorted(Path(job_dir).glob("*__*")):
        if trial.is_dir():
            t = load_harbor_trial(trial, cohort)
            if t is not None:
                out.append(t)
    return out


def make_pairs(trajectories: Iterable[Trajectory]) -> list[TrajectoryPair]:
    """One pair per task: the first strong pass with the first weak failure (ordered by id, so stable)."""
    by_task: dict[str, dict[str, list[Trajectory]]] = defaultdict(lambda: {"strong": [], "weak": []})
    for t in trajectories:
        by_task[t.task_ref][t.cohort].append(t)
    pairs = []
    for task in sorted(by_task):
        strong = sorted((t for t in by_task[task]["strong"] if t.passed), key=lambda t: t.id)
        weak = sorted((t for t in by_task[task]["weak"] if not t.passed), key=lambda t: t.id)
        if strong and weak:
            pairs.append(TrajectoryPair(task_ref=task, strong_traj=strong[0].id, weak_traj=weak[0].id,
                                        parents=(strong[0].id, weak[0].id)))
    return pairs
