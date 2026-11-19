"""P4 load test and failure drills, on fake models only.

Run it through deploy/loadtest.sh (which sets up the stack's environment without any API key):
    deploy/loadtest.sh ramp [--levels 16,32,64,128] [--rounds 6]
    deploy/loadtest.sh drill [NAME ...]       default: every drill in DRILLS, in order

Ramp: the same load at 16, 32, 64 and 128 attempts at a time.

At each level C:
  - C // 16 worker processes start, each with 16 threads for agent steps (one attempt per thread)
  - one load run starts: C * rounds attempts over C // 5 task copies (about as many tasks as are in their
    difficulty check at once when C attempts run in a real run), 10 pre-started sandboxes per copy
  - every 5 s: CPU busy share, free memory, containers running, sandboxd's counters
  - at the end: the load report (attempts per minute, attempt time, queue wait, model calls, double
    charges) and the sandbox start-time distribution for that level, saved to runs/loadtest/level-C.json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs" / "loadtest"
PY = str(ROOT / ".venv" / "bin" / "python")
THREADS_PER_WORKER = 16


def now_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S")


def cpu_times() -> tuple[int, int]:
    parts = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = parts[3] + parts[4]
    return sum(parts) - idle, sum(parts)


def mem_available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1024 / 1024, 1)
    return -1.0


def containers() -> int:
    out = subprocess.run(["docker", "ps", "-q", "--filter", "label=taskforge.owner"], capture_output=True, text=True)
    return len(out.stdout.split())


def start_histogram(sandboxd: str) -> dict[tuple[str, float], float]:
    """Cumulative bucket counts of taskforge_sandbox_start_seconds, by (source, upper bound)."""
    out = {}
    for line in httpx.get(f"{sandboxd}/metrics", timeout=10).text.splitlines():
        if line.startswith("taskforge_sandbox_start_seconds_bucket{"):
            labels, value = line[line.index("{") + 1:line.index("}")], float(line.rsplit(" ", 1)[1])
            d = dict(p.split("=", 1) for p in labels.split(","))
            le = d["le"].strip('"')
            out[(d["source"].strip('"'), float("inf") if le == "+Inf" else float(le))] = value
    return out


def quantiles(before: dict, after: dict) -> dict:
    """For each source (pool / cold): count and the bucket bound under which 50% / 95% of starts fell."""
    res = {}
    for source in sorted({s for s, _ in after}):
        bounds = sorted(le for s, le in after if s == source)
        cum = [after[(source, le)] - before.get((source, le), 0.0) for le in bounds]
        total = cum[-1] if cum else 0
        if not total:
            continue
        pick = lambda q: next(le for le, c in zip(bounds, cum) if c >= q * total)   # noqa: E731
        res[source] = {"count": int(total), "p50_under_s": pick(0.5), "p95_under_s": pick(0.95)}
    return res


class Sampler(threading.Thread):
    def __init__(self, sandboxd: str, every_s: float = 5.0):
        super().__init__(daemon=True)
        self.sandboxd, self.every_s = sandboxd, every_s
        self.samples: list[dict] = []
        self.stop = threading.Event()

    def run(self):
        busy0, total0 = cpu_times()
        while not self.stop.wait(self.every_s):
            busy1, total1 = cpu_times()
            cpu = (busy1 - busy0) / max(1, total1 - total0)
            busy0, total0 = busy1, total1
            try:
                cap = httpx.get(f"{self.sandboxd}/v1/capacity", timeout=10).json()
            except httpx.HTTPError:
                cap = {}
            self.samples.append({"t": time.time(), "cpu": round(cpu, 3), "mem_free_gb": mem_available_gb(),
                                 "containers": containers(), "in_use": cap.get("in_use"), "idle": cap.get("idle"),
                                 "load1": float(Path("/proc/loadavg").read_text().split()[0])})

    def summary(self) -> dict:
        s = self.samples
        if not s:
            return {}
        return {"cpu_mean": round(sum(x["cpu"] for x in s) / len(s), 3), "cpu_max": max(x["cpu"] for x in s),
                "mem_free_min_gb": min(x["mem_free_gb"] for x in s), "containers_max": max(x["containers"] for x in s),
                "in_use_max": max((x["in_use"] or 0) for x in s), "load1_max": max(x["load1"] for x in s)}


def cli(*args: str, env) -> str:
    r = subprocess.run([PY, "-m", "taskforge.cli", *args], capture_output=True, text=True, env=env, cwd=ROOT)
    if r.returncode:
        raise SystemExit(f"taskforge {' '.join(args)} failed:\n{r.stderr[-3000:]}")
    return r.stdout


def start_workers(n: int, env) -> list[subprocess.Popen]:
    OUT.mkdir(parents=True, exist_ok=True)
    conc = f"agent={THREADS_PER_WORKER},cpu=2,llm=2,sandbox=2,human=1"
    procs = []
    for i in range(n):
        log = open(OUT / f"worker{i + 1}.log", "a")
        procs.append(subprocess.Popen([PY, "-m", "taskforge.cli", "worker", "--concurrency", conc], cwd=ROOT,
                                      env=env, stdout=log, stderr=subprocess.STDOUT))
    return procs


def stop_workers(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()


def wait_finished(run_id: str, env, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = json.loads(cli("run", "status", run_id, env=env))
        except SystemExit as e:             # e.g. the database is restarting (a drill): ask again
            print(f"{now_et()} status check failed, retrying: {str(e)[-200:]}", flush=True)
            time.sleep(5)
            continue
        if state.get("finished"):
            return state
        time.sleep(5)
    raise SystemExit(f"{run_id} did not finish in {timeout_s:.0f} s")


def wait_sandboxes_free(sandboxd: str, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if httpx.get(f"{sandboxd}/v1/capacity", timeout=10).json().get("in_use") == 0:
            return
        time.sleep(2)


def run_level(c: int, rounds: int, env, sandboxd: str, *, prewarm: int | None = None, label: str = "") -> dict:
    workers = max(1, c // THREADS_PER_WORKER)
    tasks = max(2, c // 5)
    print(f"{now_et()} level {c}: {workers} workers, {c * rounds} attempts over {tasks} task copies", flush=True)
    wait_sandboxes_free(sandboxd)
    procs = start_workers(workers, env)
    sampler = Sampler(sandboxd)
    before = start_histogram(sandboxd)
    counts_before = httpx.get(f"{sandboxd}/v1/capacity", timeout=10).json()
    sampler.start()
    try:
        extra = ["--prewarm", str(prewarm)] if prewarm is not None else []
        run_id = cli("load", "start", "--attempts", str(c * rounds), "--tasks", str(tasks), *extra, env=env).strip()
        state = wait_finished(run_id, env, timeout_s=3600)
    finally:
        sampler.stop.set()
        stop_workers(procs)
    counts_after = httpx.get(f"{sandboxd}/v1/capacity", timeout=10).json()
    report = json.loads(cli("load", "report", run_id, env=env))
    out = {"level": c, "label": label, "prewarm_per_task": prewarm, "workers": workers, "task_copies": tasks,
           "run_status": state["counts"], **report,
           "sandbox_start": quantiles(before, start_histogram(sandboxd)),
           "sandbox_counts": {k: counts_after.get(k, 0) - counts_before.get(k, 0)
                              for k in ("pool_hits", "cold_starts", "busy", "prewarmed", "idle_expired",
                                        "removed", "remove_batches")},
           "host": sampler.summary(), "samples": sampler.samples}
    (OUT / f"level-{c}{'-' + label if label else ''}.json").write_text(json.dumps(out, indent=1, default=str))
    print(f"{now_et()} level {c}: {report['succeeded']}/{report['attempts']} done, {report['attempts_per_min']}/min, "
          f"attempt p50 {report['attempt_s']['p50']} s p95 {report['attempt_s']['p95']} s, "
          f"doubles {report['double_charges']}, host {out['host']}", flush=True)
    return out


# ------------------------------------------------------------------ drills
SANDBOXD_PID = ROOT / "runs" / "stack" / "sandboxd.pid"
SMALL_DISK = Path("/mnt/tf-small")
SMALL_IMG = ROOT / "runs" / "loadtest" / "small-disk.img"


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"{cmd!r} failed: {r.stderr[-2000:]}")
    return r.stdout.strip()


def start_sandboxd(env, snapshot_dir: str | None = None) -> None:
    e = dict(env)
    if snapshot_dir:
        e["TASKFORGE_SNAPSHOT_DIR"] = snapshot_dir
    log = open(ROOT / "runs" / "stack" / "sandboxd.log", "a")
    p = subprocess.Popen([PY, "-m", "taskforge.cli", "sandboxd", "--max", "320"], cwd=ROOT, env=e, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    SANDBOXD_PID.write_text(str(p.pid))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{env['TASKFORGE_SANDBOX']}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("sandboxd did not come back")


def stop_sandboxd() -> None:
    pid = int(SANDBOXD_PID.read_text())
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)


def inject_none(ctx) -> dict:
    return {}


def inject_kill_worker(ctx) -> dict:
    """Force-stop one worker with no chance to clean up (SIGKILL); a replacement starts 5 s later,
    as a process supervisor would do."""
    victim = ctx["procs"][0]
    with ctx["db"].tx() as c:
        held = c.execute("SELECT count(*) AS n FROM step_runs WHERE run_id = %s AND status = 'RUNNING' "
                         "AND lease_owner LIKE %s", (ctx["run_id"], f"%:{victim.pid}:%")).fetchone()["n"]
    victim.kill()
    time.sleep(5)
    ctx["procs"].extend(start_workers(1, ctx["env"]))
    return {"killed_pid": victim.pid, "steps_it_was_running": held}


def inject_kill_containers(ctx) -> dict:
    """Delete 8 random sandbox containers: ones attempts are working in, and pre-started ones waiting in
    the pool (those carry the label purpose=pool, because the pool starts them before anyone asks)."""
    import random
    ids = sh("docker ps -q --filter label=taskforge.owner=sandboxd").split()
    picked = random.sample(ids, min(8, len(ids)))
    if picked:
        sh("docker rm -f " + " ".join(picked))
    return {"containers_deleted": len(picked), "containers_running": len(ids)}


def inject_rate_limit(ctx) -> dict:
    """For 90 s the fake API refuses half of all calls with '429 too many requests'."""
    faults = Path(ctx["env"]["TASKFORGE_FAULTS"])
    faults.write_text(json.dumps({"rate_limit_share": 0.5}))
    time.sleep(90)
    faults.write_text("{}")
    return {"storm_s": 90, "share_refused": 0.5}


def inject_restart_sandboxd(ctx) -> dict:
    t = time.monotonic()
    stop_sandboxd()
    start_sandboxd(ctx["env"], ctx.get("snapshot_dir"))
    return {"sandboxd_down_s": round(time.monotonic() - t, 1)}


def inject_restart_postgres(ctx) -> dict:
    t = time.monotonic()
    sh("docker restart taskforge-postgres-1")
    sh("until docker exec taskforge-postgres-1 pg_isready -U taskforge -q; do sleep 0.5; done")
    return {"postgres_down_s": round(time.monotonic() - t, 1)}


def inject_disk_full(ctx) -> dict:
    """Fill the small disk that holds sandboxd's /app copies for 60 s, then free it."""
    free = int(sh(f"df --output=avail -B1 {SMALL_DISK} | tail -1"))
    sh(f"dd if=/dev/zero of={SMALL_DISK}/filler bs=64K 2>/dev/null; sync", check=False)   # runs until the disk is full
    left = int(sh(f"df --output=avail -B1 {SMALL_DISK} | tail -1"))
    time.sleep(60)
    sh(f"rm -f {SMALL_DISK}/filler")
    return {"full_s": 60, "free_before_bytes": free, "free_while_full_bytes": left}


def setup_small_disk(env) -> str:
    """A 512 MB file formatted as its own disk, mounted at /mnt/tf-small; sandboxd restarted to keep its
    /app copies there. The VM's real disk is never filled."""
    if not sh(f"mountpoint -q {SMALL_DISK} && echo yes", check=False):
        sh(f"fallocate -l 512M {SMALL_IMG} && mkfs.ext4 -q -F -m 0 {SMALL_IMG} && mkdir -p {SMALL_DISK} "
           f"&& mount -o loop {SMALL_IMG} {SMALL_DISK}")
    sh(f"mkdir -p {SMALL_DISK}/snapshots")
    stop_sandboxd()
    start_sandboxd(env, f"{SMALL_DISK}/snapshots")
    return f"{SMALL_DISK}/snapshots"


def teardown_small_disk(env) -> None:
    try:
        stop_sandboxd()
        start_sandboxd(env)
    finally:
        sh(f"rm -f {SMALL_DISK}/filler; umount {SMALL_DISK} && rm -f {SMALL_IMG}", check=False)


DRILLS = {
    "baseline": inject_none,
    "kill_worker": inject_kill_worker,
    "kill_containers": inject_kill_containers,
    "rate_limit": inject_rate_limit,
    "restart_sandboxd": inject_restart_sandboxd,
    "restart_postgres": inject_restart_postgres,
    "disk_full": inject_disk_full,
}


def run_drill(name: str, env, sandboxd: str, *, attempts: int = 160, workers: int = 2, at_s: float = 40) -> dict:
    from taskforge.core.pg import Database
    tasks = 12
    snapshot_dir = setup_small_disk(env) if name == "disk_full" else None
    print(f"{now_et()} drill {name}: {attempts} attempts, {workers * THREADS_PER_WORKER} at a time", flush=True)
    wait_sandboxes_free(sandboxd)
    procs = start_workers(workers, env)
    db = Database(env["TASKFORGE_PG_DSN"], schema=env["TASKFORGE_PG_SCHEMA"], max_size=2)
    t0 = time.monotonic()
    try:
        run_id = cli("load", "start", "--attempts", str(attempts), "--tasks", str(tasks), env=env).strip()
        time.sleep(at_s)
        injected_at = round(time.monotonic() - t0, 1)
        info = DRILLS[name]({"procs": procs, "env": env, "run_id": run_id, "db": db, "snapshot_dir": snapshot_dir})
        print(f"{now_et()} drill {name}: injected at {injected_at} s: {info}", flush=True)
        state = wait_finished(run_id, env, timeout_s=2400)
        finished_s = round(time.monotonic() - t0, 1)
    finally:
        stop_workers(procs)
        db.close()
    try:
        report = json.loads(cli("load", "report", run_id, env=env))
    finally:
        if snapshot_dir:
            teardown_small_disk(env)
    cap = httpx.get(f"{sandboxd}/v1/capacity", timeout=10).json()
    ok = (report["succeeded"] == attempts and report["passed"] == attempts and report["double_charges"] == 0
          and report["missing_trajectories"] == 0)
    out = {"drill": name, "ok": ok, "injected_at_s": injected_at, "injected": info, "finished_s": finished_s,
           "run_status": state["counts"], **report, "sandboxd_after": cap}
    (OUT / f"drill-{name}.json").write_text(json.dumps(out, indent=1, default=str))
    print(f"{now_et()} drill {name}: {'PASS' if ok else 'FAIL'} - {report['succeeded']}/{attempts} done, "
          f"{report['passed']} passed, finished at {finished_s} s, steps failed {report['steps_failed']}, "
          f"re-run {report['rerun_attempts']}, engine retries {report['engine_retries']}, "
          f"doubles {report['double_charges']}, lost records {report['missing_trajectories']}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("ramp")
    r.add_argument("--levels", default="16,32,64,128")
    r.add_argument("--rounds", type=int, default=6, help="attempts per concurrent slot")
    r.add_argument("--prewarm", type=int, default=None,
                   help="sandboxes to pre-start per task copy (default: 2 per attempt; the first ramp used 10)")
    r.add_argument("--label", default="", help="suffix for the result files, e.g. after")
    d = sub.add_parser("drill")
    d.add_argument("names", nargs="*", default=list(DRILLS))
    args = ap.parse_args()
    env = dict(os.environ)
    if env.get("TASKFORGE_PROFILE") != "loadtest" or any(k.endswith("_API_KEY") for k in env):
        print("run this through deploy/loadtest.sh: it needs TASKFORGE_PROFILE=loadtest and no API keys", file=sys.stderr)
        return 2
    sandboxd = env["TASKFORGE_SANDBOX"]
    cli("db", "init", env=env)
    if args.mode == "ramp":
        results = [run_level(int(c), args.rounds, env, sandboxd, prewarm=args.prewarm, label=args.label)
                   for c in args.levels.split(",")]
        print(json.dumps([{k: r[k] for k in ("level", "succeeded", "attempts", "attempts_per_min", "attempt_s",
                                             "double_charges", "sandbox_start", "host")} for r in results],
                         indent=1, default=str))
        return 0
    unknown = set(args.names) - set(DRILLS)
    if unknown:
        print(f"unknown drills {sorted(unknown)}; known: {list(DRILLS)}", file=sys.stderr)
        return 2
    results = [run_drill(n, env, sandboxd) for n in args.names]
    print(json.dumps([{k: r[k] for k in ("drill", "ok", "injected", "finished_s", "steps_failed", "rerun_attempts",
                                         "engine_retries", "double_charges", "missing_trajectories")}
                      for r in results], indent=1, default=str))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
