---
name: writ
description: >
  Hybrid RAG knowledge retrieval service for AI coding rule enforcement.
  Replaces flat-file rule loading with graph-native retrieval via a five-stage
  pipeline (domain filter, BM25 keyword, ANN vector, graph traversal, RRF ranking).
  Rules are injected automatically into Claude's context via hooks -- no manual
  loading required. Activates on ALL software engineering tasks: code generation,
  code review, design, research, auditing, debugging, testing, planning, and
  architecture decisions. The knowledge base is always queried -- task complexity
  only determines the level of ceremony, not whether Writ activates.
metadata:
  author: lucio-saldivar
  version: "2.0"
---

# Writ -- Hybrid RAG Rule Retrieval + Enforcement

## Two layers, one system

**Knowledge layer (Writ server):** A stateless RAG service. Answers "what rules
apply to this context?" via a five-stage pipeline (BM25 + vector + graph traversal
+ RRF ranking). Rules are facts -- they don't change based on workflow state.

**Enforcement layer (hooks + writ-session.py):** A session-aware workflow engine.
Owns phase state, gate criteria, file classification, and mode routing. Hooks are
thin clients that delegate decisions to `writ-session.py`.

## How rules reach you

Rules are injected automatically by `writ-rag-inject.sh` on every user turn:

1. Takes the user's prompt as a natural language query
2. POSTs to `http://localhost:8765/query` with session state (budget, loaded IDs)
3. Writ's pipeline ranks rules by relevance
4. Returns rules formatted as a `--- WRIT RULES ---` block in your context

If no rules appear, either the server is down (you'll see a warning) or the prompt
was too short (< 10 chars) or budget was exhausted.

Rules are phase-aware: only current-phase rule IDs are excluded from re-injection.
When a phase advances, previously loaded rules can be re-injected for the new phase.

## What the rules look like

```
--- WRIT RULES (N rules, <mode> mode) ---

[RULE-ID] (severity, authority, domain) score=N.NNN
WHEN: trigger condition
RULE: what must be done
VIOLATION: example of doing it wrong
CORRECT: example of doing it right

--- END WRIT RULES ---
```

## Rule authority

- **human** -- highest trust, manually authored
- **ai-promoted** -- AI-proposed, graduated via frequency tracking (n>=50, ratio>=0.75)
- **ai-provisional** -- AI-proposed, not yet graduated, lowest trust

Human rules outrank AI rules at equal relevance (hard preference rule).

## Mode system

Every session operates in one of four modes. The mode determines workflow ceremony,
RAG strategy, and whether code generation is allowed. All modes receive rules.

| Mode | Purpose | Gates | Code generation |
|------|---------|-------|-----------------|
| Conversation | Discussion, brainstorming | None | No |
| Debug | Investigating a problem | None | No |
| Review | Evaluating code against rules | None | No |
| Work | Building/modifying code | plan + test-skeletons | Yes |

No mode declared = all writes blocked (except plan.md).

Set via: `python3 <writ>/bin/lib/writ-session.py mode set <conversation|debug|review|work> <session_id>`
(the RAG inject hook prints the exact command with paths filled in).

The `tier` command is still supported as a facade for backward compatibility.
Tier 0 maps to Conversation, tiers 1-3 map to Work.

## Gate enforcement

`writ-session.py` is the sole authority on phase state:

- **can-write**: Decides whether a file write is allowed. Reads the tool input envelope,
  classifies the file against `gate-categories.json`, checks the session's approved gates.
  Returns allow/deny. The shell hook (`check-gate-approval.sh`) is a thin client.

- **advance-phase**: Validates artifacts (plan.md sections, test files), creates gate
  files, clears current-phase rule IDs, logs the transition to the audit trail.
  The shell hook (`auto-approve-gate.sh`) detects approval patterns and delegates.

- **current-phase**: Returns the authoritative current phase from session state.

Gate files (`.claude/gates/*.approved`) are artifacts created by `advance-phase`.
They contain the session ID. Stale gates from previous sessions are rejected.

### Gate criteria (what the validator checks)

**plan (phase-a)** (Work mode): plan.md with `## Files`, `## Analysis`,
`## Rules Applied` (rule IDs or "No matching rules" declaration),
`## Capabilities` (checkboxes). Validated automatically by ExitPlanMode hook.

**test-skeletons** (Work mode): At least one test file with a test method signature.

## Session management

`bin/lib/writ-session.py` manages per-session state in temp files:

- **Mode** (conversation, debug, review, work)
- **Current phase** (planning, testing, implementation -- Work mode only)
- **Gates approved** (source of truth, not inferred from disk)
- **Loaded rule IDs by phase** (for exclude-list scoping)
- **Phase transitions** (audit trail with timestamps and triggers)
- **Token budget** (starts at 8000, decrements per query)
- **Context pressure** (skips RAG queries when context > 75%)
- **Analysis results** (per-file pass/fail from static analysis)
- **Pending violations** (rule violations awaiting phase-boundary routing)
- **Invalidation history** (gate invalidation records for escalation detection)

## Hooks inventory

| Hook | Event | Role |
|------|-------|------|
| `writ-rag-inject.sh` | UserPromptSubmit | RAG query, rule injection, mode/workflow reminders |
| `auto-approve-gate.sh` | UserPromptSubmit | Approval pattern detection, delegates to advance-phase |
| `check-gate-approval.sh` | PreToolUse(Write/Edit) | Thin client for can-write |
| `pre-validate-file.sh` | PreToolUse(Write/Edit) | Static analysis before write |
| `validate-exit-plan.sh` | PreToolUse(ExitPlanMode) | Plan format validation + auto gate advance |
| `enforce-final-gate.sh` | PreToolUse(Write/Edit) | Blocks completion markers (Work mode) |
| `inject-tier-workflow.sh` | PostToolUse(Bash) | Immediate workflow injection after mode/tier set |
| `validate-file.sh` | PostToolUse(Write/Edit) | Static analysis after write |
| `validate-rules.sh` | PostToolUse(Write/Edit) | Rule compliance validation via /analyze |
| `validate-handoff.sh` | PostToolUse(Write/Edit) | Handoff JSON schema validation |
| `friction-logger.sh` | Stop | Friction capture (gate denials, mode changes, phase transitions) |
| `writ-context-tracker.sh` | Stop | Context pressure + auto-feedback |
| `log-session-metrics.sh` | Stop | Gate approval context metrics |

## Supported languages and frameworks

Gate categories support: PHP, Python, JavaScript, TypeScript, Go, Rust, Java, Ruby,
GraphQL, XML. Framework-specific patterns: Magento 2, Django, Rails, Spring, NestJS,
Express, Laravel. Add new patterns via `bin/lib/gate-categories.json`.

Static analysis routes by file extension: PHPStan, ESLint, ruff, xmllint, cargo check,
go vet. Add new analyzers via `bin/run-analysis.sh`.

## Proposing new rules

Propose a rule when any of these occur:

1. **Bug fix reveals a missing guard** -- you fixed a bug that a rule should have
   prevented. Propose for the root cause pattern, not the symptom.
2. **Architectural decision with no prior art** -- you made a design choice with no
   matching rule in the injected set, and it would benefit future tasks.
3. **User corrects your approach** -- the user says "don't do X" or "always do Y" and
   no injected rule covers it.
4. **Framework/library gotcha** -- a non-obvious constraint that would trap future agents.

Do NOT propose for: one-off project decisions (use project memory), obvious language
usage, or duplicates of already-injected rules.

```bash
curl -X POST http://localhost:8765/propose -H 'Content-Type: application/json' -d '{
  "rule_id": "DOMAIN-CATEGORY-NNN",
  "domain": "architecture",
  "severity": "medium",
  "scope": "function",
  "trigger": "when this situation occurs",
  "statement": "what must be done",
  "violation": "example of doing it wrong",
  "pass_example": "example of doing it right",
  "enforcement": "how to verify compliance",
  "rationale": "why this matters",
  "last_validated": "YYYY-MM-DD",
  "task_description": "what you were doing when you discovered this",
  "query_that_triggered": "the prompt that led here"
}'
```

Rule ID convention: `{DOMAIN}-{CATEGORY}-{NNN}` where DOMAIN is a broad area
(ARCH, PY, PHP, FW, DB, TEST, PERF, SEC, ENF, OPS). Check existing rules to avoid
ID collisions.

Proposed rules enter as `ai-provisional` and must pass the structural gate (schema,
specificity, redundancy, novelty, conflict checks).

### Recording feedback

When a rule directly influenced your implementation (you followed it, or it prevented
an error), record positive feedback:

```bash
curl -X POST http://localhost:8765/feedback -H 'Content-Type: application/json' \
  -d '{"rule_id": "RULE-ID-HERE", "signal": "positive"}'
```

Negative feedback (rule was present but didn't prevent an error) is recorded
automatically by the enforcement hooks.

## Server requirements

Writ requires:
- Neo4j at `bolt://localhost:7687`
- Writ server: `writ serve` (default: `localhost:8765`)
- Bible rules imported: `writ import-markdown`
- Hooks installed: `bash scripts/install-skill.sh`

When loaded as a plugin, `ensure-server.sh` starts Neo4j (Docker) and the Writ
server automatically via the Init lifecycle hook.

## Architecture reference

- Server: `writ/server.py` (FastAPI, async)
- Pipeline: `writ/retrieval/pipeline.py` (5-stage hybrid)
- Schema: `writ/graph/schema.py` (Rule, Abstraction, Edge models)
- Config: `writ.toml` (all tunable parameters)
- Session engine: `bin/lib/writ-session.py` (phase state, gate management, audit trail)
- Hook parser: `bin/lib/parse-hook-stdin.py` (normalizes Claude Code stdin envelope)
- Gate categories: `bin/lib/gate-categories.json` (language/framework file patterns)
- Checklists: `bin/lib/checklists.json` (phase exit criteria)
- Static analysis: `bin/run-analysis.sh` (multi-language router)
- Verification: `bin/verify-matrix.sh` (capabilities completion), `bin/verify-files.sh` (batch existence)
- Hooks: `.claude/hooks/` (12 hooks, see inventory above)
- Plugin manifest: `.claude-plugin/plugin.json` (auto-discovery, lifecycle)
- Lifecycle: `scripts/ensure-server.sh`, `scripts/stop-server.sh`
- Install: `scripts/install-skill.sh` (hook wiring into settings.json)
- Full spec: `RAG_arch_handbook.md`
