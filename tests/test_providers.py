"""The two real model connections, tested against a fake SDK client and a pretend HTTP server (no paid calls)."""
import json
from types import SimpleNamespace as NS

import httpx
import pytest

from taskforge.core.agent import SOLVER_TOOLS, run_agent
from taskforge.core.errors import BudgetExceeded
from taskforge.core.gateway import Gateway, ModelRequest, ProviderError
from taskforge.core.gateway.anthropic import FALLBACK_BETA, AnthropicProvider, cost_of, to_anthropic_messages
from taskforge.core.gateway.openrouter import OpenRouterProvider
from taskforge.core.sandbox import FakeSandbox
from taskforge.core.scope import CallScope


class Block(NS):
    def to_dict(self, mode="json", exclude_none=True):
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


def usage(i=100, o=50, cw=0, cr=0, iterations=None):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
              iterations=iterations)


class FakeStream:
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get_final_message(self): return self.message


class FakeClient:
    """Records every request; answers from a script of messages."""
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Msgs:
            def stream(self, **kw):
                outer.requests.append(("plain", kw))
                return FakeStream(outer.replies.pop(0))
        self.messages = Msgs()

        class BetaMsgs:
            def stream(self, **kw):
                outer.requests.append(("beta", kw))
                return FakeStream(outer.replies.pop(0))
        self.beta = NS(messages=BetaMsgs())


def message(blocks, stop="end_turn", u=None, model="claude-opus-5-5", stop_details=None):
    return NS(content=blocks, stop_reason=stop, usage=u or usage(), model=model, stop_details=stop_details)


def test_anthropic_one_shot_uses_fallback_effort_and_prices_cache_tokens():
    client = FakeClient([message([Block(type="thinking", thinking="", signature="sig1"),
                                  Block(type="text", text='{"ok": true}', citations=None)],
                                 u=usage(i=1000, o=500, cw=200, cr=3000))])
    p = AnthropicProvider(client=client)
    r = p.call(ModelRequest(model="anthropic/claude-opus-5-5", params={"effort": "low"}, max_tokens=4000,
                            messages=({"role": "user", "content": "hi"},)), role="rubric_scorer")
    kind, kw = client.requests[0]
    assert kind == "beta" and kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "low"} and "thinking" not in kw and "tool_choice" not in kw
    assert r.text == '{"ok": true}' and r.usage.input_tokens == 4200 and r.usage.cache_read_tokens == 3000
    assert r.cost_usd == pytest.approx((1000 * 4 + 500 * 20 + 200 * 5 + 3000 * 0.2) / 1e6)
    assert r.raw[0] == {"type": "thinking", "thinking": "", "signature": "sig1"}    # kept for sending back


def test_anthropic_fallback_attempts_are_each_priced_at_their_own_model():
    its = [NS(type="message", model="claude-opus-5-5", input_tokens=100, output_tokens=0,
              cache_creation_input_tokens=0, cache_read_input_tokens=0),
           NS(type="fallback_message", model="claude-opus-4-8", input_tokens=100, output_tokens=200,
              cache_creation_input_tokens=0, cache_read_input_tokens=0)]
    client = FakeClient([message([Block(type="text", text="ok")], u=usage(100, 200, iterations=its),
                                 model="claude-opus-4-8")])
    r = AnthropicProvider(client=client).call(ModelRequest(model="anthropic/claude-opus-5-5", messages=(
        {"role": "user", "content": "x"},)), role="r")
    assert r.served_by == "claude-opus-4-8"
    assert r.cost_usd == pytest.approx(cost_of("claude-opus-5-5", 100, 0) + cost_of("claude-opus-4-8", 100, 200))


def test_anthropic_agent_loop_sends_reasoning_blocks_back_unchanged_and_groups_tool_results():
    turn1 = [Block(type="thinking", thinking="", signature="S1"),
             Block(type="tool_use", id="tu_1", name="run_shell", input={"command": "cat /app/a.txt"}),
             Block(type="tool_use", id="tu_2", name="read_file", input={"path": "/app/a.txt"})]
    turn2 = [Block(type="thinking", thinking="", signature="S2"), Block(type="tool_use", id="tu_3", name="finish", input={})]
    client = FakeClient([message(turn1, stop="tool_use"), message(turn2, stop="tool_use")])

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass
    gw = Gateway({"anthropic": AnthropicProvider(client=client)}, budget=Budget())
    sb = FakeSandbox()
    sid = sb.create(sb.build({"environment/app/a.txt": "hello"}), purpose="rollout")
    n = iter(range(1, 99))
    res = run_agent(gateway=gw, next_scope=lambda: CallScope("r", "s", "k", next(n)), role="hacker",
                    model="anthropic/claude-opus-5-5", system="sys", task_prompt="do it", sandbox=sb, sid=sid,
                    tools=SOLVER_TOOLS, params={"effort": "low", "cache": True})
    assert res.exit == "finished"
    kind, second = client.requests[1]
    assert kind == "plain" and "fallbacks" not in second              # no fallback inside agent loops
    assert second["cache_control"] == {"type": "ephemeral"}
    msgs = second["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == [b.to_dict() for b in turn1]        # exactly as received, thinking included
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["tu_1", "tu_2"]   # both results in one message


def test_anthropic_refusal_is_a_result_and_ends_the_agent_loop():
    client = FakeClient([message([], stop="refusal", stop_details=NS(category="cyber"))])

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass
    gw = Gateway({"anthropic": AnthropicProvider(client=client)}, budget=Budget())
    sb = FakeSandbox()
    sid = sb.create(sb.build({}), purpose="hack")
    res = run_agent(gateway=gw, next_scope=lambda: CallScope("r", "s", "k", 1), role="hacker",
                    model="anthropic/claude-opus-5-5", system="", task_prompt="x", sandbox=sb, sid=sid)
    assert res.exit == "refused" and "cyber" in res.steps[-1].message


def test_neutral_to_anthropic_conversion_without_raw():
    out = to_anthropic_messages([{"role": "user", "content": "q"},
                                 {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "f", "arguments": {"x": 1}}]},
                                 {"role": "tool", "tool_call_id": "a", "content": "r"}])
    assert out[1]["content"] == [{"type": "tool_use", "id": "a", "name": "f", "input": {"x": 1}}]
    assert out[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "r"}]}


# ------------------------------------------------------------------ OpenRouter
def openrouter(handler):
    return OpenRouterProvider("key", host="siliconflow/fp8", price_in=0.07, price_out=0.28,
                              transport=httpx.MockTransport(handler))


def test_openrouter_pins_the_host_parses_tool_calls_and_reads_cost():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"provider": "SiliconFlow", "model": "qwen/qwen3-coder-30b-a3b-instruct",
                                         "choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                                             {"id": "c1", "type": "function", "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}},
                                             {"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": "{not json"}}]}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.000098}})
    p = openrouter(handler)
    r = p.call(ModelRequest(model="openrouter/qwen/qwen3-coder-30b-a3b-instruct", system="sys",
                            params={"temperature": 0.7, "top_p": 0.8, "top_k": 20, "effort": "ignored"},
                            tools=SOLVER_TOOLS[:1], messages=(
                                {"role": "user", "content": "go"},
                                {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "name": "run_shell", "arguments": {"command": "pwd"}}]},
                                {"role": "tool", "tool_call_id": "x", "content": "/app"})), role="weak_solver")
    assert seen["provider"] == {"order": ["siliconflow/fp8"], "allow_fallbacks": False} and seen["usage"] == {"include": True}
    assert seen["model"] == "qwen/qwen3-coder-30b-a3b-instruct" and seen["temperature"] == 0.7 and "effort" not in seen
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert json.loads(seen["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {"command": "pwd"}
    assert seen["messages"][3] == {"role": "tool", "tool_call_id": "x", "content": "/app"}
    assert [c.name for c in r.tool_calls] == ["run_shell", "write_file"] and r.tool_calls[1].arguments == {"_raw": "{not json"}
    assert r.cost_usd == pytest.approx(0.000098) and r.served_by.startswith("SiliconFlow")


def test_openrouter_empty_reply_is_retried_and_402_pauses():
    empty = openrouter(lambda req: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}))
    with pytest.raises(ProviderError) as e:
        empty.call(ModelRequest(model="openrouter/m", messages=({"role": "user", "content": "x"},)), role="weak_solver")
    assert e.value.retryable
    broke = openrouter(lambda req: httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}))
    with pytest.raises(BudgetExceeded):
        broke.call(ModelRequest(model="openrouter/m", messages=({"role": "user", "content": "x"},)), role="weak_solver")
    bad = openrouter(lambda req: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(ProviderError) as e:
        bad.call(ModelRequest(model="openrouter/m", messages=()), role="weak_solver")
    assert not e.value.retryable


def test_provider_cap_stops_spending_on_one_provider_only():
    from taskforge.core.gateway import FakeProvider

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass
    gw = Gateway({"a": FakeProvider(lambda r, q: "x", cost_per_call=1.0), "b": FakeProvider(lambda r, q: "y", cost_per_call=1.0)},
                 budget=Budget(), provider_caps={"a": 2.0})
    for i in range(2):
        gw.complete(CallScope("run", "s", "k", i), "r", ModelRequest(model="a/m", messages=()))
    with pytest.raises(BudgetExceeded):
        gw.complete(CallScope("run", "s", "k", 9), "r", ModelRequest(model="a/m", messages=()))
    gw.complete(CallScope("run", "s", "k", 10), "r", ModelRequest(model="b/m", messages=()))   # other provider unaffected
    assert gw.spent("run") == 3.0


def test_openrouter_missing_host_makes_callers_wait_without_burning_attempts():
    from taskforge.core.errors import Retryable
    gone = openrouter(lambda req: httpx.Response(404, json={"error": {"code": 404, "message": "No endpoints found for qwen/x."}}))

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass
    gw = Gateway({"openrouter": gone}, budget=Budget(), sleep=lambda s: None)
    with pytest.raises(Retryable) as e:
        gw.complete(CallScope("r", "s", "k", 1), "weak_solver", ModelRequest(model="openrouter/m", messages=()))
    assert e.value.consume_attempt is False and e.value.delay_s == 60.0


def test_markup_glued_to_a_tool_name_is_cleaned_in_the_history_sent_back():
    from taskforge.core.gateway.openrouter import to_openai_messages
    out = to_openai_messages("", [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "finish</function", "arguments": {}}]}, {"role": "tool", "tool_call_id": "c1", "content": "x"}])
    assert out[0]["tool_calls"][0]["function"]["name"] == "finish__function"


def test_too_many_requests_makes_the_step_wait_without_using_up_attempts():
    """P4 drill rate_limit: a 90 s storm of 429 replies used up all three attempts of 16 steps in 15 s."""
    from taskforge.core.errors import Retryable
    from taskforge.core.gateway import FakeProvider

    class Budget:
        def reserve(self, scope, est): return None
        def settle(self, token, actual): pass

    def refuse(role, req):
        raise ProviderError("429: too many requests", retryable=True, rate_limited=True)
    gw = Gateway({"p": FakeProvider(refuse)}, budget=Budget(), sleep=lambda s: None)
    with pytest.raises(Retryable) as e:
        gw.complete(CallScope("r", "s", "k", 1), "x", ModelRequest(model="p/m", messages=()))
    assert e.value.consume_attempt is False and 10.0 <= e.value.delay_s <= 30.0

    def broken(role, req):
        raise ProviderError("503: unavailable", retryable=True)
    gw = Gateway({"p": FakeProvider(broken)}, budget=Budget(), sleep=lambda s: None)
    with pytest.raises(Retryable) as e:
        gw.complete(CallScope("r", "s", "k", 2), "x", ModelRequest(model="p/m", messages=()))
    assert e.value.consume_attempt is True                  # other faults still count, so a dead provider ends

