# TaskForge — Design

*Status: 2026-11-17. P0 (design), P1 (pipeline logic on scripted models), P2 (the platform), P3 (paid pilot, `docs/pilot_report.md`) and the free part of P4 (load test and failure drills, `docs/load_test.md`) are done. Changes forced by building, the pilot and the drills are marked **[P1]** / **[P2]** / **[P3]** / **[P4]**. Sections 4 (engine) and 12 (data model) are the parts everything else is built on.*

TaskForge is a small data-production platform. Its first workflow rebuilds a failure-driven task-synthesis pipeline: compare a strong and a weak coding agent on the same tasks, turn the weak model's mistakes into named bad patterns, generate new terminal tasks that target each pattern, and accept a task only after it passes a chain of executed checks and human review points.

The business logic follows a written method spec for this pipeline (called **the spec** below; it is not part of this repository). Everything here is newly written code on public libraries and synthetic or self-generated data.

How to read this file: each section starts with what the component does in plain words, then gives the rules. Section 16 is a glossary of every systems term used.

---

## 1. Goals and non-goals

**Goals**

1. Reproduce the spec faithfully: every role, the five executed gates, the difficulty-repair loop, the seven human review points (H1–H7) and the eight invariants, each invariant backed by a test.
2. Run that pipeline on a platform that survives failures: a crashed process loses no finished work and pays for no model call twice.
3. Make every result traceable: any accepted task can be followed back to the proposal, the bad pattern, the rubric items and the trajectory pairs it came from.
4. Produce measured numbers (section 13), not claims.

**Non-goals (v1)**

- Model training and the per-pattern training ablation. The pipeline ends at an accepted, exported task set plus a per-pattern funnel report.
- More than one machine. The design says how it would extend (section 4.11); only single-machine numbers will be claimed.
- User accounts and permissions. One operator, one reviewer.
- Tasks that need a long-running service to be graded (for example "start an HTTP server"). v1 tasks are graded from files left in `/app`.

---

## 2. Constraints

Every decision below was checked against this list.

| Constraint | Rule |
|---|---|
| Budget | No step that calls a paid model can run unless its run has `paid_approved = true` and a dollar budget. The gateway refuses calls beyond the budget (section 6). Caps for the pilot are set by the owner, not here. |
| Data | Clean-room: all code, prompts and data are written or generated for this project. The owner's own earlier prototypes (`taskengine-mvp`, `rollout-mvp`) may be reused. |
| Same setup for compared runs | All weak-model rollouts in one run use one model version and one pinned provider. Both are recorded on every trajectory. |
| Contamination | Terminal-Bench 2.1 is a held-out comparison set. Any proposal or built task that overlaps it is dropped and never sent to a human (invariant 3). Only abstract pattern descriptions reach the Proposal Writer, never benchmark task text. |
| Statistics | Difficulty is "passes out of 5 independent runs", accepted when 1 ≤ k ≤ 4; k = 0 or 5 triggers 5 more runs. Reported rates carry their sample sizes. |
| Independence | Separate project. No imports from the `agent_harness` repo. Stored trajectories from earlier runs are used as input data only. |
| Languages | Python only. All files in English. |

---

## 3. Architecture

Two layers. The business layer says *what* happens to a task; the platform layer makes it happen reliably.

```
┌─ Business layer: taskforge/taskgen ───────────────────────────────────────────┐
│ stored trajectories ─▶ pair ─▶ Rubric Writer ─▶ partial-order check ─▶ [H1]    │
│ ─▶ Proposal Writer ─▶ overlap check ─▶ [H2] ─▶ Task Builder                    │
│ ─▶ oracle / initial-state / Hacker / leak gates ─▶ weak model × 5              │
│ ─▶ repair ≤ 2 rounds ─▶ [H3] ─▶ Blind Judge ─▶ [H4] ─▶ [H5] ─▶ [H6] ─▶ export  │
└────────────────────────────────────────────────────────────────────────────────┘
                 │ every box is one "step" run by the engine
┌─ Platform layer: taskforge/core ──────────────────────────────────────────────┐
│ engine    runs steps, saves each result, resumes after crashes, waits for people│
│ sandbox   one isolated Linux environment per run; grading in a second fresh one │
│ agent     the loop that lets a model work in a sandbox through tools            │
│ gateway   the only door to paid models: limits, retries, cache, cost ledger     │
│ store     records in Postgres, large files in a blob store, lineage links       │
│ tools     tools defined once, usable by agents and exposed over MCP             │
│ kb        searchable memory: patterns, accepted tasks, repair examples          │
│ review    web console and CLI for the human review points                       │
│ obs       traces, metrics, logs                                                 │
└────────────────────────────────────────────────────────────────────────────────┘
```

**What runs where (all on one Linux VM, started with Docker Compose):**

| Program | Job | Talks to |
|---|---|---|
| `worker` (several copies) | Picks up steps and runs them | Postgres, gateway code, sandboxd, blob store |
| `sandboxd` | The only program allowed to control Docker. Creates, runs and deletes sandboxes | Docker |
| `api` | Review console and control API | Postgres, blob store |
| Postgres | All state: steps, records, model-call ledger, reviews | — |
| SeaweedFS | Blob store with the S3 API, for task packages (MinIO's images are no longer published) **[P2]** | — |
| Jaeger, Prometheus, Grafana | Trace viewer, metric storage, dashboards | — |

Workers never touch Docker directly. If a worker is compromised by something a model wrote, it still cannot start containers or reach the host.

---

## 4. The orchestration engine

**In plain words.** A pipeline is a set of steps. The engine keeps one database row per step execution. A worker picks a row that is ready, runs the step's function, and writes the result plus the rows for the following steps in a single database write. If the worker dies halfway, nothing was written, and another worker runs the step again. Steps are built so that running one twice has the same effect as running it once.

### 4.1 Vocabulary

| Term | Meaning |
|---|---|
| **run** | One execution of a workflow over one batch of input. Has a budget and an approval flag. |
| **step** | A named function, for example `gate_oracle`. Declared once in code. |
| **step run** | One row in the `step_runs` table: "step S for key K in run R". |
| **key** | A string naming what the step works on, derived from its input: `task:<id>`, `pattern:<id>`, `rollout:<task id>:3`, `batch`. |
| **resource class** | What a step needs: `cpu`, `llm`, `sandbox`, `agent` (model plus sandbox), `human`. |

A step run is unique on `(run_id, step, key)`. Records are immutable and content-addressed (section 7), so anything that changes a task produces a new task id and therefore a new key. Loops, such as "repair, then go through the gates again", need no round counter.

### 4.2 What a step function returns

```python
def gate_oracle(ctx, inp) -> Outcome:
    result = ctx.sandbox.verify(inp.task_id, solution=True)   # 1. run the reference solution, grade in a fresh container
    if not result.passed:
        return Done(reject("ORACLE_FAILS"))                    # 2. this branch ends here
    return Next(spawn("gate_initial", key=inp.key, input=inp)) # 3. finished; enqueue the following step
```

| Outcome | Meaning |
|---|---|
| `Done(output)` | Step finished. This branch ends. |
| `Next(spawn, ...)` | Step finished. Enqueue one following step (a chain) or several (a fan-out). |
| `Fork(spawns, then_wait=True)` | Enqueue children and pause this step until **all** of their branches end. Then the function is called again with the children's results. |
| `Wait(signal)` | Pause until a named signal arrives: a human decision, or an external job such as a model batch finishing. The function is called again with the signal's payload. |
| raise `Retryable` | Infrastructure fault (network error, sandbox fault, provider overloaded). Try again later. A fault of the machinery rather than of the step (provider says "too many requests", sandboxd unreachable or out of disk, all sandbox slots taken) waits without using up an attempt **[P4]**. |
| raise anything else | Bug or bad input. The step run is marked `FAILED` and kept for inspection. Exception: a lost database connection (`psycopg.OperationalError`) is retried like a machinery fault **[P4]**. |

A failing test is a *result*, not a fault: `gate_oracle` returning "oracle fails" is `Done`, never `Retryable`. Only faults are retried.

### 4.3 States

```
                 claim                       commit Done / Next
   PENDING ───────────────────▶ RUNNING ─────────────────────────▶ SUCCEEDED
      ▲  ▲                        │ │ │
      │  │  Retryable error, or   │ │ └── commit Wait / Fork ──▶ WAITING ── signal, or last
      │  └── claim expired ───────┘ │                               │        child branch ends
      │      (attempts left)        │                               │
      └─────────────────────────────┼───────────────────────────────┘
                                    └── other error, or no attempts left ──▶ FAILED
   CANCELLED: set by the operator, or for every open step when a run is cancelled
```

Every transition is one database transaction and also appends a row to the `events` table, so the full history of a run can be read back in order.

### 4.4 Claiming a step, and what happens when a worker dies

A worker claims a step run with one statement: pick a `PENDING` row of a class it serves whose `available_at` has passed, mark it `RUNNING`, write the worker's name, set `lease_expires_at = now() + 60 s`, and add 1 to `lease_token` and to `attempt`. The row is selected with Postgres's `FOR UPDATE SKIP LOCKED`, which lets one worker lock a row while other workers skip it instead of queueing behind it, so two workers never claim the same row.

This time-limited claim is called a **lease**. While the step runs, a background thread in the worker extends the lease every 20 s (a **heartbeat**). If the worker process dies, the heartbeat stops, the lease runs out, and a periodic sweep puts the row back to `PENDING`.

Three details that prevent subtle bugs:

- **A slow worker must not overwrite a newer one.** Suppose worker A stalls for two minutes, its lease expires, worker B claims the step and finishes it, and then A wakes up and tries to write its own result. A's write is `UPDATE … WHERE id = ? AND lease_token = ?` with the token A was given. B's claim raised the token, so A's write matches zero rows and A's whole transaction is rolled back. This guard number is known as a **fencing token**.
- **All times come from the database clock.** Lease expiry is computed with Postgres `now()`, never a worker's own clock, so workers with slightly different clocks cannot disagree about whether a lease has expired.
- **A stuck step function is also caught.** The heartbeat stops extending the lease once the step's own `timeout_s` has passed, so a function that hangs forever loses its lease even though the process is alive. A step's timeout must therefore be longer than anything it legitimately waits for: steps that drive an agent loop use 3600 s, the agent's 1800 s budget plus room for one last turn and grading, and `TaskgenConfig` refuses budgets that would not fit (D-044).

### 4.5 The commit rule

When a step function returns, the worker performs one transaction that:

1. marks the step run `SUCCEEDED` (or `WAITING`), guarded by the lease token;
2. stores its output;
3. inserts the following step runs with `INSERT … ON CONFLICT DO NOTHING` on `(run_id, step, key)`;
4. updates the branch counter of whoever is waiting on this branch (section 4.7);
5. appends the event row.

Either all five happen or none do. There is no moment where a step is finished but its successor was never enqueued (a lost step), and no way to enqueue the same successor twice (a duplicated step).

### 4.6 Making repeated execution harmless

The engine guarantees each step runs **at least once**. Three rules turn that into "each effect happens once":

| Effect | Rule |
|---|---|
| Paid model call | Every call carries an idempotency key = hash of (provider, model, parameters, messages, the step run's `(run_id, step, key)`, call number within the step). The gateway stores the response under that key. A repeated step gets the stored response back and pays nothing. |
| Records and blobs | Ids are the hash of the content. Writing the same record twice is a no-op. |
| Sandbox work | Gate steps are deterministic and cheap; they simply run again in a fresh container. |

One honest limit in v1: an agent rollout that dies at turn 30 of 40 restarts from turn 1 in a fresh sandbox. Its earlier model calls are replayed from the gateway cache for as long as the sandbox produces the same outputs; after the first difference, new calls are paid. No single request is ever billed twice, but some spend can be repeated work. This is measured as **replay waste** (section 13). Removing it needs per-turn checkpoints and re-attaching to a live sandbox, listed in section 17.

### 4.7 Waiting: people, external jobs, and "all of these"

- **Human decision.** A human-point step creates a review record and returns `Wait("review:<id>")`. Submitting the decision in the console delivers the signal in the same transaction that stores the decision, and the step becomes `PENDING` again.
- **External job.** Non-urgent model calls can go through a provider's batch endpoint at half price: the step submits the batch and returns `Wait("llm_batch:<id>")`; a poller delivers the signal when results are ready. The engine side (wait + signal) exists; the provider side arrives with the real model adapters in P3.
- **A signal that races with the wait [P2].** Two transactions can miss each other: the step that starts waiting checks the mailbox before the signal is stored, and the signal looks for a waiter before the wait is committed. Both sides first take the same lock, keyed on run and signal name (a Postgres advisory lock), so one always sees the other. Tested with 150 waits and 6 threads firing signals in random order.
- **All branches under me.** `Fork` is how the difficulty gate runs 5 rollouts and then counts passes, and how a batch-level stop (H1, H6) waits for every item. Each step run stores `join_parent_id` (who is waiting on my branch) and `open_branches` (how many branches I am waiting on). When a step commits:
  - `Done` → parent's `open_branches -= 1`;
  - `Next` with *n* spawns → the spawns inherit the same `join_parent_id`, parent's `open_branches += n − 1`;
  - `FAILED` → counts as an ended branch, and the parent sees the failure in the results.
  - A step can fork more than once (the difficulty gate forks 5 rollouts, then 5 more). Each fork has a generation number; a branch only touches the parent's counter if it belongs to the parent's current fork **[P2]**. So a branch that ends late, or a failed step an operator runs again after the parent has moved on, cannot corrupt a later join.

  The counter is adjusted by the number of rows the insert in section 4.5 actually created, so a spawn that already existed is not counted twice.

  The decrement is a single `UPDATE … SET open_branches = open_branches − 1 … RETURNING open_branches` on the parent's row. Postgres locks that row for the duration, so when the last two children finish at the same instant, exactly one of them sees 0 and wakes the parent. (The naive alternative, "count my unfinished siblings and wake the parent if there are none", lets both children see each other as unfinished and wake nobody.)

### 4.8 Concurrency limits and back-pressure

Each worker process is configured with a number of threads per resource class, by default `cpu: 4, human: 2, llm: 8, sandbox: 8, agent: 8` (`human` steps only open a review and wait, so they are cheap). The sum across workers is the cap for that class. `sandboxd` also refuses to hand out a sandbox when all its slots are taken; the step then raises `SandboxBusy`, a `Retryable` with a short delay that does not use up an attempt. Work piles up as `PENDING` rows, which is cheap, instead of as half-started containers. Measured **[P2]**: with empty steps one worker process moves about 300 to 360 step runs per second through claim, commit and event rows; more threads in the same process do not raise that (one Python process runs one thread at a time), more processes do.

### 4.9 Budget and approval guard

- Steps of class `llm` or `agent` are only claimable when their run has `paid_approved = true`. A run started without approval executes its free steps and then stops in front of the first paid one.
- Before each paid call the gateway reserves an estimated cost against `runs.budget_usd` in one guarded `UPDATE`. If the reservation would exceed the budget, the run is set to `PAUSED_BUDGET`; nothing more is claimed for it until the operator raises the budget.
- Each reservation is a row tied to the step run that made it **[P2]**. If the worker dies between reserving and settling, the sweep that takes the step back also releases its reservations; otherwise dead workers would slowly eat the budget.

### 4.10 Failure model

| Failure | What happens | Guarantee |
|---|---|---|
| Worker killed mid-step | Lease expires, step is re-run; a model call it had in flight is taken over at once (D-036), and sandboxd frees its sandboxes after 2 minutes without a keep-alive (D-038) **[P4]** | No lost step; model calls replayed from cache, which works because every container of an image looks the same to the agent (D-035) **[P4]** |
| Worker stalls, then resumes | Its commit is rejected by the fencing token | No stale overwrite, no duplicate successors |
| `sandboxd` restarts | On start it removes every container an earlier sandboxd left (their bookkeeping died with it), in batches **[P4]**; steps that were using one fail with `Retryable` and run again **[P2]**; steps that cannot reach it wait without using up attempts **[P4]** | No leaked containers; each also has its own time limit. Drill: 160/160 attempts, sandboxd down 9 s |
| Model provider returns "too many requests" or errors | Gateway backs off and retries; repeated failures open a circuit breaker for that provider. "Too many requests" then makes the step wait 10-30 s without using up an attempt; other errors use attempts **[P4]** | Workers are not blocked on a dead provider; throttling never drops a step |
| Postgres down | Workers pause and retry connecting; nothing can be claimed or committed. A step that loses its connection mid-way is retried, not failed **[P4]** | No progress, no corruption. Drill: 160/160 attempts |
| Disk full | On the sandbox machine: sandboxd answers 507 and the step waits for space without using up attempts; counted as `taskforge_sandbox_disk_full_total` **[P4]**. Postgres's own disk was not drilled | Steps resume when space returns. Drill: 160/160 attempts after 60 s full |
| Step function bug | Step run goes to `FAILED` with the traceback | Other branches continue; the operator can requeue |

### 4.11 Compared with Temporal

Temporal is the best-known engine for this job. It records every event of a workflow and rebuilds state after a crash by re-running the workflow code against that history, which requires workflow code to be strictly deterministic. TaskForge saves the *output of each step* instead and never re-runs finished steps. That is simpler to reason about and to explain, at the cost that a workflow must be written as explicit steps with explicit successors. TaskForge also lacks what Temporal provides out of the box: multi-machine scaling, workflow versioning tools, and a mature UI. Moving to several machines would need only more worker hosts pointing at the same Postgres plus one `sandboxd` per host; the claim and commit rules already assume workers do not trust each other's clocks or liveness.

---

## 5. Sandbox service (`sandboxd`)

**In plain words.** A sandbox is a Docker container: a small Linux environment with its own files and processes, cut off from the host and the network. Models run commands inside it. Grading never happens in the container the model worked in.

**API** (HTTP with JSON bodies; the shape follows AIO Sandbox so it is familiar):

| Call | Does |
|---|---|
| `POST /v1/images/build` | Build a task's image from its `environment/`. The image id is the hash of the build inputs, so an unchanged task is never rebuilt. |
| `POST /v1/sandboxes` | Create a sandbox from an image with limits. Returns its id, how long the start took, and whether it came from the warm pool. |
| `POST /v1/sandboxes/{id}/exec` | Run a command with a timeout. Returns exit code and output. Long output keeps its tail and says explicitly that it was truncated. |
| `PUT` / `GET /v1/sandboxes/{id}/files` | Write or read files. |
| `POST /v1/sandboxes/{id}/snapshot` | Save `/app` inside sandboxd and return a reference (the hash of the archive). The archive never travels through a worker **[P2]**. |
| `DELETE /v1/sandboxes/{id}` | Remove it. |
| `POST /v1/verify` | Start a **fresh** container from the task image plus the hidden tests, restore an `/app` snapshot into it, run the tests, return the reward. |
| `POST /v1/pool/prewarm` | A demand hint: "about *n* sandboxes of this image will be asked for soon". |

**Isolation defaults:** no network; CPU, memory and process-count limits; all Linux capabilities dropped except three that root needs for the sandbox's own setup work (CHOWN, DAC_OVERRIDE, FOWNER: restoring and re-owning `/app` before grading) **[P1]**; no privilege escalation; every command a model asks for runs as `agent` (uid 1000), which holds no capabilities at all; a time limit after which the container is removed even if nobody asks.

**Grading rules** (each exists because a cheating drill on an earlier project beat the simpler setup):

1. `tests/` and `solution/` are never copied into the sandbox the model works in.
2. Grading runs in a fresh container; only `/app` is carried over.
3. Tests, and any model-written code they call, run as an unprivileged user with Python in isolated mode. The reward file is written afterwards by a wrapper from the test exit code, not by anything the model's code can reach.
4. Every sandbox a model works in contains a decoy answer file (`/opt/grader/expected_outputs.json`) with a unique marker string (a canary); the grading container does not. Any command that touches the decoy path, or any output containing the marker, flags the trajectory as suspect. **[P1]**
5. Hidden tests run the agent's program in a separate process and never import it, so code the agent wrote cannot end the test process with a success code. The Task Builder prompt requires this, and the scripted `exit_on_import` shortcut checks it. **[P1]**

**Warm pool [P2].** The workflow gives demand hints: after the oracle gate passes it asks for 2 + 2 × 7 sandboxes of that task's image (initial-state check and each scripted shortcut need one to work in and one to be graded in), and before the difficulty gate for 2 × 5 more. Background threads start containers until `idle ≤ demand`; every hand-out lowers the demand by one, so once the expected work is done nothing more is started. A container is used once and destroyed. Idle ones nobody claimed are removed after 5 minutes. Measured on the VM: handing out a pre-started container takes 0.04 ms inside the service against 248 ms for a cold start when idle; during a real run with two workers the medians were 2.5 ms and 1.15 s. In that run 53 of 108 hand-outs came from the pool: the Task Builder's self-check and the oracle gate run before any hint is given, and a hint can arrive only moments before the demand.

**Background removal and a pool that tops itself up [P4].** Deleting a container takes about 1 s on an idle machine and 3 s under load, and before P4 every attempt waited for two of them (its own container before grading, the grading container before its result). Now `WarmPool.release()` returns at once and four threads delete returned containers in batches of up to 50 per `docker rm -f`; 30 in one command take 3.3 s. A container waiting to be deleted still counts against the cap. The pool remembers the whole hint and keeps up to 8 per image started, topping up as they are taken; pre-started containers may not take the last eighth of the slots. The difficulty check also hints its second batch of attempts and re-runs of failed ones. Effect on the load test: 47 attempts a minute at 16 at once instead of 31.5, and 2.2 s per attempt outside the model instead of 11.7 s ([docs/load_test.md](docs/load_test.md), D-043).

**Capacity.** `max_containers` (default 48) counts everything sandboxd has started: in use, idle and being created. At the limit it answers "busy" (section 4.8).

---

## 6. Model gateway

**In plain words.** One module that every model call goes through. It is where spending is controlled and recorded.

| Feature | Rule |
|---|---|
| Idempotent calls | Response stored under the idempotency key (section 4.6). A key is first claimed as "in flight"; a second caller with the same key (the same step running twice by accident) waits for the first to finish and then reads the stored reply, instead of calling the provider too **[P2]**. An in-flight claim older than 10 minutes can be taken over; only then could one request be billed twice, and the ledger would show it. **[P4]** A claim also records the step run that made it and is taken over at once when that step run is no longer held by the claim's worker (it died; the drill without this waited 600 s). |
| Rate limiting | Per provider, a token bucket: a counter refilled at a fixed rate; each call must take tokens from it or wait. The bucket is a row in Postgres updated in one statement, so all worker processes share one limit **[P2]**. A long wait gives the step back to the queue without using up an attempt. |
| Retries | "Too many requests" and server errors are retried with growing, randomised delays. |
| Circuit breaker | After 5 consecutive failures a provider is skipped for 30 s, then tested with a single call. Kept per worker process **[P2]**: each process learns on its own, which costs a few extra failed calls and needs no shared state. A bad request (the provider answered, the request is wrong) neither retries nor counts. |
| Cost ledger | Every call writes model, role, prompt version, token counts, cost, latency and the step run that made it. Cost per task and per accepted task come from this table. |
| Budget guard | Section 4.9. |
| Batch mode | Non-urgent calls (rubric scoring, Blind Judge) use the batch endpoint at half price. Arrives with the real adapters in P3. |
| Prompt registry | Each role's prompt is a versioned file; its hash is logged with every call. |

**Models.** Every model role inside the system uses `claude-opus-5-5` through the official `anthropic` SDK. The weak model under test is Qwen3-Coder-30B-A3B through OpenRouter with one pinned provider and fallbacks disabled. Rubric scoring and the Blind Judge may later move to `claude-sonnet-5-5`; that is the owner's call after the pilot.

**Information barrier for judges (invariant 4).** A judge function accepts only a `JudgeView`, and the only way to construct one is `render_for_judge(trajectory)`, which keeps commands and outputs and drops the reward and the model's own reasoning text. The model's reasoning is a claim, not evidence.

---

## 7. Storage and lineage

**In plain words.** Small structured things go in Postgres. Large files go in a blob store and are referred to by the hash of their content. Every record lists the records it was made from.

| Table | Holds |
|---|---|
| `runs` | Workflow name and version, parameters, status, `paid_approved`, `budget_usd`, `spent_usd` |
| `step_runs` | Section 4: status, key, class, lease fields, attempt, input, output, error, `join_parent_id`, `open_branches`, trace id |
| `events` | Append-only history of every state change |
| `records` | Every business record: `id` (content hash), `kind`, `schema_version`, `body` (JSON), `parents` (ids), `produced_by` (step run) |
| blobs | Not a table: bytes live in the blob store under their SHA-256 hash (S3 API in the stack, a folder or memory in tests) **[P2]** |
| `reservations`, `signals`, `branch_results`, `rate_buckets`, `llm_cache`, `kb_docs` | Budget reservations in flight; signals that arrived early; results of ended branches; shared rate-limit buckets; replies by idempotency key; knowledge-base documents **[P2]** |
| `llm_calls` | The cost ledger, plus the stored response blob |
| `sandbox_runs` | Image, purpose (agent / oracle / initial / hack / verify), limits, start latency, exit code |
| `reviews` | Human decisions (section 10) |

Records are never edited. A repaired task is a new record whose `parents` contains the task it came from. Following `parents` upward from an accepted task reaches its proposal, its bad pattern, the rubric items and the trajectory pairs (invariant 5). `produced_by` links every record to the exact step run, and through `llm_calls` to the exact prompts, that created it.

---

## 8. Agent runtime and tool registry

**Agent runtime (`core/agent`).** One loop used by every role that acts in a sandbox (weak-model rollouts, Task Builder, the model-driven Hacker, the Repairer): ask the model for a tool call, execute it in the sandbox, append the result, repeat until the model calls `finish` or a budget runs out. Wall-clock time is the main budget; a step cap only guards against endless loops. A failed tool call becomes text the model can read, never an exception. Trajectories are stored in a format compatible with ATIF v1.8 (the format of the stored runs), so imported and new trajectories are read by the same code.

**Tool registry (`core/tools`).** A tool is a name, a description, a JSON schema for its arguments, a handler function and a scope saying which roles may use it. The registry renders the same definition as a tool spec for model calls and as an MCP server for outside clients.

| Tool | Scope |
|---|---|
| `run_shell`, `read_file`, `write_file`, `finish` | every sandbox role |
| `kb_search` | registered, for agent roles that search the knowledge base themselves; today's roles receive knowledge-base results in their prompts instead |

---

## 9. Knowledge base

A searchable memory across runs, kept in Postgres with full-text search plus the same word n-gram overlap measure used by the contamination check.

| Kind | Read by | Why |
|---|---|---|
| bad patterns, earlier proposals, accepted tasks | Proposal Writer | avoid near-duplicates, cover a pattern from different angles |
| human repair examples | Repairer | few-shot examples (the spec requires human fixes to flow back) |
| rubric items a human deleted at H1 | Rubric Writer | counter-examples |
| hack strategies found by humans at H4 / H5 | Hacker | new attacks become permanent |

Vector search is deliberately left out of v1: it would add an embedding provider, and lexical search is enough at this scale.

**Stable replays [P2].** A prompt that includes search results could differ between a step's first attempt and its retry, because other steps add documents in between; the retry would then miss the reply cache and pay again. So each document records the newest step run that existed when it was added, and a step only sees documents added before the step itself was created. Its own additions and anything later are invisible to it on every attempt.

---

## 10. Human review

Seven review points from the spec. H1 and H6 stop the whole batch; the others hold back only the item under review while the rest keep flowing.

| # | Where | Reviewer sees | Allowed decisions | Feeds back into |
|---|---|---|---|---|
| H1 | after rubric validation | bad-pattern table, 2–3 evidence pairs per pattern | merge / split / delete patterns; delete rubric items | taxonomy; deleted items → Rubric Writer counter-examples |
| H2 | after overlap check | 3–5 proposals per pattern, plus every proposal in the grey overlap zone | keep / drop | overlap threshold; dropped look-alikes → Proposal Writer |
| H3 | repair or hack-fix rounds used up | task, each round's change and pass count, weak trajectories, judge verdict | edit instruction / tests / environment / oracle, or drop | repair examples → Repairer |
| H4 | after Blind Judge | tasks judged unreasonable or unclear; a per-pattern sample of accepted tasks, repaired ones first | keep / reject | new hacks → Hacker |
| H5 | before export | sample of passing weak trajectories | clean / shortcut | new hacks → Hacker, plus a verifier fix for that task |
| H6 | before export | batch summary: gate pass rates, pattern coverage, difficulty spread | export / hold | which patterns need more tasks |
| H7 | side path | about 50 tasks labelled blind with the judge's four labels | — | disagreements → Blind Judge few-shot examples |

**Rules enforced by the server, not by convention:**

- People can only tighten. The set of allowed decisions is computed from the automatic decision: an accepted task can be rejected; a task rejected by an executed gate never enters a queue at all (invariants 1, 3, 6).
- Anything a person edits at H3 becomes a new task record and goes through every gate again with fresh rollouts (invariant 7).
- Every decision needs a reason code from a fixed list and records the automatic decision next to the human one, so the disagreement rate per stage can be computed.

Two interfaces over the same `reviews` table: the web console in `taskforge/api` (queue, one page per review with the decision form, a page per record showing what it was made from, and per-stage statistics), and the file-based flow the spec describes (`taskforge review export DIR`, fill in `decision` and `reason_code`, `taskforge resume --stage H1 --dir DIR`). With the Postgres backend a decision record, the review being marked decided and the signal that wakes the waiting step are one transaction **[P2]**.

---

## 11. Observability

| Signal | What | Tool |
|---|---|---|
| Traces | One trace per run **[P2]**, one span per step execution, child spans for each model call and sandbox command; the worker passes the trace context to sandboxd in a `traceparent` header, so spans from both processes join one trace. Spans carry `step`, `key` and ids, so one task's spans are found by filtering | OpenTelemetry → Jaeger |
| Metrics | Queue length and oldest waiting step per class; step transitions; lease expiries; fenced commits; retries; step duration; dollars spent, reserved and by role; model calls and cache hits; requests paid twice; reviews pending; gate results and task decisions. All read from Postgres by the api process on each scrape **[P2]**, so workers need no metrics port. sandboxd adds pool size, hand-out time (pool vs cold) and command time | Prometheus → Grafana (19 panels, generated by `deploy/grafana/make_dashboard.py`) |
| Logs | JSON lines carrying `run_id`, `step_run_id` and `trace_id` | stdout |
| History | The `events` table: every state change in order | Postgres |

---

## 12. The task-generation workflow

### 12.1 Steps

Stage numbers refer to section 5 of the spec.

| Step | Class | Spec stage | Does |
|---|---|---|---|
| `ingest_trajectories` | cpu | 1 | Import stored trial folders (`agent/trajectory.json` in ATIF v1.8, reward from `result.json`) |
| `pair` | cpu | 1 | Same task, strong model passed, weak model failed |
| `write_rubric` | llm | 2 | Per pair: 3–6 yes/no items contrasting what each side did; must not reveal which side passed |
| `score_rubric` | llm (batch) | 3 | Score every item on every trajectory through `JudgeView` |
| `validate_rubric` | cpu | 3 | Partial-order rule (12.4) |
| `cluster_patterns` | llm | 2 | Group surviving items into bad patterns with evidence pairs |
| `h1_calibrate` | human | 3 | Batch stop |
| `propose` | llm | 4 | Per pattern: proposals from the abstract pattern description only |
| `check_contamination` | cpu | 4 | Exact and fuzzy fingerprint against the held-out benchmark: clean / grey / overlap |
| `h2_sample` | human | 4 | Sampled, plus all grey |
| `build_task` | agent | 5 | Task Builder writes the package and iterates in a sandbox until its own oracle passes (≤ 2 fix rounds) |
| `gate_oracle` | sandbox | 6 | Reference solution passes, twice |
| `gate_initial` | sandbox | 6 | Untouched state fails |
| `gate_hack` | sandbox, then agent | 6 | Scripted shortcuts, then a model-driven Hacker; a success sends the task to `fix_verifier` |
| `fix_verifier` | agent | 6 | Strengthen the tests, not drop the task (≤ R rounds, then H3) |
| `gate_leak` | cpu + llm | 6 | 12.4 |
| `difficulty` | fork of `rollout` × 5 | 6–7 | Count passes k; rerun 5 if k is 0 or 5; diagnose |
| `repair` | agent | 7 | One change per round, ≤ R rounds; the child task re-enters `gate_oracle` |
| `h3_fix` | human | 7 | |
| `blind_judge` | llm | 8 | Verdict from weak trajectories without scores |
| `h4_review` | human | 9 | |
| `h5_audit`, `h6_decide` | human | 10 | |
| `export` | cpu | 11 | Accepted set plus per-pattern funnel report |

### 12.2 Records

Field lists follow section 7 of the spec. Every record also has `id`, `schema_version`, `parents` and `produced_by` (section 7 here).

```text
Trajectory        task_ref, model, provider, harness, steps[], reward, source
TrajectoryPair    task_ref, strong_traj, weak_traj
RubricItem        pattern_id?, description, scoring_rule, validated, strong_mean, weak_mean
BadPattern        category, description, evidence_pairs[]
PatternBrief      the only view of a BadPattern the Proposal Writer receives: category + description, no task text
TaskProposal      target_pattern_id, statement, contamination_status
TaskPackage       environment/, instruction.md, tests/, solution/, task.toml  (Harbor layout, stored as one blob)
VerifyRecord      oracle_pass, oracle_stable, initial_fails, hack_results{strategy: passed}, leakage_ok,
                  weak_pass_count, weak_n_runs, decision, reasons[]
DifficultyRecord  parent_task, round, diagnosis (too_easy | exploit | broken | truly_hard), mutation,
                  changed_files[], pass_count, n_runs, by (agent | human)
JudgeRecord       verdict (reasonable | unreasonable | cannot_judge | no_good_solution), evidence
ReviewRecord      stage (H1..H7), reviewer, auto_decision, decision, reason_code, notes, minutes_spent
```

### 12.3 Invariants and how each is enforced

| # | Invariant (spec section 7) | Enforced by | Test |
|---|---|---|---|
| 1 | Executed gates are a hard veto; judge and rubric cannot override | Acceptance is computed by one function that returns REJECT if any gate field is false before it looks at any judge output | `test_gate_veto_beats_judge` |
| 2 | Hacker success → fix the verifier, do not drop the task | `gate_hack` can only return `Next(fix_verifier)` or `Next(h3_fix)` on a successful hack | `test_hack_routes_to_fix` |
| 3 | Benchmark overlap → dropped, never reviewed | `check_contamination` returns `Done(reject)` on overlap; review queues refuse records with that status | `test_overlap_never_queued` |
| 4 | Judge never sees scores | `JudgeView` type (section 6) | `test_judge_view_has_no_reward_or_reasoning` |
| 5 | Every task traces back to a bad pattern | A `TaskProposal` without `target_pattern_id` fails validation; lineage walk | `test_lineage_reaches_pattern` |
| 6 | People can only tighten | Allowed decisions computed server-side (section 10) | `test_human_cannot_revive_rejected` |
| 7 | Any modified task re-passes all gates with fresh rollouts | A modified task is a new record with a new id, so no earlier step run or rollout matches its key | `test_child_task_reuses_nothing` |
| 8 | One change per repair round, parent → child recorded | `DifficultyRecord` validation rejects more than one mutation | `test_single_mutation_per_round` |

### 12.4 Defaults for what the spec leaves open

| Open item in the spec | Default here | Reason |
|---|---|---|
| Pairing rule | Only "strong passed, weak failed" pairs | Simplest unambiguous signal |
| Partial-order threshold | Keep an item if `strong_mean − weak_mean ≥ 0.25` **and** `strong_mean > 0.5` | The second condition drops items where both sides score low and the weak side is merely lower |
| Difficulty gate | 5 runs; band 1 ≤ k ≤ 4; rerun 5 when k is 0 or 5; repair limit R = 2 | The spec's suggested values |
| Strong model for diagnosing all-fail tasks | `claude-opus-5-5` on the same agent runtime | No extra provider |
| Leak gate | Hard reject if: a workspace file of 40+ characters equals a solution or hidden-test file; or 50% or more of the solution's 8-word pieces appear in the instruction or workspace. A line of 24+ characters from a string in the hidden tests that appears in the workspace only flags the task for H4 **[P3]**: hidden tests often reuse lines of the visible input files (decision D-030). A model's *suspicion* of a leak sends the task to H4; it cannot reject **[P1]** | Keeps the hard veto on deterministic evidence only |
| Overlap with the benchmark | Texts are cut into overlapping 5-word pieces; the score is the larger of the two shares (of my pieces found in the item, of the item's pieces found in me). OVERLAP at 0.15 or more, or identical text; GREY from 0.05 **[P1]** | Containment in both directions, so neither padding nor partial copying hides overlap. 0.15 was the drop threshold of an earlier synthesis run, where the largest score against TB 2.1 was 0.006 |
| Hack strategies (scripted) | `empty_edit`; `constant_output`; `visible_lookup` (programs that print the visible sample's expected output); `exit_on_import`; `plant_test_overrides` (`conftest.py`, `pytest.py`, `sitecustomize.py` in `/app`); `outside_app_writes` (into `/tests`, the reward path, `site-packages` via a `.pth` file, a fake `pytest` on `PATH`); `decoy_copy` **[P1]** | Start from `taskengine-mvp`'s three plus those that beat a shared-container grader before |
| How gates, judge and review combine | `acceptance.decide()`: any executed gate failed or missing → REJECT, and nothing else is consulted. Otherwise a human `reject` → REJECT; a human `keep` → ACCEPT; no human decision and judge `reasonable` → ACCEPT; any other verdict → REVIEW (goes to H4) **[P1]** | Invariants 1 and 6. A person may keep a task the judge flagged, because the judge is a signal, not a gate |
| Sample sizes | H2: 3–5 per pattern; H7: about 50, scaled down for the pilot | The spec's suggested values |

---

## 13. What will be measured

Definitions are fixed here, before any run, so they cannot be adjusted to flatter a result.

| Number | Definition |
|---|---|
| Extra rejections beyond oracle | Counted per proposal, because a repaired task replaces its parent: (proposals whose task passed `gate_oracle` at least once and whose final outcome is REJECT) ÷ (proposals whose task passed `gate_oracle` at least once) **[P1]** |
| Cost per accepted task | total ledger dollars of the run ÷ accepted tasks, with the split by role |
| Human minutes per accepted task | sum of `minutes_spent` ÷ accepted tasks |
| Sandbox start latency | median and 95th percentile, cold start vs warm pool |
| Throughput vs concurrency | completed scripted rollouts per minute at 16 / 32 / 64 / 128 concurrent sandboxes, with the first resource to saturate |
| Lost steps after a kill | step runs expected by the workflow definition minus step runs recorded, after workers are killed at random during a run. Target 0 |
| Double charges after a kill | idempotency keys billed more than once in `llm_calls`. Target 0 |
| Replay waste | dollars spent on rollouts that were restarted ÷ total rollout dollars |
| Human/automatic disagreement | per stage, share of reviews where `decision ≠ auto_decision` |

---

## 14. Mapping to the target role

| Role responsibility | Where it lives here |
|---|---|
| Sandbox, Tool Use, Memory, knowledge base, trajectory management | Sections 5, 8, 9, 7 |
| Workflows for data synthesis, labelling, evaluation | Section 12 (synthesis), section 10 (labelling), `difficulty` and `blind_judge` (evaluation) |
| Stability, performance and resource use under high concurrency; end-to-end observability | Sections 4.8, 4.10, 11, 13 |
| Agent runtime, orchestration, state management, execution governance | Sections 4, 8 |
| Agents applied inside the data platform | Task Builder, Hacker, Repairer; review reason codes flowing back into prompts (section 10) |

---

## 15. Repository layout and build order

```
taskforge/
  core/
    engine/     step_runs state machine, claim/commit, waits, sweeper
    sandbox/    sandboxd service and its client
    agent/      tool-calling loop, trajectory format
    gateway/    provider adapters, cache, limits, ledger, budget guard
    store/      records, blobs, lineage
    tools/      registry, MCP server
    kb/         search
    review/     review rules, queues (memory and Postgres), file export/import
    obs/        tracing, metrics, logging setup
    pg.py       connection pool and the schema of every table
  taskgen/
    schemas.py  pairs.py  verify.py  difficulty.py  workflow.py  views.py
    agents/     rubric.py  proposal.py  builder.py  hacker.py  judge.py  repairer.py  llm.py
    prompts/    one versioned file per role (inside the package so installed copies find them)
  api/          review console (pages), control API (JSON), /metrics
  runtime.py    builds engine + services on memory or Postgres; settings from TASKFORGE_* variables
  cli.py
deploy/         docker-compose (Postgres, S3 store, Jaeger, Prometheus, Grafana), stack.sh, dashboard
tests/
docs/decisions.md
```

**P1 (logic, scripted fake models, no paid calls):** `taskgen/schemas.py` → `pairs.py` → rubric with the partial-order check as a pure function → proposal with the contamination check → builder with one handwritten task → gates in the order oracle, initial, hack, leak → difficulty loop → judge → review points → the eight invariant tests. In P1 the engine is a single-process in-memory version behind the same `Outcome` interface. The sandbox sits behind one interface with two implementations: a scripted fake for unit tests (the laptop has no Docker), and a thin Docker client on the VM for running the handwritten task for real.

**P1 limits and what P2 did about them.** Removed: no leases and no way to end a step past its `timeout_s` (the Postgres engine has both); `resume --stage` across processes (works on Postgres). Still true: H1 accepts keep/delete per pattern but not merge/split; a rollout branch that fails for infrastructure reasons after all its attempts rejects the task (`rollouts_failed`); an H3 edit that changes nothing, or touches a different area than the chosen action, rejects the task (`h3:invalid_edit`).

**P2 (platform), done:** Postgres engine with leases, fencing and joins, plus the worker loop → the same workflow tests on both backends → sandboxd with the warm pool → gateway protections → store, blobs and lineage → review queue in Postgres and the console → tools and MCP → knowledge base wired into prompts → tracing and metrics → the stack on the VM.

**How P2 is tested.** Every engine and workflow test runs twice, on memory and on Postgres (`tests/conftest.py`). `tests/test_pg_concurrency.py` adds what only many workers can show: 16 threads with 15% injected crashes on a fan-out of 280 steps; a stalled worker whose late commit must be refused; a heartbeat keeping a long step and a timeout ending a stuck one; signals racing with waits; 24 branches ending in the same instant; a reservation released when its worker dies; one request from two threads paid once; and three real worker processes with one killed by `kill -9`. Removing the fencing guard makes the stalled-worker tests fail, which is the evidence that they test it.

---

## 16. Glossary

| Term | Plain meaning |
|---|---|
| Container | An isolated mini Linux environment started from an image, with its own files and processes |
| Image | The frozen file system a container starts from |
| Worker | A background program that repeatedly picks up a step and runs it |
| Transaction | A group of database changes that either all happen or none do |
| Lease | A claim on a piece of work that expires by itself unless renewed |
| Heartbeat | The periodic "still alive" renewal of a lease |
| Fencing token | A number that goes up with each claim; writes carrying an old number are refused |
| Idempotent | Doing it twice has the same effect as doing it once |
| Idempotency key | The label that lets a service recognise a repeated request and return the earlier answer |
| At-least-once | Guaranteed to run, possibly more than once |
| Fan-out / join | Start several pieces of work; wait until all have finished |
| Back-pressure | Slowing intake when a downstream resource is full, instead of piling up half-started work |
| Token bucket | A counter refilled at a steady rate; each request must take from it |
| Circuit breaker | A switch that stops calls to a failing service for a while |
| Blob store | Storage for large files, addressed here by the hash of their content |
| Content-addressed | The id of a thing is the hash of its bytes, so equal content has equal id |
| Lineage | The chain of "made from" links between records |
| Trace / span | The timed record of one item's journey / one step within it |
| p95 | The value 95% of measurements fall below |
| MCP | Model Context Protocol, a standard way to offer tools to model clients |
| Oracle | The reference solution shipped with a task |
| Canary | A unique marker string planted to detect that something read a forbidden file |

---

## 17. Open questions and later work

- **Harness mismatch.** The stored trajectories were produced by a different agent harness (Terminus 2) than the runtime in section 8. A pattern caused by that harness's reply format would not reproduce here. Mitigation: H1 keeps only patterns visible in commands and outputs (for example "declares success without running a check"), and the pilot first confirms on a handful of tasks that the weak model shows the chosen pattern on this runtime.
- **Rollout checkpoints.** Per-turn checkpoints plus re-attaching to a live sandbox would remove replay waste (section 4.6).
- **Stronger isolation.** gVisor as the container runtime for model-controlled sandboxes.
- **Service tasks.** Grading tasks that must leave a server running.
- **Several machines.** Not built. It needs one sandboxd per machine with the gateway choosing between them, the blob store and Postgres reachable from all, and nothing else: claim, fencing and commit already assume workers share only the database.
- **Console access.** The stack listens on localhost only and is reached through an SSH tunnel; there are no user accounts.
- **Trace noise.** The web framework adds its own spans to every sandboxd request, roughly tripling the span count of a run.
- **Budget caps** are the owner's decisions and are set per run, not here.
