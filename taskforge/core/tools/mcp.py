"""Serve a ToolRegistry over the Model Context Protocol, so outside clients (an IDE, another agent)
can use the same sandbox tools the system's own agents use.

Written against the `mcp` Python SDK v2 low-level server: `on_list_tools` and `on_call_tool` handlers.
"""
from __future__ import annotations

from typing import Iterable

import anyio
import mcp.types as types
from mcp.server import Server

from .registry import DEFAULT_REGISTRY, ToolContext, ToolRegistry

MCP_TOOLS = ("run_shell", "read_file", "write_file")


def build_server(ctx: ToolContext, *, registry: ToolRegistry = DEFAULT_REGISTRY,
                 names: Iterable[str] = MCP_TOOLS) -> Server:
    names = tuple(names)

    async def list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool(name=s.name, description=s.description,
                                                       input_schema=dict(s.parameters)) for s in registry.specs(names)])

    async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        # handlers block on the sandbox, so run them off the event loop
        text = await anyio.to_thread.run_sync(lambda: registry.call(params.name, params.arguments or {}, ctx,
                                                                    allowed=names))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    is_error=text.startswith("error:"))

    return Server("taskforge", version="0.0.1", instructions="Tools that act inside one TaskForge sandbox.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


def serve_stdio(sandbox, image: str) -> int:
    """One sandbox for the lifetime of the connection; destroyed when the client disconnects."""
    from mcp.server.stdio import stdio_server
    sid = sandbox.create(image, purpose="mcp")
    server = build_server(ToolContext(sandbox=sandbox, sid=sid))

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    try:
        anyio.run(run)
    finally:
        sandbox.destroy(sid)
    return 0
