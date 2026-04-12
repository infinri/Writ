# Writ orchestrator -- Work mode sub-agent dispatch

In Work mode, delegate each phase to a named sub-agent worker.
You are the orchestrator. You manage mode, gates, and approvals.

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

## Constraints

- You handle all user approvals -- workers never interact with the user
- Each worker gets its own Writ session (fresh RAG budget, isolated state)
- All Writ hooks fire inside workers (gates, RAG, validation)
- Pass exploration results and plan content as text in the worker's prompt
