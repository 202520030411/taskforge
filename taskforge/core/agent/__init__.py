"""Agent runtime (DESIGN.md section 8): the tool-calling loop and the trajectory format it records."""
from .loop import (FINISH, READ_FILE, RUN_SHELL, SOLVER_TOOLS, WRITE_FILE, AgentBudget, AgentResult, execute_tool,
                   run_agent)
from .trajectory import AgentStep, ToolCallRecord

__all__ = ["AgentBudget", "AgentResult", "AgentStep", "FINISH", "READ_FILE", "RUN_SHELL", "SOLVER_TOOLS",
           "ToolCallRecord", "WRITE_FILE", "execute_tool", "run_agent"]
