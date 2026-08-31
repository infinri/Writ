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

Everything here is true for a specific build. The map was first captured on 2.1.183
(2026-06-19) and re-verified against live capture on 2.1.220 (2026-08-01). Claude Code changes
between versions: field names appear, events get added. Treat this document as a snapshot, not a
permanent contract. Facts re-confirmed on 2.1.220 say so; facts only seen on the older build keep
an explicit `[observed 2.1.183]` tag. Part 3 explains how to re-capture for a new version.

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

## Part 2: Technical reference (field walk on build 2.1.220; baseline 2.1.183)

### Header

| Field | Value |
|---|---|
| Claude Code version | The field-by-field walk below was done on `2.1.220 (Claude Code)` [observed]; baseline capture was `2.1.183`. The most recent capture and its census are stamped `2.1.251 (Claude Code)` and were NOT walked field by field (see the provenance gap below the table) |
| Model in session | `claude-fable-5` (from SessionStart.model) [observed]; `claude-opus-4-8[1m]` on 2.1.183 |
| Capture date | 2026-08-11 (sub-agent transcript pass: 42 captured SubagentStop payloads, 130 transcript files); 2026-08-01 (refresh); 2026-06-19 (baseline) [observed] |
| Host OS | Linux x86_64 [observed] |
| Live capture source | `~/.claude/writ-blackbox.jsonl` (raw envelopes; refresh filtered to real sessions only, because the same file also collects synthetic test-fixture envelopes that would poison the schema) [observed] |
| Doc reference | https://code.claude.com/docs/en/hooks [doc] |
| Doc reference (alias) | https://docs.anthropic.com/en/docs/claude-code/hooks redirects to the page above [doc] |

**The provenance gap, stated rather than papered over.** The newest capture window and the
census built from it are stamped `2.1.251 (Claude Code)` and cover 2026-08-28 to 2026-08-29
(`docs/reference/blackbox-census.json`, 9,388 records). The field-by-field walk behind every
per-event table in this Part was done on 2.1.220. Nobody has re-walked those tables against
2.1.251, so relabelling this Part would convert an unverified claim into an asserted one. The
tables are therefore UNVERIFIED on the current build, which is not the same as known-stale: no
field below has been shown to have changed, and none has been shown to have survived either.
Passages later in this document that cite the 9,388-record corpus are citing an earlier
2.1.251 artifact; the tables are not.

UPDATED 2026-08-31. A capture session HAS since run, from 2026-08-28 to 2026-08-31, and the
committed census was regenerated from it: 13,477 records, 70 record classes, stamped 2.1.251.
The two staleness defects this paragraph used to record are therefore CLOSED, and the sentence
that recorded them is removed rather than left to mislead. What did NOT change is the reason
the tables stay unverified: a census counts which keys appeared, while the per-event tables
below describe what each field MEANS and when it is present, and nobody has re-walked them
field by field against 2.1.251. Capture is OFF again as of that regeneration, measured at about
275 ms per write across the 15 write-path hooks while it was on.

Schema claims below are scoped to 2.1.220 where re-observed on 2026-08-01; claims seen only on
the older build carry `[observed 2.1.183]`. The public changelog for 2.1.184-2.1.220 announces
almost none of the payload changes recorded here (its only hook entries: a new `DirectoryAdded`
event in v2.1.219, a hook-timeout misreport fix in v2.1.210, a frontmatter-hook trust requirement
in v2.1.216, and a plugin-hook shell-injection fix in v2.1.207), so live capture remains the only
reliable source. Where the live capture and the docs disagree, the live capture wins.

Headline changes measured between 2.1.183 and 2.1.220:
- **`prompt_id` (UUID) is now on every observed event**, including SessionStart, SessionEnd, and
  the compaction pair. It identifies the user turn the event belongs to. [observed]
- **Five events moved from DOC-ONLY to OBSERVED**: Stop, SessionEnd, PreCompact, PostCompact
  (all captured live), and PostCompact now delivers the full `compact_summary` text. [observed]
- **SubagentStart carries no task text at all on this build** (17/17 real spawns had only
  `agent_id`/`agent_type` plus the universal fields). The 2.1.183 note that `prompt` appears
  "sometimes" did not reproduce. [observed]
- New tool_response fields: Bash `persistedOutputPath`/`persistedOutputSize`; Edit and Write
  `memdirStamped`; Write `result`. [observed]

### The envelope at a glance

Each captured line in the log is `{ts, hook, direction, payload, pid, session, event}`, plus
`exit_code` on an `exit` row [observed]. Four things trip people up:
- `hook` is the hook SCRIPT name (for example `validate-file`), NOT the event name. The event name
  is usually inside, at `payload.hook_event_name`; `event` at the record level is the fallback for
  a row whose payload cannot carry one.
- `payload` is a JSON string, so it must be decoded twice. `direction` is `in` (the envelope
  Claude Code delivered), `out` (what the script sent back) or `exit` (the script's own non-zero
  exit status, written by the shared exit trap in `bin/lib/common.sh`; a zero exit writes no row).
  The authoritative session key is `payload.session_id`.
- `event` and `exit_code` [added 2026-08-31] POSTDATE the first 9,388-record corpus, so a row
  from it carries NEITHER key. The distinction is load-bearing rather than cosmetic: a key that
  is ABSENT means
  the row predates the field, and a key that is PRESENT and null means the writing process
  observed no event. Test membership (`"event" in record`), never truthiness, or the two collapse.
  `writ/analysis/blackbox.py` censuses them as `unknown` and `event_not_observed` respectively.
- `pid` is the HOOK SHELL's pid, and was not always. Until 2026-08-31 it was the pid of the
  ephemeral python encoder each `blackbox_log` call forks, so on a row from the old corpus an IN
  row and an OUT row from one hook invocation carry DIFFERENT pids and cannot be joined.

**What an `out` row covers, and what it does not.** Every JSON envelope a hook replies with goes
through one funnel: `emit_hook_reply` (`bin/lib/common.sh:733-745`) prints the payload and then
logs THAT SAME variable, and `blackbox_log out` may not appear anywhere under `hooks/scripts/`
(`tests/_inventory.py::direct_blackbox_out_calls` must be empty). So an `out` row holds the bytes
that were sent rather than a reconstruction of them. That is NOT the same claim as "every hook's
output is captured now", and the difference is large. Per
`docs/adr/ADR-blackbox-record-schema.md`, PLAIN STDOUT is roughly 44 inline emission sites that
print text directly: about 22 in `writ-rag-inject.sh` plus 3 more through its two shared directive
emitters (`emit_mode_directive`, `emit_post_compact_directive`), about 21 in
`auto-approve-gate.sh`, and 1 in the manual-test-grant hook. All of them are on
UserPromptSubmit, all are deferred by that ADR with the cost stated, and none produces an `out`
row today. The consequence for a reader of the capture log: the absence of an `out` row for a
UserPromptSubmit hook says nothing about whether that hook emitted.

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
| Stop | main agent about to stop | none | Yes: prevents stop, continues the turn [doc] | additionalContext [doc] | OBSERVED (2.1.220) |
| CwdChanged | working directory changes | none | No [doc] | none | DOC-ONLY (did not fire for an in-command `cd`; see Gaps) |
| PreCompact | before history compaction | `trigger` (manual, auto) | Yes: blocks compaction [doc] | none | OBSERVED (2.1.220, manual `/compact`) |
| PostCompact | after history compaction | `trigger` (manual, auto) | No [doc] | none | OBSERVED (2.1.220; carries `compact_summary`) |
| SessionEnd | session ends | `reason` | No [doc] | none | OBSERVED (2.1.220) |
| DirectoryAdded | after `/add-dir` or SDK `register_repo_root` | UNVERIFIED | UNVERIFIED | UNVERIFIED | DOC-ONLY (added in v2.1.219) |
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
subscribes to.** During the 2.1.183 capture the subscribed tools were `Agent`, `Bash`, `Edit`,
`Write`, and unsubscribed tools (`Read`, `NotebookEdit`, the `TaskCreate`/`TaskUpdate` family)
fired no PreToolUse/PostToolUse at all [observed]. On 2.1.220, with `Read` added to the matcher
list, PreToolUse fires for `Read` too [observed]. So which actions you can gate is set by your
matcher list, not by Claude Code.

### Common input fields

These fields appear across events. "Universal" means on every captured event; the rest are
conditional.

| Field | Where it appears | Source | Notes |
|---|---|---|---|
| `session_id` | universal | [both] | session identifier (the authoritative key) |
| `transcript_path` | universal | [both] | absolute path to the conversation log. **On SubagentStop it names the PARENT session's transcript, not the finished sub-agent's** (42/42 captured payloads, [observed 2026-08-11]); the sub-agent's own file is `agent_transcript_path` |
| `cwd` | universal | [both] | working directory at hook time |
| `hook_event_name` | universal | [both] | the event that fired |
| `prompt_id` | universal since 2.1.2xx (every event observed on 2.1.220, including SessionStart/SessionEnd/Pre/PostCompact) | [observed] | UUID of the user turn the event belongs to; did not exist on 2.1.183 |
| `permission_mode` | UserPromptSubmit, Pre/PostToolUse, PostToolUseFailure, Stop, SubagentStop | [observed] | NOT on SessionStart or SubagentStart [observed] |
| `effort` `{level}` | Pre/PostToolUse, PostToolUseFailure, Stop, SubagentStop | [observed] | the model's reasoning-effort level; absent on UserPromptSubmit, SessionStart, SubagentStart |
| `tool_name`, `tool_input` | Pre/PostToolUse, PostToolUseFailure | [both] | the action and its arguments |
| `tool_use_id` | Pre/PostToolUse, PostToolUseFailure | [observed] | ties a PreToolUse to its PostToolUse |
| `tool_response` | PostToolUse | [both] | the action's result (per-tool shape below) |
| `duration_ms` | PostToolUse, PostToolUseFailure | [observed] | how long the tool took |
| `error` | PostToolUseFailure | [observed] | the failure message, a STRING (see below) |
| `is_interrupt` | PostToolUseFailure | [observed] | whether the failure was an interrupt |
| `prompt` | UserPromptSubmit only on 2.1.220 | [observed] | the user's text. On 2.1.183 it appeared on SubagentStart "sometimes"; 17/17 real spawns on 2.1.220 had NO task text (design consequence: a SubagentStart hook cannot read the task and must query context another way) |
| `source` | SessionStart | [observed] | observed values on 2.1.220: `startup`, `resume`, `compact` (only `startup` was seen on 2.1.183) |
| `model` | SessionStart | [observed] | observed: `claude-fable-5` (2.1.220), `claude-opus-4-8[1m]` (2.1.183) |
| `trigger` | PreCompact, PostCompact | [observed] | observed: `manual`; docs also list `auto` [doc] |
| `custom_instructions` | PreCompact | [observed] | observed `null` on a bare manual `/compact` |
| `compact_summary` | PostCompact | [observed] | the FULL generated summary text of the compacted conversation |
| `reason` | SessionEnd | [observed] | observed: `prompt_input_exit` |
| `agent_id`, `agent_type` | SubagentStart, SubagentStop, and tool events inside a subagent | [both] | values seen: `general-purpose`, `Explore`, `writ-explorer`, `workflow-subagent` |
| `stop_hook_active` | SubagentStop, Stop | [observed] | true while Claude is re-invoking a stop hook after a block |
| `last_assistant_message`, `background_tasks`, `session_crons` | Stop, SubagentStop | [observed] | the last message text and (observed empty) task/cron lists |
| `agent_transcript_path` | SubagentStop | [observed 2026-08-11] | absolute path to the SUB-AGENT's own transcript, resolved by Claude Code; present in 42/42 captured payloads. Not durable (see "Sub-agent transcripts" below) |

Observed `permission_mode` values: `default`, `auto`, `acceptEdits` (2.1.220); `plan` (2.1.183).
Documented set also includes `dontAsk`, `bypassPermissions` [doc].
Observed `effort.level` values: `xhigh` (2.1.220); `high` (2.1.183). Documented set: `low`,
`medium`, `high`, `xhigh`, `max` [doc].

### Per-event input (exact fields seen on 2.1.220, real sessions only)

Every event below also carries the universal four (`session_id, transcript_path, cwd,
hook_event_name`) plus `prompt_id`; only the additional fields are listed.

UserPromptSubmit [observed]: `permission_mode, prompt`.

PreToolUse [observed]: `permission_mode, effort{level}, tool_name, tool_input, tool_use_id`
(plus `agent_id, agent_type` when the call runs inside a subagent).

PostToolUse [observed]: all PreToolUse fields, plus `tool_response, duration_ms`.

PostToolUseFailure [observed]: `permission_mode, effort{level}, tool_name, tool_input,
tool_use_id, error, is_interrupt, duration_ms`.

SessionStart [observed]: `source, model`.

SubagentStart [observed]: `agent_id, agent_type`. No task text, no `permission_mode`, no
`effort`, no tool fields (17/17 real spawns).

SubagentStop [observed]: `permission_mode, effort{level}, agent_id, agent_type, stop_hook_active,
agent_transcript_path, last_assistant_message, background_tasks, session_crons`. That plus the
universals is the COMPLETE key set, with no variation across 42 captured payloads
[observed 2026-08-11]: `agent_id, agent_transcript_path, agent_type, background_tasks, cwd, effort,
hook_event_name, last_assistant_message, permission_mode, prompt_id, session_crons, session_id,
stop_hook_active, transcript_path`. Note the two transcript keys point at different files (next
section).

Stop [observed, new since 2.1.183]: `permission_mode, effort{level}, stop_hook_active,
last_assistant_message, background_tasks, session_crons`. Same envelope as SubagentStop minus the
agent fields.

PreCompact [observed, new since 2.1.183]: `trigger, custom_instructions`.

PostCompact [observed, new since 2.1.183]: `trigger, compact_summary`.

SessionEnd [observed, new since 2.1.183]: `reason`.

For the events still only in the docs (Setup, CwdChanged, DirectoryAdded, the permission and
worktree and elicitation families), the documented input fields are listed in the doc-only table
at the end of this section.

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
| MultiEdit | NOT PRESENT in 2.1.183 (no such tool; the only edit primitive is `Edit`); no MultiEdit envelope in the 2.1.220 capture either | [observed] |
| TodoWrite | NOT PRESENT in 2.1.183 (task tracking is the `TaskCreate` / `TaskUpdate` family); no real TodoWrite envelope in the 2.1.220 capture either | [observed] |

### tool_response per tool (the result of an action)

Inner field names are mostly camelCase. The docs describe `tool_response` only as a generic object
or string [doc]; these shapes are observed. The transcript's stored result equals the hook's
`tool_response` where both were captured (Edit, Write, Bash, WebFetch).

| Tool | tool_response fields | Source |
|---|---|---|
| Bash | `stdout`, `stderr`, `interrupted`, `isImage`, `noOutputExpected`; on 2.1.220 also `backgroundTaskId`, `returnCodeInterpretation`, and (new) `persistedOutputPath`, `persistedOutputSize` | [both]. The 2.1.183 transcript additionally showed `assistantAutoBackgrounded`, `gitOperation`, `staleReadFileStateHint` |
| Edit | `filePath`, `oldString`, `newString`, `originalFile`, `structuredPatch`, `userModified`, `replaceAll`; on 2.1.220 also `memdirStamped` (new) | [both] |
| Write | `type`, `filePath`, `content`, `originalFile`, `structuredPatch`, `userModified`; on 2.1.220 also `memdirStamped`, `result` (new) | [both] |
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

Gating behavior in this tree: the PreToolUse gate replies with exit 0 plus JSON, and exit 2 is
used elsewhere. An earlier version of this sentence said "our gate never uses exit 2", which was
wrong about our own code. Three real blocking sites disprove it:
`hooks/scripts/enforce-violations.sh:75` (registered on Stop; blocks the stop while violations
are pending) and `hooks/scripts/validate-rules.sh:57` and `:337` (both registered on PostToolUse;
each fires only when the gate-invalidation sentinel is present). All three write their reason to
stderr before exiting, because exit 2 discards stdout, so a blocking exit with no echo is a
refusal that names no action.

The PreToolUse shapes below are unchanged and still hold:
- **Allow is silence:** an allowed action produces exit 0 and NO output.
- **Deny:** exit 0 with `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
  "deny", "permissionDecisionReason": "...", "additionalContext": "..."}}`.
- **Force-swap allow:** exit 0 with `permissionDecision: "allow"` plus `updatedInput`.

The two exit-code paths now have names in the shared delivery vocabulary: `exit2_stderr` and
`exit_nonzero_stderr` in `writ/shared/delivery.py:69-77`. Both classify to the `USER` bucket
rather than `MODEL`, on the reading that the harness surfaces a non-zero exit's stderr and no
captured record shows that text arriving in the model's context. That is the documented
contract's best reading, NOT a capture result, which is why the same module's
`delivery_provenance` returns `unproven` for them: the committed census holds zero
`exit`-direction rows (Part 3).

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

`additionalContext` is EVENT-SCOPED, and an unaccepted event costs you the whole reply
[observed 2026-08-14]. A real `/compact` ran `writ-postcompact.sh`, which replied
`{"hookSpecificOutput": {"hookEventName": "PostCompact", "additionalContext": "..."}}`. Claude
Code answered "(root): Invalid input" and discarded the entire object: `PostCompact` is not an
accepted `hookEventName` variant for that shape. The validator error listed the variants it does
accept: PreToolUse, PostToolUse, PostToolBatch, UserPromptSubmit, Stop, SubagentStop and
SubagentStart. Two consequences worth stating plainly. First, the failure is silent on the case
that matters: a human typing `/compact` sees the validator error echoed, but on an automatic
compaction nothing is shown and nothing is delivered, which is how this shipped and stayed green
for two cycles. Second, the rejection is total, not partial, so a valid `systemMessage` sitting
beside an invalid `additionalContext` in the same object dies with it. `PostCompact` and
`PreCompact` have no model-visible channel at all; queue the text in state and emit it from the
next `UserPromptSubmit` instead. `writ/shared/delivery.py::ADDITIONAL_CONTEXT_EVENTS` encodes the
accepted set and `writ/hooks_lint.py` flags a wired script that violates it.

SubagentStart `additionalContext` was re-probed the same day and DOES deliver on this build
[observed 2026-08-14], independently of the capture noted above. It appears in the validator's
accepted-variant list and a live probe confirmed the sub-agent received the text, so the
compaction finding above does not implicate the sub-agent injection path.

Stop-hook caveat, load-bearing for anyone injecting context at turn end: on the builds where it
was measured, `additionalContext` returned from a Stop hook is treated as a turn BLOCK (Claude
continues the turn instead of stopping), so a Stop hook that always adds context loops until the
retry cap unless it checks `stop_hook_active` [observed 2.1.1xx]. Not re-verified on 2.1.220 (no
Stop block occurred in the capture window); Writ's own Stop hooks still guard on
`stop_hook_active`, and `writ/shared/delivery.py` encodes which events deliver bare stdout to the
model. If a new build changes either behavior, that is a code change (plus `test_delivery.py`),
not just a doc edit.

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

Force-swap recipe (PreToolUse). The JSON shape is the one verified live on this build; the shell
around it is the pattern the live implementation uses
(`hooks/scripts/writ-dispatch-discipline.sh:54-143`):

```bash
#!/usr/bin/env bash
# PreToolUse (matcher: Task). Exit is always 0; the decision rides in the JSON reply.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
source "$WRIT_DIR/bin/lib/common.sh"
load_hook_env                 # reads stdin once; sets HOOK_ENVELOPE and HOOK_SESSION_ID
SESSION_ID="$HOOK_SESSION_ID"

# Build the envelope INTO A VARIABLE, never straight to stdout. The quoted '<<PY'
# delimiter means the shell substitutes nothing inside the body, and the payload rides in
# on an env var instead of being spliced into the program text.
DECISION=$(WRIT_PARSED_ENVELOPE="$HOOK_ENVELOPE" python3 <<'PY'
import json, os
parsed = json.loads(os.environ.get("WRIT_PARSED_ENVELOPE") or "{}")
ti = parsed.get("tool_input") or {}
if ti.get("subagent_type") == "general-purpose":
    ti["subagent_type"] = "writ-explorer"          # your swap
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": ti}}))                     # whole object back, a replacement
PY
)

# ONE writer for stdout. An empty payload is a no-op, which is how "allow is silence"
# stays the default path.
emit_hook_reply "$DECISION" "" "$SESSION_ID"
```

The teaching point is still `updatedInput`: pass the WHOLE `tool_input` object back, because it
replaces the original rather than merging into it. What changed is the plumbing. Since `1eb48b1`
no script under `hooks/scripts/` may print an envelope directly to stdout; an earlier version of
this snippet piped a `python3 -c` block's stdout straight through, sourced nothing, and called no
funnel, which is exactly the shape `tests/_inventory.py::bare_envelope_emissions` flags. Capturing
into a variable and emitting once is what makes the `out` row byte-identical to the reply, and it
also keeps capture off the critical path: `emit_hook_reply` prints first and logs second.

### The subagent-spawn naming question (recorded, not resolved)

A common confusion: what is the tool that spawns a sub-agent called? Three different names are in
play, so all are recorded.

| Source | Name it uses | Detail |
|---|---|---|
| Live PreToolUse envelope | `tool_name: "Agent"` | The runtime tool. Carries `tool_input.subagent_type`. [observed] |
| The hook config matcher | `Task` | Registered as matcher `"Task"`, yet it fired on the `Agent` tool. So the `Task` matcher catches the `Agent` tool on this build. [observed] |
| Doc reference | `TaskCreate` | The docs name a `TaskCreate` tool with TaskCreated/TaskCompleted events, never "Task" or "Agent" for spawning. [doc] |
| Tool registry | `TaskCreate`, `TaskGet`, `TaskList`, `TaskStop`, `TaskUpdate` exist as tools, alongside an `Agent` tool. [observed] |

Practical guidance: to force-swap a dispatch, match `Task` (it catches the runtime `Agent` tool)
and read/write `tool_input.subagent_type`. This is the path verified live on 2.1.183, observed
working on 2.1.220 (the `Task` matcher produced envelopes with `tool_name: "Agent"` in the
2026-08-01 capture), and re-confirmed by dispatch on 2026-08-27: the swap fired and rewrote a
generic `Explore` request to `writ-explorer` before the sub-agent started.

### The sub-agent lifecycle, probe-verified 2026-08-27

Recorded because it was got WRONG from logs first, and the error is the useful part. A report
claiming `SubagentStart` never fires, the `Task` matcher never matches, and cache inheritance
never happens was produced by grepping `var/logs` with plain `grep -r`, which **silently skips
gzipped files**. 226 archives held 1,421 `subagent_start` rows. Three probe dispatches settled
in seconds what an hour of log-reading had inverted. **Verify a harness behaviour by triggering
it.**

What three dispatches showed, on ordinary `Agent` tool calls [observed 2026-08-27]:

| Stage | Result |
|---|---|
| `PreToolUse` matcher `Task` | fires; `tool_input.subagent_type` present and rewritable |
| `SubagentStart` | fires; envelope is `{session_id, transcript_path, cwd, prompt_id, agent_id, agent_type, hook_event_name}` |
| `agent_type` on both start and stop | POPULATED (`writ-explorer`, `general-purpose`), including for a generic dispatch using the `[general-purpose]` escape |
| tool events inside the sub-agent | fire, with `agent_id` present, so their rows file under the agent's own id |
| `SubagentStop` | fires; `agent_type` populated |

**Two populations, and only one of them is governed.** Across the full log corpus, 1,381
distinct agents have a `subagent_start` row and 2,219 do not, and the sets do **not** intersect.
Every agent in the second group arrives at `SubagentStop` with `agent_type: ""`. So an empty
`agent_type` is the signature of a spawn path that skipped `SubagentStart` entirely, not a
property of the build. Of those 2,219, 1,425 still produced rows under their own agent id (Writ
hooks ran inside them) and 794 produced only stop-side rows. These four counts come from the
`var/logs` archives, including the 226 gzipped ones (`bin/lib/common.sh:411-419` carries the same
measurement), NOT from the blackbox JSONL corpus discussed elsewhere in this document, so they
are not expected to reconcile with its 9,388 records.

**Sidecar lifetime differs between the two.** For a governed dispatch the sidecar
(`agent-<agent_id>.meta.json`) survives the agent's completion; for the ungoverned population
none of 57 completed agents still had one, while 68 from the same session persisted. Any design
that reads the sidecar must therefore treat it as ephemeral and persist what it learns.

**A `subagent_start` row is NECESSARY but NOT SUFFICIENT, and was fixed in `170cfa6`.** The
paragraphs above call the population with a start row "governed". Read that as "has a start row".
The row alone was never proof, and for two cycles it was actively misleading.
`hooks/scripts/writ-subagent-start.sh` passed the parent's ENTIRE session cache JSON as an unread
`argv[1]` to its python blocks; the seeder never read it, because the inheritance logic had moved
into `writ/session/subagent_seed.py`, which re-reads the parent cache from the session id itself.
Linux caps a single argv string at MAX_ARG_STRLEN (131072 here; the live parent measured 181529
bytes), so past that threshold `execve` failed E2BIG before python started and `2>/dev/null || true`
swallowed the error. The start row is written at the END of the hook whether or not the seed block
ran, so a dispatch could carry a `subagent_start` row and still run with NO mode, NO inherited
gates and NO plan directory. Outside the skill directory every write from such a sub-agent hit the
`[ENF-GATE-MODE]` no-mode deny, and the role write-scope authority that requires
`cache_source == "subagent_start"` had never once fired on a real dispatch.

It read as a KNOWN LIMITATION rather than a defect because it was threshold-triggered by session
age: seeding works while the parent cache is small and stops once the cache crosses the cap, and a
long session is exactly when sub-agents get used most. So the missing telemetry looked like the
already-documented ungoverned population above, which had a ready innocent explanation.

Fixed in `170cfa6`. The unread argument is deleted rather than worked around (the bounded session
id goes in on named env variables and the cache is read in process), the seed block now reports
`seeded` or `skipped` and fires exactly one bounded `subagent_seed_failed` record on an empty
result, and `writ/session/doctor.py:1485-1586` splits the census into `governed`, `lazy`,
`seed_failed`, `reachable` and `unreachable`. `governed` subtracts an agent only on that agent's
OWN `subagent_seed_failed` row and never on a missing one, so archives written before that record
existed are not retroactively relabelled and a zero in `seed_failed` means unrecorded rather than
proven clean.

### Sub-agent transcripts, and queued input misdelivered into a sub-agent turn

All of this was measured on 2026-08-11 against 42 captured SubagentStop payloads and 130 local
transcript files [observed 2026-08-11]. It is recorded here because it is a harness behaviour no
consumer can fix, only detect.

**Two transcript keys, two different files.** A SubagentStop payload carries both, and they never
name the same file: `transcript_path` is the PARENT session's transcript (42/42) and
`agent_transcript_path` is the finished sub-agent's own transcript, already resolved by Claude Code
(42/42). Nothing in the payload marks which is which, so code that reaches for the "obvious"
universal key reads the parent's conversation while believing it is reading the worker's.

**Layout.** A sub-agent transcript lives under its parent session's directory:
`~/.claude/projects/<project-slug>/<parent-session-id>/subagents/agent-<agent-id>.jsonl`, beside an
`agent-<agent-id>.meta.json` sidecar. Fan-out sub-agents spawned by a workflow nest one level
deeper: `<parent-session-dir>/subagents/workflows/wf_<workflow-id>/agent-<agent-id>.jsonl`, which
was 10 of the 42 captured payloads. A directory walk that assumes the flat form misses those ten.

**Sub-agent transcripts are not durable.** Claude Code removes them when the session ends. Only the
parent transcript survives, and it does not contain the sub-agent's messages. Corroborated off the
log: in this project's transcript store, 1 of 5 session directories has a `subagents/` directory at
all, and it is the live session. The consequence for hook authors is hard: **SubagentStop is the
only reliable window in which a sub-agent's transcript can be read.** Anything a later event
(Stop, SessionEnd, a nightly job) intends to read there will usually find nothing.

**Queued user input can be misdelivered into a sub-agent's pending turn.** A keystroke the user
queues while a sub-agent is running can be spliced into that SUB-AGENT's turn instead of being held
for the parent, so the worker receives an instruction addressed to the orchestrator and the
orchestrator never sees it. There is no field, flag, or event announcing this.

The signature is structural, not textual: a `user`-role entry in the sub-agent's transcript that
carries a free-text block it has no business carrying. After the opening task prompt, every
legitimate `user`-role entry in a sub-agent transcript is the harness returning a tool result, so
loose prose there is anomalous by construction. The one finding re-verified live on 2026-08-11 is
that shape exactly: line 139 of a sub-agent transcript in this project dated 2026-08-10, with a
132-character text block sitting in the SAME user envelope as a returned tool result
(`text_count: 1, tool_result_count: 1`). Across the corpus the predicate matched **2 of ~10,446
user messages in 130 transcript files (0.02%)**, and neither match was a benign harness sentinel.
That rate, low and clean, is why detection is worth wiring into a hook rather than leaving to a
command someone remembers to run.

Detecting it must not act on it. A Stop-family hook that answers with `additionalContext` BLOCKS
the turn (see the Output contract and gap 2 below), so a tripwire that "reported" a finding that way
would halt real turns on a structural heuristic. Writ's implementation
(`hooks/scripts/writ-subagent-stop.sh` plus `writ/session/transcript_tripwire.py`) logs the finding
against the parent session and returns nothing.

### Gaps (what is still not nailed down on this build)

Closed since the first pass: per-tool input/result schemas for all tools that exist; the
PostToolUseFailure envelope (field is `error`, a string); the SessionStart, SubagentStart, and
SubagentStop envelopes. Closed in the 2026-08-01 refresh: the Stop, SessionEnd, PreCompact, and
PostCompact envelopes (all captured live on 2.1.220).

**Nothing in the numbered list below was closed by the 2026-08-28 to 2026-08-31 cycles**
(`30b1ac0`, `1eb48b1`, `170cfa6`), and saying so is the point: a reader should neither
re-investigate one of these believing recent work moved it, nor assume one quietly closed. Those
three cycles changed OUR record schema, OUR emission funnel and OUR sub-agent seeding. Every gap
1 through 6 is a question about Claude Code's own behavior, and answering any of them needs a
live capture session with the triggering condition exercised. Capture is currently OFF, so none
of them moved. Gaps 7 through 10 are new, opened by those same cycles.

Still open:
1. **CwdChanged did not fire** for a directory change made inside a Bash command (`cd` within one
   command). The change took effect (later envelopes carried the new directory), but no CwdChanged
   envelope appeared. UNVERIFIED whether it fires for a genuine Claude Code working-directory
   change; a per-command shell reset may suppress the in-command case.
2. **The Stop `additionalContext` turn-block semantics** (see the Output contract) were not
   re-exercised on 2.1.220; the guard-on-`stop_hook_active` discipline stands until re-measured.
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
7. **The exit-code delivery answer is UNPROVEN, not observed.** `exit2_stderr` and
   `exit_nonzero_stderr` (`writ/shared/delivery.py:69-77`) classify to the `USER` bucket on the
   documented contract's best reading. No captured record backs it: the committed census holds
   zero `exit`-direction rows, and its only observed mechanisms are `additionalContext` and
   `permissionDecisionReason`. `delivery_provenance` therefore answers `unproven` for every
   exit-code entry, and the fire drill reports that rather than asserting the classification.
   Closing this needs a capture window containing a real refusal.
8. **Plain stdout is uncaptured, at roughly 44 sites.** All of them are on UserPromptSubmit and
   all are deferred by `docs/adr/ADR-blackbox-record-schema.md` (see "What an `out` row covers"
   above). Until that is done, absence of an `out` row for a UserPromptSubmit hook is evidence of
   nothing.
9. **Whether SubagentStart carries task text is now UNSETTLED rather than settled, in our own
   artifacts.** The 2.1.220 walk recorded no task text on 17/17 real spawns. The committed
   2.1.251 census's `SubagentStart|writ-subagent-start|in` record class holds 124 rows, of which
   35 carry a `task` key and 28 carry a `prompt` key, while only 12 of the 124 carry
   `transcript_path` or `prompt_id`, which is the harness fingerprint the census partitions on
   (`writ/analysis/blackbox.py:179-196`). By arithmetic at least 23 of the `task`-bearing rows
   are not harness-origin and could be hand-built probe payloads. So the artifact cannot settle
   this in EITHER direction, and neither number should be read as Claude Code behavior. It is
   recorded here as a question for the next capture session, not as a correction to the tables
   above.
10. **The Part 2 per-event tables are unverified on the current build.** See the provenance gap
    under the header: the walk is 2.1.220, the newest capture is 2.1.251, and the two have not
    been reconciled field by field.

---

## Part 3: How to re-capture this for a new version

This map is a snapshot. To rebuild it on a new build in a few minutes, in a throwaway directory:
turn capture on (`touch ~/.claude/writ-blackbox.on`), exercise every tool and event you can
(write/edit/read files, run commands, spawn a subagent, force a few failures, run `/compact`), then
read two sources: the capture log `~/.claude/writ-blackbox.jsonl` for hook envelopes, and the
session's own transcript for per-tool input and result shapes. Tag every field by source, mark
what would not fire, and re-stamp the version. Turn capture off when done (`rm
~/.claude/writ-blackbox.on`).

The sentinel and the log path are both read by `blackbox_enabled` in `bin/lib/common.sh:616-618`:
capture is on when `WRIT_BLACKBOX=1` or the sentinel file exists, and the log defaults to
`${HOME}/.claude/writ-blackbox.jsonl` (override `WRIT_BLACKBOX_LOG`). As of 2026-08-31 the
sentinel is absent, so capture is OFF. That splits what can be checked today: a claim about this
tree's own code (which hook emits what, the record schema, the wiring) is verifiable by reading
the code, and a claim about what Claude Code sends or accepts is not verifiable at all until
capture is turned back on.

Then fold the log into the committed artifact:

```bash
writ blackbox-census --log ~/.claude/writ-blackbox.jsonl \
  --out docs/reference/blackbox-census.json --cc-version 2.1.251
```

`writ/cli.py:301-354` implements it. It reads only; it changes no capture coverage. An absent or
empty log is deliberately NOT an error: it yields a well-formed artifact with zero records and
every registered event listed under `events_never_observed`, which is the correct state right
after capture is switched on.

**The census partitions every row by ORIGIN**, into `harness`, `synthetic` and `undetermined`
(`writ/analysis/blackbox.py:179-196`), on a POSITIVE fingerprint rather than on absence:
synthetic when `hook_event_name` is wholly missing, which no real Claude Code envelope omits;
harness when the payload carries `transcript_path` or `prompt_id`; undetermined when it carries
`hook_event_name` and neither fingerprint field. The split exists because roughly a third of one
capture window was hand-built latency-probe payloads fed to hooks during a measurement, and
without the split a bare `record_count` reads as a count of real Claude Code traffic and
overstates the evidence by the contaminated share. The committed artifact records 4,650 harness,
2,754 synthetic and 1,984 undetermined out of 9,388. Proven-synthetic rows move to a
`synthetic_records` sibling; undetermined rows STAY in `records`, because moving a row out on the
strength of a missing field is the same error pointed the other way, and it would strip `out`
record classes of the evidence the provenance check reads.

That artifact is what `writ/shared/delivery.py::delivery_provenance` reads to back or refuse an
"observed" claim. An (event, mechanism) pair comes back `observed` only when some record class
for that event lists that mechanism with a non-zero count, and `unproven` otherwise. An absent,
unreadable or malformed artifact yields `unproven` for everything, because absence of evidence is
never evidence. `CENSUS_PATH` is read fresh on every call, so a regenerated artifact takes effect
without restarting the daemon.

**BOTH STALENESS DEFECTS ARE CLOSED as of 2026-08-31**, and this paragraph records what the
regeneration actually showed rather than only that it happened. The artifact used to list
`PostToolUse` under `directions_never_observed` despite `writ-posttool-rag.sh` having been
converted to the funnel in `1eb48b1`, and it held zero `exit`-direction rows because the `event`
and `exit_code` fields postdated it. A capture session from 2026-08-28 to 2026-08-31 fixed both:
13,477 records, 70 record classes.

WHAT THE REGENERATION PROVED, and it is the funnel's own argument confirmed on real traffic. OUT
record classes went from 4 to 14, and nine hooks that had never produced a single OUT row now do
(`pre-validate-file`, `validate-test-file`, `writ-bash-write-gate`, `writ-debug-code-gate`,
`writ-read-rag`, `writ-state-write-gate`, `writ-worktree-safety`, `writ-quality-judge`,
`inject-tier-workflow`), none of them edited individually. `writ-bash-write-gate|out` carries
`harness: 5` and `writ-quality-judge|out` carries `harness: 3`, where every OUT class in the
previous artifact read `harness: 0`, because the recorded pid used to be the per-call encoder's
and the origin join could never match. Real refusal exits appear for the first time:
`writ-comms-output-gate` 30 and `enforce-violations` 3.

WHAT IT STILL DOES NOT PROVE, which matters more than what it does. Every exit row files under
`event_not_observed` rather than under `Stop`, so `Stop` remains in `directions_never_observed`
and 11 of 12 events still carry no attributed exit row. That is not a capture gap: those hooks
read stdin with a bare `cat`, never call `load_hook_env`, and so observe no event name, and the
record says so instead of guessing. `delivery_provenance("Stop", "exit_nonzero_stderr")`
therefore still answers `unproven`, correctly. Capture is OFF again; it cost about 275 ms per
write across the 15 write-path hooks while on.
