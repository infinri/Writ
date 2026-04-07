# Writ -- RAG-Powered Rule Retrieval

Writ automatically injects relevant coding rules into Claude's context via hooks.
You do not need to load rules manually. The hooks handle it.

---

## Development workflow -- MANDATORY

Before any task, classify its complexity tier. The tier controls ceremony (how many
gates), not knowledge (which rules are loaded). All tiers receive the same injected
rules from the `--- WRIT RULES ---` block.

### Step 1: Classify the task tier

Read the task description. Count: files affected, new interfaces, state transitions,
endpoints, concurrency concerns. Declare the tier in one sentence, then register it:

```bash
python3 <writ>/bin/lib/writ-session.py tier set <0-3> <session_id>
```

The RAG inject hook provides the exact command with paths filled in.

### Tier definitions

| Tier | Label | Criteria | Ceremony |
|------|-------|----------|----------|
| 0 | Research | No code generation. Auditing, investigating, explaining, reviewing. | Rules injected. Deliver findings. No phases, no gates, no plan.md. |
| 1 | Patch | 1-3 files changed. No new interfaces or contracts. No new state transitions. No new endpoints. | Rules injected. Write code. Static analysis. No phases, no gates, no plan.md. |
| 2 | Standard | New class/interface, or modifying existing contracts/signatures. Single domain. No concurrency, no queues, no multi-actor writes. | Phases A-C combined (one approval gate: `phase-a.approved`). Test skeletons. Implementation. Static analysis. |
| 3 | Complex | Multi-domain. Concurrency, state machines, queues, new endpoints, or multi-actor writes. | Full protocol: separate A, B, C, [D] gates. Test skeletons. Slices. ENF-GATE-FINAL. |

**Escalation rule:** Tiers only go UP, never down. If during a Patch you discover a new
interface is needed, escalate to Standard. If Standard triggers concurrency concerns,
escalate to Complex. The session helper enforces up-only.

**No tier declared?** Gate hooks fall back to Tier 3 behavior (full gates). Declaring a
tier makes things easier, never harder.

### Tier 0 -- Research

- Read code, investigate, explain, audit, answer questions.
- No code files are written. No gates.

### Tier 1 -- Patch

- Fix bugs, change config, small refactors (1-3 files).
- Write code directly. Run static analysis after each file.
- No plan.md, no phases, no gates.

### Tier 2 -- Standard

Present Phases A-C as a single combined analysis:
- What the feature does and why, which files change (Phase A)
- Interfaces, type contracts, domain invariants (Phase B)
- API contracts, DI wiring, integration seam justification (Phase C)

Present to user and STOP. Tell the user: "Say **approved** to proceed."
The user's approval automatically creates the gate file.

Then: test skeletons. Present and STOP. User says "approved" again. Then: implementation.

If Phase D concerns arise (concurrency, queues), escalate to Tier 3.

### Tier 3 -- Complex

Full sequential phases, each with its own gate:

**Phase A: Design and call-path declaration**
Produce a plan in the module directory (`{module}/plan.md`):
- What the feature/fix does and why
- Which files will be created or modified
- Call-path: entry point -> service -> repository -> output
- Which Writ rules apply and how you will satisfy them
- Dependencies on existing code (interfaces, framework APIs)

STOP. Tell user: "Say **approved** to proceed to Phase B."

**Phase B: Domain invariants and validation**
- Define interfaces and type contracts
- Identify validation rules and domain constraints
- Declare what must be true for the feature to be correct

STOP. Tell user: "Say **approved** to proceed to Phase C."

**Phase C: Integration points and seam justification**
- Define API contracts, DI wiring, plugin/observer declarations
- Justify each integration seam (why this extension point, not another)
- Declare how this integrates with existing modules

STOP. Tell user: "Say **approved** to proceed."

**Phase D: Concurrency modeling (when applicable)**
Only when the task involves queues, consumers, async workers, or parallel processing:
- Declare concurrency model
- Identify race conditions and prevention
- Define retry/dead-letter behavior

STOP. Tell user: "Say **phase d approved** to proceed."

**Test skeletons**
- Test class skeletons with method signatures and docstrings
- No implementation yet

STOP. Tell user: "Say **approved** to proceed to implementation."

**Implementation**
- Write code following the approved plan
- Apply all Writ rules from the injected block
- Run static analysis after each file
- ENF-GATE-FINAL required before marking complete

### Critical rules

- **NEVER create gate files yourself.** Gate files are created automatically when the user says "approved."
- **NEVER `touch` gate files or run commands to approve gates.** The auto-approve hook handles this.
- If a gate hook blocks a write, you are ahead of the workflow. Present the phase deliverable, tell the user to say "approved", and STOP.
- Present phase deliverables clearly so the user can evaluate.
- Classify the tier FIRST. If you skip classification, the hooks default to Tier 3 (maximum ceremony).

---

## How it works

1. **UserPromptSubmit hook** fires at the start of every turn
2. Hook queries Writ's RAG server (`localhost:8765/query`) with the user's prompt
3. Writ's hybrid pipeline (BM25 + vector + graph) ranks and returns the most relevant rules
4. Rules are injected into your context as a `--- WRIT RULES ---` block
5. A session cache deduplicates rules across turns and tracks token budget

You will see rules appear at the start of each turn. Use them. They are the institutional
knowledge base -- architecture principles, coding standards, enforcement rules, and
domain-specific constraints accumulated from production experience.

## Rule authority model

Rules have three authority levels:
- **human** -- authored and validated by humans. Highest trust.
- **ai-promoted** -- AI-proposed, graduated through frequency tracking (n>=50, ratio>=0.75). Ceiling: peer-reviewed confidence.
- **ai-provisional** -- AI-proposed, not yet graduated. Lowest trust. Ceiling: speculative confidence.

At equal relevance scores, human rules outrank AI rules (hard preference, not weight-based).

## When Writ is unavailable

If the server is not running, hooks fall back gracefully:
- You will see `[Writ: server unavailable, proceeding without rules]`
- Proceed normally. No rules are blocked.
- To start the server: `writ serve` (requires Neo4j running)

## Proposing new rules

You MUST propose a rule when any of these occur during a task:

1. **Bug fix reveals a missing guard** -- you fixed a bug that a rule should have prevented. Propose a rule for the root cause pattern, not the symptom.
2. **Architectural decision with no prior art** -- you made a design choice (e.g., "use Protocol over ABC", "extract to a shared module") that has no matching rule in the injected set. If the decision would benefit future tasks, propose it.
3. **User corrects your approach** -- the user says "don't do X" or "always do Y" and no injected rule covers it. The correction is a candidate rule.
4. **Framework/library gotcha** -- you discover a non-obvious constraint (e.g., "CartTotalRepository returns stale data after quote save") that would trap future agents.

Do NOT propose rules for:
- One-off project-specific decisions (use project memory instead)
- Obvious language syntax or standard library usage
- Rules that duplicate an already-injected rule (check the WRIT RULES block first)

### How to propose

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
  "last_validated": "2026-03-29",
  "task_description": "what you were doing when you discovered this",
  "query_that_triggered": "the prompt that led here"
}'
```

**Rule ID convention:** `{DOMAIN}-{CATEGORY}-{NNN}` where DOMAIN is a broad area
(ARCH, PY, PHP, FW, DB, TEST, PERF, SEC, ENF, OPS) and CATEGORY is a subcategory.
Check existing rules in the injected block to avoid ID collisions.

Proposed rules enter as `ai-provisional` and must pass the structural gate
(schema, specificity, redundancy, novelty, conflict checks) before ingestion.
They graduate to `ai-promoted` through frequency tracking (n>=50, ratio>=0.75).

## Recording feedback

When a rule from the `--- WRIT RULES ---` block directly influenced your implementation
(you followed it, or it prevented an error), record positive feedback:

```bash
curl -X POST http://localhost:8765/feedback -H 'Content-Type: application/json' \
  -d '{"rule_id": "RULE-ID-HERE", "signal": "positive"}'
```

If a rule was misleading or inapplicable to the situation, record negative feedback:

```bash
curl -X POST http://localhost:8765/feedback -H 'Content-Type: application/json' \
  -d '{"rule_id": "RULE-ID-HERE", "signal": "negative"}'
```

Feedback drives frequency tracking: rules with high positive-to-negative ratios
graduate from `ai-provisional` to `ai-promoted`. Rules with sustained negative
feedback get flagged for human review.

## Context hygiene

- Rules injection skips automatically when context > 75%
- Rules injection skips when session token budget (8000) is exhausted
- Trivial prompts (< 10 chars) do not trigger rule queries
- The Stop hook tracks context pressure between turns

## Development hooks

These hooks enforce the full software engineering cycle. They are project-agnostic
and work with any language or framework.

### RAG injection
- **writ-rag-inject.sh** (UserPromptSubmit) -- queries Writ, injects relevant rules
- **writ-context-tracker.sh** (Stop) -- records context pressure for skip decisions

### Gate enforcement
- **auto-approve-gate.sh** (UserPromptSubmit) -- detects approval patterns ("approved", "lgtm", "proceed", etc.) in the user's prompt and creates the next pending gate file. Tier-aware: skips for Tier 0-1, uses Tier 2/3 gate sequences.
- **check-gate-approval.sh** (PreToolUse) -- blocks writes until phase approval markers exist. Three-tier pattern matching: cross-language, language-specific, framework-specific. Gate sequence: A -> B -> C -> [D] -> test-skeletons.
- **enforce-final-gate.sh** (PreToolUse) -- blocks completion markers until ENF-GATE-FINAL verified.

### Validation
- **pre-validate-file.sh** (PreToolUse) -- static analysis on proposed content BEFORE write. Language-routed via `bin/run-analysis.sh`.
- **validate-file.sh** (PostToolUse) -- static analysis on written file AFTER write.
- **validate-handoff.sh** (PostToolUse) -- validates handoff JSON schema for slice handoffs.

### Session metrics
- **log-session-metrics.sh** (Stop) -- logs context metrics when gate files are touched.

## Verification scripts

| Script | Purpose |
|--------|---------|
| `bin/lib/writ-session.py` | Session cache helper (read, update, format, should-skip) |
| `bin/run-analysis.sh` | Static analysis routing (PHPStan, ESLint, ruff, xmllint, cargo, go vet) |
| `bin/check-gates.sh` | Gate approval status check |
| `bin/verify-files.sh` | Batch file existence check |
| `bin/scan-deps.sh` | Import/dependency scanning (PHP, JS, TS, Python, Go, Rust, Ruby) |
| `bin/verify-matrix.sh` | Completion matrix from plan.md capabilities block |
| `bin/validate-handoff.sh` | Handoff JSON schema validation |
| `bin/lib/common.sh` | Shared functions (project root, language detection, JSON output) |
| `bin/lib/gate-categories.json` | Framework-agnostic gate classification matrix |

## Static analysis

Before and after file writes, validation hooks run language-appropriate static analysis
via `bin/run-analysis.sh`. Supported: PHPStan (PHP), ESLint (JS/TS), ruff (Python),
xmllint (XML), cargo check (Rust), go vet (Go). Fix any errors before proceeding.

## Gate approval files

Gate markers live in the consuming project, never in Writ. They are created
automatically by the `auto-approve-gate.sh` hook when the user says "approved."
You do not need to create them manually.

```
{PROJECT_ROOT}/.claude/gates/phase-a.approved
{PROJECT_ROOT}/.claude/gates/phase-b.approved
{PROJECT_ROOT}/.claude/gates/phase-c.approved
{PROJECT_ROOT}/.claude/gates/phase-d.approved
{PROJECT_ROOT}/.claude/gates/test-skeletons.approved
{PROJECT_ROOT}/.claude/gates/gate-final.approved
```

## plan.md location

Each module writes its own plan.md to its module directory -- never the project root.
The enforce-final-gate hook validates this.

---

## Writ codebase reference

When modifying Writ source code, read `.claude/CODEBASE.md` first. It contains:
- Architecture overview and five-stage pipeline
- Module map with load-bearing flags
- Key invariants (8 items -- violating any breaks the system)
- Configuration reference (writ.toml)
- Test structure (282 tests, 12 benchmarks)
- Testing and benchmarking directives (what to run after which changes)
