"""Overlap check against the held-out benchmark (DESIGN.md section 12, invariant 3).

Texts are cut into overlapping 5-word pieces. A text's score against one benchmark
item is the larger of: the share of the text's pieces found in the item, and the
share of the item's pieces found in the text - so neither copying a benchmark item
into a longer text nor copying part of one into a shorter text hides the overlap.
  score >= 0.15 or identical text  -> OVERLAP (dropped, never reviewed)
  0.05 <= score < 0.15             -> GREY (always shown to a person at H2)
The index refuses to run empty: an unconfigured benchmark must not look like "no overlap".
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

N = 5
OVERLAP = 0.15
GREY = 0.05
_WORD = re.compile(r"[a-z0-9]+")


class ContaminationError(RuntimeError):
    pass


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def shingles(text: str, n: int = N) -> set[str]:
    w = _words(text)
    if len(w) < n:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


@dataclass(frozen=True)
class Check:
    status: str          # CLEAN | GREY | OVERLAP
    score: float
    closest: str | None


class BenchmarkIndex:
    def __init__(self, texts: Mapping[str, str]):
        if not texts:
            raise ContaminationError("benchmark index is empty; refusing to report 'no overlap'")
        self.exact = {hashlib.sha256(" ".join(_words(t)).encode()).hexdigest() for t in texts.values()}
        self.items = {name: shingles(t) for name, t in texts.items()}

    @classmethod
    def from_dir(cls, root: Path, pattern: str = "**/instruction.md") -> "BenchmarkIndex":
        root = Path(root)
        texts = {str(p.relative_to(root)): p.read_text(errors="replace") for p in sorted(root.glob(pattern))}
        return cls(texts)

    def check(self, text: str) -> Check:
        if hashlib.sha256(" ".join(_words(text)).encode()).hexdigest() in self.exact:
            return Check("OVERLAP", 1.0, "exact match")
        mine = shingles(text)
        best, closest = 0.0, None
        for name, theirs in self.items.items():
            if not mine or not theirs:
                continue
            shared = len(mine & theirs)
            score = max(shared / len(mine), shared / len(theirs))
            if score > best:
                best, closest = score, name
        status = "OVERLAP" if best >= OVERLAP else "GREY" if best >= GREY else "CLEAN"
        return Check(status, round(best, 4), closest)
