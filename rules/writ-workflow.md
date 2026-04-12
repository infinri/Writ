# Writ workflow -- failure mode instructions

These rules apply when Writ hooks are active (gate denials, RAG injection).
The happy-path workflow is in ~/.claude/CLAUDE.md. These cover edge cases.

## Gate denials

If a write is denied with [ENF-GATE-PLAN] or [ENF-GATE-TEST], STOP.
Do not attempt additional writes. The denial applies to ALL files, not
just the one denied. Read the denial message and follow its instructions.

## /plan UI approval is not code-write approval

After /plan mode exits and you see "User approved Claude's plan", this is
NOT approval to write code. It is only the /plan UI confirming format.
You must still present the plan in conversation and wait for the user to
type "approved" before writing any files.

## plan.md timing

Write plan.md to the project root WHILE STILL IN /plan mode, BEFORE
calling ExitPlanMode. The ExitPlanMode hook validates plan.md on exit.
/plan mode also stores its own internal copy; this double-write is expected.

## Test presentation

After writing test skeleton files, say ONLY this format:

"Test skeletons written: ClassName (N tests), ClassName (N tests). Say approved to proceed."

WRONG (do not do this):
- ClassName -- N tests:
  - testMethodName -- description
  - testOtherMethod -- description

The user can read the files. Do not reproduce method names or descriptions.

## Phase boundaries

Never write non-test files before test-skeletons approval.
Never batch writes across phase boundaries (plan -> test -> implementation).
