# Decision log

One entry per decision: what was decided, why, what was rejected, and what would change it. Newest at the bottom. Times are US Eastern.

D-001 to D-004 were first taken from the plan's recommendations, marked provisional. The owner then asked to start P1 without objecting to them (2026-11-16), and P1 is built on them, so they now stand.

---

## D-001 Repository name and location (2026-11-16; confirmed by going ahead with P1)
**Decision.** New standalone repository `taskforge`.
**Why.** The project is independent of the owner's other repositories (for example `agent_harness`); a repository of its own keeps that visible.
**Would change it.** The owner prefers another name or place. Renaming is a folder move plus the package name in `pyproject.toml`.

## D-002 Python only (2026-11-16; confirmed by going ahead with P1)
**Decision.** All code in Python 3.11+.
**Why.** The owner must be able to explain and defend every line. A Go service would split a small codebase into two languages, with code the owner could not defend as well.
**Rejected.** One small Go service (for example `sandboxd`).

## D-003 Own orchestration engine on Postgres, not Temporal (2026-11-16; confirmed by going ahead with P1)
**Decision.** Write a small step engine: one row per step run, leases with fencing tokens, single-transaction commit, branch counters for joins (DESIGN.md section 4).
**Why.** "Where is state kept and how do you recover from a crash" is the core question for this role; with Temporal the honest answer is "the library does it".
**Rejected.** Temporal (less code, mature, multi-machine). Kept as the comparison in DESIGN.md section 4.11.
**Risk.** Concurrency bugs in hand-written claim/commit code. Covered by kill tests in P4.

## D-004 Starting trajectories: reuse stored Terminal-Bench 2.1 runs (2026-11-16; confirmed by going ahead with P1)
**Decision.** Input to pairing is the stored runs on the VM: strong = `e2-baseline-strong` (gpt-6-sol, 41 tasks), weak = `qarm-h0-baseline` + `qarm2-h0-baseline` (Qwen3-Coder-30B-A3B, 30 tasks, 120 trials, 101 failures). Checked 2026-11-16: 26 tasks have a strong pass and a weak failure. Data only; no code is shared with the project that produced them.
**Why.** Free. New runs would cost roughly $50–130.
**Risk.** The patterns are mined from the held-out benchmark. Handled by `PatternBrief` (the Proposal Writer never sees task text) and the overlap check on every proposal and built task.

## D-005 Own agent runtime for new rollouts (2026-11-16)
**Decision.** New weak-model rollouts use the tool-calling loop in `core/agent`, not the harness that produced the stored trajectories.
**Why.** The agent runtime is part of what this project is meant to demonstrate, and it lets the engine, sandbox service and gateway see every model call and command.
**Risk.** A bad pattern caused by the old harness's reply format will not reproduce. Mitigation in DESIGN.md section 17: choose patterns visible in commands and outputs, and confirm the pattern on this runtime with a few tasks before the pilot spends on task building.

## D-006 At-least-once steps with deduplicated effects; rollouts restart from scratch (2026-11-16)
**Decision.** Steps may run more than once; model calls are deduplicated by idempotency key, records and blobs by content hash. A crashed rollout restarts in a fresh sandbox.
**Why.** Simple and correct. No request is billed twice.
**Cost.** Some repeated work after a crash, reported as replay waste.
**Rejected for v1.** Per-turn checkpoints with re-attach to a live sandbox.

## D-007 Models (2026-11-16)
**Decision.** Every model role in the system uses `claude-opus-5-5`, including the strong solver used to diagnose all-fail tasks. Weak model: Qwen3-Coder-30B-A3B via OpenRouter, one pinned provider, fallbacks off. Claude Fable is never used inside the system.
**Open.** Whether rubric scoring and the Blind Judge move to `claude-sonnet-5-5` is decided after the pilot shows their share of cost.

## D-008 v1 task scope: graded from files in `/app` (2026-11-16)
**Decision.** No tasks whose grading needs a long-running service.
**Why.** Grading happens in a fresh container that receives only `/app`; restarting a model-started service there safely is a project of its own.

## D-009 Knowledge base is lexical in v1 (2026-11-16)
**Decision.** Postgres full-text search plus n-gram overlap; no embeddings.
**Why.** Avoids another paid provider; enough at this scale.

## D-010 File language (2026-11-16, owner)
**Decision.** Everything in this repository is in English.

## D-011 Overlap thresholds (2026-11-16, P1)
**Decision.** 5-word pieces, score = larger of the two containment shares, OVERLAP at 0.15 (or identical text), GREY from 0.05. An empty benchmark index raises instead of reporting "clean".
**Why.** 0.15 was the drop threshold in an earlier synthesis run, where no synthetic task scored above 0.006 against TB 2.1, so it leaves wide room for honest tasks. Checking both directions means pasting a benchmark item into a longer text, or copying part of a short item, both still count.
**Would change it.** H2 grey-zone decisions: if people keep most grey proposals, raise GREY; if they find look-alikes marked CLEAN, lower both.

## D-012 Acceptance rule (2026-11-16, P1)
**Decision.** `acceptance.decide()`: a failed or missing gate rejects before anything else is read; then a human reject, then a human keep, then the judge.
**Why.** The spec says the judge is an audit signal, not a gate, and that H4 can pass a judge-flagged task; the earlier wording in DESIGN.md ("judge verdict is not unreasonable") would have let the judge veto.

## D-013 Leak gate hard rules (2026-11-16, P1)
**Decision.** Three deterministic rules (DESIGN.md section 12.4). The model's leak suspicion is stored and sends the task to H4, never rejects.
**Risk.** The string-literal rule can misfire if a hidden test re-uses a visible sample line of 24+ characters that is not in the instruction. Seen at H4 if it happens.

## D-014 Hidden tests run agent code in a subprocess (2026-11-16, P1)
**Decision.** Required by the Task Builder prompt; checked by the `exit_on_import` shortcut.
**Why.** If tests import the agent's module, one line (`os._exit(0)`) at import ends pytest with a success code. Grading in a fresh container does not prevent that; only the test design does.

## D-015 Measures counted per proposal (2026-11-16, P1)
**Decision.** "Extra rejections beyond oracle" counts proposals, not task versions.
**Why.** A repaired task replaces its parent. Counting versions would count one proposal several times and call the parent "rejected" even when its child is accepted.

## D-016 Sandbox details (2026-11-16, P1)
**Decision.** Containers keep three capabilities for root's own setup work (CHOWN, DAC_OVERRIDE, FOWNER); every model command runs as uid 1000, which has none. The decoy file is in every sandbox a model works in, not in the grading container. The task Dockerfile gets two users and `/app` ownership appended at build time, so tasks need not know about them.
**Measured on the VM.** Sandbox start (create + decoy) took a median 0.30 s, max 0.38 s over 20 starts, without a warm pool. The handwritten task's full gate check (20 containers) took 37 s.

## D-017 Python environment on the VM (2026-11-16, P1)
**Decision.** `uv venv` + `uv pip install -e ".[dev]"` in `/root/work/taskforge`.
**Why.** The VM's system Python lacks `python3-venv`; `uv` was already installed, so no system package was added.

## D-018 Postgres engine: three rules added while building it (2026-11-16, P2)
**Fork generations.** A step can fork more than once. Each fork raises the parent's `fork_gen`; children carry it as `join_gen`; a branch only changes the parent's counter when the two match and the parent is still waiting. Without this, a branch ending late (or a failed step the operator runs again) would decrement the counter of a *later* fork.
**Signal lock.** "A step starts waiting for X" and "X arrives" both take a Postgres advisory lock keyed on run and signal name. Without it each side can commit without seeing the other and the wake-up is lost.
**Reservations are rows.** A budget reservation belongs to a step run; the sweep that takes a dead worker's step back also releases its reservations.
**Evidence.** `tests/test_pg_concurrency.py`, repeated 8 times with no failure. With the fencing condition removed from the commit statement, the stalled-worker and timeout tests fail (the stale result overwrites the newer one); restored, they pass.
**Budget interface.** `reserve(scope, estimate)` returns a token, `settle(token, actual)` uses it; both engines implement it.

## D-019 Workers also serve `human` steps (2026-11-16, P2)
**Decision.** A "human" step is a normal step that opens a review and returns `Wait`; workers run it like any other. DESIGN.md first said workers never claim that class, which would have left reviews unopened.

## D-020 Sandbox snapshots stay in sandboxd; the pool takes demand hints (2026-11-16, P2)
**Decision.** `snapshot_app` returns a reference (hash); the archive is kept by sandboxd and used by `verify`. `prewarm(image, n)` means "n more will be asked for soon"; each hand-out lowers the demand; containers are single-use.
**Why.** Archives of `/app` should not pass through workers twice per grading. A fixed pool size per image would either leak idle containers or need someone to shrink it; a demand counter drains by itself.
**Measured.** Idle machine: 0.04 ms from the pool vs 248 ms cold (inside the service). During a two-worker run: medians 2.5 ms vs 1.15 s; 53 of 108 hand-outs from the pool.
**Known gap.** Hints arrive only after the oracle gate, so the Task Builder's self-check and the oracle gate always start cold.

## D-021 sandboxd removes leftovers on start (2026-11-16, P2)
**Decision.** On start, every container labelled as owned by sandboxd is removed. Each sandboxd instance (and each test server) uses its own owner label and only ever removes its own.
**Rejected.** Re-adopting containers after a restart: sandboxd would have to persist which step holds which container; retrying the affected steps is simpler and already works.

## D-022 SeaweedFS instead of MinIO for the S3 store (2026-11-16, P2)
**What happened.** `minio/minio` could not be pulled from Docker Hub or quay.io on 2026-11-16.
**Decision.** `chrislusf/seaweedfs` in S3 mode, with one access key generated by `stack.sh`. `S3BlobStore` needed no change: put, has, get and a missing key were checked against it before switching.
**Would change it.** Any S3-compatible store works; the interface is three methods.

## D-023 Gateway protections (2026-11-16, P2)
**In-flight claim.** An idempotency key is claimed before the provider is called; a second caller waits and reads the stored reply. Takeover after 10 minutes is the one path where a request could be billed twice, and the ledger would count it.
**Shared rate limit, per-process breaker.** The token bucket is a Postgres row so all processes share one limit, because the provider's limit is per account. The breaker is per process: sharing it would save a handful of failed calls and cost a shared-state protocol.
**Bad requests** do not retry and do not count against the breaker.

## D-024 Metrics from Postgres; one trace per run (2026-11-16, P2)
**Decision.** The api process computes every engine, gateway and review metric from Postgres on each scrape. One trace covers a whole run.
**Why.** Workers already write all state to the database, so one exporter reports for all of them and survives worker restarts. One trace per item would need a rule for where items begin; filtering one run's spans by `key` gives the same view.
**Cost.** Each scrape runs about a dozen aggregate queries; fine at this size, and the place to add counters tables if the event history grows large.

## D-025 Knowledge-base results must not change between a step's attempts (2026-11-16, P2)
**Decision.** Each document stores the newest step-run number at the time it was added; a step only sees documents with a smaller number than its own.
**Why.** Otherwise a retry builds a different prompt, misses the reply cache, and pays again for work already paid for.

## D-026 MCP through the SDK's low-level server (2026-11-16, P2)
**Decision.** `core/tools/mcp.py` builds an `mcp.server.Server` (SDK 2.3) with `on_list_tools` / `on_call_tool` from the registry, exposing `run_shell`, `read_file`, `write_file` on one sandbox per connection. Checked against the installed SDK's signatures, and tested with the SDK's own in-process client.
**Removed.** The planned `run_hidden_tests` tool: the Task Builder is not a free-running agent, it calls the self-check directly.

## D-027 How the stack runs (2026-11-16, P2)
**Decision.** Postgres, the S3 store, Jaeger, Prometheus and Grafana run under Docker Compose. sandboxd, the api and the workers are host processes started by `deploy/stack.sh` (logs and pid files under `runs/stack/`). Everything listens on 127.0.0.1 and is reached through an SSH tunnel.
**Ports.** Postgres 54329, S3 59000, Jaeger UI 56686, traces in 54318, Prometheus 59090, Grafana 53000, sandboxd 58700, api and console 58800.
**Measured.** A scripted run through the stack (2 worker processes, real containers, reviews over HTTP): 60 step runs, 108 sandboxes, 102 s, 0 requests paid twice. Engine overhead alone: about 300 to 360 empty step runs per second per worker process.

## D-028 P3 pilot: real models, settings, and what the first hour showed (2026-11-16, owner approved)
**Approved.** 10 proposals for one pattern; caps Anthropic $30 and OpenRouter $15 inside a $45 run budget; the owner does the reviews.
**Connections.** `core/gateway/anthropic.py` (official SDK 2.9: streaming, effort per role, prompt caching in agent loops, assistant turns sent back exactly as received including reasoning blocks, refusals recorded, server-side fallback on one-shot calls only) and `core/gateway/openrouter.py` (one pinned host, no fallback, cost from the reply). Smoke test: $0.01, all three paths worked.
**Measured.** Mining 26 pairs cost $9.28 (estimate $9.5). The partial-order check kept 139 of 144 items: with one strong attempt per task it hardly filters, which makes H1 the real filter. Task Builder: $0.84 per task (estimate $0.40), 42k output tokens each, mostly hidden thinking at effort high; no fix rounds needed in the first 7. Hacker: $0.013 per call (low effort + caching).
**H1.** Kept skips-end-to-end-verification. The two terminal-session patterns were avoided because they cannot reproduce on this runtime (section 17).

## D-029 Qwen host: SiliconFlow -> Amazon Bedrock (2026-11-16 22:45 ET, owner approved)
**What happened.** About 25 minutes after the smoke test, OpenRouter stopped listing SiliconFlow for qwen3-coder-30b-a3b-instruct; the pinned route returned 404 "No endpoints found", and the first task to reach the difficulty check lost all 5 attempts.
**Decision.** Pin `amazon-bedrock/us-west-2` ($0.15 / $0.60, 2.1x). No Qwen attempt had succeeded before the switch, so every attempt in the pilot comes from one host. Bedrock does not state its precision.
**Also.** A host that is not serving the model now makes callers wait (retry after 60 s without using up attempts) instead of failing, so an outage pauses work rather than rejecting tasks.

## D-030 Leak rule "hidden-test line in the workspace" is a flag, not a rejection (2026-11-16, owner approved)
**What happened.** It rejected a config-converter task because the hidden tests reuse lines of the visible input files (`host = db-primary.internal`). Inputs are not answers; the model's leak check said no leak. This was the risk noted in D-013.
**Decision.** Matching lines send the task to H4 (`test_lines_in_workspace` flag). The two strong rules stay hard rejections.

## D-031 Operator re-run of a branch (2026-11-16, owner approved)
**Decision.** `engine.reopen_branch(run, step, key, new_key)` (CLI `run reopen`) runs a step that ended a branch again under a new key, back inside the same fork, while the parent still waits; its new outcome replaces the old one in the run's results. Rollout and strong-run keys now follow the difficulty step's key, so a re-run gets fresh attempts.
**Used for.** The two pilot tasks lost to D-029 and D-030. Their old step rows and old decision records stay (records are immutable); the run's export uses the new outcomes.

## D-032 Zero bytes in command output (2026-11-16 22:56 ET, bug fix during the pilot)
**What happened.** A Qwen command printed a NUL byte; Postgres cannot store `\u0000` inside JSON, so saving the attempt failed, the attempt step failed, and its task was rejected as `rollouts_failed` before it could be re-run.
**Fix.** The agent loop and the Docker sandbox replace NUL with the visible text `\0` before anything is stored or shown to the model. Test: `test_zero_bytes_in_command_output_never_reach_storage`. The task was recovered with `reopen_branch`.

## D-033 Tool names sent back to OpenRouter hosts are cleaned (2026-11-16 22:59 ET, bug fix during the pilot)
**What happened.** Qwen emitted a tool call named `finish</function` (markup glued to the name). Our registry answered "unknown tool", which is right, but the name was then sent back as history, and Amazon Bedrock rejects any request whose history contains a tool name outside `[a-zA-Z0-9_-]`; the attempt failed and its task was rejected as `rollouts_failed`.
**Fix.** `openrouter.clean_name()` replaces other characters with `_` in the history only. The task was recovered with `reopen_branch`.

## D-034 Fixes from the pilot, before P4 (2026-11-17, owner approved)
- **Failed attempts are retried.** The difficulty check re-runs only the attempts that failed for infrastructure reasons, under new keys (`rollout:<task>:<i>#r1`), up to `rollout_retries = 2` times, keeping the finished ones; only then does it reject. The strong attempt in diagnosis is retried the same way. Test: `test_an_attempt_that_keeps_failing_is_retried_instead_of_rejecting_the_task`.
- **The Repairer is told the pass rate and the target range** and asked for the smallest change; the prompt names overshooting as a failure.
- **Hidden tests must not reuse the visible example's inputs or expected output** (Task Builder prompt).
- **Warm-pool bookkeeping.** `WarmPool.reconcile()` (run by the reap loop) forgets sandboxes counted as in use whose container no longer exists. Test: `test_warm_pool_forgets_sandboxes_whose_container_is_gone`.
- **Not done, because they cost money:** more strong attempts per task for the partial-order check; comparing Task Builder effort high vs medium. Both are in the P4 plan.


## D-035 Every container of an image looks the same to the agent (2026-11-17, P4 drills)
- **Found by:** drill kill_worker. The 16 attempts a killed worker was running were re-run; each got only its first model reply from the cache and paid 84 calls again. Cause: `ls -la /app` prints a `..` line with the time of `/`, and Docker sets that to when the container was made. A re-run in a later minute sees different output, so every later request differs from the cached one.
- **Change:** `DockerSandbox.create` gives every container the hostname `sandbox` and sets the times of `/`, `/opt` and the decoy folder to the image's `/app` time. Test: `test_two_containers_of_one_image_look_the_same_to_the_agent`.
- **Result:** the same drill afterwards paid exactly the no-fault 1,600 calls (100 replayed from the cache). Commands whose output really varies (`date`, random numbers) still end the replay; that cannot be avoided.

## D-036 A model call left in flight by a dead worker is taken over at once (2026-11-17, P4 drills)
- **Found by:** drill kill_worker, once D-035 made replays exact: the re-run attempts reached the very call the dead worker had in flight, found it claimed, and waited for the claim to be 600 s old. The run took 659 s instead of 156 s. One-shot steps (Task Builder, judge, scorer) would hit this on every crash, because their retried request is always identical.
- **Change:** a claim in `llm_cache` records the step run that made it (`step_run_id`). It is dead as soon as that step run is no longer RUNNING under the claim's worker (lease owners are `<worker>:<thread>`, claim owners `<worker>`), which is the case once the sweeper has re-queued the step. Calls outside any step keep the 600 s rule. Test: `test_a_model_call_left_in_flight_by_a_dead_worker_is_taken_over_at_once`.
- **Accepted risk:** a worker that only stalled (not dead) past its lease may still finish its call; both calls are then paid, and the ledger counts it as a double charge, so it is visible.

## D-037 "Too many requests" makes a step wait, not fail (2026-11-17, P4 drills)
- **Found by:** drill rate_limit (half of all calls refused with 429 for 90 s). The gateway's three quick tries ran out, the step's three attempts went in 15 s (waits of 5 s and 10 s), and 16 attempts failed for good; re-running them cost 271 extra paid calls (+17%).
- **Change:** `ProviderError(rate_limited=True)` for HTTP 429 (Anthropic `RateLimitError`, OpenRouter 429, the fake provider). When the quick tries are used up on a 429, the step is retried after 10-30 s without using up an attempt. Other retryable errors (5xx, connection) still use attempts, so a provider that is really broken still ends its steps. Test: `test_too_many_requests_makes_the_step_wait_without_using_up_attempts`.

## D-038 sandboxd frees sandboxes whose holder died (2026-11-17, P4 drills)
- **Found by:** drill kill_worker. The killed worker's 16 sandboxes stayed counted as in use until their one-hour expiry (D-034's reconcile only handles containers that are already gone). Every later drill waited 2 minutes for capacity because of them; with the pilot's 48 slots it would have been a third of the machine.
- **Change:** `SandboxdClient` tells sandboxd every 20 s which sandboxes it holds (`POST /v1/sandboxes/keepalive`); the pool removes any sandbox in use that has not been mentioned for 120 s (`WarmPool.reap_orphans`, counter `orphaned`). Grading containers, which sandboxd holds itself inside one request, are not tracked. Test: `test_warm_pool_frees_sandboxes_whose_holder_stopped_keeping_them_alive`.
- **Result:** the next kill_worker drill ended with 15 sandboxes freed as orphans and 0 left in use.

## D-039 A lost database connection inside a step is retried (2026-11-17, P4 drills)
- **Found by:** drill restart_postgres (down 1.6 s). Five steps were in the middle of a database call; `psycopg.errors.AdminShutdown` was treated as a bug in the step and they failed for good. Only the load run's own re-run saved them; most real steps (Task Builder, judge, repair) have no such re-run, so their tasks would be lost.
- **Change:** `PgEngine.execute` treats `psycopg.OperationalError` and `PoolTimeout` raised by a step as "run it again in 5 s without using up an attempt". If the database is still down, recording the retry fails too and the step's lease runs out instead, which the sweeper already handles.

## D-040 An unreachable sandboxd makes steps wait; sandboxd stops quickly (2026-11-17, P4 drills)
- **Found by:** drill restart_sandboxd. sandboxd was down 72 s: on the way out it removed ~70 containers one `docker rm` at a time. Every running step got "connection refused", used its three attempts in 15 s, and the load run's two re-run rounds failed the same way: 35 of 160 attempts finished.
- **Change:** a request that never reached sandboxd (`httpx.ConnectError`, `ConnectTimeout`) raises `SandboxUnavailable`: retried after 10 s without using up an attempt. A request cut off half-way still uses one (the sandbox may be gone). sandboxd removes containers in batches of 50 per `docker rm` (`DockerSandbox.destroy_many`), on shutdown and on start. Test: `test_an_unreachable_sandboxd_makes_steps_wait_without_using_up_attempts`.

## D-041 A full disk on the sandbox machine makes steps wait for space (2026-11-17, P4 drills)
- **Found by:** drill disk_full (the disk holding sandboxd's copies of /app full for 60 s). Saving a copy failed with `ENOSPC`, sandboxd answered 500, and 29 steps used up their attempts; re-running them cost 52% more paid calls than the no-fault run.
- **Change:** sandboxd answers `ENOSPC` with HTTP 507 and counts it (`taskforge_sandbox_disk_full_total`); the client turns 507 into `SandboxUnavailable` with a 30 s wait that does not use up an attempt. Test: `test_a_full_disk_on_the_sandbox_machine_makes_steps_wait_for_space`.

**Common rule behind D-037 to D-041.** A fault of the machinery (provider throttling, database, sandbox service, disk) must not use up the attempts of the step that happened to be running: the step waits. A fault that never ends then shows up as a stalled run on the dashboard instead of as silently rejected tasks. For a data pipeline that is the better failure: dropped work costs model money to redo, a stall costs nothing.

## D-042 The paid P4 scale run is dropped (2026-11-18, owner approved)
- **Planned:** 60 proposals on 3 patterns from the pilot, about $107 (Anthropic $83, OpenRouter $24).
- **Why dropped:** this is a platform project, not a dataset. The five numbers the project plan asked for are all measured already: what the later checks reject after the oracle passes (pilot: 40%), cost per accepted task (pilot: $4.52), warm pool against cold start (P2, pilot), the ceiling of one machine and what limits it (load test), steps lost and calls paid twice after a crash (drills: 0 and 0). A scale run would only narrow the pilot's acceptance rate from about ±30 to ±12 points and make more tasks. The real-provider failures a platform has to survive (a provider leaving, malformed tool names, zero bytes in output) were already met and fixed in the pilot.
- **What is given up:** the D-034 prompt changes (Repairer told the target range, hidden inputs different from the example) are tested with fake models only, not re-run on real ones.
- **Instead ($0):** remove the bottleneck the load test found and measure again (D-043); then P5.

## D-043 Containers are deleted in the background, in batches; the pool tops itself up (2026-11-18, P4)
- **Found by:** `deploy/docker_probe.py`, which times each docker call on its own. `docker rm -f` takes 1.16 s with one at a time and about 3 s with 16 or more, while `docker run` takes 0.21-0.40 s up to 16 at once; one `docker rm -f` naming 30 containers takes 3.3 s. Every attempt waited for two deletions (its own container before grading, the grading one before its result). The first P4 write-up blamed container starts: its quick probe ran `docker rm` after the timer stopped.
- **Change:** `WarmPool.release()` queues the container and returns; four threads (`TASKFORGE_POOL_REMOVE_THREADS`) delete queued containers up to 50 per `docker rm -f`. A queued container still counts against `max_containers`; shutdown waits for deletions already running. The pool keeps the whole demand hint (up to 1,000) and at most 8 started per image, refilling as they are taken (`TASKFORGE_POOL_REFILL_THREADS`, default 8); pre-started containers may not take the last eighth of the slots (`reserve`). The difficulty check also hints its second batch and re-runs of failed attempts. The load run gives the difficulty check's hint (2 per attempt). Tests: `test_release_returns_at_once_and_removal_is_batched_but_still_holds_the_slot`, `test_warm_pool_tops_itself_up_while_the_hinted_work_runs`, `test_pre_started_sandboxes_never_take_the_reserved_slots`.
- **Result:** attempts a minute 31.5 → 47.1 at 16 at once, 61.9 → 86.7 at 32, 117.3 → 160.2 at 64, 195.7 → 242.6 at 128; time outside the model 11.7 → 2.2 s per attempt at 16. 0 calls paid twice. Four runs at 128 (238.5, 232.6, 242.6, 247.3) show the gain there is the background deletion; they differ by about ±3%.
- **Not built:** a cap on how many containers start at the same moment (proposed in the first write-up). The probe showed Docker gets more done with more at once (0.68 to 7.48 start-and-delete cycles a second from 1 to 64), so a cap would have lowered throughput.
- **Limit now:** CPU at 128 at once (75% mean, 89% peak). The platform's own Python is about 3% of the machine; starting the `docker` program for every operation is about 3 of 16 cores (76 starts a second x 40 ms). Next step, not done: call Docker's HTTP interface on its local socket from sandboxd.

## D-044 Steps that drive an agent outlast the agent's time budget (2026-11-18, owner approved)
- **Found by:** re-reading the engine while writing up the project. The five steps that drive an agent loop (`build_task`, `gate_hack`, `fix_verifier`, `rollout`, `repair`) kept the default `timeout_s` of 900 s, while the agent's time budget (`solver_wall_s`, and the Hacker's default) is 1800 s. The heartbeat stops renewing a lease once a step is past its own timeout, so an attempt longer than 900 s would have been treated as stuck: another worker runs it again while the first copy keeps calling models, and the two meet on the same idempotency key, where the D-036 takeover can pay for it twice. In the pilot the longest `rollout` took 772 s and the longest `build_task` 499 s, so it never fired.
- **Change:** those five steps declare `timeout_s = AGENT_STEP_TIMEOUT_S` (3600 s). That is the 1800 s budget plus `AGENT_STEP_SLACK_S` (1500 s): room for one last turn that starts just before the budget runs out (a model call, up to 900 s on the Anthropic client, and a shell command, up to 150 s) and the grading after it (up to 420 s). `TaskgenConfig` refuses a `solver_wall_s` or `hacker_wall_s` that does not leave that slack. The Hacker's budget is now a config field (`hacker_wall_s`, 1800 s) instead of a default hidden in `RoleLLM.agent`. Test: `test_agent_steps_outlast_the_agent_time_budget` (fails with the old timeouts).
- **Cost of the change:** a worker that dies is still noticed within the 60 s lease, but an agent step that hangs inside a live worker is now taken back after up to an hour instead of 15 minutes.
- **Checked:** 149 tests pass on the VM (Postgres and Docker included); a demo run on the restarted stack (`run-e6ef712780`) created its agent steps with 3600 s and the rest with 900 s, 75 calls for 75 keys.
