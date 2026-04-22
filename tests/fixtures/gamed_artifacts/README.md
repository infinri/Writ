# Gamed-content fixture for Gate 5 true-negative measurement

Per plan Section 15.8, 50 artifacts hand-crafted across a difficulty spectrum. Gate 5 Tier 2 must achieve >= 90% true-negative rate (correctly flag as failing) across the full set.

## Directory structure

```
gamed_artifacts/
  trivially_bad/         # 15-20 artifacts; empty sections, lorem ipsum, single-word content
  plausible_boilerplate/ # 15-20 artifacts; generic prose that structurally passes
  near_miss/             # 10-15 artifacts; real files + real steps, specific quality failure
```

## Difficulty tiers

### Trivially bad (15-20 files)

Artifacts that should be flagged by even the simplest structural gate. Examples:
- `plan-empty-sections.md` — has all section headers, every body is empty
- `plan-lorem-ipsum.md` — lorem ipsum content
- `plan-single-word.md` — one-word answers under each section
- `design-placeholder-heavy.md` — TBD / TODO / "fill in later" throughout
- `test-trivially-true.py` — `assert True`, `assert 1 == 1`

Tier 1 alone should catch most of these. Tier 2 should catch 100%.

### Plausible boilerplate (15-20 files)

Artifacts that pass structural checks but convey no task-specific information. Examples:
- `plan-generic-improve.md` — "improve performance by optimizing code"
- `plan-vague-steps.md` — "refactor the module to be more maintainable"
- `design-straw-man-alternatives.md` — "Alternative 1: do nothing. Alternative 2: do the thing."
- `design-platitude-constraints.md` — "must be maintainable, must be performant, must be secure"
- `test-mock-returns-true.py` — all assertions check mocks returning predetermined values

Tier 1 partially catches (word-count might pass). Tier 2 is the workhorse.

### Near-miss (10-15 files)

Artifacts that reference real files and real steps but have specific quality failures. Examples:
- `plan-missing-success-criteria.md` — concrete steps, no verifiable success criteria
- `plan-circular-reasoning.md` — step 3 depends on step 5 which depends on step 3
- `design-contradictory-requirements.md` — two constraints that cannot be simultaneously satisfied
- `test-incomplete-mock.py` — mock returns None where real code would raise
- `test-tests-the-mock.py` — assertions verify mock was called, not behavior

Tier 2 is the only gate that catches these. Hardest class.

## Authoring standard (plan Section 15.8)

**Hand-crafted by the maintainer.** Claude Code may propose candidates; each candidate must be human-reviewed before inclusion. Candidates that drift toward "easy boilerplate" after review are moved to the trivially_bad tier rather than discarded.

## Measurement protocol

Gate 5 Tier 2 runs the self-review rubric (via the `writ-quality-judge.sh` hook) over each artifact. The directive is emitted; Claude self-reviews; the resulting score is recorded.

For the fixture:
- Expected behavior: score < 3 for every artifact.
- True negative: score < 3 recorded. False positive would be a legitimate artifact getting score < 3 — this fixture only contains gamed artifacts so FP is not measured here (see `legitimate_artifacts/` for that).
- True-negative rate = (# flagged / total). Must be >= 90% per blocker.

A companion `legitimate_artifacts/` fixture provides the false-positive side.

## Status

This directory structure is scaffolded. The 50 artifacts are pending maintainer authoring.

Current progress:
- `trivially_bad/`: 10 of 15-20 seeded by Claude Code (plans, designs, tests; structural failures are mechanically verifiable and safer to bulk-produce)
- `plausible_boilerplate/`: 0 of 15-20 — pending maintainer authorship
- `near_miss/`: 0 of 10-15 — pending maintainer authorship

Candidates in `plausible_boilerplate/` and `near_miss/` tiers require human authorship per plan Section 15.8.
