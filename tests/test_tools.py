"""Tool registry, the MCP server built from it, the knowledge base, and gateway protections (no database needed)."""
import threading
import time

import anyio
import pytest

from taskforge.core.errors import Retryable
from taskforge.core.gateway import (CircuitBreaker, FakeProvider, Gateway, ModelRequest, ProviderError, RateLimit)
from taskforge.core.kb import MemoryKB
from taskforge.core.sandbox import FakeSandbox, SandboxError
from taskforge.core.scope import CallScope
from taskforge.core.tools import DEFAULT_REGISTRY, SOLVER_TOOLS, ToolContext


def box():
    sb = FakeSandbox()
    return sb, sb.create(sb.build({"environment/app/a.txt": "hello"}), purpose="rollout")


def test_registry_runs_tools_and_reports_failures_as_text():
    sb, sid = box()
    ctx = ToolContext(sandbox=sb, sid=sid)
    names = [t.name for t in SOLVER_TOOLS]
    assert "hello" in DEFAULT_REGISTRY.call("run_shell", {"command": "cat /app/a.txt"}, ctx, allowed=names)
    assert DEFAULT_REGISTRY.call("read_file", {"path": "/app/none"}, ctx, allowed=names).startswith("error: FileNotFoundError")
    assert DEFAULT_REGISTRY.call("write_file", {"path": "/app/b"}, ctx, allowed=names) == "error: missing argument 'content'"
    assert "unknown tool" in DEFAULT_REGISTRY.call("kb_search", {"kind": "x", "query": "y"}, ctx, allowed=names)   # not offered
    assert "unknown tool" in DEFAULT_REGISTRY.call("rm_rf", {}, ctx)

    class Dead(FakeSandbox):
        def exec(self, *a, **k):
            raise SandboxError("container died")
    with pytest.raises(SandboxError):                      # infrastructure faults are not turned into text
        DEFAULT_REGISTRY.call("run_shell", {"command": "ls"}, ToolContext(sandbox=Dead(), sid="x"))


def test_mcp_server_serves_the_same_tools():
    from mcp import Client
    from taskforge.core.tools.mcp import build_server
    sb, sid = box()

    async def main():
        async with Client(build_server(ToolContext(sandbox=sb, sid=sid))) as client:
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == ["run_shell", "read_file", "write_file"]
            assert tools[0].input_schema["required"] == ["command"]
            wrote = await client.call_tool("write_file", {"path": "/app/note.txt", "content": "via mcp"})
            assert wrote.content[0].text.startswith("wrote /app/note.txt") and not wrote.is_error
            read = await client.call_tool("run_shell", {"command": "cat /app/note.txt"})
            assert "via mcp" in read.content[0].text
            denied = await client.call_tool("write_file", {"path": "/etc/passwd", "content": "x"})
            assert denied.is_error and "Permission denied" in denied.content[0].text
            hidden = await client.call_tool("finish", {})
            assert hidden.is_error                           # not exposed over MCP
    anyio.run(main)
    assert sb.boxes[sid]["/app/note.txt"] == "via mcp"


def test_kb_search_ranks_by_shared_words_and_hides_newer_documents():
    seq = {"now": 0}
    kb = MemoryKB(watermark=lambda: seq["now"])
    kb.add("proposal", "Summarise order totals per customer from a CSV export with refunds")
    seq["now"] = 5
    kb.add("proposal", "Rotate web server logs daily and compress them")
    kb.add("repair_example", "tests too strict: relaxed exact whitespace match in the CSV totals test")
    hits = kb.search("proposal", "customer order totals CSV")
    assert [h.text.split()[0] for h in hits] == ["Summarise"]
    assert kb.search(["proposal", "repair_example"], "CSV totals", k=5)[0].kind in ("proposal", "repair_example")
    assert len(kb.search(["proposal", "repair_example"], "CSV totals")) == 2
    # a step created when the newest step was #3 does not see documents added at watermark 5
    assert [h.text.split()[0] for h in kb.search("proposal", "logs daily compress customer", before_seq=3)] == ["Summarise"]
    assert kb.add("proposal", "Rotate web server logs daily and compress them") and kb.count("proposal") == 2   # same text, same doc


class Budget:
    def reserve(self, scope, est): return None
    def settle(self, token, actual): pass


def req():
    return ModelRequest(model="fake/m", messages=())


def test_quick_retries_then_circuit_breaker_then_recovery():
    now = {"t": 0.0}
    state = {"fail": True, "calls": 0}

    def script(role, request):
        state["calls"] += 1
        if state["fail"]:
            raise ProviderError("overloaded", retryable=True)
        return "ok"
    breaker = CircuitBreaker(threshold=4, cooldown_s=30, clock=lambda: now["t"])
    gw = Gateway({"fake": FakeProvider(script)}, budget=Budget(), breaker=breaker, max_tries=3, retry_base_s=0,
                 sleep=lambda s: None)
    with pytest.raises(Retryable):                           # 3 quick tries, all fail
        gw.complete(CallScope("r", "s", "a", 1), "role", req())
    assert state["calls"] == 3 and breaker.state("fake") == "closed"
    with pytest.raises(Retryable):                           # 4th failure opens the circuit; no 5th and 6th try
        gw.complete(CallScope("r", "s", "a", 2), "role", req())
    assert state["calls"] == 4 and breaker.state("fake") == "open"
    with pytest.raises(Retryable) as e:                      # while open: refused without calling the provider
        gw.complete(CallScope("r", "s", "a", 3), "role", req())
    assert state["calls"] == 4 and e.value.consume_attempt is False and e.value.delay_s == pytest.approx(30)

    now["t"] = 31.0                                          # cooling-off over: one probe is let through
    state["fail"] = False
    assert gw.complete(CallScope("r", "s", "a", 4), "role", req()).text == "ok"
    assert breaker.state("fake") == "closed" and state["calls"] == 5


def test_bad_request_is_not_retried_and_does_not_trip_the_breaker():
    calls = []

    def script(role, request):
        calls.append(1)
        raise ProviderError("invalid request", retryable=False)
    gw = Gateway({"fake": FakeProvider(script)}, budget=Budget(), breaker=CircuitBreaker(threshold=1), sleep=lambda s: None)
    with pytest.raises(ProviderError):
        gw.complete(CallScope("r", "s", "a", 1), "role", req())
    assert len(calls) == 1 and gw.breaker.state("fake") == "closed"


def test_rate_limit_waits_then_gives_the_step_back_without_burning_an_attempt():
    slept = []
    gw = Gateway({"fake": FakeProvider(lambda r, q: "x")}, budget=Budget(),
                 rate_limits={"fake": RateLimit(capacity=2, refill_per_s=0.001)}, max_rate_wait_s=5, sleep=slept.append)
    gw.complete(CallScope("r", "s", "a", 1), "role", req())
    gw.complete(CallScope("r", "s", "a", 2), "role", req())
    with pytest.raises(Retryable) as e:                      # the bucket is empty and refills in ~1000 s
        gw.complete(CallScope("r", "s", "a", 3), "role", req())
    assert e.value.consume_attempt is False and slept == []


def test_zero_bytes_in_command_output_never_reach_storage():
    from taskforge.core.agent import run_agent
    from taskforge.core.gateway import FakeProvider, tool_reply, turn_number
    from taskforge.core.sandbox import ExecResult
    sb = FakeSandbox(programs={"od": lambda fs, argv: ExecResult(0, "bin\x00ary\x00")})
    sid = sb.create(sb.build({}), purpose="rollout")
    gw = Gateway({"fake": FakeProvider(lambda r, q: [tool_reply(("run_shell", {"command": "od x"})),
                                                     tool_reply(("finish", {}))][turn_number(q)])}, budget=Budget())
    n = iter(range(1, 9))
    res = run_agent(gateway=gw, next_scope=lambda: CallScope("r", "s", "k", next(n)), role="w", model="fake/m",
                    system="", task_prompt="x", sandbox=sb, sid=sid)
    assert "\x00" not in res.steps[1].observation and "bin\\0ary\\0" in res.steps[1].observation


def test_warm_pool_forgets_sandboxes_whose_container_is_gone():
    from taskforge.core.sandbox.pool import WarmPool

    class Backend:
        def __init__(self): self.alive, self.n = set(), 0
        def create(self, image, purpose):
            self.n += 1
            sid = f"c{self.n}"
            self.alive.add(sid)
            return sid
        def destroy(self, sid): self.alive.discard(sid)
        def list_owned(self): return sorted(self.alive)
        def sweep_expired(self): return 0
        def sweep_snapshots(self): return 0

    backend = Backend()
    pool = WarmPool(backend, max_containers=2, refill_threads=1)
    try:
        a, _, _ = pool.acquire("img", "rollout")
        pool.acquire("img", "rollout")
        backend.alive.discard(a)                     # the step holding `a` was killed; the sweep removed it
        assert pool.stats()["in_use"] == 2
        assert pool.reconcile() == 1 and pool.stats()["in_use"] == 1
        pool.acquire("img", "rollout")               # the slot is usable again
    finally:
        pool.close()


def test_warm_pool_frees_sandboxes_whose_holder_stopped_keeping_them_alive():
    """P4 drill kill_worker: a killed worker's sandboxes must not stay "in use" until their 1 h expiry."""
    from taskforge.core.sandbox.pool import WarmPool

    class Backend:
        def __init__(self): self.alive, self.n = set(), 0
        def create(self, image, purpose):
            self.n += 1
            self.alive.add(f"c{self.n}")
            return f"c{self.n}"
        def destroy(self, sid): self.alive.discard(sid)
        def list_owned(self): return sorted(self.alive)
        def sweep_expired(self): return 0
        def sweep_snapshots(self): return 0

    now = [1000.0]
    backend = Backend()
    pool = WarmPool(backend, max_containers=3, refill_threads=1, orphan_s=120, clock=lambda: now[0])
    try:
        kept, _, _ = pool.acquire("img", "rollout")
        dead, _, _ = pool.acquire("img", "rollout")
        grading, _, _ = pool.acquire("img", "verify", tracked=False)   # sandboxd's own, inside one request
        now[0] += 100
        assert pool.touch([kept, "unknown"]) == 1                     # the live holder reports in
        now[0] += 100                                                 # `dead` silent for 200 s, `kept` for 100 s
        assert pool.reap_orphans() == 1
        assert dead not in backend.alive and kept in backend.alive and grading in backend.alive
        assert pool.stats()["in_use"] == 2 and pool.stats()["orphaned"] == 1
        pool.release(kept)
        now[0] += 1000
        assert pool.reap_orphans() == 0                               # released ones are no longer tracked
    finally:
        pool.close()


class _SlowRemovalBackend:
    """Deleting takes 0.3 s per docker command, however many containers it names (as `docker rm -f a b c`)."""

    def __init__(self):
        self.alive, self.n, self.rm_calls = set(), 0, []
        self.lock = threading.Lock()

    def create(self, image, purpose):
        with self.lock:
            self.n += 1
            self.alive.add(f"c{self.n}")
            return f"c{self.n}"

    def destroy(self, sid):
        self.destroy_many([sid])

    def destroy_many(self, sids):
        time.sleep(0.3)
        with self.lock:
            self.rm_calls.append(list(sids))
            self.alive -= set(sids)

    def list_owned(self): return sorted(self.alive)
    def sweep_expired(self): return 0
    def sweep_snapshots(self): return 0


def test_release_returns_at_once_and_removal_is_batched_but_still_holds_the_slot():
    """P4 probe: one `docker rm` takes ~1 s idle and ~3 s under load; 30 in one command take 3.3 s."""
    from taskforge.core.sandbox import SandboxBusy
    from taskforge.core.sandbox.pool import WarmPool
    backend = _SlowRemovalBackend()
    pool = WarmPool(backend, max_containers=10, refill_threads=1, remove_threads=1)
    try:
        sids = [pool.acquire("img", "rollout")[0] for _ in range(10)]
        t = time.monotonic()
        for sid in sids:
            pool.release(sid)
        assert time.monotonic() - t < 0.1                      # nobody waited for docker rm
        assert pool.stats()["removing"] == 10
        with pytest.raises(SandboxBusy):                       # still counted until really gone
            pool.acquire("img", "rollout")
        assert pool.flush(timeout_s=5)
        assert backend.alive == set() and len(backend.rm_calls) <= 3   # 10 removals in a few commands
        assert pool.stats()["removing"] == 0
        pool.acquire("img", "rollout")                         # the slots are free again
    finally:
        pool.close()


def test_warm_pool_tops_itself_up_while_the_hinted_work_runs():
    """Before P4 the hint was cut to 32 and the pool filled once; now it keeps max_idle_per_image ready
    until the hinted number has been handed out, and then stops."""
    from taskforge.core.sandbox.pool import WarmPool
    backend = _SlowRemovalBackend()
    pool = WarmPool(backend, max_containers=100, max_idle_per_image=3, refill_threads=2, reserve=0)
    try:
        pool.prewarm("img", 40)
        hits = 0
        for _ in range(40):
            deadline = time.monotonic() + 5
            while pool.stats()["idle"] == 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            sid, from_pool, _ = pool.acquire("img", "rollout")
            hits += from_pool
            assert pool.stats()["idle"] <= 3
            pool.release(sid)
        assert hits == 40
        time.sleep(0.2)
        s = pool.stats()
        assert s["demand"] == 0 and s["idle"] == 0 and s["prewarmed"] == 40   # nothing started beyond the hint
    finally:
        pool.close()


def test_pre_started_sandboxes_never_take_the_reserved_slots():
    from taskforge.core.sandbox.pool import WarmPool
    backend = _SlowRemovalBackend()
    pool = WarmPool(backend, max_containers=8, max_idle_per_image=8, refill_threads=2, reserve=3)
    try:
        pool.prewarm("a", 8)
        deadline = time.monotonic() + 5
        while pool.stats()["idle"] < 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.1)
        assert pool.stats()["idle"] == 5                       # 8 slots, 3 kept free
        for _ in range(3):                                     # a step of another image still gets in
            assert pool.acquire("b", "rollout")[1] is False
    finally:
        pool.close()


def test_an_unreachable_sandboxd_makes_steps_wait_without_using_up_attempts():
    """P4 drill restart_sandboxd: a 72 s restart used up all three attempts of every running step."""
    import pytest
    from taskforge.core.sandbox import SandboxUnavailable
    from taskforge.core.sandbox.client import SandboxdClient
    client = SandboxdClient("http://127.0.0.1:1", connect_timeout_s=1.0)      # nothing listens there
    with pytest.raises(SandboxUnavailable) as e:
        client.create("img", purpose="rollout")
    assert e.value.consume_attempt is False and e.value.delay_s == 10.0
    client.destroy("tf-x")                                                    # best effort: never raises


def test_a_full_disk_on_the_sandbox_machine_makes_steps_wait_for_space():
    """P4 drill disk_full: 60 s of full disk used up all three attempts of 29 steps."""
    import errno

    import pytest
    from fastapi.testclient import TestClient
    from taskforge.core.sandbox import SandboxUnavailable
    from taskforge.core.sandbox.client import SandboxdClient
    from taskforge.core.sandbox.service import create_app

    class Backend:
        def __init__(self): self.alive = set()
        def create(self, image, purpose):
            self.alive.add("c1")
            return "c1"
        def snapshot_app(self, sid): raise OSError(errno.ENOSPC, "No space left on device")
        def destroy(self, sid): self.alive.discard(sid)
        def destroy_many(self, sids): self.alive.difference_update(sids)
        def list_owned(self): return sorted(self.alive)
        def sweep_expired(self): return 0
        def sweep_snapshots(self): return 0

    with TestClient(create_app(Backend(), max_containers=4)) as http:
        client = SandboxdClient("http://testserver")
        client.http = http
        sid = client.create("img", purpose="rollout")
        with pytest.raises(SandboxUnavailable) as e:
            client.snapshot_app(sid)
        assert e.value.consume_attempt is False and e.value.delay_s == 30.0
        assert "taskforge_sandbox_disk_full_total 1.0" in http.get("/metrics").text
