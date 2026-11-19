"""Workflow settings. Defaults follow the spec's suggested values (DESIGN.md section 12.4)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

OPUS = "anthropic/claude-opus-5-5"
QWEN = "openrouter/qwen/qwen3-coder-30b-a3b-instruct"

# The engine treats a step still running after its timeout_s as stuck: it stops renewing the lease and
# another worker runs the step again while the first copy keeps going and keeps calling models (D-044).
# So a step that drives an agent loop must outlast the loop's time budget, plus one more turn that may
# start just before the budget runs out (a model call, up to 900 s on the Anthropic client, and a shell
# command, up to 150 s) and the grading after it (up to 420 s).
AGENT_STEP_TIMEOUT_S = 3600.0
AGENT_STEP_SLACK_S = 1500.0


class TaskgenConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    models: dict[str, str] = {
        "rubric_writer": OPUS, "rubric_scorer": OPUS, "pattern_clusterer": OPUS, "proposal_writer": OPUS,
        "task_builder": OPUS, "hacker": OPUS, "leak_checker": OPUS, "blind_judge": OPUS, "repairer": OPUS,
        "strong_solver": OPUS, "weak_solver": QWEN,
    }
    solver_params: dict = {"temperature": 1.0}
    # sent with every call of a role: effort for Claude roles, sampling for the weak model, and whether
    # an agent loop asks the provider to cache the growing prompt prefix
    role_params: dict[str, dict] = {}
    max_tokens: dict[str, int] = {}
    max_pairs: int | None = None       # None = every strong/weak pair
    max_patterns: int | None = None    # cap on patterns kept at H1 (the pilot keeps one)
    strong_solver_max_steps: int = 40

    items_per_pair: int = 6
    keep_margin: float = 0.25          # strong_mean - weak_mean >= margin ...
    keep_floor: float = 0.5            # ... and strong_mean > floor
    proposals_per_pattern: int = 5
    h2_sample_per_pattern: int = 3

    builder_fix_rounds: int = 2
    hack_fix_rounds: int = 2           # R for verifier fixes
    repair_rounds: int = 2             # R for difficulty repairs
    use_model_hacker: bool = True
    hacker_max_steps: int = 15
    hacker_wall_s: float = 1800.0

    n_runs: int = 5
    rollout_retries: int = 2           # a failed attempt (infrastructure) is re-run this many times before giving up
    band_low: int = 1                  # accept when band_low <= k <= n - band_low
    solver_max_steps: int = 40
    solver_wall_s: float = 1800.0

    h4_sample_rate: float = 0.2
    h5_sample_per_task: int = 1

    @model_validator(mode="after")
    def _agent_budgets_fit_the_step_timeout(self) -> "TaskgenConfig":
        for name in ("solver_wall_s", "hacker_wall_s"):
            wall = getattr(self, name)
            if wall + AGENT_STEP_SLACK_S > AGENT_STEP_TIMEOUT_S:
                raise ValueError(f"{name} = {wall:g} s leaves less than {AGENT_STEP_SLACK_S:g} s before the agent "
                                 f"steps' timeout of {AGENT_STEP_TIMEOUT_S:g} s, so a long attempt would run twice")
        return self


SILICONFLOW = "siliconflow/fp8"
BEDROCK = "amazon-bedrock/us-west-2"
HOST_PRICES = {SILICONFLOW: (0.07, 0.28), BEDROCK: (0.15, 0.60)}      # dollars per million tokens, in / out

# The P3 pilot (approved 2026-11-16): 10 proposals for one pattern, one repair round, Hacker <= 10 turns.
PILOT = TaskgenConfig(
    proposals_per_pattern=10,
    repair_rounds=1,
    hacker_max_steps=10,
    strong_solver_max_steps=25,
    max_patterns=1,
    solver_params={"temperature": 0.7, "top_p": 0.8, "top_k": 20},    # Qwen3-Coder's recommended sampling
    role_params={
        "rubric_writer": {"effort": "medium"}, "rubric_scorer": {"effort": "low"},
        "pattern_clusterer": {"effort": "medium"}, "proposal_writer": {"effort": "medium"},
        "task_builder": {"effort": "high"}, "hacker": {"effort": "low", "cache": True},
        "leak_checker": {"effort": "low"}, "blind_judge": {"effort": "medium"}, "repairer": {"effort": "high"},
        "strong_solver": {"effort": "medium", "cache": True},
    },
    max_tokens={"task_builder": 64000, "repairer": 64000, "rubric_writer": 24000, "rubric_scorer": 8000,
                "strong_solver": 32000, "hacker": 16000, "weak_solver": 8000},
)
