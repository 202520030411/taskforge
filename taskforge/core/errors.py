"""Exception types shared by the engine, the gateway and the sandbox.

The engine treats these differently (DESIGN.md section 4.2):
  Retryable       infrastructure fault; the step runs again later
  BudgetExceeded  the run pauses; the step is not failed and its attempt is not used up
  WorkerCrash     test hook that behaves like the worker process dying mid-step
  anything else   bug or bad input; the step run is marked FAILED
"""
from __future__ import annotations


class Retryable(Exception):
    """An infrastructure fault: run the step again later. A failing test is never Retryable."""

    def __init__(self, message: str = "", *, delay_s: float | None = None, consume_attempt: bool = True):
        super().__init__(message)
        self.delay_s = delay_s
        self.consume_attempt = consume_attempt


class BudgetExceeded(Exception):
    """A paid call would push the run past its budget."""


class NotApproved(Exception):
    """A paid call was attempted in a run without paid approval.

    The engine never claims paid steps in such a run, so reaching this means a
    step of a free class tried to call a model - a bug, not a condition to retry.
    """


class ModelOutputError(Exception):
    """The model's reply could not be turned into the structure the caller needs."""


class WorkerCrash(BaseException):
    """Test hook: behaves like the worker process dying in the middle of a step.

    It derives from BaseException so that `except Exception` inside step code
    cannot swallow it - just as step code cannot catch its own process being killed.
    """
