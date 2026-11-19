"""Client for sandboxd; implements the same Sandbox interface as FakeSandbox and DockerSandbox."""
from __future__ import annotations

import threading
import time
from typing import Mapping

import httpx

from .. import obs
from .base import BuildError, ExecResult, SandboxBusy, SandboxError, SandboxUnavailable, VerifyResult


class SandboxdClient:
    """While this process holds sandboxes, a background thread tells sandboxd every `keepalive_s` which
    ones; if the process dies, sandboxd frees them after its orphan time (pool.WarmPool.reap_orphans)."""

    def __init__(self, base_url: str, *, connect_timeout_s: float = 5.0, keepalive_s: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(60.0, connect=connect_timeout_s),
                                 limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))
        self.created = 0
        self.destroyed = 0
        self.pool_hits = 0
        self.start_latencies: list[float] = []
        self.keepalive_s = keepalive_s
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._keeper: threading.Thread | None = None

    def _keep_alive(self) -> None:
        while True:
            time.sleep(self.keepalive_s)
            with self._lock:
                ids = sorted(self._held)
            if ids:
                try:
                    self.http.post("/v1/sandboxes/keepalive", json={"ids": ids}, timeout=10)
                except httpx.HTTPError:
                    pass            # sandboxd restarting; the next round tries again

    def _hold(self, sid: str) -> None:
        with self._lock:
            self._held.add(sid)
            if self._keeper is None:
                self._keeper = threading.Thread(target=self._keep_alive, name="sandbox-keepalive", daemon=True)
                self._keeper.start()

    def _call(self, method: str, path: str, *, timeout: float = 60.0, **kw) -> dict:
        headers = {}
        span = obs.tracing.current()
        if span is not None:
            headers["traceparent"] = span.traceparent
        try:
            r = self.http.request(method, path, timeout=timeout, headers=headers, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # nothing reached sandboxd: wait for it without using up an attempt
            raise SandboxUnavailable(f"sandboxd unreachable: {type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            # the request was cut off mid-way (sandboxd died during it): the sandbox may be gone, so the step
            # starts over, using up an attempt
            raise SandboxError(f"sandboxd request failed: {type(e).__name__}: {e}") from e
        if r.status_code == 503:
            raise SandboxBusy(r.text or "sandbox capacity is full", delay_s=float(r.headers.get("Retry-After", 2)))
        if r.status_code == 507:      # disk full on the sandbox machine (P4 drill disk_full): wait for space
            raise SandboxUnavailable(r.text or "sandboxd disk full", delay_s=float(r.headers.get("Retry-After", 30)))
        if r.status_code == 422 and path == "/v1/images/build":
            raise BuildError(r.json().get("detail", r.text))
        if r.status_code == 404 and path.endswith("/file"):
            raise FileNotFoundError(r.json().get("detail", "file not found"))
        if r.status_code >= 400:
            raise SandboxError(f"sandboxd {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def build(self, files: Mapping[str, str]) -> str:
        with obs.tracing.span("sandbox.build"):
            return self._call("POST", "/v1/images/build", json={"files": dict(files)}, timeout=1500)["image"]

    def create(self, image: str, *, purpose: str) -> str:
        with obs.tracing.span("sandbox.create", {"purpose": purpose}):
            r = self._call("POST", "/v1/sandboxes", json={"image": image, "purpose": purpose}, timeout=180)
        self.created += 1
        self.pool_hits += bool(r["from_pool"])
        self.start_latencies.append(r["start_latency_ms"] / 1000)
        self._hold(r["id"])
        return r["id"]

    def exec(self, sid: str, command: str, *, timeout_s: float = 120, user: str = "agent") -> ExecResult:
        with obs.tracing.span("sandbox.exec", {"user": user}):
            r = self._call("POST", f"/v1/sandboxes/{sid}/exec", timeout=timeout_s + 60,
                           json={"command": command, "timeout_s": timeout_s, "user": user})
        return ExecResult(r["exit_code"], r["output"], r["timed_out"], r["truncated"])

    def write_files(self, sid: str, files: Mapping[str, str], *, user: str = "agent") -> dict[str, str]:
        return self._call("PUT", f"/v1/sandboxes/{sid}/files", json={"files": dict(files), "user": user})["errors"]

    def read_file(self, sid: str, path: str, *, user: str = "agent") -> str:
        return self._call("GET", f"/v1/sandboxes/{sid}/file", params={"path": path, "user": user})["content"]

    def snapshot_app(self, sid: str) -> str:
        return self._call("POST", f"/v1/sandboxes/{sid}/snapshot", timeout=360)["snapshot"]

    def destroy(self, sid: str) -> None:
        with self._lock:
            self._held.discard(sid)
        try:
            self._call("DELETE", f"/v1/sandboxes/{sid}")
        except (SandboxError, SandboxUnavailable):
            pass                    # sandboxd removes leftovers itself when their time limit passes
        self.destroyed += 1

    def verify(self, image: str, tests: Mapping[str, str], snapshot: str, *, timeout_s: float = 300) -> VerifyResult:
        with obs.tracing.span("sandbox.verify"):
            r = self._call("POST", "/v1/verify", timeout=timeout_s + 120,
                           json={"image": image, "tests": dict(tests), "snapshot": snapshot, "timeout_s": timeout_s})
        return VerifyResult(r["passed"], r["exit_code"], r["output"], r["timed_out"])

    def prewarm(self, image: str, n: int) -> None:
        try:
            self._call("POST", "/v1/pool/prewarm", json={"image": image, "n": n})
        except (SandboxError, SandboxUnavailable):
            pass                    # a hint only; never worth failing a step for

    def capacity(self) -> dict:
        return self._call("GET", "/v1/capacity")
