# Golden decisions: named human review worksheet (M0-05a)

**Status:** prepared for review. **No verdict below has been entered.** All 26 fixture cases are `PENDING INDEPENDENT REVIEW`.

## Who may review

- A named human who did not author the cases. Claude, any AI assistant, and the AI-assisted reviews 1 to 4 do not qualify; their assessments are listed only as input.
- The project owner made the policy decisions for cases 01, 08 and 26 (D-011). If the owner also reviews, record that in the attestation so the independence limitation is visible.
- The reviewer judges the expected decision against the policy fixture and the V2 architecture, not against the current code. A test passing is not evidence that an expectation is right.

## Inputs

- Per-case detail (inputs, rationale, invariants, ambiguities): [GOLDEN-DECISION-REVIEW](GOLDEN-DECISION-REVIEW.md)
- Fixture: [m0-golden.json](../../contracts/fixtures/decisions/m0-golden.json); policy: [m0-reference.bundle.json](../../contracts/fixtures/policy/m0-reference.bundle.json)
- Semantics: [DECISION-LOG](../../implementation/DECISION-LOG.md) D-008, D-010, D-011; [invariants](../../security/invariants.md); [threat model](../../security/threat-model.md)
- AI-assisted assessments: [review 1](INDEPENDENT-REVIEW-M0.md), [review 2](INDEPENDENT-REVIEW-M0-2.md), [review 3](INDEPENDENT-REVIEW-M0-3.md), [review 4](INDEPENDENT-REVIEW-M0-4.md)

## Verdict options

- **AGREE**: expected decision is correct.
- **DISAGREE**: write the corrected `status / reason_code / matched_rule_ids` and why.
- **NEEDS POLICY DECISION**: the policy, not the case, is undecided; name the question.

## Worksheet

| # | Case | Expected | Owner policy | AI reviews 2/3 | Verdict | Corrected expectation | Notes |
|---|---|---|---|---|---|---|---|
| 01 | `workspace-file-write` | allow / WORKSPACE_FILE_PERMITTED / permit-workspace-files | D-011 | AGREE / AGREE | | | |
| 02 | `secrets-write-forbidden` | deny / SECRETS_PATH_FORBIDDEN / forbid-secrets |  | AGREE / AGREE | | | |
| 03 | `secrets-read-forbidden` | deny / SECRETS_PATH_FORBIDDEN / forbid-secrets |  | AGREE / AGREE | | | |
| 04 | `egress-forbidden-tenant` | deny / NETWORK_EGRESS_FORBIDDEN / forbid-network-egress |  | AGREE / AGREE | | | |
| 05 | `egress-other-tenant-default-deny` | deny / DEFAULT_DENY / none |  | AGREE / AGREE | | | |
| 06 | `publish-requires-approval` | require_approval / PUBLISH_REQUIRES_APPROVAL / approve-publish |  | AGREE / AGREE | | | |
| 07 | `process-requires-approval` | require_approval / PROCESS_REQUIRES_APPROVAL / approve-process |  | AGREE / AGREE | | | |
| 08 | `operator-process-still-requires-approval` | require_approval / PROCESS_REQUIRES_APPROVAL / approve-process | D-011 | AGREE / AGREE | | | |
| 09 | `unknown-capability` | deny / UNKNOWN_CAPABILITY / none |  | AGREE / AGREE | | | |
| 10 | `untrusted-issuer` | deny / PRINCIPAL_UNVERIFIED / none |  | AGREE / AGREE | | | |
| 11 | `stale-revision` | invalid / STATE_REVISION_CONFLICT / none |  | AGREE / AGREE | | | |
| 12 | `policy-pin-mismatch` | unavailable / POLICY_VERSION_MISMATCH / none |  | AGREE / AGREE | | | |
| 13 | `no-policy-loaded` | unavailable / POLICY_UNAVAILABLE / none |  | AGREE / AGREE | | | |
| 14 | `missing-principal` | invalid / REQUEST_SCHEMA_INVALID / none |  | AGREE / AGREE | | | |
| 15 | `resource-not-canonical` | invalid / RESOURCE_NOT_CANONICAL / none |  | AGREE / AGREE | | | |
| 16 | `resource-dot-dot` | invalid / RESOURCE_NOT_CANONICAL / none |  | AGREE / AGREE | | | |
| 17 | `cross-tenant-resource` | deny / RESOURCE_SCOPE_MISMATCH / none |  | AGREE / AGREE | | | |
| 18 | `cross-project-resource` | deny / RESOURCE_SCOPE_MISMATCH / none |  | AGREE / AGREE | | | |
| 19 | `piggyback-forbidden-capability` | deny / NETWORK_EGRESS_FORBIDDEN / forbid-network-egress |  | AGREE / AGREE | | | |
| 20 | `piggyback-approval-capability` | require_approval / PUBLISH_REQUIRES_APPROVAL / approve-publish |  | AGREE / AGREE | | | |
| 21 | `piggyback-unpermitted-capability` | deny / CAPABILITY_NOT_PERMITTED / permit-workspace-files |  | AGREE / AGREE | | | |
| 22 | `attributes-cannot-grant` | deny / NETWORK_EGRESS_FORBIDDEN / forbid-network-egress |  | AGREE / AGREE | | | |
| 23 | `project-memory-write` | allow / PROJECT_MEMORY_PERMITTED / permit-project-memory |  | AGREE / AGREE | | | |
| 24 | `workspace-outside-project-prefix-default-deny` | deny / RESOURCE_SCOPE_MISMATCH / none |  | AGREE / AGREE | | | |
| 25 | `resource-trailing-empty-segment` | invalid / RESOURCE_NOT_CANONICAL / none |  | AGREE / AGREE | | | |
| 26 | `deadline-exceeded` | invalid / DEADLINE_EXCEEDED / none | D-011 | AGREE / AGREE | | | |

## Attestation

| Field | Value |
|---|---|
| Reviewer name | |
| Role and relationship to the project | |
| Authored any case or the reference code? (yes/no) | |
| Date | |
| Fixture revision reviewed (commit SHA) | |
| Signature or verifiable channel (e.g. signed commit, PR review) | |

## Recording procedure

1. The reviewer fills the worksheet and attestation, preferably as a signed commit or a GitHub PR review from their own account.
2. Only then may the fixture be updated, per case: `review_status` to `REVIEWED`, plus `reviewer`, `reviewed_at` and `verdict`. `m0/tests/test_decision.py::test_golden_cases_carry_review_status_and_none_is_self_approved` rejects a non-pending status without reviewer and date.
3. A DISAGREE verdict changes `expected` only in the same commit that cites the reviewer's corrected expectation; the decision code is then fixed test-first if it disagrees.
4. Claude may transcribe a completed, attributable worksheet into the fixture when asked, but must not fill any verdict itself.
