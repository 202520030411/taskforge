"""Many workers at once on the Postgres engine, with crashes, stalls and races injected on purpose.
Skipped without TASKFORGE_PG_DSN."""
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from taskforge.core.engine import Done, Fork, Next, Spawn, StepStatus, Workflow
from taskforge.core.errors import WorkerCrash
from taskforge.core.gateway import FakeProvider, Gateway, ModelRequest, PgGatewayState, RateLimit
from taskforge.core.scope import CallScope
from tests.pgflows import TREE, WAITS, expected_total

pytestmark = pytest.mark.skipif(not os.environ.get("TASKFORGE_PG_DSN"), reason="set TASKFORGE_PG_DSN to run")


def engine_for(db, workflow, **kw):
    from taskforge.core.engine.postgres import PgEngine
    return PgEngine(workflow, None, db, **kw)


def wait_finished(eng, run, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if eng.summary(run)["finished"]:
            return
        time.sleep(0.05)
    raise AssertionError(f"run did not finish: {eng.summary(run)}")


def transitions(db, run, to_status):
    with db.tx() as c:
        return c.execute("SELECT step_run_id, count(*) AS n FROM events WHERE run_id = %s AND to_status = %s "
                         "GROUP BY step_run_id", (run, to_status)).fetchall()


def test_fan_out_and_joins_survive_crashes_with_16_threads(pg_db):
    from taskforge.core.engine.worker import Worker
    mids, leaves = 40, 6
    eng = engine_for(pg_db, TREE, lease_ttl_s=5)
    run = eng.start_run("root", "batch", {"mids": mids, "leaves": leaves, "crash": 0.15, "flaky": 0.15})
    worker = Worker(eng, {"cpu": 16}, poll_s=0.02, sweep_s=0.1).start()
    try:
        wait_finished(eng, run)
    finally:
        worker.stop()
    out = eng.get(run, "root", "batch").output
    assert out == {"total": expected_total(mids, leaves), "branches": mids}        # nothing lost, nothing counted twice
    steps = eng.all_steps(run)
    assert {s.status for s in steps} == {StepStatus.SUCCEEDED}
    n_tails = len(eng.find(run, "tail"))
    assert len(steps) == 1 + mids + mids * leaves + n_tails and 0 < n_tails < mids * leaves
    assert all(r["n"] == 1 for r in transitions(pg_db, run, "SUCCEEDED"))            # each step run committed once
    with pg_db.tx() as c:
        crashed = c.execute("SELECT count(*) AS n FROM events WHERE run_id = %s AND detail LIKE 'lease expired%%'",
                            (run,)).fetchone()["n"]
    assert crashed > 10                                                              # the crashes really happened


def test_stalled_worker_is_fenced_out(pg_db):
    """Worker A stalls past its lease with no heartbeat. B takes the step over and finishes it.
    When A wakes up, its commit must be refused and leave no trace."""
    wf = Workflow("z", "1")
    release_a = threading.Event()

    @wf.step("slow")
    def slow(ctx, inp):
        if ctx.attempt == 1:
            release_a.wait(20)                      # A hangs here, lease long expired
        return Next(Spawn("after", "x", {"by_attempt": ctx.attempt}), output={"by_attempt": ctx.attempt})

    @wf.step("after")
    def after(ctx, inp):
        return Done(inp)

    a, b = engine_for(pg_db, wf, lease_ttl_s=0.5, worker_id="A"), engine_for(pg_db, wf, lease_ttl_s=0.5, worker_id="B")
    run = a.start_run("slow")
    row = a.claim()
    thread = threading.Thread(target=a.execute, args=(row,))
    thread.start()
    time.sleep(0.8)
    assert b.sweep() == 1
    assert b.run_until_idle(run) == 2               # B runs `slow` (attempt 2) and `after`
    release_a.set()
    thread.join()
    slow_run = b.get(run, "slow", "batch")
    assert slow_run.output == {"by_attempt": 2} and slow_run.lease_owner.startswith("B")
    assert b.get(run, "after", "x").input == {"by_attempt": 2} and len(b.find(run, "after")) == 1
    assert [e["to_status"] for e in b.recent_events(run) if e["step"] == "slow"].count("SUCCEEDED") == 1
    assert [e for e in b.recent_events(run) if e["to_status"] == "FENCED" and e["worker"].startswith("A")]


def test_heartbeat_keeps_a_long_step_and_timeout_ends_a_stuck_one(pg_db):
    from taskforge.core.engine.worker import Worker
    wf = Workflow("h", "1")

    @wf.step("long")                                 # runs 4 lease lengths, well inside its timeout
    def long(ctx, inp):
        time.sleep(2.0)
        return Done(ctx.attempt)

    @wf.step("stuck", timeout_s=0.6)
    def stuck(ctx, inp):
        if ctx.attempt == 1:
            time.sleep(3.0)                          # past its own timeout: the heartbeat stops renewing
        return Done(ctx.attempt)

    eng = engine_for(pg_db, wf, lease_ttl_s=0.5)
    r1, r2 = eng.start_run("long"), eng.start_run("stuck")
    worker = Worker(eng, {"cpu": 4}, poll_s=0.02, heartbeat_s=0.15, sweep_s=0.1).start()
    try:
        wait_finished(eng, r1)
        wait_finished(eng, r2)
        time.sleep(2.0)                              # let the stuck first attempt wake up and get fenced
    finally:
        worker.stop()
    assert eng.get(r1, "long", "batch").output == 1 and not [e for e in eng.recent_events(r1) if "lease expired" in e["detail"]]
    assert eng.get(r2, "stuck", "batch").output == 2
    assert [e for e in eng.recent_events(r2) if e["to_status"] == "FENCED"]


def test_signals_are_never_lost_when_they_race_with_waits(pg_db):
    from taskforge.core.engine.worker import Worker
    n = 150
    eng = engine_for(pg_db, WAITS)
    run = eng.start_run("fan", "batch", {"n": n})
    worker = Worker(eng, {"cpu": 12}, poll_s=0.01).start()

    def fire(indexes):
        for i in indexes:
            eng.signal(run, f"sig:{i}", i)

    order = list(range(n))
    random.Random(7).shuffle(order)
    senders = [threading.Thread(target=fire, args=(order[k::6],)) for k in range(6)]   # signals race with the waits
    for t in senders:
        t.start()
    try:
        wait_finished(eng, run)
    finally:
        worker.stop()
        for t in senders:
            t.join()
    assert eng.get(run, "fan", "batch").output == list(range(n))


def test_branches_ending_at_the_same_instant_wake_the_parent_once(pg_db):
    from taskforge.core.engine.worker import Worker
    n = 24
    barrier = threading.Barrier(n)
    joins = []
    wf = Workflow("j", "1")

    @wf.step("parent")
    def parent(ctx, inp):
        if ctx.resume is None:
            return Fork(*[Spawn("child", f"c{i}") for i in range(n)])
        joins.append(len(ctx.resume.results))
        return Done(len(ctx.resume.results))

    @wf.step("child")
    def child(ctx, inp):
        barrier.wait(10)                             # all n children return, and commit, together
        return Done(1)

    for _ in range(3):
        barrier.reset()
        joins.clear()
        eng = engine_for(pg_db, wf)
        run = eng.start_run("parent")
        worker = Worker(eng, {"cpu": n}, poll_s=0.01).start()
        try:
            wait_finished(eng, run)
        finally:
            worker.stop()
        assert joins == [n] and eng.get(run, "parent", "batch").output == n


def test_budget_reservation_is_released_when_the_worker_dies(pg_db):
    wf = Workflow("b", "1")
    holder = {}

    @wf.step("pay")
    def pay(ctx, inp):
        token = holder["eng"].reserve(ctx.call_scope(), 1.0)
        if ctx.attempt == 1:
            raise WorkerCrash()                      # dies holding the reservation
        holder["eng"].settle(token, 0.4)
        return Done("ok")

    eng = holder["eng"] = engine_for(pg_db, wf)
    run = eng.start_run("pay", paid_approved=True, budget_usd=1.5)     # a leaked $1 would block the retry
    eng.run_until_idle(run)
    info = eng.run_info(run)
    assert eng.get(run, "pay", "batch").output == "ok"
    assert info["spent_usd"] == pytest.approx(0.4) and info["reserved_usd"] == pytest.approx(0.0)


def test_same_model_call_from_two_threads_is_paid_once(pg_db):
    def slow_script(role, request):
        time.sleep(0.4)
        return {"ok": True}
    provider = FakeProvider(slow_script, cost_per_call=0.5)

    class Budget:
        def reserve(self, scope, est): return scope.run_id
        def settle(self, token, actual): pass
    gateways = [Gateway({"fake": provider}, budget=Budget(), state=PgGatewayState(pg_db), owner=f"w{i}",
                        inflight_poll_s=0.05) for i in range(2)]
    req = ModelRequest(model="fake/m", messages=({"role": "user", "content": "hi"},))
    scope = CallScope("run", "sr", "run/step/key", 1)
    out = []
    threads = [threading.Thread(target=lambda g=g: out.append(g.complete(scope, "r", req))) for g in gateways]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(provider.calls) == 1 and out[0] == out[1]
    ledger = gateways[0].ledger
    assert sorted(c.cached for c in ledger) == [False, True] and gateways[0].double_charges() == 0


def test_shared_rate_limit_spreads_calls_out(pg_db):
    provider = FakeProvider(lambda role, request: "x")

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass
    gw = Gateway({"fake": provider}, budget=Budget(), state=PgGatewayState(pg_db),
                 rate_limits={"fake": RateLimit(capacity=5, refill_per_s=50)})
    started = time.monotonic()
    for i in range(20):                              # 5 at once, then 50 per second
        gw.complete(CallScope("run", "sr", "s", i), "r", ModelRequest(model="fake/m", messages=()))
    assert len(provider.calls) == 20 and time.monotonic() - started >= 0.25


def test_kill_dash_nine_on_a_worker_process_loses_nothing(pg_db):
    """Three real worker processes; one is killed with SIGKILL mid-run. The others finish its work."""
    mids, leaves, ttl = 12, 5, 1.5
    eng = engine_for(pg_db, TREE, lease_ttl_s=ttl)
    run = eng.start_run("root", "batch", {"mids": mids, "leaves": leaves, "sleep": 0.25})
    script = str(Path(__file__).with_name("pg_worker_main.py"))
    procs = [subprocess.Popen([sys.executable, script, pg_db.dsn, pg_db.schema, "4", str(ttl)],
                              stdout=subprocess.PIPE, text=True) for _ in range(3)]
    try:
        for p in procs:
            assert p.stdout.readline().strip() == "ready"
        time.sleep(1.2)
        with pg_db.tx() as c:
            held = c.execute("SELECT count(*) AS n FROM step_runs WHERE status = 'RUNNING' AND lease_owner LIKE %s",
                             (f"%:{procs[0].pid}:%",)).fetchone()["n"]
        os.kill(procs[0].pid, signal.SIGKILL)
        assert held > 0                               # it really died holding work
        wait_finished(eng, run, timeout=60)
    finally:
        for p in procs[1:]:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            p.wait(timeout=30)
    assert eng.get(run, "root", "batch").output == {"total": expected_total(mids, leaves), "branches": mids}
    assert all(r["n"] == 1 for r in transitions(pg_db, run, "SUCCEEDED"))
    expired = [e for e in eng.recent_events(run, 5000) if "lease expired" in e["detail"]]
    assert len(expired) == held                       # exactly the steps it held were taken over


def test_a_model_call_left_in_flight_by_a_dead_worker_is_taken_over_at_once(pg_db):
    """P4 drill kill_worker: the claim of a worker that lost its step must not block the retry for 600 s."""
    wf = Workflow("y", "1")

    @wf.step("s")
    def s(ctx, inp):
        return Done({})

    a = engine_for(pg_db, wf, lease_ttl_s=60, worker_id="A")
    a.start_run("s")
    row = a.claim()
    state = PgGatewayState(pg_db)
    assert state.claim("k1", "A", 600, row["id"]) == ("mine", None)
    assert state.claim("k1", "B", 600, row["id"])[0] == "busy"      # A still holds the step: B waits
    with pg_db.tx() as c:                                            # A dies; the sweeper re-queues the step
        c.execute("UPDATE step_runs SET status = 'PENDING', lease_owner = NULL WHERE id = %s", (row["id"],))
    assert state.claim("k1", "B", 600, row["id"])[0] == "mine"      # no 600 s wait
    assert state.claim("k2", "B", 600, None) == ("mine", None)       # calls outside any step keep the old rule
    assert state.claim("k2", "C", 600, None)[0] == "busy"

