# ADR: field-level dedup between the always-on and ranked channels

Status: accepted
Date: 2026-09-01
Plan: `.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md`
Touches: `writ/retrieval/prompt_bundle.py`, `writ/session/budget_tracking.py`,
`writ/server/routes/query.py`, `writ/analysis/injection_footprint.py`

## What this cycle saves: zero bytes, today

Stated first so it cannot be missed. **This change saves NO bytes on the current corpus
under the default retrieval configuration.** The overlap it acts on is empty on the
shipped path, measured rather than assumed, for the reason set out under
"How often this fires today" below. The mechanism is shipped LATENT: correct, tested, and
costing nothing while the overlap stays empty.

The 1,331-byte figure that appears throughout this document is what one occurrence WOULD
save, if the overlap ever became reachable. It is not a realized saving, it is not a
per-turn saving, and it must not be reported as one. Anybody citing a token reduction from
this cycle is citing a number that has never happened.

## Context

Two of the three per-prompt injection channels can deliver the same rule in the same turn.

The always-on channel (`render_always_on`) renders a rule's trigger and statement. The
ranked channel (`cmd_format`, standard mode) renders that rule's trigger, statement,
violation and pass_example. A rule that is both `always_on: true` and eligible for the
ranked pool therefore appears twice, and the hook emits the always-on block and the
ranked block a few hundred bytes apart in the same context window, so "already above"
is literally true rather than a figure of speech.

The size of that population is not a constant and is not asserted anywhere.
`writ injection-footprint` recomputes the intersection per run for exactly that reason:
the id list that circulated in review notes was already stale, and the "5 always-on rules
live in the ranked pool" figure in `writ/session/budget_tracking.py` describes
eligibility, not what any particular turn renders.

Measured with that instrument on 2026-09-01, work mode, corpus at 288 rules, probe prompt
`"output is clear precise and to the point lead with the answer BLUF plain language cut
bloat"`, the worst case is `ENF-COMMS-OUTPUT-001`: 1,476 bytes in the always-on block and
2,142 bytes in the ranked block. Of the ranked entry, 1,398 bytes are a byte-for-byte
repeat of what is already in context and 698 bytes are new (the violation and the
pass_example). The second delivery pays roughly three bytes for every one byte of new
content. Every figure in this paragraph is a snapshot of one run against one corpus, not
a constant; re-run the instrument rather than citing them.

### How often this fires today, measured rather than assumed

Zero times, on the shipped retrieval path, and that is worth stating plainly.

Only three `:Rule` nodes are both `always_on: true` and `mandatory: false`, so only three
can be in the ranked pool at all: `ENF-COMMS-OUTPUT-001`, `ENF-COMMS-001` and
`ENF-PROC-DEBUG-001`. All three belong to categories whose `routes` list omits
`semantic` (`CAT-COMM-001` routes `[always_on, action]`, `CAT-PROC-001` routes
`[state, action, pull]`), and the default semantic retrieval path applies a route filter
that admits only candidates whose category routes include `semantic`. `/prompt-bundle`
passes no `node_types`, so it never bypasses that filter, and no hook or route in the
repo passes one either.

Verified by running both: with the default filter the probe above returns
`CLEAN-FUNC-002, SEC-INJ-HEADER-001, SEC-VAL-ENCODING-001, CLEAN-COMMENT-001,
CLEAN-FORMAT-001` and the measured overlap is empty; with `node_types: ["Rule"]`, which
bypasses the route filter, the same probe returns `ENF-COMMS-OUTPUT-001` at rank 1 and the
measured overlap is `{ENF-COMMS-OUTPUT-001}`.

So this decision buys 1,331 bytes per occurrence and currently has no occurrences on the
default path. It is a correctness guard on a duplication that a single category `routes`
edit, a caller passing `node_types`, or a new always-on rule in a semantic-routed category
would make live tomorrow, and the instrument is what will say when that happens. It is
not, today, a measured token saving, and nothing in this repo should claim it is.

## An unresolved contradiction this finding exposes

The measurement above puts two things in the repo in direct conflict, and this ADR records
that rather than resolving it, because resolving it needs evidence neither the plan nor
this cycle gathered.

`writ/session/budget_tracking.py`, in `_upd_add_always_on_rules`, justifies keeping
always-on ids in a field separate from `loaded_rule_ids` on the grounds that writing them
into the exclude list "would silently stop them being retrieved by relevance". That
sentence is the load-bearing rationale for the whole separate-field design, and Option A
below is rejected largely by appeal to it.

**The route filter already stops exactly that.** All three rules the sentence is about are
in categories whose `routes` omit `semantic`, and `writ/retrieval/pipeline.py:286-287`
states that the default semantic filter admits a candidate only when its route list
contains `semantic`. So the relevance retrieval the comment is protecting does not happen
for those rules regardless of what the exclude list contains.

One of two things is therefore a defect, and this ADR does not pick between them:

- **The comment is stale.** It may predate the category route lists, or have been written
  while `_node_routes` was `None` and the pipeline was still on its pre-Phase-0 Rule-only
  fallback, in which case it describes a delivery path that no longer exists and should be
  rewritten. Note that the separate field could still be the right design for reasons the
  comment does not give, such as citation validation, so "stale rationale" would not by
  itself mean "wrong code".
- **The route configuration is wrong.** If those rules are supposed to be reachable by
  relevance, then omitting `semantic` from `CAT-COMM-001` and `CAT-PROC-001` is the defect,
  the comment is describing a correct intent, and the routing silently defeats it.

Evidence that would settle it, none of which was gathered here:

1. Dated history for both artifacts: `git log` on the two category nodes' `routes` value
   (via `writ-corpus.cypher`, since `bible/` is gitignored) against `git log` on the
   `_upd_add_always_on_rules` docstring. If the routes predate the comment, the comment was
   wrong when it was written; if the comment predates the routes, a routing change
   invalidated it silently, which is the more interesting failure.
2. An authoring record for `CAT-COMM-001` and `CAT-PROC-001` saying whether omitting
   `semantic` was a decision or a default. The category nodes' own `statement` fields only
   restate their route lists, so they cannot answer this.
3. A retrieval measurement over a real window: whether any ranked `/query` has ever
   returned `ENF-COMMS-OUTPUT-001`, `ENF-COMMS-001` or `ENF-PROC-DEBUG-001`, from the
   `retrieval_result` rows on the metrics stream. This is corroboration from a non-code
   artifact, and it distinguishes "blocked by a filter" from "blocked and also never
   wanted".

Not this cycle's to fix. Queued.


## Decision

Suppress the DUPLICATED FIELDS, not the duplicated entry.

For a ranked hit whose id is in this turn's rendered always-on set, `cmd_format` renders
the header line, a one-line pointer at the always-active block, `VIOLATION:` and
`CORRECT:`, and omits `WHEN:` and `RULE:`. Everything else about the entry is unchanged.

Three properties hold the shape together:

1. **The flag is additive.** `tag_overlap` returns copies with `already_injected: true`
   set and every other key carried through. It does not delete `trigger` or `statement`
   from the rule dict, because `extract_rule_objects(qresp)` caches those objects via
   `--add-rule-objects` and the compliance-matching path reads the statement back out of
   them. Stripping the fields would have rendered correctly and corrupted the cache, with
   the damage surfacing somewhere else entirely.
2. **The overlap comes from the RENDERED ids.** `always_on_rule_ids` applies the same
   renderable filter `render_always_on` does, so a rule dropped for a missing trigger or
   statement is absent from both. Building the overlap from the raw `/always-on` response
   instead would suppress a ranked rule's text AND point the reader at a block that never
   contained it. That failure raises no exception, drops no field, and shows no symptom;
   it is the citation bug inverted. `tests/test_ranked_alwayson_dedup.py` pins the call
   site structurally, by AST, because a substring grep for `always_on_rule_ids(` passes
   on the pre-existing citation-recording call.
3. **The overlap is per turn and per mode.** The process-domain strip in
   `writ/server/routes/query.py` removes non-mandatory `domain: process` always-on rules
   outside `{work, debug}`, so `ENF-PROC-DEBUG-001` is in the always-on block in work and
   debug and absent elsewhere. The suppression follows the block, because it is computed
   from the block.

## Alternatives rejected

### A. Add the always-on ids to the ranked exclude list

This is the larger saving and it is the one the code already argues against.
`writ/session/budget_tracking.py` (`_upd_add_always_on_rules`) keeps always-on ids in a
SEPARATE cache field from `loaded_rule_ids` precisely because `loaded_rule_ids` doubles
as the ranked query's exclude list, and it says in as many words that writing them there
"would silently stop them being retrieved by relevance". Overriding that rationale is
why this ADR exists.

What exclusion would buy: the whole ranked entry, 2,142 bytes for the worst case in the
run above, plus one of the five slots in the standard cap, which is the larger
second-order win.

What the reader loses: the violation and the pass_example never arrive at all. Those are
698 bytes of content the always-on channel does not carry in that same run, and for
`FRB-COMMS-001`
and `FRB-COMMS-002` the violation IS the operative content, the list of forbidden
phrases. Exclusion would be worst exactly where the rule matters most. The retrieval
feedback signal for those rules also stops, and because `loaded_rule_ids` is cumulative
and phase-partitioned, the exclusion lasts for the rest of the phase rather than the
turn.

Rejected.

### B. Drop a suppressed entry whose body is empty

A tagged rule with an empty violation and an empty pass_example renders as a header plus
a pointer: two short lines where a full entry would be an order of magnitude more.
Deleting those last two lines was considered and rejected: the
`--- WRIT RULES (N rules, ...) ---` header and
the `WRIT_META` rule ids both count entries, so dropping one would tell the session a
rule was delivered that the agent never saw. That is the same defect Option A was
rejected for, in miniature, for a marginal gain.

### C. Suppress in summary mode too

Summary mode renders trigger and statement and nothing else, so a suppressed entry there
would carry zero content. The choices were to drop the entry (defect B) or to leave the
mode alone. Summary mode is left alone: tagged and untagged renders are byte-identical
there, and `tests/test_ranked_alwayson_dedup.py` pins it. Summary mode engages only below
a 2,000-token budget, that is, late in a session, so the residual duplication is real,
priced and bounded rather than unknown.

## What this does not fix

**Field dedup does not recover the ranked slot.** The deduplicated rule still occupies
one of the five entries in the standard cap, so the ranked channel still returns four
fresh rules rather than five. Recovering the slot means requesting more rules, which ADDS
bytes, and this cycle's budget is bytes. Recorded here as a known limitation rather than
deferred silently.

The always-on channel's own size is untouched. So is summary mode. So is the orchestrator
branch of the rag-inject hook, which never calls `/prompt-bundle` and therefore never
computes an overlap.

## Consequences

The reader loses nothing that was not already on screen: the omitted text is verbatim
above and the pointer names both the rule and the block. Relevance retrieval, the
relevance score, the feedback counters, the recorded rule ids and the cached rule objects
are all preserved, which is what the exclusion-list rationale was protecting.

The per-prompt Neo4j round-trip count is unchanged. The two `/always-on` queries already
ran on every prompt; they are now resolved before the ranked channel RENDERS rather than
after, and the ranked channel's RETRIEVAL still runs first, so the channel-1-error early
return still skips them entirely. The added work is one dict copy over at most five
ranked rules.

No test asserts a byte count. A literal byte pin on live corpus text is a count pin that
breaks on the next content edit and teaches nothing when it does; the tests pin STRUCTURE
(which fields appear, for which rule, in which mode) and `writ injection-footprint`
reports MAGNITUDE on demand.

Unrelated to this decision but discovered while landing it, and recorded so the next person
does not think they broke it: `make docs-check` exits 1 on `docs/reference/http-api.md`, and
running `make docs` does not fix it cleanly, because the generator re-emits a double hyphen
from the `methodology_companion` docstring in `writ/server/routes/query.py` and the doc-style
ratchet then refuses the regenerated file, so the two guards are in direct conflict; the
drift is pre-existing, `docs/reference/cli.md` regenerates cleanly, and the fix (editing that
docstring) is queued separately.
