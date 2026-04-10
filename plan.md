# Plan: Approval Broadening + PostToolUse RAG + Confidence Metrics

## Files

| File | Action |
|------|--------|
| `.claude/hooks/auto-approve-gate.sh` | modify -- broaden approval regex |
| `.claude/hooks/writ-posttool-rag.sh` | create -- PostToolUse hook for post-write rule checking |
| `bin/lib/writ-session.py` | modify -- add `metrics` command, add `pretool_queried_files` tracking |
| `.claude/hooks/writ-pretool-rag.sh` | modify -- write file path to `pretool_queried_files` in session cache |
| `.claude/settings.json` | modify -- register PostToolUse hook |
| `tests/test_approval_patterns.py` | create -- approval pattern unit tests |
| `tests/test_posttool_rag.py` | create -- PostToolUse hook tests |
| `tests/test_metrics.py` | create -- confidence metrics tests |

## Analysis

### What and why

Three features that close remaining gaps in the enforcement layer:

**1. Approval pattern broadening.** The current approval detector in auto-approve-gate.sh
misses natural phrasing like "ok proceed with remaining work". The friction log (lines 5, 9)
confirms two misses. Root causes: (a) the regex patterns require the approval word at position
zero, but users prefix with "ok/okay/sure/yeah"; (b) the 60-char ceiling rejects any prompt
longer than 60 characters, even when it clearly contains approval intent.

Fix: add a prefix-tolerant regex layer and raise the ceiling to 120 chars for prompts that
contain a known approval word.

**2. PostToolUse RAG query.** PreToolUse queries Writ with the file path before a write. This
catches relevant rules but misses violations in the actual code. PostToolUse queries Writ with
code-derived patterns from the content Claude just wrote, catching violations after the fact.

**Gap-only firing:** PostToolUse only fires when PreToolUse did NOT already query for the same
file. PreToolUse writes the file path to `pretool_queried_files` in the session cache after each
query. PostToolUse checks that set and skips if present. This avoids doubling budget consumption
(the last session used 35% across 12 PreToolUse queries; doubling would leave thin headroom).

Design: new hook `.claude/hooks/writ-posttool-rag.sh`, hook type PostToolUse (matcher: Write|Edit).
Always exits 0 -- advisory only, never blocks. Extracts code content from `tool_input` in the
envelope (Write: `content` field, Edit: `new_string` field). Builds a keyword query from code
patterns, queries `/query`, formats and injects results. Budget capped at 1/4 of remaining per
query (same as PreToolUse). Relevance threshold 0.4 (same as PreToolUse).

**Query construction by content type:**

Source code (PHP, Python, JS/TS, Go, Rust, Java): extract function/method names, class
references, import/use statements, framework-specific calls (repository calls, DI patterns,
decorator names). Standard token extraction via regex.

XML config (di.xml, webapi.xml, events.xml, etc.): extract class references from XML attributes
(`class="..."`, `instance="..."`, `type="..."`), route/resource definitions, plugin method names,
event observer names. Uses a separate XML-aware extraction path that reads attribute values and
tag-specific patterns rather than treating markup like source code.

Non-source files (JSON, YAML, markdown, etc.): skip entirely. Same `detect_language` check as
PreToolUse -- if unknown, exit early.

**3. Confidence metrics.** Reads `workflow-friction.log` files and session caches to produce a
structured report on enforcement quality. Added as `cmd_metrics` in writ-session.py. Computes:
- Clean run rate (sessions with no gate invalidations / total sessions)
- Phase transition time (average, p50, p90 seconds between gates)
- Friction event frequency by type
- Tier distribution
- Approval pattern miss rate
- Per-rule violation/invalidation frequency

Output: JSON report to stdout. Input: either a specific friction log path or auto-detected from
the project root. Operates on historical data only -- no I/O in any hot path.

### Interfaces and contracts

**PostToolUse hook contract:**
- Input: Claude Code PostToolUse envelope (JSON on stdin) with `tool_input.content` or
  `tool_input.new_string`, and `tool_input.file_path`
- Output: formatted rule block on stdout (same format as PreToolUse/UserPromptSubmit injection)
- Exit: always 0
- Skip condition: file path exists in `pretool_queried_files` set in session cache (PreToolUse
  already covered this file)
- Side effects: updates session cache (rule IDs, budget cost, query count)

**PreToolUse hook change:**
- After querying Writ, writes the file path to `pretool_queried_files` in session cache via
  `writ-session.py update --add-pretool-file <path>`. One new `--add-pretool-file` flag in
  `cmd_update`.

**Metrics command contract:**
- Input: `writ-session.py metrics [--log PATH] [--format json|text]`
- Output: JSON report on stdout (or human-readable table with --format text)
- Exit: 0 on success, 1 on no data
- Reads: workflow-friction.log, /tmp/writ-session-*.json

**Approval detector contract (unchanged interface):**
- Input: lowercased, trimmed prompt
- Output: "yes" or "no" on stdout
- No new dependencies

### Integration points

**PostToolUse hook registration:** Must be added to the project's `.claude/settings.json` as a
PostToolUse hook entry with matcher `Write|Edit`, matching the existing PreToolUse pattern.

**Session cache integration:** PostToolUse hook uses the same `writ-session.py update` command
as existing hooks. Adds rules to the same phase-partitioned rule ID lists. Uses the same
`should-skip` budget check. PreToolUse writes to `pretool_queried_files`; PostToolUse reads it.
Both go through `cmd_update` / `cmd_read` -- no new commands, just one new `--add-pretool-file`
flag and one new field in the cache default.

**Metrics command:** Standalone read-only command. No integration with the hot path. Reads the
same friction log and session cache files that existing hooks write.

## Rules Applied

- **PERF-IO-001**: PostToolUse hook must not add significant latency to the write path. Tight
  curl timeout (0.3s connect, 1s max). Code content extraction is in-memory string processing
  only. The metrics command reads historical files, not hot-path data.

- **PY-PYDANTIC-001**: If a new server endpoint were needed, request/response bodies would go
  through Pydantic models. The current plan reuses the existing `/query` endpoint, so no new
  models are needed.

## Capabilities

- [ ] Approval detector matches "ok proceed with remaining work"
- [ ] Approval detector matches "sure, go ahead"
- [ ] Approval detector matches "yeah approved, continue with implementation"
- [ ] Approval detector rejects non-approval prompts that happen to contain approval substrings
- [ ] PostToolUse hook skips files already covered by PreToolUse (gap-only firing)
- [ ] PreToolUse hook records queried file paths in session cache
- [ ] PostToolUse hook extracts code content from Write envelope
- [ ] PostToolUse hook extracts code content from Edit envelope
- [ ] PostToolUse hook builds keyword query from source code patterns
- [ ] PostToolUse hook builds keyword query from XML config patterns (class refs, routes, plugins)
- [ ] PostToolUse hook queries Writ and injects relevant rules
- [ ] PostToolUse hook respects session budget and skip conditions
- [ ] PostToolUse hook skips non-source files
- [ ] Metrics command computes clean run rate from friction log
- [ ] Metrics command computes phase transition time statistics
- [ ] Metrics command reports friction event frequency by type
- [ ] Metrics command reports tier distribution
