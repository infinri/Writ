# Writ orchestrator -- Work mode sub-agent dispatch

In Work mode, delegate each phase to a named sub-agent worker.
You are the orchestrator. You manage mode, gates, and approvals.

## Set mode with --orchestrator

Before dispatching any worker, set Work mode with the `--orchestrator` flag:

```
python3 $SKILL_DIR/bin/lib/writ-session.py mode set work $SESSION_ID --orchestrator
```

Why: the flag sets `is_orchestrator: true` in the master's session cache.
`writ-rag-inject.sh` then suppresses the ~1400-token broad RAG injection on
every UserPromptSubmit in the master session and emits a compact status line
instead. Without the flag, the master accumulates ~3000+ tokens of rule
injections it does not need (workers already have their own RAG budget) --
you will see "broad" rag_query events in the friction log if this is wrong.

## Dispatch sequence

1. **Explore**: Spawn `writ-explorer` with the user's task description.
   Save the results -- they feed into the planner.

2. **Plan**: Spawn `writ-planner` with the task + exploration results.
   It writes plan.md and capabilities.md to the project root.
   Present the plan summary. Say: "Say **approved** to proceed."

3. **Test skeletons**: After approval, spawn `writ-test-writer` with
   the plan.md content. It writes test files to disk.
   Present test class names and counts only.
   Say: "Say **approved** to proceed to implementation."

4. **Implement**: After approval, spawn `writ-implementer` with the
   plan.md content and test file paths. It writes all files.

## When NOT to use sub-agents

- Conversation, Debug, Review modes: never
- Small tasks (< 5 files): single-session is simpler
- When the user explicitly requests single-session work

## Execution mode

Spawn workers in the **foreground** (default -- do NOT pass
`run_in_background=true`). Foreground blocks the input prompt while the
worker runs, which is the intended UX: the user cannot accidentally send
mid-dispatch instructions that desync the pipeline. The terminal spinner
stays on screen; that is fine. Background mode exists for genuinely
parallel, independent work -- orchestrator phases are sequential and
each worker's output is a gate for the next.

## Constraints

- You handle all user approvals -- workers never interact with the user
- Each worker gets its own Writ session (fresh RAG budget, isolated state)
- Workers bypass mode/gate checks entirely (`is_subagent: true` is set by
  `writ-subagent-start.sh`). They do not set a mode, do not need to know
  about modes, and cannot be denied by the phase-a/test-skeletons gates.
  Their job is: do the assigned task, let RAG hooks surface relevant rules,
  report back.
- PostToolUse RAG still fires inside workers -- they get rule injection
  on every file write, same as the master would.
- Pass exploration results and plan content as text in the worker's prompt
