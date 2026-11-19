# TaskForge

**English** | [中文](README.zh.md)

A small data-production platform whose first workflow is failure-driven task synthesis for coding agents: mine a weak model's bad patterns from strong/weak trajectory pairs, generate terminal tasks that target each pattern, and accept a task only after executed gates (oracle, initial state, hack, leak, difficulty) and human review.

**Status:** P3 pilot done (2026-11-17): the first run on real models (Claude Opus 5.5 for every role, Qwen3-Coder-30B as the weak model) produced 6 accepted tasks from 10 proposals for $27.09 ([docs/pilot_report.md](docs/pilot_report.md)). P4 load test and failure drills done the same day on fake models: 128 attempts at once on one 16-core VM, and seven drills that found and fixed seven faults ([docs/load_test.md](docs/load_test.md)). On 2026-11-18 the bottleneck the load test found (deleting containers, 1-3 s each, on every attempt's path) was moved to the background: 47 attempts a minute instead of 31.5 at 16 at once, 243 instead of 196 at 128 (D-043). The paid P4 scale run was dropped: this is a platform project and the numbers it would add were already measured (D-042).

Load test: `deploy/loadtest.sh ramp [--levels 16,128] [--label NAME]` and `deploy/loadtest.sh drill [NAME ...]` (no API key is present in that environment). Docker's own speed, without taskforge: `.venv/bin/python deploy/docker_probe.py`.

| Read | For |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, engine state rules, data model, invariants, what will be measured |
| [docs/decisions.md](docs/decisions.md) | Why each choice was made and what was rejected |
| [docs/pilot_report.md](docs/pilot_report.md) | The P3 pilot: funnel, costs, findings, incidents |

## Layout

```
taskforge/core/      platform: engine, sandbox, agent, gateway, store, tools, kb, review, obs
taskforge/taskgen/   the task-generation workflow and its agent roles
tests/
docs/
```

## Try it

One process, no database:

```bash
python -m taskforge.cli demo                                         # whole workflow, scripted models, fake sandbox
python -m taskforge.cli check-task tasks/handwritten/order-totals    # executed gates on a real task (needs Docker)
python -m taskforge.cli pairs --strong JOB_DIR --weak JOB_DIR ...    # import stored Harbor runs and pair them
```

The stack (on a Linux machine with Docker):

```bash
deploy/stack.sh up 2                                         # backing services + sandboxd + api + 2 worker processes
python -m taskforge.cli demo --stack http://127.0.0.1:58800  # a scripted run, reviews answered over HTTP
deploy/stack.sh status | down | env
```

Everything listens on 127.0.0.1. From a laptop, open a tunnel and use a browser:

```bash
ssh -N -L 58800:127.0.0.1:58800 -L 53000:127.0.0.1:53000 -L 56686:127.0.0.1:56686 user@vm
# console http://localhost:58800   dashboard http://localhost:53000/d/taskforge   traces http://localhost:56686
```

| Where | What |
|---|---|
| `taskforge/core/engine/postgres.py`, `worker.py` | Claim with a lease, fencing, single-transaction commit, joins, sweeper; the worker loop |
| `taskforge/core/engine/memory.py` | The same rules in one process, for tests |
| `taskforge/core/gateway/` | Idempotent model calls, circuit breaker, shared rate limit, budget guard, cost ledger, prompt registry |
| `taskforge/core/sandbox/` | `service.py` (sandboxd), `pool.py` (warm pool), `client.py`, `docker.py`, `fake.py` |
| `taskforge/core/store/`, `core/pg.py` | Content-addressed records with lineage, blob stores, the schema of every table |
| `taskforge/core/review/`, `taskforge/api/` | Review rules and queues; the console, control API and `/metrics` |
| `taskforge/core/tools/`, `core/kb/`, `core/obs/` | Tool registry and MCP server; knowledge base; tracing, logs, metrics |
| `taskforge/core/agent/loop.py` | The tool-calling loop used by every sandbox role |
| `taskforge/taskgen/` | The 28-step workflow, executed gates, shortcut attempts, agent roles, scripted demo world |
| `taskforge/runtime.py` | Builds engine + services on memory or Postgres from `TASKFORGE_*` settings |
| `deploy/` | Docker Compose file, `stack.sh`, Prometheus config, Grafana dashboard |

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,platform]"
python -m pytest -q                                           # 93 tests on the memory backend; the rest are skipped
TASKFORGE_PG_DSN=... TASKFORGE_DOCKER=1 python -m pytest -q   # on the VM: all 149 (memory + Postgres + Docker)
```

Tests that take the `db` fixture run twice, on memory and on Postgres, each Postgres test in its own schema.

Containers are not run on the development laptop; anything that needs Docker runs on the project VM.
