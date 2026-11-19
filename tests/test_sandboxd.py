"""sandboxd over real HTTP with real containers. Skipped unless TASKFORGE_DOCKER=1 (run on the VM).
Each test server labels its containers with its own owner name, so it can never touch another sandboxd's."""
import os
import socket
import statistics
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TASKFORGE_DOCKER") != "1", reason="set TASKFORGE_DOCKER=1 to run")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, max_containers=24, owner=None):
        import uvicorn
        from taskforge.core.sandbox import DockerSandbox
        from taskforge.core.sandbox.client import SandboxdClient
        from taskforge.core.sandbox.service import create_app
        self.owner = owner or f"test-{uuid.uuid4().hex[:8]}"
        self.backend = DockerSandbox(owner=self.owner)
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.backend, max_containers=max_containers),
                                                    host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.client = SandboxdClient(f"http://127.0.0.1:{self.port}")
        for _ in range(100):
            try:
                self.client.capacity()
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError("sandboxd did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(20)


@pytest.fixture
def server():
    s = Server()
    yield s
    s.stop()
    assert s.backend.list_owned() == []          # shutdown leaves no container behind


@pytest.fixture(scope="module")
def files():
    from taskforge.taskgen import package as pkg
    from taskforge.taskgen.demo import HANDWRITTEN
    return pkg.load_dir(HANDWRITTEN)


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_gates_through_sandboxd(server, files):
    from taskforge.taskgen.verify import gate_initial, gate_oracle, run_scripted_hacks
    sb = server.client
    assert gate_oracle(sb, files).passed and gate_initial(sb, files).passed
    assert not any(v["passed"] for v in run_scripted_hacks(sb, files).values())
    assert sb.created == sb.destroyed and sb.capacity()["in_use"] == 0


def test_warm_pool_hands_out_prestarted_sandboxes_and_is_faster(server, files):
    sb = server.client
    image = sb.build(files)
    cold = []
    for _ in range(6):
        sid = sb.create(image, purpose="rollout")
        cold.append(sb.start_latencies[-1])
        sb.destroy(sid)
    assert sb.pool_hits == 0
    sb.prewarm(image, 6)
    wait_for(lambda: sb.capacity()["idle"] == 6)
    warm, sids = [], []
    for _ in range(6):
        sids.append(sb.create(image, purpose="rollout"))
        warm.append(sb.start_latencies[-1])
    assert sb.pool_hits == 6
    assert "TASKFORGE-CANARY" in sb.exec(sids[0], "cat /opt/grader/expected_outputs.json").output   # pooled ones are complete
    assert sb.exec(sids[0], "id -u").output.strip() == "1000"
    for sid in sids:
        sb.destroy(sid)
    time.sleep(0.5)
    stats = sb.capacity()
    assert stats["idle"] == 0 and stats["demand"] == 0                   # demand used up: nothing more is started
    assert statistics.median(warm) < statistics.median(cold) / 5
    print(f"\ncold median {statistics.median(cold) * 1000:.0f} ms, warm median {statistics.median(warm) * 1000:.2f} ms")


def test_capacity_limit_refuses_with_busy_and_recovers(files):
    from taskforge.core.sandbox import SandboxBusy
    s = Server(max_containers=3)
    try:
        sb = s.client
        image = sb.build(files)
        sids = [sb.create(image, purpose="rollout") for _ in range(3)]
        with pytest.raises(SandboxBusy) as e:
            sb.create(image, purpose="rollout")
        assert e.value.consume_attempt is False              # back-pressure never burns a step's attempt
        sb.destroy(sids.pop())
        wait_for(lambda: sb.capacity()["removing"] == 0)    # the slot is free once the container is really gone
        sids.append(sb.create(image, purpose="rollout"))
        for sid in sids:
            sb.destroy(sid)
    finally:
        s.stop()


def test_unknown_sandbox_is_a_retryable_fault_and_missing_file_is_not(server, files):
    from taskforge.core.sandbox import SandboxError
    sb = server.client
    with pytest.raises(SandboxError):
        sb.exec("tf-rollout-doesnotexist", "true")
    sid = sb.create(sb.build(files), purpose="rollout")
    with pytest.raises(FileNotFoundError):
        sb.read_file(sid, "/app/nope.txt")
    assert sb.write_files(sid, {"/etc/x": "1"}) != {}       # permission errors come back as data
    sb.destroy(sid)


def test_restart_removes_containers_left_by_a_dead_sandboxd(files):
    from taskforge.core.sandbox import DockerSandbox
    owner = f"test-{uuid.uuid4().hex[:8]}"
    orphan_owner = DockerSandbox(owner=owner)
    orphan = orphan_owner.create(orphan_owner.build(files), purpose="rollout")     # as if a sandboxd died holding it
    assert orphan in orphan_owner.list_owned()
    s = Server(owner=owner)
    try:
        assert orphan_owner.list_owned() == []
    finally:
        s.stop()


def test_whole_workflow_through_sandboxd(server, tmp_path):
    from taskforge.taskgen.config import TaskgenConfig
    from taskforge.taskgen.demo import OrderTotalsKit, WorldConfig, run_world
    world = WorldConfig(proposals=1, add_overlap_proposal=False, weak_pass=(0, 3))
    w = run_world(tmp_path, world, sandbox=server.client, kit=OrderTotalsKit(world),
                  config=TaskgenConfig(h4_sample_rate=0.0))
    assert w.engine.summary(w.run_id)["finished"] and len(w.export["accepted"]) == 1
    assert server.client.created == server.client.destroyed
    assert server.client.pool_hits > 0                       # the demand hints were used
