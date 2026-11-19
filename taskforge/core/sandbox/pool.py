"""A pool of pre-started containers (DESIGN.md section 5).

The workflow gives a demand hint: prewarm(image, n) means "about n sandboxes of this image will be
asked for soon". The pool remembers the whole number (up to `max_demand_per_image`) but keeps at most
`max_idle_per_image` of them started and waiting, so it tops itself up while the work runs: every
acquisition lowers the demand by one and, while demand is left, a refill thread starts the next one.
Once the expected work is done nothing more is started. (Before P4 the hint itself was cut to 32, so a
task copy with 100 attempts got 32 ready sandboxes and then only cold starts; load_test.md.)
A container is used once and destroyed, never reused. Idle ones that nobody claimed are removed after
`idle_ttl_s`.

`max_containers` caps everything this pool has started (in use + idle + being created + being removed).
When the cap is reached, acquire() raises SandboxBusy and the calling step retries later without using up
an attempt. Pre-started containers may fill the cap only up to `max_containers - reserve`, so idle
containers of one image never take the last slots a step of another image needs.

Removal happens in the background. release() returns at once and remover threads delete the returned
containers in batches of up to 50 per `docker rm -f`. Deleting one container takes ~1 s even on an idle
machine, ~3 s with 16 at once, but 30 in one command take 3.3 s (deploy/docker_probe.py, load_test.md).
Before P4 each attempt waited for two removals: its own container before grading, the grading one before
its result came back. A container waiting to be removed still counts against `max_containers`.

Holders keep their sandboxes alive: the client says every few seconds which ids it still holds (touch).
A sandbox in use that nobody has mentioned for `orphan_s` belonged to a process that died; it is removed
and its slot freed (P4 drill kill_worker: a killed worker's 16 sandboxes otherwise stayed "in use" until
their one-hour expiry). Sandboxes sandboxd holds itself during a request (grading) are not tracked.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .base import SandboxBusy
from .docker import DockerSandbox

log = logging.getLogger(__name__)


class WarmPool:
    def __init__(self, backend: DockerSandbox, *, max_containers: int = 48, max_demand_per_image: int = 1000,
                 max_idle_per_image: int = 8, reserve: int | None = None, idle_ttl_s: float = 300.0,
                 refill_threads: int = 4, remove_threads: int = 4, remove_batch: int = 50,
                 orphan_s: float = 120.0, clock=time.monotonic):
        self.backend = backend
        self.max_containers = max_containers
        self.reserve = max(1, max_containers // 8) if reserve is None else reserve
        self.remove_batch = remove_batch
        self.max_demand_per_image = max_demand_per_image
        self.max_idle_per_image = max_idle_per_image
        self.idle_ttl_s = idle_ttl_s
        self.orphan_s = orphan_s
        self.clock = clock
        self._seen: dict[str, float] = {}           # tracked sandbox in use -> when its holder last said so
        self._cv = threading.Condition()
        self._idle: dict[str, deque[tuple[str, float]]] = {}
        self._demand: dict[str, int] = {}
        self._creating: dict[str, int] = {}         # being started, for a caller or for the pool
        self._warming: dict[str, int] = {}          # the part of _creating that is for the pool
        self._in_use: set[str] = set()
        self._to_remove: deque[str] = deque()      # released, not yet handed to docker rm
        self._removing = 0                          # released and not yet gone (queued + being removed)
        self._closed = False
        self.counts = {"pool_hits": 0, "cold_starts": 0, "busy": 0, "prewarmed": 0, "idle_expired": 0,
                       "orphaned": 0, "removed": 0, "remove_batches": 0}
        self._threads = [threading.Thread(target=self._refill_loop, name=f"pool-refill-{i}", daemon=True)
                         for i in range(refill_threads)]
        self._threads += [threading.Thread(target=self._remove_loop, name=f"pool-remove-{i}", daemon=True)
                          for i in range(remove_threads)]
        self._threads.append(threading.Thread(target=self._reap_loop, name="pool-reap", daemon=True))
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------- state
    def _total(self) -> int:
        return (len(self._in_use) + sum(len(q) for q in self._idle.values()) + sum(self._creating.values())
                + self._removing)

    def stats(self) -> dict:
        with self._cv:
            return {"max": self.max_containers, "in_use": len(self._in_use),
                    "idle": sum(len(q) for q in self._idle.values()), "creating": sum(self._creating.values()),
                    "removing": self._removing, "demand": sum(self._demand.values()), **self.counts}

    def owns(self, sid: str) -> bool:
        with self._cv:
            return sid in self._in_use

    # ------------------------------------------------------------------ api
    def prewarm(self, image: str, n: int) -> None:
        with self._cv:
            self._demand[image] = min(self.max_demand_per_image, self._demand.get(image, 0) + max(0, n))
            self._cv.notify_all()

    def acquire(self, image: str, purpose: str, *, tracked: bool = True) -> tuple[str, bool, float]:
        """Returns (sandbox id, came from the pool?, seconds it took). tracked=False: sandboxd itself holds
        it for the length of one request, so no keep-alive is expected."""
        started = time.monotonic()
        with self._cv:
            self._demand[image] = max(0, self._demand.get(image, 0) - 1)
            queue = self._idle.get(image)
            if queue:
                sid, _ = queue.popleft()
                self._in_use.add(sid)
                if tracked:
                    self._seen[sid] = self.clock()
                self.counts["pool_hits"] += 1
                self._cv.notify_all()
                return sid, True, time.monotonic() - started
            if self._total() >= self.max_containers:
                self.counts["busy"] += 1
                raise SandboxBusy(f"all {self.max_containers} sandbox slots are taken")
            self._creating[image] = self._creating.get(image, 0) + 1
        try:
            sid = self.backend.create(image, purpose=purpose)
        except BaseException:
            with self._cv:
                self._creating[image] -= 1
            raise
        with self._cv:
            self._creating[image] -= 1
            self._in_use.add(sid)
            if tracked:
                self._seen[sid] = self.clock()
            self.counts["cold_starts"] += 1
        return sid, False, time.monotonic() - started

    def touch(self, ids) -> int:
        """The holder still has these. Returns how many of them this pool knows."""
        now, known = self.clock(), 0
        with self._cv:
            for sid in ids:
                if sid in self._seen:
                    self._seen[sid] = now
                    known += 1
        return known

    def release(self, sid: str) -> None:
        """Hand the sandbox back; a remover thread deletes it soon after."""
        with self._cv:
            if sid not in self._in_use:
                return
            self._in_use.discard(sid)
            self._seen.pop(sid, None)
            if self._closed:
                queued = False
            else:
                self._to_remove.append(sid)
                self._removing += 1
                queued = True
            self._cv.notify_all()
        if not queued:
            self.backend.destroy(sid)

    def flush(self, timeout_s: float = 60.0) -> bool:
        """Wait until every released sandbox is gone (tests, shutdown). True if it got there in time."""
        with self._cv:
            return self._cv.wait_for(lambda: self._removing == 0, timeout=timeout_s)

    def _destroy_batch(self, sids: list[str]) -> None:
        destroy_many = getattr(self.backend, "destroy_many", None)
        if destroy_many is not None:
            destroy_many(sids)
        else:
            for sid in sids:
                self.backend.destroy(sid)

    def _remove_loop(self) -> None:
        while True:
            with self._cv:
                while not self._to_remove and not self._closed:
                    self._cv.wait(timeout=5.0)
                if not self._to_remove:
                    return                                  # closed and nothing left
                batch = [self._to_remove.popleft() for _ in range(min(self.remove_batch, len(self._to_remove)))]
            try:
                self._destroy_batch(batch)
            except Exception:  # noqa: BLE001 - the expiry sweep removes whatever is left
                log.exception("removing %d sandboxes failed", len(batch))
            with self._cv:
                self._removing -= len(batch)
                self.counts["removed"] += len(batch)
                self.counts["remove_batches"] += 1
                self._cv.notify_all()

    def reap_orphans(self) -> int:
        """Remove tracked sandboxes whose holder has not mentioned them for orphan_s."""
        cutoff = self.clock() - self.orphan_s
        with self._cv:
            orphans = [sid for sid, seen in self._seen.items() if seen < cutoff]
            for sid in orphans:
                self._in_use.discard(sid)
                del self._seen[sid]
            self.counts["orphaned"] += len(orphans)
            if orphans:
                self._cv.notify_all()
        for sid in orphans:
            log.warning("sandbox %s: its holder stopped keeping it alive; removed", sid)
            self.backend.destroy(sid)
        return len(orphans)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            leftovers = [sid for q in self._idle.values() for sid, _ in q] + list(self._in_use) + list(self._to_remove)
            self._removing -= len(self._to_remove)
            self._to_remove.clear()
            self._idle.clear()
            self._in_use.clear()
            self._cv.notify_all()
        self._destroy_batch(leftovers)
        self.flush(timeout_s=120)                      # batches a remover thread already started

    # -------------------------------------------------------------- threads
    def _next_to_fill(self) -> str | None:
        if self._total() >= self.max_containers - self.reserve:
            return None
        for image, demand in self._demand.items():
            if len(self._idle.get(image, ())) + self._warming.get(image, 0) < min(demand, self.max_idle_per_image):
                return image
        return None

    def _refill_loop(self) -> None:
        while True:
            with self._cv:
                while not self._closed and (image := self._next_to_fill()) is None:
                    self._cv.wait(timeout=5.0)
                if self._closed:
                    return
                self._creating[image] = self._creating.get(image, 0) + 1
                self._warming[image] = self._warming.get(image, 0) + 1
            try:
                sid = self.backend.create(image, purpose="pool")
            except Exception:  # noqa: BLE001 - e.g. the image was removed; forget the demand for it
                log.exception("prewarm of %s failed", image)
                with self._cv:
                    self._creating[image] -= 1
                    self._warming[image] -= 1
                    self._demand[image] = 0
                continue
            with self._cv:
                self._creating[image] -= 1
                self._warming[image] -= 1
                if self._closed:
                    pass
                else:
                    self._idle.setdefault(image, deque()).append((sid, time.monotonic()))
                    self.counts["prewarmed"] += 1
                    sid = None
            if sid is not None:
                self.backend.destroy(sid)

    def reconcile(self) -> int:
        """Forget sandboxes that are counted as in use but whose container no longer exists - for example
        when the step holding one was killed and the expiry sweep removed the container. Returns how many."""
        with self._cv:
            before = set(self._in_use)                 # only ids created before the listing below
        alive = set(self.backend.list_owned())
        with self._cv:
            stale = (before - alive) & self._in_use
            self._in_use -= stale
            for sid in stale:
                self._seen.pop(sid, None)
            if stale:
                self._cv.notify_all()
        return len(stale)

    def _reap_loop(self) -> None:
        while True:
            with self._cv:
                if self._cv.wait_for(lambda: self._closed, timeout=min(30.0, self.idle_ttl_s / 2)):
                    return
                cutoff, expired = time.monotonic() - self.idle_ttl_s, []
                for image, queue in self._idle.items():
                    while queue and queue[0][1] < cutoff:
                        expired.append(queue.popleft()[0])
                    if not queue:
                        self._demand[image] = 0          # nobody came for them: stop refilling
                self.counts["idle_expired"] += len(expired)
            for sid in expired:
                self.backend.destroy(sid)
            try:
                self.reap_orphans()
                self.backend.sweep_expired()
                self.backend.sweep_snapshots()
                self.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")
