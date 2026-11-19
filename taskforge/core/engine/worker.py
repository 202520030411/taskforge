"""A worker process: threads that claim and run steps, one that keeps leases alive, one that sweeps
expired leases (DESIGN.md sections 4.4 and 4.8).

`concurrency` maps a resource class to the number of threads serving it, e.g. {"llm": 16, "sandbox": 8}.
The sum across all worker processes is the cap for that class.
"""
from __future__ import annotations

import logging
import random
import threading
import time

from .postgres import PgEngine

log = logging.getLogger(__name__)
DEFAULT_CONCURRENCY = {"cpu": 4, "human": 2, "llm": 8, "sandbox": 8, "agent": 8}


class Worker:
    def __init__(self, engine: PgEngine, concurrency: dict[str, int] | None = None, *, poll_s: float = 0.2,
                 heartbeat_s: float | None = None, sweep_s: float = 2.0):
        self.engine = engine
        self.concurrency = dict(concurrency or DEFAULT_CONCURRENCY)
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else engine.lease_ttl_s / 3
        self.sweep_s = sweep_s
        self._stop = threading.Event()
        self._active: dict[str, int] = {}          # step run id -> lease token, for the heartbeat
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.executed = 0

    def start(self) -> "Worker":
        for cls, n in self.concurrency.items():
            for i in range(n):
                self._spawn(self._work_loop, f"{cls}-{i}", cls)
        self._spawn(self._heartbeat_loop, "heartbeat")
        self._spawn(self._sweep_loop, "sweep")
        return self

    def _spawn(self, target, name: str, *args) -> None:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, timeout: float = 30.0) -> None:
        """Stop claiming new steps and wait for running ones to finish."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def _work_loop(self, cls: str) -> None:
        idle = 0
        while not self._stop.is_set():
            try:
                row = self.engine.claim([cls])
            except Exception:  # noqa: BLE001 - e.g. the database is restarting: wait and try again
                log.exception("claim failed")
                self._stop.wait(2.0)
                continue
            if row is None:
                idle = min(idle + 1, 5)
                self._stop.wait(self.poll_s * idle * (0.5 + random.random()))   # back off while the queue is empty
                continue
            idle = 0
            with self._lock:
                self._active[row["id"]] = row["lease_token"]
            try:
                self.engine.execute(row)
            except Exception:  # noqa: BLE001 - losing the database mid-commit: the lease will expire
                log.exception("execute failed for %s", row["id"])
            finally:
                with self._lock:
                    self._active.pop(row["id"], None)
                    self.executed += 1

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            with self._lock:
                active = list(self._active.items())
            for step_run_id, token in active:
                try:
                    self.engine.heartbeat(step_run_id, token)
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat failed for %s", step_run_id)

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.sweep_s * (0.5 + random.random())):
            try:
                self.engine.sweep()
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")
