---
rule_id: ENF-PROC-WORKTREE-001
domain: process
severity: high
scope: task
trigger: "When the agent runs git worktree add <path> where <path> is inside the repo tree and is not listed in .gitignore."
statement: "Project-local worktree directories must be gitignored. Bash gate denies 'git worktree add' commands targeting non-ignored repo-local paths."
violation: "Agent runs 'git worktree add ./work_trees/feature-x' without adding './work_trees/' to .gitignore. Gate denies the Bash call."
pass_example: "Agent confirms .gitignore contains '.worktrees/' or equivalent, then runs 'git worktree add .worktrees/feature-x'. Gate permits."
enforcement: "writ-worktree-safety.sh on PreToolUse Bash matching 'git worktree add': parse target path, check .gitignore, deny if project-local and not ignored."
rationale: "Non-ignored project-local worktrees pollute the main branch's working tree and cause accidental commits. The safety check is absolute, not advisory."
mandatory: true
always_on: false
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
mechanical_enforcement_path: ".claude/hooks/writ-worktree-safety.sh"
rationalization_counters:
  - { thought: "I'll add to gitignore later.", counter: "Later means never. Add first, then create the worktree." }
  - { thought: "It's fine, I'll remember to clean up.", counter: "Memory is not a safety mechanism. gitignore is." }
red_flag_thoughts:
  - "Skip the gitignore check"
  - "I'm sure it's fine"
  - "Just this once"
tags: [enforcement, git, gitignore, process, worktree]
source_attribution: "superpowers@5.0.7"
source_commit: null
body: ""
edges:
  - { target: TEC-PROC-WORKTREE-001, type: TEACHES }
---

# Rule: Worktree gitignore safety

Mechanical via `writ-worktree-safety.sh`.
