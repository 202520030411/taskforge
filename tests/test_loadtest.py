"""The P4 load test: its fan-out step, the fake solver, and fault injection. No Docker, no paid model."""
import json

import pytest

from taskforge.core.gateway import ModelRequest, ProviderError
from taskforge.core.gateway.fake import FakeProvider
from taskforge.core.sandbox import FakeSandbox
from taskforge.runtime import build_memory
from taskforge.taskgen import loadtest, package as pkg
from taskforge.taskgen.contamination import BenchmarkIndex
from taskforge.taskgen.demo import BENCHMARK_TEXTS


def test_load_run_forks_every_attempt_and_pays_each_model_call_once():
    provider = FakeProvider(loadtest.load_script(turns=5), cost_per_call=0.001, record=False)
    platform = build_memory(providers={"anthropic": provider, "openrouter": provider}, sandbox=FakeSandbox(),
                            benchmark=BenchmarkIndex(BENCHMARK_TEXTS))
    run_id = loadtest.start(platform, attempts=7, tasks=3, prewarm_per_task=2, tag="t")
    platform.engine.run_until_idle(run_id)
    steps = platform.engine.all_steps(run_id)
    rollouts = [s for s in steps if s.step == "rollout"]
    assert len(rollouts) == 7 and all(s.status.value == "SUCCEEDED" for s in rollouts)
    assert {s.input["task_id"] for s in rollouts} == set(next(s for s in steps if s.step == "load_fanout").input["task_ids"])
    fanout = next(s for s in steps if s.step == "load_fanout")
    assert fanout.output["succeeded"] == 7 and fanout.output["failed"] == 0
    ledger = platform.svc.gateway.ledger
    assert sum(not c.cached for c in ledger) == 7 * 5          # five turns per attempt
    assert platform.svc.gateway.double_charges() == 0


def test_task_copies_differ_only_in_a_dockerfile_label():
    base = pkg.load_dir(loadtest.TASK_DIR)
    a, b = loadtest.task_copy(base, 0), loadtest.task_copy(base, 1)
    assert a["environment/Dockerfile"] != b["environment/Dockerfile"]
    assert {k: v for k, v in a.items() if k != "environment/Dockerfile"} == \
        {k: v for k, v in b.items() if k != "environment/Dockerfile"}
    assert "def main" in loadtest.reference_program(base)


def test_fault_file_turns_rate_limit_errors_on_and_off(tmp_path):
    path = tmp_path / "faults.json"
    faults = loadtest.FaultFile(str(path))
    provider = FakeProvider(lambda role, req: "ok", faults=faults)
    request = ModelRequest(model="openrouter/x", messages=({"role": "user", "content": "hi"},))
    assert provider.call(request, role="r").text == "ok"           # no file: no faults
    path.write_text(json.dumps({"rate_limit_share": 1.0}))
    faults._read_at = 0.0                                          # skip the once-a-second re-read wait
    with pytest.raises(ProviderError) as e:
        provider.call(request, role="r")
    assert e.value.retryable and "429" in str(e.value)
    path.write_text("{}")
    faults._read_at = 0.0
    assert provider.call(request, role="r").text == "ok"


class FailingSnapshots(FakeSandbox):
    """The first `n` snapshots raise a plain error, which fails their rollout step for good."""

    def __init__(self, n: int):
        super().__init__()
        self.left = n

    def snapshot_app(self, sid):
        if self.left > 0:
            self.left -= 1
            raise RuntimeError("injected: disk full")
        return super().snapshot_app(sid)


def test_failed_attempts_are_run_again_under_new_keys():
    provider = FakeProvider(loadtest.load_script(turns=4), record=False)
    platform = build_memory(providers={"anthropic": provider, "openrouter": provider}, sandbox=FailingSnapshots(2),
                            benchmark=BenchmarkIndex(BENCHMARK_TEXTS))
    run_id = loadtest.start(platform, attempts=5, tasks=2, prewarm_per_task=0, tag="r")
    platform.engine.run_until_idle(run_id)
    steps = platform.engine.all_steps(run_id)
    fanout = next(s for s in steps if s.step == "load_fanout")
    assert fanout.output["succeeded"] == 5 and fanout.output["failed"] == 0
    assert fanout.output["failed_per_round"] == [2, 0]
    reruns = sorted(s.key for s in steps if s.step == "rollout" and "#" in s.key)
    assert len(reruns) == 2 and all(k.endswith("#r1") for k in reruns)
    # a re-run under a new key is a new attempt: its calls are paid again (not a double charge); only a
    # retry of the same step (same key) would replay them from the cache
    assert platform.svc.gateway.double_charges() == 0
    assert sum(not c.cached for c in platform.svc.gateway.ledger) == (5 + 2) * 4
