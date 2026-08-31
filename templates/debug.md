# Debug companion

Scaffold for a disciplined debug-mode investigation. Fill sections top-to-bottom.

Two hard gates enforce the diagnostic order (INV-9 + Increment 4); everything else is
**advisory**:
1. **## Evidence + ## Narrowing** gate CODE READING: in the runtime (debug) lens, Grep and
   reading source-code files are blocked until both have real content. Observe runtime data
   first (logs, traces, queries via Bash, whose output is auto-captured) and record it
   here; reading debug.md, logs, and non-code files stays allowed meanwhile.
2. **## Root cause** gates source EDITS: source-file changes are blocked until it has a
   non-empty body (the diagnostic analogue of plan.md).

Both are presence-checked, not truth-checked; it is on you to make them real. (See
PBK-PROC-DEBUG-001, the diagnose-* playbooks, and DEBUG-MODE-PROPOSAL.md.)

## Symptom
<what is observably wrong: the error, the slow path, the failing case>

## Evidence
<runtime data points: a stack trace, a log line with a timestamp, a query result,
a profiler/microtime sample, a failing test. "The code says X could happen" is NOT
evidence; it is a hypothesis. (advisory)>

## Narrowing
<the smallest affected unit: one endpoint, one SKU pattern, one consumer, one test. (advisory)>

## Hypothesis
<the candidate cause, stated so it can be checked. (advisory)>

## Reproduction
<local repro steps + observed result, or an explicit "cannot reproduce because X".
For nondeterministic bugs, record a failure RATE, not a single run. (advisory)>

## Falsification
<how this hypothesis could be wrong, and the result of checking it. The one step
that applies to every bug class. (advisory but strongly recommended)>

## Triangulation
<two independent sources that agree, when a single source is not conclusive. Do not
manufacture a contrived second source. (advisory)>

## Root cause
<REQUIRED before editing source: the established cause, specific enough that the fix
follows from it. Not "something in X" but the actual mechanism.>

## Fix
<what to change, derived from the root cause. In Work mode this becomes the plan.>
