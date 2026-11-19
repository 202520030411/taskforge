"""Difficulty gate logic (spec section 5, stage 7). Pure functions; the workflow runs the rollouts.

The measure is "passes out of independent runs" (k of n), not pass@n. With 5 runs a task
whose true pass rate is 20% fails all 5 about a third of the time (0.8^5 = 0.33), so
k = 0 or k = n triggers n more runs before anything is concluded.
"""
from __future__ import annotations

from typing import Literal, Sequence


def in_band(k: int, n: int, low: int = 1) -> bool:
    """1 <= k <= n-1: the weak model sometimes succeeds and sometimes fails."""
    return low <= k <= n - low


def after_first(passes: Sequence[bool], low: int = 1) -> Literal["in_band", "rerun"]:
    return "in_band" if in_band(sum(passes), len(passes), low) else "rerun"


def after_rerun(first: Sequence[bool], rerun: Sequence[bool], low: int = 1) -> Literal["in_band", "all_pass", "all_fail"]:
    passes = list(first) + list(rerun)
    k, n = sum(passes), len(passes)
    if in_band(k, n, low):
        return "in_band"
    return "all_pass" if k == n else "all_fail"


def diagnose_all_pass(suspect_flags: Sequence[Sequence[str]]) -> Literal["exploit", "too_easy"]:
    """All runs passed: a shortcut is checked before calling the task easy."""
    return "exploit" if any(flags for flags in suspect_flags) else "too_easy"


def diagnose_all_fail(strong_passed: bool, judge_verdict: str) -> Literal["broken", "truly_hard", "unclear"]:
    """All runs failed: strong model also fails and the judge calls the task unreasonable -> broken;
    strong model passes and the judge calls it reasonable -> truly hard; anything else goes to a person."""
    if not strong_passed and judge_verdict == "unreasonable":
        return "broken"
    if strong_passed and judge_verdict == "reasonable":
        return "truly_hard"
    return "unclear"
