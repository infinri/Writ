# Writ Integration Guide

Install, hook wiring, mode system, CLI and HTTP reference. For the
top-level pitch and architecture overview, see the [README](../README.md).

---

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Neo4j 5.x (Docker or local):

```bash
docker run -d \
  --name writ-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/writdevpass \
  -e NEO4J_PLUGINS='["apoc"]' \
  -v writ-neo4j-data:/data \
  neo4j:5-community
```

## Quick Start

```bash
# Migrate existing rules into the graph
python scripts/migrate.py

# Optional: generate abstraction nodes for compressed retrieval
writ compress

# Start the service (pre-warms all indexes)
writ serve

# Query from CLI
writ query "controller contains SQL query"

# Or hit the API directly
curl -X POST http://localhost:8765/query \
  -H "Content-Type: application/json" \
  -d '{"query": "async blocking event loop"}'
```

## Claude Code Integration

Writ integrates with Claude Code via hooks and a plugin manifest that
automatically inject relevant rules into Claude's context on every turn.
No manual rule loading required.

### Setup

```bash
# 1. Start Neo4j (if not already running)
docker start writ-neo4j

# 2. Ingest rules and start the server
python scripts/migrate.py       # first time only
writ serve                      # runs on localhost:8765

# 3. Install hooks into Claude Code
bash scripts/install-skill.sh   # patches ~/.claude/settings.json

# 4. Restart Claude Code
```

After setup, every Claude Code session automatically queries Writ for
relevant rules. The hooks handle injection, deduplication, budget
tracking, and context pressure.

### Plugin mode

Writ also ships a `.claude-plugin/plugin.json` manifest. When Claude
Code discovers the plugin, it registers hooks and lifecycle scripts
automatically -- no manual `install-skill.sh` step required.

The plugin lifecycle handles server startup:

- **Init** (`scripts/ensure-server.sh`) -- starts Neo4j (Docker) and
  `writ serve` if they are not already running. Non-fatal: if Docker is
  missing or startup times out, hooks fall back gracefully.
- **Shutdown** (`scripts/stop-server.sh`) -- stops the Writ server.
  Neo4j is left running since it may be shared with other tools.

### Mode system

Every session operates in one of four modes that control ceremony and
code generation:

| Mode | Purpose | Gates | Code generation |
|------|---------|-------|-----------------|
| Conversation | Discussion, brainstorming | None | No |
| Debug | Investigating a problem | None | No |
| Review | Evaluating code against rules | None | No |
| Work | Building/modifying code | plan + test-skeletons | Yes |

The RAG inject hook prompts Claude to set a mode on the first turn.
Gate hooks read the mode from the session cache and adjust enforcement
accordingly. If no mode is declared, hooks deny all writes (except
plan.md). A legacy tier facade (0-3) is supported for backward
compatibility.

### How it works

1. **UserPromptSubmit hook** fires at the start of every user turn
2. Hook POSTs the user's prompt to `localhost:8765/query`
3. Writ's hybrid pipeline ranks rules by relevance (BM25 + vector + graph proximity)
4. Rules are injected into Claude's context as a `--- WRIT RULES ---` block
5. If no mode is declared, a classification directive is also injected
6. A session cache deduplicates rules across turns and tracks a token budget (8000 tokens)
7. When context exceeds 75% or budget is exhausted, injection silently skips

### What Claude sees

```
--- WRIT RULES (3 rules, standard mode) ---

[PY-ASYNC-001] (?, ?, ?) score=0.892
WHEN: Calling a sync I/O function inside an async def function.
RULE: Async call chains must use async I/O end-to-end.
VIOLATION: Using requests.get() in an async handler
CORRECT: Using httpx.AsyncClient within async context

--- END WRIT RULES ---
```

### Feedback loop

The integration includes a complete feedback cycle:

- **Automatic feedback** -- the Stop hook correlates which rules were
  in context with static analysis outcomes (pass/fail) and auto-POSTs
  feedback to Writ. This feeds the frequency tracking system so rules
  can graduate from `ai-provisional` to `ai-promoted`.
- **Rule proposals** -- when no matching rules are found (or scores
  are low), the hook appends a proposal nudge to Claude's context.
  Claude can propose new rules via `POST /propose` when it discovers
  patterns not in the knowledge base.
- **Coverage tracking** -- the session cache records which files were
  written and which rule domains covered them, logged to
  `/tmp/writ-coverage-*.log`.

### Ensuring Writ starts with Claude Code

**Plugin mode (recommended):** If Claude Code discovers the
`.claude-plugin/plugin.json` manifest, the `ensure-server.sh` Init
lifecycle script starts Neo4j and the Writ server automatically. No
manual setup needed.

**Manual mode:** If not using the plugin, run `scripts/ensure-server.sh`
before opening Claude Code, or start the services yourself:

```bash
docker start writ-neo4j                   # start Neo4j
source .venv/bin/activate && writ serve   # start Writ
```

If Writ is not running, the hooks fall back gracefully -- Claude sees
`[Writ: server unavailable, proceeding without rules]` and proceeds
normally.

### Hooks

All hooks parse Claude Code's stdin JSON envelope via a shared parser
(`bin/lib/parse-hook-stdin.py`) with `$CLAUDE_TOOL_INPUT` env var
fallback. Gate enforcement is centralized in `bin/lib/writ-session.py`
-- hooks are thin clients that delegate decisions and enforce the
result. PostToolUse hooks skip validation when the write itself failed
(`tool_result_is_error`).

| Hook | Event | Matcher | Purpose |
|------|-------|---------|---------|
| `writ-rag-inject.sh` | UserPromptSubmit | all | Query Writ, inject rules, mode/workflow state |
| `auto-approve-gate.sh` | UserPromptSubmit | all | Detect approval patterns, delegate to advance-phase |
| `check-gate-approval.sh` | PreToolUse | Write\|Edit | Thin client for can-write; deny-to-ask escalation |
| `pre-validate-file.sh` | PreToolUse | Write\|Edit | Static analysis before write |
| `enforce-final-gate.sh` | PreToolUse | Write\|Edit | Block completion until ENF-GATE-FINAL |
| `writ-pretool-rag.sh` | PreToolUse | Write\|Edit | File-context RAG injection before writes |
| `writ-read-rag.sh` | PreToolUse | Read | File-context RAG injection for Review/Debug reads |
| `validate-exit-plan.sh` | PreToolUse | ExitPlanMode | Plan format validation on /plan exit |
| `inject-tier-workflow.sh` | PostToolUse | Bash | Immediate workflow injection after mode/tier set |
| `validate-file.sh` | PostToolUse | Write\|Edit | Static analysis after write |
| `validate-rules.sh` | PostToolUse | Write\|Edit | Rule compliance validation via /analyze |
| `validate-handoff.sh` | PostToolUse | Write\|Edit | Handoff JSON schema validation |
| `writ-posttool-rag.sh` | PostToolUse | Write\|Edit | Post-write RAG injection from code patterns |
| `friction-logger.sh` | Stop | all | Friction capture (gate denials, phase transitions, hook timing) |
| `writ-context-tracker.sh` | Stop | all | Context pressure, auto-feedback, token snapshots |
| `log-session-metrics.sh` | Stop | all | Gate approval context metrics |
| `writ-subagent-start.sh` | SubagentStart | all | Create isolated session cache, inject Writ rules |
| `writ-subagent-stop.sh` | SubagentStop | all | Log sub-agent completion metrics |

All hooks are project-agnostic -- they work with any language or
framework. Hooks fire inside sub-agents with `agent_id` and
`agent_type` in the payload. Each sub-agent gets its own session cache
(isolated RAG budget and gate state).

## CLI Commands

| Command | Description |
|---|---|
| `writ serve` | Start the local service. Pre-warms indexes. Binds to localhost:8765. |
| `writ ingest <path>` | Parse Markdown rules and ingest into the graph. Validates schema. Auto-exports on success. |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy, unreviewed count, frequency staleness, graduation flags. |
| `writ add` | Interactive rule authoring with relationship suggestion and conflict detection. |
| `writ edit <rule_id>` | Edit an existing rule with re-validation and relationship re-analysis. |
| `writ export <path>` | Regenerate Markdown from graph. Round-trip fidelity with ingest. |
| `writ compress` | Cluster rules into abstraction nodes. Evaluates HDBSCAN vs k-means. |
| `writ propose` | Propose an AI-generated rule. Runs structural gate before ingestion. |
| `writ review` | Review AI-proposed rules. List, inspect, promote, reject, downweight, stats. |
| `writ feedback <rule_id> <signal>` | Record positive/negative feedback for a rule (hook integration). |
| `writ migrate` | One-time migration of existing rules into graph. |
| `writ query "..."` | CLI rule query for testing retrieval quality. |
| `writ status` | Health check: rule count, index status, export staleness. |

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/query` | POST | Ranked rule retrieval. Accepts `query`, `domain`, `budget_tokens`, `exclude_rule_ids`, `loaded_rule_ids`. |
| `/propose` | POST | AI rule proposal. Runs structural gate, ingests if accepted with authority=ai-provisional. |
| `/feedback` | POST | Record positive/negative feedback for a rule. Increments frequency counters. |
| `/rule/{rule_id}` | GET | Full rule node. `?include_graph=true` adds 1-hop context. Returns `abstraction_id` and `sibling_rule_ids`. |
| `/conflicts` | POST | CONFLICTS_WITH edges between provided rule_ids. |
| `/abstractions` | GET | All abstraction nodes with member counts. |
| `/abstractions/{id}` | GET | Full abstraction with member rule details. |
| `/health` | GET | Status, rule/mandatory count, index state, startup time. |

## Testing

```bash
# Unit and integration tests (~320 tests across 30 test files)
pytest tests/ -q

# Performance benchmarks (12 tests, requires Neo4j with migrated rules)
pytest benchmarks/bench_targets.py -v -s

# Neo4j traversal scale benchmarks (1K/10K nodes)
pytest benchmarks/run_benchmarks.py -v -s

# Comprehensive scale benchmark (80/500/1K/10K rules)
python benchmarks/scale_benchmark.py

# Export ONNX model (one-time, required for ONNX inference)
python scripts/export_onnx.py
```

## Configuration

All settings in `writ.toml`, overridable via `WRIT_` environment
variables.
