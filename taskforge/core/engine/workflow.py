"""Step declarations: a workflow is a named set of step functions with a resource class each."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class Resource(str, Enum):
    CPU = "cpu"
    LLM = "llm"
    SANDBOX = "sandbox"
    AGENT = "agent"       # model + sandbox
    HUMAN = "human"


# Steps of these classes spend money; they are only claimed in runs with paid approval (section 4.9).
PAID_RESOURCES = frozenset({Resource.LLM, Resource.AGENT})


@dataclass(frozen=True)
class StepDef:
    name: str
    fn: Callable[[Any, dict], Any]
    resource: Resource
    max_attempts: int = 3
    timeout_s: float = 900.0


class Workflow:
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self.steps: dict[str, StepDef] = {}

    def step(self, name: str | None = None, *, resource: Resource = Resource.CPU,
             max_attempts: int = 3, timeout_s: float = 900.0):
        """Decorator: register `fn` as a step."""
        def register(fn):
            step_name = name or fn.__name__
            if step_name in self.steps:
                raise ValueError(f"step {step_name!r} registered twice")
            self.steps[step_name] = StepDef(step_name, fn, resource, max_attempts, timeout_s)
            return fn
        return register

    def get(self, name: str) -> StepDef:
        try:
            return self.steps[name]
        except KeyError:
            raise KeyError(f"workflow {self.name!r} has no step {name!r}") from None
