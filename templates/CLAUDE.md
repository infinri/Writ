# Global Claude Code Instructions

Loaded in every session, every project.

**Always read at session start:** `~/.claude/memory/GLOBAL.md`

Workflow rules, the mode system, gate enforcement, and orchestrator dispatch are delivered on demand via RAG-retrieved Methodology nodes (`SKL-PROC-MODE-001`, `PBK-PROC-WORK-WORKFLOW-001`, `PBK-PROC-ORCHESTRATOR-001`, `SKL-PROC-WRIT-FAILURE-001`). The `writ-rag-inject.sh` hook surfaces them at the right trigger.

A `[Writ: no matching rules found ...]` (NO_RULES) line with no `--- WRIT RULES ---` block means retrieval ran and nothing matched beyond the rules already shown. No Writ output at all, or a `[Writ: server unavailable ...]` line, means the Writ server is unavailable or the turn was skipped. Either way, proceed with normal engineering judgment; hooks will block destructive writes if mode is unset.

## Global preferences

- No emojis unless explicitly requested
- No em dashes and no double hyphens (`--`) used as em-dash substitutes. Use standard punctuation: hyphens (-) only when joining words, plus commas, colons, semicolons, or parentheses for clause breaks.
- Confirm before: pushing to remote, deleting files/branches, force-pushing, amending published commits
- Short, direct responses; lead with the answer, not the reasoning
- Do not add comments, error handling, or abstractions beyond what was asked
