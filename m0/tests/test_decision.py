"""Reference decision oracle (M0-03b): golden cases, precedence and determinism properties."""
from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings, strategies as st

from writ_m0 import canonical, contracts, decision
from conftest import NOW, STATE, merge_patch

GOLDEN = contracts.load_fixture("decisions/m0-golden.json")


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["case_id"])
def test_golden_case(case, bundle):
    request = merge_patch(GOLDEN["base_request"], case["patch"])
    result = decision.decide(request, bundle if case["bundle_loaded"] else None,
                             decision.KernelState(GOLDEN["state_revision"]), NOW)
    got = {k: result[k] for k in ("status", "reason_code", "matched_rule_ids")}
    assert got == case["expected"], case["rationale"]


def test_golden_cases_cover_every_decision_status():
    assert {c["expected"]["status"] for c in GOLDEN["cases"]} == {
        "allow", "deny", "require_approval", "invalid", "unavailable"}


class TestBundleLoading:
    def test_sha_excludes_signature_and_is_stable(self, bundle):
        signed = bundle.document | {"publisher_signature": "sig"}
        assert decision.bundle_sha(signed) == bundle.sha
        assert bundle.sha.startswith("sha256:")

    def test_pin_mismatch_rejected(self, bundle):
        with pytest.raises(decision.BundleError):
            decision.load_bundle(bundle.document, pinned_sha="sha256:" + "0" * 64)

    def test_damaged_bundle_rejected(self, bundle):
        damaged = copy.deepcopy(bundle.document)
        damaged["statements"][0]["effect"] = "allow_all"
        with pytest.raises(decision.BundleError):
            decision.load_bundle(damaged)

    def test_statement_with_unknown_capability_rejected(self, bundle):
        damaged = copy.deepcopy(bundle.document)
        damaged["statements"][0]["action_kinds"] = ["shell.anything"]
        with pytest.raises(decision.BundleError):
            decision.load_bundle(damaged)

    def test_duplicate_statement_ids_rejected(self, bundle):
        damaged = copy.deepcopy(bundle.document)
        damaged["statements"].append(damaged["statements"][0])
        with pytest.raises(decision.BundleError):
            decision.load_bundle(damaged)


class TestDeterminismAndPrecedence:
    def test_repeated_denial_never_changes(self, bundle, base_request):
        request = merge_patch(base_request, {"action": {"kind": "network.egress", "resource": {
            "canonical_resource_id": "net:fixture-tenant/fixture-project/example.org"}}})
        results = [decision.decide(request, bundle, STATE, NOW) for _ in range(50)]
        assert all(r == results[0] for r in results)
        assert results[0]["status"] == "deny"

    def test_forbid_wins_even_when_every_other_effect_matches(self, bundle, base_request):
        doc = copy.deepcopy(bundle.document)
        doc["statements"] += [
            {"statement_id": "z-permit-egress", "effect": "permit", "action_kinds": ["network.egress"], "reason_code": "X"},
            {"statement_id": "z-approve-egress", "effect": "require_approval", "action_kinds": ["network.egress"], "reason_code": "Y"},
        ]
        widened = decision.load_bundle(doc)
        request = merge_patch(base_request, {"action": {"kind": "network.egress", "resource": {
            "canonical_resource_id": "net:fixture-tenant/fixture-project/example.org"}}})
        assert decision.decide(request, widened, STATE, NOW)["status"] == "deny"

    @settings(max_examples=200, deadline=None)
    @given(attributes=st.dictionaries(st.text(max_size=12), st.one_of(st.booleans(), st.text(max_size=20), st.integers()), max_size=6),
           agent_id=st.one_of(st.none(), st.text(min_size=1, max_size=12)),
           session_id=st.text(min_size=1, max_size=12),
           kind=st.sampled_from(["file.write", "file.read", "network.egress", "repository.publish", "process.execute"]))
    def test_untrusted_payload_fields_never_change_the_verdict(self, bundle, attributes, agent_id, session_id, kind):
        base = merge_patch(GOLDEN["base_request"], {"action": {"kind": kind}})
        noisy = merge_patch(base, {"session_id": session_id, "action": {"attributes": attributes}})
        noisy["agent_id"] = agent_id
        strip = lambda d: {k: d[k] for k in ("status", "reason_code", "matched_rule_ids", "policy_bundle_sha")}
        assert strip(decision.decide(noisy, bundle, STATE, NOW)) == strip(decision.decide(base, bundle, STATE, NOW))

    @settings(max_examples=200, deadline=None)
    @given(patch=st.dictionaries(
        st.sampled_from(["tenant_id", "project_id", "request_id", "idempotency_key", "principal", "action"]),
        st.one_of(st.none(), st.text(max_size=8), st.integers(), st.dictionaries(st.text(max_size=6), st.text(max_size=6), max_size=3)),
        max_size=3))
    def test_schema_invalid_requests_are_invalid(self, bundle, patch):
        request = merge_patch(GOLDEN["base_request"], patch)
        result = decision.decide(request, bundle, STATE, NOW)
        contracts.validate("decision", result)
        if contracts.violations("action-request", request):
            assert result["status"] == "invalid"

    def test_decision_is_a_pure_function_of_inputs(self, bundle, base_request):
        a = decision.decide(base_request, bundle, STATE, NOW)
        b = decision.decide(copy.deepcopy(base_request), bundle, decision.KernelState(5), NOW)
        assert canonical.digest(a) == canonical.digest(b)

    def test_approval_reference_binds_digest(self, bundle, base_request):
        req = merge_patch(base_request, {"action": {"kind": "repository.publish", "resource": {
            "canonical_resource_id": "repo:fixture-tenant/fixture-project/origin"}}})
        other = merge_patch(req, {"action": {"action_digest": "sha256:" + "b" * 64}})
        a = decision.decide(req, bundle, STATE, NOW)["approval_request_reference"]
        b = decision.decide(other, bundle, STATE, NOW)["approval_request_reference"]
        assert a and b and a != b


def test_golden_cases_carry_review_status_and_none_is_self_approved():
    """Cases stay PENDING until an independent reviewer records a verdict (M0-05a)."""
    for case in GOLDEN["cases"]:
        status = case.get("review_status")
        assert status is not None, case["case_id"]
        if status != "PENDING INDEPENDENT REVIEW":
            assert case.get("reviewer") and case.get("reviewed_at"), case["case_id"]
