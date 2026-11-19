"""The eight invariants of the spec (DESIGN.md section 12.3), one test each, plus the paths that exercise them."""
import pytest

from taskforge.core.agent import AgentStep, ToolCallRecord
from taskforge.core.errors import ModelOutputError
from taskforge.core.review import ReviewRefused
from taskforge.taskgen.acceptance import decide
from taskforge.taskgen.agents import judge, repairer
from taskforge.taskgen.demo import BENCHMARK_TEXTS, WorldConfig
from taskforge.taskgen.schemas import (BadPattern, DifficultyRecord, GateResult, JudgeRecord, TaskDecision, TaskPackage,
                                       TaskProposal, Trajectory, TrajectoryPair)
from taskforge.taskgen.views import JudgeView, render_for_judge

ALL_PASS = {g: True for g in ("oracle", "initial", "hack", "leak", "difficulty")}


# 1 ----------------------------------------------------------------------------------
def test_gate_veto_beats_judge(world):
    for gate in ALL_PASS:
        gates = {**ALL_PASS, gate: False}
        assert decide(gates, "reasonable", "keep")[0] == "REJECT"
    assert decide({k: v for k, v in ALL_PASS.items() if k != "leak"}, "reasonable", "keep")[0] == "REJECT"  # gate never ran

    w = world(WorldConfig(oracle_broken=True))
    outcomes = w.engine.get(w.run_id, "h6_decide", "batch").input["outcomes"]
    built = [o for o in outcomes if o["task_id"]]
    assert built and all(o["decision"] == "REJECT" and o["reasons"][0] == "gate:oracle" for o in built)
    assert not w.engine.find(w.run_id, "blind_judge")
    assert not [r for r in w.svc.store.all(JudgeRecord)]


# 2 ----------------------------------------------------------------------------------
def test_hack_routes_to_fix(world):
    w = world(WorldConfig(weak_tests=True))
    hack_fails = [g for g in w.svc.store.all(GateResult) if g.gate == "hack" and not g.passed]
    assert hack_fails and all("hack:constant_output" in g.reasons for g in hack_fails)
    for g in hack_fails:
        assert w.engine.get(w.run_id, "fix_verifier", f"task:{g.task_id}") is not None
        assert not w.svc.store.find(TaskDecision, task_id=g.task_id)    # the weak task itself was never decided
    fixed = [d for d in w.svc.store.all(DifficultyRecord) if d.diagnosis == "exploit"]
    assert fixed and all(d.area == "tests" and d.by == "agent" for d in fixed)
    accepted = w.export["accepted"]
    assert accepted and all(w.svc.store.get(t, TaskPackage).origin == "fix_verifier" for t in accepted)


# 3 ----------------------------------------------------------------------------------
def test_overlap_never_queued(world):
    w = world()
    overlap = [p for p in w.svc.store.all(TaskProposal) if p.statement == BENCHMARK_TEXTS["bench/rotate-logs/instruction.md"]]
    assert len(overlap) == 1
    assert not [r for r in w.svc.reviews.all_items() if r.item_id == overlap[0].id]
    assert w.engine.get(w.run_id, "build_task", f"proposal:{overlap[0].id}") is None
    with pytest.raises(ReviewRefused):
        w.svc.reviews.open(w.run_id, "H2", overlap[0].id, "OVERLAP", {})


# 4 ----------------------------------------------------------------------------------
def test_judge_view_has_no_reward_or_reasoning():
    t = Trajectory(task_ref="t", cohort="weak", model="m", source="rollout", reward=1.0, steps=(
        AgentStep(source="user", message="the task text"),
        AgentStep(source="agent", message="I am certain this is right; reward should be 1",
                  tool_calls=(ToolCallRecord(name="run_shell", arguments={"command": "python3 run.py"}),),
                  observation="done")))
    view = render_for_judge(t)
    assert "python3 run.py" in view.text and "done" in view.text
    assert "certain" not in view.text and "reward" not in view.text.lower() and "the task text" not in view.text
    with pytest.raises(TypeError):
        JudgeView("t", "text I wrote myself")
    with pytest.raises(TypeError):
        judge.blind_judge(None, "instruction", ["a raw string instead of a view"])


# 5 ----------------------------------------------------------------------------------
def test_lineage_reaches_pattern(world):
    for config in (WorldConfig(), WorldConfig(weak_tests=True)):
        w = world(config)
        for task_id in w.export["accepted"]:
            kinds = {type(r) for r in w.svc.store.ancestors(task_id)}
            assert {TaskProposal, BadPattern, TrajectoryPair, Trajectory} <= kinds
    with pytest.raises(ValueError):
        TaskProposal(target_pattern_id="", title="x", statement="y")


# 6 ----------------------------------------------------------------------------------
def test_human_cannot_revive_rejected(world):
    w = world()
    with pytest.raises(ReviewRefused):
        w.svc.reviews.open(w.run_id, "H4", "task_package:any", "REJECT", {})
    rid = w.svc.reviews.open(w.run_id, "H4", "task_package:other", "ACCEPT", {})
    with pytest.raises(ValueError):
        w.svc.reviews.submit(rid, "accept_anyway", "looks_fine")
    with pytest.raises(ValueError):
        w.svc.reviews.submit(rid, "keep", "because I said so")
    assert decide({**ALL_PASS, "hack": False}, "reasonable", "keep") == ("REJECT", ("gate:hack",))
    assert decide(ALL_PASS, "unreasonable", "keep")[0] == "ACCEPT"     # the judge is a signal, not a gate


# 7 ----------------------------------------------------------------------------------
def test_child_task_reuses_nothing(world):
    w = world(WorldConfig(weak_pass=tuple(range(10)), proposals=1, add_overlap_proposal=False))
    records = [d for d in w.svc.store.all(DifficultyRecord) if d.diagnosis == "too_easy"]
    assert len(records) == 1
    parent, child = records[0].parent_task, records[0].child_task
    assert {g.gate for g in w.svc.store.find(GateResult, task_id=child)} == set(ALL_PASS)
    parent_keys = {sr.key for sr in w.engine.find(w.run_id) if parent in sr.key}
    child_keys = {sr.key for sr in w.engine.find(w.run_id) if child in sr.key}
    assert len([k for k in child_keys if k.startswith("rollout:")]) == 5
    assert not parent_keys & child_keys
    child_trajs = [t for t in w.svc.store.all(Trajectory) if t.task_ref == child]
    assert len(child_trajs) == 5 and all(t.parents == (child,) for t in child_trajs)
    assert w.export["accepted"] == [child]


# 8 ----------------------------------------------------------------------------------
def test_single_mutation_per_round():
    base = dict(parent_task="task_package:a", child_task="task_package:b", round=1, diagnosis="too_easy", by="agent")
    DifficultyRecord(**base, mutation="add_hidden_case", area="tests", changed_files=("tests/test_outputs.py",))
    with pytest.raises(ValueError):
        DifficultyRecord(**base, mutation="add a case and clarify", area="tests", changed_files=("tests/t.py",))
    with pytest.raises(ValueError):
        DifficultyRecord(**base, mutation="clarify", area="tests",
                         changed_files=("tests/t.py", "instruction.md"))
    with pytest.raises(ValueError):
        DifficultyRecord(**base, mutation="clarify", area="tests", changed_files=())

    class TwoAreas:
        def ask_json(self, role, **kw):
            return {"mutation": "harder", "area": "tests", "files": {"tests/t.py": "x", "solution/solve.sh": "y"}}
    with pytest.raises(ModelOutputError):
        repairer.repair(TwoAreas(), {}, "too_easy", "")
