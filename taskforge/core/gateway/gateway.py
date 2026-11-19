"""The only path to paid models (DESIGN.md section 6).

One call goes through these stages, in order:
  1. idempotency   look the request up by its key; a finished call returns its stored reply. A call that
                   is in flight elsewhere (same step running twice by accident) is waited for, not repeated
  2. breaker       a provider that keeps failing is skipped for a cooling-off period
  3. rate limit    take a token from the provider's shared bucket, or wait for one
  4. budget        reserve the estimated cost against the run's budget
  5. call          with a few quick retries on "overloaded / too many requests" errors
  6. record        store the reply under the key, settle the budget, write the ledger row
"""
from __future__ import annotations

import dataclasses
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .. import obs
from ..errors import BudgetExceeded, NotApproved, Retryable
from ..hashing import content_hash
from ..scope import CallScope
from .state import GatewayState, MemoryGatewayState
from .types import BudgetGuard, ModelRequest, ModelResponse, Provider, ProviderError


@dataclass(frozen=True)
class LLMCall:
    """One row of the cost ledger."""
    idem_key: str
    run_id: str
    step_run_id: str
    scope_id: str
    call_no: int
    role: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool          # True: answered from the cache, nothing was paid
    latency_s: float


@dataclass(frozen=True)
class RateLimit:
    capacity: float        # burst size
    refill_per_s: float    # steady rate


class CircuitBreaker:
    """Per provider, per process. closed -> open after `threshold` failures in a row -> after `cooldown_s`
    one probe call is let through (half-open) -> closed on success, open again on failure."""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown_s, self.clock = threshold, cooldown_s, clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._probing: set[str] = set()

    def before(self, provider: str) -> None:
        with self._lock:
            opened = self._opened_at.get(provider)
            if opened is None:
                return
            remaining = self.cooldown_s - (self.clock() - opened)
            if remaining > 0:
                raise Retryable(f"{provider}: circuit open", delay_s=remaining, consume_attempt=False)
            if provider in self._probing:
                raise Retryable(f"{provider}: circuit half-open, probe in flight", delay_s=1.0, consume_attempt=False)
            self._probing.add(provider)

    def success(self, provider: str) -> None:
        with self._lock:
            self._failures.pop(provider, None)
            self._opened_at.pop(provider, None)
            self._probing.discard(provider)

    def abort(self, provider: str) -> None:
        """The caller was let through but never reached the provider: free the probe slot."""
        with self._lock:
            self._probing.discard(provider)

    def failure(self, provider: str) -> None:
        with self._lock:
            n = self._failures.get(provider, 0) + 1
            self._failures[provider] = n
            if n >= self.threshold or provider in self._probing:
                self._opened_at[provider] = self.clock()
            self._probing.discard(provider)

    def state(self, provider: str) -> str:
        with self._lock:
            if provider not in self._opened_at:
                return "closed"
            return "half_open" if self.clock() - self._opened_at[provider] >= self.cooldown_s else "open"


class Gateway:
    def __init__(self, providers: Mapping[str, Provider], *, budget: BudgetGuard | None = None,
                 state: GatewayState | None = None, rate_limits: Mapping[str, RateLimit] | None = None,
                 breaker: CircuitBreaker | None = None, max_tries: int = 3, retry_base_s: float = 0.5,
                 max_rate_wait_s: float = 30.0, inflight_timeout_s: float = 600.0, inflight_poll_s: float = 0.2,
                 sleep: Callable[[float], None] = time.sleep, owner: str = "local",
                 provider_caps: Mapping[str, float] | None = None, rate_limited_wait_s: float = 20.0):
        self.providers = dict(providers)
        self.provider_caps = dict(provider_caps or {})   # dollars per run per provider, inside the run's budget
        self.budget = budget
        self.state = state or MemoryGatewayState()
        self.rate_limits = dict(rate_limits or {})
        self.breaker = breaker or CircuitBreaker()
        self.max_tries, self.retry_base_s, self.max_rate_wait_s = max_tries, retry_base_s, max_rate_wait_s
        self.inflight_timeout_s, self.inflight_poll_s = inflight_timeout_s, inflight_poll_s
        self.sleep = sleep
        self.owner = owner
        self.rate_limited_wait_s = rate_limited_wait_s

    def bind_budget(self, budget: BudgetGuard) -> None:
        self.budget = budget

    @staticmethod
    def idempotency_key(request: ModelRequest, scope: CallScope) -> str:
        return content_hash({
            "model": request.model, "system": request.system, "messages": list(request.messages),
            "tools": [dataclasses.asdict(t) for t in request.tools], "max_tokens": request.max_tokens,
            "params": dict(request.params), "tags": dict(request.tags),
            "scope": scope.scope_id, "call_no": scope.call_no,
        })

    def complete(self, scope: CallScope, role: str, request: ModelRequest) -> ModelResponse:
        key = self.idempotency_key(request, scope)
        with obs.tracing.span(f"llm {role}", {"role": role, "model": request.model, "call_no": scope.call_no,
                                              "prompt_version": request.prompt_version}) as span:
            status, stored = self._claim(key, scope.step_run_id)
            if status == "done":
                span.set(cached=True)
                self._log(key, scope, role, request, stored, cached=True, latency=0.0)
                return stored
            try:
                response, latency = self._call(scope, role, request)
            except BaseException:
                self.state.release(key, self.owner)
                raise
            self.state.finish(key, response)
            span.set(cached=False, cost_usd=response.cost_usd, input_tokens=response.usage.input_tokens,
                     output_tokens=response.usage.output_tokens)
            self._log(key, scope, role, request, response, cached=False, latency=latency)
            return response

    def _claim(self, key: str, step_run_id: str | None = None) -> tuple[str, ModelResponse | None]:
        """'done' with the stored reply, or 'mine' once this caller owns the call."""
        deadline = time.monotonic() + self.inflight_timeout_s + 60
        while True:
            status, stored = self.state.claim(key, self.owner, self.inflight_timeout_s, step_run_id)
            if status != "busy":
                return status, stored
            if time.monotonic() > deadline:
                raise Retryable("the same model call is still in flight elsewhere", consume_attempt=False)
            self.sleep(self.inflight_poll_s)

    def _call(self, scope: CallScope, role: str, request: ModelRequest) -> tuple[ModelResponse, float]:
        try:
            provider = self.providers[request.provider]
        except KeyError:
            raise ValueError(f"no provider {request.provider!r} for model {request.model!r}") from None
        if self.budget is None:
            raise NotApproved("gateway has no budget guard bound")
        name = request.provider
        self.breaker.before(name)
        try:
            self._take_rate_token(name)
            estimate = provider.estimate_cost(request)
            cap = self.provider_caps.get(name)
            if cap is not None and self.state.spent(scope.run_id, name) + estimate > cap:
                raise BudgetExceeded(f"{name}: this run's cap of ${cap:.2f} for {name} would be passed")
            token = self.budget.reserve(scope, estimate)
        except BaseException:
            self.breaker.abort(name)        # no call was made, so this was not the probe
            raise
        started = time.monotonic()
        try:
            response = self._call_with_retries(provider, name, request, role)
        except BaseException:
            self.budget.settle(token, 0.0)
            raise
        self.budget.settle(token, response.cost_usd)
        return response, time.monotonic() - started

    def _call_with_retries(self, provider: Provider, name: str, request: ModelRequest, role: str) -> ModelResponse:
        for attempt in range(1, self.max_tries + 1):
            try:
                response = provider.call(request, role=role)
            except ProviderError as e:
                if e.unavailable:
                    raise Retryable(f"{name}: unavailable: {e}", delay_s=60.0, consume_attempt=False) from e
                if not e.retryable:
                    self.breaker.success(name)       # the provider answered; the request itself is bad
                    raise
                self.breaker.failure(name)
                if attempt == self.max_tries or self.breaker.state(name) != "closed":
                    if e.rate_limited:
                        # "too many requests" is the provider asking us to slow down, not a fault of this step:
                        # wait longer and keep the step's attempts (P4 drill rate_limit, decision D-037)
                        raise Retryable(f"{name}: rate limited: {e}", consume_attempt=False,
                                        delay_s=self.rate_limited_wait_s * (0.5 + random.random())) from e
                    raise Retryable(f"{name}: {e}") from e
                self.sleep(self.retry_base_s * 2 ** (attempt - 1) * (0.5 + random.random()))
            else:
                self.breaker.success(name)
                return response
        raise AssertionError("unreachable")

    def _take_rate_token(self, name: str) -> None:
        limit = self.rate_limits.get(name)
        if limit is None:
            return
        waited = 0.0
        while True:
            wait = self.state.take_token(name, limit.capacity, limit.refill_per_s)
            if wait <= 0:
                return
            if waited + wait > self.max_rate_wait_s:
                raise Retryable(f"{name}: rate limit, next token in {wait:.1f}s", delay_s=wait, consume_attempt=False)
            self.sleep(wait)
            waited += wait

    def _log(self, key, scope, role, request, response, *, cached, latency):
        self.state.log(LLMCall(
            idem_key=key, run_id=scope.run_id, step_run_id=scope.step_run_id, scope_id=scope.scope_id,
            call_no=scope.call_no, role=role, model=request.model, prompt_version=request.prompt_version,
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
            cost_usd=0.0 if cached else response.cost_usd, cached=cached, latency_s=latency))

    # -- reporting -------------------------------------------------------------
    @property
    def ledger(self) -> list[LLMCall]:
        return self.state.ledger()

    def spent(self, run_id: str | None = None) -> float:
        return self.state.spent(run_id)

    def double_charges(self) -> int:
        """Idempotency keys paid for more than once. Must always be 0."""
        return self.state.double_charges()

    def cost_by_role(self, run_id: str | None = None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for c in self.state.ledger(run_id):
            r = out.setdefault(c.role, {"paid_calls": 0, "cached_calls": 0, "cost_usd": 0.0, "input_tokens": 0,
                                        "output_tokens": 0})
            r["cached_calls" if c.cached else "paid_calls"] += 1
            r["cost_usd"] = round(r["cost_usd"] + c.cost_usd, 6)
            if not c.cached:
                r["input_tokens"] += c.input_tokens
                r["output_tokens"] += c.output_tokens
        return dict(sorted(out.items()))
