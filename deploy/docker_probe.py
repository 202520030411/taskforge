"""How fast is the Docker daemon itself, at different numbers of jobs at once? (P4, no model, $0)

Uses `DockerSandbox` directly (no sandboxd, no engine), with the same container flags as a real
sandbox, on the image of the handwritten task. Two measurements per level P:

  start  P threads, each in a loop for --seconds: start a sandbox (docker run + the setup command),
         then remove it. Reports the time of each docker call and how many sandboxes a second the
         machine started in total.
  exec   P sandboxes started first; P threads, each in a loop: run `true` in its sandbox. Reports
         the time per command and commands a second in total.

    .venv/bin/python deploy/docker_probe.py [--levels 1,2,4,8,16,32,64] [--seconds 15] [--label before]

Containers carry the owner label `probe`, so sandboxd never touches them; all are removed at the end.
Results: runs/loadtest/docker-probe-LABEL.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from taskforge.core.sandbox.docker import DockerSandbox  # noqa: E402
from taskforge.taskgen import package as pkg  # noqa: E402
from taskforge.taskgen.loadtest import TASK_DIR  # noqa: E402

OUT = ROOT / "runs" / "loadtest"


class TimedDocker(DockerSandbox):
    """Records how long every docker command took, by its first word (run, exec, rm)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lock = threading.Lock()
        self.times: dict[str, list[float]] = defaultdict(list)

    def _run(self, args, **kw):
        t = time.monotonic()
        try:
            return super()._run(args, **kw)
        finally:
            with self.lock:
                self.times[args[0]].append(time.monotonic() - t)

    def take(self) -> dict[str, list[float]]:
        with self.lock:
            out, self.times = self.times, defaultdict(list)
        return out


def q(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p * len(s)))], 3)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": q(values, 0.5), "p95": q(values, 0.95),
            "mean": round(statistics.fmean(values), 3) if values else None}


def loop(p: int, seconds: float, body) -> tuple[int, float]:
    """Run body() in p threads until `seconds` have passed; returns (completed calls, wall seconds)."""
    stop_at = time.monotonic() + seconds
    done = [0] * p

    def worker(i: int):
        while time.monotonic() < stop_at:
            body(i)
            done[i] += 1

    t = time.monotonic()
    with ThreadPoolExecutor(p) as ex:
        list(ex.map(worker, range(p)))
    return sum(done), time.monotonic() - t


def probe_start(d: TimedDocker, image: str, p: int, seconds: float) -> dict:
    d.take()

    def body(_):
        sid = d.create(image, purpose="rollout")
        d.destroy(sid)

    n, wall = loop(p, seconds, body)
    times = d.take()
    exec_times = times.get("exec", [])
    return {"sandboxes": n, "per_s": round(n / wall, 2), "docker_run": summary(times.get("run", [])),
            "setup_exec": summary(exec_times), "docker_rm": summary(times.get("rm", []))}


def probe_exec(d: TimedDocker, image: str, p: int, seconds: float) -> dict:
    with ThreadPoolExecutor(min(p, 8)) as ex:
        sids = list(ex.map(lambda _: d.create(image, purpose="rollout"), range(p)))
    lat: list[float] = []
    lock = threading.Lock()

    def body(i):
        t = time.monotonic()
        d.exec(sids[i], "true", timeout_s=60)
        with lock:
            lat.append(time.monotonic() - t)

    try:
        n, wall = loop(p, seconds, body)
    finally:
        d.destroy_many(sids)
    d.take()
    return {"commands": n, "per_s": round(n / wall, 1), "exec": summary(lat)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2,4,8,16,32,64")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--label", default="probe")
    ap.add_argument("--only", choices=("start", "exec"), default=None)
    args = ap.parse_args()

    d = TimedDocker(owner="probe")
    image = d.build(pkg.load_dir(TASK_DIR))
    d.destroy_many(d.list_owned())
    result = {"image": image, "seconds": args.seconds, "levels": {}}
    try:
        for p in (int(x) for x in args.levels.split(",")):
            row = {}
            if args.only in (None, "start"):
                row["start"] = probe_start(d, image, p, args.seconds)
            if args.only in (None, "exec"):
                row["exec"] = probe_exec(d, image, p, args.seconds)
            result["levels"][p] = row
            s, e = row.get("start"), row.get("exec")
            print(f"P={p:3d}"
                  + (f"  start {s['per_s']:6.2f}/s  run p50 {s['docker_run']['p50']}s p95 {s['docker_run']['p95']}s"
                     f"  setup p50 {s['setup_exec']['p50']}s  rm p50 {s['docker_rm']['p50']}s" if s else "")
                  + (f"  | exec {e['per_s']:7.1f}/s  p50 {e['exec']['p50']}s p95 {e['exec']['p95']}s" if e else ""),
                  flush=True)
    finally:
        d.destroy_many(d.list_owned())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"docker-probe-{args.label}.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
