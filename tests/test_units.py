"""Small pure pieces: import and pairing, overlap check, partial order, difficulty logic, gateway, store, hacks."""
import pytest

from taskforge.core.agent import AgentStep, ToolCallRecord, run_agent
from taskforge.core.errors import BudgetExceeded, NotApproved, Retryable
from taskforge.core.gateway import FakeProvider, Gateway, ModelRequest, ProviderError, tool_reply, turn_number
from taskforge.core.sandbox import CANARY, DECOY_PATH, FakeSandbox, truncate_tail
from taskforge.core.scope import CallScope
from taskforge.core.store import MemoryStore
from taskforge.taskgen import difficulty as diff
from taskforge.taskgen.agents.llm import parse_json_reply
from taskforge.taskgen.agents.rubric import partial_order
from taskforge.taskgen.contamination import BenchmarkIndex, ContaminationError
from taskforge.taskgen.demo import BENCHMARK_TEXTS, FakeKit, WorldConfig, write_fake_jobs
from taskforge.taskgen.hacks import HackContext, scan_suspect
from taskforge.taskgen.pairs import load_job, make_pairs
from taskforge.taskgen.policy import pick
from taskforge.taskgen.verify import gate_initial, gate_leak, gate_oracle, run_scripted_hacks


# ---------------------------------------------------------------- import + pairs
def test_import_and_pairing(tmp_path):
    jobs = write_fake_jobs(tmp_path)
    strong = load_job(jobs["strong_jobs"][0], "strong")
    weak = load_job(jobs["weak_jobs"][0], "weak")
    assert len(strong) == 4 and len(weak) == 5
    t = strong[0]
    assert t.task_ref == "task-a" and t.passed and t.harness == "terminus-2 2.0"
    agent_steps = [s for s in t.steps if s.source == "agent"]
    assert agent_steps[0].tool_calls[0].name == "bash_command" and agent_steps[0].observation.startswith("$ ls")
    pairs = make_pairs(strong + weak)
    assert [p.task_ref for p in pairs] == ["task-a", "task-b"]      # task-c has no strong run
    assert pairs == make_pairs(list(reversed(strong + weak)))       # independent of input order


# ---------------------------------------------------------------- contamination
def test_contamination_levels():
    index = BenchmarkIndex(BENCHMARK_TEXTS)
    text = BENCHMARK_TEXTS["bench/rotate-logs/instruction.md"]
    assert index.check(text).status == "OVERLAP"
    assert index.check("Please: " + text.upper() + " Thanks.").status == "OVERLAP"           # case and padding
    assert index.check("Intro. " + " ".join(text.split()[:20]) + " plus a long new story " * 30).status == "OVERLAP"
    assert index.check("Sum the order amounts per customer from a CSV export.").status == "CLEAN"
    grey = " ".join(text.split()[:7]) + " " + " ".join(f"word{i}" for i in range(150))   # 3 of ~28 pieces
    assert index.check(grey).status == "GREY"
    with pytest.raises(ContaminationError):
        BenchmarkIndex({})


# -------------------------------------------------------- rubric + difficulty
def test_partial_order_rule():
    assert partial_order([1, 1], [1, 0], margin=0.25, floor=0.5) == (True, 1.0, 0.5)
    assert partial_order([1, 1], [1, 1], margin=0.25, floor=0.5)[0] is False       # no separation
    assert partial_order([1, 0, 0, 0], [0, 0], margin=0.25, floor=0.5)[0] is False  # both low, weak lower
    assert partial_order([], [0], margin=0.25, floor=0.5)[0] is False


def test_difficulty_logic():
    assert diff.after_first([True, False, False, False, False]) == "in_band"
    assert diff.after_first([False] * 5) == "rerun" and diff.after_first([True] * 5) == "rerun"
    assert diff.after_rerun([False] * 5, [False] * 4 + [True]) == "in_band"
    assert diff.after_rerun([True] * 5, [True] * 5) == "all_pass"
    assert diff.diagnose_all_pass([[], ["read_decoy"]]) == "exploit"
    assert diff.diagnose_all_pass([[], []]) == "too_easy"
    assert diff.diagnose_all_fail(False, "unreasonable") == "broken"
    assert diff.diagnose_all_fail(True, "reasonable") == "truly_hard"
    assert diff.diagnose_all_fail(True, "unreasonable") == "unclear"


def test_sampling_is_stable():
    ids = [f"task_package:{i}" for i in range(20)]
    assert pick(ids, 3, "H4") == pick(list(reversed(ids)), 3, "H4") and len(pick(ids, 3, "H4")) == 3


# ------------------------------------------------------------------- gateway
class Budget:
    def __init__(self, limit, approved=True):
        self.limit, self.spent, self.approved = limit, 0.0, approved

    def reserve(self, scope, est):
        if not self.approved:
            raise NotApproved("no")
        if self.spent + est > self.limit:
            raise BudgetExceeded("over")
        return scope.run_id

    def settle(self, token, actual):
        self.spent += actual


def test_gateway_caches_by_idempotency_key_and_charges_once():
    provider = FakeProvider(lambda role, req: {"ok": True}, cost_per_call=0.5)
    gw = Gateway({"fake": provider}, budget=Budget(10))
    req = ModelRequest(model="fake/m", messages=({"role": "user", "content": "hi"},))
    scope = CallScope("run", "sr", "run/step/key", 1)
    a, b = gw.complete(scope, "r", req), gw.complete(scope, "r", req)
    assert a == b and len(provider.calls) == 1 and gw.spent() == 0.5 and gw.double_charges() == 0
    gw.complete(CallScope("run", "sr", "run/step/key", 2), "r", req)        # a different call in the same step
    assert len(provider.calls) == 2


def test_gateway_budget_and_provider_errors():
    gw = Gateway({"fake": FakeProvider(lambda r, q: "x", cost_per_call=1)}, budget=Budget(1.5))
    req = ModelRequest(model="fake/m", messages=())
    gw.complete(CallScope("r", "s", "a", 1), "role", req)
    with pytest.raises(BudgetExceeded):
        gw.complete(CallScope("r", "s", "a", 2), "role", req)
    with pytest.raises(NotApproved):
        Gateway({"fake": FakeProvider(lambda r, q: "x")}, budget=Budget(9, approved=False)).complete(
            CallScope("r", "s", "a", 1), "role", req)

    def flaky(role, request):
        raise ProviderError("overloaded", retryable=True)
    with pytest.raises(Retryable):
        Gateway({"fake": FakeProvider(flaky)}, budget=Budget(9)).complete(CallScope("r", "s", "a", 1), "role", req)


def test_parse_json_reply():
    assert parse_json_reply('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('{"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    for bad in ("no json here", '{"a": }', "[1, 2]"):
        with pytest.raises(Exception):
            parse_json_reply(bad)
    with pytest.raises(Exception):
        parse_json_reply('{"a": 1}', required=("b",))


# --------------------------------------------------------------------- store
def test_store_ids_are_content_hashes_and_parents_must_exist():
    from taskforge.taskgen.schemas import TaskProposal
    store = MemoryStore()
    p = TaskProposal(target_pattern_id="bad_pattern:x", title="t", statement="s", parents=())
    assert p.id == p.model_copy(update={"produced_by": "elsewhere"}).id      # provenance is not part of the id
    assert store.put(p) == store.put(p)
    with pytest.raises(KeyError):
        store.put(TaskProposal(target_pattern_id="bad_pattern:x", title="t", statement="s2", parents=("missing",)))


# --------------------------------------------------------------- agent loop
def test_agent_loop_turns_tool_failures_into_text_and_stops_on_finish():
    sb = FakeSandbox()
    image = sb.build({"environment/app/a.txt": "hello"})
    sid = sb.create(image, purpose="rollout")

    def script(role, req):
        return [tool_reply(("read_file", {"path": "/app/missing"}), ("run_shell", {"command": "cat /app/a.txt"})),
                tool_reply(("write_file", {"path": "/etc/passwd", "content": "x"})),
                tool_reply(("finish", {}))][turn_number(req)]
    gw = Gateway({"fake": FakeProvider(script)}, budget=Budget(9))
    calls = iter(range(1, 100))
    res = run_agent(gateway=gw, next_scope=lambda: CallScope("r", "s", "x", next(calls)), role="weak_solver",
                    model="fake/m", system="", task_prompt="do it", sandbox=sb, sid=sid)
    assert res.exit == "finished"
    obs = [s.observation for s in res.steps if s.source == "agent"]
    assert "FileNotFoundError" in obs[0] and "hello" in obs[0] and "Permission denied" in obs[1]


def test_truncation_keeps_the_tail_and_says_so():
    text, cut = truncate_tail("a" * 100 + "ERROR at the end", limit=20)
    assert cut and text.endswith("ERROR at the end") and "truncated" in text


# --------------------------------------------------------------------- hacks
def test_hack_context_and_suspicion_scan():
    files = FakeKit(WorldConfig()).package("t")
    ctx = HackContext.from_files(files)
    assert ctx.outputs == ("/app/answer.txt", "/app/work.log") and ctx.programs == ()
    assert ctx.sample_expected == "15\n"
    steps = [AgentStep(source="agent", tool_calls=(ToolCallRecord(name="run_shell", arguments={"command": f"cat {DECOY_PATH}"}),),
                       observation=f"... {CANARY} ...")]
    assert scan_suspect(steps) == ("canary_in_output", "read_decoy")
    assert scan_suspect([AgentStep(source="agent", tool_calls=(ToolCallRecord(name="run_shell", arguments={"command": "pytest"}),))]) == ()


def test_gates_on_fake_tasks():
    sb = FakeSandbox()
    good = FakeKit(WorldConfig()).package("t")
    assert gate_oracle(sb, good).passed and gate_initial(sb, good).passed and gate_leak(good).passed
    assert not any(v["passed"] for v in run_scripted_hacks(sb, good).values())
    weak = FakeKit(WorldConfig(weak_tests=True)).package("t")
    hacks = run_scripted_hacks(sb, weak)
    assert hacks["constant_output"]["passed"] and hacks["outside_app_writes"]["denied_writes"]
    broken = FakeKit(WorldConfig(oracle_broken=True)).package("t")
    assert gate_oracle(sb, broken).reasons == ("oracle_fails",)
    assert sb.leaked == 0


def test_agent_steps_outlast_the_agent_time_budget():
    """D-044: a step still running after its timeout_s is run again by another worker while the first copy
    keeps calling models, so every agent step must allow the agent's whole time budget plus slack."""
    from pydantic import ValidationError

    from taskforge.core.engine import Resource
    from taskforge.taskgen.config import AGENT_STEP_SLACK_S, PILOT, TaskgenConfig
    from taskforge.taskgen.workflow import WORKFLOW

    agent_steps = {s.name: s for s in WORKFLOW.steps.values() if s.resource == Resource.AGENT}
    assert set(agent_steps) == {"build_task", "gate_hack", "fix_verifier", "rollout", "repair"}
    for cfg in (TaskgenConfig(), PILOT):
        need = max(cfg.solver_wall_s, cfg.hacker_wall_s) + AGENT_STEP_SLACK_S
        assert all(s.timeout_s >= need for s in agent_steps.values()), (need, agent_steps)
    with pytest.raises(ValidationError, match="would run twice"):
        TaskgenConfig(solver_wall_s=3000)
    with pytest.raises(ValidationError, match="hacker_wall_s"):
        TaskgenConfig(hacker_wall_s=2500)
