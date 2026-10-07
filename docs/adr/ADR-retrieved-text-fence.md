# ADR: fence retrieved text, show raw similarity, say what an absent block means

Status: accepted
Date: 2026-10-07
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (knowledge-engine program item 5, workstream P, phase 0)
Touches: `writ/shared/injection_text.py`, `writ/session/budget_tracking.py`,
`writ/retrieval/prompt_bundle.py`, `writ/retrieval/injection_ceiling.py`,
`writ/server/routes/query.py`, `writ/session/recall.py`, `writ/retrieval/ranking.py`,
`writ/retrieval/pipeline.py`, `templates/CLAUDE.md`
Siblings: `docs/adr/ADR-ranked-header-fields.md` (the sim slot),
`docs/adr/ADR-prompt-injection-split.md` (the recall clamp).

## Context

Every injection channel prints text read back from the graph: rule fields, methodology
nodes, abstraction summaries, decision titles and rationales, and (workstream D) document
chunks. The frames around that text were plain lines, `--- WRIT RULES (...) ---` and
`--- END WRIT RULES ---`, and nothing stopped the text inside from containing the same
lines. Three concrete forgeries were possible:

1. A stored statement holding a line `--- END WRIT RULES ---` closes the block early, so
   whatever follows reads as Writ's own text.
2. `split_format` reads the last `WRIT_META:` line of cmd_format's output as the list of
   rule ids the session records. `str.splitlines` treats U+2028, U+2029, U+0085 and several
   control characters as line ends, so a statement holding U+2028 followed by
   `WRIT_META:{...}` was parsed as a meta line and could put invented ids on the record.
3. Bidirectional overrides, zero-width characters and the tag block (U+E0000 to U+E007F)
   reach the model invisibly to a human reviewing the same text.

Long documents (item 5's goal) make all three more likely: they are written by anyone with
commit access, not reviewed as doctrine.

## Decision

### One sanitizer: `sanitize_retrieved(text)`

Applied to retrieved text only, never to Writ's own notices.

1. `None` or `""` returns `""`.
2. `"\r\n"`, `"\r"`, U+0085, U+2028 and U+2029 become `"\n"`.
3. Every other character of Unicode category Cc (except `"\n"` and `"\t"`) and every Cf
   character is removed. The class is a precompiled expression written as explicit ranges
   (Unicode 15.0.0), so import scans nothing; a test checks it against `unicodedata` over
   every code point.
4. A line whose first non-blank text, in any case, is a fence marker gets a backslash before
   that text. Markers: `---` or `===`, optional blanks, optional `END `, then `WRIT`,
   `ALWAYS-ACTIVE` or `APPLICABLE RULES`; and `WRIT_META:`. So `--- END WRIT RULES ---`
   inside a statement prints as `\--- END WRIT RULES ---`. Removal runs before escaping, so a
   marker hidden behind a zero-width character is still escaped.
5. Mentions in the middle of a line are prose and stay: the corpus has a trigger reading
   "the --- WRIT RULES --- block" and an inline `[writ:dispatch-ok]` escape hatch an agent
   copies, and escaping them would corrupt both. Only a line-start marker can impersonate
   a boundary.
6. Text with nothing to change is returned as the same object, and the function is
   idempotent, because an escaped line no longer starts with a marker.

Writ's own notice lines (`[Writ: ...]`, the truncation marker, the nudges, the decision
card lead-in) are not markers: they are not boundaries, and escaping them would escape
Writ's own truncation marker inside a clamped block.

### One frame: `fence(label, body, detail="")`

    --- WRIT <LABEL> (<detail>) ---
    <sanitized body>
    --- END WRIT <LABEL> ---

LABEL is upper-case ASCII letters, spaces and hyphens; anything else raises `ValueError`
(a programmer error). The detail is sanitized and collapsed onto one line, and an empty
detail drops the parentheses. This is byte for byte the frame cmd_format already printed,
so the ranked block, the methodology companion's inner block, `/session/format`,
`/pre-write-check`'s `rag_rules`, `/subagent/start-context` and the action push keep their
bytes for clean text. fence sanitizes the whole body, which is safe because no line Writ
writes inside a block (`[ID] (...) score=`, `WHEN:`, `RULE:`, `VIOLATION:`, `CORRECT:`,
`RATIONALE:`, `RELATED:`, `[ABSTRACT: ...]`, `SEE: ...`, card lines) starts with a marker,
and it means a caller cannot forget a field. The cost of a frame is
`len(fence(label, "", detail))`, with no separate overhead constant to drift.
`is_fence_close(line)` is true exactly for a Writ close line.

### Block form and line form, per channel

| channel | change |
|---|---|
| ranked, methodology companion, `/session/format`, `/subagent/start-context`, `/pre-write-check` rag_rules, read and post-tool RAG, action push | all run cmd_format, which frames through `fence("RULES", ...)` |
| always-on block | `_renderable_always_on` sanitizes trigger and statement, so `render_always_on`, `render_always_on_section` and `always_on_rule_ids` see the same text; a rule left empty is dropped from the block and the recorded ids alike |
| `/always-on` (and the pre-write APPLICABLE RULES block built from it) | rows sanitized before `est_tokens` is computed |
| recall | `fence("RECALL", cards, ...)`; `clamp_fenced` in the route |
| pre-write decision card | line form: title, rationale, reason and commit subject sanitized before clipping |
| precompact, postcompact, verify-before-claim, dispatch discipline | no retrieved text, no change |

The always-on and APPLICABLE RULES blocks keep their `===` markers: converting them would
change bytes pinned by the prompt-bundle golden diff for no gain, since the sanitizer
already treats those markers as boundaries.

The decision card stays one line: `clip` collapses all whitespace, U+2028 included, so no
field can start a line, and two marker lines would cost about 50 of its 1,000 characters.

### The one header change: recall

Recall opened with `[Writ recall: recent decisions on this project (rule-grounded,
read-back from decision memory)]` and had no end. It is now
`--- WRIT RECALL (recent decisions on this project, rule-grounded, read back from decision memory) ---`,
the cards unchanged, then `--- END WRIT RECALL ---`. `compile_recall` starts its budget at
the cost of the empty frame, so both marker lines are paid for. The route clamps with
`clamp_fenced`, which keeps the close line and puts the truncation marker inside the block.
Each card's head is built from sanitized fields before clipping, so the head is exactly the
line the block carries and shown-marking (`head in recall_block`) still matches;
sanitizing only at fence time would have re-shown such a card every prompt.

### Raw similarity

The ranked header shows ` sim=0.734` (the vector-stage cosine) or ` sim=n/a` (keyword-only)
after the score. See `docs/adr/ADR-ranked-header-fields.md` for the spellings and the carry.

### What an absent block means

| ranked hook output | meaning |
|---|---|
| a WRIT RULES block | rules matched; a LOW_SCORES line after it means all are weak |
| the NO_RULES line, no block | retrieval ran and nothing matched: the abstention gate fired, the filter left nothing, or every match was already shown this epoch |
| `[Writ: server unavailable ...]` or `[Writ: query failed ...]` | the daemon is down or errored |
| nothing at all | the turn was skipped (budget, context pressure, a short prompt) or the hook could not run |

The NO_RULES nudge is the statement, reused with no second message: it keeps its opening
sentence and adds "No WRIT RULES block follows: no rule matched this prompt beyond those
already shown." `templates/CLAUDE.md` says the same and no longer claims that a missing
block means the server is down. The rare case where rules matched but not even the
one-rule summary fits prints nothing; covering it needs a reserve near the whole ceiling,
so it is recorded here, not changed.

## Accounting

Every ceiling measures rendered text, so escapes, removals, the sim slot and the longer
nudge are counted where they land: `fit_ranked` against `ranked_char_limit` (which pays for
the nudge), `fit_methodology` against its body limit, `render_always_on_section` with the
sanitized fields, recall through the frame cost and `clamp_fenced`, the decision card
through its final clip.

## Alternatives rejected

- **Escape every mention, inline ones too.** Corrupts a shipped trigger and the inline
  escape hatch, and an inline mention cannot close a block.
- **Random per-turn boundary tokens.** Stronger in theory, but every pinned header, the
  golden diff and `split_format` would change, and the model gains nothing a fixed frame
  with escaped content does not give it.
- **Strip marker lines instead of escaping them.** Silently changes what a rule says; the
  backslash keeps the text readable and visibly not a boundary.
- **Sanitize at ingest.** Text arrives through ingest, propose, edit, records and (later)
  documents; sanitizing at render covers every path once, and the stored text stays what
  its author wrote.

## Consequences

- With the shipped corpus every channel prints the same bytes as before except the sim slot
  and the recall frame; a real-graph test checks every retrieved field of every Rule,
  methodology node and Abstraction against the sanitizer.
- Workstream D calls `fence("DOCUMENTS", ...)`, `sanitize_retrieved`, `similarity_slot` and
  `clamp_fenced`; none may be re-implemented.
- Text that never reaches the session's context (PR comments, the offline analyzer prompt,
  the `--add-rule-objects` cache) is out of scope.
