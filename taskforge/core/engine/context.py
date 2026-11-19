"""The object a step function receives as `ctx`."""
from __future__ import annotations

from typing import Any

from ..scope import CallScope
from .outcomes import Resume


class StepContext:
    def __init__(self, *, run_id: str, step_run_id: str, step: str, key: str, attempt: int,
                 resume: Resume | None, services: Any, seq: int = 0):
        self.run_id = run_id
        self.step_run_id = step_run_id
        self.step = step
        self.key = key
        self.attempt = attempt
        self.resume = resume
        self.services = services
        self.seq = seq          # creation order of this step run; the knowledge base only shows it older documents
        self._calls = 0

    @property
    def scope_id(self) -> str:
        return f"{self.run_id}/{self.step}/{self.key}"

    def call_scope(self) -> CallScope:
        """Identity for the next model call. Numbering restarts on every execution of the step,
        so a re-executed step makes the same calls with the same keys and gets cached replies."""
        self._calls += 1
        return CallScope(self.run_id, self.step_run_id, self.scope_id, self._calls)
