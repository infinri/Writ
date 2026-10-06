# ADR: The tool-failure budget (program item 7c)

Status: accepted for wave 1, workstream C.

## Context

An agent that hits a failing tool call tends to repeat it, and nothing in Writ noticed: a fourth
identical test run, Edit or WebFetch cost the same as the first and taught nothing. The program
(docs/programs/knowledge-engine-program.md, "Tool failure budget") asks for a refusal after three
consecutive failures of the same tool, and for a refusal of an exact repeat of an irreversible call.

Constraints:

- Every tool call pays the hook twice (before and after), and hook cost is measured in process
  starts (tests/test_write_path_process_budget.py), dominated by python starts.
- The harness fires PostToolUseFailure for a failed call and PostToolUse for a successful one. A
  call a PreToolUse hook refuses fires neither, and some tool validation failures fail before
  PostToolUseFailure can fire (docs/reference/claude-code-blackbox.md, "Failure payload").
- The envelope parser already resolves identity (HOOK_SESSION_ID is the agent id when present,
  parse-hook-stdin.jq line 128) and normalizes tool_input (JSON strings parsed, the
  CLAUDE_TOOL_INPUT fallback applied), so each sub-agent has its own cache file.

## Decision

1. One streak per agent and tool in the session-cache field `tool_failure_streak`, key
   `<agent id or main>|<tool>`, value `{count, input_hash, tool, agent, last_failed_at}`, written
   only by bin/lib/writ_tool_failure.py under `mutate_cache`, in the cache file the parser's
   HOOK_SESSION_ID names.
2. Both hooks hash the parser's HOOK_ENVELOPE tool_input; the helper normalizes nothing itself, so
   the counting side and the refusing side cannot hash different inputs. An empty input hashes as
   the empty object on both.
3. The streak counts IDENTICAL failures only: the same hash extends it, any other input restarts it
   at 1. An interrupted call is not counted (a literal probe of the raw envelope, because the
   normalized one drops `is_interrupt`).
4. A successful call of the same tool clears that tool's entry; a successful Write, Edit or
   NotebookEdit clears every entry the agent holds.
5. PreToolUse refuses when the entry's count is at least 3 and the call's hash equals the stored
   one, with `[ENF-TOOL-BUDGET]`, the next step (change the input), and the override (for a shell
   command, the user runs it with a leading `!`; otherwise ask the user). Each refusal writes one
   `gate_decision` audit row (gate `tool-budget`) and one `tool_budget_denied` friction row.
6. Neither hook calls load_hook_env; both parse with `_writ_parse_hook_stdin` plus eval, the
   writ-blackbox-capture.sh pattern, so the sub-agent seeder never runs on every tool call.
7. Fail open everywhere and never create a cache.
8. The hot path decides whether python is needed with a NEW zero-process read, `$(<file)` plus a
   literal `case` test. It is not a reuse of `writ_session_mode_pair`, which reads the same file
   through `json_transform` and so spawns the JSON filter or python.
9. Irreversible exact repeat: no new mechanism. The Bash gate's classifier refuses every command it
   matches on every attempt, so the repeat is refused as the first was; this is pinned by a test.

## Alternatives considered

- Count every consecutive failure of a tool regardless of input (the program's literal wording).
  Rejected: it refuses the changed input the refusal asks for.
- Exclude test-runner commands from the streak. Rejected: it needs a classifier of commands, a
  second classifier of the kind the design forbids, and its false positive (rerun after an edit) is
  removed more precisely by decision 4.
- A separate counter file per session. Rejected for the approved design's reasons: one schema with
  backfill, one lock, and the cache directory is already protected gate state.
- Refusing from PreToolUse state alone, without a PostToolUse reset. Rejected: a passing rerun is
  invisible there, so a later single failure would refuse.
- Hashing the raw envelope's tool_input on the counting side. Rejected: it re-implements the
  parser's normalization and diverges on the CLAUDE_TOOL_INPUT fallback.
- Adding `is_interrupt` to the parser's normalized envelope. Rejected for this cycle: it changes
  both parser arms and their parity tests for one boolean a literal probe answers.
- Tracking irreversible calls in state. Rejected: the stateless classifier already refuses every
  attempt, and a store would be a second classifier.
- A UserPromptSubmit reset so a user's reply clears the streak. Deferred: the user-owned `!`
  clearance and the success resets cover the override without a fourth registration on the prompt
  path.

## Consequences

- Registrations 48 to 51 (record on PostToolUseFailure, budget on PreToolUse and PostToolUse);
  events unchanged at 12.
- Two more hooks on every tool call, each starting no interpreter on the hot path and neither
  running the seeder. The write-path ratchet's hook tuple is not extended; the new hooks carry
  their own process pin.
- Residue, all failing open: a validation failure that never fires PostToolUseFailure is not
  counted; a number the parser re-spells across its two arms could hash two identical inputs apart
  only if the two calls were parsed by different arms; a tool name the JSON writer escapes is never
  seen by the literal probe.
