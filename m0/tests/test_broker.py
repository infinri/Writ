"""Broker, approval ledger and effect mediation (M0-03b; INV-01, 03, 06, 07, 08, 09)."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from writ_m0 import broker as b
from writ_m0 import decision, ingress
from conftest import INGRESS_ADAPTER, INGRESS_KEY, NOW, SEALER, STATE, merge_patch

PUBLISH = {"action": {"kind": "repository.publish", "resource": {
    "canonical_resource_id": "repo:fixture-tenant/fixture-project/origin"}}}


@pytest.fixture
def env(tmp_path, bundle):
    ledger = b.Ledger(tmp_path / "ledger.db")
    signer = b.PermitSigner(ledger)
    current = {"bundle": bundle}
    broker = b.Broker(ledger=ledger, bundle_provider=lambda: current["bundle"],
                      state_provider=lambda: STATE, signer=signer,
                      ingress=ingress.IngressVerifier({INGRESS_ADAPTER: INGRESS_KEY}))
    channel = b.TrustedChannel("approval-ui:fixture", {"human:alice": "alice-secret"})
    approvals = b.ApprovalService(ledger, [channel])
    effect = b.RecordingEffect(signer, lambda: NOW)
    return {"ledger": ledger, "broker": broker, "channel": channel, "approvals": approvals,
            "effect": effect, "current": current, "signer": signer,
            # A trusted adapter seals what it normalized; tests stand in for that adapter.
            "execute": lambda req, *a, **k: broker.execute(SEALER.seal(req), *a, **k)}


def approve(env, request, **kw):
    pending = decision.decide(request, env["current"]["bundle"], STATE, NOW)
    human = env["channel"].authenticate("human:alice", "alice-secret")
    return env["approvals"].decide(request=request, decision=pending, human=human, approve=True, now=NOW, **kw)


class TestAllowPath:
    def test_allowed_effect_produces_success_receipt(self, env, base_request):
        receipt = env["execute"](base_request, env["effect"], NOW)
        assert receipt["status"] == "observed_success"
        assert env["effect"].applied == [base_request["action"]["action_digest"]]

    def test_denied_effect_never_reaches_adapter(self, env, base_request):
        req = merge_patch(base_request, {"action": {"resource": {
            "canonical_resource_id": "workspace:fixture-tenant/fixture-project/secrets/key"}}})
        for _ in range(5):  # INV-03: retries do not soften a deny
            with pytest.raises(b.BrokerRefusal) as info:
                env["execute"](req, env["effect"], NOW)
            assert info.value.code == "SECRETS_PATH_FORBIDDEN"
        assert env["effect"].applied == []
        assert env["ledger"].audit_kinds().count("refused") == 5


class TestEffectMediation:
    def test_direct_adapter_call_without_permit_refused(self, env, base_request):
        with pytest.raises(b.UnauthorizedEffect):
            env["effect"].apply(None, base_request)

    def test_forged_permit_refused(self, env, base_request):
        forged = {"action_digest": base_request["action"]["action_digest"], "broker_mac": "0" * 64,
                  "expires_at": "2099-01-01T00:00:00Z"}
        with pytest.raises(b.UnauthorizedEffect):
            env["effect"].apply(forged, base_request)

    def test_permit_for_one_operation_cannot_drive_another(self, env, base_request):
        captured = {}

        class Capture(b.RecordingEffect):
            def apply(self, permit, request):
                captured["permit"] = permit
                return super().apply(permit, request)

        env["execute"](base_request, Capture(env["signer"], lambda: NOW), NOW)
        other = merge_patch(base_request, {"action": {"action_digest": "sha256:" + "c" * 64}})
        with pytest.raises(b.UnauthorizedEffect):
            env["effect"].apply(captured["permit"], other)

    def test_expired_permit_refused(self, env, base_request):
        captured = {}

        class Capture(b.RecordingEffect):
            def apply(self, permit, request):
                captured["permit"] = permit
                return "sha256:" + "0" * 64

        env["execute"](base_request, Capture(env["signer"], lambda: NOW), NOW)
        late = b.RecordingEffect(env["signer"], lambda: NOW + b.PERMIT_TTL + timedelta(seconds=1))
        with pytest.raises(b.UnauthorizedEffect):
            late.apply(captured["permit"], base_request)


class TestApprovals:
    def test_require_approval_without_grant_refused(self, env, base_request):
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](merge_patch(base_request, PUBLISH), env["effect"], NOW)
        assert info.value.code == "APPROVAL_REQUIRED"

    def test_authenticated_grant_allows_exactly_once(self, env, base_request):
        req = merge_patch(base_request, PUBLISH)
        grant = approve(env, req)
        assert env["execute"](req, env["effect"], NOW, grant["grant_id"])["status"] == "observed_success"
        replay = merge_patch(req, {"idempotency_key": "idem-2", "request_id": "req-2"})
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](replay, env["effect"], NOW, grant["grant_id"])
        assert info.value.code == "GRANT_EXHAUSTED"

    @pytest.mark.parametrize("swap", [
        {"action": {"action_digest": "sha256:" + "d" * 64}},
        {"action": {"resource": {"canonical_resource_id": "repo:fixture-tenant/fixture-project/upstream"}}},
        {"principal": {"id": "agent:other-session"}},
    ], ids=["changed-content", "swapped-target", "other-principal"])
    def test_grant_bound_to_exact_operation(self, env, base_request, swap):
        req = merge_patch(base_request, PUBLISH)
        grant = approve(env, req)
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](merge_patch(req, swap), env["effect"], NOW, grant["grant_id"])
        assert info.value.code.startswith("GRANT_MISMATCH")
        assert env["effect"].applied == []

    def test_policy_change_invalidates_pending_grant(self, env, base_request, bundle):
        req = merge_patch(base_request, PUBLISH)
        grant = approve(env, req)
        newer = dict(bundle.document, version="0.1.1")
        env["current"]["bundle"] = decision.load_bundle(newer)
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](req, env["effect"], NOW, grant["grant_id"])
        assert "policy_bundle_sha" in info.value.code

    def test_expired_grant_refused(self, env, base_request):
        req = merge_patch(base_request, PUBLISH)
        grant = approve(env, req, ttl=timedelta(seconds=1))
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](req, env["effect"], NOW + timedelta(seconds=2), grant["grant_id"])
        assert info.value.code == "GRANT_EXPIRED"

    def test_denied_verdict_does_not_authorize(self, env, base_request):
        req = merge_patch(base_request, PUBLISH)
        pending = decision.decide(req, env["current"]["bundle"], STATE, NOW)
        human = env["channel"].authenticate("human:alice", "alice-secret")
        grant = env["approvals"].decide(request=req, decision=pending, human=human, approve=False, now=NOW)
        with pytest.raises(b.BrokerRefusal):
            env["execute"](req, env["effect"], NOW, grant["grant_id"])

    def test_grant_cannot_override_a_forbid(self, env, base_request):
        req = merge_patch(base_request, {"action": {"kind": "network.egress", "resource": {
            "canonical_resource_id": "net:fixture-tenant/fixture-project/example.org"}}})
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](req, env["effect"], NOW, grant_id="grant:anything")
        assert info.value.code == "NETWORK_EGRESS_FORBIDDEN"

    def test_unknown_or_forged_grant_id_refused(self, env, base_request):
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](merge_patch(base_request, PUBLISH), env["effect"], NOW, "grant:forged")
        assert info.value.code == "GRANT_UNKNOWN"

    def test_wrong_credential_cannot_authenticate(self, env):
        with pytest.raises(PermissionError):
            env["channel"].authenticate("human:alice", "approved")

    def test_human_from_untrusted_channel_rejected(self, env, base_request):
        req = merge_patch(base_request, PUBLISH)
        rogue = b.TrustedChannel("approval-ui:fixture", {"human:mallory": "x"})  # same id, different instance
        human = rogue.authenticate("human:mallory", "x")
        pending = decision.decide(req, env["current"]["bundle"], STATE, NOW)
        with pytest.raises(PermissionError):
            env["approvals"].decide(request=req, decision=pending, human=human, approve=True, now=NOW)
        assert env["ledger"].grant_count() == 0

    def test_requester_cannot_self_approve(self, env, base_request):
        req = merge_patch(base_request, PUBLISH | {"principal": {"id": "human:alice"}})
        with pytest.raises(PermissionError):
            approve(env, req)

    def test_concurrent_consumers_get_at_most_one_use(self, env, base_request):
        req = merge_patch(base_request, PUBLISH)
        grant = approve(env, req)
        outcomes, barrier = [], threading.Barrier(16)

        def attempt(i):
            barrier.wait()
            try:
                r = env["execute"](merge_patch(req, {"idempotency_key": f"k-{i}", "request_id": f"r-{i}"}),
                                          env["effect"], NOW, grant["grant_id"])
                outcomes.append(r["status"])
            except b.BrokerRefusal as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("observed_success") == 1
        assert outcomes.count("GRANT_EXHAUSTED") == 15
        assert len(env["effect"].applied) == 1


class TestIdempotencyAndReceipts:
    def test_duplicate_key_same_digest_returns_original_without_reapplying(self, env, base_request):
        first = env["execute"](base_request, env["effect"], NOW)
        second = env["execute"](base_request, env["effect"], NOW)
        assert first == second
        assert len(env["effect"].applied) == 1

    def test_duplicate_key_different_digest_refused(self, env, base_request):
        env["execute"](base_request, env["effect"], NOW)
        changed = merge_patch(base_request, {"action": {"action_digest": "sha256:" + "e" * 64}})
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](changed, env["effect"], NOW)
        assert info.value.code == "IDEMPOTENCY_CONFLICT"
        assert len(env["effect"].applied) == 1

    def test_concurrent_duplicates_dispatch_once(self, env, base_request):
        barrier, results = threading.Barrier(8), []

        def attempt():
            barrier.wait()
            results.append(env["execute"](base_request, env["effect"], NOW)["attempt_id"])

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(env["effect"].applied) == 1
        assert len(set(results)) == 1

    def test_crash_after_effect_leaves_reconcilable_receipt(self, env, base_request):
        env["effect"].crash_after_apply = True
        with pytest.raises(b.SimulatedCrash):
            env["execute"](base_request, env["effect"], NOW)
        stranded = env["ledger"].receipt("fixture-tenant", base_request["idempotency_key"])
        assert stranded["status"] == "dispatched"
        env["effect"].crash_after_apply = False
        # A retry must not dispatch again while the outcome is unknown.
        assert env["execute"](base_request, env["effect"], NOW)["status"] == "dispatched"
        assert len(env["effect"].applied) == 1
        reconciled = env["broker"].reconcile(NOW)
        assert [r["status"] for r in reconciled] == ["unknown_reconcile"]

    def test_effect_failure_is_recorded_not_swallowed(self, env, base_request):
        class Failing(b.RecordingEffect):
            def apply(self, permit, request):
                super().apply(permit, request)
                raise OSError("disk full")

        receipt = env["execute"](base_request, Failing(env["signer"], lambda: NOW), NOW)
        assert receipt["status"] == "observed_failure"
        assert receipt["reconciliation_notes"] == "OSError"

    def test_unavailable_policy_fails_closed(self, env, base_request):
        env["current"]["bundle"] = None
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](base_request, env["effect"], NOW)
        assert info.value.code == "POLICY_UNAVAILABLE"
        assert env["effect"].applied == []


class TestDeadlinesAtUseTime:
    """SR-03: the deadline is re-checked when the broker executes, and bounds the permit."""

    def test_valid_grant_cannot_execute_after_the_deadline(self, env, base_request):
        req = merge_patch(base_request, PUBLISH | {"deadline_at": "2026-11-05T12:01:00Z"})
        grant = approve(env, req)
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](req, env["effect"], NOW + timedelta(minutes=2), grant["grant_id"])
        assert info.value.code == "DEADLINE_EXCEEDED"
        assert env["effect"].applied == []

    def test_permit_expiry_bounded_by_request_deadline(self, env, base_request):
        captured = {}

        class Capture(b.RecordingEffect):
            def apply(self, permit, request):
                captured["permit"] = permit
                return super().apply(permit, request)

        req = merge_patch(base_request, {"deadline_at": "2026-11-05T12:00:05Z"})
        env["execute"](req, Capture(env["signer"], lambda: NOW), NOW)
        assert captured["permit"]["expires_at"] == "2026-11-05T12:00:05Z"


class TestApprovalBindsCapabilitySet:
    """SR-02: one grant cannot cover a different requested capability set."""

    def test_capability_substitution_under_same_grant_refused(self, env, base_request):
        approved = merge_patch(base_request, PUBLISH)
        grant = approve(env, approved)
        widened = merge_patch(approved, {"idempotency_key": "idem-2", "request_id": "req-2",
                                         "action": {"requested_capabilities": ["process.execute"]}})
        assert decision.decide(widened, env["current"]["bundle"], STATE, NOW)["status"] == "require_approval"
        with pytest.raises(b.BrokerRefusal) as info:
            env["execute"](widened, env["effect"], NOW, grant["grant_id"])
        assert info.value.code.startswith("GRANT_MISMATCH")
        assert env["effect"].applied == []

    def test_grant_records_the_approved_capability_set(self, env, base_request):
        req = merge_patch(base_request, PUBLISH | {"action": {"requested_capabilities": ["process.execute"]}})
        assert approve(env, req)["requested_capabilities"] == ["process.execute"]

    def test_capability_order_does_not_matter(self, env, base_request):
        a = merge_patch(base_request, {"action": {"requested_capabilities": ["repository.publish", "process.execute"]}})
        c = merge_patch(base_request, {"action": {"requested_capabilities": ["process.execute", "repository.publish"]}})
        grant = approve(env, a)
        assert env["execute"](c, env["effect"], NOW, grant["grant_id"])["status"] == "observed_success"


class TestPermitReplay:
    """SR-07: a captured, still-valid permit cannot drive a second effect."""

    def capture(self, env, request):
        captured = {}

        class Capture(b.RecordingEffect):
            def apply(self, permit, req):
                captured["permit"] = permit
                return super().apply(permit, req)

        effect = Capture(env["signer"], lambda: NOW)
        env["execute"](request, effect, NOW)
        return captured["permit"], effect

    def test_direct_replay_at_same_adapter_refused(self, env, base_request):
        permit, effect = self.capture(env, base_request)
        with pytest.raises(b.UnauthorizedEffect):
            effect.apply(permit, base_request)
        assert len(effect.applied) == 1

    def test_replay_at_a_fresh_adapter_instance_refused(self, env, base_request):
        """Restart of the effect adapter: single use is recorded durably, not in adapter memory."""
        permit, _ = self.capture(env, base_request)
        restarted = b.RecordingEffect(env["signer"], lambda: NOW)
        with pytest.raises(b.UnauthorizedEffect):
            restarted.apply(permit, base_request)
        assert restarted.applied == []

    def test_concurrent_replays_apply_at_most_once(self, env, base_request):
        permit, _ = self.capture(env, base_request)
        fresh = b.RecordingEffect(env["signer"], lambda: NOW)
        barrier, outcomes = threading.Barrier(8), []

        def attempt():
            barrier.wait()
            try:
                fresh.apply(permit, base_request)
                outcomes.append("applied")
            except b.UnauthorizedEffect:
                outcomes.append("refused")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("applied") == 0


class TestAuthenticatedIngress:
    """SR-01: the broker accepts only requests sealed by a registered trusted adapter."""

    def test_unsealed_request_with_trusted_issuer_string_refused(self, env, base_request):
        assert base_request["principal"]["verified_by"] == "writ-host-adapter:fixture"
        for forged in (base_request, ingress.SealedRequest(base_request, "writ-host-adapter:fixture", "0" * 64)):
            with pytest.raises(b.BrokerRefusal) as info:
                env["broker"].execute(forged, env["effect"], NOW)
            assert info.value.code == "INGRESS_UNAUTHENTICATED"
        assert env["effect"].applied == []

    @pytest.mark.parametrize("patch", [
        {"principal": {"id": "human:admin"}},
        {"principal": {"roles": ["operator"]}},
        {"tenant_id": "tenant-b"},
        {"session_id": "session-2"},
        {"action": {"action_digest": "sha256:" + "9" * 64}},
        {"action": {"requested_capabilities": ["process.execute"]}},
    ], ids=["principal", "roles", "tenant", "session", "digest", "capabilities"])
    def test_any_change_after_sealing_refused(self, env, base_request, patch):
        sealed = SEALER.seal(base_request)
        tampered = ingress.SealedRequest(merge_patch(sealed.request, patch), sealed.adapter_id, sealed.seal)
        with pytest.raises(b.BrokerRefusal) as info:
            env["broker"].execute(tampered, env["effect"], NOW)
        assert info.value.code == "INGRESS_UNAUTHENTICATED"

    def test_seal_from_unregistered_key_refused(self, env, base_request):
        rogue = ingress.IngressSealer("writ-host-adapter:fixture", b"attacker-key")
        with pytest.raises(b.BrokerRefusal) as info:
            env["broker"].execute(rogue.seal(base_request), env["effect"], NOW)
        assert info.value.code == "INGRESS_UNAUTHENTICATED"

    def test_adapter_cannot_vouch_for_another_issuers_principal(self, env, base_request):
        other = merge_patch(base_request, {"principal": {"verified_by": "writ-host-adapter:other"}})
        with pytest.raises(b.BrokerRefusal) as info:
            env["broker"].execute(SEALER.seal(other), env["effect"], NOW)
        assert info.value.code == "INGRESS_UNAUTHENTICATED"

    def test_sealed_request_from_trusted_adapter_executes(self, env, base_request):
        assert env["broker"].execute(SEALER.seal(base_request), env["effect"], NOW)["status"] == "observed_success"


class TestPermitBindsWholeRequest:
    """SR-10: a valid, unused permit cannot be spent on a request that differs in any field."""

    def unused_permit(self, env, request):
        captured = {}

        class CaptureOnly(b.RecordingEffect):
            def apply(self, permit, req):
                captured["permit"] = permit  # keep it unused
                return "sha256:" + "0" * 64

        env["execute"](request, CaptureOnly(env["signer"], lambda: NOW), NOW)
        return captured["permit"]

    @pytest.mark.parametrize("patch", [
        {"principal": {"id": "agent:other-session", "roles": ["operator"]}},
        {"action": {"requested_capabilities": ["memory.write"]}},
        {"action": {"kind": "file.read"}},
        {"session_id": "session-2"},
        {"request_id": "req-other", "idempotency_key": "idem-other"},
    ], ids=["principal", "capabilities", "kind", "session", "request-id"])
    def test_altered_request_refused_and_permit_not_consumed(self, env, base_request, patch):
        permit = self.unused_permit(env, base_request)
        altered = merge_patch(base_request, patch)
        # Same tenant, resource and action digest as the permit: the old three-field check passes.
        assert (altered["tenant_id"], altered["action"]["resource"], altered["action"]["action_digest"]) == (
            base_request["tenant_id"], base_request["action"]["resource"], base_request["action"]["action_digest"])
        adapter = b.RecordingEffect(env["signer"], lambda: NOW)
        with pytest.raises(b.UnauthorizedEffect):
            adapter.apply(permit, altered)
        assert adapter.applied == []
        adapter.apply(permit, base_request)  # the legitimate request still succeeds
        assert adapter.applied == [base_request["action"]["action_digest"]]
