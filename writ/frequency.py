"""Frequency-based confidence graduation.

Stateless computation: given observation counts, determine whether a rule
has earned empirical confidence. No graph or pipeline dependencies.

Per ARCH-ORG-001: domain logic in its own module, consumed by ranking and integrity.
"""

from __future__ import annotations

from dataclasses import dataclass

# Per ARCH-CONST-001: defaults from writ.toml [frequency] section.
DEFAULT_GRADUATION_THRESHOLD = 50
DEFAULT_GRADUATION_RATIO_MIN = 0.75


@dataclass
class GraduationResult:
    """Result of evaluate_graduation()."""

    graduated: bool
    flagged: bool
    ratio: float
    n: int


def evaluate_graduation(
    times_positive: int,
    times_negative: int,
    threshold: int = DEFAULT_GRADUATION_THRESHOLD,
    ratio_min: float = DEFAULT_GRADUATION_RATIO_MIN,
) -> GraduationResult:
    """Evaluate whether a rule qualifies for empirical confidence graduation.

    Returns:
    - graduated=True if n >= threshold and ratio >= ratio_min
    - flagged=True if n >= threshold and ratio < ratio_min (needs human review)
    - Both False if n < threshold (insufficient data)
    """
    n = times_positive + times_negative
    if n == 0:
        return GraduationResult(graduated=False, flagged=False, ratio=0.0, n=0)

    ratio = times_positive / n

    if n < threshold:
        return GraduationResult(graduated=False, flagged=False, ratio=ratio, n=n)

    if ratio >= ratio_min:
        return GraduationResult(graduated=True, flagged=False, ratio=ratio, n=n)

    return GraduationResult(graduated=False, flagged=True, ratio=ratio, n=n)
