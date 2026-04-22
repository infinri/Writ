# Phase 2 decision: Gate 5 Tier 2 uses hook-directive self-review, not external Haiku

_Dated 2026-04-21. Supersedes plan Section 15.4's Haiku-based design._

## Change

Plan Section 15.4 (and follow-on Sections 15.5-15.7) specified Gate 5 Tier 2 as an external Haiku API call from a PostToolUse hook. That design inherited a traditional external-LLM-review architecture.

**Revised:** Gate 5 Tier 2 emits a self-review directive from the PostToolUse hook; Claude Code (the agent driving the session) reads the directive and self-reviews the artifact against the rubric in its next turn, posting the score to `/session/{sid}/quality-judgment`. Completion gates enforce the recorded score.

## Rationale

1. **Superpowers v5.0.6 precedent.** Superpowers' own release notes document the decision to replace subagent review loops with inline self-review: "Regression testing across 5 versions with 5 trials each showed identical quality scores regardless of whether the review loop ran. Self-review catches 3-5 real bugs per run in ~30s instead of ~25 min, with comparable defect rates to the subagent approach." Applying an external Haiku judge re-introduces the exact architecture Superpowers concluded was unnecessary.

2. **No API key required.** Claude Code IS Claude. The external Haiku call makes sense only when the evaluator is a separate process without access to the current agent. Inside a Claude Code session, the agent is already present and can evaluate its own artifact without going out to an API.

3. **Protocol match.** Writ's existing hooks (`validate-exit-plan.sh:148-155`, `auto-approve-gate.sh`) already use the hook-stdout-as-directive pattern. Tier 2 becomes another instance of that pattern rather than a new external-dependency class.

4. **Determinism at the protocol level.** The external Haiku call was parametrized for determinism (temperature=0, N=1 baseline, N=3 voting available). Hook-directive self-review is deterministic at the PROTOCOL level: the hook always emits the same directive for the same artifact shape; Claude always gets the same rubric input; session state always records the emitted score with inline rationale.

5. **Lower latency and zero marginal cost.** No 5s timeout window needed; no API cost per judgment. Claude's next-turn processing handles the judgment.

## Architecture

**PostToolUse hook (`writ-quality-judge.sh`):**
- Triggers on Write to `docs/**/plans/*.md`, `docs/**/specs/*-design.md`, and test files
- Computes the rubric prompt from the artifact type (plan, design-doc, test file)
- Emits a `[WRIT QUALITY-JUDGE]` directive block on stdout
- Claude reads the directive in its next turn

**Claude's next turn:**
- Reads the directive
- Evaluates the artifact against the rubric
- POSTs `{artifact_path, score, failing_section, rationale}` to `/session/{sid}/quality-judgment` (endpoint already exists from Phase 1 deliverable 6)

**`writ-verify-before-claim.sh` extension:**
- Already checks `verification_evidence`
- Also checks `quality_judgment_state` for each completion-claim artifact
- Denies when score < 3 and no override recorded

## Rubric content

Rubrics by artifact type (plan Section 15.4-15.6 content repurposed as directive text, not API prompt):

### Plan (`docs/**/plans/*.md`)

```
[WRIT QUALITY-JUDGE] You just wrote a plan. Before marking the task completed,
self-review against these criteria and POST your score to
/session/{sid}/quality-judgment:

Score each ## section 0-5 on whether it contains substantive content SPECIFIC
to this task, or generic boilerplate that could be pasted into any plan.

- Specific: names concrete files, verifiable success criteria, realistic steps
- Generic: placeholder-level content that structurally exists but conveys no
  task-specific information

Overall score is the minimum across sections. Score ≥ 3 required to mark the
task completed. If any section scores < 3, state which section is failing
and what specific change would raise it to 3+.
```

### Design doc (`docs/**/specs/*-design.md`)

```
[WRIT QUALITY-JUDGE] You just wrote a design doc. Before marking the task
completed, self-review and POST score to /session/{sid}/quality-judgment:

- Alternatives Considered: does it name real alternatives (not straw-men)?
- Constraints: real constraints, not generic platitudes?
- Risks: actionable risks with plausible mitigations (not "might have issues")?

Overall score is the lowest of the three. Score ≥ 3 required.
```

### Test file (`tests/**/*.{py,js,ts,php}`)

```
[WRIT QUALITY-JUDGE] You just wrote a test file. Before marking the task
completed, self-review and POST score to /session/{sid}/quality-judgment:

Do assertions test real behavior (call production code, verify outputs
against expectations), or do they test mocks / trivially-true conditions?

Reference: the 5 named anti-patterns in ANT-PROC-TDD-001 through
ANT-PROC-TDD-005.

Score ≥ 3 required.
```

## Override policy (plan Section 0.4 decision 4)

Unchanged: 3 `--override-quality-judge` invocations per session before
pressure-audit friction log escalates. Override mechanism: the agent POSTs
`{overridden: true, reason: "..."}` to the quality-judgment endpoint; the
endpoint increments `quality_override_count`.

## False-positive fixture

Plan Section 15.9 stays unchanged: 50 hand-labeled legitimate plans /
designs / test files in `tests/fixtures/legitimate_artifacts/`. The false
positive rate target remains ≤ 5% Tier 1, ≤ 10% Tier 2. Measurement runs
the hooks over each fixture and counts denies.

## Gamed-content fixture

Plan Section 15.8 stays unchanged: 50 hand-crafted gamed artifacts at
`tests/fixtures/gamed_artifacts/`. The ≥90% true-negative blocker remains.
Measurement runs the Tier 2 rubric over each; since the judge is Claude
self-reviewing a Claude-produced artifact, we also collect inter-run
variance metrics (score delta across re-runs of the same artifact) as a
Phase 5 calibration signal.

## Net effect on plan release-blockers

- **[BLOCKER]** Gate 5 Tier 2 ≥90% true-negative rate: preserved. Measurement unchanged.
- **[BLOCKER]** Tier 2 false-positive rate ≤ 10% on legitimate-artifact fixture: preserved. Measurement unchanged.
- **Operational delta:** no ANTHROPIC_API_KEY environment dependency, no 5s latency window, no per-judgment cost.

## Backward compatibility

No external ingredient ever existed in prior phases. Nothing to break. The
`LlmAnalyzer` class (`writ/analysis/llm.py`) remains for the `/analyze`
endpoint, which serves a different purpose (pattern-match-augmented
compliance analysis, not artifact quality review).
