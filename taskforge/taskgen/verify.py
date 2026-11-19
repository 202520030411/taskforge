"""The executed gates (spec section 5, stage 6). Each returns a GateOutcome; none of them calls a model.

  oracle    the reference solution passes, on two independent runs
  initial   the untouched workspace fails
  hack      every scripted shortcut fails (the model-driven Hacker runs in the workflow step)
  leak      the solution and hidden test data are not visible to the agent

A failing check is a result, never an exception. Only sandbox faults (SandboxError) propagate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from ..core.sandbox import BuildError, Sandbox, section, session, workspace_files
from .hacks import STRATEGIES, HackContext


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    reasons: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


def build_image(sandbox: Sandbox, files: Mapping[str, str]) -> tuple[str | None, GateOutcome | None]:
    try:
        return sandbox.build(files), None
    except BuildError as e:
        return None, GateOutcome(False, ("image_build_failed",), {"error": str(e)[-2000:]})


def run_solution(sandbox: Sandbox, image: str, files: Mapping[str, str]) -> tuple[int, str, str]:
    """Run solution/solve.sh in a fresh sandbox; return its exit code, output and the resulting /app."""
    solution = {"/solution/" + rel: c for rel, c in section(files, "solution").items()}
    with session(sandbox, image, purpose="oracle") as sid:
        errors = sandbox.write_files(sid, solution, user="root")
        if errors:
            return 126, f"could not place the solution: {errors}", ""
        sandbox.exec(sid, "chmod -R a+rX /solution", user="root")
        r = sandbox.exec(sid, "bash /solution/solve.sh", timeout_s=600)
        return r.exit_code, r.output, sandbox.snapshot_app(sid)


def gate_oracle(sandbox: Sandbox, files: Mapping[str, str], *, repeats: int = 2) -> GateOutcome:
    image, failed = build_image(sandbox, files)
    if failed:
        return failed
    tests = section(files, "tests")
    runs = []
    for _ in range(repeats):
        code, out, snap = run_solution(sandbox, image, files)
        if code != 0:
            return GateOutcome(False, ("solve_sh_failed",), {"exit_code": code, "output": out[-2000:]})
        v = sandbox.verify(image, tests, snap)
        runs.append({"passed": v.passed, "exit_code": v.exit_code, "output": v.output[-1500:]})
    if not runs[0]["passed"]:
        return GateOutcome(False, ("oracle_fails",), {"runs": runs})
    if not all(r["passed"] for r in runs):
        return GateOutcome(False, ("oracle_unstable",), {"runs": runs})
    return GateOutcome(True, (), {"runs": len(runs)})


def gate_initial(sandbox: Sandbox, files: Mapping[str, str]) -> GateOutcome:
    image, failed = build_image(sandbox, files)
    if failed:
        return failed
    with session(sandbox, image, purpose="initial") as sid:
        snap = sandbox.snapshot_app(sid)
    v = sandbox.verify(image, section(files, "tests"), snap)
    if v.passed:
        return GateOutcome(False, ("initial_state_passes",), {"output": v.output[-1500:]})
    return GateOutcome(True, (), {"exit_code": v.exit_code})


def run_scripted_hacks(sandbox: Sandbox, files: Mapping[str, str], strategies=None) -> dict[str, dict]:
    """Every strategy in a fresh sandbox, then graded like a real attempt."""
    image = sandbox.build(files)
    tests = section(files, "tests")
    ctx = HackContext.from_files(files)
    results = {}
    for name, strategy in (strategies or STRATEGIES).items():
        attempt = strategy(ctx)
        with session(sandbox, image, purpose="hack") as sid:
            denied = sandbox.write_files(sid, attempt.writes)
            for command in attempt.commands:
                sandbox.exec(sid, command, timeout_s=60)
            snap = sandbox.snapshot_app(sid)
        v = sandbox.verify(image, tests, snap)
        results[name] = {"passed": v.passed, "denied_writes": sorted(denied)}
    return results


# ------------------------------------------------------------------------ leak
_CODE_WORD = re.compile(r"[A-Za-z0-9_]+")
_PY_STRING = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'', re.S)
MIN_LITERAL = 24
SOLUTION_CONTAINMENT = 0.5


def _ngrams(text: str, n: int = 8) -> set[tuple[str, ...]]:
    w = _CODE_WORD.findall(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def gate_leak(files: Mapping[str, str]) -> GateOutcome:
    """Hard rules only; a model's suspicion is recorded by the workflow and can never reject on its own."""
    instruction = files.get("instruction.md", "")
    workspace = workspace_files(files)
    visible = instruction + "\n" + "\n".join(workspace.values())
    reasons, details = [], {}

    solution = section(files, "solution")
    tests = section(files, "tests")
    same_as_solution = sorted(p for p, c in workspace.items() if len(c) >= 40 and c in solution.values())
    same_as_tests = sorted(p for p, c in workspace.items() if len(c) >= 40 and c in tests.values())
    if same_as_solution:
        reasons.append("solution_file_in_workspace")
        details["solution_files"] = same_as_solution
    if same_as_tests:
        reasons.append("hidden_test_in_workspace")
        details["test_files"] = same_as_tests

    sol_grams = _ngrams("\n".join(c for p, c in solution.items()))
    if len(sol_grams) >= 10:
        share = len(sol_grams & _ngrams(visible)) / len(sol_grams)
        details["solution_text_share"] = round(share, 3)
        if share >= SOLUTION_CONTAINMENT:
            reasons.append("solution_text_in_workspace")

    leaked_lines = []
    for rel, src in tests.items():
        if not rel.endswith(".py"):
            continue
        for m in _PY_STRING.finditer(src):
            literal = next(g for g in m.groups() if g is not None)
            for line in literal.splitlines():
                line = line.strip()
                if len(line) >= MIN_LITERAL and line not in instruction and any(line in c for c in workspace.values()):
                    leaked_lines.append(line[:80])
    if leaked_lines:
        # A flag for H4, not a rejection: hidden tests often reuse lines of the visible input files, which
        # matched this rule without leaking anything (pilot, 2026-11-16; decision D-029).
        details["suspect_test_lines"] = sorted(set(leaked_lines))[:10]
    return GateOutcome(not reasons, tuple(reasons), details)


def self_check(sandbox: Sandbox, files: Mapping[str, str]) -> GateOutcome:
    """What the Task Builder runs on its own draft: one oracle run and the initial-state check."""
    oracle = gate_oracle(sandbox, files, repeats=1)
    if not oracle.passed:
        return oracle
    return gate_initial(sandbox, files)
