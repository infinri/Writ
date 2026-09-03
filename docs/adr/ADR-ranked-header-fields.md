# ADR: what the ranked rule header renders (severity always, authority as an exception, domain dropped)

Status: accepted
Date: 2026-09-03
Plan: `.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md`
Touches: `writ/retrieval/ranking.py`, `writ/session/budget_tracking.py`,
`writ/retrieval/pipeline.py`
Sibling: `docs/adr/ADR-alwayson-ranked-field-dedup.md`, which decides what the ranked
render OMITS for a rule already injected on the always-on channel. This one decides what
the header line itself carries.

## The slot was already billed, and it was returning nothing

Every ranked rule injected into a session rendered:

    [ERR-RETRY-002] (?, ?, ?) score=0.921

while methodology nodes on the same turn rendered `(medium, human, process)`. The
parenthesized slot is unconditional in the formatter, so the tokens were already being
spent. They bought three question marks.

This was not a missing feature. `writ/retrieval/pipeline.py:_final_rank` set `authority`,
`severity` and `domain` on every scored rule entry, and its comment named this exact
symptom. `writ/retrieval/ranking.py:_project_rules` then rebuilt a NEW dict holding
`rule_id`, `node_type`, `score` plus only the fields named in the per-mode `str_fields`
whitelist, and none of the three header fields is in any mode's list. `POST /query`
returned the header fields ABSENT rather than empty, which is what identified the
projection rather than a failed metadata lookup as the cause. The formatter was innocent:
it defaults each missing field to `"?"` and printed exactly what it received.

The fix was originally written at the wrong boundary. `_final_rank` is a PRODUCER and
`_project_rules` is a downstream WHITELIST in a different module, so every field the
producer adds has to be added twice with nothing linking the two sites. That is why the
code read as already fixed.

## Decision

Rendered shape, per rule line:

- `severity` ALWAYS: `[SEC-INJ-SQL-002] (critical) score=0.921`
- `authority` ONLY when it is not `human`: `[X-001] (critical, ai-provisional) score=0.921`
- `domain` DROPPED from the rule header entirely, and removed from the producer's entry
  because after the drop nothing reads that key

Absence still renders `?`, so an entry that reaches the formatter with neither field
renders `(?, ?)` rather than `()` or `(, )`. That is deliberate: a future projection that
strips the fields again prints its own failure into the agent's context instead of
failing silently, which is the only reason this defect was ever noticed.

The carry lives in the BASE dict of `_project_rules`, via a module-level `_HEADER_FIELDS`
tuple and a `_carry_header_fields(entry, rule)` helper, not in the per-mode `str_fields`
lists. `str_fields` entries default to `""`, which would render an empty slot and would
permanently shadow the formatter's `"?"`, because the key would always be present. The
copy is CONDITIONAL (`if f in rule`), never a defaulted `get`: the projection's job is to
select fields, not to invent values, and exactly one module (the formatter) decides what
absence looks like. A second default in `ranking.py` is how the fields went missing.

The helper exists because `ranking.py` has TWO stripping sites, not one:
`_summary_with_abstractions` builds its own ungrouped-rule fallback entry inline with the
same whitelist behavior. Leaving that one alone would make the header shape depend on
whether abstractions happen to exist in the graph, which nobody would predict from
reading either function.

Key order: the old `_project_rules` docstring called its key order "the serialized output
contract". It is not a contract, it is a stability convention. Every consumer reads by
key (the formatter, `extract_rule_objects`, its bash mirror in `bin/lib/common.sh`,
`measure_ranked_channel`, and FastAPI's encoder, which returns the dict verbatim), no
consumer indexes by position, and no test compares serialized bytes. The new keys go in a
FIXED place, immediately after `score`, so diffs stay readable; the docstring now says
convention rather than contract.

## The corpus measurement, which is the part worth re-checking

Measured on `writ-corpus.cypher`, the corpus dump in this repo, re-measured at
implementation time and matching the plan exactly:

| field | distribution |
| --- | --- |
| `severity` | 58 `critical`, 144 `high`, 135 `medium`, 12 `low`, across 349 valued nodes |
| `authority` | 406 present, 405 `human`, 1 `ai-provisional` |
| `domain` | 406 present, of which 109 `process` |

`severity` VARIES, and it is the field that changes what the agent should do, because it
says how hard the rule binds. It earns the slot.

`authority` is effectively CONSTANT, and structurally so rather than incidentally:
`writ/graph/ingest.py:169` ASSIGNS `result["authority"] = "human"` for every rule parsed
from a RULE-START block (an assignment, not a `setdefault`), and the front-matter path
defaults the same way. The only other value comes from the rule-proposal path in
`writ/graph/db/rule_store.py`, which has never run. Rendering authority on every line
would pay tokens 405 times out of 406 to print a constant, and worse, it would train the
reader to skip the one slot where a value that matters would appear.

`domain` is present everywhere and is largely redundant with the rule id on the same line:
`API-BREAKING-001`, `API-CONTRACT-001` and `API-ERROR-001` all carry `domain: 'api-design'`,
and all 109 `process` nodes have methodology ids (`TEC-PROC-...`, `PBK-PROC-...`). That
correspondence is a naming CONVENTION, not an enforced identity, so the honest claim is
"largely redundant", not "provably identical". The tiebreaker is that domain is not
actionable: it does not tell the agent to behave differently, and when the caller passes a
detected domain, retrieval has already filtered on it, so every hit in the block shares
the same value.

## Pricing the alternatives

Per rule line, in characters:

| shape | rendered | chars | vs before |
| --- | --- | --- | --- |
| before | `(?, ?, ?)` | 9 | 0 |
| A. all three filled | `(critical, human, api-design)` | 29 | +20 |
| B. severity + authority always | `(critical, human)` | 17 | +8 |
| chosen | `(critical)` | 10 | +1 |
| chosen, provisional rule | `(critical, ai-provisional)` | 26 | +17 |

The chosen shape is token-neutral: `+1` for `critical`, `-1` for `medium`, `-3` for
`high`, `-4` for `low`. Filling the slot costs nothing on average, and the pathological
case is the 1-in-406 rule whose trust is actually in question.

### A. Render all three fields, rejected

+20 characters per line to print one varying field, one corpus-wide constant and one field
already implied by the id sitting next to it. This is the shape the original comment
promised, and the cost is why it was not simply restored.

### B. Render severity and authority unconditionally, rejected

+8 characters per line for a value that is `human` 405 times out of 406. Also the
readability cost above: a slot that always says the same thing stops being read.

### C. Exempt summary mode, rejected

Summary mode gets the fields, at no cost, because the chosen shape is CHEAPER than what
summary rendered before: `(?, ?, ?)` is 9 characters and `(high)` is 6. There is no budget
interaction either, since `cost_for` is a flat per-rule rate by mode and never looks at
content, so the header shape cannot perturb the remaining budget that selects the next
turn's mode. An exemption would add a fourth field list to keep in agreement and buy
nothing.

### D. Add `confidence` and `staleness_window`, out of scope

The queue entry that started this work named those two fields, and neither appears in the
slot the code actually renders. Adding a field to the header is a different decision from
filling a slot that is already billed, and `confidence` is 405-of-406
`production-validated` in the same dump, so it would repeat the authority mistake exactly.

## Consequences

- The `/methodology-companion` channel builds its own entries with all three fields and
  shares the formatter, so its lines shrink from `(medium, human, process)` to `(medium)`.
  That is the point of a shared render: both channels get the same shape, and the
  companion's hardcoded `"authority": "human"` correctly renders nothing.
- The `[ABSTRACT: <id>] (covers N rules, <domain>)` line is a separate branch with its own
  `domain` read and is unchanged. A capability guards that boundary, because applying the
  domain drop there would be the easy overreach.
- `extract_rule_objects` and its bash mirror now cache a real `severity` into
  `loaded_rules` instead of `""`. Nothing reads it, so no behavior changes. `domain` in
  that cache stays `""`, unchanged, because the projection still does not carry it.
- The anti-recurrence guard is a test that starts at node METADATA and ends at the
  RENDERED LINE, through `query()`, `tag_overlap()` and `cmd_format()`. A test asserting
  on `_final_rank`'s `rule_entry` would have passed green through this entire regression,
  so it is explicitly rejected as the oracle. The mode population that test iterates is
  DERIVED from `ranking.py` by an `ast` walk, so a fourth budget mode with its own field
  list fails by name instead of passing three-mode-shaped assertions.
- The mocked chain cannot prove that the shipped Rule nodes in Neo4j actually carry
  `severity`, nor that the daemon's JSON serialization preserves the keys end to end. That
  seam is declared unprovable by the test and is carried as an operational capability,
  verified by one live turn reading the injected header.

## The mixed case, and why it gets no capability

An entry carrying `severity` but not `authority` renders `(critical, ?)`, because the exception
branch tests `authority == "human"` and `"?"` is not `"human"`. That follows the stated rule and
keeps absence visible, which is the intended reading: do not "fix" it into a bare `(critical)`,
because that would hide a field the projection failed to carry.

IT IS UNREACHABLE ON EVERY SHIPPED PATH, verified rather than assumed. Only two channels feed
`cmd_format`: the ranked channel, whose entries come from `_final_rank` (which sets both fields with
defaults) through `_carry_header_fields` (which copies both from one dict, so it cannot split them),
and the methodology channel, which sets both. The two builders that set `severity` without
`authority` render elsewhere: the always-on summary bundle goes through `render_always_on`, and
`extract_rule_objects` feeds the compliance cache and is never rendered.

So the state is reachable only from a hand-built fixture. It gets no capability for the same reason
the sub-agent scope deferral was deleted in `e881c6d`: specifying a state the system cannot produce
makes it read as designed and argues against its own later change.
