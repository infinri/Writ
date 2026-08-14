# Session state, modes, and gates

The enforcement layer's contract: where session state lives, how modes and phases move, exactly what each gate checks, and the approval token's lifecycle. Source of truth: `writ/session/` (a package; `bin/lib/writ-session.py` is the CLI facade hooks call when the daemon is down) and `writ/server/routes/{gate,session_state}.py`.

## 1. The session cache

One JSON file per session: `<install>/var/session/writ-session-<sid>.json` (`WRIT_CACHE_DIR` overrides; the default is deliberately not `/tmp`, which systemd empties at boot; that wipe once destroyed every cache and presented as `mode=None`).

**Schema** (single source `_default_cache()`, `writ/session/cache.py`; missing keys are backfilled on read, evolution is additive): mode, `current_phase`, `gates_approved`, `loaded_rule_ids` and `loaded_rule_ids_by_phase`, `always_on_rule_ids`, `remaining_budget`, `context_percent`, `files_written`, `analysis_results`, `pending_violations`, `invalidation_history`, `escalation`, `paused_work_state`, `is_subagent`, `is_orchestrator`, `parent_session_id`, `agent_type`, `citation_log`, `coverage_scope`, `source_type`, `verification_evidence`, `quality_judgment_state`, `queried_rules_by_file`, `phase_transitions`, `project_root`, and friends.

Two fields that look redundant and are not: `loaded_rule_ids` doubles as the ranked-query *exclude* list, so always-on rules are tracked separately in `always_on_rule_ids`; merging them would make always-on rules unretrievable and their citations read as hallucinated.

**Concurrency**: atomic writes (unique temp file, fsync, rename); read-modify-write under a per-session flock, reentrant per thread; a raising mutation writes nothing. Sub-agent caches are separate files keyed by `agent_id`; at commit time their per-file queried rules union into the parent's record.

**Session identity**: `$CLAUDE_SESSION_ID` > `$CLAUDE_JOB_DIR` basename > **None** (`resolve_current_session_id`, `writ/session/cache.py`). There is no third tier. The two that were there both answered "which session am I" with "whichever one moved last": the `/tmp/writ-current-session` pointer is one file per machine that every session's UserPromptSubmit hook overwrites, and the newest-cache-by-mtime glob read a directory every project shares. Callers now fail loud instead (the hook-facing CLI exits 2 naming the two env vars; `writ doctor` reports no session), because a wrong answer here writes a mode, an approval or an audit scope into somebody else's session and no log shows it happened. On a harness-rotated session id, the SessionStart hook carries the **mode only** into the fresh session, same project only; gates are never inherited.

**Compaction boundary**: PreCompact drops full rule objects (keeps ids); PostCompact is the authoritative recovery signal for *state*: budget resets to 8,000, the phase-scoped exclusion list clears, and the sticky-rule preference clears. Neither compaction event reaches the model, so re-orientation is deferred rather than delivered there: Claude Code's validator rejects a PostCompact `hookSpecificOutput` reply outright and bare PostCompact stdout lands only in the CC debug log (observed on a real `/compact`, 2026-08-14), so `cmd_reset_after_compaction` sets `post_compact_pending`, `writ-postcompact.sh` itself emits nothing, and `writ-rag-inject.sh` emits the mode-and-phase state line plus the re-verify directive on the next `UserPromptSubmit`, clearing the flag as it goes (pre-compaction verification output is second-hand).

## 2. Modes

`MODE_CONFIG` (`writ/session/mode_engine.py`) is the single source; five modes: `conversation`, `debug`, `investigate`, `review`, `work`.

- **`mode set`** always re-initializes: phase to the mode's initial phase, gates cleared, denial counts cleared, `project_root` stamped from cwd. "Gates cleared" means **this session's** `gates_approved` and this session's `.approved` artifacts (`_clear_gate_artifacts` takes the session id and refuses to default it); a sibling session's approvals in the same repo are untouched, which the flat project-wide path it replaced could not promise.
- **`mode init`** (the auto-router's entry) is a no-op when a mode is already set; it can never wipe a live gate cycle.
- **`mode switch`** out of work snapshots `{phase, gates, phase-scoped rules}` into `paused_work_state`; switching back restores it. The debug-to-work transition seeds `plan.md`'s `## Root Cause Evidence` from `debug.md` (idempotent, skipped when restoring a paused implementation phase).
- Auto-routing (`bin/lib/writ_mode_hint.py`, standalone stdlib for hot-path safety) fires only on unset mode: audit/explore/research shapes route to `investigate`, build shapes to `work`.

## 3. The write gate

`_can_write_check` (`writ/session/gates.py`) evaluates in strict order:

1. **Credential deny, every mode, overrides every exemption**: secret directories (`/.ssh/`, `/secrets/`, `/.gnupg/`, `/.kube/`), then credential basenames and extensions (`.env` minus templates, `*.key`, `*.pem`, `id_rsa`, `kubeconfig`, ...). Path-based; the file is never opened.
2. **Exemptions, each logged, never silent**: the Writ install's own tree; `~/.claude/settings.json` / `settings.local.json` (exact basenames, `realpath`-resolved to kill symlink escapes); sub-agents (`is_subagent`) bypass entirely: the orchestrator already passed the human gate, and worker scope is bounded by role tools, not re-policing.
3. **Special files**: `plan.md` writable pre-mode and through planning/testing (denied in implementation); `capabilities.md` always writable.
4. **No mode**: deny all writes (except the special files).
5. **Debug**: source edits denied until `debug.md` has a non-empty `## Root cause`.
6. **Review / conversation / investigate**: allow all.
7. **Work**: allow when both gates are approved; otherwise only the exclusion list (`bin/lib/gate-categories.json`: tests, migrations, `__init__.py`, `.claude/` config and markdown) is writable, everything else denies with `[ENF-GATE-PLAN]` or `[ENF-GATE-TEST]`. Every denial increments a per-gate counter; repeated denials escalate the hook decision from deny to ask.

Glob caution for the exclusion list: `*` spans `/` and matches the raw path, so a directory-shaped glob exempts any depth and `..` escapes it. Keep patterns extension-anchored or exact filenames.

The same check serves the Write/Edit/NotebookEdit tools (`/pre-write-check`, which also returns file-context RAG on allow) and Bash-mediated writes (`writ-bash-write-gate.sh` extracts redirect and copy targets quote-aware, plus write targets inside interpreter one-liners such as `python -c` and `node -e`, and calls `/session/{sid}/can-write` with each). The runtime-lens read gate (`_can_read_code_check`) blocks Read/Grep/Glob on source until `debug.md` has real Evidence and Narrowing sections; it fails open on any internal error by design.

## 4. Work phases and validators

`planning -> (phase-a) -> testing -> (test-skeletons) -> implementation`. Advancing from a terminal phase errors; it never silently no-ops.

- **phase-a** validates `plan.md`: `## Files` entries need a backticked path, a change type, and a reason; `## Analysis` non-empty; `## Rules Applied` must cite at least one rule id (or say "No matching rules"), and every cited id is checked against the union of loaded, phase-scoped, and always-on rule ids; anything outside that union is recorded as hallucinated. `## Capabilities` must be unchecked `- [ ]` boxes.
- **test-skeletons** requires a session-written test file with a real test signature, falling back to a project scan.

The validators are presence checks and say so; the correctness check is the human reading the artifact before approving. `apply_phase_advance` is the one mutation unit both the CLI and HTTP paths share (a cross-path parity test pins byte-identical cache effects): union the gate into `gates_approved`, move the phase, clear that gate's denials, roll the phase rule bucket to history, record the transition, clear a matching escalation.

**Plan-boundary enforcement after approval**: every write is analyzed post-hoc; at the boundary, a confirmed violation of a rule that was loaded at planning time invalidates phase-a (a sentinel file makes the *next* hook run exit blocking) and deletes the `.approved` artifact. Three invalidation cycles set an escalation with a differential diagnosis (`same-rule`, `different-rules`, or `mixed`), cleared only when the escalated gate advances.

## 5. The approval token

The anti-self-approval keystone, `writ/session/gate_token.py` plus `hooks/scripts/auto-approve-gate.sh`:

- **Minted** only by the hook, only when the *user's typed prompt* matches an approval pattern (`bin/lib/approval_match.py`: exact set, small-edit fuzzy, bounded regexes; fail-closed), only if no token already exists. `secrets.token_hex(16)`, chmod 600, at `/tmp/writ-gate-token-<sid>` (deliberately `/tmp`: byte-identical path with the bash writer; a reboot costs one re-approval, not a session).
- **Claimed atomically**: `os.rename` to a unique name means exactly one of N concurrent claimers wins; claiming *is* consuming, which closed a real double-advance race.
- **Spent by**: a successful advance, or a failed artifact validation (a changed artifact needs fresh approval). **Not spent by**: no pending gate, or an unresolvable project root.
- **Consumers**: `/advance-phase` and `/promote-candidate` only, the two routes that move workflow or write canon. Both validate the target gate *before* claiming. A tokenless attempt logs `agent_self_approval_blocked` to the audit stream and refuses.
- Two confirmation sources, one endpoint: the pattern path (`auto-approve-gate.sh`, `confirmation_source="pattern"`, only in work mode during planning/testing) and the tool path (`/writ-approve`, `confirmation_source="tool"`, which must send `cwd` because the server cannot substitute its own working directory). Approval writes a `<project_root>/.claude/gates/<session_id>/<gate>.approved` artifact: an audit record, not the source of truth. The `<session_id>` component is the isolation boundary (one python source, `writ/session/locators.py::gate_dir`, mirrored byte-for-byte in bash as `writ_gate_dir`): the flat per-project path it replaces let one session's approval read as approved to every other session in the same repo, and let `mode set` in one session delete another's. The accepted cost is that **approvals do not outlive the session that earned them**: `writ-session-end.sh` writes each artifact's metrics row and then removes it, so a new session re-approves.

## 6. Investigate mode's gates

Lens table (`writ/session/investigations.py`): `code` -> synthesis gate (advisory), `web` -> triangulation gate (**hard**), `runtime` -> the debug gates. Coverage uses a frozen denominator (`--freeze-scope`; re-freeze needs `--force`), examined = citations plus queried files; the synthesis gate requires a frozen scope and at least one examined in-scope file and explicitly claims nothing more. Triangulation requires citations from two independent registrable domains and fails closed at zero. Web content is auto-captured into citations by the WebFetch/WebSearch hook; Bash output auto-captures as runtime evidence in debug mode. Audit fan-out partitions the frozen scope first-fit-decreasing at 2,000 LOC / 30 files per worker, rolls coverage up, aggregates findings with contradiction surfacing, and ranks partitions by attention score.

## 7. Sub-agent lifecycle

`SubagentStart` creates the worker's isolated cache: inherits mode (read **file-direct** from the parent's cache, never daemon-first; a divergent daemon once served `mode=None` to every worker), phase, and approved gates; resets budget and rules; sets `is_subagent`. The CC payload carries only `agent_type` (no task text), so start-time RAG falls back to a role-derived query; rules reach workers via `additionalContext`. Dispatch discipline rewrites confident generic dispatches to the five `writ-*` roles in work/investigate/unset modes and asks on ambiguity (escapes: `[general-purpose]`, `[writ:dispatch-ok]`). `SubagentStop` records completion metrics, drains the worker's telemetry buffer, records a reviewer verdict, and runs the transcript tripwire: it is the only reliable read window on a sub-agent transcript, because Claude Code deletes those files when the session ends. One deliberate exception to agent-id keying: pending-test markers key on the RAW parent session id, because only the parent's Stop hook ever reads them.

## 8. Failure posture: fail open, by decision

When the daemon is unreachable (and, for gates, when a decision helper crashes outright), enforcement hooks allow rather than block: the pre-write dispatch and the Bash write gate proceed without a decision, Stop-time enforcement does not fire when mode resolution fails, and advisory checks skip. This is a deliberate availability-over-enforcement decision (user directive 2026-08-01): an infrastructure outage must never lock a user out of their own repository. Two boundaries hold regardless: credential-path denial in the Bash gate needs no server and stays closed, and the approval token cannot be minted or spent without the daemon, so workflow *advancement* (as opposed to raw writes) still halts. The compensating control is visibility, not blocking: gate crashes surface as hook-error notices rather than silence, degraded checks say so on stderr, and the audit stream records what ran. Audits should treat fail-open-on-outage as the specification, not a defect.

## 9. Observability of enforcement

Every gate decision (allow and deny), mode change, phase advance, self-approval block, invalidation, promotion, verification-evidence record, and citation lands on the 365-day `audit` stream (`writ/shared/logging.py`); hook executions and timings land on `metrics`. `writ audit-session <sid>` renders a per-session timeline; `writ analyze-friction --rule-effectiveness` computes per-rule denial stick rates. The citation log in the cache is a bounded working set; the durable copy is the stream, written only after the cache write succeeds.
