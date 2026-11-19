"""Real containers. Skipped unless TASKFORGE_DOCKER=1 (the development laptop has no Docker; run on the VM)."""
import os
import time

import pytest

from taskforge.core.sandbox import DECOY_PATH, DockerSandbox, session
from taskforge.taskgen import package as pkg
from taskforge.taskgen.demo import BUGGY_ORDER_TOTALS, HANDWRITTEN, OrderTotalsKit, WorldConfig, run_world
from taskforge.taskgen.config import TaskgenConfig
from taskforge.core.sandbox import section
from taskforge.taskgen.verify import gate_initial, gate_leak, gate_oracle, run_scripted_hacks

pytestmark = pytest.mark.skipif(os.environ.get("TASKFORGE_DOCKER") != "1", reason="set TASKFORGE_DOCKER=1 to run")


@pytest.fixture(scope="module")
def sb():
    return DockerSandbox()


@pytest.fixture(scope="module")
def files():
    return pkg.load_dir(HANDWRITTEN)


def grade(sb, files, writes):
    image = sb.build(files)
    with session(sb, image, purpose="rollout") as sid:
        assert sb.write_files(sid, writes) == {}
        snap = sb.snapshot_app(sid)
    return sb.verify(image, section(files, "tests"), snap)


def test_handwritten_task_passes_every_gate(sb, files):
    assert gate_oracle(sb, files).passed
    assert gate_initial(sb, files).passed
    hacks = run_scripted_hacks(sb, files)
    assert {k for k, v in hacks.items() if v["passed"]} == set()
    assert set(hacks["outside_app_writes"]["denied_writes"]) == {"/tests/test_outputs.py", "/logs/verifier/reward.txt"}
    assert gate_leak(files).passed


def test_correct_program_passes_and_buggy_one_fails(sb, files):
    correct = OrderTotalsKit(WorldConfig()).correct
    assert grade(sb, files, {"/app/order_totals.py": correct}).passed
    bad = grade(sb, files, {"/app/order_totals.py": BUGGY_ORDER_TOTALS})
    assert not bad.passed and "test_sample" not in bad.output.split("FAILED")[-1]


def test_agent_cannot_reach_outside_app_and_sees_the_decoy(sb, files):
    image = sb.build(files)
    with session(sb, image, purpose="rollout") as sid:
        assert sb.exec(sid, "id -u").output.strip() == "1000"
        assert sb.exec(sid, "touch /usr/local/bin/x").exit_code != 0
        assert sb.exec(sid, "ls /tests /solution").exit_code != 0          # never copied in
        assert "TASKFORGE-CANARY" in sb.exec(sid, f"cat {DECOY_PATH}").output
        assert sb.exec(sid, "python3 -c \"import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)\"").exit_code != 0


def test_two_containers_of_one_image_look_the_same_to_the_agent(sb, files):
    """A re-run attempt replays its cached model calls only while every command prints what it printed
    the first time (P4 drill kill_worker): no creation time or container id may show."""
    image = sb.build(files)
    seen = []
    for i in range(2):
        time.sleep(1.1 * i)                     # created in different seconds
        with session(sb, image, purpose="rollout") as sid:
            seen.append([sb.exec(sid, c).output for c in ("ls -la /app", "ls -la /opt /opt/grader", "hostname",
                                                         "stat -c %Y /")])
    assert seen[0] == seen[1]
    assert seen[0][2].strip() == "sandbox"


def test_whole_workflow_on_real_containers(tmp_path, sb):
    world = WorldConfig(proposals=1, add_overlap_proposal=False, weak_pass=(0, 3))
    w = run_world(tmp_path, world, sandbox=sb, kit=OrderTotalsKit(world), config=TaskgenConfig(h4_sample_rate=0.0))
    assert w.engine.summary(w.run_id)["finished"]
    assert len(w.export["accepted"]) == 1
    assert w.export["measures"]["double_charges"] == 0
    assert sb.created == sb.destroyed
