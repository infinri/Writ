# Writ -- Enforcement Layer

Writ injects relevant coding rules into your context automatically via hooks.
You do not load rules manually. The hooks handle retrieval, injection, and budget.

---

## Hard constraints

These are not suggestions. Violating any of these will be caught by hooks and blocked.

1. **You do not write code until the tier is declared.** The only exception is plan.md
   -- no other file is permitted without a tier. The plan informs the tier classification,
   so it must be writable before the tier is declared.

2. **You do not create gate files.** Gate files (.claude/gates/*.approved) are created by
   the auto-approve hook when the user says "approved." You do not touch, create, or
   modify gate files. You do not run commands that create gate files.

3. **You do not proceed past a phase boundary without explicit user approval.** After
   presenting a phase deliverable, you STOP and tell the user: "Say **approved** to
   proceed." You do not continue working, write code, or start the next phase until the
   user responds with an approval.

---

## Workflow

### Step 1: Classify the task tier

Before writing ANY code, plan, or analysis -- classify the task complexity.
The RAG inject hook prints the exact `tier set` command with paths filled in. Run it.

| Tier | Label | Criteria |
|------|-------|----------|
| 0 | Research | No code generation. Auditing, investigating, explaining, reviewing. |
| 1 | Patch | 1-3 files changed. No new interfaces or contracts. |
| 2 | Standard | New class/interface, or modifying contracts. Single domain. |
| 3 | Complex | Multi-domain. Concurrency, queues, state machines, new endpoints. |

**Escalation rule:** Tiers only go UP, never down. If a Patch needs a new interface,
escalate to Standard. If Standard triggers concurrency concerns, escalate to Complex.
The session helper enforces up-only.

**No tier declared?** The gate hook denies all writes (except plan.md). Declaring a tier
unblocks the workflow for that tier's ceremony level.

### Step 2: Follow the tier's ceremony

**Tier 0 -- Research**
Deliver findings. No code files written. No gates. No plan.md.

**Tier 1 -- Patch**
Write code directly. Static analysis runs automatically after each file.
No plan.md required. No gates. No approval steps.

**Tier 2 -- Standard**
1. Write plan.md with a combined Phase A-C analysis. See "Gate criteria" below for
   required sections.
2. Present the plan to the user. STOP. Tell the user: "Say **approved** to proceed."
3. After phase-a approval: write test skeleton file(s) with method signatures.
4. Present test skeletons. STOP. Tell the user: "Say **approved** to proceed to
   implementation."
5. After test-skeletons approval: write implementation code.

**Tier 3 -- Complex**
Full sequential phases, each requiring its own approval:

1. **Phase A** -- Write plan.md with design and call-path declaration.
   See "Gate criteria" for required sections. Present, STOP, wait for approval.
2. **Phase B** -- Update plan.md with domain invariants and validation contracts.
   See "Gate criteria" for required section. Present, STOP, wait for approval.
3. **Phase C** -- Update plan.md with integration points and seam justification.
   See "Gate criteria" for required section. Present, STOP, wait for approval.
4. **Phase D** (only when concurrency/queues are involved) -- Update plan.md with
   concurrency modeling. Present, STOP, wait for approval. Skipped unless user or
   task explicitly involves concurrency.
5. **Test skeletons** -- Write test files with method signatures.
   Present, STOP, wait for approval.
6. **Implementation** -- Write code. Static analysis runs automatically.
7. **gate-final** -- All capabilities checked, all planned files exist.

---

## Gate criteria

This is the single reference for what the gate validator checks before creating each
gate file. If your deliverable is missing any required element, the gate will not open
and you will see a message naming what is missing.

### phase-a (Tier 2 and Tier 3)

plan.md must contain all four of these sections:

- **`## Files`** -- every file to be created or modified, with action (create/modify).
- **`## Analysis`** -- combined analysis content:
  - What the feature does and why
  - Interfaces, type contracts, invariants
  - Integration points, DI wiring, seam justification
- **`## Rules Applied`** -- at least one rule ID from the injected `--- WRIT RULES ---`
  block (matching pattern `[A-Z]+-[A-Z]+-\d{3}`), with a sentence on how each applies.
  If Writ returned no rules or all scores were below 0.3, write instead:
  `No matching rules. Domain: <domain description>`.
- **`## Capabilities`** -- checkbox items (`- [ ] description`) mapping to testable
  behaviors.

### phase-b (Tier 3 only)

plan.md updated with:
- **`## Domain Invariants`** -- interfaces, type contracts, validation rules, what must
  be true.

### phase-c (Tier 3 only)

plan.md updated with:
- **`## Integration Points`** -- API contracts, DI wiring, plugin/observer declarations,
  seam justification.

### phase-d (Tier 3 only, when applicable)

plan.md updated with:
- **`## Concurrency`** -- concurrency model, race conditions, retry behavior.

### test-skeletons (Tier 2 and Tier 3)

At least one test file exists matching language-appropriate patterns (e.g., `*Test.php`,
`test_*.py`, `*.test.ts`). The file must contain at least one test method signature.
Empty test files do not count.

### gate-final (Tier 3 only)

All capabilities from `## Capabilities` are checked. All files from `## Files` exist.

---

## Writ rules

Rules appear as a `--- WRIT RULES ---` block at the start of each turn. Use them.
They are the institutional knowledge base -- architecture principles, coding standards,
and domain-specific constraints from production experience.

When a rule is relevant to your current work, apply it. When writing plan.md, reference
rule IDs explicitly in `## Rules Applied`.

### When Writ is unavailable

If the server is not running, you will see `[Writ: server unavailable]`. Proceed
normally. No rules are blocked. To start the server: `writ serve`.

### Proposing new rules

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

### Recording feedback

When a rule directly influenced your implementation (you followed it, or it prevented
an error), record positive feedback:

```bash
curl -X POST http://localhost:8765/feedback -H 'Content-Type: application/json' \
  -d '{"rule_id": "RULE-ID-HERE", "signal": "positive"}'
```

Negative feedback (rule was present but didn't prevent an error) is recorded
automatically by the enforcement hooks.
