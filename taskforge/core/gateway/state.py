"""Where the gateway keeps its state: the reply cache, the cost ledger and the rate-limit buckets.

`MemoryGatewayState` is for one process (tests, P1). `PgGatewayState` is shared by every worker
process, which is what makes "the same request is paid once" hold across processes.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Protocol

from .types import ModelResponse, ToolCall, Usage

if TYPE_CHECKING:
    from .gateway import LLMCall


def response_to_json(r: ModelResponse) -> dict:
    return {"text": r.text, "stop_reason": r.stop_reason, "cost_usd": r.cost_usd,
            "usage": {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
                      "cache_read_tokens": r.usage.cache_read_tokens, "cache_write_tokens": r.usage.cache_write_tokens},
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": dict(c.arguments)} for c in r.tool_calls],
            "raw": [dict(b) for b in r.raw] if r.raw is not None else None, "served_by": r.served_by,
            "refusal": r.refusal}


def response_from_json(d: dict) -> ModelResponse:
    u = d["usage"]
    return ModelResponse(text=d["text"], stop_reason=d["stop_reason"], cost_usd=d["cost_usd"],
                         usage=Usage(u["input_tokens"], u["output_tokens"], u.get("cache_read_tokens", 0),
                                     u.get("cache_write_tokens", 0)),
                         tool_calls=tuple(ToolCall(c["id"], c["name"], c["arguments"]) for c in d["tool_calls"]),
                         raw=tuple(d["raw"]) if d.get("raw") is not None else None, served_by=d.get("served_by", ""),
                         refusal=d.get("refusal"))


class GatewayState(Protocol):
    def claim(self, key: str, owner: str, inflight_timeout_s: float,
              step_run_id: str | None = None) -> tuple[str, ModelResponse | None]:
        """'done' + reply | 'mine' (the caller now owns the call) | 'busy' (someone else is making it).
        A claim whose step run is no longer RUNNING under the claim's owner is dead and can be taken at once."""
    def finish(self, key: str, response: ModelResponse) -> None: ...
    def release(self, key: str, owner: str) -> None: ...
    def take_token(self, bucket: str, capacity: float, refill_per_s: float) -> float:
        """Take one token; returns 0 on success, else the seconds until one is available."""
    def log(self, call: "LLMCall") -> None: ...
    def ledger(self, run_id: str | None = None) -> list["LLMCall"]: ...
    def spent(self, run_id: str | None = None) -> float: ...
    def double_charges(self) -> int: ...


class MemoryGatewayState:
    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._done: dict[str, ModelResponse] = {}
        self._inflight: dict[str, tuple[str, float]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}
        self._ledger: list["LLMCall"] = []

    def claim(self, key, owner, inflight_timeout_s, step_run_id=None):
        with self._lock:
            if key in self._done:
                return "done", self._done[key]
            holder = self._inflight.get(key)
            if holder is None or holder[0] == owner or self._clock() - holder[1] > inflight_timeout_s:
                self._inflight[key] = (owner, self._clock())
                return "mine", None
            return "busy", None

    def finish(self, key, response):
        with self._lock:
            self._done[key] = response
            self._inflight.pop(key, None)

    def release(self, key, owner):
        with self._lock:
            if self._inflight.get(key, ("", 0))[0] == owner:
                del self._inflight[key]

    def take_token(self, bucket, capacity, refill_per_s):
        with self._lock:
            now = self._clock()
            tokens, at = self._buckets.get(bucket, (capacity, now))
            tokens = min(capacity, tokens + (now - at) * refill_per_s)
            if tokens >= 1:
                self._buckets[bucket] = (tokens - 1, now)
                return 0.0
            self._buckets[bucket] = (tokens, now)
            return (1 - tokens) / refill_per_s

    def log(self, call):
        with self._lock:
            self._ledger.append(call)

    def ledger(self, run_id=None):
        with self._lock:
            return [c for c in self._ledger if run_id is None or c.run_id == run_id]

    def spent(self, run_id=None, provider=None):
        return sum(c.cost_usd for c in self.ledger(run_id) if provider is None or c.model.startswith(provider + "/"))

    def double_charges(self):
        paid: dict[str, int] = {}
        for c in self.ledger():
            if not c.cached:
                paid[c.idem_key] = paid.get(c.idem_key, 0) + 1
        return sum(1 for n in paid.values() if n > 1)


class PgGatewayState:
    def __init__(self, db):
        self.db = db

    def claim(self, key, owner, inflight_timeout_s, step_run_id=None):
        with self.db.tx() as c:
            mine = c.execute("INSERT INTO llm_cache (idem_key, status, owner, step_run_id) VALUES (%s, 'IN_FLIGHT', %s, %s) "
                             "ON CONFLICT (idem_key) DO NOTHING RETURNING idem_key", (key, owner, step_run_id)).fetchone()
            if mine:
                return "mine", None
            # stale: too old, or the step run that made the claim is no longer held by the claim's worker
            # (it died or stalled; the sweeper gave the step back to the queue). Lease owners are
            # "<worker>:<thread>", claim owners "<worker>". P4 drill kill_worker, decision D-036.
            row = c.execute(
                "SELECT status, response, owner, claimed_at < now() - %s * interval '1 second' "
                "OR EXISTS (SELECT 1 FROM step_runs s WHERE s.id = llm_cache.step_run_id "
                "           AND NOT (s.status = 'RUNNING' "
                "                    AND left(s.lease_owner, length(llm_cache.owner) + 1) = llm_cache.owner || ':')) AS stale "
                "FROM llm_cache WHERE idem_key = %s FOR UPDATE", (inflight_timeout_s, key)).fetchone()
            if row is None:                       # released between the two statements
                return "busy", None
            if row["status"] == "DONE":
                return "done", response_from_json(row["response"])
            if row["owner"] == owner or row["stale"]:
                c.execute("UPDATE llm_cache SET owner = %s, step_run_id = %s, claimed_at = now() WHERE idem_key = %s",
                          (owner, step_run_id, key))
                return "mine", None
            return "busy", None

    def finish(self, key, response):
        from psycopg.types.json import Jsonb
        with self.db.tx() as c:
            c.execute("INSERT INTO llm_cache (idem_key, status, response, done_at) VALUES (%s, 'DONE', %s, now()) "
                      "ON CONFLICT (idem_key) DO UPDATE SET status = 'DONE', response = EXCLUDED.response, "
                      "done_at = now()", (key, Jsonb(response_to_json(response))))

    def release(self, key, owner):
        with self.db.tx() as c:
            c.execute("DELETE FROM llm_cache WHERE idem_key = %s AND status = 'IN_FLIGHT' AND owner = %s", (key, owner))

    def take_token(self, bucket, capacity, refill_per_s):
        level = "LEAST(capacity, tokens + EXTRACT(EPOCH FROM (now() - updated_at)) * refill_per_s)"
        with self.db.tx() as c:
            c.execute("INSERT INTO rate_buckets (name, tokens, capacity, refill_per_s) VALUES (%s, %s, %s, %s) "
                      "ON CONFLICT (name) DO UPDATE SET capacity = EXCLUDED.capacity, "
                      "refill_per_s = EXCLUDED.refill_per_s", (bucket, capacity, capacity, refill_per_s))
            got = c.execute(f"UPDATE rate_buckets SET tokens = {level} - 1, updated_at = now() "
                            f"WHERE name = %s AND {level} >= 1 RETURNING tokens", (bucket,)).fetchone()
            if got:
                return 0.0
            row = c.execute(f"SELECT {level} AS level FROM rate_buckets WHERE name = %s", (bucket,)).fetchone()
            return max(0.001, (1 - row["level"]) / refill_per_s)

    def log(self, call):
        with self.db.tx() as c:
            c.execute("INSERT INTO llm_calls (idem_key, run_id, step_run_id, scope_id, call_no, role, model, "
                      "prompt_version, input_tokens, output_tokens, cost_usd, cached, latency_s) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (call.idem_key, call.run_id, call.step_run_id, call.scope_id, call.call_no, call.role, call.model,
                       call.prompt_version, call.input_tokens, call.output_tokens, call.cost_usd, call.cached,
                       call.latency_s))

    def ledger(self, run_id=None):
        from .gateway import LLMCall
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM llm_calls WHERE (%(r)s::text IS NULL OR run_id = %(r)s) ORDER BY id",
                             {"r": run_id}).fetchall()
        return [LLMCall(r["idem_key"], r["run_id"], r["step_run_id"], r["scope_id"], r["call_no"], r["role"],
                        r["model"], r["prompt_version"], r["input_tokens"], r["output_tokens"], r["cost_usd"],
                        r["cached"], r["latency_s"]) for r in rows]

    def spent(self, run_id=None, provider=None):
        with self.db.tx() as c:
            return c.execute("SELECT COALESCE(sum(cost_usd), 0) AS s FROM llm_calls "
                             "WHERE (%(r)s::text IS NULL OR run_id = %(r)s) AND (%(p)s::text IS NULL OR model LIKE %(p)s)",
                             {"r": run_id, "p": f"{provider}/%" if provider else None}).fetchone()["s"]

    def double_charges(self):
        with self.db.tx() as c:
            return c.execute("SELECT count(*) AS n FROM (SELECT idem_key FROM llm_calls WHERE NOT cached "
                             "GROUP BY idem_key HAVING count(*) > 1) t").fetchone()["n"]
