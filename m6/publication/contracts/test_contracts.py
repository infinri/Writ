"""Exercise the standalone D-051 schemas and cross-document references."""

import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
NAMES = ("detached-signature-v1", "authorization", "acceptance-policy-v1",
         "acceptance-policy-v2", "human-intent-v1", "publication-v1", "waiver-context-v1")


def load(path):
    return json.loads(path.read_text())


def vector(name):
    data = load(HERE / name)
    return {key: data[key] for key in ("payload", "envelope")}


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sources = {
            "authorization": ROOT / "docs/contracts/policy-bundle.schema.json",
            **{name: HERE / f"{name}.schema.json" for name in NAMES if name != "authorization"},
        }
        sources["waiver-context-v1"] = ROOT / "m6/waivers/waiver-context-v1.schema.json"
        cls.schemas = {name: load(path) for name, path in sources.items()}
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()
        )
        cls.validators = {
            name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
            for name, schema in cls.schemas.items()
        }

    def setUp(self):
        self.publication = {
            "version": "writ-policy-publication/1",
            "authorization": vector("authorization-signing-vector.json"),
            "acceptance": vector("signing-vector.json"),
            "prerequisites": {
                "version": "writ-policy-prerequisites/1",
                "draft": load(ROOT / "m6/compiler/fixtures/permit.input.json"),
                "proof_digest": "sha256:" + "1" * 64,
            },
        }

    def test_schema_and_vectors(self):
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
        self.validators["publication-v1"].validate(self.publication)

    def test_slot_and_closed_field_controls(self):
        bad = []
        document = copy.deepcopy(self.publication)
        document["authorization"]["envelope"] = document["acceptance"]["envelope"]
        bad.append(document)
        document = copy.deepcopy(self.publication)
        document["authorization"]["payload"]["publisher_signature"] = None
        bad.append(document)
        document = copy.deepcopy(self.publication)
        document["acceptance"]["envelope"]["protected"]["key"] = "inline-root"
        bad.append(document)
        document = copy.deepcopy(self.publication)
        document["acceptance"]["payload"]["criteria"][0]["required_class"] = "model_assisted"
        bad.append(document)
        for index, document in enumerate(bad):
            with self.subTest(case=index):
                self.assertFalse(self.validators["publication-v1"].is_valid(document))

    def test_waiver_policy_requires_closed_origin_and_matched_format(self):
        publication = copy.deepcopy(self.publication)
        acceptance = publication["acceptance"]
        acceptance["payload"]["version"] = "writ-acceptance-policy/2"
        acceptance["payload"]["criteria"] = [
            {"criterion_id": "review", "required_class": "human",
             "origin": "invariant", "max_waiver_seconds": 0},
            {"criterion_id": "waiver-review", "required_class": "mechanical",
             "origin": "project", "max_waiver_seconds": 300},
        ]
        validator = self.validators["publication-v1"]
        self.assertFalse(validator.is_valid(publication))
        acceptance["envelope"]["protected"]["payload_format"] = "writ-acceptance-policy/2"
        validator.validate(publication)
        for position, change in [
            (0, {"origin": "project"}),
            (0, {"max_waiver_seconds": 1}),
            (1, {"origin": "invariant"}),
            (1, {"max_waiver_seconds": 601}),
            (1, {"extra": False}),
        ]:
            altered = copy.deepcopy(publication)
            altered["acceptance"]["payload"]["criteria"][position].update(change)
            self.assertFalse(validator.is_valid(altered), change)

    def test_waiver_context_and_purpose_are_closed(self):
        digest = "sha256:" + "0" * 64
        requester = {"issuer": "controlled-ingress", "subject": "requester"}
        context = {
            "purpose": "writ-human-criterion-waiver/1",
            "tenant_id": "tenant", "project_id": "project", "task_id": "task",
            "workflow_id": "workflow", "from_state": "review", "to_state": "done",
            "expected_revision": 1, "criterion_id": "waiver-review",
            "artifact_snapshot_id": "snapshot", "artifact_digest": digest,
            "workflow_authorization_digest": digest, "requester": requester,
            "policy": {
                "tenant_id": "tenant", "project_id": "project", "activation_revision": 1,
                "authorization_digest": digest, "acceptance_digest": digest,
                "publication_digest": digest,
            },
            "exclusions": {
                "added": [], "agents": [], "artifact_changers": [],
                "linked_subjects": [], "policy_changers": [], "requesters": [requester],
                "delegation_chain": "unavailable",
            },
            "reason": "Synthetic review case", "expires_at": "2026-11-19T12:05:00Z",
        }
        validator = self.validators["waiver-context-v1"]
        validator.validate(context)
        for change in [
            {"purpose": "writ-human-policy-publisher/1"},
            {"purpose": "writ-human-criterion-waiver/2"},
            {"tenant_id": "*"},
            {"expires_at": "2026-11-19T12:05:00+00:00"},
            {"reason": "  "},
            {"status": "passed"},
        ]:
            self.assertFalse(validator.is_valid({**context, **change}), change)
        for key in context:
            altered = context.copy()
            del altered[key]
            self.assertFalse(validator.is_valid(altered), key)

    def test_exact_human_approval_intent(self):
        digest = "sha256:" + "0" * 64
        intent = {
            "purpose": "writ-human-policy-publisher/1",
            "scope": self.publication["authorization"]["envelope"]["protected"]["scope"],
            "operation": "publish",
            "expected_revision": 0,
            "publication_version": 1,
            "publication_digest": digest,
            "content_digests": dict.fromkeys(("authorization", "acceptance", "prerequisites"), digest),
        }
        validator = self.validators["human-intent-v1"]
        validator.validate(intent)
        managed = {"digest": digest, "publisher": "fixture-publisher"}
        retire = {**intent, "operation": "retire", "confirmation": "retire 1"}
        validator.validate(retire)
        validator.validate({**retire, "management": managed})
        self.assertFalse(validator.is_valid({**intent, "operation": "retire", "management": managed}))
        self.assertFalse(validator.is_valid({**retire, "confirmation": "retire 01"}))
        self.assertFalse(validator.is_valid({**retire, "confirmation": "retire 1\n"}))
        self.assertFalse(validator.is_valid({**retire, "operation": "rollback"}))
        del intent["content_digests"]["prerequisites"]
        self.assertFalse(validator.is_valid(intent))


if __name__ == "__main__":
    unittest.main()
