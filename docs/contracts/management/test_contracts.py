"""Positive and negative fixtures for the closed /manage/v1 policy envelopes."""

import json
from copy import deepcopy
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PUBLICATION = ROOT / "m6" / "publication" / "contracts"
DIGEST = "sha256:" + "1" * 64

SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}
DRAFT = {"id": "candidate", "revision": 1}
MUTATION = {"id": "candidate", "expected_revision": 0, "idempotency_key": "intent", "preview": False}
CORPUS = [{"kind": "synthetic.inspect", "resource": "synthetic:m6-lab/offline-proof/inventory"}]


def request(operation, **fields):
    return {"version": "/manage/v1", "scope": SCOPE, "operation": operation, **fields}


def response(result):
    return {"version": "/manage/v1", "ok": True, "result": result}


class DraftContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        request_schema = json.loads((HERE / "draft-request.schema.json").read_text())
        response_schema = json.loads((HERE / "draft-response.schema.json").read_text())
        decision_schema = json.loads((HERE.parent / "decision.schema.json").read_text())
        authority_schemas = [
            json.loads(path.read_text()) for path in (
                *PUBLICATION.glob("*.schema.json"),
                HERE.parent / "policy-bundle.schema.json",
                HERE / "pack-envelope.schema.json",
                HERE.parent / "capability-pack.schema.json",
                HERE.parent / "proposed/evaluator-registry-entry.schema.json",
                HERE.parent / "proposed/pack-rubric-binding.schema.json",
                ROOT / "m6/waivers/waiver-context-v1.schema.json",
            )
        ]
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in [decision_schema, *authority_schemas]
        )
        for schema in (request_schema, response_schema):
            Draft202012Validator.check_schema(schema)
        cls.request = Draft202012Validator(request_schema, registry=registry)
        cls.response = Draft202012Validator(response_schema, registry=registry)
        fixture = ROOT / "m6" / "compiler" / "fixtures" / "permit.cases.json"
        cls.decision = json.loads(fixture.read_text())[0]["expected"]

    def test_valid_requests(self):
        for item in (
            request("create", **MUTATION, candidate={}),
            request("edit", **(MUTATION | {"expected_revision": 1}), candidate={}),
            request("clone", **MUTATION, source=DRAFT),
            request("get", draft=DRAFT),
            request("history", id="candidate", after=0, limit=50),
            request("validate", draft=DRAFT),
            request("simulate", draft=DRAFT, corpus=CORPUS),
            request("compare", before=DRAFT, after=DRAFT, corpus=CORPUS),
        ):
            with self.subTest(operation=item["operation"]):
                self.request.validate(item)

    def test_custom_contract_requests(self):
        target = {"contract_id": "tenant:m6-lab:sample", "contract_version": "2.0.0"}
        changed = target | {"expected_revision": 2}
        replacement = {"replacement_version": "3.0.0", "end_date": "2026-11-19"}
        proof = {"intent_digest": DIGEST, "presentation": "opaque", "idempotency_key": "first"}
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "integer"}
        for item in (
            request("contracts.browse"),
            request("contracts.inspect", contract_id="action-request"),
            request("contracts.validate-schema", schema=schema),
            request("contracts.validate-example", **target, example=None),
            request("contracts.create", **changed, idempotency_key="first", schema=schema),
            request("contracts.preview", change="publish", **changed, acknowledge_self_publication=True),
            request("contracts.preview", change="deprecate", **changed, **replacement),
            request("contracts.preview", change="retire", **changed),
            request("contracts.publish", **changed, **proof),
            request("contracts.deprecate", **changed, **replacement, **proof, confirmation="typed"),
            request("contracts.retire", **changed, **proof, confirmation="typed"),
            request("contracts.recover", idempotency_key="first"),
        ):
            with self.subTest(operation=item["operation"]):
                self.request.validate(item)
        for item in (
            request("contracts.browse", role="Publisher"),
            request("contracts.validate-example", **target),
            request("contracts.preview", change="publish", **changed, **proof),
            request("contracts.preview", change="deprecate", **changed),
            request("contracts.deprecate", **changed, **replacement, **proof),
            request("contracts.retire", **changed, **proof, depends_on=[], confirmation="typed"),
            request("contracts.publish", **changed, **proof, grant="permission"),
            request("contracts.publish", **changed, **proof, acknowledge_self_publication="yes"),
        ):
            with self.subTest(operation=item["operation"]):
                self.assertFalse(self.request.is_valid(item))

    def test_invalid_requests(self):
        for item in (
            request("create", **MUTATION, candidate={}, role="PolicyAuthor"),
            request("create", **(MUTATION | {"expected_revision": 1}), candidate={}),
            request("edit", **MUTATION, candidate={}),
            request("edit", **(MUTATION | {"expected_revision": 1}), candidate=[]),
            request("clone", **MUTATION, source=DRAFT | {"scope": SCOPE}),
            request("get", draft=DRAFT) | {"scope": SCOPE | {"owner": "speaker"}},
            request("get", draft=DRAFT) | {"version": "/manage/v2"},
            request("history", id="candidate", after=0, limit=51),
            request("simulate", draft=DRAFT, corpus=CORPUS * 17),
            request("simulate", draft=DRAFT, corpus=[]),
            request("activate", draft=DRAFT),
        ):
            with self.subTest(operation=item["operation"]):
                self.assertFalse(self.request.is_valid(item))

    def test_valid_responses(self):
        metadata = DRAFT | {"digest": "sha256:" + "a" * 64, "source": None}
        for item in (
            response({"revision": 1, "audit_id": 1}),
            response({"revision": 1, "audit_id": None}),
            response(metadata),
            response({"versions": [metadata], "next": None}),
            response({
                "compilable": False,
                "offline_proven": False,
                "deployed_enforcement": "UNVERIFIED",
                "runtime_authority": False,
                "diagnostics": [{"code": "BAD_COMPANION", "pointer": "/companions/0"}],
            }),
            response({"runtime_authority": False, "cases": [{"case": 0, "decision": self.decision}]}),
            response({
                "runtime_authority": False,
                "structure": ["/companions/0"],
                "cases": [{"case": 0, "changed": [], "before": self.decision, "after": self.decision}],
            }),
            {
                "version": "/manage/v1", "ok": False,
                "error": {"code": "NOT_FOUND", "message": "Unavailable in this scope.", "pointer": ""},
            },
        ):
            with self.subTest(item=item):
                self.response.validate(item)

    def test_invalid_responses(self):
        for item in (
            response({"revision": 1, "audit_id": 0}),
            response({"runtime_authority": True, "cases": []}),
            response({"runtime_authority": False, "cases": [{"case": 0, "decision": {"status": "allow"}}]}),
            response({"runtime_authority": False, "structure": ["/bad~pointer"], "cases": []}),
            {
                "version": "/manage/v1", "ok": False,
                "error": {"code": "UNAVAILABLE", "message": "Unavailable.", "pointer": "", "stack": "private"},
            },
        ):
            with self.subTest(item=item):
                self.assertFalse(self.response.is_valid(item))


    def test_publication_requests(self):
        def signed(name):
            vector = json.loads((PUBLICATION / name).read_text())
            return {field: vector[field] for field in ("payload", "envelope")}

        pair = {
            "version": "writ-policy-publication/1",
            "authorization": signed("authorization-signing-vector.json"),
            "acceptance": signed("signing-vector.json"),
            "prerequisites": {
                "version": "writ-policy-prerequisites/1",
                "draft": json.loads(
                    (ROOT / "m6/compiler/fixtures/permit.input.json").read_text()
                ),
                "proof_digest": DIGEST,
            },
        }
        common = {
            "expected_revision": 1, "publication_version": 2,
            "acknowledge_self": False, "idempotency_key": "selection-2",
        }
        publish = common | {"draft": DRAFT, "publication": pair}
        authorization = {"intent_digest": DIGEST, "presentation": "opaque"}
        retirement = common | {"confirmation": "retire 2"}
        deactivation = {
            "expected_revision": 2, "publication_version": 2, "policy_id": "m6.synthetic.permit",
            "confirmation": "deactivate m6.synthetic.permit",
        }
        for item in (
            request("preview_publish", **publish),
            request("publish", **publish, **authorization),
            request("preview_rollback", **common),
            request("rollback", **common, **authorization),
            request("preview_retire", **retirement),
            request("retire", **retirement, **authorization),
            request("preview_deactivate", **deactivation),
            request("deactivate", **deactivation),
            request("publication_history", after=0, limit=50),
        ):
            self.request.validate(item)
        for item in (
            request("preview_publish", **publish, **authorization),
            request("publish", **publish, **authorization, role="Publisher"),
            request("publish", **publish, **(authorization | {"presentation": ""})),
            request("rollback", **common, **(authorization | {"intent_digest": "unbound"})),
            request("rollback", **common),
            request("preview_rollback", **(common | {"publication_version": 1000000000})),
            request("publication_history", after=-1, limit=50),
            request("retire", **common, **authorization),
            request("retire", **retirement),
            request("preview_retire", **retirement, **authorization),
            request("rollback", **retirement, **authorization),
            request("deactivate", **deactivation, **authorization),
            request("preview_deactivate", **(deactivation | {"policy_id": []})),
        ):
            with self.subTest(operation=item["operation"]):
                self.assertFalse(self.request.is_valid(item))

    def test_publication_responses(self):
        context = {
            "version": "writ-managed-publication-context/1", "scope": SCOPE,
            "publisher": "human-publisher", "draft": DRAFT, "draft_digest": DIGEST,
            "draft_head_revision": 1, "proof_digest": DIGEST, "idempotency_key_digest": DIGEST,
            "self_publication_acknowledged": False,
        }
        intent = {
            "purpose": "writ-human-policy-publisher/1", "operation": "rollback", "scope": SCOPE,
            "management": {"digest": DIGEST, "publisher": "human-publisher"},
            "publication_digest": DIGEST, "expected_revision": 2, "publication_version": 1,
            "content_digests": dict.fromkeys(("authorization", "acceptance", "prerequisites"), DIGEST),
        }
        selection = {"revision": 3, "publication_version": 1}
        receipt = selection | {"audit_id": 3}
        preview = selection | {"intent": intent, "context": context, "intent_digest": DIGEST}
        event = receipt | {
            "operation": "rollback", "publication_digest": DIGEST, "intent_digest": DIGEST,
            "human": "human-publisher", "at": "2026-11-19T12:00:00Z", "context": context,
        }
        history = {"events": [event], "next": None, "selected": receipt | {"verified": True}}
        retirement_context = context | {"confirmation": "retire 1"}
        standalone_context = {
            key: value for key, value in retirement_context.items()
            if key not in ("draft", "draft_digest", "draft_head_revision")
        }
        retirement_preview = preview | {
            "revision": 2, "intent": intent | {"operation": "retire", "confirmation": "retire 1"},
            "context": retirement_context,
        }
        retirement_history = history | {
            "events": [event | {"operation": "retire", "context": standalone_context}],
        }
        for item in (
            preview, receipt | {"intent_digest": DIGEST}, history, retirement_preview,
            retirement_preview | {"context": standalone_context}, retirement_history,
        ):
            self.response.validate(response(item))
        for item in (
            preview | {"intent": intent | {"operation": "retire"}},
            retirement_preview | {"context": context},
            retirement_preview | {"context": standalone_context | {"draft": DRAFT}},
            retirement_preview | {"context": standalone_context | {"self_publication_acknowledged": True}},
            preview | {"context": context | {"self_publication_acknowledged": "yes"}},
            preview | {"context": {key: val for key, val in context.items() if key != "proof_digest"}},
            receipt | {"intent_digest": DIGEST, "presentation": "private"},
            history | {"selected": receipt},
        ):
            self.assertFalse(self.response.is_valid(response(item)))

    def test_policy_lifecycle_refusals(self):
        for code in ("ACTIVE_DEPENDENTS", "UNSUPPORTED", "UNAVAILABLE", "INVALID_POLICY"):
            self.response.validate({
                "version": "/manage/v1", "ok": False,
                "error": {"code": code, "message": "The lifecycle operation was refused.", "pointer": ""},
            })

    def test_evaluator_and_waiver_contracts(self):
        entry = json.loads(
            (HERE.parent / "proposed/examples/evaluator-registry-entry.valid.json").read_text()
        )
        waiver = {"waiver_id": DIGEST, "transition_ref": "fixture-transition"}
        authorize = {"intent_digest": DIGEST, "idempotency_key": "publish", "presentation": "opaque"}
        finish = {
            **waiver, "challenge_id": "challenge", "idempotency_key": "ceremony",
            "dispatch_key": "record", "presentation": "opaque",
        }
        for item in (
            request("evaluator_preview_publish", expected_revision=11, entry=entry),
            request("evaluator_publish", expected_revision=11, entry=entry, **authorize),
            request("evaluator_preview_revoke", expected_revision=12, evaluator_id="ci-runner"),
            request("evaluator_recover", idempotency_key="publish"),
            request("waiver_begin", **waiver, approver_issuer="fixture-human",
                    approver_subject="fixture-approver", idempotency_key="ceremony"),
            request("waiver_finish", **finish),
            request("waiver_inspect", waiver_id=DIGEST),
            request("waiver_validate", **waiver),
            request("waiver_recover", dispatch_key="record"),
        ):
            self.request.validate(item)
        for item in (
            request("evaluator_preview_publish", expected_revision=11, entry=entry, **authorize),
            request("evaluator_publish", expected_revision=11, entry=entry),
            request("evaluator_inspect", evaluator_id="ci-runner", after=0, limit=51),
            request("waiver_begin", **waiver, approver_issuer="operator",
                    approver_subject="fixture-approver", idempotency_key="ceremony", excluded=[]),
            request("waiver_finish", **(finish | {"transition_ref": {}})),
            request("waiver_finish", **finish, acceptance_record={}),
            request("waiver_recover", idempotency_key="record"),
        ):
            self.assertFalse(self.request.is_valid(item), item)

        receipt = {
            "revision": 13, "sequence": 44, "evaluator_id": "ci-runner", "status": "revoked",
            "intent_digest": DIGEST, "entry_digest": DIGEST, "management_correlation": DIGEST,
        }
        context = {
            "purpose": "writ-human-criterion-waiver/1", "tenant_id": "t1", "project_id": "p1",
            "task_id": "task", "workflow_id": "workflow", "from_state": "review",
            "to_state": "accepted", "expected_revision": 1, "criterion_id": "review",
            "artifact_snapshot_id": "snapshot", "artifact_digest": DIGEST,
            "workflow_authorization_digest": DIGEST,
            "requester": {"issuer": "fixture", "subject": "requester"},
            "policy": {
                "tenant_id": "t1", "project_id": "p1", "authorization_digest": DIGEST,
                "acceptance_digest": DIGEST, "activation_revision": 1, "publication_digest": DIGEST,
            },
            "reason": "Fixture exception", "expires_at": "2026-11-19T12:00:00Z",
            "exclusions": {
                **dict.fromkeys(
                    ("added", "agents", "artifact_changers", "linked_subjects", "policy_changers"), [],
                ),
                "delegation_chain": "unavailable",
                "requesters": [{"issuer": "fixture", "subject": "requester"}],
            },
        }
        historical = {
            "disposition": "historical_receipt", "result": "waived", "context": context,
            "approver_issuer": "fixture", "approver_subject": "approver",
            "challenge_id": "challenge", "recorded_at": "2026-11-19T11:59:30Z", "ledger_sequence": 44,
        }
        present = {"waiver_id": DIGEST, "result": "waived", "context": context, "currently_valid": True}
        for item in (receipt, historical, present, {
            "waiver_id": DIGEST, "context": context, "historical_receipt": historical,
        }):
            self.response.validate(response(item))
        for item in (
            receipt | {"presentation": "protected"},
            historical | {"currently_valid": True},
            present | {"result": "satisfied"},
        ):
            self.assertFalse(self.response.is_valid(response(item)), item)

    @staticmethod
    def pack_envelope():
        binding = json.loads(
            (HERE.parent / "proposed/examples/pack-rubric-binding.valid.json").read_text()
        )
        binding.update(
            pack_id="sample-pack", pack_version="1.0.0", rubric_version="1.0.0",
            tenant_id=SCOPE["tenant"], project_id=SCOPE["project"],
        )
        contract = {"id": "tenant:m6-lab:pack-report", "version": "1.0.0", "digest": DIGEST}
        return {
            "signature": "a" * 128,
            "payload": {
                "purpose": "writ-capability-pack-signature/1",
                "scope": SCOPE.copy(),
                "compatibility": "2.0",
                "key_id": "fixture-pack-signature",
                "manifest": {
                    "contract_version": "2.0", "pack_id": "sample-pack", "version": "1.0.0",
                    "publisher": "fixture-pack-publisher", "domain": "review",
                    "signature_ref": "fixture-pack-signature", "task_types": ["document-review"],
                    "supported_modes": ["external_host"], "tool_capabilities": ["fs.read"],
                    "workflows": [{
                        "workflow_id": "document-review", "task_type": "document-review",
                        "steps": ["review", "request-signoff", "record"],
                        "human_gates": ["review-signoff"],
                    }],
                    "artifacts": [{
                        "artifact_type": "report", "schema_ref": f"{contract['id']}@{contract['version']}",
                    }],
                    "evaluators": [{
                        "evaluator_id": binding["evaluator_id"], "kind": binding["class"],
                        "rubric_version": binding["rubric_version"], "blocking": False,
                    }],
                    "memory": {"read_types": [], "write_types": [], "retention_class": "ephemeral"},
                    "limits": {"max_tool_calls": 1, "max_duration_seconds": 60, "max_model_calls": 0},
                },
                "contracts": [contract],
                "rubrics": [{"binding": binding, "entry_digest": DIGEST}],
            },
        }

    def test_pack_requests(self):
        target = {"pack_id": "sample-pack", "pack_version": "1.0.0"}
        change = target | {"expected_revision": 1}
        envelope = self.pack_envelope()
        proof = {"intent_digest": DIGEST, "presentation": "opaque", "idempotency_key": "first"}
        install = request("packs.install", **change, envelope=envelope, **proof)

        for operation in ("install", "enable", "disable", "upgrade"):
            fields = change | ({"envelope": envelope} if operation == "install" else {})
            confirmation = (
                {"confirmation": f"{operation} sample-pack@1.0.0 from 1.0.0"}
                if operation in ("disable", "upgrade") else {}
            )
            with self.subTest(operation=operation):
                self.request.validate(request("packs.preview", pack_operation=operation, **fields))
                self.request.validate(request(f"packs.{operation}", **fields, **proof, **confirmation))
        for item in (
            request("packs.verify", **target),
            request("packs.inspect", **target),
            request("packs.history", pack_id=target["pack_id"]),
            request("packs.recover", idempotency_key=proof["idempotency_key"]),
        ):
            with self.subTest(operation=item["operation"]):
                self.request.validate(item)

        for item in (
            request("packs.preview", pack_operation="install", **change),
            request("packs.preview", pack_operation="install", **change, envelope=envelope, **proof),
            request("packs.preview", pack_operation="verify", **change),
            request("packs.preview", pack_operation="enable", **change, envelope=envelope),
            request("packs.install", **change, envelope=envelope),
            request("packs.enable", **change, **proof, confirmation="unexpected"),
            request("packs.disable", **change, **proof),
            request("packs.upgrade", **change, **proof, confirmation=""),
            request("packs.history", **target),
            request("packs.recover", **target, idempotency_key=proof["idempotency_key"]),
            install | {"pack_version": "01.0.0"},
            install | {"expected_revision": 513},
            install | {"presentation": ""},
            install | {"role": "Publisher"},
        ):
            with self.subTest(operation=item["operation"], fields=item.keys()):
                self.assertFalse(self.request.is_valid(item))

    def test_pack_envelope_rejects_malformed_nested_values(self):
        install = request(
            "packs.preview", pack_operation="install", pack_id="sample-pack",
            pack_version="1.0.0", expected_revision=0,
        )
        envelope = self.pack_envelope()
        self.request.validate(install | {"envelope": envelope})

        for path, value in (
            (("signature",), "A" * 128),
            (("payload", "purpose"), "writ-human-capability-pack-admin/1"),
            (("payload", "scope", "role"), "Publisher"),
            (("payload", "compatibility"), "3.0"),
            (("payload", "manifest", "extra"), True),
            (("payload", "manifest", "task_types"), []),
            (("payload", "contracts", 0, "digest"), "unpinned"),
            (("payload", "rubrics", 0, "entry_digest"), "unpinned"),
            (("payload", "rubrics", 0, "binding", "registry_revision"), 0),
            (("payload", "rubrics", 0, "binding", "class"), "unknown"),
        ):
            altered = deepcopy(envelope)
            parent = altered
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            with self.subTest(path=path):
                self.assertFalse(self.request.is_valid(install | {"envelope": altered}))

    def test_pack_responses(self):
        identity = {
            "operation": "install", "scope": SCOPE, "subject": "fixture-publisher",
            "pack_id": "sample-pack", "pack_version": "1.0.0", "envelope_digest": DIGEST,
            "registry_identity": "a" * 32,
        }
        preview = {
            "intent_digest": DIGEST,
            "intent": identity | {
                "purpose": "writ-human-capability-pack-admin/1", "expected_revision": 0,
                "active_version": None, "confirmation": None, "management_identity": "b" * 32,
            },
        }
        receipt = identity | {
            "revision": 1, "previous_active_version": None, "intent_digest": DIGEST,
            "ceremony": DIGEST, "at": "2026-11-19T12:00:00Z",
        }
        inspection = {"revision": 1, "active_version": None, "envelope": self.pack_envelope()}
        verification = {"verified": True, "revision": 1, "envelope_digest": DIGEST}
        history = {"revision": 1, "active_version": None, "history": [receipt]}

        for item in (preview, receipt, inspection, verification, history):
            with self.subTest(fields=item.keys()):
                self.response.validate(response(item))
        for code in ("UNAUTHORIZED", "NOT_FOUND", "INVALID_PACK", "PACK_CONFLICT", "UNSUPPORTED",
                     "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT"):
            with self.subTest(code=code):
                self.response.validate({
                    "version": "/manage/v1", "ok": False,
                    "error": {"code": code, "message": "The pack operation was refused.", "pointer": ""},
                })
        for item in (
            preview | {"intent": preview["intent"] | {"purpose": "writ-capability-pack-signature/1"}},
            receipt | {"presentation": "opaque"},
            inspection | {"envelope": inspection["envelope"] | {"signature": ""}},
            verification | {"verified": False},
            history | {"history": [receipt | {"envelope_digest": "unbound"}]},
        ):
            with self.subTest(fields=item.keys()):
                self.assertFalse(self.response.is_valid(response(item)))
