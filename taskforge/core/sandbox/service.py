"""sandboxd: the only process that controls Docker (DESIGN.md section 5).

HTTP + JSON. Workers talk to it through `SandboxdClient`; they never hold the Docker socket, so
something a model wrote that compromises a worker still cannot start containers or reach the host.
On start it removes any container left by an earlier sandboxd (their bookkeeping is gone); steps
that were using them fail with a retryable error and run again.
"""
from __future__ import annotations

import errno

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from .. import obs
from .base import BuildError, SandboxBusy, SandboxError
from .docker import DockerSandbox
from .pool import WarmPool

START_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5, 10)
EXEC_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


class BuildBody(BaseModel):
    files: dict[str, str]


class CreateBody(BaseModel):
    image: str
    purpose: str = "rollout"


class ExecBody(BaseModel):
    command: str
    timeout_s: float = 120
    user: str = "agent"


class FilesBody(BaseModel):
    files: dict[str, str]
    user: str = "agent"


class VerifyBody(BaseModel):
    image: str
    tests: dict[str, str]
    snapshot: str
    timeout_s: float = 300


class KeepaliveBody(BaseModel):
    ids: list[str]


class PrewarmBody(BaseModel):
    image: str
    n: int


def create_app(backend: DockerSandbox | None = None, *, max_containers: int | None = None) -> FastAPI:
    backend = backend or DockerSandbox(owner="sandboxd", snapshot_dir=os.environ.get("TASKFORGE_SNAPSHOT_DIR"))
    max_containers = max_containers or int(os.environ.get("TASKFORGE_MAX_SANDBOXES", "48"))
    state: dict[str, Any] = {}

    reg = CollectorRegistry()
    starts = Histogram("taskforge_sandbox_start_seconds", "Time to hand out a sandbox", ["source"], registry=reg,
                       buckets=START_BUCKETS)
    execs = Histogram("taskforge_sandbox_exec_seconds", "Time one command took", registry=reg, buckets=EXEC_BUCKETS)
    verifies = Counter("taskforge_sandbox_verify_total", "Gradings in a fresh container", ["result"], registry=reg)
    builds = Counter("taskforge_sandbox_build_total", "Image builds", ["result"], registry=reg)
    busy = Counter("taskforge_sandbox_busy_total", "Requests refused because every slot was taken", registry=reg)
    disk_full = Counter("taskforge_sandbox_disk_full_total", "Requests refused because the disk was full", registry=reg)
    gauges = {name: Gauge(f"taskforge_sandbox_{name}", help_, registry=reg) for name, help_ in (
        ("in_use", "Sandboxes handed out and not yet destroyed"), ("idle", "Pre-started sandboxes waiting"),
        ("creating", "Sandboxes being started"), ("removing", "Sandboxes released and not yet deleted"),
        ("capacity", "Maximum sandboxes"),
        ("demand", "Sandboxes the workflow said it will need soon"))}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = 256    # one thread per in-flight request
        backend.destroy_many(backend.list_owned())     # leftovers of an earlier sandboxd
        state["pool"] = WarmPool(backend, max_containers=max_containers,
                                 refill_threads=int(os.environ.get("TASKFORGE_POOL_REFILL_THREADS", "8")),
                                 remove_threads=int(os.environ.get("TASKFORGE_POOL_REMOVE_THREADS", "4")))
        yield
        state["pool"].close()

    app = FastAPI(title="taskforge sandboxd", lifespan=lifespan)

    def pool() -> WarmPool:
        return state["pool"]

    def traced(name: str, request: Request, **attrs):
        trace_id, parent = obs.tracing.parse_traceparent(request.headers.get("traceparent"))
        return obs.tracing.span(name, attrs, trace_id=trace_id, parent_span_id=parent)

    def known(sid: str) -> str:
        if not pool().owns(sid):
            raise HTTPException(404, f"unknown sandbox {sid}")
        return sid

    @app.exception_handler(SandboxBusy)
    async def _busy(request, exc):
        busy.inc()
        return Response(status_code=503, content=str(exc), headers={"Retry-After": "2"})

    @app.exception_handler(SandboxError)
    async def _fault(request, exc):
        return Response(status_code=502, content=str(exc))

    @app.exception_handler(OSError)
    async def _disk(request, exc):
        if exc.errno == errno.ENOSPC:          # the machine's disk is full: callers wait, nothing is their fault
            disk_full.inc()
            return Response(status_code=507, content=f"sandboxd disk full: {exc}", headers={"Retry-After": "30"})
        return Response(status_code=500, content=f"{type(exc).__name__}: {exc}")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, **pool().stats()}

    @app.get("/v1/capacity")
    def capacity():
        return pool().stats()

    @app.get("/metrics")
    def metrics():
        s = pool().stats()
        for name in ("in_use", "idle", "creating", "removing", "demand"):
            gauges[name].set(s[name])
        gauges["capacity"].set(s["max"])
        return Response(generate_latest(reg), media_type="text/plain; version=0.0.4")

    @app.post("/v1/images/build")
    def build(body: BuildBody, request: Request):
        with traced("sandbox build", request):
            try:
                image = backend.build(body.files)
            except BuildError as e:
                builds.labels("failed").inc()
                raise HTTPException(422, str(e)) from None
            builds.labels("ok").inc()
            return {"image": image}

    @app.post("/v1/sandboxes")
    def create(body: CreateBody, request: Request):
        with traced("sandbox create", request, purpose=body.purpose) as span:
            sid, from_pool, latency = pool().acquire(body.image, body.purpose)
            starts.labels("pool" if from_pool else "cold").observe(latency)
            span.set(from_pool=from_pool)
            return {"id": sid, "from_pool": from_pool, "start_latency_ms": round(latency * 1000, 2)}

    @app.post("/v1/sandboxes/{sid}/exec")
    def exec_(sid: str, body: ExecBody, request: Request):
        with traced("sandbox exec", request, user=body.user):
            with execs.time():
                r = backend.exec(known(sid), body.command, timeout_s=body.timeout_s, user=body.user)
            return {"exit_code": r.exit_code, "output": r.output, "timed_out": r.timed_out, "truncated": r.truncated}

    @app.put("/v1/sandboxes/{sid}/files")
    def write(sid: str, body: FilesBody):
        return {"errors": backend.write_files(known(sid), body.files, user=body.user)}

    @app.get("/v1/sandboxes/{sid}/file")
    def read(sid: str, path: str, user: str = "agent"):
        try:
            return {"content": backend.read_file(known(sid), path, user=user)}
        except FileNotFoundError as e:
            raise HTTPException(404, f"file not found: {e}") from None

    @app.post("/v1/sandboxes/{sid}/snapshot")
    def snapshot(sid: str):
        return {"snapshot": backend.snapshot_app(known(sid))}

    @app.delete("/v1/sandboxes/{sid}")
    def destroy(sid: str):
        if pool().owns(sid):
            pool().release(sid)
        return {"ok": True}

    @app.post("/v1/verify")
    def verify(body: VerifyBody, request: Request):
        with traced("sandbox verify", request) as span:
            sid, from_pool, latency = pool().acquire(body.image, "verify", tracked=False)
            starts.labels("pool" if from_pool else "cold").observe(latency)
            try:
                v = backend.verify(body.image, body.tests, body.snapshot, timeout_s=body.timeout_s, sid=sid)
            finally:
                pool().release(sid)
            verifies.labels("pass" if v.passed else "fail").inc()
            span.set(passed=v.passed)
            return {"passed": v.passed, "exit_code": v.exit_code, "output": v.output, "timed_out": v.timed_out}

    @app.post("/v1/sandboxes/keepalive")
    def keepalive(body: KeepaliveBody):
        return {"known": pool().touch(body.ids)}

    @app.post("/v1/pool/prewarm")
    def prewarm(body: PrewarmBody):
        pool().prewarm(body.image, body.n)
        return pool().stats()

    return app
