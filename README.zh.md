# TaskForge

[English](README.md) | **中文**

一个小型的数据生产平台。上面跑的第一个 workflow 是给 coding agent 造题，从模型的失败出发：先从强/弱模型的 trajectory 对里找出弱模型反复犯的错（bad pattern），再针对每个 bad pattern 生成新的终端任务；一道题要过完一串真的跑代码的 gate（oracle、初始状态、hack、leak、难度）和人工 review，才会被收下。

**进度：** P3 pilot 已完成（2026-11-17）。这是第一次用真模型跑：所有角色用 Claude Opus 5.5，被测的弱模型是 Qwen3-Coder-30B。10 条 proposal 收下 6 道题，共花 $27.09（[docs/pilot_report.md](docs/pilot_report.md)）。同一天用假模型做完了 P4 的压测和故障演练：一台 16 核 VM 上同时跑 128 个 attempt；7 个演练找到并修好了 7 个问题（[docs/load_test.md](docs/load_test.md)）。2026-11-18 把压测发现的瓶颈挪到了后台：删容器每个要 1–3 秒，原来每个 attempt 都要等它。改完后，16 个同时跑时每分钟 47 个 attempt（原来 31.5），128 个时 243 个（原来 196）（D-043）。P4 原计划的付费大规模 run 取消了：这是平台项目，那次 run 要补的数字都已经测到了（D-042）。

压测：`deploy/loadtest.sh ramp [--levels 16,128] [--label NAME]` 和 `deploy/loadtest.sh drill [NAME ...]`（这个环境里没有任何 API key）。不经过 taskforge、只测 Docker 本身的速度：`.venv/bin/python deploy/docker_probe.py`。

下面这些文档都是英文的。

| 文档 | 内容 |
|---|---|
| [DESIGN.md](DESIGN.md) | 架构、引擎的状态规则、数据模型、invariant、要测哪些数字 |
| [docs/decisions.md](docs/decisions.md) | 每个选择的理由，以及放弃了哪些方案 |
| [docs/pilot_report.md](docs/pilot_report.md) | P3 pilot：漏斗、花费、发现的问题、出过的事故 |

## 目录

```
taskforge/core/      平台：engine、sandbox、agent、gateway、store、tools、kb、review、obs
taskforge/taskgen/   造题 workflow 和它的各个 agent 角色
tests/
docs/
```

## 试一下

单进程，不需要数据库：

```bash
python -m taskforge.cli demo                                         # 整个 workflow：按剧本回答的假模型，假 sandbox
python -m taskforge.cli check-task tasks/handwritten/order-totals    # 在一道真题上跑这些 gate（需要 Docker）
python -m taskforge.cli pairs --strong JOB_DIR --weak JOB_DIR ...    # 导入存好的 Harbor run，配成强/弱对
```

整套服务（在装了 Docker 的 Linux 机器上）：

```bash
deploy/stack.sh up 2                                         # 后台服务 + sandboxd + api + 2 个 worker 进程
python -m taskforge.cli demo --stack http://127.0.0.1:58800  # 跑一次按剧本的 run，review 通过 HTTP 自动作答
deploy/stack.sh status | down | env
```

所有服务只监听 127.0.0.1。在笔记本上开一个 SSH 隧道，再用浏览器打开：

```bash
ssh -N -L 58800:127.0.0.1:58800 -L 53000:127.0.0.1:53000 -L 56686:127.0.0.1:56686 user@vm
# console http://localhost:58800   看板 http://localhost:53000/d/taskforge   trace http://localhost:56686
```

| 位置 | 内容 |
|---|---|
| `taskforge/core/engine/postgres.py`、`worker.py` | 带 lease 的领取、fencing、单事务提交、join、清扫过期 lease；worker 主循环 |
| `taskforge/core/engine/memory.py` | 同一套规则的单进程版本，给测试用 |
| `taskforge/core/gateway/` | 幂等的模型调用、熔断、共享限流、预算检查、花费记录、prompt 版本管理 |
| `taskforge/core/sandbox/` | `service.py`（sandboxd）、`pool.py`（warm pool）、`client.py`、`docker.py`、`fake.py` |
| `taskforge/core/store/`、`core/pg.py` | 按内容 hash 命名、记着来源的记录；blob 存储；所有表的 schema |
| `taskforge/core/review/`、`taskforge/api/` | review 规则和队列；网页 console、控制 API 和 `/metrics` |
| `taskforge/core/tools/`、`core/kb/`、`core/obs/` | tool registry 和 MCP server；知识库；trace、日志、指标 |
| `taskforge/core/agent/loop.py` | 所有在 sandbox 里干活的角色共用的 tool calling 循环 |
| `taskforge/taskgen/` | 28 个 step 的 workflow、跑代码的 gate、钻空子的尝试、各 agent 角色、按剧本的演示环境 |
| `taskforge/runtime.py` | 按 `TASKFORGE_*` 配置，在内存或 Postgres 上搭好引擎和各项服务 |
| `deploy/` | Docker Compose 文件、`stack.sh`、Prometheus 配置、Grafana 看板 |

## 开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,platform]"
python -m pytest -q                                           # 内存后端上的 93 个测试；其余跳过
TASKFORGE_PG_DSN=... TASKFORGE_DOCKER=1 python -m pytest -q   # 在 VM 上：全部 149 个（内存 + Postgres + Docker）
```

带 `db` fixture 的测试会跑两遍，内存一遍、Postgres 一遍，每个 Postgres 测试用自己的 schema。

开发用的笔记本上不跑容器；需要 Docker 的都在项目的 VM 上跑。
