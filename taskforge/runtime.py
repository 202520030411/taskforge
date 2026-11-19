"""Assemble a platform: engine + services, on memory (one process) or Postgres (many processes).

`Settings.from_env()` reads TASKFORGE_* variables; `build_from_settings()` is what every long-running
process (worker, api) and the CLI call, so they all see the same database, blob store and sandbox.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .core.engine import InMemoryEngine
from .core.gateway import Gateway, MemoryGatewayState, Provider
from .core.kb import MemoryKB, PgKB
from .core.review import PgReviewService, ReviewService
from .core.sandbox import Sandbox
from .core.store import FsBlobStore, MemoryBlobStore, MemoryStore, S3BlobStore
from .taskgen.config import TaskgenConfig
from .taskgen.contamination import BenchmarkIndex
from .taskgen.policy import POLICY
from .taskgen.workflow import WORKFLOW, Services

GatewayFactory = Callable[..., Gateway]


@dataclass
class Platform:
    engine: Any
    svc: Services
    db: Any = None

    def close(self) -> None:
        if self.db is not None:
            self.db.close()


def _default_gateway(providers, store, **kw) -> Gateway:
    return Gateway(providers, **kw)


def build_memory(*, providers: Mapping[str, Provider], sandbox: Sandbox, benchmark: BenchmarkIndex,
                 config: TaskgenConfig | None = None, gateway_factory: GatewayFactory = _default_gateway,
                 **gateway_kw) -> Platform:
    store, kb = MemoryStore(), MemoryKB()
    gateway = gateway_factory(providers, store, state=MemoryGatewayState(), **gateway_kw)
    reviews = ReviewService(POLICY, store)
    svc = Services(store=store, gateway=gateway, sandbox=sandbox, reviews=reviews, benchmark=benchmark,
                   config=config or TaskgenConfig(), kb=kb)
    engine = InMemoryEngine(WORKFLOW, svc)
    gateway.bind_budget(engine)
    reviews.bind_signal(engine.signal)
    kb.bind_watermark(lambda: engine.last_seq)
    return Platform(engine, svc)


def build_pg(db, *, providers: Mapping[str, Provider], sandbox: Sandbox, benchmark: BenchmarkIndex,
             blobs=None, config: TaskgenConfig | None = None, gateway_factory: GatewayFactory = _default_gateway,
             lease_ttl_s: float = 60.0, worker_id: str | None = None, **gateway_kw) -> Platform:
    from .core.engine.postgres import PgEngine
    from .core.gateway import PgGatewayState
    from .core.store.postgres import PgStore
    store = PgStore(db, blobs or MemoryBlobStore())
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    gateway = gateway_factory(providers, store, state=PgGatewayState(db), owner=worker_id, **gateway_kw)
    reviews = PgReviewService(POLICY, store, db)
    svc = Services(store=store, gateway=gateway, sandbox=sandbox, reviews=reviews, benchmark=benchmark,
                   config=config or TaskgenConfig(), kb=PgKB(db))
    engine = PgEngine(WORKFLOW, svc, db, lease_ttl_s=lease_ttl_s, worker_id=worker_id)
    gateway.bind_budget(engine)
    reviews.bind_signal(engine.signal)
    return Platform(engine, svc, db)


# ------------------------------------------------------------------ settings
@dataclass
class Settings:
    pg_dsn: str | None = None
    pg_schema: str = "public"
    blobs: str = "memory"                 # memory | fs:/path | s3://bucket@host:port
    s3_key: str = ""
    s3_secret: str = ""
    sandbox: str = "fake"                 # fake | docker | http://host:port (sandboxd)
    otlp: str | None = None               # OTLP/HTTP endpoint for traces, e.g. http://127.0.0.1:54318
    profile: str = "demo"                 # which models answer: "demo" = scripted fakes, no paid calls
    world: dict = field(default_factory=dict)
    lease_ttl_s: float = 60.0
    pool_size: int = 24
    caps: dict = field(default_factory=dict)          # dollars per run per provider, e.g. {"anthropic": 30}
    benchmark_dir: str = "/root/.cache/harbor/tasks/packages/terminal-bench"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(pg_dsn=e.get("TASKFORGE_PG_DSN") or None, pg_schema=e.get("TASKFORGE_PG_SCHEMA", "public"),
                   blobs=e.get("TASKFORGE_BLOBS", "memory"), s3_key=e.get("TASKFORGE_S3_KEY", ""),
                   s3_secret=e.get("TASKFORGE_S3_SECRET", ""), sandbox=e.get("TASKFORGE_SANDBOX", "fake"),
                   otlp=e.get("TASKFORGE_OTLP") or None, profile=e.get("TASKFORGE_PROFILE", "demo"),
                   world=json.loads(e.get("TASKFORGE_WORLD") or "{}"),
                   lease_ttl_s=float(e.get("TASKFORGE_LEASE_TTL", "60")),
                   pool_size=int(e.get("TASKFORGE_DB_POOL", "24")),
                   caps={k: float(v) for k, _, v in (p.partition("=") for p in e.get("TASKFORGE_CAPS", "").split(",") if p)},
                   benchmark_dir=e.get("TASKFORGE_BENCHMARK_DIR", cls.benchmark_dir))


def make_blobs(settings: Settings):
    spec = settings.blobs
    if spec == "memory":
        return MemoryBlobStore()
    if spec.startswith("fs:"):
        return FsBlobStore(Path(spec[3:]))
    if spec.startswith("s3://"):
        bucket, _, endpoint = spec[5:].partition("@")
        return S3BlobStore(endpoint, settings.s3_key, settings.s3_secret, bucket=bucket)
    raise ValueError(f"unknown blob store {spec!r}")


def make_sandbox(settings: Settings) -> Sandbox:
    from .core.sandbox import DockerSandbox, FakeSandbox
    if settings.sandbox == "fake":
        return FakeSandbox()
    if settings.sandbox == "docker":
        return DockerSandbox()
    if settings.sandbox.startswith("http"):
        from .core.sandbox.client import SandboxdClient
        return SandboxdClient(settings.sandbox)
    raise ValueError(f"unknown sandbox {settings.sandbox!r}")


def make_database(settings: Settings):
    from .core.pg import Database
    if not settings.pg_dsn:
        raise SystemExit("TASKFORGE_PG_DSN is not set (run `deploy/stack.sh env` to see the stack's settings)")
    return Database(settings.pg_dsn, schema=settings.pg_schema, max_size=settings.pool_size)


def real_providers(env: Mapping[str, str] | None = None) -> dict[str, Provider]:
    from .core.gateway.anthropic import AnthropicProvider
    from .core.gateway.openrouter import OpenRouterProvider
    from .taskgen.config import BEDROCK, HOST_PRICES
    e = os.environ if env is None else env
    missing = [k for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY") if not e.get(k)]
    if missing:
        raise SystemExit(f"missing {missing}: the pilot profile calls paid models")
    host = e.get("TASKFORGE_QWEN_HOST", BEDROCK)
    if host not in HOST_PRICES:
        raise SystemExit(f"unknown Qwen host {host!r}; known: {sorted(HOST_PRICES)}")
    price_in, price_out = HOST_PRICES[host]
    return {"anthropic": AnthropicProvider(e["ANTHROPIC_API_KEY"]),
            "openrouter": OpenRouterProvider(e["OPENROUTER_API_KEY"], host=host, price_in=price_in, price_out=price_out)}


def build_from_settings(settings: Settings, *, service: str = "taskforge") -> Platform:
    """The platform every process of the stack shares.
      demo   scripted models, no paid calls
      pilot  Claude Opus 5.5 + Qwen3-Coder on SiliconFlow; paid steps still need a run with paid approval"""
    from .core import obs
    from .taskgen import demo
    obs.tracing.configure(service, settings.otlp)
    if settings.profile == "pilot":
        from .core.gateway import RateLimit
        from .taskgen.config import PILOT
        if not settings.caps:
            raise SystemExit("the pilot profile needs TASKFORGE_CAPS, e.g. anthropic=30,openrouter=15")
        return build_pg(make_database(settings), providers=real_providers(), sandbox=make_sandbox(settings),
                        benchmark=BenchmarkIndex.from_dir(Path(settings.benchmark_dir), "*/*/instruction.md"),
                        blobs=make_blobs(settings), config=PILOT, lease_ttl_s=settings.lease_ttl_s,
                        provider_caps=settings.caps,
                        rate_limits={"anthropic": RateLimit(capacity=10, refill_per_s=1.0),
                                     "openrouter": RateLimit(capacity=20, refill_per_s=3.0)})
    if settings.profile == "loadtest":
        # P4 load test and failure drills: fake models with API-like delays, real everything else
        from .core.gateway.fake import FakeProvider
        from .taskgen.loadtest import FaultFile, load_script
        lo, _, hi = os.environ.get("TASKFORGE_LOAD_LATENCY", "0.5,3.0").partition(",")
        provider = FakeProvider(load_script(int(os.environ.get("TASKFORGE_LOAD_TURNS", "10"))), cost_per_call=0.0001,
                                latency_s=(float(lo), float(hi)), faults=FaultFile(os.environ.get("TASKFORGE_FAULTS")),
                                record=False)
        return build_pg(make_database(settings), providers={"anthropic": provider, "openrouter": provider},
                        sandbox=make_sandbox(settings), benchmark=BenchmarkIndex(demo.BENCHMARK_TEXTS),
                        blobs=make_blobs(settings), config=TaskgenConfig(solver_max_steps=20),
                        lease_ttl_s=settings.lease_ttl_s)
    if settings.profile != "demo":
        raise SystemExit(f"unknown profile {settings.profile!r}; use demo, pilot or loadtest")
    world = demo.WorldConfig(**{k: tuple(v) if isinstance(v, list) else v for k, v in settings.world.items()})
    sandbox = make_sandbox(settings)
    kit = demo.FakeKit(world) if settings.sandbox == "fake" else demo.OrderTotalsKit(world)
    provider = demo.FakeProvider(demo.make_script(world, kit), cost_per_call=0.01)
    return build_pg(make_database(settings), providers={"anthropic": provider, "openrouter": provider},
                    sandbox=sandbox, benchmark=BenchmarkIndex(demo.BENCHMARK_TEXTS), blobs=make_blobs(settings),
                    gateway_factory=demo.tagging_gateway, lease_ttl_s=settings.lease_ttl_s)
