"""Review rules for H1-H7 (DESIGN.md section 10) and the deterministic sampling used to pick items.

People can only tighten: items an executed gate rejected, and proposals that overlap
the benchmark, are never queued, so there is nothing to override (invariants 3 and 6).
"""
from __future__ import annotations

from typing import Iterable

from ..core.hashing import content_hash
from ..core.review import ReviewPolicy

DECISIONS = {
    "H1": ("approve",),
    "H2": ("keep", "drop"),
    "H3": ("edit_instruction", "edit_tests", "edit_environment", "edit_oracle", "drop"),
    "H4": ("keep", "reject"),
    "H5": ("clean", "shortcut"),
    "H6": ("export", "hold"),
    "H7": ("reasonable", "unreasonable", "cannot_judge", "no_good_solution"),
}

REASON_CODES = {
    "H1": ("patterns_ok", "not_model_fault", "too_generic", "duplicate_pattern", "env_or_verifier_broken"),
    "H2": ("on_target", "off_target", "benchmark_lookalike", "too_vague"),
    "H3": ("ambiguous_spec", "tests_too_strict", "tests_too_weak", "env_unstable", "oracle_wrong", "cannot_fix"),
    "H4": ("looks_fine", "illegal_difficulty", "ambiguous_spec", "tests_too_strict", "new_hack_found"),
    "H5": ("clean", "shortcut_found"),
    "H6": ("ready", "pattern_coverage_low", "quality_concern"),
    "H7": ("blind_label",),
}

# The automatic decision each stage may queue. Anything else is refused.
QUEUEABLE = {
    "H1": {"PROPOSED"},
    "H2": {"CLEAN", "GREY"},              # OVERLAP is never queued
    "H3": {"NEEDS_FIX"},
    "H4": {"ACCEPT", "REVIEW"},           # REJECT (a gate failed) is never queued
    "H5": {"PASS"},
    "H6": {"READY"},
    "H7": {"JUDGED"},
}

EDIT_AREAS = {"edit_instruction": "instruction", "edit_tests": "tests", "edit_environment": "environment",
              "edit_oracle": "solution"}


def allowed(stage: str, auto_decision: str) -> tuple[str, ...]:
    return DECISIONS[stage] if auto_decision in QUEUEABLE.get(stage, ()) else ()


def agrees(stage: str, auto_decision: str, decision: str) -> bool:
    """Did the person confirm what the automatic side would have done?"""
    return {
        ("H1", "PROPOSED"): decision == "approve",
        ("H2", "CLEAN"): decision == "keep",
        ("H2", "GREY"): decision == "drop",
        ("H4", "ACCEPT"): decision == "keep",
        ("H4", "REVIEW"): decision == "reject",
        ("H5", "PASS"): decision == "clean",
        ("H6", "READY"): decision == "export",
    }.get((stage, auto_decision), True)


POLICY = ReviewPolicy(allowed, REASON_CODES, agrees)


def stable_fraction(item_id: str, salt: str = "") -> float:
    """A number in [0, 1) fixed by the id, so a re-executed step samples the same items."""
    return int(content_hash([salt, item_id])[:8], 16) / 16 ** 8


def pick(ids: Iterable[str], n: int, salt: str) -> list[str]:
    """The n ids with the smallest stable_fraction: a fixed 'random' sample."""
    return sorted(ids, key=lambda i: stable_fraction(i, salt))[:n]
