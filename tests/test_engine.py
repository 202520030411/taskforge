"""The engine's rules (DESIGN.md section 4). Every test runs on the in-memory engine and on Postgres."""
from taskforge.core.engine import Done, Fork, Next, Resource, Spawn, StepStatus, Wait, Workflow
from taskforge.core.errors import Retryable, WorkerCrash


def test_chain_runs_in_order_and_finishes(make_engine):
    wf = Workflow("t", "1")
    seen = []

    @wf.step()
    def a(ctx, inp):
        seen.append("a")
        return Next(Spawn("b", ctx.key, {"x": inp["x"] + 1}))

    @wf.step()
    def b(ctx, inp):
        seen.append(("b", inp["x"]))
        return Done({"x": inp["x"]})

    eng = make_engine(wf)
    run = eng.start_run("a", "k", {"x": 1})
    eng.run_until_idle()
    assert seen == ["a", ("b", 2)]
    assert eng.summary(run)["finished"]
    assert eng.get(run, "b", "k").output == {"x": 2}


def test_fork_join_collects_every_branch_including_chains_and_fan_outs(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def parent(ctx, inp):
        if ctx.resume is None:
            return Fork(*[Spawn("child", f"c{i}", {"i": i}) for i in range(3)], state={"note": "kept"})
        assert ctx.resume.kind == "join" and ctx.resume.state == {"note": "kept"}
        return Done(sorted((r.root_key, r.step, r.output) for r in ctx.resume.results))

    @wf.step()
    def child(ctx, inp):
        if inp["i"] == 0:
            return Done(0)
        if inp["i"] == 1:
            return Next(Spawn("leaf", "c1-leaf", {"v": 10}))                     # chain
        return Next(Spawn("leaf", "c2-x", {"v": 20}), Spawn("leaf", "c2-y", {"v": 21}))  # fan-out

    @wf.step()
    def leaf(ctx, inp):
        return Done(inp["v"])

    eng = make_engine(wf)
    run = eng.start_run("parent")
    eng.run_until_idle()
    out = eng.get(run, "parent", "batch").output
    assert out == [["c0", "child", 0], ["c1", "leaf", 10], ["c2", "leaf", 20], ["c2", "leaf", 21]]


def test_nested_fork_and_failed_branch_is_reported_not_lost(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def top(ctx, inp):
        if ctx.resume is None:
            return Fork(Spawn("mid", "m"), Spawn("bad", "b"))
        return Done(sorted((r.root_step, r.status) for r in ctx.resume.results))

    @wf.step()
    def mid(ctx, inp):
        if ctx.resume is None:
            return Fork(Spawn("leaf", "l1"), Spawn("leaf", "l2"))
        return Done(len(ctx.resume.results))

    @wf.step()
    def leaf(ctx, inp):
        return Done(1)

    @wf.step(max_attempts=1)
    def bad(ctx, inp):
        raise ValueError("boom")

    eng = make_engine(wf)
    run = eng.start_run("top")
    eng.run_until_idle()
    assert eng.get(run, "top", "batch").output == [["bad", "FAILED"], ["mid", "SUCCEEDED"]]
    assert eng.get(run, "mid", "m").output == 2
    assert "boom" in eng.get(run, "bad", "b").error


def test_duplicate_spawns_are_ignored_and_do_not_block_the_join(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def parent(ctx, inp):
        if ctx.resume is None:
            return Fork(Spawn("child", "same"), Spawn("child", "same"))
        return Done(len(ctx.resume.results))

    @wf.step()
    def child(ctx, inp):
        return Done("x")

    eng = make_engine(wf)
    run = eng.start_run("parent")
    eng.run_until_idle()
    assert eng.get(run, "parent", "batch").output == 1
    assert len(eng.find(run, "child")) == 1


def test_wait_and_signal_in_either_order(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def ask(ctx, inp):
        if ctx.resume is None:
            return Wait(f"review:{ctx.key}", state={"asked": True})
        return Done({"answer": ctx.resume.payload, "state": ctx.resume.state})

    eng = make_engine(wf)
    r1 = eng.start_run("ask", "early")
    eng.run_until_idle()
    assert eng.summary(r1)["waiting_on_signals"] == ["review:early"]
    eng.signal(r1, "review:early", "yes")
    eng.run_until_idle()
    assert eng.get(r1, "ask", "early").output == {"answer": "yes", "state": {"asked": True}}

    r2 = eng.start_run("ask", "late")
    eng.signal(r2, "review:late", "no")          # arrives before anyone waits
    eng.run_until_idle()
    assert eng.get(r2, "ask", "late").output["answer"] == "no"


def test_retryable_is_retried_with_backoff_then_fails_when_attempts_run_out(make_engine):
    wf = Workflow("t", "1")
    calls = []

    @wf.step(max_attempts=3)
    def flaky(ctx, inp):
        calls.append(ctx.attempt)
        if inp["fail_times"] >= ctx.attempt:
            raise Retryable("sandbox fault")
        return Done("ok")

    eng = make_engine(wf, backoff_s=5)
    ok = eng.start_run("flaky", "a", {"fail_times": 2})
    bad = eng.start_run("flaky", "b", {"fail_times": 9})
    eng.run_until_idle()
    assert eng.get(ok, "flaky", "a").status == StepStatus.SUCCEEDED
    assert eng.get(bad, "flaky", "b").status == StepStatus.FAILED
    delays = [e["detail"].split(":")[0] for e in eng.recent_events(ok) if e["detail"].startswith("retry in")]
    assert delays == ["retry in 5s", "retry in 10s"]      # back-off doubles


def test_back_pressure_retry_does_not_use_up_attempts(make_engine):
    wf = Workflow("t", "1")
    tries = []

    @wf.step(max_attempts=1)
    def busy(ctx, inp):
        tries.append(1)
        if len(tries) < 4:
            raise Retryable("pool full", delay_s=1, consume_attempt=False)
        return Done("got a slot")

    eng = make_engine(wf)
    run = eng.start_run("busy")
    eng.run_until_idle()
    assert eng.get(run, "busy", "batch").output == "got a slot"


def test_worker_crash_reruns_the_step_and_commits_only_once(make_engine):
    wf = Workflow("t", "1")
    side_effects = []

    @wf.step()
    def work(ctx, inp):
        side_effects.append(ctx.attempt)
        if ctx.attempt == 1:
            raise WorkerCrash()
        return Next(Spawn("after", "x"))

    @wf.step()
    def after(ctx, inp):
        return Done()

    eng = make_engine(wf)
    run = eng.start_run("work")
    eng.run_until_idle()
    assert side_effects == [1, 2]                       # ran at least once ...
    assert len(eng.find(run, "after")) == 1             # ... but its successor exists once
    assert eng.summary(run)["finished"]


def test_paid_steps_wait_for_approval_and_budget_pauses_the_run(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def free(ctx, inp):
        return Next(Spawn("paid", "p"))

    @wf.step(resource=Resource.LLM)
    def paid(ctx, inp):
        eng.settle(eng.reserve(ctx.call_scope(), 1.0), 1.0)
        return Next(Spawn("paid2", "p"))

    @wf.step(resource=Resource.LLM)
    def paid2(ctx, inp):
        eng.settle(eng.reserve(ctx.call_scope(), 1.0), 1.0)
        return Done("expensive")

    eng = make_engine(wf)
    run = eng.start_run("free")
    eng.run_until_idle()
    s = eng.summary(run)
    assert s["blocked_paid_steps"] == 1 and not s["finished"]

    eng.approve(run, budget_usd=1.5)
    eng.run_until_idle()
    assert eng.run_info(run)["status"] == "PAUSED_BUDGET"
    assert eng.get(run, "paid2", "p").status == StepStatus.PENDING

    eng.approve(run, budget_usd=3.0)
    eng.run_until_idle()
    assert eng.get(run, "paid2", "p").output == "expensive"


def test_bad_outcome_fails_the_step_without_side_effects(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def oops(ctx, inp):
        return Next(Spawn("nope", "x"))

    eng = make_engine(wf)
    run = eng.start_run("oops")
    eng.run_until_idle()
    sr = eng.get(run, "oops", "batch")
    assert sr.status == StepStatus.FAILED and "nope" in sr.error
    assert len(eng.all_steps(run)) == 1


def test_every_state_change_is_in_the_event_log(make_engine):
    wf = Workflow("t", "1")

    @wf.step()
    def a(ctx, inp):
        return Done()

    eng = make_engine(wf)
    run = eng.start_run("a")
    eng.run_until_idle()
    assert [(e["from_status"], e["to_status"]) for e in eng.recent_events(run)] == [
        (None, "PENDING"), ("PENDING", "RUNNING"), ("RUNNING", "SUCCEEDED")]


def test_operator_can_rerun_a_finished_branch_while_its_parent_waits(make_engine):
    wf = Workflow("t", "1")
    calls = {"n": 0}

    @wf.step()
    def parent(ctx, inp):
        if ctx.resume is None:
            return Fork(Spawn("child", "a"), Spawn("slow", "s"), Spawn("gate", "g"))
        return Done(sorted((r.key, r.output) for r in ctx.resume.results))

    @wf.step()
    def child(ctx, inp):
        return Done("ok")

    @wf.step()
    def slow(ctx, inp):                       # keeps the parent waiting, like proposals still at H2
        if ctx.resume is None:
            return Wait("later")
        return Done("late")

    @wf.step()
    def gate(ctx, inp):
        calls["n"] += 1
        return Done("bad result" if calls["n"] == 1 else "good result")

    eng = make_engine(wf)
    run = eng.start_run("parent")
    eng.run_until_idle(run)
    assert eng.get(run, "parent", "batch").status == StepStatus.WAITING
    assert eng.reopen_branch(run, "gate", "g", "g#retry1")
    eng.run_until_idle(run)
    eng.signal(run, "later")
    eng.run_until_idle(run)
    assert eng.get(run, "parent", "batch").output == [["a", "ok"], ["g#retry1", "good result"], ["s", "late"]]
    import pytest
    with pytest.raises(ValueError):           # the parent has moved on now
        eng.reopen_branch(run, "gate", "g#retry1", "g#retry2")
