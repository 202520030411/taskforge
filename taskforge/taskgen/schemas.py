"""Records of the task-generation workflow (DESIGN.md section 12.2; field lists follow spec section 7).

Every record is immutable and content-addressed (core/store). A changed task is a
new TaskPackage whose `parents` holds the task it came from.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..core.agent import AgentStep
from ..core.review import ReviewRecord
from ..core.store import Record
from . import package as pkg

Decision = Literal["ACCEPT", "REJECT", "REVIEW"]
Verdict = Literal["reasonable", "unreasonable", "cannot_judge", "no_good_solution"]
Diagnosis = Literal["too_easy", "exploit", "broken", "truly_hard", "human_edit"]
GATES = ("oracle", "initial", "hack", "leak", "difficulty")


class Trajectory(Record):
    KIND = "trajectory"
    task_ref: str
    cohort: Literal["strong", "weak"]
    model: str
    provider: str = ""
    harness: str = ""
    source: str                        # "import:<trial dir>" or "rollout"
    rollout_index: int | None = None
    task_prompt: str = ""
    steps: tuple[AgentStep, ...]
    reward: float | None
    exit: str = ""
    suspect: tuple[str, ...] = ()      # reasons this trajectory looks like a shortcut (hacks.scan_suspect)

    @property
    def passed(self) -> bool:
        return self.reward is not None and self.reward >= 1.0


class TrajectoryPair(Record):
    KIND = "trajectory_pair"
    task_ref: str
    strong_traj: str
    weak_traj: str


class RubricItem(Record):
    KIND = "rubric_item"
    pair_id: str
    task_ref: str
    description: str
    scoring_rule: str


class RubricScore(Record):
    KIND = "rubric_score"
    item_id: str
    traj_id: str
    cohort: Literal["strong", "weak"]
    score: Literal[0, 1]


class RubricVerdict(Record):
    """Outcome of the partial-order check for one rubric item."""
    KIND = "rubric_verdict"
    item_id: str
    strong_mean: float
    weak_mean: float
    n_strong: int
    n_weak: int
    kept: bool


class BadPattern(Record):
    KIND = "bad_pattern"
    category: str
    description: str
    item_ids: tuple[str, ...]
    evidence_pairs: tuple[str, ...]


class PatternBrief(BaseModel):
    """The only view of a BadPattern the Proposal Writer gets: no task text, no trajectories."""
    model_config = ConfigDict(frozen=True)
    pattern_id: str
    category: str
    description: str


class TaskProposal(Record):
    KIND = "task_proposal"
    target_pattern_id: str
    title: str
    statement: str

    @field_validator("target_pattern_id")
    @classmethod
    def _has_pattern(cls, v: str) -> str:
        if not v.startswith("bad_pattern:"):
            raise ValueError("every proposal must name the bad pattern it targets (invariant 5)")
        return v


class ContaminationCheck(Record):
    KIND = "contamination_check"
    target_id: str
    text_kind: Literal["proposal", "instruction"]
    status: Literal["CLEAN", "GREY", "OVERLAP"]
    score: float
    closest: str | None = None


class TaskPackage(Record):
    KIND = "task_package"
    proposal_id: str
    target_pattern_id: str
    files_blob: str                     # sha256 of the canonical JSON {path: content}
    origin: Literal["builder", "fix_verifier", "repair", "human"]
    builder_oracle_ok: bool | None = None


class GateResult(Record):
    KIND = "gate_result"
    task_id: str
    gate: Literal["oracle", "initial", "hack", "leak", "difficulty"]
    passed: bool
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] = {}


class DifficultyRecord(Record):
    """One repair round: parent task -> child task, exactly one change (invariant 8)."""
    KIND = "difficulty_record"
    parent_task: str
    child_task: str
    round: int
    diagnosis: Diagnosis
    mutation: str
    area: Literal["instruction", "environment", "tests", "solution"]
    changed_files: tuple[str, ...]
    pass_count: int | None = None
    n_runs: int | None = None
    by: Literal["agent", "human"]

    @model_validator(mode="after")
    def _one_change(self) -> "DifficultyRecord":
        if not self.mutation.strip() or any(sep in self.mutation for sep in (";", "\n", " and ", "+")):
            raise ValueError(f"one change per round: mutation must name a single change, got {self.mutation!r}")
        if not self.changed_files:
            raise ValueError("a repair round must change at least one file")
        areas = {pkg.area(p) for p in self.changed_files}
        if areas != {self.area}:
            raise ValueError(f"one change per round: files touch {sorted(areas)}, declared area {self.area!r}")
        return self


class JudgeRecord(Record):
    KIND = "judge_record"
    task_id: str
    verdict: Verdict
    evidence: str
    traj_ids: tuple[str, ...]


class TaskDecision(Record):
    KIND = "task_decision"
    task_id: str
    decision: Decision
    reasons: tuple[str, ...]
    gates: dict[str, bool]


class ExportRecord(Record):
    KIND = "export"
    run_id: str
    accepted_task_ids: tuple[str, ...]
    funnel: dict[str, Any]
    measures: dict[str, Any]


__all__ = ["BadPattern", "ContaminationCheck", "DifficultyRecord", "ExportRecord", "GATES", "GateResult",
           "JudgeRecord", "PatternBrief", "ReviewRecord", "RubricItem", "RubricScore", "RubricVerdict", "TaskDecision",
           "TaskPackage", "TaskProposal", "Trajectory", "TrajectoryPair"]
