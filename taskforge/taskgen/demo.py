"""A scripted world for running the whole workflow without paid models.

Every model role is answered by `FakeProvider` with fixed rules; every review point by
`AutoReviewer`. Two task kits:
  FakeKit       tiny tasks for FakeSandbox (unit tests; no Docker needed)
  OrderTotalsKit the handwritten task in tasks/handwritten/order-totals, for DockerSandbox on the VM

`WorldConfig` switches change which path a task takes (weak tests, too easy, too hard ...),
so the tests can drive each branch of the workflow on purpose.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.gateway import FakeProvider, Gateway, ModelRequest, tool_reply, turn_number
from ..core.review import ReviewService
from ..core.sandbox import FakeSandbox, Sandbox
from . import package as pkg
from .config import TaskgenConfig
from .contamination import BenchmarkIndex
from .workflow import Services

REPO = Path(__file__).resolve().parents[2]
HANDWRITTEN = REPO / "tasks" / "handwritten" / "order-totals"

BENCHMARK_TEXTS = {
    "bench/rotate-logs/instruction.md": (
        "Configure log rotation for the web service so that files in /var/log/web are rotated daily, compressed "
        "after one day, and at most fourteen rotated files are kept. Verify the configuration with a dry run."),
    "bench/fix-merge/instruction.md": (
        "The repository in /repo has an unfinished merge between the main branch and the feature branch. Resolve "
        "the conflicts so that both the new parser and the old command line flags keep working, then commit."),
}


@dataclass(frozen=True)
class WorldConfig:
    weak_pass: tuple[int, ...] = (0, 2, 4)        # rollout indexes the weak model passes
    strong_passes: bool = True
    judge_verdict: str = "reasonable"
    weak_tests: bool = False                       # the built task's tests can be beaten by a shortcut
    oracle_broken: bool = False                    # the built task's reference solution is wrong
    add_overlap_proposal: bool = True              # one proposal copies a benchmark item
    proposals: int = 3
    model_hacker_wins: bool = False
    repair_target_pass: tuple[int, ...] = (1, 3)   # after a repair, these indexes pass
    leak_in_workspace: bool = False


# ----------------------------------------------------------------------- kits
class FakeKit:
    """Tasks for FakeSandbox: the answer is a file; grading compares tests/expect/<rel> with /app/<rel>."""
    ANSWER, LOG, EXTRA = "42\n", "computed from /app/data/input.txt\n", "edge-case-handled\n"

    def __init__(self, world: WorldConfig):
        self.world = world

    def package(self, title: str) -> dict[str, str]:
        w = self.world
        solution_answer = "41\n" if w.oracle_broken else self.ANSWER
        tests = {"tests/test.sh": "#!/bin/bash\nexit 0\n", "tests/expect/answer.txt": self.ANSWER}
        if not w.weak_tests:
            tests["tests/expect/work.log"] = self.LOG
        files = {
            "instruction.md": (f"# {title}\n\nRead /app/data/input.txt and write the answer to /app/answer.txt and a "
                               f"one-line note to /app/work.log. /app/samples/input.txt and /app/samples/expected.txt "
                               f"show an example.\n"),
            "task.toml": "[metadata]\ndifficulty = \"easy\"\n",
            "environment/Dockerfile": "FROM python:3.12-slim\nCOPY app/ /app/\n",
            "environment/app/data/input.txt": "6 7\n",
            "environment/app/samples/input.txt": "3 5\n",
            "environment/app/samples/expected.txt": self.ANSWER if w.weak_tests else "15\n",
            "solution/solve.sh": "#!/bin/bash\n# the fake sandbox copies /solution/files/* into /app\n",
            "solution/files/answer.txt": solution_answer,
            "solution/files/work.log": self.LOG,
            "solution/files/extra.txt": self.EXTRA,
            **tests,
        }
        if w.leak_in_workspace:
            files["environment/app/notes.txt"] = "x" * 10 + self.LOG * 3
            files["environment/app/answer_key.txt"] = "the expected answer file is kept here for reference: 42"
            files["solution/files/answer_key.txt"] = files["environment/app/answer_key.txt"]
        return files

    def solver_writes(self, index: int, task_prompt: str, role: str, repaired: bool) -> dict[str, str]:
        w = self.world
        if role == "strong_solver":
            good = w.strong_passes
        else:
            good = index in (w.repair_target_pass if repaired else w.weak_pass)
        return {"/app/answer.txt": self.ANSWER if good else "40\n", "/app/work.log": self.LOG,
                "/app/extra.txt": self.EXTRA if good else "forgot\n"}

    def hacker_writes(self) -> dict[str, str]:
        if self.world.model_hacker_wins:
            return {"/app/answer.txt": self.ANSWER, "/app/work.log": self.LOG}
        return {"/app/answer.txt": "15\n"}

    def fixed_tests(self) -> dict[str, str]:
        return {"tests/expect/work.log": self.LOG}

    def harder_tests(self) -> dict[str, str]:
        return {"tests/expect/extra.txt": self.EXTRA}


BUGGY_ORDER_TOTALS = """import csv
import json
import sys

totals = {}
for row in csv.DictReader(open(sys.argv[1])):
    if row["status"] == "cancelled":
        continue
    totals[row["customer_id"]] = round(totals.get(row["customer_id"], 0) + float(row["amount"].replace(",", "")), 2)
json.dump(dict(sorted(totals.items())), open(sys.argv[2], "w"), indent=2)
"""


class OrderTotalsKit:
    """The handwritten task, for Docker. The weak model writes either the reference program or a buggy one."""

    def __init__(self, world: WorldConfig):
        self.world = world
        self.files = pkg.load_dir(HANDWRITTEN)
        solve = self.files["solution/solve.sh"]
        self.correct = solve.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0] + "\n"

    def package(self, title: str) -> dict[str, str]:
        return dict(self.files)

    def solver_writes(self, index: int, task_prompt: str, role: str, repaired: bool) -> dict[str, str]:
        good = (role == "strong_solver" and self.world.strong_passes) or (role == "weak_solver" and index in self.world.weak_pass)
        return {"/app/order_totals.py": self.correct if good else BUGGY_ORDER_TOTALS}

    def hacker_writes(self) -> dict[str, str]:
        expected = self.files["environment/app/samples/expected.json"]
        return {"/app/order_totals.py": f"import sys\nopen(sys.argv[2], 'w').write({expected!r})\n"}

    def fixed_tests(self) -> dict[str, str]:
        return {}

    def harder_tests(self) -> dict[str, str]:
        return {}


# --------------------------------------------------------------- fake models
def _section(text: str, header: str) -> str:
    return text.split(header + "\n", 1)[1] if header + "\n" in text else ""


def make_script(world: WorldConfig, kit) -> Callable[[str, ModelRequest], Any]:
    def script(role: str, request: ModelRequest):
        prompt = str(request.messages[0].get("content", "")) if request.messages else ""
        if role == "rubric_writer":
            return {"items": [
                {"description": "Compared the program's output on the sample input with the expected output (for "
                                "example with diff) before finishing",
                 "scoring_rule": "score 1 if a diff or equivalent comparison against the expected sample output appears"},
                {"description": "Ran at least one shell command", "scoring_rule": "score 1 if any command was run"}]}
        if role == "rubric_scorer":
            attempt = _section(prompt, "ATTEMPT")
            items = re.findall(r"^(\d+)\. (.*)$", _section(prompt, "RUBRIC ITEMS"), re.M)
            return {"scores": {n: (1 if "diff" in attempt else 0) if "diff" in d else 1 for n, d in items}}
        if role == "pattern_clusterer":
            n = len(re.findall(r"^\d+\. ", _section(prompt, "ITEMS"), re.M))
            return {"patterns": [{"category": "no-self-check",
                                  "description": "Declares the task done without comparing its output against the "
                                                 "available expected output.",
                                  "items": list(range(1, n + 1))}]}
        if role == "proposal_writer":
            props = [{"title": f"Summarise records {i}", "statement": f"Variant {i}: produce a summary file from a CSV "
                      f"export where a sample with expected output is provided and stated edge cases matter."}
                     for i in range(world.proposals)]
            if world.add_overlap_proposal:
                props.append({"title": "Rotate logs", "statement": BENCHMARK_TEXTS["bench/rotate-logs/instruction.md"]})
            return {"proposals": props}
        if role == "task_builder" and "SHORTCUTS THAT PASSED" in prompt:      # verifier-fix prompt
            return {"files": kit.fixed_tests()}       # empty -> unusable fix -> the task goes to H3
        if role == "task_builder" and "PROBLEM REPORT" in prompt:             # self-check fix prompt
            return {"files": {"task.toml": "[metadata]\ndifficulty = \"easy\"\nfixed = true\n"}}
        if role == "task_builder":
            title = re.search(r"PROPOSAL: (.*)", prompt).group(1).strip()
            return {"files": kit.package(title)}
        if role == "leak_checker":
            return {"suspect": False, "reason": "the hidden inputs differ from the visible sample"}
        if role == "blind_judge":
            return {"verdict": world.judge_verdict, "evidence": "the attempts read the instruction and ran the program"}
        if role == "repairer":
            return {"mutation": "add_hidden_edge_case", "area": "tests", "files": kit.harder_tests()}
        if role in ("weak_solver", "strong_solver", "hacker"):
            turn = turn_number(request)
            if turn == 0:
                return tool_reply(("run_shell", {"command": "cat /app/samples/input.csv"
                                                 if isinstance(kit, OrderTotalsKit) else "cat /app/samples/input.txt"}))
            if turn == 1:
                if role == "hacker":
                    writes = kit.hacker_writes()
                else:
                    repaired = "tests/expect/extra.txt" in request.tags.get("task_files", ())
                    writes = kit.solver_writes(int(request.tags.get("index", 0)), prompt, role, repaired)
                return tool_reply(*[("write_file", {"path": p, "content": c}) for p, c in writes.items()])
            return tool_reply(("finish", {}))
        raise AssertionError(f"the fake world has no rule for role {role!r}")
    return script


# --------------------------------------------------------------- fake reviewer
@dataclass
class AutoReviewer:
    """Answers every pending review. `overrides[stage]` = (decision, reason_code, payload)."""
    reviews: ReviewService
    overrides: dict[str, tuple[str, str, dict]] = field(default_factory=dict)
    minutes: float = 2.0
    DEFAULTS = {"H1": ("approve", "patterns_ok", {}), "H2": ("keep", "on_target", {}),
                "H3": ("drop", "cannot_fix", {}), "H4": ("keep", "looks_fine", {}),
                "H5": ("clean", "clean", {}), "H6": ("export", "ready", {}), "H7": ("reasonable", "blind_label", {})}

    def answer_all(self) -> int:
        n = 0
        for item in self.reviews.pending():
            decision, reason, payload = self.overrides.get(item.stage, self.DEFAULTS[item.stage])
            self.reviews.submit(item.review_id, decision, reason, reviewer="auto", minutes_spent=self.minutes,
                                payload=payload)
            n += 1
        return n


# ----------------------------------------------------------------- fake jobs
def write_fake_jobs(root: Path) -> dict[str, list[str]]:
    """Two stored 'benchmark' tasks, each with 2 strong runs (both pass) and 2 weak runs (one pass, one fail)."""
    def trial(job: str, task: str, n: int, model: str, reward: float, checks: bool) -> None:
        d = Path(root) / job / f"{task}__{model}{n}"
        (d / "agent").mkdir(parents=True, exist_ok=True)
        cmds = ["ls /app\n", "python3 /app/report.py > /tmp/out.txt\n"]
        if checks:
            cmds.append("diff /tmp/out.txt /app/expected.txt\n")
        steps = [{"step_id": 1, "source": "user", "message": f"Task {task}: write /app/report.py ..."}]
        for i, c in enumerate(cmds):
            steps.append({"step_id": i + 2, "source": "agent", "model_name": model,
                          "message": f"Analysis: step {i}. I am confident the output is correct.",
                          "tool_calls": [{"tool_call_id": f"c{i}", "function_name": "bash_command",
                                          "arguments": {"keystrokes": c, "duration": 1}}],
                          "observation": {"results": [{"source_call_id": f"c{i}", "content": f"$ {c}ok"}]}})
        (d / "agent" / "trajectory.json").write_text(json.dumps(
            {"schema_version": "ATIF-v1.8", "agent": {"name": "terminus-2", "model_name": model}, "steps": steps}))
        (d / "result.json").write_text(json.dumps(
            {"task_name": f"terminal-bench/{task}", "verifier_result": {"rewards": {"reward": reward}},
             "agent_info": {"name": "terminus-2", "version": "2.0", "model_info": {"name": model, "provider": "fake"}}}))
    for task in ("task-a", "task-b"):
        for n in range(2):
            trial("strong-job", task, n, "strong", 1.0, True)
        trial("weak-job", task, 0, "weak", 1.0, True)
        trial("weak-job", task, 1, "weak", 0.0, False)
    trial("weak-job", "task-c", 0, "weak", 0.0, False)          # no strong run: never paired
    return {"strong_jobs": [str(Path(root) / "strong-job")], "weak_jobs": [str(Path(root) / "weak-job")]}


# ------------------------------------------------------------------- running
@dataclass
class WorldRun:
    engine: Any
    svc: Services
    run_id: str
    reviewer: AutoReviewer
    provider: FakeProvider

    @property
    def export(self) -> dict | None:
        sr = self.engine.get(self.run_id, "export", "batch")
        return sr.output if sr else None


class TaggingGateway(Gateway):
    """Adds the current task's file list to agent requests' tags, so fake solvers can tell a repaired task apart.
    Tags are never sent to a real provider."""

    def __init__(self, *a, store, **kw):
        super().__init__(*a, **kw)
        self._store = store

    def complete(self, scope, role, request):
        task_id = request.tags.get("task_id")
        if task_id and self._store.has(task_id):
            files = self._store.get_json_blob(self._store.get(task_id).files_blob)
            request = dataclasses.replace(request, tags={**request.tags, "task_files": sorted(files)})
        return super().complete(scope, role, request)


def tagging_gateway(providers, store, **kw) -> Gateway:
    return TaggingGateway(providers, store=store, **kw)


def build_world(tmp: Path, world: WorldConfig = WorldConfig(), *, sandbox: Sandbox | None = None, kit=None,
                config: TaskgenConfig | None = None, cost_per_call: float = 0.01, db=None):
    """A complete scripted platform. With `db` (a core.pg.Database) everything is stored in Postgres;
    without it, in memory."""
    from .. import runtime
    kit = kit or FakeKit(world)
    provider = FakeProvider(make_script(world, kit), cost_per_call=cost_per_call)
    common = dict(providers={"anthropic": provider, "openrouter": provider}, sandbox=sandbox or FakeSandbox(),
                  benchmark=BenchmarkIndex(BENCHMARK_TEXTS), config=config, gateway_factory=tagging_gateway)
    platform = runtime.build_pg(db, **common) if db is not None else runtime.build_memory(**common)
    return platform.engine, platform.svc, AutoReviewer(platform.svc.reviews), provider, write_fake_jobs(tmp)


def run_world(tmp: Path, world: WorldConfig = WorldConfig(), *, budget_usd: float = 5.0, overrides=None,
              max_rounds: int = 50, **kw) -> WorldRun:
    engine, svc, reviewer, provider, jobs = build_world(tmp, world, **kw)
    if overrides:
        reviewer.overrides.update(overrides)
    run_id = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=budget_usd)
    for _ in range(max_rounds):
        engine.run_until_idle(run_id)
        if reviewer.answer_all() == 0:
            break
    return WorldRun(engine, svc, run_id, reviewer, provider)
