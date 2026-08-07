# Ranking levers: measured decision, 2026-08-06

Two ranking features shipped implemented but permanently inert: authority-preference
re-ranking (threshold hardcoded to 0.0, never configured) and the bundle-cohesion
ranking weight (`w_bundle_cohesion` defaulted to 0, stage skipped). An external review
called both out as wire-or-delete. This is the measurement that decided each one.

## Method

Read-only, offline, against the live graph. Indexes are built ONCE via `build_pipeline`
and every arm reuses the same BM25 / vector / adjacency / model / metadata /
abstractions / node_routes, so only the ranking parameters vary. Same methodology as
`scripts/sweep_ranking.py`.

- Gold set: `tests/fixtures/ground_truth_queries.json`, 193 queries (47 ambiguous).
- Metrics: MRR@5 over the ambiguous subset, hit-rate@5 over all queries, nDCG@10 over
  all queries.
- Significance: two-sided exact paired sign test on per-query RR@5 against the shipped
  baseline. `n_pos` = queries the arm wins, `n_neg` = queries it loses, ties dropped.
- When sweeping a weight, the other five are scaled by `(1 - w) / base_sum` so
  `RankingWeights.validate()` still sums to 1.0. A weight sweep is therefore never a
  pure addition; it always trades against the existing weights.

Corpus state at measurement time: 287 rules, 32 mandatory, 62 abstractions, 16 skills,
0 category orphans.

### Measurement noise, and why the sign test is the load-bearing evidence

Discovered while verifying this cycle: **the benchmark is not reproducible run to run.**
Five consecutive `benchmarks/bench_targets.py` runs, with unchanged code and an unchanged
graph, returned:

| run | hit@5 (of 193) | MRR@5 | nDCG@10 |
|---|---|---|---|
| 1 | 156 | 0.6064 | 0.7331 |
| 2 | 158 | 0.6167 | 0.7343 |
| 3 | 157 | 0.6124 | 0.7336 |
| 4 | 159 | 0.6238 | 0.7338 |
| 5 | 157 | 0.6135 | 0.7337 |

Spread: 3 queries on hit@5, 0.0174 on MRR@5, 0.0012 on nDCG@10.

The cause is Python hash randomization. Pinning `PYTHONHASHSEED=0` makes consecutive runs
byte-identical (157/193, MRR@5 0.6106, twice). Dict and set iteration order feeds
candidate ordering, so ties among equal-scoring candidates resolve differently per
process. nDCG@10 is far more stable than hit@5 and MRR@5 because it is not decided by
which side of a rank-5 boundary a tied candidate lands on.

Two consequences for reading everything below:

1. **Absolute values carry roughly +/- 2 queries of seed noise.** Any published metric
   should be read with that band, including the floors (a 90% floor against 92-93%
   measured is a thinner margin than the single number suggests).
2. **The arm-to-arm comparisons in this document do NOT carry that noise.** Every arm ran
   inside ONE process against ONE index build, so all arms shared a single hash seed;
   only the ranking parameters varied. That is why the paired sign test, not the absolute
   metric delta, is the evidence relied on here.

Fixing the harness reproducibility is a separate piece of work, tracked as a finding out
of this cycle.

## Result 1: authority preference has nothing to act on

| authority_preference_threshold | MRR@5 | hit@5 | nDCG@10 | n_pos | n_neg | p |
|---|---|---|---|---|---|---|
| 0.00 (shipped) | 0.6135 | 0.8135 | 0.7342 | 0 | 0 | 1.0000 |
| 0.02 | 0.6135 | 0.8135 | 0.7342 | 0 | 0 | 1.0000 |
| 0.05 | 0.6135 | 0.8135 | 0.7342 | 0 | 0 | 1.0000 |
| 0.10 | 0.6135 | 0.8135 | 0.7342 | 0 | 0 | 1.0000 |

Byte-identical at every threshold, and the reason is structural rather than statistical.
The graph holds exactly one `ai-provisional` node, `SKL-PROC-WRIT-DIAGNOSIS-001`, a
Skill. Its category (`Process and workflow`) carries routes `['state', 'action', 'pull']`
with no `semantic`, so the default route filter never admits it. Measured directly:
across all 193 gold queries, zero results contained an ai-provisional node and zero mixed
authority values.

The feature is reachable, just not from the default filter. Both `retrieval_mode="literal"`
and an explicit `node_types=['Skill']` admit the node (verified: each returns it), and
`/query` accepts `prefer_rule_ids`, `retrieval_mode` and `node_types` on the same request.

**Decision: wire it, default OFF.** The premise is live and growing (the propose,
graduate, promote path produces ai-provisional nodes), so this is not dead code. It now
has a real config path, `[retrieval] authority_preference_threshold`, read by
`writ/config.py get_authority_preference_threshold` and passed into `build_pipeline` at
daemon startup. The shipped default stays 0.0, because a measurement showing no change is
not a reason to alter ranking.

## Result 2: bundle cohesion has no do-no-harm setting

| w_bundle_cohesion | MRR@5 | hit@5 | nDCG@10 | n_pos | n_neg | p |
|---|---|---|---|---|---|---|
| 0.00 (shipped) | 0.6135 | 0.8135 | 0.7342 | 0 | 0 | 1.0000 |
| 0.01 | 0.6135 | 0.8135 | 0.7328 | 1 | 3 | 0.6250 |
| 0.02 | 0.6135 | 0.8135 | 0.7328 | 1 | 3 | 0.6250 |
| 0.03 | 0.6135 | 0.8135 | 0.7328 | 1 | 3 | 0.6250 |
| 0.05 | 0.6195 | 0.8187 | 0.7337 | 4 | 4 | 1.0000 |
| 0.08 | 0.6195 | 0.8187 | 0.7336 | 4 | 4 | 1.0000 |
| 0.10 | 0.6167 | 0.8187 | 0.7327 | 4 | 7 | 0.5488 |

No arm separates from the baseline. Every nonzero value lowers nDCG@10, and the two best
arms raise MRR@5 by 0.0060 and hit@5 by 0.0052 while winning four queries and losing four
at p = 1.0000. Read against the noise band above, those deltas are small: +0.0052 hit@5 is
one query, against a harness whose own run-to-run spread is three. The sign test is what
settles it, and it says the per-query ranking is indistinguishable from baseline. No
setting improves all three metrics, so the do-no-harm rule used for `w_graph` selects
nothing.

There was signal available to weight, so this is not a measurement of an empty feature:
183 of 193 queries have at least one candidate whose 1-hop neighbours overlap the
candidate set, over 6,287 neighbour edges walked. The weight simply did not convert that
signal into better ranking.

**Decision: delete it.** Two reasons beyond the numbers.

Its design premise is gone. The code comment said the zero default held "until
methodology nodes are folded into the production ranking (Phase 2)". That never happened
and is not planned: the methodology channel shipped deterministic instead, as a trigger
index with no embeddings and no ranking weights (`/methodology-companion`). The only path
bundle cohesion could ever reach is the coding-rule semantic ranking measured above.

The implementation also carried an inverse-hub bias. Cohesion was
`overlap / len(neighbors)`, so a node with one neighbour in the anchor set scored 1.0
while a hub with 40 neighbours and 3 in the anchor set scored 0.075, and the anchor set
was only `FIRST_PASS_TOP_N = 3`. Reviving the idea should start from a different
normalization, not from this code.

Git history keeps it recoverable if the methodology-ranking premise ever returns.

## Pass-ordering defect found while testing this

Wiring the authority threshold exposed a latent ordering bug rather than creating one.
`_apply_sticky_tiebreak` groups results by treating the first element of each run as the
group maximum, which is valid only on descending-sorted input, and a comment in
`pipeline.py` recorded that this held only because the authority pass ahead of it never
reordered anything.

With the threshold enabled, the authority pass leaves the list non-descending and the
tiebreak then builds wrong groups. The consequence is not cosmetic: `apply_context_budget`
trims by taking the first N in list order, so grouping decides which rules get injected.

Comparing the two pass orders over an exhaustive 12,960-configuration search (five rules,
score gaps drawn from {0.005, 0.015, 0.03}, all 32 authority assignments, each rule
preferred in turn, threshold 0.05), **4,923 configurations produce different output**.
The shape that matters:

```
scores      [1.0, 0.995, 0.99, 0.985, 0.98]   only the last rule is ai-provisional
prefer      the ai-provisional rule
old order   ['R4', 'R0', 'R1', 'R2', 'R3']    <- ai-provisional promoted to position 0
new order   ['R0', 'R1', 'R2', 'R3', 'R4']    <- preference holds
```

With the authority pass first it had nothing to do, since the ai-provisional rule was
already last; the sticky tiebreak then promoted that rule above four human rules, exactly
inverting the preference that was switched on. Applying the hard preference LAST both
restores the tiebreak's sorted-input precondition and gives the preference the final word.
Pinned by `tests/test_ranking_levers.py::TestPassComposition`.

## Reproducing

```
PYTHONHASHSEED=0 .venv/bin/python scripts/sweep_ranking.py
```

Pin the seed. Without it the absolute numbers move by up to three queries between runs
for the reason given under Method, and a re-run will not match this document.

Sweeps `w_graph` and `authority_preference_threshold` with the paired sign test. The
`w_bundle_cohesion` arm is not reproducible from the current tree: the weight it varied
has been deleted, and the table above is the record of that arm.
