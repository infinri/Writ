# M0 V1 baseline audit summary

**Generated** by `python -m writ_m0.audit generate` (run from `m0/`). Do not edit by hand;
`python -m writ_m0.audit check` fails when this file drifts from the generators.

**Baseline commit:** `ebdafe5450b201339f81e717ed104e6bcfd3fe3e` (committed 2026-11-03T13:49:44-06:00).
All inputs are read from Git objects at that commit, not from the working tree.

## Evidence status

Every figure below is a static, reproducible observation of the baseline. None is a
runtime observation of a live Claude Code host. Host lifecycle semantics are taken from
the documented hook contract and still need isolated host tests (M0-01e). Rule
dispositions are an automated first pass awaiting semantic review (M0-01d).

## Tree

- Tracked files: 1139
- Hook scripts checksummed: 48

## Hook registrations ([CSV](hook-registrations.csv))

- Registrations: 52 across 12 lifecycle events
- Distinct scripts registered: 47

| Coverage class | Count |
|---|---|
| context_injection | 9 |
| observability_or_state | 15 |
| post_effect_advisory | 14 |
| preventive_candidate | 13 |
| workflow_gate | 1 |

| Mechanical finding | Registrations |
|---|---|
| LOGS_DENY_WITHOUT_HOST_BLOCK | 5 |
| MINTS_AUTHORITY_FROM_PROMPT_TEXT | 2 |
| PARTIAL_CAPABILITY_MATCH | 14 |
| POST_EFFECT_REFUSAL_CANNOT_PREVENT | 4 |
| SCRIPT_REGISTERED_MULTIPLE_TIMES | 7 |

Pre-effect registrations that consult the daemon default to allow when it is unreachable
(unless `WRIT_STRICT=1`): H27, H28, H29, H32, H36.

## Rule corpus ([manifest](v1-corpus-manifest.json), [dispositions](rule-disposition.csv))

- Nodes: 468; edges: 1268; dangling edges: 0
- Rules: 288 (mandatory 32, with mechanical_enforcement_path 33)

| Disposition (first pass) | Rules |
|---|---|
| advisory | 251 |
| candidate_enforced | 23 |
| obsolete | 11 |
| unverified | 3 |

- Near-duplicate statement pairs (word Jaccard >= 0.6): 0 ([CSV](rule-near-duplicates.csv))
- Conflict analysis: not performed (requires semantic review).

## V1 tests and benchmarks ([CSV](v1-test-inventory.csv))

- Python test/benchmark files: 602; test functions: 11008

| Topic tag (file name match) | Files |
|---|---|
| approval | 24 |
| bash_gate | 16 |
| concurrency | 7 |
| fail_open | 3 |
| hooks | 76 |
| isolation | 22 |
| path_safety | 9 |
| retrieval | 30 |

## Retrieval golden sets ([JSON](v1-golden-retrieval.json))

| Expectation | Cases |
|---|---|
| abstain | 20 |
| document | 12 |
| rank | 233 |

- Ranked cases whose expected ids are absent from the corpus: 0
- Document cases target V1 docs removed on this branch; they need the baseline docs corpus.
