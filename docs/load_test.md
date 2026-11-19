# P4 load test and failure drills (fake models, real platform)

Run on 2026-11-17 on the stack VM (16 cores, 30 GB memory); the bottleneck it found was fixed and measured
again on 2026-11-18 (last section before Cost). Code: `taskforge/taskgen/loadtest.py`, `deploy/loadtest.py`,
`deploy/loadtest.sh`, `deploy/docker_probe.py`. Raw numbers: `runs/loadtest/level-*.json`,
`runs/loadtest/drill-*.json` and `runs/loadtest/docker-probe-before.json` on the VM. No paid model was called: the load-test environment holds no
API key, and `taskforge load` refuses to run unless the models are fakes.

## What one attempt is

The production path of one weak-solver attempt, with only the model replaced:

```
engine claims a `rollout` step ──▶ sandboxd starts a container (pool or cold)
   ──▶ 10 model turns through the gateway (cache, ledger, budget reservation; the fake
       model waits 0.5-3 s per turn, mean 1.75 s)
   ──▶ each turn's tool call runs in the container (look around, write the program, run it)
   ──▶ copy of /app saved ──▶ graded in a fresh container ──▶ trajectory stored ──▶ commit
```

The task is the handwritten `order-totals`, in N copies that differ only in a Dockerfile label, so each
copy has its own image the way distinct tasks do. The scripted solver writes the reference program,
so every attempt should pass grading. An attempt that fails would mean something went wrong.

## Ramp: 16 to 128 attempts at a time

Ten attempts per slot at each level (level 16 = 160 attempts). One worker process per 16 attempt threads.

| At once | Attempts | Finished | Graded pass | Per minute | Attempt p50 / p95 | CPU busy mean / max | Free memory min | Load average max | Paid twice |
|---|---|---|---|---|---|---|---|---|---|
| 16 | 160 | 160 | 160 | 31.5 | 29.2 s / 34.1 s | 18% / 28% | 27.0 GB | 4.3 | 0 |
| 32 | 320 | 320 | 320 | 61.9 | 30.0 s / 35.2 s | 28% / 41% | 26.7 GB | 6.9 | 0 |
| 64 | 640 | 640 | 640 | 117.3 | 31.8 s / 36.7 s | 43% / 65% | 26.1 GB | 14.1 | 0 |
| 128 | 1,280 | 1,280 | 1,280 | 195.7 | 37.4 s / 43.4 s | 62% / 78% | 24.7 GB | 35.0 | 0 |

Every attempt at every level finished and passed. All 24,000 model calls (1,600 + 3,200 + 6,400 + 12,800)
were paid exactly once; no step failed or was retried.

**Throughput keeps rising but stops doubling at 128.** Per slot it is 1.97, 1.93, 1.83 and 1.53
attempts a minute. At 128 the VM gives 78% of what 8 times level 16 would be.

**What slows down is the part that is not the model.** The fake model accounts for about 17.5 s of
each attempt (10 turns x 1.75 s) at every level. The rest (containers, commands, grading, engine) is
11.7 s at 16 and 19.9 s at 128: it grows by 70%.

**First reading: container starts (wrong, corrected in "Removing the bottleneck" below).** sandboxd measured the time to
hand out a container that was not pre-started ("cold") as 2.5-5 s for half of them at 16-64 and
5-10 s at 128 (histogram buckets, so ranges). Docker alone, with no taskforge code, on the idle VM:

| Loops in parallel (each: start, one command, remove; only start + command were timed) | Median | Slowest 5% |
|---|---|---|
| 1 | 0.29 s | 0.31 s |
| 16 | 0.48 s | 2.4 s |
| 64 | 4.1 s | 5.7 s |

So the Docker daemon slows down sharply when many containers are created and removed at once. Every
attempt needs two (one to work in, one to grade in), and each of its ten commands is a `docker exec`
through the same daemon. What this probe missed: the `docker rm` in each loop ran after the timer
stopped, and removal turned out to be the slower half.

**The warm pool hardly helped in this test (8% of 4,832 containers).** The pool pre-starts containers
only when told what is coming; the load run gives that hint once per task copy (10 containers, as the
leak gate does before a real difficulty check), while each copy then got 50-100 attempts. In the P3
pilot, where each task gets about 10-20 attempts, the same hint covered 40%. The ramp therefore mostly
measures cold starts: close to a worst case for the pool. Since 2026-11-18 the load run gives the
difficulty check's own hint (2 per attempt) and the pool keeps topping itself up.

**What it means for a real run.** The pilot profile uses 16 attempt threads, and a real Qwen attempt lasts minutes
(about 30 model turns, slower answers), so container time is a small share of it and the VM is far
from these limits. There the limit is the request rate the model gateway allows per provider
(OpenRouter 3 requests a second, Anthropic 1 a second, set for the pilot), not the machine.

**What would raise the ceiling.** Proposed here: refill the pool while a task's attempts are running,
not only once before; cap how many containers sandboxd starts at the same moment; or use a lighter
container runtime. The first was done on 2026-11-18; the second was measured first and dropped, because
Docker gets more done, not less, with more starts at once (next section).

## Failure drills

Each drill is one load run of 160 attempts, 32 at a time (2 workers), with one fault injected 40 s in.
A drill passes when all 160 attempts finish and pass grading, no model call is paid twice, and no
finished attempt is missing its trajectory record. Round 1 ran the code as it was; each failure
there led to one fix (decisions D-035 to D-041); round 2 ran every drill again on the fixed code.

| Drill | Fault | Round 1: finished / time / steps out of tries / paid calls | Round 2: same | Fix |
|---|---|---|---|---|
| baseline | none | 160 / 156 s / 0 / 1,600 | 160 / 155 s / 0 / 1,600 | |
| kill_worker | one of 2 workers force-stopped (SIGKILL); a new one starts 5 s later | 160 / 172 s / 0 / 1,684 | 160 / 166 s / 0 / 1,600 | D-035, D-036, D-038 |
| kill_containers | 8 random sandbox containers deleted | 160 / 164 s / 0 / 1,637 | 160 / 165 s / 0 / 1,620 | |
| rate_limit | half of all model calls refused with 429 for 90 s | 160 / 251 s / 16 / 1,871 | 160 / 206 s / 0 / 1,600 | D-037 |
| restart_sandboxd | sandboxd stopped and started | **35** / 114 s / 375 / 594 (down 72 s) | 160 / 181 s / 0 / 1,600 (down 9 s) | D-040 |
| restart_postgres | `docker restart` of Postgres (down 1.6-1.8 s) | 160 / 186 s / 5 / 1,634 | 160 / 163 s / 0 / 1,600 | D-039 |
| disk_full | the disk holding sandboxd's /app copies full for 60 s | 160 / 214 s / 29 / 2,439 | 160 / 194 s / 0 / 1,600 | D-041 |

No drill in either round paid a call twice or lost a record. In round 1, "160 finished" often only held
because the load run re-runs failed attempts under new keys (as the difficulty check does since
D-034), and every such re-run paid its model calls again: up to 52% extra (disk_full). Most real steps
(Task Builder, judge, repair) have no such re-run, so in a real run those faults would have dropped
tasks. In round 2 no step ran out of attempts and the paid calls equal the no-fault run's 1,600 in
six drills; kill_containers paid 20 extra (1.25%) because a command cut off by its container's deletion
printed something different the second time.

**What each round-1 failure was.**

- **kill_worker, two hidden costs.** (1) The 16 re-run attempts got only their first reply from the
  cache: `ls -la /app` shows `..` with the container's creation time, so every later request differed
  and was paid again (D-035). (2) Once that was fixed, the re-runs reached the call the dead worker had in
  flight and waited for its claim to be 600 s old: the run took **659 s** (D-036; measured between the two
  rounds). (3) The dead worker's 16 sandboxes stayed "in use" for their one-hour expiry (D-038).
- **rate_limit.** After the gateway's three quick tries, a 429 used up one of the step's three attempts;
  the engine's waits of 5 s and 10 s spent all three in 15 s, far inside a 90 s storm (D-037).
- **restart_sandboxd.** sandboxd took 72 s to stop because it removed about 70 containers one at a time,
  and "connection refused" used up attempts exactly like a broken sandbox (D-040).
- **restart_postgres.** `psycopg.errors.AdminShutdown` inside a step was treated as a bug in the step (D-039).
- **disk_full.** `No space left on device` came back as a plain 500 and used up attempts (D-041).

**Common rule (D-037 to D-041).** A fault of the machinery (provider throttling, database, sandbox
service, disk) no longer uses up the attempts of whatever step was running; the step waits. If such a
fault never ends, the run stalls where the dashboard shows it, instead of silently rejecting tasks.

**Not covered.** Postgres's own disk filling up; a network split between the workers and Postgres
(the VM runs everything on one machine); losing the VM itself; a provider returning wrong answers
rather than errors.

## Removing the bottleneck (2026-11-18)

**Removal, not start, was the slow part.** `deploy/docker_probe.py` times every docker call on its own,
with the real sandbox flags, on the idle VM: P threads, each in a loop for 15 s.

| At once | Start + remove cycles a second | `docker run` p50 / p95 | `docker rm -f` p50 | `docker exec true` a second | its p50 |
|---|---|---|---|---|---|
| 1 | 0.68 | 0.21 / 0.25 s | 1.16 s | 13 | 0.07 s |
| 4 | 1.16 | 0.23 / 2.15 s | 2.26 s | 63 | 0.06 s |
| 16 | 3.79 | 0.40 / 2.17 s | 3.07 s | 163 | 0.10 s |
| 64 | 7.48 | 3.52 / 5.87 s | 3.37 s | 185 | 0.34 s |

Separately: `docker kill` takes 0.08 s, then `docker rm` of the stopped container about 1.05 s. One
`docker rm -f` naming 10 containers took 3.5 s, naming 30 took 3.3 s.

What the numbers say:
- **Deleting a container costs about 1 s on an idle machine and about 3 s under load,** and every attempt
  waited for two deletions: its own container before grading, and the grading container before the result
  came back.
- **Many deletions in one command cost about as much as one:** about 0.11 s each in a batch of 30.
- **Docker gets more done with more work at once** (0.68 to 7.48 cycles a second from 1 to 64). The cap on
  simultaneous starts proposed above would have lowered throughput, so it was not built.
- Commands inside containers are not a limit: 185 a second at 64 at once, against about 50 a second
  that 128 attempts need.

**Changes (decision D-043).**
1. `WarmPool.release()` returns at once. Four background threads delete returned containers in batches of
   up to 50 per `docker rm -f`. A container waiting to be deleted still counts against `max_containers`,
   so the machine never holds more than the cap.
2. The pool remembers the whole hint (up to 1,000 per image) and keeps up to 8 per image started,
   topping up as they are taken. Pre-started containers may not take the last eighth of the slots
   (`reserve`), so idle containers of one image never block a step of another.
3. The difficulty check also hints its second batch of 10 attempts and re-runs of failed attempts; both
   started cold before. The load run now gives the difficulty check's hint (2 per attempt) instead of 10
   per task copy.

**Result.** Same VM, same load, same 10 attempts per slot as the first ramp:

| At once | Per minute, before → after | Attempt p50, before → after | Outside the model, per attempt | CPU busy mean | From the pool |
|---|---|---|---|---|---|
| 16 | 31.5 → **47.1** (+50%) | 29.2 → 19.7 s | 11.7 → 2.2 s | 18% → 24% | 97% |
| 32 | 61.9 → **86.7** (+40%) | 30.0 → 21.0 s | 12.5 → 3.5 s | 28% → 36% | 55% |
| 64 | 117.3 → **160.2** (+37%) | 31.8 → 22.8 s | 14.3 → 5.3 s | 43% → 55% | 40% |
| 128 | 195.7 → **242.6** (+24%) | 37.4 → 30.1 s | 19.9 → 12.6 s | 62% → 75% | 18% |

"Outside the model" is the attempt time minus the fake model's 17.5 s. Every attempt at every level
finished and passed; no model call was paid twice. The 16-64 rows ran with two deletion threads, the
128 row with four (the default now).

**Runs at 128 separate the changes.** Background deletion alone, with the old hint: 238.5 a
minute. Plus the new hint, two deletion threads: 232.6. Plus four deletion threads: 242.6. So almost all
of the gain at 128 comes from not waiting for deletions. A fourth run with four threads gave 247.3; the
runs differ by about ±3%, which is noise.
With two threads, 1,324 requests were refused for a moment because containers waiting to be deleted
filled the 320 slots (each waits 2 s and retries without using an attempt; none failed); with four,
505.

**What limits it now.** At 16-64 attempts at once an attempt spends 2-5 s outside the model, against
12-14 s before, and the pool hands out most sandboxes. At 128 the CPU is nearly full (75% mean, 89% peak,
load average 87 on 16 cores), and a container then takes 5-10 s to start, so the pool cannot start them
ahead fast enough. Where the CPU goes at 128 (a fourth run, 247.3 a minute; `top` every 5 s for 2 min):

| What | Cores of 16 | How it was found |
|---|---|---|
| Idle | 3.4 | measured (21%) |
| Docker's own services: `dockerd` 1.06, `containerd` 1.27, its per-container helpers 0.32 | 2.7 | measured per process |
| Starting the `docker` program for every operation | about 3 | calculated: about 76 starts a second (18-19 per attempt, counted in the code) x 40 ms of CPU each (measured: `docker exec` 40 ms, `docker ps` 34 ms) |
| Our Python: 8 worker processes 0.28, sandboxd 0.19 | 0.5 | measured per process |
| Postgres | 0.2 | measured per process |
| The rest: programs too short-lived for `top` to catch (`runc`, which Docker runs to start each container and command; the commands inside the containers) and the kernel's share of starting containers and processes | about 6 | the remainder; the kernel's share of all CPU time was 25% |

So the platform's own code is about 3% of the machine; the rest is Docker's machinery. The next step
(not done) would be for sandboxd to talk to Docker's HTTP interface on its local socket instead of
starting the `docker` program for every call, which would free about 3 cores; past that, a lighter
container runtime.

**For a real run** an attempt lasts minutes, so 12 s of overhead was already small; now it is 2 s at the
pilot's 16 threads.

**The failure drills still pass on the new code** (run again after the change; 160 attempts, 32 at a
time, one fault 40 s in):

| Drill | Round 2: time / paid calls | After D-043: time / paid calls |
|---|---|---|
| baseline | 155 s / 1,600 | 110 s / 1,600 |
| kill_worker | 166 s / 1,600 | 132 s / 1,600 |
| kill_containers | 165 s / 1,620 | 120 s / 1,622 |
| rate_limit | 206 s / 1,600 | 177 s / 1,600 |
| restart_sandboxd | 181 s / 1,600 (down 9 s) | 151 s / 1,600 (down 12.6 s) |
| restart_postgres | 163 s / 1,600 | 124 s / 1,600 |
| disk_full | 194 s / 1,600 | 143 s / 1,600 |

All 160 attempts finished and passed in every drill, with no step out of attempts, no call paid twice
and no record lost. sandboxd now takes a few seconds longer to restart because on the way out it waits
for deletions already running; kill_containers still pays about 20 extra calls for the reason given above.

## Cost

$0 of model API: no API key was present in any load-test process (`deploy/loadtest.sh` removes them,
`taskforge load` refuses to run on real providers). The VM is a fixed monthly cost.
The same holds for the 2026-11-18 probe, fix and re-runs.
