# M1-02 fixture review package

**Status: PENDING INDEPENDENT REVIEW.** Revision 4 (2026-11-07), after owner decisions [D-017, D-018 and D-019](DECISION-LOG.md). Prepared by Claude, who also wrote every expectation. Expectations must not be changed to make a prototype pass; a disagreement is resolved by a recorded review decision. Earlier revisions: `4a66364`, `a97e8f1`, `226e4d2`. Change logs: [amendment 1](../../m1/fixtures/AMENDMENT-1.md), [amendment 2](../../m1/fixtures/AMENDMENT-2.md), [amendment 3](../../m1/fixtures/AMENDMENT-3.md).

## Revision 4 summary

| Set | Cases | Added in amendment 3 | Expected values changed in amendment 3 |
|---|---|---|---|
| canonical-vector | 1 | 0 | 0 |
| strict-json | 37 | 0 | 0 |
| golden-decisions | 26 | 0 | 0 |
| acceptance-binding | 97 | 0 | 0 |
| transition-decisions | 56 | 11 | 2 (`tx-06`, `tx-38`) |
| transition-scenarios | 66 | 17 | 1 (`sc-08`); 7 effect cases restart from a promoted workflow (inputs and final state only) |
| external-refusal | 56 | 20 | 0 |
| broker-scenarios (new) | 43 | 43 | n/a |
| **Total** | **382** | **91** | **3** |

Findings from revision 3: **N-1** resolved (kernel and broker both enforce protection from one pinned classification); **N-2** resolved (43 language-neutral broker cases, BROKER.md); **N-3** resolved (effect prerequisites and promotion receipts, sc-55..sc-65); **N-4** resolved (protected edges, union semantics, bypassable definitions rejected at load).

Remaining assumptions needing owner attention: (C-1) one generic external code `PROMOTION_REFUSED` for all non-validation security refusals, including broker and effect refusals, and `ACCEPTANCE_BINDING_REQUIRED` hidden externally although it tells the caller what to supply; (C-2) protected-edge bypass analysis treats a statement without `from_states` as permitting any named state; (C-3) a crash after reservation resumes on retry (differs from M0); (C-4) malformed stored grants (`GRANT_MALFORMED`) are not reachable through the fixture `approve` operation; (C-5) the broker comparison with M0 is by test correspondence, not execution (M1-05); (C-6) concurrency in fixtures is checked as a serial-equivalent outcome only.

The sections below describe revision 3 and remain accurate except where the summary above supersedes them.

## Files

| Path | Content |
|---|---|
| [`m1/fixtures/acceptance/SEMANTICS.md`](../../m1/fixtures/acceptance/SEMANTICS.md) | `check-acceptance` m1-draft-3: input trust, strict JSON and depth, identifiers, check order, internal versus external codes |
| [`m1/fixtures/transition/SCENARIOS.md`](../../m1/fixtures/transition/SCENARIOS.md) | Boundary table, promotion versus effect, kernel extension (capabilities, transitions), broker promotion, protection, effects and reconciliation |
| [`m1/fixtures/acceptance/acceptance-binding.cases.json`](../../m1/fixtures/acceptance/acceptance-binding.cases.json) | 97 cases (source) |
| [`m1/fixtures/transition/transition-decisions.cases.json`](../../m1/fixtures/transition/transition-decisions.cases.json) | 45 cases (source) |
| [`m1/fixtures/transition/transition-scenarios.cases.json`](../../m1/fixtures/transition/transition-scenarios.cases.json) | 49 cases (source) |
| [`m1/fixtures/canonical/strict-json.cases.json`](../../m1/fixtures/canonical/strict-json.cases.json) | 37 cases (source) |
| [`m1/fixtures/refusal/external-refusal.cases.json`](../../m1/fixtures/refusal/external-refusal.cases.json) | 36 cases (source) |
| [`m1/fixtures/transition/m1-transition.bundle.json`](../../m1/fixtures/transition/m1-transition.bundle.json), [`m1-transition-unscoped.bundle.json`](../../m1/fixtures/transition/m1-transition-unscoped.bundle.json) | Policy fixtures (0.3.0; the second must be rejected at load) |
| [`m1/fixtures/export_m0_fixtures.py`](../../m1/fixtures/export_m0_fixtures.py), [`manifest.json`](../../m1/fixtures/manifest.json), `m1/fixtures/sets/*.jsonl` | Builder (assembles inputs only), manifest, 7 sets, 291 cases |
| [`m1/driver/tests/test_fixtures.py`](../../m1/driver/tests/test_fixtures.py) | Fixture integrity tests |

## Counts

| Set | Cases | Added in amendment 2 | Expected values changed in amendment 2 |
|---|---|---|---|
| canonical-vector | 1 | 0 | 0 |
| strict-json | 37 | 15 | 0 |
| golden-decisions | 26 | 0 | 0 |
| acceptance-binding | 97 | 3 | 0 |
| transition-decisions | 45 | 10 | 1 (`tx-09`) |
| transition-scenarios | 49 | 19 | 30 (format only: `final` gains `refusals` and `effects`) |
| external-refusal | 36 | 36 | n/a (new) |
| **Total** | **291** | **83** | **1 value, 30 format** |

## Category to invariant map (INV-32..INV-44 PROPOSED)

| Category | Cases | Invariants |
|---|---|---|
| Strict parsing and depth | cj-*, js-*, tx-31..34, tx-43..45, sc-48, sc-49 | INV-41, INV-18 |
| Exact transition authorization | tx-01..06, tx-24..30, tx-36 | INV-42, INV-03, INV-04 |
| Capability redundancy and composites | tx-07..09, tx-40..42, sc-28, sc-37 | INV-42, RM-01 |
| DENY versus INVALID | tx-07, tx-39, tx-40 | INV-42 |
| Protected and unprotected transitions | tx-36..38, sc-31..35 | INV-43, INV-32 |
| Permit binding (request, record, authorization bundle) | sc-09, sc-13, sc-18..26, sc-36 | INV-42, INV-01, D-012 |
| Requirement completeness, model-assisted advisory | pos-02, pos-02b, req-*, cmp-*, ma-*, crit-09, sc-29 | INV-33, INV-34, INV-37, INV-38 |
| Revoked and stale evaluators, time | crit-06, crit-07, crit-13..16, int-02..07, exp-01, ord-04, sc-11, sc-30 | INV-36, INV-38 |
| Isolation | scope-*, tx-10, tx-11, sc-16, sc-21, sc-27 | INV-10, INV-33 |
| Stale state and policy | rev-01, pol-*, ord-05, tx-12..15, tx-29, sc-06, sc-08, sc-15, sc-20, sc-36 | INV-05, INV-12, INV-17, INV-36 |
| Replay, idempotency, races, reconciliation | rep-01, sc-02..04, sc-10, sc-13, sc-38..47 | INV-06, INV-07, INV-08, INV-09, INV-44 |
| Artifact and snapshot identity | art-*, int-01, sc-05, sc-07, sc-22, sc-23, sc-26 | INV-33, INV-40 (identity binding only) |
| Disclosure | external-refusal set | INV-44 |
| Injection and alternate routes | mal-02, tx-20..22, sc-33, sc-34 | INV-02, INV-11, INV-13, INV-14, INV-43 |

## Depth limit (A1)

Root depth 0; array elements and object member names and values at parent plus 1; depth greater than 64 rejected (traversal depth, not container count). Boundary results after freezing, against the D-006 reference module: 14 of 14 agree (cj-19, cj-20, cj-23..cj-34), including canonical bytes and digests. Key distinction: 65 nested empty arrays accepted (cj-19), 65 nested arrays around a scalar rejected (cj-24), an object at depth 64 rejected only when it has a member (cj-25, cj-26).

## Can the requirements be enforced independently of the AI?

By design, yes; demonstrated only at the **Designed** level. Deciding inputs come from trusted state (requirement set, protection list, active authorization and acceptance-policy digests, clock, revocations, consumed ids, permits). The agent supplies only the request and the record, both fully checked, and receives only sanitized refusals. Real independence also needs authentic issuance and attestation (boundary 2), immutable snapshots and credential isolation (boundary 4), and SR-06 evidence of host mediation.

## New findings in this revision

| # | Finding | Status |
|---|---|---|
| N-1 | Protection is enforced only by the broker: the kernel allows a protected transition without acceptance attributes (tx-38) and the broker refuses it (sc-32). One enforcement point | Assumption B-1 for owner decision (alternative: also carry protection in the authorization bundle so the kernel refuses too) |
| N-2 | The "Broker semantics" hard-gate set in the parity suite still points at Python tests in `m0/tests/test_broker.py`; those scenarios (grant consumption, single-use grants under a 16-way race, crash reconciliation for effects) are not yet language-neutral fixtures. A6's "refusals never consume a grant" is therefore not covered by any M1 fixture | Gap: export them as a fixture set before M1-03 |
| N-3 | The effect operation does not require the workflow to be in a promoted state; coupling "publish only from accepted" is a policy concern not modeled here | Open for M2 contracts |
| N-4 | Protection is keyed by target state only; protection that depends on the source state cannot be expressed | Assumption B-2 |

No contradiction found with D-006 (depth agrees), D-008, D-010, D-012 or ADR-0008 after these amendments.

## Security assumptions that remain unverified

- Boundaries 2 and 4 (record issuance, evaluator attestation, signatures, snapshot immutability, credential isolation): not covered by any fixture.
- Trusted clock integrity, skew and rollback (M3, risk 14).
- Single protection enforcement point (N-1) and target-state protection (N-4).
- The external-refusal mapping itself (which codes are safe to show) is a judgment, not a verified property.
- Grant consumption semantics in M1 (N-2).
- Host mediation of any of this (SR-06 NOT VERIFIED).

## Evidence

[TEST-EVIDENCE](TEST-EVIDENCE.md) E-020.
