"""The trajectory format the agent runtime records (a subset of ATIF v1.8, the format of the stored runs)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    arguments: dict[str, Any]


class AgentStep(BaseModel):
    """One turn. `message` is the model's own text (its reasoning); judges never see it."""
    model_config = ConfigDict(frozen=True)
    source: Literal["system", "user", "agent"]
    message: str = ""
    tool_calls: tuple[ToolCallRecord, ...] = ()
    observation: str | None = None
