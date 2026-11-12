"""Schema-level positive and negative tests for the PROPOSED M3-06 acceptance contracts
(D-033 item 4). Semantic rules that need state (binding equality at use time, ledger-wide
single consumption, registry lookup, MAC verification) are specified in
docs/implementation/M3-ACCEPTANCE-CONTRACTS.md and are tested in code with M3-05 and M3-07.

  python3 docs/contracts/proposed/test_proposed_contracts.py
"""
import copy
import json
import unittest
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
SCHEMAS = {n: json.loads((HERE / f"{n}.schema.json").read_text())
           for n in ("evaluator-registry-entry", "runner-attestation", "evaluation-record-scope")}
VALID = {n: json.loads((HERE / "examples" / f"{n}.valid.json").read_text()) for n in SCHEMAS}
CONTEXT = ["tenant_id", "project_id", "task_id", "workflow_id", "transition", "artifact_snapshot_id", "artifact_digest",
           "canonical_target", "acceptance_policy_id", "acceptance_policy_version", "acceptance_policy_bundle_sha",
           "state_revision", "acceptance_record_id", "criterion_id"]
EVALUATOR = ["evaluator_id", "evaluator_version", "implementation_digest", "environment_digest", "registry_revision"]


def ok(name, doc):
    return jsonschema.Draft202012Validator(SCHEMAS[name]).is_valid(doc)


def mutated(name, fn):
    d = copy.deepcopy(VALID[name])
    fn(d)
    return d


class Contracts(unittest.TestCase):
    def test_schemas_are_valid_draft_2020_12_and_examples_validate(self):
        for n, s in SCHEMAS.items():
            jsonschema.Draft202012Validator.check_schema(s)
            self.assertTrue(ok(n, VALID[n]), n)

    def test_closed_objects_reject_unknown_members(self):
        for n in SCHEMAS:
            self.assertFalse(ok(n, mutated(n, lambda d: d.__setitem__("extra", 1))), n)
        self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d["auth"].__setitem__("sig", "x"))))
        self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d["transition"].__setitem__("x", "y"))))

    def test_every_context_and_evaluator_binding_is_required(self):
        for n in ("runner-attestation", "evaluation-record-scope"):
            for f in CONTEXT + EVALUATOR:
                with self.subTest(contract=n, field=f):
                    self.assertFalse(ok(n, mutated(n, lambda d: d.pop(f))))
        for f in ("command_id", "command_digest", "input_digests", "output_digest", "exit_status", "purpose", "auth",
                  "attestation_id", "fresh_until"):
            self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d.pop(f))), f)

    def test_algorithm_policy_and_tag_format(self):
        for alg in ("hmac-sha1", "ed25519", "HMAC-SHA256", "none", ""):
            self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d["auth"].__setitem__("alg", alg))), alg)
            self.assertFalse(ok("evaluator-registry-entry", mutated("evaluator-registry-entry",
                                                                    lambda d: d["attestation"].__setitem__("alg", alg))), alg)
        for mac in ("A" * 64, "a" * 63, "a" * 65, "zz" * 32, ""):
            self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d["auth"].__setitem__("mac", mac))), mac)
        self.assertFalse(ok("runner-attestation", mutated("runner-attestation", lambda d: d.__setitem__("purpose", "writ.permit.v1"))))

    def test_registry_scope_is_explicit_and_class_trust_coherent(self):
        r = "evaluator-registry-entry"
        self.assertFalse(ok(r, mutated(r, lambda d: d.pop("scope"))))
        self.assertFalse(ok(r, mutated(r, lambda d: d.__setitem__("scope", "global"))))
        self.assertFalse(ok(r, mutated(r, lambda d: d["scope"].pop("project_id"))))
        self.assertFalse(ok(r, mutated(r, lambda d: d.pop("command"))), "mechanical needs a registered command")
        self.assertFalse(ok(r, mutated(r, lambda d: d.__setitem__("trust", "MA"))), "mechanical is trust M")
        self.assertFalse(ok(r, mutated(r, lambda d: d.__setitem__("status", "revoked"))), "revoked needs revoked_at")
        self.assertTrue(ok(r, mutated(r, lambda d: d.update(status="revoked", revoked_at="2026-11-08T00:00:00Z"))))
        self.assertFalse(ok(r, mutated(r, lambda d: d.__setitem__("revoked_at", "2026-11-08T00:00:00Z"))), "active with revoked_at")
        self.assertFalse(ok(r, mutated(r, lambda d: d.__setitem__("criteria", []))))

    def test_field_types_are_strict(self):
        a = "runner-attestation"
        for f, v in (("artifact_digest", "sha256:" + "B" * 64), ("started_at", "2026-11-07T10:58:00.5Z"),
                     ("fresh_until", "2026-11-07T11:30:00+00:00"), ("exit_status", 256), ("state_revision", -1),
                     ("state_revision", 2 ** 53), ("canonical_target", "workflow:T1/p1/wf-1"), ("input_digests", []),
                     ("criterion_id", "Build")):
            self.assertFalse(ok(a, mutated(a, lambda d: d.__setitem__(f, v))), f"{f}={v!r}")

    def test_mechanical_evaluation_needs_an_attestation(self):
        e = "evaluation-record-scope"
        self.assertFalse(ok(e, mutated(e, lambda d: d.pop("attestation_ref"))))
        self.assertTrue(ok(e, mutated(e, lambda d: (d.pop("attestation_ref"), d.__setitem__("evaluator_trust", "E")))))
        self.assertFalse(ok(e, mutated(e, lambda d: d.__setitem__("mode", "verified"))))


if __name__ == "__main__":
    unittest.main()
