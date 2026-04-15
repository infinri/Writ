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

Spawn workers with `run_in_background=true`. Keeps the CLI scrollback clean
(no in-place `Cascading…` spinner redrawing) and reduces visual noise when
copy-pasting transcripts. The workflow is still sequential -- you wait for
each worker to complete before spawning the next -- but the progress
indicator stays off-screen. You get a notification when the worker
finishes; read its summary then and decide the next step.

## Constraints

- You handle all user approvals -- workers never interact with the user
- Each worker gets its own Writ session (fresh RAG budget, isolated state)
- All Writ hooks fire inside workers (gates, RAG, validation)
- Pass exploration results and plan content as text in the worker's prompt
