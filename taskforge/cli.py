"""Command line: `python -m taskforge.cli <command>`.

One-process tools (no database needed):
  demo         whole workflow with scripted models and an automatic reviewer
  pairs        import stored Harbor jobs and count trajectories and strong/weak pairs
  check-task   run the executed gates (oracle, initial, scripted hacks, leak) on one task directory

The stack (settings come from TASKFORGE_* variables; `deploy/stack.sh env` prints them):
  db init      create the tables
  worker       claim and run steps until stopped
  sandboxd     the sandbox service (the only process that controls Docker)
  api          review console, control API and /metrics
  run start    start a run            run status   show a run
  review export / review import       the file-based review flow
  resume       import the filled-in review files of one stage (the spec's `resume --stage`)
  demo --stack drive a scripted run through a running stack, answering reviews over HTTP
  mcp          expose the sandbox tools to MCP clients over stdio
  smoke        PAID (cents): one real call per kind - Opus one-shot, Opus tool loop, Qwen tool loop
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path


def _sandbox(kind: str):
    from .core.sandbox import DockerSandbox, FakeSandbox
    return DockerSandbox() if kind == "docker" else FakeSandbox()


def _platform(service: str):
    from .core import obs
    from .runtime import Settings, build_from_settings
    obs.logs.setup()
    return build_from_settings(Settings.from_env(), service=service)


# --------------------------------------------------------------- one process
def cmd_demo(args) -> int:
    if args.stack:
        return _demo_stack(args)
    from .taskgen.config import TaskgenConfig
    from .taskgen.demo import OrderTotalsKit, WorldConfig, run_world
    world = WorldConfig(**json.loads(args.world)) if args.world else WorldConfig()
    kw = {}
    if args.sandbox == "docker":
        world = WorldConfig(**{**world.__dict__, "proposals": min(world.proposals, args.proposals)})
        kw = {"sandbox": _sandbox("docker"), "kit": OrderTotalsKit(world), "config": TaskgenConfig(h4_sample_rate=0.0)}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        w = run_world(Path(tmp), world, **kw)
    summary = w.engine.summary(w.run_id)
    steps = w.engine.all_steps(w.run_id)
    failed = [{"step": s.step, "key": s.key, "error": (s.error or "").strip().splitlines()[-1:]}
              for s in steps if s.status.value == "FAILED"]
    ledger = w.svc.gateway.ledger
    out = {"summary": summary, "failed_steps": failed, "export": w.export,
           "steps_by_name": dict(Counter(s.step for s in steps)),
           "sandboxes": {"created": w.svc.sandbox.created, "destroyed": w.svc.sandbox.destroyed},
           "model_calls": {"paid": sum(not c.cached for c in ledger), "from_cache": sum(c.cached for c in ledger)},
           "wall_s": round(time.monotonic() - started, 1)}
    print(json.dumps(out, indent=1, default=str))
    return 0 if summary["finished"] and not failed else 1


DEFAULT_ANSWERS = {"H1": ("approve", "patterns_ok"), "H2": ("keep", "on_target"), "H3": ("drop", "cannot_fix"),
                   "H4": ("keep", "looks_fine"), "H5": ("clean", "clean"), "H6": ("export", "ready")}


def _demo_stack(args) -> int:
    """Start a scripted run on a running stack and answer every review over HTTP, like a person at the console."""
    import httpx
    http = httpx.Client(base_url=args.stack, timeout=30)
    started = time.monotonic()
    run_id = http.post("/api/runs", json={"demo_jobs": True, "paid_approved": True, "budget_usd": args.budget}).json()["run_id"]
    print(f"run {run_id} started", file=sys.stderr)
    answered = 0
    while time.monotonic() - started < args.timeout:
        for item in http.get("/api/reviews", params={"run_id": run_id}).json():
            decision, reason = DEFAULT_ANSWERS[item["stage"]]
            http.post(f"/api/reviews/{item['review_id']}", json={"decision": decision, "reason_code": reason,
                                                                 "minutes_spent": 1.0, "reviewer": "auto"}).raise_for_status()
            answered += 1
        state = http.get(f"/api/runs/{run_id}").json()
        if state["finished"]:
            print(json.dumps({**state, "reviews_answered": answered, "wall_s": round(time.monotonic() - started, 1)},
                             indent=1, default=str))
            return 0 if state["export"] and not state["failed_steps"] else 1
        time.sleep(0.5)
    print(json.dumps(http.get(f"/api/runs/{run_id}").json(), indent=1, default=str))
    return 1


def cmd_pairs(args) -> int:
    from .taskgen.pairs import load_job, make_pairs
    trajs = [t for j in args.strong for t in load_job(Path(j), "strong")]
    trajs += [t for j in args.weak for t in load_job(Path(j), "weak")]
    pairs = make_pairs(trajs)
    by = Counter((t.cohort, t.passed) for t in trajs)
    print(json.dumps({"trajectories": len(trajs),
                      "strong": {"passed": by[("strong", True)], "failed": by[("strong", False)]},
                      "weak": {"passed": by[("weak", True)], "failed": by[("weak", False)]},
                      "tasks": len({t.task_ref for t in trajs}), "pairs": len(pairs),
                      "paired_tasks": [p.task_ref for p in pairs]}, indent=1))
    return 0


def cmd_check_task(args) -> int:
    from .taskgen import package as pkg
    from .taskgen.verify import gate_initial, gate_leak, gate_oracle, run_scripted_hacks
    files = pkg.load_dir(Path(args.task))
    problems = pkg.validate_layout(files)
    if problems:
        print(json.dumps({"layout_problems": problems}, indent=1))
        return 1
    sb = _sandbox(args.sandbox)
    started = time.monotonic()
    oracle, initial = gate_oracle(sb, files), gate_initial(sb, files)
    hacks = run_scripted_hacks(sb, files)
    leak = gate_leak(files)
    lat = sorted(getattr(sb, "start_latencies", []))
    out = {"oracle": oracle.passed, "oracle_reasons": oracle.reasons, "initial": initial.passed,
           "hacks": hacks, "hack_gate": not any(v["passed"] for v in hacks.values()),
           "leak": leak.passed, "leak_reasons": leak.reasons, "leak_details": leak.details,
           "sandboxes": {"created": sb.created, "destroyed": sb.destroyed},
           "start_latency_s": {"n": len(lat), "median": round(lat[len(lat) // 2], 2), "max": round(lat[-1], 2)} if lat else None,
           "wall_s": round(time.monotonic() - started, 1)}
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["oracle"] and out["initial"] and out["hack_gate"] and out["leak"] else 1


# ------------------------------------------------------------------ the stack
def cmd_db(args) -> int:
    from .runtime import Settings, make_database
    db = make_database(Settings.from_env())
    db.init_schema()
    print(f"tables ready in schema {db.schema!r}")
    db.close()
    return 0


def _wait_for_stop() -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: stop.set())
    stop.wait()


def cmd_worker(args) -> int:
    from .core import obs
    from .core.engine.worker import DEFAULT_CONCURRENCY, Worker
    platform = _platform("taskforge-worker")
    concurrency = dict(DEFAULT_CONCURRENCY)
    for part in filter(None, (args.concurrency or "").split(",")):
        name, _, n = part.partition("=")
        concurrency[name] = int(n)
    worker = Worker(platform.engine, concurrency).start()
    print(f"worker {platform.engine.worker_id} serving {concurrency}", file=sys.stderr, flush=True)
    _wait_for_stop()
    worker.stop()
    obs.tracing.flush()
    platform.close()
    return 0


def cmd_sandboxd(args) -> int:
    import uvicorn
    from .core import obs
    from .core.sandbox.service import create_app
    from .runtime import Settings
    obs.logs.setup()
    obs.tracing.configure("taskforge-sandboxd", Settings.from_env().otlp)
    uvicorn.run(create_app(max_containers=args.max), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_api(args) -> int:
    import uvicorn
    from .api.app import create_app
    platform = _platform("taskforge-api")
    uvicorn.run(create_app(platform), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_run(args) -> int:
    platform = _platform("taskforge-cli")
    engine = platform.engine
    if args.action == "start":
        if args.from_patterns:
            run_id = engine.start_run("generate", "batch", {"pattern_ids": args.from_patterns},
                                      paid_approved=args.approve is not None, budget_usd=args.approve or 0.0)
            print(run_id)
            platform.close()
            return 0
        if args.demo_jobs:
            from .taskgen.demo import write_fake_jobs
            jobs = write_fake_jobs(Path(tempfile.mkdtemp(prefix="taskforge-demo-jobs-")))
        else:
            if not args.strong or not args.weak:
                print("give --strong and --weak job directories, or --demo-jobs", file=sys.stderr)
                return 2
            jobs = {"strong_jobs": args.strong, "weak_jobs": args.weak}
        run_id = engine.start_run("ingest", "batch", jobs, paid_approved=args.approve is not None,
                                  budget_usd=args.approve or 0.0)
        print(run_id)
    elif args.action == "reopen":
        print(engine.reopen_branch(args.run_id, args.step, args.key, args.new_key))
    else:
        steps = engine.all_steps(args.run_id)
        export = next((s.output for s in steps if s.step == "export" and s.output), None)
        print(json.dumps({**engine.summary(args.run_id), "pending_reviews": len(platform.svc.reviews.pending(args.run_id)),
                          "export": export}, indent=1, default=str))
    platform.close()
    return 0


def cmd_review(args) -> int:
    platform = _platform("taskforge-cli")
    reviews = platform.svc.reviews
    if args.action == "export":
        paths = reviews.export_pending(Path(args.dir), stage=args.stage, run_id=args.run)
        print(f"wrote {len(paths)} review file(s) to {args.dir}; fill in `decision` and `reason_code`, then import")
    else:
        done = reviews.import_decisions(Path(args.dir), stage=args.stage)
        print(f"submitted {len(done)} decision(s): {done}")
    platform.close()
    return 0


def cmd_smoke(args) -> int:
    """Checks both real connections end to end with the smallest calls that exercise every code path."""
    from .core.agent import SOLVER_TOOLS, AgentBudget, run_agent
    from .core.errors import BudgetExceeded
    from .core.gateway import Gateway, ModelRequest
    from .core.sandbox import FakeSandbox
    from .core.scope import CallScope
    from .runtime import real_providers
    from .taskgen.agents.llm import parse_json_reply
    from .taskgen.config import PILOT, QWEN, OPUS

    class Cap:
        def __init__(self, limit): self.limit, self.spent = limit, 0.0
        def reserve(self, scope, est):
            if self.spent + est > self.limit:
                raise BudgetExceeded(f"smoke cap ${self.limit} reached")
            return est
        def settle(self, token, actual): self.spent += actual

    cap = Cap(args.cap)
    gw = Gateway(real_providers(), budget=cap)
    n = iter(range(1, 1000))
    tag = f"smoke-{int(time.time())}"
    scope = lambda: CallScope(tag, tag, f"{tag}/smoke", next(n))   # noqa: E731
    report = {}

    t0 = time.monotonic()
    r = gw.complete(scope(), "rubric_scorer", ModelRequest(
        model=OPUS, max_tokens=4000, params=PILOT.role_params["rubric_scorer"],
        messages=({"role": "user", "content": 'Score: is 2 + 2 = 4? Reply with only {"scores": {"1": 1}} or {"scores": {"1": 0}}.'},)))
    report["opus_one_shot"] = {"parsed": parse_json_reply(r.text, ("scores",)), "served_by": r.served_by,
                               "refusal": r.refusal, "tokens_in_out": [r.usage.input_tokens, r.usage.output_tokens],
                               "cost_usd": round(r.cost_usd, 5), "seconds": round(time.monotonic() - t0, 1)}

    task = ("There is a file /app/a.txt. Use run_shell to print it, then write its content in capital letters to "
            "/app/b.txt with write_file, read /app/b.txt back with read_file to check it, and call finish.")
    for label, model, role, params in (("opus_tool_loop", OPUS, "hacker", PILOT.role_params["hacker"]),
                                       ("qwen_tool_loop", QWEN, "weak_solver", PILOT.solver_params)):
        sb = FakeSandbox()
        sid = sb.create(sb.build({"environment/app/a.txt": "hello smoke"}), purpose="rollout")
        t0 = time.monotonic()
        res = run_agent(gateway=gw, next_scope=scope, role=role, model=model, system="You are a coding agent working "
                        "through tools in a Linux container. Call finish when done.", task_prompt=task, sandbox=sb,
                        sid=sid, tools=SOLVER_TOOLS, params=params, max_tokens=4000,
                        budget=AgentBudget(max_steps=6))
        calls = [c for c in gw.ledger if c.role == role]
        report[label] = {"exit": res.exit, "turns": len(res.steps) - 1, "b_txt": sb.boxes[sid].get("/app/b.txt"),
                         "tools_used": [c.name for s in res.steps for c in s.tool_calls],
                         "calls": len(calls), "cost_usd": round(sum(c.cost_usd for c in calls), 5),
                         "seconds": round(time.monotonic() - t0, 1)}
    report["total_usd"] = round(cap.spent, 5)
    report["checks"] = {
        "opus_json_ok": report["opus_one_shot"]["parsed"].get("scores") is not None,
        "opus_loop_finished": report["opus_tool_loop"]["exit"] == "finished" and report["opus_tool_loop"]["b_txt"] == "HELLO SMOKE",
        "qwen_loop_finished": report["qwen_tool_loop"]["exit"] == "finished" and report["qwen_tool_loop"]["b_txt"] == "HELLO SMOKE",
    }
    print(json.dumps(report, indent=1, default=str))
    return 0 if all(report["checks"].values()) else 1


def cmd_mcp(args) -> int:
    from .core.tools.mcp import serve_stdio
    from .taskgen import package as pkg
    sandbox = _sandbox(args.sandbox)
    image = sandbox.build(pkg.load_dir(Path(args.task)))
    return serve_stdio(sandbox, image)


def cmd_load(args) -> int:
    """P4 load test: start a load run, or print its numbers. Needs TASKFORGE_PROFILE=loadtest (no paid model)."""
    from .taskgen import loadtest
    platform = _platform("taskforge-load")
    if platform.svc.gateway.providers["openrouter"].__class__.__name__ != "FakeProvider":
        print("refusing: the load test runs only on fake models (TASKFORGE_PROFILE=loadtest)", file=sys.stderr)
        return 2
    if args.action == "start":
        print(loadtest.start(platform, attempts=args.attempts, tasks=args.tasks, prewarm_per_task=args.prewarm))
    else:
        print(json.dumps(loadtest.load_report(platform.db, args.run_id), indent=1, default=str))
    platform.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="taskforge")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="whole workflow with scripted models")
    d.add_argument("--sandbox", choices=("fake", "docker"), default="fake")
    d.add_argument("--world", help="JSON object of WorldConfig fields")
    d.add_argument("--proposals", type=int, default=1, help="with --sandbox docker: proposals per pattern")
    d.add_argument("--stack", metavar="URL", help="drive a running stack through its api, e.g. http://127.0.0.1:58800")
    d.add_argument("--budget", type=float, default=5.0)
    d.add_argument("--timeout", type=float, default=900.0)
    d.set_defaults(fn=cmd_demo)

    p = sub.add_parser("pairs", help="import stored Harbor jobs and pair them")
    p.add_argument("--strong", nargs="+", required=True)
    p.add_argument("--weak", nargs="+", required=True)
    p.set_defaults(fn=cmd_pairs)

    c = sub.add_parser("check-task", help="run the executed gates on a task directory")
    c.add_argument("task")
    c.add_argument("--sandbox", choices=("fake", "docker"), default="docker")
    c.set_defaults(fn=cmd_check_task)

    db = sub.add_parser("db", help="database setup")
    db.add_argument("action", choices=("init",))
    db.set_defaults(fn=cmd_db)

    w = sub.add_parser("worker", help="claim and run steps until stopped")
    w.add_argument("--concurrency", help="threads per resource class, e.g. cpu=4,llm=16,sandbox=8,agent=8,human=2")
    w.set_defaults(fn=cmd_worker)

    s = sub.add_parser("sandboxd", help="the sandbox service")
    s.add_argument("--port", type=int, default=58700)
    s.add_argument("--max", type=int, default=None, help="maximum containers (default: TASKFORGE_MAX_SANDBOXES or 48)")
    s.set_defaults(fn=cmd_sandboxd)

    a = sub.add_parser("api", help="review console, control API, /metrics")
    a.add_argument("--port", type=int, default=58800)
    a.set_defaults(fn=cmd_api)

    r = sub.add_parser("run", help="start or inspect a run")
    rs = r.add_subparsers(dest="action", required=True)
    start = rs.add_parser("start")
    start.add_argument("--strong", nargs="*")
    start.add_argument("--weak", nargs="*")
    start.add_argument("--demo-jobs", action="store_true")
    start.add_argument("--from-patterns", nargs="+", metavar="PATTERN_ID",
                       help="skip mining: start at proposal generation from patterns an earlier run mined")
    start.add_argument("--approve", type=float, metavar="BUDGET_USD", help="approve paid steps with this budget")
    status = rs.add_parser("status")
    status.add_argument("run_id")
    reopen = rs.add_parser("reopen", help="operator: run a step that ended a branch again, as a new branch")
    reopen.add_argument("run_id")
    reopen.add_argument("step")
    reopen.add_argument("key")
    reopen.add_argument("--new-key", required=True)
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("review", help="file-based review flow")
    v.add_argument("action", choices=("export", "import"))
    v.add_argument("dir")
    v.add_argument("--stage")
    v.add_argument("--run")
    v.set_defaults(fn=cmd_review)

    res = sub.add_parser("resume", help="import the filled-in review files of one stage and let the run continue")
    res.add_argument("--stage", required=True)
    res.add_argument("--dir", required=True)
    res.set_defaults(fn=cmd_review, action="import", run=None)

    ld = sub.add_parser("load", help="P4 load test on fake models (TASKFORGE_PROFILE=loadtest)")
    ldx = ld.add_subparsers(dest="action", required=True)
    lds = ldx.add_parser("start")
    lds.add_argument("--attempts", type=int, required=True)
    lds.add_argument("--tasks", type=int, required=True, help="copies of the task, each with its own image")
    lds.add_argument("--prewarm", type=int, default=None,
                     help="sandboxes to pre-start per task copy (default: 2 per attempt, as the difficulty check)")
    ldr = ldx.add_parser("report")
    ldr.add_argument("run_id")
    ld.set_defaults(fn=cmd_load)

    sm = sub.add_parser("smoke", help="PAID (cents): check both real model connections")
    sm.add_argument("--cap", type=float, default=1.0, help="stop if the smoke test would spend more than this")
    sm.set_defaults(fn=cmd_smoke)

    m = sub.add_parser("mcp", help="expose sandbox tools to MCP clients over stdio")
    m.add_argument("task", help="task directory whose image the sandbox is started from")
    m.add_argument("--sandbox", choices=("fake", "docker"), default="docker")
    m.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
