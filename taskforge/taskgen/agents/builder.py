"""Task Builder (spec section 5, stage 5) and its verifier-fix mode (stage 6, hack gate).

The builder drafts a full package as JSON, runs its own oracle and initial-state check,
and fixes the draft from the error report, at most `fix_rounds` times. A draft that still
fails is returned anyway: the oracle gate then rejects it, so the funnel shows the loss.
"""
from __future__ import annotations

import json
from typing import Mapping

from ...core.errors import ModelOutputError
from ...core.sandbox import Sandbox
from .. import package as pkg
from ..schemas import PatternBrief
from ..verify import self_check
from .llm import RoleLLM

MAX_FILES_JSON = 60000


def _files_from(reply: dict) -> dict[str, str | None]:
    files = reply.get("files")
    if not isinstance(files, dict) or not files:
        raise ModelOutputError("'files' must be a non-empty object of path -> content")
    return {str(p): (None if c is None else str(c)) for p, c in files.items()}


def draft(llm: RoleLLM, sandbox: Sandbox, brief: PatternBrief, title: str, statement: str,
          fix_rounds: int) -> tuple[dict[str, str], bool, list[str]]:
    reply = llm.ask_json("task_builder", required=("files",), max_tokens=32000, category=brief.category,
                         pattern=brief.description, title=title, statement=statement)
    files = {p: c for p, c in _files_from(reply).items() if c is not None}
    log: list[str] = []
    for round_no in range(fix_rounds + 1):
        problems = pkg.validate_layout(files)
        if problems:
            feedback = "Layout problems:\n" + "\n".join(f"- {p}" for p in problems)
        else:
            check = self_check(sandbox, files)
            if check.passed:
                log.append(f"round {round_no}: self-check passed")
                return files, True, log
            feedback = f"Self-check failed: {', '.join(check.reasons)}\n{json.dumps(check.details, default=str)[:6000]}"
        log.append(f"round {round_no}: {feedback.splitlines()[0]}")
        if round_no == fix_rounds:
            break
        reply = llm.ask_json("task_builder", prompt="task_builder_fix", required=("files",), max_tokens=32000,
                             feedback=feedback,
                             files=json.dumps(files, indent=1)[:MAX_FILES_JSON])
        files = pkg.apply_changes(files, _files_from(reply))
    return files, False, log


def strengthen_tests(llm: RoleLLM, files: Mapping[str, str], hack_report: Mapping[str, dict]) -> dict[str, str | None]:
    """Change only tests/ so that the shortcuts that passed now fail and the reference solution still passes."""
    passed = {k: v for k, v in hack_report.items() if v.get("passed")}
    reply = llm.ask_json("task_builder", prompt="verifier_fixer", required=("files",), max_tokens=32000,
                         instruction=files.get("instruction.md", ""),
                         tests=json.dumps({p: c for p, c in files.items() if p.startswith("tests/")}, indent=1),
                         shortcuts=json.dumps(passed, indent=1, default=str))
    changes = _files_from(reply)
    outside = [p for p in changes if pkg.area(p) != "tests"]
    if outside:
        raise ModelOutputError(f"the verifier fix may only change tests/, but it changed {outside}")
    return changes
