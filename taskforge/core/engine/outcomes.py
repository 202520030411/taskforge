"""What a step function returns (DESIGN.md section 4.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class Spawn:
    """Ask the engine to create step run (step, key) with this input. Duplicates are ignored."""
    step: str
    key: str
    input: Mapping[str, Any] = field(default_factory=dict)


class Outcome:
    __slots__ = ()


@dataclass(frozen=True)
class Done(Outcome):
    """Finished; this branch ends here."""
    output: Any = None


@dataclass(frozen=True, init=False)
class Next(Outcome):
    """Finished; continue with one following step (a chain) or several (a fan-out)."""
    spawns: tuple[Spawn, ...]
    output: Any

    def __init__(self, *spawns: Spawn, output: Any = None):
        object.__setattr__(self, "spawns", tuple(spawns))
        object.__setattr__(self, "output", output)


@dataclass(frozen=True, init=False)
class Fork(Outcome):
    """Start children and pause until every one of their branches has ended.

    The step function is then called again with `ctx.resume.kind == "join"`,
    the children's results, and the `state` saved here.
    """
    spawns: tuple[Spawn, ...]
    state: Any

    def __init__(self, *spawns: Spawn, state: Any = None):
        object.__setattr__(self, "spawns", tuple(spawns))
        object.__setattr__(self, "state", state)


@dataclass(frozen=True)
class Wait(Outcome):
    """Pause until `signal` arrives; then called again with `ctx.resume.kind == "signal"`."""
    signal: str
    state: Any = None


@dataclass(frozen=True)
class BranchResult:
    """How one branch under a Fork ended."""
    root_step: str        # the child the Fork created
    root_key: str
    step: str             # the step that ended the branch (may be further down a chain)
    key: str
    status: str           # "SUCCEEDED" or "FAILED"
    output: Any = None
    error: str | None = None


@dataclass(frozen=True)
class Resume:
    kind: Literal["join", "signal"]
    state: Any = None
    results: tuple[BranchResult, ...] = ()
    payload: Any = None
