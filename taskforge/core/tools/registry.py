"""Tool registry (DESIGN.md section 8): a tool is defined once and used two ways.

  - by agents inside the system: `specs()` gives the definitions sent to the model, `call()` runs one
  - by outside clients: core.tools.mcp serves the same registry over the Model Context Protocol

A tool failure is returned as text the caller can read. Sandbox infrastructure faults are not caught:
they propagate so the engine retries the whole step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from ..errors import Retryable
from ..gateway.types import ToolSpec

MAX_READ_CHARS = 8000


@dataclass(frozen=True)
class ToolContext:
    """What a tool may touch: one sandbox session, and optionally the knowledge base."""
    sandbox: Any = None
    sid: str | None = None
    cmd_timeout_s: float = 120.0
    kb: Any = None
    kb_before_seq: int | None = None


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Callable[[ToolContext, Mapping[str, Any]], str]

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} registered twice")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self, names: Iterable[str]) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[n].spec for n in names)

    def call(self, name: str, arguments: Mapping[str, Any], ctx: ToolContext, *, allowed: Iterable[str] | None = None) -> str:
        allowed = list(self._tools if allowed is None else allowed)
        if name not in allowed or name not in self._tools:
            return f"error: unknown tool {name!r}; available: {', '.join(allowed)}"
        try:
            return self._tools[name].handler(ctx, arguments)
        except Retryable:
            raise
        except KeyError as e:
            return f"error: missing argument {e}"
        except Exception as e:  # noqa: BLE001 - every tool failure is something the caller can read
            return f"error: {type(e).__name__}: {e}"


# ------------------------------------------------------------------ the tools
def _run_shell(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    r = ctx.sandbox.exec(ctx.sid, str(args["command"]), timeout_s=ctx.cmd_timeout_s)
    return f"{r.output}\n[exit {r.exit_code}{' (timed out)' if r.timed_out else ''}]"


def _read_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    text = ctx.sandbox.read_file(ctx.sid, str(args["path"]))
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n[file truncated: showing the first {MAX_READ_CHARS} of {len(text)} characters]"
    return text


def _write_file(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    path, content = str(args["path"]), str(args["content"])
    errors = ctx.sandbox.write_files(ctx.sid, {path: content})
    return f"error: {errors[path]}" if errors else f"wrote {path} ({len(content)} characters)"


def _finish(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    return "finished"


def _kb_search(ctx: ToolContext, args: Mapping[str, Any]) -> str:
    if ctx.kb is None:
        return "error: no knowledge base is attached"
    hits = ctx.kb.search(str(args["kind"]), str(args["query"]), k=int(args.get("k", 5)), before_seq=ctx.kb_before_seq)
    return "\n\n".join(f"[{h.kind}] {h.text}" for h in hits) or "(nothing found)"


RUN_SHELL = ToolSpec("run_shell", "Run a bash command in /app and return its output and exit code.",
                     {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
READ_FILE = ToolSpec("read_file", "Read a text file.",
                     {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
WRITE_FILE = ToolSpec("write_file", "Create or overwrite a text file.",
                      {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]})
FINISH = ToolSpec("finish", "Call when the task is complete.", {"type": "object", "properties": {}})
KB_SEARCH = ToolSpec("kb_search", "Search the knowledge base (kinds: proposal, accepted_task, repair_example, "
                                  "rubric_counterexample, hack_note).",
                     {"type": "object", "properties": {"kind": {"type": "string"}, "query": {"type": "string"},
                                                       "k": {"type": "integer"}}, "required": ["kind", "query"]})

DEFAULT_REGISTRY = ToolRegistry([Tool(RUN_SHELL, _run_shell), Tool(READ_FILE, _read_file), Tool(WRITE_FILE, _write_file),
                                 Tool(FINISH, _finish), Tool(KB_SEARCH, _kb_search)])
SOLVER_TOOLS = (RUN_SHELL, READ_FILE, WRITE_FILE, FINISH)
