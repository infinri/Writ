# M1-02 fixture review package

**Status: PENDING INDEPENDENT REVIEW.** Prepared 2026-11-07 by Claude, who also wrote every expectation. No Rust or Go code exists. Expectations must not be changed to make a prototype pass; a disagreement is resolved by a recorded review decision.

## Files

| Path | Content |
|---|---|
| [`m1/fixtures/acceptance/SEMANTICS.md`](../../m1/fixtures/acceptance/SEMANTICS.md) | Pure `check-acceptance` semantics: record and current shapes, check order, refusal codes |
| [`m1/fixtures/acceptance/acceptance-binding.cases.json`](../../m1/fixtures/acceptance/acceptance-binding.cases.json) | 50 hand-reasoned cases (source) |
| [`m1/fixtures/transition/SCENARIOS.md`](../../m1/fixtures/transition/SCENARIOS.md) | Boundary coverage table, transition decision assumptions, `transition-scenario` semantics |
| [`m1/fixtures/transition/m1-transition.bundle.json`](../../m1/fixtures/transition/m1-transition.bundle.json) | Fixture policy for `workflow.transition` (validates against `policy-bundle.schema.json`) |
| [`m1/fixtures/transition/transition-decisions.cases.json`](../../m1/fixtures/transition/transition-decisions.cases.json) | 23 hand-reasoned kernel decisions (source) |
| [`m1/fixtures/transition/transition-scenarios.cases.json`](../../m1/fixtures/transition/transition-scenarios.cases.json) | 17 hand-reasoned promotion scenarios (source) |
| [`m1/fixtures/export_m0_fixtures.py`](../../m1/fixtures/export_m0_fixtures.py) | Builds the JSON Lines sets (patches, record digests); `--check` detects drift |
| [`m1/fixtures/manifest.json`](../../m1/fixtures/manifest.json), `m1/fixtures/sets/*.jsonl` | Driver inputs: 5 sets, 117 cases |

## Counts

| Set | Command | Cases | Categories |
|---|---|---|---|
| canonical-vector | `canonical` | 1 | D-006 known vector |
| golden-decisions | `decide` | 26 | M0 golden (provisional oracle, unchanged) |
| acceptance-binding | `check-acceptance` | 50 | valid 4, malformed 12, integrity 4, replay 1, scope 4, transition 2, artifact and snapshot 2, policy 3, revision 1, expiry 1, criteria trust, freshness and status 12, check order 4 |
| transition-decisions | `decide` | 23 | precedence and roles 6, composite 3, cross-project and cross-tenant 2, stale revision and policy pin or absence 4, identity 2, non-canonical paths 2, deadline 1, unknown capability 1, injected attributes 1, attribute and resource disagreement 1 |
| transition-scenarios | `transition-scenario` | 17 | commit 1, idempotency 3, record replay 1, artifact or snapshot substitution 2, stale state 2, policy change 1, kernel not allow 1, 16-way race 1, expiry 1, refusal side-effect freedom 1, malformed 1, cross-project 1, transition not evaluated 1 |

## Category to invariant map

| Category | Cases | Invariants (INV-32..INV-40 are PROPOSED, ADR-0008) |
|---|---|---|
| Kernel authorization of transitions | tx-01..06, tx-09, tx-14 | INV-03 (deny never becomes allow), INV-04, INV-32 |
| Composite sub-effects | tx-07, tx-08, tx-09 | RM-01, INV-23 |
| Isolation | scope-01..04, tx-10, tx-11, sc-16 | INV-10, INV-33 |
| Stale state and revisions | rev-01, tx-12, sc-06, sc-15 | INV-17, INV-36 |
| Policy version and downgrade | pol-01..03, tx-13, tx-15, sc-08 | INV-05, INV-12, INV-36, INV-37 |
| Replay and idempotency | rep-01, sc-02, sc-03, sc-04, sc-13 | INV-06, INV-07, INV-09 |
| Concurrent races | sc-10 | INV-07, INV-17 |
| Artifact and snapshot substitution | art-01, art-02, int-01, sc-05, sc-07 | INV-33, INV-40 (identity binding only) |
| Untrusted evaluation | crit-01..03, crit-10, crit-11, sc-12 | INV-25, INV-34, INV-35 (as a field only) |
| Incomplete, stale, expired | crit-04..09, exp-01, ord-04, sc-11 | INV-36, INV-38 |
| Malformed input | mal-01..12, sc-14, tx-23 | INV-18, INV-38 |
| Injection and alternate routes | mal-02, tx-17, tx-20, tx-21 | INV-02, INV-11, INV-13, INV-14 |
| Kernel decision required for promotion | sc-09 | INV-32 |

## Not covered by these fixtures

Pure fixtures cannot establish boundary 2 (authentic evaluators and record signatures) or boundary 4 (governed execution and artifact immutability). Trust levels and snapshot ids are plain fields here. Boundary 3 (transactional promotion) is specified, but only a prototype's own concurrency and crash tests (M1-05) can exercise real locking; the race scenario checks the aggregate outcome only.

## Expectations resting on assumptions, not established specifications

| Id | Assumption | Cases affected |
|---|---|---|
| A-1 | Target state encoded in the resource id (`workflow:<t>/<p>/transition/<to_state>/<wf>`) | all tx |
| A-2 | Composite transitions refused by a bundle forbid, not a kernel rule | tx-07..09 |
| A-3 | Kernel ignores the `to_state` attribute; broker must refuse attribute and resource disagreement (not yet in any scenario) | tx-22 |
| A-4 | The check order and refusal codes in SEMANTICS and SCENARIOS | all acceptance and scenario cases, especially ord-* |
| A-5 | Trust must match the class exactly (human trust does not satisfy a mechanical criterion) | crit-03 |
| A-6 | Expiry and freshness limits are exclusive; no clock-skew tolerance; `issued_at` after `now` is invalid | exp-01, crit-06, int-02, pos-03 |
| A-7 | A record whose status contradicts its criteria is refused rather than recomputed | int-03 |
| A-8 | No mandatory criterion means incomplete; waivers unsupported in M1 | crit-08, crit-09 |
| A-9 | Refusals store no receipt, so a refused idempotency key can be reused | sc-13 |
| A-10 | Compare-and-set precedes the acceptance check, so race losers report `REVISION_CONFLICT` | sc-10, sc-15 |
| A-11 | Artifact updates bump the workflow revision; policy activation does not | sc-05..08 |
| A-12 | One timestamp spelling (`YYYY-MM-DDTHH:MM:SSZ`) and lowercase hex digests | mal-06, mal-07 |

## Open semantics to decide before M1-03 and M1-04

1. **Duplicate JSON object keys** in any input: unspecified. Parsers differ (first wins, last wins, reject), which allows field smuggling. Recommendation: reject as malformed in every command; add cases once decided.
2. **Unicode normalization and case** of ids and states: byte-exact comparison assumed; not stated in the contracts.
3. **Attribute and resource agreement (A-3):** the broker check is unspecified; a scenario op carries `to_state` without a resource id.
4. **Binding the kernel permit to the promote operation:** scenarios take `decision_status` as an input; fingerprint binding of a transition permit (D-012 style) is not yet specified for transitions.
5. **Trusted time:** `now` is an input everywhere; clock trust is risk 14 (M3).
6. **Large integers** in `decide` requests (`expected_state_revision` above 2^53-1): handled by schema and D-006 in M0; worth an explicit case.

## Evidence so far

- Bundle validates against `policy-bundle.schema.json`; 22 of 23 transition requests validate against `action-request.schema.json` (tx-23 omits the principal on purpose).
- After the expectations were written, the M0 reference oracle was run through the driver as a consistency check: canonical 1/1, golden 26/26, transition-decisions 23/23. Agreement with the M0 oracle shows consistency with M0 semantics, not correctness (R-ORACLE). The M0 harness has no acceptance or scenario logic, so 67 cases have no implementation at all yet.
- Driver self-tests: 11 of 11 pass with all five sets in the manifest; exported sets current.
