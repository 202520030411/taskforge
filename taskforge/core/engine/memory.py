"""Single-process, in-memory engine (DESIGN.md section 4, P1 version).

It follows the same rules as the Postgres engine planned for P2, minus leases:
  - one row per (run, step, key); spawning an existing one does nothing
  - a step's result and its successors are recorded together, or not at all
  - Fork / Wait pause a step; the last branch to end, or the signal, wakes it
  - paid steps are only claimed in runs with paid approval
  - a crashed attempt (WorkerCrash) leaves no trace except a used-up attempt

Time is virtual: when nothing is ready but a retry is scheduled for later,
the clock jumps forward, so tests with back-off delays run instantly.
"""
from __future__ import annotations

import itertools
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import BudgetExceeded, NotApproved, Retryable, WorkerCrash
from ..hashing import jsonable
from .context import StepContext
from .outcomes import BranchResult, Done, Fork, Next, Outcome, Resume, Spawn, Wait
from .workflow import PAID_RESOURCES, Resource, Workflow


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


OPEN = frozenset({StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING})


class RunStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    CANCELLED = "CANCELLED"


@dataclass
class Run:
    id: str
    paid_approved: bool
    budget_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    status: RunStatus = RunStatus.ACTIVE


@dataclass
class StepRun:
    id: str
    seq: int
    run_id: str
    step: str
    key: str
    input: dict
    resource: Resource
    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    available_at: float = 0.0
    output: Any = None
    error: str | None = None
    join_parent_id: str | None = None   # who is waiting for this branch to end
    branch_root_id: str | None = None   # the child a Fork created at the top of this branch
    open_branches: int = 0              # branches this step is waiting on
    fork_gen: int = 0                   # how many times this step has forked
    join_gen: int = 0                   # which fork of the parent this branch belongs to
    branch_results: list[BranchResult] = field(default_factory=list)
    wait_signal: str | None = None
    state: Any = None
    resume: Resume | None = None


@dataclass(frozen=True)
class Event:
    seq: int
    at: float
    run_id: str
    step_run_id: str
    step: str
    key: str
    from_status: str | None
    to_status: str
    detail: str = ""


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def advance_to(self, t: float) -> None:
        self.now = max(self.now, t)


class InMemoryEngine:
    def __init__(self, workflow: Workflow, services: Any = None, *, backoff_s: float = 5.0):
        self.workflow = workflow
        self.services = services
        self.backoff_s = backoff_s
        self.clock = VirtualClock()
        self.runs: dict[str, Run] = {}
        self.steps: dict[str, StepRun] = {}
        self._index: dict[tuple[str, str, str], str] = {}
        self._mailbox: dict[tuple[str, str], Any] = {}
        self.events: list[Event] = []
        self._seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    # ------------------------------------------------------------------ runs
    def start_run(self, step: str, key: str = "batch", input: dict | None = None, *,
                  paid_approved: bool = False, budget_usd: float = 0.0) -> str:
        run_id = f"run-{len(self.runs) + 1:04d}"
        self.runs[run_id] = Run(run_id, paid_approved, budget_usd)
        self._insert(run_id, Spawn(step, key, input or {}), join_parent_id=None, branch_root_id=None, join_gen=0)
        return run_id

    def approve(self, run_id: str, budget_usd: float) -> None:
        """Allow paid steps and set the budget. Also un-pauses a run that hit its budget."""
        run = self.runs[run_id]
        run.paid_approved = True
        run.budget_usd = budget_usd
        if run.status == RunStatus.PAUSED_BUDGET:
            run.status = RunStatus.ACTIVE

    def cancel(self, run_id: str) -> None:
        self.runs[run_id].status = RunStatus.CANCELLED
        for sr in self.steps.values():
            if sr.run_id == run_id and sr.status in OPEN:
                self._transition(sr, StepStatus.CANCELLED, "run cancelled")

    def signal(self, run_id: str, name: str, payload: Any = None) -> None:
        """Deliver a signal; if nobody waits for it yet, keep it until someone does."""
        for sr in self.steps.values():
            if sr.run_id == run_id and sr.status == StepStatus.WAITING and sr.wait_signal == name:
                self._wake(sr, Resume("signal", sr.state, (), jsonable(payload)), f"signal {name}")
                return
        self._mailbox[(run_id, name)] = jsonable(payload)

    def requeue(self, step_run_id: str) -> None:
        """Operator action: run a FAILED step again."""
        sr = self.steps[step_run_id]
        if sr.status != StepStatus.FAILED:
            raise ValueError(f"{step_run_id} is {sr.status.value}, only FAILED steps can be requeued")
        if sr.join_parent_id is not None:
            # Its branch was already counted as ended. If the parent is still waiting on that same fork,
            # reopen the branch; otherwise the parent has moved on and this step runs detached.
            parent = self.steps[sr.join_parent_id]
            if parent.status == StepStatus.WAITING and parent.wait_signal is None and parent.fork_gen == sr.join_gen:
                parent.open_branches += 1
                parent.branch_results = [b for b in parent.branch_results if (b.step, b.key) != (sr.step, sr.key)]
            else:
                sr.join_parent_id = None
        sr.attempt = 0
        sr.error = None
        self._transition(sr, StepStatus.PENDING, "requeued by operator")

    def reopen_branch(self, run_id: str, step: str, key: str, new_key: str) -> str:
        """Same rule as PgEngine.reopen_branch."""
        sr = self.get(run_id, step, key)
        if sr is None or sr.status not in (StepStatus.SUCCEEDED, StepStatus.FAILED) or sr.join_parent_id is None:
            raise ValueError(f"{step}[{key}] has not ended a branch under a waiting parent")
        parent = self.steps[sr.join_parent_id]
        if parent.status != StepStatus.WAITING or parent.wait_signal is not None or parent.fork_gen != sr.join_gen:
            raise ValueError("the parent has already moved on; this branch can no longer be re-run")
        before = len(parent.branch_results)
        parent.branch_results = [b for b in parent.branch_results if (b.step, b.key) != (step, key)]
        if len(parent.branch_results) != before - 1:
            raise ValueError(f"no recorded result for {step}[{key}] under its parent")
        child = self._insert(run_id, Spawn(step, new_key, dict(sr.input)), sr.join_parent_id, sr.branch_root_id,
                             sr.join_gen)
        if child is None:
            raise ValueError(f"{step}[{new_key}] already exists; pick another key")
        parent.open_branches += 1
        self._event(sr, sr.status, sr.status, f"re-run as {new_key} by operator")
        return child.id

    # ------------------------------------------------------- budget guard
    def reserve(self, scope, estimate_usd: float) -> tuple[str, float]:
        """Set money aside for one model call. Returns a token to pass to settle()."""
        run = self.runs[scope.run_id]
        if not run.paid_approved:
            raise NotApproved(f"{run.id} has no paid approval")
        if run.spent_usd + run.reserved_usd + estimate_usd > run.budget_usd + 1e-12:
            raise BudgetExceeded(f"{run.id}: spent {run.spent_usd:.4f} + reserved {run.reserved_usd:.4f} "
                                 f"+ {estimate_usd:.4f} > budget {run.budget_usd:.4f}")
        run.reserved_usd += estimate_usd
        return run.id, estimate_usd

    def settle(self, token: tuple[str, float], actual_usd: float) -> None:
        run_id, estimate_usd = token
        run = self.runs[run_id]
        run.reserved_usd = max(0.0, run.reserved_usd - estimate_usd)
        run.spent_usd += actual_usd

    # ----------------------------------------------------------- running
    def run_until_idle(self, run_id: str | None = None, *, max_executions: int = 1_000_000,
                       fast_forward: bool = True) -> int:
        """Execute ready steps until none is ready. Returns how many executions happened."""
        executed = 0
        while executed < max_executions:
            sr = self._next_ready(run_id)
            if sr is None:
                later = [s.available_at for s in self._candidates(run_id) if s.available_at > self.clock.time()]
                if not later:
                    break
                self.clock.advance_to(min(later))
                continue
            self._execute(sr)
            executed += 1
        return executed

    def _candidates(self, run_id: str | None):
        for sr in self.steps.values():   # dicts keep insertion order = creation order
            if run_id is not None and sr.run_id != run_id:
                continue
            if sr.status != StepStatus.PENDING:
                continue
            run = self.runs[sr.run_id]
            if run.status != RunStatus.ACTIVE:
                continue
            if sr.resource in PAID_RESOURCES and not run.paid_approved:
                continue
            yield sr

    def _next_ready(self, run_id: str | None) -> StepRun | None:
        now = self.clock.time()
        return next((sr for sr in self._candidates(run_id) if sr.available_at <= now), None)

    def _execute(self, sr: StepRun) -> None:
        sdef = self.workflow.get(sr.step)
        run = self.runs[sr.run_id]
        sr.attempt += 1
        self._transition(sr, StepStatus.RUNNING, f"attempt {sr.attempt}")
        ctx = StepContext(run_id=sr.run_id, step_run_id=sr.id, step=sr.step, key=sr.key,
                          attempt=sr.attempt, resume=sr.resume, services=self.services, seq=sr.seq)
        try:
            outcome = sdef.fn(ctx, sr.input)
            if not isinstance(outcome, Outcome):
                raise TypeError(f"step {sr.step!r} returned {type(outcome).__name__}, not an Outcome")
        except WorkerCrash:
            if sr.attempt >= sdef.max_attempts:
                self._fail(sr, "worker crashed on every attempt")
            else:
                self._transition(sr, StepStatus.PENDING, "claim expired: worker crashed")
            return
        except BudgetExceeded as e:
            sr.attempt -= 1
            run.status = RunStatus.PAUSED_BUDGET
            self._transition(sr, StepStatus.PENDING, f"run paused: {e}")
            return
        except Retryable as e:
            if not e.consume_attempt:
                sr.attempt -= 1
            if sr.attempt >= sdef.max_attempts:
                self._fail(sr, f"retries exhausted: {e}")
                return
            delay = e.delay_s if e.delay_s is not None else self.backoff_s * 2 ** max(0, sr.attempt - 1)
            sr.available_at = self.clock.time() + delay
            self._transition(sr, StepStatus.PENDING, f"retry in {delay:g}s: {e}")
            return
        except Exception as e:  # noqa: BLE001 - a step bug must fail its step, not the engine
            self._fail(sr, "".join(traceback.format_exception(e))[-4000:])
            return
        self._commit(sr, outcome)

    # ------------------------------------------------------------ commit
    def _commit(self, sr: StepRun, outcome: Outcome) -> None:
        """Apply an outcome. Everything is validated first, so a bad outcome changes nothing but the step's status."""
        try:
            spawns = getattr(outcome, "spawns", ())
            for sp in spawns:
                self.workflow.get(sp.step)
            inputs = [jsonable(dict(sp.input)) for sp in spawns]
            output = jsonable(getattr(outcome, "output", None))
            state = jsonable(getattr(outcome, "state", None))
        except Exception as e:  # noqa: BLE001
            self._fail(sr, f"invalid outcome from {sr.step!r}: {e!r}")
            return
        spawns = [Spawn(sp.step, sp.key, inp) for sp, inp in zip(spawns, inputs)]
        sr.resume = None

        if isinstance(outcome, (Done, Next)):
            sr.output = output
            names = ",".join(f"{s.step}[{s.key}]" for s in spawns)
            self._transition(sr, StepStatus.SUCCEEDED, f"next: {names}" if names else "done")
            created = [c for sp in spawns
                       if (c := self._insert(sr.run_id, sp, sr.join_parent_id, sr.branch_root_id, sr.join_gen)) is not None]
            if not created:
                self._end_branch(sr)
            elif sr.join_parent_id is not None and self.steps[sr.join_parent_id].fork_gen == sr.join_gen:
                # this branch continues as len(created) branches
                self.steps[sr.join_parent_id].open_branches += len(created) - 1
        elif isinstance(outcome, Fork):
            sr.state = state
            sr.wait_signal = None
            sr.branch_results = []
            sr.fork_gen += 1
            self._transition(sr, StepStatus.WAITING, f"fork: {len(spawns)}")
            created = []
            for sp in spawns:
                child = self._insert(sr.run_id, sp, sr.id, None, sr.fork_gen)
                if child is not None:
                    child.branch_root_id = child.id
                    created.append(child)
            sr.open_branches = len(created)
            if not created:
                self._wake(sr, Resume("join", sr.state, ()), "fork created nothing new")
        elif isinstance(outcome, Wait):
            sr.state = state
            sr.wait_signal = outcome.signal
            self._transition(sr, StepStatus.WAITING, f"wait: {outcome.signal}")
            pending = self._mailbox.pop((sr.run_id, outcome.signal), _MISSING)
            if pending is not _MISSING:
                self._wake(sr, Resume("signal", sr.state, (), pending), f"signal {outcome.signal} (was waiting)")
        else:  # pragma: no cover - guarded in _execute
            self._fail(sr, f"unknown outcome {outcome!r}")

    def _insert(self, run_id: str, spawn: Spawn, join_parent_id: str | None,
                branch_root_id: str | None, join_gen: int) -> StepRun | None:
        key = (run_id, spawn.step, spawn.key)
        if key in self._index:
            return None
        seq = next(self._seq)
        sr = StepRun(id=f"sr-{seq:06d}", seq=seq, run_id=run_id, step=spawn.step, key=spawn.key,
                     input=dict(spawn.input), resource=self.workflow.get(spawn.step).resource,
                     available_at=self.clock.time(), join_parent_id=join_parent_id,
                     branch_root_id=branch_root_id, join_gen=join_gen)
        self.steps[sr.id] = sr
        self._index[key] = sr.id
        self._event(sr, None, StepStatus.PENDING, "created")
        return sr

    def _end_branch(self, sr: StepRun) -> None:
        if sr.join_parent_id is None:
            return
        parent = self.steps[sr.join_parent_id]
        if parent.fork_gen != sr.join_gen or parent.status != StepStatus.WAITING or parent.wait_signal is not None:
            return      # a late or requeued branch of an earlier fork: the parent has moved on
        root = self.steps[sr.branch_root_id] if sr.branch_root_id else sr
        parent.branch_results.append(BranchResult(root.step, root.key, sr.step, sr.key, sr.status.value,
                                                  sr.output, sr.error))
        parent.open_branches -= 1
        if parent.open_branches == 0:
            self._wake(parent, Resume("join", parent.state, tuple(parent.branch_results)), "all branches ended")

    def _wake(self, sr: StepRun, resume: Resume, detail: str) -> None:
        sr.resume = resume
        sr.wait_signal = None
        sr.attempt = 0
        sr.available_at = self.clock.time()
        self._transition(sr, StepStatus.PENDING, detail)

    def _fail(self, sr: StepRun, error: str) -> None:
        sr.error = error
        self._transition(sr, StepStatus.FAILED, error.strip().splitlines()[-1][:200] if error.strip() else "failed")
        self._end_branch(sr)

    def _transition(self, sr: StepRun, to: StepStatus, detail: str = "") -> None:
        before = sr.status
        sr.status = to
        self._event(sr, before, to, detail)

    def _event(self, sr: StepRun, before: StepStatus | None, to: StepStatus, detail: str) -> None:
        self.events.append(Event(next(self._event_seq), self.clock.time(), sr.run_id, sr.id, sr.step, sr.key,
                                 before.value if before else None, to.value, detail))

    # ------------------------------------------------------------- views
    def find(self, run_id: str, step: str | None = None, status: StepStatus | None = None) -> list[StepRun]:
        return [sr for sr in self.steps.values()
                if sr.run_id == run_id and (step is None or sr.step == step)
                and (status is None or sr.status == status)]

    @property
    def last_seq(self) -> int:
        """Creation number of the newest step run (the knowledge base stamps new documents with it)."""
        return len(self.steps)

    def all_steps(self, run_id: str) -> list[StepRun]:
        return self.find(run_id)

    def get(self, run_id: str, step: str, key: str) -> StepRun | None:
        sid = self._index.get((run_id, step, key))
        return self.steps[sid] if sid else None

    def get_step(self, step_run_id: str) -> StepRun | None:
        return self.steps.get(step_run_id)

    def run_info(self, run_id: str) -> dict:
        run = self.runs[run_id]
        return {"id": run.id, "status": run.status.value, "paid_approved": run.paid_approved,
                "budget_usd": run.budget_usd, "spent_usd": run.spent_usd, "reserved_usd": run.reserved_usd,
                "workflow": self.workflow.name, "workflow_version": self.workflow.version, "created_at": None}

    def list_runs(self) -> list[dict]:
        return [self.run_info(r) for r in reversed(list(self.runs))]

    def recent_events(self, run_id: str, limit: int = 50) -> list[dict]:
        rows = [e for e in self.events if e.run_id == run_id][-limit:]
        return [{"seq": e.seq, "at": e.at, "run_id": e.run_id, "step_run_id": e.step_run_id, "step": e.step,
                 "key": e.key, "from_status": e.from_status, "to_status": e.to_status, "detail": e.detail,
                 "worker": "local"} for e in rows]

    def summary(self, run_id: str) -> dict:
        run = self.runs[run_id]
        steps = [sr for sr in self.steps.values() if sr.run_id == run_id]
        counts: dict[str, int] = {}
        for sr in steps:
            counts[sr.status.value] = counts.get(sr.status.value, 0) + 1
        blocked = sum(1 for sr in steps if sr.status == StepStatus.PENDING
                      and sr.resource in PAID_RESOURCES and not run.paid_approved)
        return {
            "run_id": run_id,
            "status": run.status.value,
            "finished": not any(sr.status in OPEN for sr in steps),
            "counts": counts,
            "waiting_on_signals": sorted(sr.wait_signal for sr in steps
                                         if sr.status == StepStatus.WAITING and sr.wait_signal),
            "blocked_paid_steps": blocked,
            "spent_usd": round(run.spent_usd, 6),
            "budget_usd": run.budget_usd,
        }


_MISSING = object()
