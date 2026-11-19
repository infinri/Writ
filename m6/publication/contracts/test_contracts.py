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
         "human-intent-v1", "publication-v1")


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
        del intent["content_digests"]["prerequisites"]
        self.assertFalse(validator.is_valid(intent))


if __name__ == "__main__":
    unittest.main()
