# V1 regression scenarios ported to V2 (M0-01f)

**Baseline:** main `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Source files are read with `git show ebdafe5:tests/<file>`; the digest column is the first 16 hex characters of the file's sha256. V1 mechanisms (transcript-scanned approvals, gate files, session caches, shell fallback) are **not** ported. The failure lesson is re-expressed against V2 contracts in [test_v1_regressions.py](../../../m0/tests/test_v1_regressions.py) unless an existing test already covers it.

## Approval

| V1 source (file::test, digest) | Lesson | V2 test | Status |
|---|---|---|---|
| `test_approval_exact_only.py::test_phrase_inside_a_sentence_does_not_grant` (124bf88a23253d88) | Quoting an approval phrase minted a real grant | `TestApprovalRegressions::test_approval_text_anywhere_mints_nothing` | ported |
| `test_approval_exact_only.py::test_is_approval_fails_closed` (124bf88a23253d88) | Approval predicate must fail closed on odd input | `test_approval_service_accepts_no_text` | ported |
| `test_approval_exact_only.py` docstring, empty-gate token as canon-write credential (124bf88a23253d88) | A token for one purpose authorized another | `test_grant_for_one_action_kind_cannot_authorize_another` | ported |
| `test_approval_evidence.py::test_no_token_is_minted` (5154f6dd900bc1b2) | Approval with nothing pending must mint nothing | `test_no_grant_without_a_pending_approval_request` | ported |
| `test_approval_expiry_on_completion.py::test_a_completed_plan_clears_both_gates` (e29c7467c6a98a47) | Approval outlived its work by days | `test_approval_does_not_outlive_its_window`; single use in `test_broker.py::test_authenticated_grant_allows_exactly_once` | ported |
| `test_gate_approval_plan_binding.py::test_an_approval_does_not_survive_a_plan_rewrite` (d8a97aaab324d09e) | Approval survived a rewrite of what it approved | `test_approval_does_not_survive_a_change_to_what_was_approved` | ported |
| `test_gate_approval_plan_binding.py::test_an_approval_with_no_recorded_plan_is_not_honored` (d8a97aaab324d09e) | Unfingerprinted approval must count as unapproved | `test_grant_missing_a_binding_field_is_not_honored` (4 fields) | ported; **found defect** (see below) |
| `test_manual_test_grant.py::test_grant_copied_to_another_session_is_rejected` (908dc5c741ab5372) | Grant must not transfer across sessions | `test_grant_copied_to_another_session_is_rejected` | ported |
| `test_manual_test_grant.py::test_child_inherits_live_grant_with_parent_expiry` (908dc5c741ab5372) | V1 allowed subagent inheritance | `test_subagent_does_not_inherit_parent_grant` | **deliberate divergence**: V2 requires a separate approval per requester principal; reviewer should confirm |
| `test_phase3b_approval_rewrap.py::test_work_planning_approval_advances` (6a9446cd1c4bd596) | Hook advanced gates on typed text as "trusted infra" | `test_broker.py::test_requester_cannot_self_approve`, `test_human_from_untrusted_channel_rejected` | covered by existing tests |

## Fail-open

| V1 source | Lesson | V2 test | Status |
|---|---|---|---|
| `test_gates_fail_open.py::test_a_crashed_evaluator_still_allows_by_default` (24d1e658b160971b) | A crashed evaluator defaulted to allow | `TestFailOpenRegressions::test_crashed_policy_provider_does_not_allow`, `test_crashed_state_provider_does_not_allow` | ported |
| `test_gates_fail_open.py::test_a_crashed_evaluator_denies_under_strict` (24d1e658b160971b) | Safety depended on an opt-in strict flag | `test_there_is_no_non_strict_mode` | ported |
| `test_gates_fail_open.py` (decision store unavailable) | Store outage must not allow | `test_ledger_failure_before_dispatch_prevents_the_effect`; `test_broker.py::test_unavailable_policy_fails_closed` | ported |
| `test_gates_fail_open.py::test_a_source_write_is_refused_when_the_plan_changed` (24d1e658b160971b) | Write gate ignored the plan its approval named | same as plan-rewrite row above | covered |

## Isolation

| V1 source | Lesson | V2 test | Status |
|---|---|---|---|
| `test_session_identity_process_isolation.py::test_a_real_process_with_no_resolvable_identity_refuses_even_beside_a_populated_directory` (db44d635e10915b0) | No identity must refuse | `TestIsolationRegressions::test_missing_identity_refuses` | ported |
| `test_session_identity_process_isolation.py::test_a_stray_newer_cache_from_an_unrelated_session_does_not_redirect_either_process` (db44d635e10915b0) | Newest session state hijacked identity | `test_newer_identity_from_unrelated_session_cannot_redirect` (2 threads x 200 requests) | ported (threads, not separate OS processes) |
| `test_subagent_isolation.py::test_agent_session_isolated_from_parent` (ee5c5d307d7b343e) | Subagent must not share parent state | `test_subagent_does_not_inherit_parent_grant` | ported |
| `test_cross_project_retrieval_isolation.py::TestRecordScoping` (e3507e1e7c7656ac) | Records must not cross scope | `test_receipts_are_scoped_per_tenant` | ported |
| `test_cross_project_retrieval_isolation.py::TestEnrichmentIsScopedLikeCandidates` (e3507e1e7c7656ac) | Enrichment must be scoped like candidates | `test_project_memory_cannot_reach_a_sibling_project` (write side only) | partial: retrieval and graph enrichment do not exist until M7/M8 |

## Defect found while porting

A ledger grant row missing a binding field (`action_digest`, `policy_bundle_sha`, `requester_principal_id`, `expires_at`) made `Broker._consume_grant` raise `KeyError`. No effect ran, so the failure was closed, but untyped. Fixed by validating the stored grant against the ApprovalGrant schema before use; it now refuses with `GRANT_MALFORMED`. Test written first and observed failing (4 failures) before the fix.

## Not ported (gaps)

- **Host-level crash semantics.** V1's deepest fail-open was a crashing hook that Claude Code treats as non-blocking. The M0 harness has no Claude Code hook-output layer, so this cannot be tested here. The M5 Claude Code adapter must turn any internal error into an explicit deny reply. Tracked in M0-01e.
- **Project-scoped evidence sources.** Addressed after this port by SR-05 (`f5a950e`): Evidence requires `project_id` and sources resolve per tenant and project; see `test_evaluation.py::TestProjectIsolation`. Retrieval and graph isolation remain M7/M8.
- **Retrieval isolation against a real graph.** Requires M7/M8 and a disposable Neo4j.
