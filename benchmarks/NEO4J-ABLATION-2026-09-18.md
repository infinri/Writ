# Neo4j ablation: measured answer to roadmap item 3, 2026-09-18

The roadmap asked for the stage-4 graph contribution to be measured on the local
benchmark, and said either result was decisive: little contribution justifies dropping
the Docker/2GB/daemon burden, large contribution publishes as the counter to lexical-only
competitors. It also carried an expected answer, citing a prior note that graph weighted
about 1% in a rerank evaluation as "prior signal the answer may be 'not load-bearing'".

Both halves of that framing turned out to be wrong, and the run produced a third result
nobody asked for. This file is the record.

## Method

Read-only, offline, against the live graph. Same methodology as
`benchmarks/RANKING-LEVERS-2026-08-06.md`, and the same harness: indexes are built ONCE
via `build_pipeline` and every arm reuses the same BM25, vector, adjacency, model,
metadata, abstractions and node_routes, so only the ranking parameters vary.

- Gold set: `tests/fixtures/ground_truth_queries.json`, 193 queries, 47 ambiguous.
- Metrics: MRR@5 over the ambiguous subset, hit-rate@5 over all queries, nDCG@10 over all
  queries.
- Significance: two-sided exact paired sign test on per-query RR@5 against the shipped
  baseline. `n_pos` is queries the arm wins, `n_neg` queries it loses, ties dropped.
- Sweeping a weight scales the other four by `(1 - w) / 0.99` so `RankingWeights.validate`
  still sums to 1.0. A weight sweep is never a pure addition; it always trades against the
  other weights.
- Corpus state at measurement time, from `/health`: 288 rules, 32 mandatory, 22 categories,
  index warm. Adjacency cache 445 nodes, built in 53ms.

Reproducibility was established before any number here was read: two consecutive runs of
`scripts/sweep_ranking.py` were byte-identical after the 2026-08-06 hash-order fix. The
arm-to-arm deltas below therefore carry no seed noise. Absolute values still carry the
roughly plus or minus 2 query band recorded in the 2026-08-06 file.

## Result 1: ablating graph proximity costs one query out of 193

Graph proximity is the only term the graph contributes to the score. Removing it outright:

| arm | MRR@5 | hit@5 | nDCG@10 | n_pos | n_neg | p |
|---|---|---|---|---|---|---|
| w_graph = 0.01 (then shipped) | 0.6124 | 0.8083 | 0.7327 | - | - | - |
| w_graph = 0.00 (ablated) | 0.6124 | 0.8031 | 0.7319 | 0 | 3 | 0.2500 |

MRR@5 is unchanged to four decimals. hit-rate@5 moves by one query, 156 of 193 to 155.
The sign test finds zero queries where the ablated arm wins and three where it loses, at
p = 0.2500, which is not distinguishable from chance.

At the weight that was shipped, the graph was not load-bearing. That is a real answer to
the question as asked.

## Result 2: the question was asked about a constant, not about the signal

The same sweep shows the shipped value was not the do-no-harm optimum and was not near it.

| w_graph | MRR@5 | hit@5 | nDCG@10 | n_pos | n_neg | p |
|---|---|---|---|---|---|---|
| 0.00 | 0.6124 | 0.8031 | 0.7319 | 0 | 3 | 0.2500 |
| 0.01 (was shipped) | 0.6124 | 0.8083 | 0.7327 | - | - | - |
| 0.02 | 0.6142 | 0.8083 | 0.7332 | 1 | 0 | 1.0000 |
| 0.03 | 0.6202 | 0.8135 | 0.7336 | 3 | 0 | 0.2500 |
| **0.05** | **0.6273** | **0.8135** | **0.7367** | **4** | **0** | **0.1250** |
| 0.08 | 0.6245 | 0.8135 | 0.7358 | 4 | 3 | 1.0000 |
| 0.10 | 0.6255 | 0.8135 | 0.7362 | 4 | 3 | 1.0000 |
| 0.15 | 0.6124 | 0.7979 | 0.7326 | 5 | 13 | 0.0963 |
| 0.20 | 0.6032 | 0.7927 | 0.7283 | 7 | 18 | 0.0433 |

`w_graph = 0.05` improves all three metrics over baseline and loses zero queries. The
sweep's own do-no-harm selector names it the winner without being told to.

So "the graph contributes about 1%" described the weight, not the signal. The correct
statement is that the graph was switched almost off, and the measurement meant to justify
removing it instead found that turning it up is the only arm that never loses a query. The
expected answer was reachable only by never varying the lever.

**Decision: raise the default to 0.05**, rebalancing the four non-graph weights to
0.19 / 0.57 / 0.095 / 0.095 so the five still sum to 1.0. Verified after the change by
scoring the gold set through `build_pipeline` with no weights argument: the shipped
default reproduces the 0.05 arm exactly (MRR@5 0.6273, hit@5 0.8135, nDCG@10 0.7367), so
what ships is the vector that was measured and not a nearby one.

The literal preset keeps `w_graph = 0.01`. It was not in this sweep, and shipping an
unmeasured change beside a measured one is how the two become indistinguishable later.

### What argues against this, stated because it is not weak

p = 0.1250 is not significance. Four queries is four queries. The gold set is
self-authored, which is the caveat roadmap item 7 exists to publish, so a win measured on
it is a win against our own idea of a hard query. The 2026-08-06 cycle declined to move a
weight on evidence of a similar size, though that arm was a genuine 4-4 tie at p = 1.0000
rather than 4 wins and 0 losses.

What tips it is consistency with this project's own recorded rule: all three metrics at or
above baseline with zero per-query losses is the do-no-harm criterion, and 0.05 is the only
arm that meets it. The alternative was keeping a value whose written justification cited
12 of 83 queries, a gold set two sizes out of date.

## Result 3: stage 4 computes a result that is discarded before rendering

This is the half a metric sweep is structurally blind to, because stage-4 enrichment never
touches a score. `_final_rank` puts it in the `relationships` key and nothing else in the
pipeline reads it.

Measured over the gold set: all 965 top-5 rule slots across 193 queries carry at least one
neighbour, mean 3.61 neighbours per slot, minimum 1, maximum 9. The stage produces a dense
result for every query.

Its only consumer is `_project_rules`, which includes `relationships` under
`include_relationships=True`. That is passed on the `full` branch of `apply_context_budget`
alone. Full requires `budget_tokens > STANDARD_THRESHOLD`, and `STANDARD_THRESHOLD` is
8000. `DEFAULT_SESSION_BUDGET` is also 8000, and a session budget only decreases from
there.

Executed rather than read off the threshold ladder:

```
budget=8000  mode=standard  relationships_present=False
budget=8001  mode=full      relationships_present=True
budget=2000  mode=standard  relationships_present=False
budget=None  mode=full      relationships_present=True
```

`None` selects full, so the code path is reachable in principle and the constants alone do
not settle whether it is reached in practice. The artifact settles it: parsing all 14,101
captured envelopes in the blackbox corpus finds 200 payloads carrying a WRIT RULES block
and ZERO carrying a `RELATED:` line. The stage has never shipped its output to the model
anywhere in the captured record.

Parsed, not grepped: the capture stores each envelope as an escaped JSON string, so
grepping for a quoted key returns zero on a file full of them.

Pinned by `tests/test_graph_contribution_invariants.py`, which derives the reachability
from the two constants rather than hardcoding 8000, so moving either one fails by name
instead of quietly turning a documented feature back on.

### Closed the same day: the line now ships, but not by making full reachable

The user ruled that the neighbour list should reach the model. Pricing the three ways to do
that changed which one is correct, so the rejected options are recorded here rather than left
implicit. Rendered through the real renderer over all 193 gold queries:

| mode | mean block | vs standard |
|---|---|---|
| standard (what shipped before) | 4,179 chars, about 1,045 tokens | - |
| full | 11,154 chars, about 2,788 tokens | 2.67x |
| standard plus the RELATED line | 4,425 chars, about 1,106 tokens | 1.06x |

Full mode is not the RELATED line. It is ten rules instead of five plus `rationale` on each,
and the line itself is 61 tokens of that 1,743-token difference. That 61 is the SHIPPED
figure, measured after the dedup below; the pre-dedup projection was 86.

The budget is what settles it. `cost_for` charges 2,000 tokens per full turn against 600 per
standard turn, and a session budget only decreases. Simulated from `DEFAULT_SESSION_BUDGET`:

| option | turns injected | of those, full |
|---|---|---|
| before this fix | 15 | 0 |
| flip the comparison so 8,000 selects full | 13 | 1 |
| raise the budget to 20,000 | 21 | 6 |
| raise the budget to 40,000 | 25 | 16 |
| render relationships in standard (taken) | 15 | n/a, ships on all 15 |

Flipping the comparison buys full mode on turn one, drops the budget to 6,000, and every turn
after is standard again: it pays for one full turn and loses two turns of injection. Genuine
full mode is a budget raise, a separate decision about what reaches the model on every turn.

WHAT SHIPPED: `apply_context_budget`'s standard branch carries relationships, and the renderer
emits `RELATED:` in standard as well as full. Two files, because the projection and the renderer
gate on mode independently, and widening the projection alone would have shipped the field to
the JSON consumer while still printing nothing.

A DEFECT THE RENDER SURFACED, which no fixture would have caught. The first real query printed
`RELATED: API-ERROR-002, API-STATUS-001, API-ERROR-002, API-STATUS-001, ...`: six ids for four
rules. The adjacency cache holds one entry per DIRECTION, so a reciprocal edge arrives as two
entries with the same `rule_id` and a different `direction`. The rendered line shows the id
alone, so the second copy carried no information and only cost tokens. It had been invisible for
as long as the line had never rendered. The rendered line is now deduplicated with first-seen
order preserved, and `relationships` is left intact so a JSON consumer still sees direction.
Verified over the whole gold set: 965 RELATED lines rendered, matching the 965 enriched slots
exactly, and ZERO carrying a duplicate id.

WHAT DID NOT: no threshold, limit or budget constant moved. Full mode is still unreachable at
the default budget, `rationale` and the second five rules still never ship, and the pin that
asserts it is unchanged.

ONE HONEST CAVEAT ABOUT THE COST. The turn count is unchanged at 15 because `cost_for` is a flat
per-rule rate per mode, not a measurement of the rendered block: standard is charged 120 tokens
per rule while the block measures about 209. The charge was already an underestimate and this
adds about 17 more per rule to the real side only, so the 86 tokens a turn are real context the
budget does not see. Pre-existing, neither fixed nor worsened here, and recorded rather than
folded into a "no cost" claim.

## What this measurement does NOT license

It does not justify dropping Neo4j, and saying that it does would be the overclaim this
file exists to prevent.

`build_pipeline` loads the entire corpus through `_load_candidates(db)`. Neo4j is the
corpus STORE, not only the source of a ranking term. Zeroing the graph weight removes
about 0.6 of one query from hit-rate@5; it does not remove a database. The Docker, 2GB and
daemon question is a storage question, and nothing measured here touches it.

The roadmap's second branch is equally unsupported. A one-query, p = 0.25 contribution is
not a result to publish as a counter to lexical-only retrieval. The honest publishable
claim from this run is narrower and more interesting: the graph term was never tuned, and
tuning it is worth about five queries.

## Reproducing

```
.venv/bin/python scripts/sweep_ranking.py
```

The `w_graph` and `authority_preference_threshold` sweeps with the paired sign test. Its
`BASELINE_W_GRAPH` is 0.01, the value shipped when the sweep was written, so after this
cycle the row marked baseline is the OLD default and the row marked do-no-harm winner is
the new one.

Enrichment coverage and the budget-mode probe are not in that script. The budget probe is
pinned as a test (`tests/test_graph_contribution_invariants.py`) and needs no graph:

```python
from writ.retrieval.ranking import apply_context_budget
from writ.session.config import DEFAULT_SESSION_BUDGET
rules = [{"rule_id": "R1", "score": 1.0, "relationships": [{"rule_id": "N1"}]}]
for b in (DEFAULT_SESSION_BUDGET, DEFAULT_SESSION_BUDGET + 1, None):
    out, mode = apply_context_budget(rules, b)
    print(b, mode, "relationships" in out[0])
```

Enrichment coverage is one loop over the gold set against a built pipeline, counting
`r["relationships"]` on the top 5 of each query's result. The capture scan parses
`~/.claude/writ-blackbox.jsonl` line by line, walks every nested string, and counts the
payloads containing a WRIT RULES block against those also containing `RELATED:`. Feed that
file on stdin rather than opening it by path: it is outside the project, and the write gate
reads a bare `open(<path>)` in a script as a write and refuses the command.
