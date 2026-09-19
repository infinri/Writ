# Claude Code Hook Black Box Reference

This document explains what Claude Code sends to hooks and what hooks can return.
It is designed as a quick reference for non-developers and junior developers.

It is a version-pinned, empirical map of exactly what Claude Code hands a hook script and
exactly what a script can hand back. Captured live on build 2.1.220 and compared against
2.1.183. Every single field carries an evidence tag: observed in real data, documented but not
seen, or unverified. The build pin covers the original capture, and this file has kept growing
since: it also carries findings observed on 2026-08-11 and 2026-08-14, each stamped with its own
date. Read the tag next to a claim rather than the version at the top.

It records five events that moved from documented only to actually observed, payload fields the
public changelog never announced, and the mechanism that lets a script rewrite a tool call
before it runs without the AI ever seeing the change. It is written so a non-engineer can follow
the idea in the opening sections and an engineer can build against the detail in the reference
tables that follow.

It is useful whether or not you use Writ. It is the reference this project wishes had existed.

## What this is

A **hook** is an automatic checkpoint in Claude Code. When an event happens, Claude Code sends a
JSON payload to your hook. The hook can log the event, add context, allow or deny an action, or in
some cases rewrite the action before it runs.

Typical flow:

1. Claude Code fires an event.
2. A configured matcher decides whether your hook should run.
3. Claude Code sends a JSON payload to the hook.
4. The hook returns nothing, an exit code, or a JSON response.
5. Claude Code continues, blocks, or changes the pending action.

The most useful event is `PreToolUse`: it can inspect, block, or fully replace a tool call before
the tool runs.

Official reference: [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

## Common input payload

Most hook events include these top-level keys.

| Key | Type | Meaning |
|---|---|---|
| `session_id` | string | Current Claude Code session ID. Use this as the main session key. |
| `prompt_id` | string | UUID for the current user turn. Optional before the first user prompt. |
| `transcript_path` | string | Path to the main session transcript. The file may lag behind the current turn. |
| `cwd` | string | Working directory when the hook fired. |
| `permission_mode` | string | Current mode, such as `default`, `plan`, `acceptEdits`, `auto`, `dontAsk`, or `bypassPermissions`. Not present on every event. |
| `effort` | object | Optional reasoning setting. Valid levels are `low`, `medium`, `high`, `xhigh`, and `max`. |
| `hook_event_name` | string | Name of the event that fired. |
| `agent_id` | string | Optional subagent ID when the event runs inside a subagent. |
| `agent_type` | string | Optional built-in or custom agent name. |

Minimal example:

```json
{
  "session_id": "abc123",
  "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/home/user/.claude/projects/.../transcript.jsonl",
  "cwd": "/home/user/project",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse"
}
```

## Event reference

The **input keys** column lists keys added to the common payload above.

| Event | When it fires | Matcher | Input keys | Hook can return |
|---|---|---|---|---|
| `SessionStart` | A session starts, resumes, clears, compacts, or forks | `source` | `source`, `model?`, `agent_type?`, `session_title?`, `seconds_since_last_response?`, `context_tokens?`, `prompt_cache_likely_expired?`, `estimated_cache_write_usd?` | `additionalContext`, `initialUserMessage`, `sessionTitle`, `watchPaths`, `reloadSkills` |
| `Setup` | Claude starts with an init or maintenance flag | `trigger` | `trigger` | No decision control; use side effects or `CLAUDE_ENV_FILE` |
| `InstructionsLoaded` | A `CLAUDE.md` or rules file loads | `load_reason` | `file_path`, `memory_type`, `load_reason`, `globs?`, `trigger_file_path?`, `parent_file_path?` | Side effects only |
| `UserPromptSubmit` | Before Claude processes a user prompt | none | `prompt` | `decision`, `reason`, `additionalContext`, `sessionTitle`, `suppressOriginalPrompt` |
| `UserPromptExpansion` | A typed slash command or MCP prompt expands | `command_name` | `expansion_type`, `command_name`, `command_args`, `command_source`, `prompt` | `decision`, `reason`, `additionalContext` |
| `MessageDisplay` | Assistant text is displayed | none | `turn_id`, `message_id`, `index`, `final`, `delta` | `displayContent` changes display only |
| `PreToolUse` | Before a tool executes | `tool_name` | `tool_name`, `tool_input`, `tool_use_id` | `permissionDecision`, `permissionDecisionReason`, `updatedInput`, `additionalContext` |
| `PermissionRequest` | Claude is about to request tool permission | `tool_name` | `tool_name`, `tool_input`, `permission_suggestions?` | `decision.behavior`, `decision.updatedInput`, `decision.updatedPermissions`, `decision.message`, `decision.interrupt` |
| `PostToolUse` | After a tool succeeds | `tool_name` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms?` | `decision`, `reason`, `additionalContext`, `classifierContext`, `updatedToolOutput`, `updatedMCPToolOutput` |
| `PostToolUseFailure` | After an executed tool fails | `tool_name` | `tool_name`, `tool_input`, `tool_use_id`, `error`, `is_interrupt?`, `duration_ms?` | `additionalContext` |
| `PostToolBatch` | After a parallel tool batch completes | none | `tool_calls[]` | `additionalContext`; may stop the loop |
| `PermissionDenied` | Auto mode denies a tool call | `tool_name` | `tool_name`, `tool_input`, `tool_use_id`, `reason` | `retry` |
| `Notification` | Claude Code sends a notification | `notification_type` | `message`, `title?`, `notification_type` | Side effects or `terminalSequence` only |
| `SubagentStart` | A subagent is spawned | `agent_type` | `agent_id`, `agent_type` | `additionalContext` for the new subagent |
| `SubagentStop` | A subagent finishes | `agent_type` | `stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message`, `background_tasks[]`, `session_crons[]` | `decision`, `reason`, `additionalContext` |
| `TaskCreated` | A task is being created | none | `task_id`, `task_subject`, `task_description?`, `teammate_name?`, `team_name?` | Block creation with exit `2` or `decision: "block"` |
| `TaskCompleted` | A task is being marked complete | none | `task_id`, `task_subject`, `task_description?`, `teammate_name?`, `team_name?` | Block completion with exit `2` |
| `Stop` | The main agent is about to finish its response | none | `stop_hook_active`, `last_assistant_message`, `background_tasks[]`, `session_crons[]` | `decision`, `reason`, `additionalContext` |
| `StopFailure` | A turn ends because of an API error | `error` | `error`, `error_details?`, `last_assistant_message?` | Side effects or `terminalSequence` only |
| `TeammateIdle` | A team agent is about to become idle | none | `teammate_name`, `team_name?` | Exit `2` keeps it working; `continue: false` stops it |
| `ConfigChange` | A settings, policy, or skill file changes | `source` | `source`, `file_path?` | Block with exit `2` or `decision: "block"`; policy settings cannot be blocked |
| `CwdChanged` | Claude Code changes working directory | none | `old_cwd`, `new_cwd` | `watchPaths`; cannot block |
| `DirectoryAdded` | `/add-dir` or SDK adds a working directory | `source` | `directory`, `source` | Side effects only |
| `FileChanged` | A watched file changes on disk | literal filenames | `file_path`, `event` | `watchPaths`; cannot block |
| `WorktreeCreate` | Claude Code is creating an isolated worktree | none | `name` | Command stdout path or `worktreePath` |
| `WorktreeRemove` | Claude Code is removing a worktree | none | `worktree_path` | Side effects only |
| `PreCompact` | Before context compaction | `trigger` | `trigger`, `custom_instructions` | Block with exit `2` or `decision: "block"` |
| `PostCompact` | After context compaction | `trigger` | `trigger`, `compact_summary` | Side effects only |
| `PreModelSwitch` | Before a requested model change | `to_model` | `from_model`, `to_model`, `requested_model`, `source`, `context_tokens`, `prompt_cache_warm`, `cache_ttl`, `estimated_cache_write_usd`, `pricing` | `permissionDecision`, `permissionDecisionReason`, or block |
| `PostModelSwitch` | After the active model changes | `to_model` | Same keys as `PreModelSwitch`; `source` may also be `auto` or `resume` | `additionalContext` |
| `Elicitation` | An MCP server asks the user for input | `mcp_server_name` | `mcp_server_name`, `message`, `mode?`, `url?`, `elicitation_id?`, `requested_schema?` | `action`, `content` |
| `ElicitationResult` | After the user answers an MCP request | `mcp_server_name` | `mcp_server_name`, `action`, `mode?`, `elicitation_id?`, `content?` | Override with `action`, `content` |
| `SessionEnd` | The session terminates | `reason` | `reason` | Side effects only |

`?` means optional.

## Nested payload keys

### `PostToolBatch.tool_calls[]`

Each item contains:

| Key | Meaning |
|---|---|
| `tool_name` | Name of the tool. |
| `tool_input` | Arguments sent to the tool. |
| `tool_use_id` | ID for that tool call. |
| `tool_response` | Serialized result the model receives. This differs from the structured `PostToolUse.tool_response`. |

### `Stop` and `SubagentStop` background work

`background_tasks[]` items may contain:

`id`, `type`, `status`, `description`, `command?`, `agent_type?`, `server?`, `tool?`, `name?`

`session_crons[]` items contain:

`id`, `schedule`, `recurring`, `prompt`

### `PermissionRequest.permission_suggestions[]`

Permission suggestions may contain:

`type`, `rules[]`, `behavior`, `destination`

### `effort`

```json
{
  "effort": {
    "level": "xhigh"
  }
}
```

## Tool call payloads

For tool events, `tool_name` identifies the action and `tool_input` contains its arguments.

### `tool_input` keys

| Tool | `tool_input` keys |
|---|---|
| `Bash` | `command`, `description?`, `timeout?`, `run_in_background?`, `dangerouslyDisableSandbox?` |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all?` |
| `Write` | `file_path`, `content` |
| `Read` | `file_path`, `limit?`, `offset?`, `pages?` |
| `Glob` | `pattern`, `path?` |
| `Grep` | `pattern`, `path?`, `output_mode?`, `-n?`, `-A?`, `head_limit?`; other flags may also appear as keys |
| `WebFetch` | `url`, `prompt`, `limit?` |
| `WebSearch` | `query`, `allowed_domains?` |
| `Agent` | `subagent_type`, `prompt`, `description?`, `run_in_background?`, `model?` |
| `NotebookEdit` | `notebook_path`, `cell_id?`, `edit_mode`, `new_source` |
| `TaskCreate` | `subject`, `description`, `activeForm` |
| `TaskUpdate` | `taskId`, `status` |

Tool schemas can change independently of hook schemas. Ignore unknown keys and check optional keys
before using them.

The runtime subagent tool is named `Agent`. A hook matcher named `Task` has also caught this tool,
but implementations should use the runtime `tool_name` from the payload.

### `tool_response` keys

| Tool | `tool_response` keys |
|---|---|
| `Bash` | `stdout`, `stderr`, `interrupted`, `isImage`, `noOutputExpected`, `backgroundTaskId?`, `returnCodeInterpretation?`, `persistedOutputPath?`, `persistedOutputSize?` |
| `Edit` | `filePath`, `oldString`, `newString`, `originalFile`, `structuredPatch`, `userModified`, `replaceAll`, `memdirStamped?` |
| `Write` | `type`, `filePath`, `content`, `originalFile`, `structuredPatch`, `userModified`, `memdirStamped?`, `result?` |
| `Read` | `type`, `file` |
| `Glob` | `filenames`, `numFiles`, `truncated`, `durationMs` |
| `Grep` | `mode`, `content`, `filenames`, `numFiles`, `numLines`, `numMatches` |
| `WebFetch` | `bytes`, `code`, `codeText`, `result`, `durationMs`, `url` |
| `WebSearch` | `query`, `results`, `searchCount`, `durationSeconds` |
| `NotebookEdit` | `new_source`, `cell_type`, `language`, `edit_mode`, `cell_id`, `error`, `notebook_path`, `original_file`, `updated_file` |
| `TaskCreate` | `task` containing `{ id, subject }` |
| `TaskUpdate` | `success`, `taskId`, `updatedFields[]`, `statusChange` containing `{ from, to }` |

## Failure payload

When an executed tool fails and `PostToolUseFailure` fires, the error is a top-level string:

```json
{
  "hook_event_name": "PostToolUseFailure",
  "tool_name": "Bash",
  "tool_input": {
    "command": "npm test"
  },
  "tool_use_id": "toolu_01ABC123",
  "error": "Exit code 1\nTest failed",
  "is_interrupt": false,
  "duration_ms": 4187
}
```

Important: `PostToolUseFailure` does not catch every rejected action. Invalid tool names, invalid
input, some tool validation failures, and permission denials may fail before this event can fire.

## Hook response format

A hook may return a JSON object on stdout. Event-specific fields normally go inside
`hookSpecificOutput`, and `hookEventName` must exactly match the event.

### Universal response keys

| Key | Meaning |
|---|---|
| `continue` | Defaults to `true`. `false` stops Claude entirely where supported. |
| `stopReason` | Message shown to the user when `continue` is `false`. |
| `suppressOutput` | Accepted but currently has no effect. |
| `systemMessage` | Warning shown to the user where supported. |
| `terminalSequence` | Approved terminal notification or bell escape sequence. |
| `decision` | Usually `"block"` for events using the top-level decision model. |
| `reason` | Explanation associated with a blocking decision. |
| `hookSpecificOutput` | Event-specific response object. Must contain `hookEventName`. |

### Exit codes

| Exit code | Meaning |
|---|---|
| `0` | Hook succeeded. With no stdout, it makes no decision. Valid JSON on stdout is processed. |
| `2` | Blocking error on events that support blocking. The exact effect depends on the event. |
| Any other non-zero | Usually a non-blocking hook error. |

Do not assume exit `0` silently approves a tool. It means the hook has no objection and normal
permission handling continues.

## Copy-paste response examples

### Allow normal handling

Return no stdout and exit `0`.

### Deny a tool call

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "This action is not allowed."
  }
}
```

### Rewrite a tool call before it runs

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {
      "subagent_type": "repository-explorer",
      "prompt": "Inspect the repository and report findings.",
      "description": "Inspect repository"
    }
  }
}
```

`updatedInput` replaces the entire original `tool_input`. Include every field the tool still
needs; it is not a partial merge.

### Add context to a user prompt

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Follow the project security policy for this request."
  }
}
```

### Replace the result shown to Claude

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "updatedToolOutput": {
      "stdout": "[redacted]",
      "stderr": "",
      "interrupted": false,
      "isImage": false
    }
  }
}
```

The tool has already run. This only changes what Claude sees. The replacement must match the
tool's expected response shape.

### Keep Claude working at `Stop`

```json
{
  "decision": "block",
  "reason": "Run the test suite before finishing."
}
```

Always check `stop_hook_active` before blocking again. Repeatedly blocking a stop can create a
loop; Claude Code currently ends the turn after eight consecutive stop-hook continuations.

### Answer an MCP elicitation

```json
{
  "hookSpecificOutput": {
    "hookEventName": "Elicitation",
    "action": "accept",
    "content": {
      "username": "alice"
    }
  }
}
```

## Important implementation rules

1. Treat unknown keys as forward-compatible additions.
2. Treat optional keys as genuinely optional.
3. Match `PreToolUse` and related tool events on the runtime `tool_name`.
4. Return the complete object for `updatedInput`; it replaces rather than merges.
5. Use `PreToolUse` to prevent or change an action. `PostToolUse` is too late to undo it.
6. Use `agent_transcript_path` for a finished subagent's transcript. On `SubagentStop`,
   `transcript_path` points to the parent session.
7. Read subagent transcripts during `SubagentStop`; they may be removed when the session ends.
8. Do not rely on `PostToolUseFailure` for every error path.
9. Keep hook stdout clean. If returning JSON, stdout should contain only the JSON object.
