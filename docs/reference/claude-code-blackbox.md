# The Claude Code Hook Black Box

A map of exactly what Claude Code hands your code while it works, and what your code can hand
back, for one exact version. Written so a non-engineer can follow the idea and an engineer can
build against the detail.

---

## Part 1: Plain-English guide (read this first)

### What is a "hook"?

While Claude Code works (reading the user's prompt, running a command, writing a file), it pauses
at fixed checkpoints and hands a small script of yours a sealed envelope of information. Your
script looks inside, then decides: wave it through, stop it, or quietly change what is about to
happen. Those checkpoints are called hooks. Each kind of moment is a different hook "event"
(for example: "the user just submitted a prompt", "a command is about to run", "a file was just
written").

### What is the "black box"?

The black box is the exact contents of that envelope (what Claude Code tells your script) and the
exact set of replies your script is allowed to send back (how your script can steer Claude). It is
"black" because Anthropic does not publish a complete, version-pinned list of it, and the public
docs do not always match what the software actually does. This document is the result of opening
the box on a real machine and writing down what is truly there.

### The one big idea: steer the AI at the checkpoint, not in the prompt

The most valuable thing in the box is this: at the "about to run a tool" checkpoint, your script
can rewrite what the AI is about to do, and the AI never sees the change. We verified this live:
the AI asked to run a generic helper, and the checkpoint script silently swapped in the correct
governed helper. The AI proceeded with the swapped version.

Why this matters to the business:
- **Control.** You can enforce company rules (security, process, quality) on the AI's actions at
  the moment of action, not by hoping a long instruction sticks.
- **Cost.** Steering at the checkpoint costs zero extra words in the conversation. The usual
  alternative, stuffing rules into every prompt, burns tokens on every single turn. (Our own
  always-on rule system used this finding to cut its per-turn rule text by about 75 percent.)
- **Reliability.** A rule enforced by code at the checkpoint cannot be argued with or forgotten.

Analogy: think of an air-traffic controller who can redirect a plane to the correct runway without
the pilot needing fresh instructions. The plane lands correctly; the pilot just flies.

### Why version matters

Everything here is true for one build (version 2.1.183). Claude Code changes between versions:
field names appear, events get added. Treat this document as a snapshot, not a permanent contract.
The header below records the exact build, and Part 5 explains how to re-capture it for a new
version in a few minutes.

### Glossary (the jargon used in Part 2)

- **Event:** a kind of checkpoint (UserPromptSubmit, PreToolUse, etc.).
- **Tool:** an action Claude can take (Bash command, Write a file, Edit, Read, etc.).
- **Envelope / payload:** the JSON bundle of information your script receives.
- **stdin / stdout:** how the script reads the envelope (stdin) and replies (stdout).
- **tool_input:** the arguments of the action about to run (the file path, the command text).
- **tool_response:** the result of the action after it ran.
- **Exit code:** a number a script returns; 0 means OK, 2 means "block this".
- **Force-swap / `updatedInput`:** the reply that rewrites the action before it runs.
- **Token:** a unit of text the AI is billed on; fewer tokens means lower cost and latency.

### How to read the evidence tags

Every fact in Part 2 carries a tag so you know how sure we are:

- **[observed]** We saw it in real data from this exact build. Strongest evidence.
- **[observed: transcript]** We saw it in the build's own conversation log (which records every
  tool call and result), and we verified that log matches what the hook receives.
- **[doc]** The official documentation says so, but we did not see it happen on this build.
- **[both]** Real data and the documentation agree.
- **UNVERIFIED** We could not confirm it either way on this build.

---

## Part 2: Technical reference (build 2.1.183)

### Header

| Field | Value |
|---|---|
| Claude Code version | `2.1.183 (Claude Code)` [observed] |
| Model in session | `claude-opus-4-8[1m]` (from SessionStart.model) [observed] |
| Capture date | 2026-06-19 [observed] |
| Host OS | Linux 6.17.0-35-generic x86_64, Ubuntu 24.04.3 LTS [observed] |
| Live capture source | `~/.claude/writ-blackbox.jsonl` (raw envelopes, this build) [observed] |
| Doc reference | https://code.claude.com/docs/en/hooks [doc] |
| Doc reference (alias) | https://docs.anthropic.com/en/docs/claude-code/hooks redirects to the page above [doc] |

This map is build-specific; schema claims are scoped to 2.1.183. The doc page carries no version
number (its only inline version notes are v2.1.139 and v2.1.141), so it is the closest live
reference but is not pinned to this build. Where the live capture and the docs disagree, the live
capture wins for this build and both are recorded.

### The envelope at a glance

Each captured line in the log is `{ts, hook, direction, payload, pid, session}` [observed]. Two
things trip people up:
- `hook` is the hook SCRIPT name (for example `validate-file`), NOT the event name. The event name
  is inside, at `payload.hook_event_name`.
- `payload` is a JSON string, so it must be decoded twice. `direction` is `in` (the envelope
  Claude Code delivered) or `out` (what the script sent back). The authoritative session key is
  `payload.session_id`.

### Event index

One row per event. "Cadence" is how often it fires. "Can block (exit 2)" is what returning exit
code 2 does. "Rewrite surface" is the reply that changes the pending action (see Part 2, Rewrite
surfaces). The last column says whether we saw it on this build (OBSERVED) or only in the docs
(DOC-ONLY).

| Event | Cadence | Matcher field | Can block (exit 2) | Rewrite surface | Source |
|---|---|---|---|---|---|
| UserPromptSubmit | per turn, before processing | none | Yes: blocks and erases the prompt [doc] | additionalContext (adds text only) | OBSERVED |
| PreToolUse | per tool call, before it runs | `tool_name` | Yes: blocks the tool call [doc] | `updatedInput` (replaces tool_input); `permissionDecision` gates the run | OBSERVED |
| PostToolUse | per tool call, after success | `tool_name` | No: the tool already ran [doc] | `updatedToolOutput` (replaces tool_response) [doc] | OBSERVED |
| PostToolUseFailure | per tool call, after a failure | `tool_name` | No [doc] | none | OBSERVED (fires for some failures only; see below) |
| SessionStart | session begins or resumes | `source` | No [doc] | initialUserMessage, sessionTitle, watchPaths, reloadSkills [doc] | OBSERVED |
| SubagentStart | a subagent is spawned | `agent_type` | No [doc] | additionalContext (adds text) | OBSERVED |
| SubagentStop | a subagent finishes | `agent_type` | Yes: prevents the subagent stopping [doc] | additionalContext [doc] | OBSERVED |
| Stop | main agent about to stop | none | Yes: prevents stop, continues the turn [doc] | additionalContext [doc] | DOC-ONLY (capturable at turn end) |
| CwdChanged | working directory changes | none | No [doc] | none | DOC-ONLY (did not fire for an in-command `cd`; see Gaps) |
| PreCompact | before history compaction | `trigger` (manual, auto) | Yes: blocks compaction [doc] | none | DOC-ONLY (needs a `/compact`) |
| PostCompact | after history compaction | `trigger` (manual, auto) | No [doc] | none | DOC-ONLY (needs a `/compact`) |
| SessionEnd | session ends | `reason` | No [doc] | none | DOC-ONLY |
| StopFailure | turn ends on an API error | `error_type` | No: output and exit ignored [doc] | none | DOC-ONLY |
| PostToolBatch | after a parallel tool batch resolves | none | Yes: stops the loop before next model call [doc] | none | DOC-ONLY (did not fire when forced; see Gaps) |
| PermissionRequest | a permission dialog appears | `tool_name` | Yes: denies the permission [doc] | `decision.updatedInput` (when behavior=allow) [doc] | DOC-ONLY |
| PermissionDenied | a tool was denied | `tool_name` | No: exit ignored, use `retry:true` [doc] | none | DOC-ONLY |
| UserPromptExpansion | a typed command expands to a prompt | `command_name` / `command` | Yes: blocks the expansion [doc] | none | DOC-ONLY |
| Setup | session init | `trigger` (init, maintenance) | No [doc] | none | DOC-ONLY (needs `--init`) |
| InstructionsLoaded | a CLAUDE.md or rules file loads | `load_reason` | No: exit ignored [doc] | none | DOC-ONLY (did not fire; see Gaps) |
| ConfigChange | a config file changes mid-session | `config_source` / `source` | Yes, except `policy_settings` [doc] | none | DOC-ONLY |
| FileChanged | a watched file changes | literal filenames | No [doc] | none | DOC-ONLY |
| Notification | Claude Code emits a notification | `notification_type` / `type` | No [doc] | none | DOC-ONLY (did not fire; see Gaps) |
| MessageDisplay | a message is about to display | none | No: display only [doc] | `displayContent` (on-screen only) [doc] | DOC-ONLY (did not fire; see Gaps) |
| TaskCreated | a task is created | none | Yes: rolls back creation [doc] | none | DOC-ONLY (not observed; see Gaps) |
| TaskCompleted | a task is marked complete | none | Yes: prevents completion [doc] | none | DOC-ONLY (not observed; see Gaps) |
| TeammateIdle | a teammate is about to idle | none | Yes: keeps it working [doc] | none | DOC-ONLY |
| WorktreeCreate | a worktree is created | none | Yes: any non-zero exit fails creation [doc] | stdout path or `worktreePath` | DOC-ONLY |
| WorktreeRemove | a worktree is removed | none | No [doc] | none | DOC-ONLY |
| Elicitation | an MCP tool requests a form | MCP server name | Yes: denies the elicitation [doc] | `action` + `content` | DOC-ONLY |
| ElicitationResult | a user submits an MCP form | MCP server name | Yes: blocks the response [doc] | `action` + `content` | DOC-ONLY |

Important practical limit: **PreToolUse and PostToolUse fire only for the tools your configuration
subscribes to.** In this build's config the subscribed tools were `Agent`, `Bash`, `Edit`,
`Write`. Other tools ran (`Read`, `NotebookEdit`, and the `TaskCreate`/`TaskUpdate` family) but
fired no PreToolUse/PostToolUse hook at all [observed]. So which actions you can gate is set by
your matcher list, not by Claude Code.

### Common input fields

These fields appear across events. "Universal" means on every captured event; the rest are
conditional.

| Field | Where it appears | Source | Notes |
|---|---|---|---|
| `session_id` | universal | [both] | session identifier (the authoritative key) |
| `transcript_path` | universal | [both] | absolute path to the conversation log |
| `cwd` | universal | [both] | working directory at hook time |
| `hook_event_name` | universal | [both] | the event that fired |
| `permission_mode` | UserPromptSubmit, Pre/PostToolUse, PostToolUseFailure, SubagentStop | [observed] | NOT on SessionStart or SubagentStart [observed] |
| `effort` `{level}` | Pre/PostToolUse, PostToolUseFailure | [observed] | the model's reasoning-effort level; absent on UserPromptSubmit, SessionStart, SubagentStart |
| `tool_name`, `tool_input` | Pre/PostToolUse, PostToolUseFailure | [both] | the action and its arguments |
| `tool_use_id` | Pre/PostToolUse, PostToolUseFailure | [observed] | ties a PreToolUse to its PostToolUse |
| `tool_response` | PostToolUse | [both] | the action's result (per-tool shape below) |
| `duration_ms` | PostToolUse, PostToolUseFailure | [observed] | how long the tool took |
| `error` | PostToolUseFailure | [observed] | the failure message, a STRING (see below) |
| `is_interrupt` | PostToolUseFailure | [observed] | whether the failure was an interrupt |
| `prompt` | UserPromptSubmit; SubagentStart (sometimes) | [observed] | the user's text / the subagent's task |
| `source` | SessionStart | [observed] | how the session started (observed: `startup`) |
| `model` | SessionStart | [observed] | observed: `claude-opus-4-8[1m]` |
| `agent_id`, `agent_type` | SubagentStart, SubagentStop, and tool events inside a subagent | [both] | values seen: `general-purpose`, `Explore`, `writ-explorer`, `workflow-subagent` |
| `stop_hook_active` | SubagentStop, Stop | [observed] | true while Claude is re-invoking a stop hook after a block |
| `agent_transcript_path`, `last_assistant_message`, `background_tasks`, `session_crons` | SubagentStop | [observed] | the subagent's log, its last message, and (observed empty) task/cron lists |

Observed `permission_mode` values: `default`, `auto`, `plan`. Documented set also includes
`acceptEdits`, `dontAsk`, `bypassPermissions` [doc].
Observed `effort.level` values: `high`, `xhigh`. Documented set: `low`, `medium`, `high`, `xhigh`,
`max` [doc].

### Per-event input (exact fields seen on this build)

UserPromptSubmit [observed]: `session_id, transcript_path, cwd, permission_mode, hook_event_name,
prompt`.

PreToolUse [observed]: `session_id, transcript_path, cwd, permission_mode, effort{level},
hook_event_name, tool_name, tool_input, tool_use_id` (plus `agent_id, agent_type` when the call
runs inside a subagent).

PostToolUse [observed]: all PreToolUse fields, plus `tool_response, duration_ms`.

PostToolUseFailure [observed]: `session_id, transcript_path, cwd, permission_mode, effort{level},
hook_event_name, tool_name, tool_input, tool_use_id, error, is_interrupt, duration_ms`.

SessionStart [observed]: `session_id, transcript_path, cwd, hook_event_name, source, model`.

SubagentStart [observed]: `session_id, transcript_path, cwd, agent_id, agent_type,
hook_event_name` (plus `prompt` sometimes). No `permission_mode`, no `effort`, no tool fields.

SubagentStop [observed]: `session_id, transcript_path, cwd, permission_mode, agent_id, agent_type,
hook_event_name, stop_hook_active, agent_transcript_path, last_assistant_message, background_tasks,
session_crons`.

For the events still only in the docs (Stop, SessionEnd, Setup, PreCompact, PostCompact,
CwdChanged, the permission and worktree and elicitation families), the documented input fields are
listed in the doc-only table at the end of this section.

### tool_input per tool (the arguments of an action)

Names are snake_case, as sent. "no hook" means the tool ran but fired no PreToolUse/PostToolUse on
this build, so its schema comes from the transcript only.

| Tool | tool_input fields | Source |
|---|---|---|
| Bash | `command`, `description`, `timeout`, `run_in_background`, `dangerouslyDisableSandbox` | `command` [both]; rest [observed] |
| Edit | `file_path`, `old_string`, `new_string`, `replace_all` | [both] |
| Write | `file_path`, `content` | [both] |
| Read | `file_path`, `limit`, `offset`, `pages` | [observed: transcript] (no hook) |
| Glob | `pattern`, `path` | [observed: transcript] (no hook) |
| Grep | `pattern`, `path`, `output_mode`, `-n`, `-A`, `head_limit` (other flags appear as keys) | [observed: transcript] (no hook) |
| WebFetch | `url`, `prompt`, `limit` | [both] |
| WebSearch | `query`, `allowed_domains` | [observed: transcript] |
| Agent (subagent dispatch) | `subagent_type`, `prompt`, `description`, `run_in_background` | [observed]; last two appear only when set |
| NotebookEdit | `notebook_path`, `cell_id`, `edit_mode`, `new_source` | [observed: transcript] (no hook) |
| TaskCreate | `subject`, `description`, `activeForm` | [observed: transcript] (no hook) |
| TaskUpdate | `taskId`, `status` | [observed: transcript] (no hook) |
| MultiEdit | NOT PRESENT in 2.1.183 (no such tool; the only edit primitive is `Edit`) | [observed] |
| TodoWrite | NOT PRESENT in 2.1.183 (task tracking is the `TaskCreate` / `TaskUpdate` family) | [observed] |

### tool_response per tool (the result of an action)

Inner field names are mostly camelCase. The docs describe `tool_response` only as a generic object
or string [doc]; these shapes are observed. The transcript's stored result equals the hook's
`tool_response` where both were captured (Edit, Write, Bash, WebFetch).

| Tool | tool_response fields | Source |
|---|---|---|
| Bash | `stdout`, `stderr`, `interrupted`, `isImage`, `noOutputExpected` | [both]. The transcript adds `assistantAutoBackgrounded`, `backgroundTaskId`, `gitOperation`, `returnCodeInterpretation`, `staleReadFileStateHint` |
| Edit | `filePath`, `oldString`, `newString`, `originalFile`, `structuredPatch`, `userModified`, `replaceAll` | [both] |
| Write | `type`, `filePath`, `content`, `originalFile`, `structuredPatch`, `userModified` | [both] |
| Read | `type`, `file` (a nested object: `filePath`, plus `cells[...]` for a notebook) | [observed: transcript] |
| Glob | `filenames`, `numFiles`, `truncated`, `durationMs` | [observed: transcript] |
| Grep | `mode`, `content`, `filenames`, `numFiles`, `numLines`, `numMatches` | [observed: transcript] |
| WebFetch | `bytes`, `code`, `codeText`, `result`, `durationMs`, `url` | [both] |
| WebSearch | `query`, `results`, `searchCount`, `durationSeconds` | [observed: transcript] |
| NotebookEdit | `new_source`, `cell_type`, `language`, `edit_mode`, `cell_id`, `error`, `notebook_path`, `original_file`, `updated_file` | [observed: transcript] |
| TaskCreate | `task` (a nested `{id, subject}`) | [observed: transcript] |
| TaskUpdate | `success`, `taskId`, `updatedFields[]`, `statusChange{from, to}` | [observed: transcript] |

### When a tool fails

There are two distinct failure paths on this build, and they behave differently:

1. The PostToolUseFailure event fires, with an `error` STRING field. We forced five failures; the
   event fired for a Read of a missing path and a Bash command that exited non-zero. **The failure
   field is named `error` and is a string. It is NOT `tool_error` and NOT `tool_response`** (this
   settles a disagreement between the two doc readings; neither was correct) [observed].
2. The event does NOT fire, and the failure is returned to the model as a result instead. This
   happened for an Edit with a non-unique or absent `old_string` and a Write whose parent path was
   a file (ENOTDIR). No hook envelope is produced at all [observed]. The internal reason for the
   split is UNVERIFIED.

Takeaway for builders: do not rely on PostToolUseFailure to catch every tool error on this build.
Some errors never reach a hook.

### Output contract (how your script replies)

Exit codes [doc]:

| Exit | Effect |
|---|---|
| 0 | Success. stdout is read as JSON. For most events stdout goes to the debug log; for UserPromptSubmit, UserPromptExpansion, and SessionStart, stdout is added as context the AI can see. |
| 2 | Block. stdout and JSON are ignored; stderr is sent to the AI as an error. The exact effect is per-event (see the Event index). |
| other non-zero | Non-blocking error for most hooks. The transcript shows a hook-error notice plus the first line of stderr. |

Observed gating behavior on this build [observed]: our gate never uses exit 2. It replies with
exit 0 plus JSON.
- **Allow is silence:** an allowed action produces exit 0 and NO output.
- **Deny:** exit 0 with `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
  "deny", "permissionDecisionReason": "...", "additionalContext": "..."}}`.
- **Force-swap allow:** exit 0 with `permissionDecision: "allow"` plus `updatedInput`.

Universal JSON reply fields [doc]: `continue` (false stops Claude entirely), `stopReason` (shown
when `continue` is false), `suppressOutput`, `systemMessage`, `terminalSequence` (a terminal
escape sequence, added in v2.1.141), `decision` (usually `"block"`), `reason`, and
`hookSpecificOutput` (an object that must carry `hookEventName`, then per-event fields like
`additionalContext`, `permissionDecision`, `updatedInput`, `updatedToolOutput`, `displayContent`,
`retry`).

PreToolUse uses `permissionDecision` (`allow`, `deny`, `ask`, `defer`) inside `hookSpecificOutput`,
not the top-level `decision` [doc]. Observed values on this build: `allow`, `deny` [observed].

`additionalContext` (adds text for the AI to read, without changing the pending action) is
documented on PreToolUse, PostToolUse, UserPromptSubmit, Stop, SubagentStop, SessionStart, and
Setup [doc]. Observed correction: one doc reading claimed SubagentStart cannot add context; the
live capture shows a SubagentStart hook returned `additionalContext` and the subagent received it
[observed].

### Rewrite surfaces (the force-swap family)

These are the replies that change the pending action. The first row is verified live.

| Event | JSON path | What it does | Source |
|---|---|---|---|
| PreToolUse | `hookSpecificOutput.updatedInput` | Replaces the ENTIRE `tool_input` before the tool runs. Verified live: a dispatch with `subagent_type: "general-purpose"` was rewritten to `writ-explorer` and Claude launched `writ-explorer`. Pass the whole object back; it is a full replacement, not a merge. | [both], verified live |
| PreToolUse | `hookSpecificOutput.permissionDecision` | `allow` / `deny` / `ask` / `defer`; gates whether the tool runs. | [both]; allow/deny observed |
| PostToolUse | `hookSpecificOutput.updatedToolOutput` | Replaces the result shown to the AI. | [doc] |
| PermissionRequest | `hookSpecificOutput.decision.updatedInput` | Modifies the tool input when allowing. | [doc] |
| MessageDisplay | `hookSpecificOutput.displayContent` | Replaces on-screen text only; does NOT change the saved transcript. | [doc] |
| SessionStart | `hookSpecificOutput.initialUserMessage / sessionTitle / watchPaths / reloadSkills` | Seeds the first message, sets the title, registers watched paths, re-scans skills. | [doc] |
| Elicitation / ElicitationResult | `hookSpecificOutput.action` + `content` | Supplies or overrides MCP form values. | [doc] |
| WorktreeCreate | stdout path or `hookSpecificOutput.worktreePath` | Sets the created worktree path; a missing path fails creation. | [doc] |
| PermissionDenied | `hookSpecificOutput.retry` | `true` lets the model retry the denied call. | [doc] |

Force-swap recipe (PreToolUse), the verified shape on this build:

```bash
#!/bin/bash
d=$(cat)
printf '%s' "$d" | python3 -c '
import sys, json
d  = json.load(sys.stdin)
ti = d.get("tool_input", {})
if ti.get("subagent_type") == "general-purpose":
    ti["subagent_type"] = "writ-explorer"     # your swap
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": ti}}))                      # whole object back
'
```

### The subagent-spawn naming question (recorded, not resolved)

A common confusion: what is the tool that spawns a sub-agent called? Three different names are in
play, so all are recorded.

| Source | Name it uses | Detail |
|---|---|---|
| Live PreToolUse envelope | `tool_name: "Agent"` | The runtime tool. Carries `tool_input.subagent_type`. [observed] |
| The hook config matcher | `Task` | Registered as matcher `"Task"`, yet it fired on the `Agent` tool. So the `Task` matcher catches the `Agent` tool on this build. [observed] |
| Doc reference | `TaskCreate` | The docs name a `TaskCreate` tool with TaskCreated/TaskCompleted events, never "Task" or "Agent" for spawning. [doc] |
| Tool registry | `TaskCreate`, `TaskGet`, `TaskList`, `TaskStop`, `TaskUpdate` exist as tools, alongside an `Agent` tool. [observed] |

Practical guidance: to force-swap a dispatch, match `Task` (it catches the runtime `Agent` tool on
this build) and read/write `tool_input.subagent_type`. This is the path verified live.

### Gaps (what is still not nailed down on this build)

Closed since the first pass: per-tool input/result schemas for all tools that exist; the
PostToolUseFailure envelope (field is `error`, a string); the SessionStart, SubagentStart, and
SubagentStop envelopes.

Still open:
1. **CwdChanged did not fire** for a directory change made inside a Bash command (`cd` within one
   command). The change took effect (later envelopes carried the new directory), but no CwdChanged
   envelope appeared. UNVERIFIED whether it fires for a genuine Claude Code working-directory
   change; a per-command shell reset may suppress the in-command case.
2. **PreCompact and PostCompact** need a `/compact` to be run in the session; not yet captured.
3. **Events that did not fire when their conditions were met**, so their existence on this build is
   UNVERIFIED: PostToolBatch (forced three parallel reads), InstructionsLoaded (a CLAUDE.md was
   loaded), MessageDisplay and Notification (none appeared). The docs describe these; this build
   did not produce them under those triggers.
4. **TaskCreated and TaskCompleted appear to be doc-only on this build.** Creating and completing a
   task via the `TaskCreate` / `TaskUpdate` tools produced no such lifecycle event; those are
   ordinary tools, not events.
5. **Events that need a special launch condition** (not triggered here, each with its condition):
   Setup (`--init` / `--init-only` / `--maintenance`); PermissionRequest and PermissionDenied (a
   tool call that hits the permission flow, not an auto-allow mode); StopFailure (an API error
   mid-turn); TeammateIdle (an agent-teams session); FileChanged (`watchPaths` registered, then a
   watched file changes); WorktreeCreate and WorktreeRemove (`--worktree` or an agent with
   `isolation: worktree`); Elicitation and ElicitationResult (an MCP server elicitation);
   ConfigChange (edit a settings file mid-session); UserPromptExpansion (invoke a slash command).
6. The two doc URLs are one page (a redirect), so there is no independent second documentation
   source.

---

## Part 3: How to re-capture this for a new version

This map is a snapshot. To rebuild it on a new build in a few minutes, in a throwaway directory:
turn capture on (`touch ~/.claude/writ-blackbox.on`), exercise every tool and event you can
(write/edit/read files, run commands, spawn a subagent, force a few failures, run `/compact`), then
read two sources: the capture log `~/.claude/writ-blackbox.jsonl` for hook envelopes, and the
session's own transcript for per-tool input and result shapes. Tag every field by source, mark
what would not fire, and re-stamp the version. Turn capture off when done (`rm
~/.claude/writ-blackbox.on`).
