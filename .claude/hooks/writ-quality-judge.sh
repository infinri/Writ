#!/usr/bin/env bash
# Phase 2: Gate 5 Tier 2 quality-judgment via hook-directive self-review.
#
# PostToolUse on Write to plan / design-doc / test file artifacts.
# Emits a self-review directive on stdout. Claude reads it in the next turn,
# evaluates the artifact against the rubric, and POSTs the score to
# /session/{sid}/quality-judgment. The existing writ-verify-before-claim.sh
# hook enforces the recorded score on completion claims.
#
# No external LLM call. See docs/phase-2-self-review-decision.md.
# Feature-flag gated.
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"

is_superpowers_absorb_enabled || exit 0

PARSED=$(parse_hook_stdin)
SESSION_ID=$(detect_session_id "$PARSED")
[ -z "$SESSION_ID" ] && exit 0
is_work_mode "$SESSION_ID" || exit 0

FILE=$(parsed_field "$PARSED" "file_path")
[ -z "$FILE" ] && exit 0

# Classify artifact type.
ARTIFACT_TYPE=""
case "$FILE" in
    */docs/*/plans/*.md|*/docs/plans/*.md|*/plan.md) ARTIFACT_TYPE="plan" ;;
    */docs/*/specs/*-design.md|*/docs/specs/*-design.md) ARTIFACT_TYPE="design" ;;
    */tests/*.py|*/tests/*.js|*/tests/*.ts|*/tests/*.php|*/tests/*.go|*/tests/*.rs|*/tests/*.java) ARTIFACT_TYPE="test" ;;
    *) exit 0 ;;
esac

# Emit the self-review directive. Claude reads this on next turn.
case "$ARTIFACT_TYPE" in
    plan)
        cat <<EOF
[WRIT QUALITY-JUDGE] You just wrote a plan: $FILE
Before marking any todo item for this task as completed, self-review this plan
and POST your judgment to the session endpoint.

Rubric (plan Section 15.4):
  Score each '## <section>' 0-5 on whether it contains substantive content
  SPECIFIC to the task, vs generic boilerplate that could be pasted into any plan.
  - Specific: names concrete files, verifiable success criteria, realistic steps
  - Generic: placeholder-level content that structurally exists but conveys
    no task-specific information
  Overall score = the LOWEST scoring section. Score ≥ 3 is required.

Post the judgment:
  curl -sX POST http://localhost:8765/session/$SESSION_ID/quality-judgment \\
    -H 'Content-Type: application/json' \\
    -d '{"artifact_path": "$FILE", "score": <0-5>, "failing_section": "<name or null>", "rationale": "<one sentence>"}'

If score < 3: name the failing section and what specific change raises it to
3+, fix the plan, re-self-review, re-post. Do NOT mark the todo completed
until a score ≥ 3 is recorded (or --override-quality-judge is used; overrides
beyond 3 per session escalate to pressure audit per Section 0.4 decision 4).
EOF
        ;;
    design)
        cat <<EOF
[WRIT QUALITY-JUDGE] You just wrote a design doc: $FILE
Before marking any todo item for this task as completed, self-review and POST
your judgment.

Rubric (plan Section 15.5):
  - Alternatives Considered: names real alternatives (not straw-men)
  - Constraints: real constraints (not generic platitudes)
  - Risks: actionable risks with plausible mitigations

Overall score = the lowest across the three areas. Score ≥ 3 required.

Post the judgment:
  curl -sX POST http://localhost:8765/session/$SESSION_ID/quality-judgment \\
    -H 'Content-Type: application/json' \\
    -d '{"artifact_path": "$FILE", "score": <0-5>, "failing_section": "<name or null>", "rationale": "<one sentence>"}'
EOF
        ;;
    test)
        cat <<EOF
[WRIT QUALITY-JUDGE] You just wrote a test file: $FILE
Before marking the implementation todo completed, self-review and POST judgment.

Rubric (plan Section 15.6):
  Do the assertions test real behavior (call production code, verify outputs
  against expectations), or do they test mocks / trivially-true conditions?
  Reference the 5 TDD anti-patterns (ANT-PROC-TDD-001 through ANT-PROC-TDD-005).

Score ≥ 3 required.

Post the judgment:
  curl -sX POST http://localhost:8765/session/$SESSION_ID/quality-judgment \\
    -H 'Content-Type: application/json' \\
    -d '{"artifact_path": "$FILE", "score": <0-5>, "failing_section": "<which anti-pattern or null>", "rationale": "<one sentence>"}'
EOF
        ;;
esac

exit 0
