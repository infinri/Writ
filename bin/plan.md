# Feedback Loop System

## Status: Phase A -- Design

## What and why

Convert Writ's linear phase workflow into a convergent feedback loop. Today, rules
are injected, Claude plans and codes, static analysis checks syntax/types, but:

- Full rule content is discarded after formatting (only IDs stored in session)
- No hook verifies rule compliance -- only linting/typing
- No mechanism routes backward when later phases reveal earlier-phase gaps
- No phase-specific checklists with machine-verifiable exit criteria
- No cycle limits or escalation when the loop fails to converge

The feedback loop closes these gaps so that rule compliance is checked against
stored rule content, gate invalidation enables backward routing with failure context,
and cycle limits prevent unbounded iteration.

## Files

| File | Action | Purpose |
|------|--------|---------|
| `bin/lib/writ-session.py` | Modify | Store full rule objects, invalidation records, cycle tracking, gate invalidation command |
| `bin/lib/checklists.json` | Create | Machine-verifiable exit criteria per phase per tier |
| `.claude/hooks/writ-rag-inject.sh` | Modify | Pass full rule objects to session; inject phase checklists; inject invalidation context on backward routing |
| `.claude/hooks/validate-rules.sh` | Create | PostToolUse: two-mode rule compliance check (per-write warnings, phase-boundary gate routing) |
| `.claude/hooks/check-gate-approval.sh` | Modify | Carry invalidation records into denial messages when gate was invalidated |
| `.claude/hooks/inject-tier-workflow.sh` | Modify | Include phase checklists from checklists.json in workflow reminders |
| `~/.claude/settings.json` | Modify | Register validate-rules.sh hook |

## Call-paths

### 1. Rule persistence (injection time)

```
writ-rag-inject.sh
  -> curl POST /query (returns full rule objects)
  -> writ-session.py format (formats for Claude context)
  -> writ-session.py update --add-rule-objects <JSON>
  -> session cache gains loaded_rules: [{rule_id, trigger, statement, violation, pass_example, enforcement}, ...]
```

### 2. Phase checklist injection

```
writ-rag-inject.sh (tier workflow section)
  -> reads bin/lib/checklists.json for current phase + tier
  -> injects structured checklist with exit criteria
  -> Claude produces artifacts matching exit criteria
  -> user reviews structured artifacts at gate approval
```

### 3. Per-write rule compliance (intermediate feedback)

```
validate-rules.sh (PostToolUse on Write/Edit)
  -> writ-session.py read <session_id> -> gets loaded_rules
  -> for each stored rule: extract violation patterns, scan written file
  -> if violation pattern matches:
     -> emit warning to stderr (Claude sees it, not blocking)
     -> log to session pending_violations: [{rule_id, file, line, evidence}]
  -> check: are all planned files written? (files_written >= plan.md file list)
     -> no: stay in warning mode, exit
     -> yes: enter phase-boundary mode (see call-path 4)
```

### 4. Phase-boundary rule compliance (gate routing)

```
validate-rules.sh (phase-boundary mode, all planned files written)
  -> scan ALL files_written from session
  -> pattern match against stored rules
  -> for each violation still present:
     -> read plan.md "## Rules Applied" section
     -> if violated rule_id NOT in plan:
        -> planning gap: writ-session.py invalidate-gate <session_id> phase-a
           --rule <rule_id> --file <path> --evidence <text>
        -> session stores invalidation record, deletes gate .approved file
        -> increments cycle counter for that gate
     -> if violated rule_id IS in plan:
        -> implementation error: emit as error to stderr
        -> no gate invalidation
        -> Claude expected to fix on next edit
```

### 5. Cycle escalation

```
writ-session.py invalidate-gate (cycle >= 3)
  -> sets escalation_needed: true in session
  -> next turn: writ-rag-inject.sh detects escalation flag
  -> reads invalidation history for the gate
  -> analyzes rule distribution across cycles:
     -> same rule every cycle: "possibly over-broad rule pattern"
     -> different rule every cycle: "plan broadly missing rule coverage"
     -> mixed: "specific gaps in plan"
  -> injects differential diagnosis, blocks automated work
  -> POST /feedback with enriched negative signal (cycle count, rule_id)
```

### 6. Backward context on re-planning

```
writ-rag-inject.sh (gate missing + invalidation records exist)
  -> reads invalidation records from session
  -> injects:
     [Writ: phase-a INVALIDATED -- cycle N of 3]
     Previous plan failed validation:
     - <rule_id> violated in <file>:<line> (<evidence>)
     Revise plan to address this gap.
     Previous plan hash: <hash> (do not resubmit unchanged)
```

## Rules applied

- ARCH-FUNC-001: validate-rules.sh decomposed into focused functions (pattern
  extraction, file scanning, plan correlation, gate invalidation)
- PY-PYDANTIC-001: invalidation records and stored rules use structured schemas
- ARCH-DRY-001: session read/write centralized in writ-session.py; new commands
  extend existing patterns
- PERF-IO-001: rule compliance reads from session cache (/tmp), no network I/O
  in validation hot path
- PERF-BIGO-001: violation pattern matching is O(rules * files), both bounded
  (rules capped by session budget, files capped by plan scope)

## Design decisions

### Tier 1 behavior in validate-rules.sh

Tier 1 has no plan.md, no gates, no phases. validate-rules.sh detects missing
plan.md and stays in warning-only mode for the entire session. No phase-boundary
mode, no gate routing, no invalidation. This is an explicit early-return path,
not an implicit fallthrough. Per-write warnings still fire for pattern matches.

### Hook execution order independence

Claude Code runs hooks in parallel, not sequentially. validate-rules.sh cannot
assume validate-file.sh (static analysis) has completed. The hook reads
analysis_results[file] from the session cache:
- "pass": proceed with rule compliance check
- "fail" or absent: skip rule compliance for that file (log "skipped: awaiting
  static analysis")
This means rule compliance feedback is delayed by one turn for newly written
files. First write triggers static analysis; second write (after fixes) triggers
rule compliance. Acceptable because per-write warnings still fire immediately
for obvious pattern matches regardless of analysis state.

### Form-level verification, not substance

Phase checklists verify structure (section exists, rule IDs present, no violation
patterns in code). They do not verify justification quality. That boundary is
explicit: machine verification eliminates "skipped the work entirely" failures.
Substance review remains the human's job at gate approval.

### Trace is human context only

The trace field in invalidation records is Claude-generated narrative. It goes into
the record for human review at escalation. It never drives automated routing. The
routing decision is form-based: grep plan.md for the violated rule ID.

### Phase-boundary detection

validate-rules.sh compares session files_written against plan.md file list. When
files_written is a superset of planned files, phase-boundary mode activates. If
Claude abandons a planned file without updating plan.md, the system stays in warning
mode -- a silent failure caught at human gate review.

### Cycle limits

Per-gate cycle counter. Escalation at 3. Differential diagnosis analyzes whether
same rule repeated (possibly over-broad), different rules each time (broadly
deficient plan), or mixed. Escalation posts enriched negative feedback to Writ
server for rule quality tracking.

## Capabilities

- [x] C1: Full rule objects stored in session cache
- [x] C2: Phase-specific checklists with exit criteria (checklists.json)
- [x] C3: Per-write violation warnings (intermediate feedback mode)
- [x] C4: Phase-boundary violation detection (completion detection)
- [x] C5: Gate invalidation with structured failure context
- [x] C6: Routing heuristic (planning gap vs implementation error)
- [x] C7: Cycle counter with escalation at 3
- [x] C8: Differential diagnosis at escalation
- [x] C9: Backward context injection on re-planning
- [x] C10: Enriched negative feedback to Writ server on escalation
