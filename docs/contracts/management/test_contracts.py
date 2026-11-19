"""Positive and negative fixtures for the closed /manage/v1 policy envelopes."""

import json
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
                *PUBLICATION.glob("*.schema.json"), HERE.parent / "policy-bundle.schema.json"
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
        for item in (
            request("preview_publish", **publish),
            request("publish", **publish, **authorization),
            request("preview_rollback", **common),
            request("rollback", **common, **authorization),
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
        for item in (preview, receipt | {"intent_digest": DIGEST}, history):
            self.response.validate(response(item))
        for item in (
            preview | {"intent": intent | {"operation": "retire"}},
            preview | {"context": context | {"self_publication_acknowledged": "yes"}},
            preview | {"context": {key: val for key, val in context.items() if key != "proof_digest"}},
            receipt | {"intent_digest": DIGEST, "presentation": "private"},
            history | {"selected": receipt},
        ):
            self.assertFalse(self.response.is_valid(response(item)))
