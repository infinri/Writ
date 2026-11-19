"""Positive and negative fixtures for the closed /manage/v1 draft envelopes."""

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


HERE = Path(__file__).parent
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
        registry = Registry().with_resource(decision_schema["$id"], Resource.from_contents(decision_schema))
        for schema in (request_schema, response_schema):
            Draft202012Validator.check_schema(schema)
        cls.request = Draft202012Validator(request_schema)
        cls.response = Draft202012Validator(response_schema, registry=registry)
        fixture = HERE.parents[2] / "m6" / "compiler" / "fixtures" / "permit.cases.json"
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
