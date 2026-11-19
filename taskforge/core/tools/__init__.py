"""Tool registry (DESIGN.md section 8).

A tool is defined once (name, description, JSON schema, handler) and is usable by the
system's own agents and, through core.tools.mcp, by outside MCP clients.
"""
from .registry import (DEFAULT_REGISTRY, FINISH, KB_SEARCH, READ_FILE, RUN_SHELL, SOLVER_TOOLS, WRITE_FILE, Tool,
                       ToolContext, ToolRegistry)

__all__ = ["DEFAULT_REGISTRY", "FINISH", "KB_SEARCH", "READ_FILE", "RUN_SHELL", "SOLVER_TOOLS", "Tool", "ToolContext",
           "ToolRegistry", "WRITE_FILE"]
