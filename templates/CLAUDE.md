# Global -- Claude Code Instructions

Loaded in every session, every project.

**Always read at session start:** `~/.claude/memory/GLOBAL.md`

---

## Memory tiers

| Tier | Location | Use for |
|---|---|---|
| Global | This file + `~/.claude/memory/` | Cross-project facts, preferences, corrections |
| Skill | Committed files in `~/.claude/skills/` | Domain knowledge -- versioned rulebooks, not memory files |
| Project | `~/.claude/projects/{encoded}/memory/MEMORY.md` | Project-specific context only |

When something is learned that should persist:
- Applies to all projects -> update this file or `~/.claude/memory/GLOBAL.md`
- Applies whenever Writ skill is active -> commit to the skill's knowledge base
- Applies to one project -> write to that project's auto-memory dir

---

## Mandatory workflow -- before any task

Rules are injected automatically by hooks. Your job is to follow the workflow.

### Step 1: Set the mode

Before writing ANY code, set the session mode. The RAG inject hook prints the
exact `mode set` command with paths filled in. Run it.

| Mode | Purpose | Code generation |
|------|---------|-----------------|
| Conversation | Discussion, brainstorming, questions | No |
| Debug | Investigating a specific problem | No |
| Review | Evaluating code against rules | No |
| Work | Building or modifying code | Yes (full workflow) |

If you skip this, gate hooks deny all writes (except plan.md). Setting a mode
unblocks the workflow.

### Step 2: Follow the mode's workflow

- **Conversation:** No ceremony. Discuss, answer questions, brainstorm.
- **Debug:** Investigate. Read logs, trace execution, form hypothesis. When fix
  is identified, recommend switching to Work mode.
- **Review:** Evaluate code against Writ rules. Produce structured findings per file.
  When findings require code changes, recommend switching to Work mode.
- **Work:** Full workflow. Enter /plan, write plan.md TO THE PROJECT ROOT
  (## Files, ## Analysis, ## Rules Applied, ## Capabilities) + capabilities.md.
  Exit /plan (format auto-validated, but gate is NOT created yet).
  Present plan, WAIT for user approval (gate created on approval).
  Write test skeleton FILES TO DISK, WAIT for approval. Then implement.
  Update capabilities.md to check off completed items.

### Step 3: Wait for user approval (Work mode only)

After presenting the plan or test skeletons, STOP and tell the user:
"Say **approved** to proceed."
When the user says "approved", a hook automatically creates the gate file.
NEVER create gate files yourself or run commands to approve gates.

### When Writ is unavailable

If the server is not running, hooks fall back gracefully. You will see a warning.
Proceed normally.

### Project-specific routing

If the project has its own `.claude/CLAUDE.md` with mode-specific or phase-specific
instructions, follow those. They override the generic workflow above.

---

## Global preferences

- No emojis unless explicitly requested
- No em dashes. Use standard punctuation: hyphens (-), double hyphens (--), commas, colons, or semicolons.
- Confirm before: pushing to remote, deleting files/branches, force-pushing, amending published commits
- Short, direct responses -- lead with the answer, not the reasoning
- Do not add comments, error handling, or abstractions beyond what was asked
