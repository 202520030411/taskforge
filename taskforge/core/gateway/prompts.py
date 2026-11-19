"""Versioned prompt files: one file per role; the version is a hash of the file text (DESIGN.md section 6)."""
from __future__ import annotations

import string
from dataclasses import dataclass
from pathlib import Path

from ..hashing import content_hash


@dataclass(frozen=True)
class Prompt:
    role: str
    text: str
    version: str


class PromptRegistry:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def get(self, name: str) -> Prompt:
        text = (self.directory / f"{name}.md").read_text()
        return Prompt(name, text, content_hash(text)[:12])

    def render(self, name: str, **values) -> tuple[str, str]:
        """Fill `$name` placeholders. Missing values raise KeyError, so a prompt never goes out half-filled."""
        prompt = self.get(name)
        return string.Template(prompt.text).substitute(**{k: str(v) for k, v in values.items()}), prompt.version
