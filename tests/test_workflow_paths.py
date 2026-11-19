"""Branches of the workflow beyond the happy path, and platform behaviour under faults."""
import json

from taskforge.core.engine import StepStatus
from taskforge.core.errors import WorkerCrash
from taskforge.core.sandbox import FakeSandbox
from taskforge.taskgen.demo import WorldConfig, build_world
from taskforge.taskgen.schemas import (DifficultyRecord, ExportRecord, GateResult, JudgeRecord, RubricVerdict,
                                       TaskDecision, Trajectory)


def test_happy_path(world):
    w = world()
    s = w.engine.summary(w.run_id)
    assert s["finished"] and s["counts"] == {"SUCCEEDED": s["counts"]["SUCCEEDED"]}
    exp = w.export
    assert len(exp["accepted"]) == 3
    assert exp["funnel"]["ended_at"] == {"accepted": 3, "contamination:proposal": 1}
    assert exp["measures"]["double_charges"] == 0
    assert w.svc.sandbox.leaked == 0
    assert len(w.svc.store.all(ExportRecord)) == 1
    # the rubric item every run satisfies was dropped by the partial-order check; the diff item was kept
    verdicts = w.svc.store.all(RubricVerdict)
    assert sorted(v.kept for v in verdicts) == [False, False, True, True]


def test_unapproved_run_stops_before_the_first_paid_step(tmp_path, db):
    engine, svc, reviewer, provider, jobs = build_world(tmp_path, db=db)
    run = engine.start_run("ingest", "batch", jobs)               # no paid approval
    engine.run_until_idle(run)
    s = engine.summary(run)
    assert not s["finished"] and s["blocked_paid_steps"] == 2      # two write_rubric steps waiting
    assert provider.calls == [] and svc.gateway.ledger == []
    engine.approve(run, budget_usd=5)
    for _ in range(20):
        engine.run_until_idle(run)
        if not reviewer.answer_all():
            break
    assert engine.summary(run)["finished"]


def test_budget_pauses_the_run_and_approval_resumes_it(tmp_path, db):
    engine, svc, reviewer, provider, jobs = build_world(tmp_path, db=db)
    run = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=0.05)
    engine.run_until_idle(run)
    assert engine.run_info(run)["status"] == "PAUSED_BUDGET"
    assert svc.gateway.spent(run) <= 0.05 + 1e-9
    engine.approve(run, budget_usd=5)
    for _ in range(20):
        engine.run_until_idle(run)
        if not reviewer.answer_all():
            break
    assert engine.summary(run)["finished"] and engine.get(run, "export", "batch").status == StepStatus.SUCCEEDED


def test_too_easy_task_is_repaired_once_and_its_child_accepted(world):
    w = world(WorldConfig(weak_pass=tuple(range(10)), proposals=1, add_overlap_proposal=False))
    [record] = w.svc.store.all(DifficultyRecord)
    assert (record.diagnosis, record.mutation, record.area, record.pass_count, record.n_runs) == \
        ("too_easy", "add_hidden_edge_case", "tests", 10, 10)
    assert w.export["accepted"] == [record.child_task]
    # repaired-to-be-harder children are always shown to a person at H4
    assert any(r.stage == "H4" and r.item_id == record.child_task for r in w.svc.reviews.all_items())


def test_all_fail_with_strong_pass_and_reasonable_judge_is_truly_hard(world):
    w = world(WorldConfig(weak_pass=(), proposals=1, add_overlap_proposal=False))
    task = w.svc.store.all(DifficultyRecord)[0]
    assert task.diagnosis == "truly_hard"
    assert w.engine.find(w.run_id, "diagnose") and w.svc.store.find(JudgeRecord, task_id=task.parent_task)
    strong = [t for t in w.svc.store.all(Trajectory) if t.cohort == "strong" and t.source == "rollout"]
    assert len(strong) == 1 and strong[0].passed


def test_all_fail_unclear_goes_to_a_person(world):
    w = world(WorldConfig(weak_pass=(), strong_passes=False, proposals=1, add_overlap_proposal=False))
    outcome = w.engine.get(w.run_id, "h6_decide", "batch").input["outcomes"][0]
    assert outcome["decision"] == "REJECT" and outcome["reasons"] == ["h3:drop"]
    [h3] = [r for r in w.svc.reviews.all_items() if r.stage == "H3"]
    assert "strong passed=False" in h3.material["why"]


def test_human_edit_at_h3_is_a_new_task_that_reruns_every_gate(world):
    fix = {"files": {"instruction.md": "# clarified\n\nRead /app/data/input.txt and write the answer to "
                                       "/app/answer.txt and a note to /app/work.log.\n"}}
    w = world(WorldConfig(weak_pass=(), strong_passes=False, proposals=1, add_overlap_proposal=False),
                  overrides={"H3": ("edit_instruction", "ambiguous_spec", fix)})
    human = [d for d in w.svc.store.all(DifficultyRecord) if d.by == "human"]
    assert human and human[0].area == "instruction"
    child = human[0].child_task
    assert {g.gate for g in w.svc.store.find(GateResult, task_id=child)} >= {"oracle", "initial", "hack", "leak"}


def test_leak_in_workspace_is_rejected_by_the_hard_rule(world):
    w = world(WorldConfig(leak_in_workspace=True, proposals=1, add_overlap_proposal=False))
    [leak] = [g for g in w.svc.store.all(GateResult) if g.gate == "leak"]
    assert not leak.passed and "solution_file_in_workspace" in leak.reasons
    [decision] = w.svc.store.all(TaskDecision)
    assert decision.decision == "REJECT"


def test_model_hacker_win_is_never_dropped_directly(world):
    w = world(WorldConfig(model_hacker_wins=True, proposals=1, add_overlap_proposal=False))
    [hack] = [g for g in w.svc.store.all(GateResult) if g.gate == "hack"]
    assert not hack.passed and hack.reasons == ("hack:model_hacker",)
    assert w.engine.find(w.run_id, "fix_verifier")             # first the tests are fixed ...
    assert [r for r in w.svc.reviews.all_items() if r.stage == "H3"]   # ... and an unusable fix goes to a person


class CrashOnce(FakeSandbox):
    """Kills the worker in the middle of a rollout, after its model calls were made: on the 1st, 3rd and 5th
    rollout snapshot. The engine re-runs a crashed step right away, so three different rollouts crash once each."""
    def __init__(self):
        super().__init__()
        self.rollout_snapshots = 0
        self.crashed = 0

    def snapshot_app(self, sid):
        if self.purposes[sid] == "rollout":
            self.rollout_snapshots += 1
            if self.rollout_snapshots in (1, 3, 5):
                self.crashed += 1
                raise WorkerCrash()
        return super().snapshot_app(sid)


def test_worker_crash_mid_rollout_replays_model_calls_without_paying_twice(world):
    sandbox = CrashOnce()
    w = world(WorldConfig(proposals=1, add_overlap_proposal=False), sandbox=sandbox)
    assert sandbox.crashed == 3
    assert w.export and len(w.export["accepted"]) == 1
    ledger = w.svc.gateway.ledger
    assert w.svc.gateway.double_charges() == 0
    assert sum(c.cached for c in ledger) >= 3                     # the replayed calls came from the cache
    assert len(w.provider.calls) == sum(not c.cached for c in ledger)   # every request reached the model once
    assert sandbox.leaked == 0                                     # sandboxes are destroyed even when a step dies


def test_review_files_round_trip(tmp_path, db):
    engine, svc, reviewer, provider, jobs = build_world(tmp_path / "w", db=db)
    run = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=5)
    engine.run_until_idle(run)
    [path] = svc.reviews.export_pending(tmp_path / "reviews", stage="H1")
    data = json.loads(path.read_text())
    assert data["allowed_decisions"] == ["approve"] and data["decision"] == ""
    assert svc.reviews.import_decisions(tmp_path / "reviews") == []          # blank files are skipped
    data.update(decision="approve", reason_code="patterns_ok", minutes_spent=3)
    path.write_text(json.dumps(data))
    assert svc.reviews.import_decisions(tmp_path / "reviews") == [data["review_id"]]
    engine.run_until_idle(run)
    assert engine.find(run, "generate")


def test_human_decisions_and_results_flow_back_through_the_knowledge_base(tmp_path, db):
    """Run 1 leaves documents behind; run 2's prompts contain them."""
    fix = {"files": {"instruction.md": "# clarified\n\nRead /app/data/input.txt and write the answer.\n"}}
    engine, svc, reviewer, provider, jobs = build_world(
        tmp_path, WorldConfig(weak_pass=(), strong_passes=False, proposals=2, add_overlap_proposal=False), db=db)
    reviewer.overrides.update({"H3": ("edit_instruction", "ambiguous_spec", fix),
                               "H5": ("shortcut", "shortcut_found", {})})

    def finish_run():
        run = engine.start_run("ingest", "batch", jobs, paid_approved=True, budget_usd=20)
        for _ in range(60):
            engine.run_until_idle(run)
            if not reviewer.answer_all():
                return run
        raise AssertionError("did not finish")

    finish_run()
    assert svc.kb.count("proposal") == 2 and svc.kb.count("repair_example") >= 1
    first_calls = len(provider.calls)

    reviewer.overrides["H1"] = ("approve", "too_generic", {"keep": []})        # run 2: delete the pattern at H1
    finish_run()
    assert svc.kb.count("rubric_counterexample") >= 1

    reviewer.overrides.pop("H1")
    finish_run()                                                               # run 3 sees everything from runs 1-2
    later = provider.calls[first_calls:]
    prompts = {role: [str(r.messages[0]["content"]) for rr, r in later if rr == role] for role in ("rubric_writer", "proposal_writer", "repairer")}
    assert any("Compared the program's output on the sample input" in p.split("Do not write items like them:")[1]
               for p in prompts["rubric_writer"])                              # deleted item shown as a counter-example
    assert any("Variant 0: produce a summary file" in p.split("Be different from these existing proposals:")[1]
               for p in prompts["proposal_writer"])                            # earlier proposals shown as "avoid"


class FailFirstRollouts(FakeSandbox):
    """The first two weak-model attempts fail with a non-retryable error, as the pilot's did (a zero byte
    that could not be stored, a tool name the host rejected): their steps end FAILED."""
    def __init__(self):
        super().__init__()
        self.rollouts = 0

    def snapshot_app(self, sid):
        if self.purposes[sid] == "rollout":
            self.rollouts += 1
            if self.rollouts <= 2:
                raise RuntimeError("could not store the attempt")
        return super().snapshot_app(sid)


def test_an_attempt_that_keeps_failing_is_retried_instead_of_rejecting_the_task(world):
    from taskforge.taskgen.config import TaskgenConfig
    sandbox = FailFirstRollouts()
    w = world(WorldConfig(proposals=1, add_overlap_proposal=False), sandbox=sandbox,
              config=TaskgenConfig(h4_sample_rate=0.0))
    steps = w.engine.all_steps(w.run_id)
    assert len([s for s in steps if s.step == "rollout" and s.status.value == "FAILED"]) == 2
    retried = sorted(s.key.rsplit(":", 1)[1] for s in steps if s.step == "rollout" and "#r" in s.key)
    assert retried == ["0#r1", "1#r1"]                          # just the failed ones, same indexes, new keys
    assert len(w.export["accepted"]) == 1 and "rollouts_failed" not in str(w.export["funnel"])
