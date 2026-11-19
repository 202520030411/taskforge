"""The Postgres engine (DESIGN.md section 4). Same rules as the in-memory engine, safe for many workers.

The four rules that make it safe:
  claim    one statement picks a ready row with FOR UPDATE SKIP LOCKED, marks it RUNNING, sets a
           time-limited lease and raises the row's lease_token
  fence    every write a worker makes to its step is `... WHERE id = ? AND lease_token = ? AND
           status = 'RUNNING'`; a worker whose lease was taken over matches zero rows and its whole
           transaction is rolled back
  commit   a step's result, its successors, the branch counter of whoever waits for it, and the event
           row are written in one transaction
  clock    every time comparison uses the database's now(), never a worker's own clock
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

from .. import obs
from ..errors import BudgetExceeded, NotApproved, Retryable, WorkerCrash
from ..hashing import jsonable
from ..pg import Database
from ..scope import CallScope
from .context import StepContext
from .memory import StepStatus
from .outcomes import BranchResult, Done, Fork, Next, Outcome, Resume, Spawn, Wait
from .workflow import PAID_RESOURCES, Resource, Workflow

log = logging.getLogger(__name__)
ALL_CLASSES = tuple(r.value for r in Resource)
PAID = [r.value for r in PAID_RESOURCES]

CLAIM_SQL = """
WITH picked AS (
    SELECT s.id
    FROM step_runs s JOIN runs r ON r.id = s.run_id
    WHERE s.status = 'PENDING' AND s.available_at <= now() AND s.resource = ANY(%(classes)s)
      AND r.status = 'ACTIVE'
      AND (r.paid_approved OR NOT (s.resource = ANY(%(paid)s)))
      AND (%(run_id)s::text IS NULL OR s.run_id = %(run_id)s)
    ORDER BY s.seq
    FOR UPDATE OF s SKIP LOCKED
    LIMIT 1)
UPDATE step_runs s
SET status = 'RUNNING', attempt = s.attempt + 1, lease_owner = %(owner)s, lease_token = s.lease_token + 1,
    lease_expires_at = now() + %(ttl)s * interval '1 second', started_at = now()
FROM picked WHERE s.id = picked.id
RETURNING s.*
"""


class _Stale(Exception):
    """This worker no longer holds the step: its lease expired and someone else claimed it."""


@dataclass(frozen=True)
class StepView:
    id: str
    seq: int
    run_id: str
    step: str
    key: str
    input: dict
    resource: str
    status: StepStatus
    attempt: int
    output: Any
    error: str | None
    wait_signal: str | None
    join_parent_id: str | None
    open_branches: int
    lease_owner: str | None
    created_at: Any
    started_at: Any
    finished_at: Any


def _view(row: dict) -> StepView:
    return StepView(row["id"], row["seq"], row["run_id"], row["step"], row["key"], row["input"], row["resource"],
                    StepStatus(row["status"]), row["attempt"], row["output"], row["error"], row["wait_signal"],
                    row["join_parent_id"], row["open_branches"], row["lease_owner"], row["created_at"],
                    row["started_at"], row["finished_at"])


class PgEngine:
    def __init__(self, workflow: Workflow, services: Any, db: Database, *, lease_ttl_s: float = 60.0,
                 backoff_s: float = 5.0, worker_id: str | None = None):
        self.workflow = workflow
        self.services = services
        self.db = db
        self.lease_ttl_s = lease_ttl_s
        self.backoff_s = backoff_s
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    def _owner(self) -> str:
        return f"{self.worker_id}:{threading.current_thread().name}"

    # ------------------------------------------------------------------ runs
    def start_run(self, step: str, key: str = "batch", input: dict | None = None, *,
                  paid_approved: bool = False, budget_usd: float = 0.0) -> str:
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        with obs.tracing.span("run start", {"run_id": run_id, "workflow": self.workflow.name}) as h, self.db.tx() as c:
            c.execute("INSERT INTO runs (id, workflow, workflow_version, paid_approved, budget_usd, trace_id) "
                      "VALUES (%s, %s, %s, %s, %s, %s)",
                      (run_id, self.workflow.name, self.workflow.version, paid_approved, budget_usd, h.trace_id))
            self._insert(c, run_id, Spawn(step, key, jsonable(input or {})), None, None, 0, h.trace_id, h.span_id)
        return run_id

    def approve(self, run_id: str, budget_usd: float) -> None:
        """Allow paid steps and set the budget. Also un-pauses a run that hit its budget."""
        with self.db.tx() as c:
            c.execute("UPDATE runs SET paid_approved = true, budget_usd = %s, "
                      "status = CASE WHEN status = 'PAUSED_BUDGET' THEN 'ACTIVE' ELSE status END WHERE id = %s",
                      (budget_usd, run_id))

    def cancel(self, run_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE runs SET status = 'CANCELLED' WHERE id = %s", (run_id,))
            rows = c.execute("UPDATE step_runs SET status = 'CANCELLED', finished_at = now() WHERE run_id = %s "
                             "AND status IN ('PENDING', 'RUNNING', 'WAITING') RETURNING *", (run_id,)).fetchall()
            for row in rows:
                self._event(c, row, None, "CANCELLED", "run cancelled")

    def signal(self, run_id: str, name: str, payload: Any = None, conn=None) -> None:
        """Deliver a signal; if nobody waits for it yet, keep it until someone does.
        Pass `conn` to deliver inside the caller's transaction (a review decision and its signal commit together)."""
        if conn is None:
            with self.db.tx() as c:
                self._signal(c, run_id, name, payload)
        else:
            self._signal(conn, run_id, name, payload)

    def _signal(self, c, run_id: str, name: str, payload: Any) -> None:
        self._signal_lock(c, run_id, name)
        row = c.execute("SELECT * FROM step_runs WHERE run_id = %s AND status = 'WAITING' AND wait_signal = %s "
                        "ORDER BY seq LIMIT 1 FOR UPDATE", (run_id, name)).fetchone()
        if row:
            self._wake(c, row, {"kind": "signal", "state": row["state"], "payload": jsonable(payload)},
                       f"signal {name}")
        else:
            c.execute("INSERT INTO signals (run_id, name, payload) VALUES (%s, %s, %s) "
                      "ON CONFLICT (run_id, name) DO UPDATE SET payload = EXCLUDED.payload",
                      (run_id, name, Jsonb(jsonable(payload))))

    @staticmethod
    def _signal_lock(c, run_id: str, name: str) -> None:
        """Serialise 'a step starts waiting for X' against 'X arrives'. Without it each side can miss the
        other: the waiter checks the mailbox before the signal is stored, and the signal looks for a waiter
        before the wait is committed."""
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{run_id}|{name}",))

    def requeue(self, step_run_id: str) -> None:
        """Operator action: run a FAILED step again."""
        with self.db.tx() as c:
            row = c.execute("UPDATE step_runs SET status = 'PENDING', attempt = 0, error = NULL, available_at = now(), "
                            "finished_at = NULL WHERE id = %s AND status = 'FAILED' RETURNING *",
                            (step_run_id,)).fetchone()
            if not row:
                raise ValueError(f"{step_run_id} is not a FAILED step")
            if row["join_parent_id"]:
                # Its branch was already counted as ended. If the parent is still waiting on that same fork,
                # reopen the branch; otherwise the parent has moved on and this step runs detached.
                reopened = c.execute(
                    "UPDATE step_runs SET open_branches = open_branches + 1 WHERE id = %s AND status = 'WAITING' "
                    "AND wait_signal IS NULL AND fork_gen = %s RETURNING id",
                    (row["join_parent_id"], row["join_gen"])).fetchone()
                if reopened:
                    c.execute("DELETE FROM branch_results WHERE parent_id = %s AND step = %s AND key = %s",
                              (row["join_parent_id"], row["step"], row["key"]))
                else:
                    c.execute("UPDATE step_runs SET join_parent_id = NULL WHERE id = %s", (row["id"],))
            self._event(c, row, "FAILED", "PENDING", "requeued by operator")

    def reopen_branch(self, run_id: str, step: str, key: str, new_key: str) -> str:
        """Operator action: run a step that ended a branch again, under `new_key`, with the same input.
        The branch is put back into the fork it belonged to, so its new outcome replaces the old one in the
        waiting parent's results. Only possible while that parent is still waiting on the same fork."""
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM step_runs WHERE run_id = %s AND step = %s AND key = %s FOR UPDATE",
                            (run_id, step, key)).fetchone()
            if row is None or row["status"] not in ("SUCCEEDED", "FAILED") or not row["join_parent_id"]:
                raise ValueError(f"{step}[{key}] has not ended a branch under a waiting parent")
            parent = c.execute("UPDATE step_runs SET open_branches = open_branches + 1 WHERE id = %s "
                               "AND status = 'WAITING' AND wait_signal IS NULL AND fork_gen = %s RETURNING id",
                               (row["join_parent_id"], row["join_gen"])).fetchone()
            if parent is None:
                raise ValueError("the parent has already moved on; this branch can no longer be re-run")
            gone = c.execute("DELETE FROM branch_results WHERE parent_id = %s AND step = %s AND key = %s",
                             (row["join_parent_id"], step, key)).rowcount
            if gone != 1:
                raise ValueError(f"no recorded result for {step}[{key}] under its parent")
            new_id = self._insert(c, run_id, Spawn(step, new_key, row["input"]), row["join_parent_id"],
                                  row["branch_root_id"], row["join_gen"], row["trace_id"], row["parent_span_id"])
            if new_id is None:
                raise ValueError(f"{step}[{new_key}] already exists; pick another key")
            self._event(c, row, row["status"], row["status"], f"re-run as {new_key} by operator")
        return new_id

    # ------------------------------------------------------- budget guard
    def reserve(self, scope: CallScope, estimate_usd: float) -> tuple[str, int]:
        with self.db.tx() as c:
            ok = c.execute("UPDATE runs SET reserved_usd = reserved_usd + %(e)s WHERE id = %(r)s AND paid_approved "
                           "AND spent_usd + reserved_usd + %(e)s <= budget_usd + 1e-9 RETURNING id",
                           {"e": estimate_usd, "r": scope.run_id}).fetchone()
            if not ok:
                run = c.execute("SELECT paid_approved, budget_usd, spent_usd, reserved_usd FROM runs WHERE id = %s",
                                (scope.run_id,)).fetchone()
                if not run or not run["paid_approved"]:
                    raise NotApproved(f"{scope.run_id} has no paid approval")
                raise BudgetExceeded(f"{scope.run_id}: spent {run['spent_usd']:.4f} + reserved "
                                     f"{run['reserved_usd']:.4f} + {estimate_usd:.4f} > budget {run['budget_usd']:.4f}")
            rid = c.execute("INSERT INTO reservations (run_id, step_run_id, amount) VALUES (%s, %s, %s) RETURNING id",
                            (scope.run_id, scope.step_run_id, estimate_usd)).fetchone()["id"]
        return scope.run_id, rid

    def settle(self, token: tuple[str, int], actual_usd: float) -> None:
        run_id, rid = token
        with self.db.tx() as c:
            r = c.execute("DELETE FROM reservations WHERE id = %s RETURNING amount", (rid,)).fetchone()
            # if the reservation is gone, the step's lease expired and the sweep released it; the money
            # was still spent, so it is still counted
            c.execute("UPDATE runs SET reserved_usd = GREATEST(0, reserved_usd - %s), spent_usd = spent_usd + %s "
                      "WHERE id = %s", (r["amount"] if r else 0.0, actual_usd, run_id))

    def _release_reservations(self, c, step_run_id: str) -> None:
        rows = c.execute("DELETE FROM reservations WHERE step_run_id = %s RETURNING run_id, amount",
                         (step_run_id,)).fetchall()
        for r in rows:
            c.execute("UPDATE runs SET reserved_usd = GREATEST(0, reserved_usd - %s) WHERE id = %s",
                      (r["amount"], r["run_id"]))

    # -------------------------------------------------------------- claiming
    def claim(self, classes: Iterable[str] = ALL_CLASSES, run_id: str | None = None) -> dict | None:
        with self.db.tx() as c:
            row = c.execute(CLAIM_SQL, {"classes": list(classes), "paid": PAID, "run_id": run_id,
                                        "owner": self._owner(), "ttl": self.lease_ttl_s}).fetchone()
            if row:
                self._event(c, row, "PENDING", "RUNNING", f"attempt {row['attempt']}")
        return row

    def heartbeat(self, step_run_id: str, lease_token: int) -> bool:
        """Extend the lease, unless the step has run past its own timeout. Returns False once the lease is lost."""
        with self.db.tx() as c:
            return c.execute(
                "UPDATE step_runs SET lease_expires_at = now() + %s * interval '1 second' "
                "WHERE id = %s AND lease_token = %s AND status = 'RUNNING' "
                "AND now() < started_at + timeout_s * interval '1 second'",
                (self.lease_ttl_s, step_run_id, lease_token)).rowcount == 1

    def sweep(self, limit: int = 200) -> int:
        """Put steps whose lease ran out back in the queue (or fail them when attempts are used up)."""
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM step_runs WHERE status = 'RUNNING' AND lease_expires_at < now() "
                             "ORDER BY seq LIMIT %s FOR UPDATE SKIP LOCKED", (limit,)).fetchall()
            for row in rows:
                self._release_reservations(c, row["id"])
                if row["attempt"] >= row["max_attempts"]:
                    self._fail(c, row, "lease expired on every attempt", guarded=False)
                else:
                    c.execute("UPDATE step_runs SET status = 'PENDING', available_at = now(), lease_expires_at = NULL "
                              "WHERE id = %s", (row["id"],))
                    self._event(c, row, "RUNNING", "PENDING", f"lease expired (held by {row['lease_owner']})")
        return len(rows)

    # ------------------------------------------------------------- executing
    def execute(self, row: dict) -> None:
        sdef = self.workflow.get(row["step"])
        with obs.logs.bind(run_id=row["run_id"], step_run_id=row["id"], step=row["step"], key=row["key"]), \
                obs.tracing.span(f"step {row['step']}",
                                 {"run_id": row["run_id"], "step": row["step"], "key": row["key"],
                                  "step_run_id": row["id"], "attempt": row["attempt"], "resource": row["resource"],
                                  "resumed": bool(row["resume"])},
                                 trace_id=row["trace_id"], parent_span_id=row["parent_span_id"]) as span:
            ctx = StepContext(run_id=row["run_id"], step_run_id=row["id"], step=row["step"], key=row["key"],
                              attempt=row["attempt"], resume=self._load_resume(row), services=self.services,
                              seq=row["seq"])
            try:
                try:
                    outcome = sdef.fn(ctx, row["input"])
                    if not isinstance(outcome, Outcome):
                        raise TypeError(f"step {row['step']!r} returned {type(outcome).__name__}, not an Outcome")
                except WorkerCrash:
                    span.set(outcome="crash")
                    self._abandon(row)
                    return
                except BudgetExceeded as e:
                    span.set(outcome="budget_pause")
                    with self.db.tx() as c:
                        self._guarded(c, row, "status = 'PENDING', attempt = attempt - 1, lease_expires_at = NULL")
                        c.execute("UPDATE runs SET status = 'PAUSED_BUDGET' WHERE id = %s AND status = 'ACTIVE'",
                                  (row["run_id"],))
                        self._event(c, row, "RUNNING", "PENDING", f"run paused: {e}")
                    return
                except Retryable as e:
                    span.set(outcome="retry")
                    self._retry(row, e)
                    return
                except (psycopg.OperationalError, PoolTimeout) as e:
                    # the database went away under the step (restart, network): not a step bug. Run it again
                    # later without using up an attempt (P4 drill restart_postgres, decision D-039). If the
                    # database is still down, recording this fails too and the lease runs out instead.
                    span.set(outcome="retry")
                    self._retry(row, Retryable(f"database connection lost: {type(e).__name__}: {e}",
                                               delay_s=5.0, consume_attempt=False))
                    return
                except Exception as e:  # noqa: BLE001 - a step bug must fail its step, not the worker
                    span.error(f"{type(e).__name__}: {e}")
                    with self.db.tx() as c:
                        self._fail(c, row, "".join(traceback.format_exception(e))[-4000:])
                    return
                span.set(outcome=type(outcome).__name__)
                self._commit(row, outcome, span)
            except _Stale:
                span.set(outcome="fenced")
                log.warning("commit refused: lease on %s was taken over", row["id"])
                with self.db.tx() as c:
                    self._event(c, row, None, "FENCED", "a worker tried to commit after losing its lease")

    def _load_resume(self, row: dict) -> Resume | None:
        r = row["resume"]
        if not r:
            return None
        results: tuple[BranchResult, ...] = ()
        if r["kind"] == "join":
            with self.db.tx() as c:
                rows = c.execute("SELECT * FROM branch_results WHERE parent_id = %s ORDER BY id", (row["id"],)).fetchall()
            results = tuple(BranchResult(b["root_step"], b["root_key"], b["step"], b["key"], b["status"], b["output"],
                                         b["error"]) for b in rows)
        return Resume(r["kind"], r.get("state"), results, r.get("payload"))

    def _guarded(self, c, row: dict, set_sql: str, params: tuple = ()) -> None:
        n = c.execute(f"UPDATE step_runs SET {set_sql} WHERE id = %s AND lease_token = %s AND status = 'RUNNING'",
                      (*params, row["id"], row["lease_token"])).rowcount
        if n != 1:
            raise _Stale(row["id"])

    def _abandon(self, row: dict) -> None:
        """Test hook for WorkerCrash: nothing is committed; the lease is left to expire (immediately)."""
        with self.db.tx() as c:
            c.execute("UPDATE step_runs SET lease_expires_at = now() - interval '1 second' "
                      "WHERE id = %s AND lease_token = %s AND status = 'RUNNING'", (row["id"], row["lease_token"]))

    def _retry(self, row: dict, e: Retryable) -> None:
        attempt = row["attempt"] - (0 if e.consume_attempt else 1)
        with self.db.tx() as c:
            if attempt >= row["max_attempts"]:
                self._fail(c, row, f"retries exhausted: {e}")
                return
            delay = e.delay_s if e.delay_s is not None else self.backoff_s * 2 ** max(0, attempt - 1)
            self._guarded(c, row, "status = 'PENDING', attempt = %s, lease_expires_at = NULL, "
                                  "available_at = now() + %s * interval '1 second'", (attempt, delay))
            self._event(c, row, "RUNNING", "PENDING", f"retry in {delay:g}s: {e}")

    # ---------------------------------------------------------------- commit
    def _commit(self, row: dict, outcome: Outcome, span) -> None:
        try:
            spawns = [Spawn(sp.step, sp.key, jsonable(dict(sp.input))) for sp in getattr(outcome, "spawns", ())]
            for sp in spawns:
                self.workflow.get(sp.step)
            output = jsonable(getattr(outcome, "output", None))
            state = jsonable(getattr(outcome, "state", None))
        except Exception as e:  # noqa: BLE001
            with self.db.tx() as c:
                self._fail(c, row, f"invalid outcome from {row['step']!r}: {e!r}")
            return
        with self.db.tx() as c:
            if isinstance(outcome, (Done, Next)):
                self._guarded(c, row, "status = 'SUCCEEDED', output = %s, resume = NULL, finished_at = now(), "
                                      "lease_expires_at = NULL", (Jsonb(output),))
                names = ",".join(f"{s.step}[{s.key}]" for s in spawns)
                self._event(c, row, "RUNNING", "SUCCEEDED", f"next: {names}" if names else "done")
                created = [i for sp in spawns if (i := self._insert(c, row["run_id"], sp, row["join_parent_id"],
                                                                    row["branch_root_id"], row["join_gen"],
                                                                    span.trace_id, span.span_id))]
                if not created:
                    self._end_branch(c, row, "SUCCEEDED", output, None)
                elif row["join_parent_id"] and len(created) > 1:      # this branch continues as several
                    c.execute("UPDATE step_runs SET open_branches = open_branches + %s WHERE id = %s AND fork_gen = %s",
                              (len(created) - 1, row["join_parent_id"], row["join_gen"]))
            elif isinstance(outcome, Fork):
                self._guarded(c, row, "status = 'WAITING', state = %s, wait_signal = NULL, resume = NULL, "
                                      "open_branches = 0, fork_gen = fork_gen + 1, lease_expires_at = NULL",
                              (Jsonb(state),))
                gen = row["fork_gen"] + 1
                c.execute("DELETE FROM branch_results WHERE parent_id = %s", (row["id"],))
                self._event(c, row, "RUNNING", "WAITING", f"fork: {len(spawns)}")
                created = [i for sp in spawns if (i := self._insert(c, row["run_id"], sp, row["id"], "self", gen,
                                                                    span.trace_id, span.span_id))]
                if created:
                    c.execute("UPDATE step_runs SET open_branches = %s WHERE id = %s", (len(created), row["id"]))
                else:
                    self._wake(c, {**row, "status": "WAITING"}, {"kind": "join", "state": state},
                               "fork created nothing new")
            elif isinstance(outcome, Wait):
                self._signal_lock(c, row["run_id"], outcome.signal)
                self._guarded(c, row, "status = 'WAITING', state = %s, wait_signal = %s, resume = NULL, "
                                      "lease_expires_at = NULL", (Jsonb(state), outcome.signal))
                self._event(c, row, "RUNNING", "WAITING", f"wait: {outcome.signal}")
                early = c.execute("DELETE FROM signals WHERE run_id = %s AND name = %s RETURNING payload",
                                  (row["run_id"], outcome.signal)).fetchone()
                if early:
                    self._wake(c, {**row, "status": "WAITING"},
                               {"kind": "signal", "state": state, "payload": early["payload"]},
                               f"signal {outcome.signal} (was waiting)")

    def _insert(self, c, run_id: str, spawn: Spawn, join_parent_id: str | None, branch_root_id: str | None,
                join_gen: int, trace_id: str | None, parent_span_id: str | None) -> str | None:
        sdef = self.workflow.get(spawn.step)
        sid = f"sr-{uuid.uuid4().hex[:14]}"
        row = c.execute(
            "INSERT INTO step_runs (id, run_id, step, key, input, resource, max_attempts, timeout_s, join_parent_id, "
            "branch_root_id, join_gen, trace_id, parent_span_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, step, key) DO NOTHING RETURNING *",
            (sid, run_id, spawn.step, spawn.key, Jsonb(dict(spawn.input)), sdef.resource.value, sdef.max_attempts,
             sdef.timeout_s, join_parent_id, sid if branch_root_id == "self" else branch_root_id, join_gen, trace_id,
             parent_span_id)).fetchone()
        if row is None:
            return None
        self._event(c, row, None, "PENDING", "created")
        return sid

    def _end_branch(self, c, row: dict, status: str, output: Any, error: str | None) -> None:
        parent_id = row["join_parent_id"]
        if not parent_id:
            return
        root_step, root_key = row["step"], row["key"]
        if row["branch_root_id"] and row["branch_root_id"] != row["id"]:
            root = c.execute("SELECT step, key FROM step_runs WHERE id = %s", (row["branch_root_id"],)).fetchone()
            root_step, root_key = root["step"], root["key"]
        # One atomic decrement on the parent's row. Postgres locks the row for the rest of this transaction,
        # so when the last two branches end at the same moment exactly one of them sees 0. The fork_gen check
        # ignores a branch that belongs to an earlier fork of the parent (a late or requeued step).
        parent = c.execute("UPDATE step_runs SET open_branches = open_branches - 1 WHERE id = %s AND fork_gen = %s "
                           "AND status = 'WAITING' AND wait_signal IS NULL RETURNING *",
                           (parent_id, row["join_gen"])).fetchone()
        if parent is None:
            return
        c.execute("INSERT INTO branch_results (parent_id, root_step, root_key, step, key, status, output, error) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                  (parent_id, root_step, root_key, row["step"], row["key"], status, Jsonb(output), error))
        if parent["open_branches"] == 0:
            self._wake(c, parent, {"kind": "join", "state": parent["state"]}, "all branches ended")

    def _wake(self, c, row: dict, resume: dict, detail: str) -> None:
        c.execute("UPDATE step_runs SET status = 'PENDING', resume = %s, wait_signal = NULL, attempt = 0, "
                  "available_at = now() WHERE id = %s AND status = 'WAITING'", (Jsonb(resume), row["id"]))
        self._event(c, row, "WAITING", "PENDING", detail)

    def _fail(self, c, row: dict, error: str, *, guarded: bool = True) -> None:
        if guarded:
            self._guarded(c, row, "status = 'FAILED', error = %s, finished_at = now(), lease_expires_at = NULL",
                          (error,))
        else:
            c.execute("UPDATE step_runs SET status = 'FAILED', error = %s, finished_at = now(), "
                      "lease_expires_at = NULL WHERE id = %s", (error, row["id"]))
        last = error.strip().splitlines()[-1][:200] if error.strip() else "failed"
        self._event(c, row, "RUNNING", "FAILED", last)
        self._end_branch(c, row, "FAILED", None, error)

    def _event(self, c, row: dict, before: str | None, to: str, detail: str) -> None:
        c.execute("INSERT INTO events (run_id, step_run_id, step, key, from_status, to_status, detail, worker) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                  (row["run_id"], row["id"], row["step"], row["key"], before, to, detail, self._owner()))

    # ---------------------------------------------- single-thread driver (tests)
    def run_until_idle(self, run_id: str | None = None, *, max_executions: int = 1_000_000,
                       fast_forward: bool = True) -> int:
        """Execute ready steps in this thread until none is ready. With `fast_forward`, retry delays are
        skipped instead of slept through (tests only; real workers wait)."""
        executed = 0
        while executed < max_executions:
            row = self.claim(ALL_CLASSES, run_id)
            if row is None:
                if self.sweep() or (fast_forward and self._skip_delays(run_id)):
                    continue
                break
            self.execute(row)
            executed += 1
        return executed

    def _skip_delays(self, run_id: str | None) -> int:
        with self.db.tx() as c:
            return c.execute("UPDATE step_runs SET available_at = now() WHERE status = 'PENDING' "
                             "AND available_at > now() AND (%(r)s::text IS NULL OR run_id = %(r)s)",
                             {"r": run_id}).rowcount

    # ----------------------------------------------------------------- views
    def find(self, run_id: str, step: str | None = None, status: StepStatus | None = None) -> list[StepView]:
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM step_runs WHERE run_id = %(r)s AND (%(s)s::text IS NULL OR step = %(s)s) "
                             "AND (%(st)s::text IS NULL OR status = %(st)s) ORDER BY seq",
                             {"r": run_id, "s": step, "st": status.value if status else None}).fetchall()
        return [_view(r) for r in rows]

    def all_steps(self, run_id: str) -> list[StepView]:
        return self.find(run_id)

    def get(self, run_id: str, step: str, key: str) -> StepView | None:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM step_runs WHERE run_id = %s AND step = %s AND key = %s",
                            (run_id, step, key)).fetchone()
        return _view(row) if row else None

    def get_step(self, step_run_id: str) -> StepView | None:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM step_runs WHERE id = %s", (step_run_id,)).fetchone()
        return _view(row) if row else None

    def run_info(self, run_id: str) -> dict:
        with self.db.tx() as c:
            run = c.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        return dict(run)

    def list_runs(self) -> list[dict]:
        with self.db.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY seq DESC").fetchall()]

    def recent_events(self, run_id: str, limit: int = 50) -> list[dict]:
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM events WHERE run_id = %s ORDER BY seq DESC LIMIT %s",
                             (run_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def summary(self, run_id: str) -> dict:
        run = self.run_info(run_id)
        with self.db.tx() as c:
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, count(*) AS n FROM step_runs WHERE run_id = %s GROUP BY status", (run_id,)).fetchall()}
            waiting = [r["wait_signal"] for r in c.execute(
                "SELECT wait_signal FROM step_runs WHERE run_id = %s AND status = 'WAITING' AND wait_signal IS NOT NULL "
                "ORDER BY wait_signal", (run_id,)).fetchall()]
            blocked = c.execute("SELECT count(*) AS n FROM step_runs WHERE run_id = %s AND status = 'PENDING' "
                                "AND resource = ANY(%s)", (run_id, PAID)).fetchone()["n"]
        return {
            "run_id": run_id,
            "status": run["status"],
            "finished": not any(counts.get(s) for s in ("PENDING", "RUNNING", "WAITING")),
            "counts": counts,
            "waiting_on_signals": waiting,
            "blocked_paid_steps": 0 if run["paid_approved"] else blocked,
            "spent_usd": round(run["spent_usd"], 6),
            "budget_usd": run["budget_usd"],
        }
