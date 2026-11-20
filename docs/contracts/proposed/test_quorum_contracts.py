"""Structural compatibility and domain separation for the D-061 extensions."""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text())


class QuorumContracts(unittest.TestCase):
    def validator(self, base, extended):
        original = load(base + ".schema.json")
        schema = load(extended + ".schema.json")
        Draft202012Validator.check_schema(schema)
        registry = Registry().with_resource(original["$id"], Resource.from_contents(original))
        return Draft202012Validator(schema, registry=registry)

    def test_member_versions_and_purpose(self):
        validator = self.validator("runner-attestation", "runner-attestation-2")
        member = load("examples/runner-attestation.valid.json")
        self.assertFalse(validator.is_valid(member))
        member.update(contract_version="runner-attestation/2", attempt_id="attempt", verdict="pass")
        validator.validate(member)
        del member["command_id"]
        self.assertFalse(validator.is_valid(member))
        del member["command_digest"]
        validator.validate(member)
        for change in ({"attempt_id": ""}, {"purpose": "writ.acceptance-record.v1"},
                       {"verdict": "unverified"}, {"exit_status": 1}):
            self.assertFalse(validator.is_valid({**member, **change}), change)
        validator.validate({**member, "verdict": "fail", "exit_status": 1})

    def test_record_limits(self):
        validator = self.validator("authenticated-acceptance-record-2", "authenticated-acceptance-record-3")
        envelope = load("examples/authenticated-acceptance-record-2.valid.json")
        self.assertFalse(validator.is_valid(envelope))
        member = dict(criterion_id="build", evaluator_id="test", attestation_id="att")
        envelope.update(contract_version="authenticated-acceptance-record/3", attempt_id="attempt",
                        contributions=[member])
        validator.validate(envelope)
        for change in ({"contributions": []}, {"contributions": [member] * 257},
                       {"contract_version": "authenticated-acceptance-record/4"},
                       {"purpose": "writ.runner-attestation.v1"}):
            self.assertFalse(validator.is_valid({**envelope, **change}), change)
