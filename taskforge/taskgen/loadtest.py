"""Platform load test and failure drills (P4). No paid model is ever called here.

A load run is many weak-solver attempts on copies of the handwritten task `order-totals`, through the
real production path: the engine claims each `rollout` step, the step starts a sandbox, the agent loop
asks the model gateway for every turn and runs each tool call in the sandbox, then the attempt is graded
in a fresh container and its trajectory stored. Only the model is fake: `load_script` plays a solver
that looks around for a few turns, writes the reference solution and finishes, and the FakeProvider
waits a random 0.5-3 s per call to stand in for a real API.

    load_fanout      forks `attempts` rollouts over `tasks` copies of the task (each copy has its own image,
                     as distinct tasks do in a real run) and asks the pool to pre-start 10 sandboxes per
                     copy, the same hint the leak gate gives before a real difficulty check
    load_report()    numbers for one load run, read from Postgres
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from ..core.engine import Done, Fork, Spawn
from ..core.gateway.fake import tool_reply, turn_number
from . import package as pkg
from .workflow import _svc, files_of, make_task, step

TASK_DIR = Path(__file__).resolve().parents[2] / "tasks" / "handwritten" / "order-totals"
LOOK_AROUND = ("ls -la /app /app/samples /app/data", "head -5 /app/samples/input.csv",
               "wc -l /app/samples/input.csv /app/data/*",
               "python3 -c 'print(sum(i * i for i in range(300000)))'", "cat /app/samples/expected.json")
CHECK = ("python3 /app/order_totals.py /app/samples/input.csv /tmp/out.json && "
         "python3 -c \"import json; assert json.load(open('/tmp/out.json')) == "
         "json.load(open('/app/samples/expected.json')); print('same as the sample')\"")


def reference_program(files: Mapping[str, str]) -> str:
    """The Python program inside solution/solve.sh (between <<'PY' and PY)."""
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", files["solution/solve.sh"], re.S)
    if not m:
        raise ValueError("solution/solve.sh has no <<'PY' block")
    return m.group(1) + "\n"


def load_script(turns: int = 10, program: str | None = None):
    """A scripted solver: `turns - 3` look-around commands, then write the program, check it, finish."""
    program = program or reference_program(pkg.load_dir(TASK_DIR))

    def reply(role: str, request):
        t = turn_number(request)
        if t < turns - 3:
            return tool_reply(("run_shell", {"command": LOOK_AROUND[t % len(LOOK_AROUND)]}))
        if t == turns - 3:
            return tool_reply(("write_file", {"path": "/app/order_totals.py", "content": program}))
        if t == turns - 2:
            return tool_reply(("run_shell", {"command": CHECK}))
        return tool_reply(("finish", {}))
    return reply


class FaultFile:
    """Faults to inject, read from a JSON file that a drill script edits while the run goes on, e.g.
    {"rate_limit_share": 0.5}. Missing file = no faults. Re-read at most once a second."""

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._read_at, self._value = 0.0, {}

    def __call__(self) -> Mapping[str, Any]:
        if self.path is None:
            return {}
        with self._lock:
            if time.monotonic() - self._read_at >= 1.0:
                self._read_at = time.monotonic()
                try:
                    self._value = json.loads(self.path.read_text() or "{}")
                except (OSError, ValueError):
                    self._value = {}
            return self._value


def task_copy(files: Mapping[str, str], n: int) -> dict[str, str]:
    """Copy n of the task: identical except a label in the Dockerfile, so every copy has its own image."""
    out = dict(files)
    out["environment/Dockerfile"] = files["environment/Dockerfile"].rstrip("\n") + f'\nLABEL taskforge.load.copy="{n}"\n'
    return out


def prepare(svc, tasks: int, *, tag: str) -> list[str]:
    """Store `tasks` copies of the handwritten task; return their task ids."""
    base = pkg.load_dir(TASK_DIR)
    return [svc.store.put(make_task(svc, task_copy(base, i), proposal_id=f"load:{tag}:{i}", pattern_id="load",
                                    origin="human", parents=(), produced_by="loadtest"))
            for i in range(tasks)]


@step("load_fanout")
def load_fanout(ctx, inp):
    """Attempts that end FAILED (infrastructure) are run again under `#rN` keys, up to `rollout_retries`
    rounds, the way the difficulty check does it (decision D-034)."""
    svc = _svc(ctx)
    task_ids, attempts = inp["task_ids"], int(inp["attempts"])

    def spawns(indexes, retry):
        suffix = f"#r{retry}" if retry else ""
        return [Spawn("rollout", f"load:{ctx.key}:{i}{suffix}",
                      {"task_id": task_ids[i % len(task_ids)], "index": i, "role": "weak_solver"}) for i in indexes]

    if ctx.resume is None:
        # the hint the difficulty check gives: 2 sandboxes per attempt (one to work in, one to grade in);
        # prewarm_per_task overrides it (the first P4 ramp gave 10 per copy, for 50-100 attempts each)
        per_task = inp.get("prewarm_per_task")
        for t, task_id in enumerate(task_ids):
            n = int(per_task) if per_task is not None else 2 * len(range(t, attempts, len(task_ids)))
            if n:
                svc.sandbox.prewarm(svc.sandbox.build(files_of(svc, task_id)), n)
        return Fork(*spawns(range(attempts), 0), state={"retries": 0, "done": [], "failed_rounds": []})
    state = ctx.resume.state or {}
    done = state.get("done", []) + [{"index": r.output["index"], "passed": bool(r.output["passed"])}
                                    for r in ctx.resume.results if r.status == "SUCCEEDED"]
    failed = sorted(int(r.root_key.rsplit(":", 1)[1].split("#")[0]) for r in ctx.resume.results
                    if r.status != "SUCCEEDED")
    rounds = state.get("failed_rounds", []) + [len(failed)]
    tries = state.get("retries", 0)
    if failed and tries < svc.config.rollout_retries:
        return Fork(*spawns(failed, tries + 1), state={"retries": tries + 1, "done": done, "failed_rounds": rounds})
    return Done({"attempts": attempts, "succeeded": len(done), "passed": sum(d["passed"] for d in done),
                 "failed": len(failed), "failed_per_round": rounds})


def start(platform, *, attempts: int, tasks: int, prewarm_per_task: int | None = None, tag: str | None = None) -> str:
    tag = tag or time.strftime("%H%M%S")
    task_ids = prepare(platform.svc, tasks, tag=tag)
    return platform.engine.start_run("load_fanout", f"load-{tag}", {"task_ids": task_ids, "attempts": attempts,
                                                                    "prewarm_per_task": prewarm_per_task},
                                     paid_approved=True, budget_usd=1e6)


# ------------------------------------------------------------------ report
def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 2)


def load_report(db, run_id: str) -> dict:
    """Throughput, attempt time and queue wait for one load run; model calls paid and paid twice.
    An attempt is one index; it may have several rollout steps when it was re-run (`#rN` keys)."""
    with db.tx() as c:
        rows = c.execute(
            "SELECT key, status, attempt, (output->>'passed')::boolean AS passed, "
            "EXTRACT(EPOCH FROM finished_at - started_at) AS run_s, created_at, finished_at "
            "FROM step_runs WHERE run_id = %s AND step = 'rollout'", (run_id,)).fetchall()
        calls = c.execute(
            "SELECT count(*) FILTER (WHERE NOT cached) AS paid, count(*) FILTER (WHERE cached) AS cached, "
            "avg(latency_s) FILTER (WHERE NOT cached) AS latency FROM llm_calls WHERE run_id = %s",
            (run_id,)).fetchone()
        doubles = c.execute(
            "SELECT count(*) AS n FROM (SELECT idem_key FROM llm_calls WHERE run_id = %s AND NOT cached "
            "GROUP BY idem_key HAVING count(*) > 1) d", (run_id,)).fetchone()["n"]
        fanout = c.execute("SELECT status, output FROM step_runs WHERE run_id = %s AND step = 'load_fanout'",
                           (run_id,)).fetchone()
        missing = c.execute(   # a finished attempt whose trajectory record is not there = lost data
            "SELECT count(*) AS n FROM step_runs s WHERE s.run_id = %s AND s.step = 'rollout' AND s.status = 'SUCCEEDED' "
            "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.id = s.output->>'traj_id')", (run_id,)).fetchone()["n"]
    index = lambda key: key.rsplit(":", 1)[1].split("#")[0]      # noqa: E731
    done = [r for r in rows if r["status"] == "SUCCEEDED"]
    start_t = min((r["created_at"] for r in rows), default=None)
    end_t = max((r["finished_at"] for r in rows if r["finished_at"]), default=None)
    wall = (end_t - start_t).total_seconds() if start_t and end_t else None
    run_s = [float(r["run_s"]) for r in done if r["run_s"] is not None]
    return {
        "run_id": run_id, "attempts": len({index(r["key"]) for r in rows}),
        "succeeded": len({index(r["key"]) for r in done}),
        "passed": len({index(r["key"]) for r in done if r["passed"]}),
        "rollout_steps": len(rows), "rerun_attempts": sum("#" in r["key"] for r in rows),
        "steps_failed": sum(r["status"] == "FAILED" for r in rows),
        "engine_retries": sum(max(0, r["attempt"] - 1) for r in rows),
        "wall_s": round(wall, 1) if wall else None,
        "attempts_per_min": round(len(done) / wall * 60, 1) if wall else None,
        "attempt_s": {"p50": _pct(run_s, 0.5), "p95": _pct(run_s, 0.95),
                      "mean": round(statistics.fmean(run_s), 2) if run_s else None},
        "model_calls": {"paid": calls["paid"], "from_cache": calls["cached"],
                        "mean_latency_s": round(float(calls["latency"]), 2) if calls["latency"] else None},
        "double_charges": doubles,
        "missing_trajectories": missing,
        "fanout": {"status": fanout["status"], "output": fanout["output"]} if fanout else None,
    }


def worker_env() -> dict[str, str]:
    """The environment the load-test processes run with: the stack's, minus every API key."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["TASKFORGE_PROFILE"] = "loadtest"
    return env
