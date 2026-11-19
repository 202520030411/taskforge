"""Task packages in Harbor layout, held as {relative path: text}.

  instruction.md           what the agent is asked to do
  task.toml                metadata
  environment/Dockerfile   builds the image; must COPY environment/app/ to /app
  environment/app/**       the starting workspace (/app/**)
  tests/test.sh            hidden grader entry point; exit code 0 = pass
  solution/solve.sh        reference solution, run from /solution
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

REQUIRED = ("instruction.md", "task.toml", "environment/Dockerfile", "tests/test.sh", "solution/solve.sh")
AREAS = ("instruction", "environment", "tests", "solution")


def area(path: str) -> str:
    if path == "instruction.md":
        return "instruction"
    for top in ("environment", "tests", "solution"):
        if path.startswith(top + "/"):
            return top
    if path == "task.toml":
        return "meta"
    return "other"


def validate_layout(files: Mapping[str, str]) -> list[str]:
    problems = [f"missing {p}" for p in REQUIRED if p not in files]
    for p in files:
        if p.startswith("/") or ".." in Path(p).parts:
            problems.append(f"bad path {p!r}")
        elif area(p) == "other":
            problems.append(f"unexpected file {p!r} (allowed: instruction.md, task.toml, environment/, tests/, solution/)")
    if "environment/Dockerfile" in files and "app/" not in files["environment/Dockerfile"]:
        problems.append("environment/Dockerfile must COPY app/ to /app")
    return problems


def changed(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))


def apply_changes(old: Mapping[str, str], changes: Mapping[str, str | None]) -> dict[str, str]:
    """`changes` maps path -> new content, or None to delete the file."""
    out = dict(old)
    for path, content in changes.items():
        if content is None:
            out.pop(path, None)
        else:
            out[path] = content
    return out


def load_dir(directory: Path) -> dict[str, str]:
    directory = Path(directory)
    return {str(p.relative_to(directory)): p.read_text() for p in sorted(directory.rglob("*")) if p.is_file()}


def write_dir(files: Mapping[str, str], directory: Path) -> None:
    for rel, content in files.items():
        path = Path(directory) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
