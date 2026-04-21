# Phase 0 schema proposal — methodology node and edge types

_Dated 2026-04-21. Proposal for maintainer review. Signed-off version becomes the Phase 1 Pydantic transcription contract._

## Scope

Plan Section 6.1 says Phase 1 adds "10 new node models" without specifying fields. Phase 0 synthetic corpus implicitly designs those fields — whatever the fixtures declare becomes the Phase 1 ingest parser's contract. Path B chosen on 2026-04-21: design once, Phase 1 transcribes, no rewrites later.

## Design principles

1. **Mirror existing `Rule` Pydantic model** (`writ/graph/schema.py:64-107`) for field naming, validation, and default values. Don't invent parallel conventions.
2. **Primary key convention:** `<type>_id: str`, matching existing `rule_id`, `abstraction_id`, `evidence_id`. No bare `id`.
3. **Retrieval surface (Stage 2 BM25 + Stage 3 vector) indexes `trigger + statement + tags` for retrievable types**, `body` at 0.5× weight per plan Section 3.2. Fields outside these four don't participate in retrieval ranking — they are schema metadata.
4. **Non-retrievable types still carry `<type>_id` + minimal schema** so they can participate in graph traversal (Stage 4) as bundle members without being primary retrieval hits.
5. **All new node types carry `source_attribution: str | None = None`** and **reserved `source_commit: str | None = None`** per Section 0.2.
6. **Severity / confidence / authority values reuse existing enums** (`Severity`, `Confidence`, `VALID_AUTHORITIES` from `schema.py:40-51, 24`).

## Common base fields

All new node models inherit from a shared base (implementation choice for Phase 1 — Pydantic `BaseModel` or an intermediate abstract class). Base fields:

| Field                  | Type                 | Required | Default                   | Notes |
| ---------------------- | -------------------- | -------- | ------------------------- | ----- |
| `<type>_id`            | `str`                | yes      | —                         | Matches existing `RULE_ID_PATTERN` regex |
| `domain`               | `str`                | yes      | —                         | Free-form; new methodology values: `process`, `communication`, `meta-authoring` |
| `severity`             | `Severity`           | yes      | —                         | Retrievable types only; non-retrievable may default `low` |
| `scope`                | `str`                | yes      | —                         | Existing `SCOPE_PATTERN` regex. New methodology values: `session`, `phase`, `task`, `workflow` |
| `trigger`              | `str`                | yes      | —                         | Non-empty; WHEN this node applies (per META-AUTH-001 discipline) |
| `statement`            | `str`                | yes      | —                         | Non-empty; the core content in one sentence |
| `rationale`            | `str`                | yes      | —                         | Non-empty; WHY this node exists |
| `tags`                 | `list[str]`          | no       | `[]`                      | BM25 Stage 2 input |
| `confidence`           | `Confidence`         | no       | `PRODUCTION_VALIDATED`    | Existing enum |
| `authority`            | `str`                | no       | `"human"`                 | Existing validator |
| `last_validated`       | `date`               | yes      | —                         | Matches Rule convention |
| `staleness_window`     | `int`                | no       | `365`                     | Matches Rule convention |
| `evidence`             | `str`                | no       | `"doc:superpowers"`       | Non-retrievable types may default `"structural"` |
| `times_seen_positive`  | `int`                | no       | `0`                       | Telemetry; retrievable types |
| `times_seen_negative`  | `int`                | no       | `0`                       | Telemetry; retrievable types |
| `last_seen`            | `str \| None`        | no       | `None`                    | Telemetry; retrievable types |
| `source_attribution`   | `str \| None`        | no       | `None`                    | `"superpowers@5.0.7"` for absorbed content |
| `source_commit`        | `str \| None`        | no       | `None`                    | Reserved per Section 0.2 |
| `body`                 | `str`                | no       | `""`                      | Retrievable types: markdown content indexed at 0.5× BM25 |

**Rule model additions (per plan Section 6.1) — extending existing `Rule`, not a new type:**

| Field                          | Type                        | Default  | Purpose |
| ------------------------------ | --------------------------- | -------- | ------- |
| `always_on`                    | `bool`                      | `False`  | Injected in always-on budget; plan Section 3.4 |
| `mechanical_enforcement_path`  | `str \| None`               | `None`   | Per Section 2.1 policy: `mandatory: true` requires non-empty |
| `rationalization_counters`     | `list[dict[str, str]]`      | `[]`     | Keys: `thought`, `counter` |
| `red_flag_thoughts`            | `list[str]`                 | `[]`     | Inline red-flag phrases for rule-level enforcement |
| `source_attribution`           | `str \| None`               | `None`   | Same as new node types |
| `source_commit`                | `str \| None`               | `None`   | Reserved |

## Retrievable node types

### Skill

`skill_id` prefix: `SKL-`

Inherits base only. No type-specific fields. Body carries the teaching — this is intentional: Skills are "how to think about this" guidance, and the teaching is prose, not structured data.

**Retrievable:** yes.

---

### Playbook

`playbook_id` prefix: `PBK-`

Base + ordered list of Phase references:

| Field                | Type                        | Required | Notes |
| -------------------- | --------------------------- | -------- | ----- |
| `phase_ids`          | `list[str]`                 | yes      | Ordered; Phase nodes referenced by ID |
| `preconditions`      | `list[str]`                 | no       | Node IDs that must hold before this Playbook runs |
| `dispatched_roles`   | `list[str]`                 | no       | SubagentRole IDs this Playbook can dispatch |

Phase nodes are stored separately (see below). Playbook + Phase IDs = ordered decomposition.

**Retrievable:** yes.

---

### Technique

`technique_id` prefix: `TEC-`

Inherits base only. Techniques are reusable subprocedures — shorter than Skills, atomic. Distinction from Skill is semantic (Skill = discipline; Technique = procedure), not schema.

**Retrievable:** yes.

---

### AntiPattern

`antipattern_id` prefix: `ANT-`

| Field                | Type                | Required | Notes |
| -------------------- | ------------------- | -------- | ----- |
| `counter_nodes`      | `list[str]`         | yes      | Skill/Playbook/Rule IDs that counter this anti-pattern |
| `named_in`           | `str \| None`       | no       | Source that introduced the name (e.g., "superpowers@5.0.7:testing-anti-patterns") |

**Retrievable:** yes.

---

### ForbiddenResponse

`forbidden_id` prefix: `FRB-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `always_on`              | `bool`              | no (`True`) | Defaults true for this type; included in universal always-on bundle per plan Section 3.4 |
| `forbidden_phrases`      | `list[str]`         | yes      | Exact phrase strings that trigger enforcement |
| `what_to_say_instead`    | `str`               | yes      | The constructive alternative |

**Retrievable:** yes (always-on).

## Non-retrievable node types

These do not participate in Stage 1-3 retrieval. They surface via Stage 4 graph traversal as bundle members of retrieved primary nodes.

### Phase

`phase_id` prefix: `PHA-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `position`               | `int`               | yes      | Ordering within parent Playbook (1-indexed) |
| `name`                   | `str`               | yes      | Short label (e.g., "Understand intent") |
| `description`            | `str`               | yes      | Full phase description |
| `parent_playbook_id`     | `str`               | yes      | Back-reference to containing Playbook |

**Retrievable:** no (structural; bundle-expansion only).

---

### Rationalization

`rationalization_id` prefix: `RAT-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `thought`                | `str`               | yes      | The excuse/rationalization text as the agent would phrase it |
| `counter`                | `str`               | yes      | The counter-argument |
| `attached_to`            | `str`               | yes      | Parent Skill/Playbook/Rule ID (single parent) |

**Retrievable:** no (bundle-only; attached to parent, surfaced when parent is retrieved).

---

### PressureScenario

`scenario_id` prefix: `PSC-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `prompt`                 | `str`               | yes      | The pressure prompt (what's sent to agent under test) |
| `expected_compliance`    | `str`               | yes      | What compliance looks like in the agent's response |
| `failure_patterns`       | `list[str]`         | yes      | Phrases/patterns indicating rationalization / non-compliance |
| `rule_under_test`        | `str`               | yes      | Rule ID this scenario tests |
| `difficulty`             | `str`               | yes      | `easy` / `medium` / `hard` |

**Retrievable:** no (test-harness only, never retrieved for agent context).

---

### WorkedExample

`example_id` prefix: `EXM-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `title`                  | `str`               | yes      | Short title |
| `before`                 | `str`               | yes      | The situation / input |
| `applied_skill`          | `str`               | yes      | Skill ID that was invoked |
| `result`                 | `str`               | yes      | The outcome after applying the skill |
| `linked_skill`           | `str`               | yes      | Parent Skill ID |

**Retrievable:** no (explicit-lookup-only; retrieved when user asks for example).

---

### SubagentRole

`role_id` prefix: `ROL-`

| Field                    | Type                | Required | Notes |
| ------------------------ | ------------------- | -------- | ----- |
| `name`                   | `str`               | yes      | e.g., `writ-code-reviewer` |
| `prompt_template`        | `str`               | yes      | System prompt for the subagent |
| `dispatched_by`          | `list[str]`         | no       | Playbook IDs that dispatch this role |
| `model_preference`       | `str \| None`       | no       | Optional model hint (e.g., `haiku`, `sonnet`) |

**Retrievable:** no (template-only; dispatched, not retrieved by agent).

## Edge types

Existing in `writ/graph/schema.py` (unused in practice but defined):

- `DependsOn`
- `Precedes`
- `ConflictsWith`
- `Supplements`
- `Supersedes`
- `RelatedTo`

New for methodology (plan Section 3.1 — 7 new types; `PRECEDES` already exists so effectively 6 new):

| Edge             | Source type            | Target type                        | Semantics |
| ---------------- | ---------------------- | ---------------------------------- | --------- |
| `TEACHES`        | Skill / Playbook       | Rule / Technique                   | "This node teaches the enforcement target" |
| `COUNTERS`       | AntiPattern            | Skill / Playbook / Rule            | "This anti-pattern is countered by the target" |
| `DEMONSTRATES`   | WorkedExample / FRB    | Skill / Rule                       | "This example/forbidden-phrase demonstrates the target's discipline" |
| `DISPATCHES`     | Playbook / Skill       | SubagentRole / Technique           | "Target is dispatched as a sub-invocation" |
| `GATES`          | Rule                   | Skill / Playbook                   | "Rule is the mechanical enforcement of the target's discipline" |
| `PRESSURE_TESTS` | PressureScenario       | Rule / Skill / Playbook            | "Scenario tests compliance with target" |
| `CONTAINS`       | Playbook               | Phase                              | "Phase is a structural member of Playbook" |
| `ATTACHED_TO`    | Rationalization        | Skill / Playbook / Rule            | "Rationalization is attached to parent" |

Plan Section 3.1 mentions 15 total edge types; this proposal has 6 existing + 8 new = 14. One edge type is unaccounted for — flagged as open question below.

## Identifier pattern

Existing `RULE_ID_PATTERN` in `schema.py:13`:

```python
r"^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)+(-\d{3}|(-[A-Z][A-Z0-9]*))$"
```

All new node IDs must match this pattern. Examples:

- `SKL-PROC-BRAIN-001` ✓
- `PBK-PROC-TDD-001` ✓
- `TEC-PROC-WORKTREE-001` ✓
- `ANT-PROC-TDD-001` ✓
- `FRB-COMMS-001` ✓
- `PHA-BRAIN-01` ✗ — two-digit suffix not allowed. Would need `PHA-BRAIN-001`.
- `RAT-BRAIN-TOO-SIMPLE` ✓ (alpha suffix allowed per regex)
- `PSC-BRAIN-001` ✓
- `EXM-TDD-001` ✓
- `ROL-CODE-REVIEWER-001` ✓

**Fix needed:** my exemplar PBK-PROC-BRAIN-001's phase IDs (`PHA-BRAIN-01` through `PHA-BRAIN-09`) violate the pattern. Rewrite as `PHA-BRAIN-001` through `PHA-BRAIN-009`.

## Enum additions for Phase 1

New `NodeType` enum to complement existing `Severity`, `Confidence`, `EvidenceType`:

```python
class NodeType(str, Enum):
    RULE = "Rule"
    ABSTRACTION = "Abstraction"
    SKILL = "Skill"
    PLAYBOOK = "Playbook"
    TECHNIQUE = "Technique"
    ANTIPATTERN = "AntiPattern"
    FORBIDDEN_RESPONSE = "ForbiddenResponse"
    PHASE = "Phase"
    RATIONALIZATION = "Rationalization"
    PRESSURE_SCENARIO = "PressureScenario"
    WORKED_EXAMPLE = "WorkedExample"
    SUBAGENT_ROLE = "SubagentRole"
```

New `EdgeType` enum (or class-per-edge per existing convention):

```python
class EdgeType(str, Enum):
    # existing
    DEPENDS_ON = "DEPENDS_ON"
    PRECEDES = "PRECEDES"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    SUPPLEMENTS = "SUPPLEMENTS"
    SUPERSEDES = "SUPERSEDES"
    RELATED_TO = "RELATED_TO"
    # new
    TEACHES = "TEACHES"
    COUNTERS = "COUNTERS"
    DEMONSTRATES = "DEMONSTRATES"
    DISPATCHES = "DISPATCHES"
    GATES = "GATES"
    PRESSURE_TESTS = "PRESSURE_TESTS"
    CONTAINS = "CONTAINS"
    ATTACHED_TO = "ATTACHED_TO"
```

Existing convention in `schema.py` uses class-per-edge (`class Precedes(_DirectedEdge)`). Phase 1 continues that pattern — new edge classes per type.

## Fixture file format (markdown + YAML front-matter)

One file per node at `tests/fixtures/synthetic_methodology/<node_id>.md`:

```markdown
---
<type>_id: SKL-PROC-BRAIN-001
node_type: Skill
domain: process
severity: high
scope: session
trigger: "..."
statement: "..."
rationale: "..."
tags: [process, brainstorming]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
source_attribution: "superpowers@5.0.7"
# type-specific fields
# ...
edges:
  - { target: PBK-PROC-BRAIN-001, type: TEACHES }
  - { target: ENF-PROC-BRAIN-001, type: GATES }
---

# Human-readable body

Markdown content. For retrievable types, indexed at 0.5× BM25 weight per plan Section 3.2.
```

**Parser:** Phase 0 benchmark harness reads front-matter via PyYAML, splits body at `^---\n` delimiter. Phase 1 ingest parser replaces this with `<!-- NODE START type=X id=Y -->` markers (plan Section 6.1 deliverable 2) — fixtures migrate to the new format when Phase 1 lands.

## Resolved decisions (maintainer signoff, 2026-04-21)

1. **Edge type count: 14.** 6 existing + 8 new. The "15 total" figure in plan Section 3.1 was an earlier draft counting `PRECEDES` as new when it already existed. Do not invent a 15th edge to hit an approximate number. If content authoring surfaces a real gap (e.g., `ALTERNATIVE_TO` for brainstorm approaches), add it then with justification.

2. **Node ID convention: keep `<type>_id` per-type.** `rule_id` stays on Rule. `skill_id`, `playbook_id`, etc. on new types. Universal `node_id` would force migration of 80+ existing rules for zero functional benefit. Per-type naming is unambiguous at call sites (seeing `playbook_id` in code = Playbook context without a type lookup).

3. **`body: str = ""` added to Rule model.** Default empty. Coding rules ignore it (empty indexes as nothing in BM25, no impact on vector embedding). Methodology-flavored Rules (ENF-PROC-*) populate it for teaching prose that doesn't fit trigger/statement/violation/pass_example. No migration required — field defaults empty.

4. **Severity optional for non-retrievable types.** `Severity | None = None` on Phase, Rationalization, PressureScenario, WorkedExample, SubagentRole. Required (`Severity`) on retrievable types. Rationale: severity feeds ranking weight `w_severity = 0.099`; non-retrievable types never enter ranking, so a required severity would be schema noise. Pydantic class hierarchy enforces the split — `_RetrievableBase` requires severity, `_NonRetrievableBase` makes it optional.

5. **`scope` regex unchanged.** Existing `[a-z][a-z0-9_-]*` accepts all proposed values (`session`, `phase`, `task`, `workflow`). No extension.

6. **`tags` normalization on ingest: lowercase, dedupe, sort.** Deterministic normalization prevents BM25 index inconsistency (e.g., "TDD" vs "tdd" as distinct terms). One-line transform at the ingest boundary. Phase 1 adds a test that ingests mixed-case duplicated tags and asserts the stored form is lowercase / deduplicated / sorted. Every downstream consumer (BM25 index, vector embedding concatenation, graph queries) sees the same canonical form.

## Rationalization representation — graph-canonical, inline-as-render-convenience

Rationalizations appear in two places in the schema:

- **`rationalization_counters: list[dict[str, str]]` on Rule** (inline, keys `thought` / `counter`)
- **`Rationalization` standalone node** (fields `thought` / `counter` / `attached_to`, edges via `ATTACHED_TO`)

**Canonical form: the standalone graph node.** Rationalization nodes participate in bundle expansion via `ATTACHED_TO` edges during Stage 4 graph traversal — that's load-bearing for the "retrieve a Rule, surface its counter-thoughts alongside" behavior that plan Section 3.1 describes.

**Inline form on Rule: render convenience, not schema duplication.** The inline `rationalization_counters` field exists so the always-on rendering path (plan Section 3.4) can produce a Rule summary without a graph traversal — the budget-injection hook reads the Rule node and renders its counters inline. Without the inline field, summary-form always-on rendering would require a traversal per rule every injection, which defeats the budget constraint.

**Ingest parser behavior:** when ingesting a Rule with rationalization content, the parser:
1. Creates standalone `Rationalization` nodes, one per entry, with `attached_to = <parent rule_id>`.
2. Creates `ATTACHED_TO` edges from each Rationalization to the parent Rule.
3. Populates the inline `rationalization_counters` field on the Rule for render-path consumption.

**On divergence, graph nodes win.** If the inline field and the graph nodes disagree (e.g., a standalone Rationalization is added post-ingest but the inline field isn't refreshed), the graph representation is authoritative. A reconciliation script may regenerate the inline field from the graph — but never the reverse.

**Same pattern applies to `red_flag_thoughts`.** Not strictly required (red flags don't have their own node type in Section 2.3), so the inline-only representation stands. If Phase 5 retrospective shows we need standalone RedFlag nodes for bundle expansion or pressure-testing, that's an additive Phase 5+ decision, not Phase 1.

## Signoff and next steps

Once this doc is signed off:
1. Phase 0: rewrite the 7 exemplar fixtures to match this schema.
2. Phase 0: batch the remaining ~44 fixtures.
3. Phase 0: implement benchmark harness using this front-matter format as parse contract.
4. Phase 1: Pydantic models in `writ/graph/schema.py` mirror these field definitions exactly. Migration script creates Neo4j labels matching `NodeType` values and relationship types matching `EdgeType` values.

_End of proposal._
