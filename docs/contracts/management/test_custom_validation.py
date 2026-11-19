"""Negative controls for the disposable custom-contract schema profile."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


VALIDATOR = Path(__file__).resolve().parents[3] / "m6/management/src/contract_validation.py"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def schema(**fields):
    return {"$schema": DIALECT, **fields}


def evaluate(value, **example):
    response = subprocess.run(
        [sys.executable, "-I", "-B", str(VALIDATOR)],
        input=json.dumps({"core": False, "schema": value, **example}),
        capture_output=True, check=True, text=True, timeout=10,
    )
    return json.loads(response.stdout)


class CustomValidation(unittest.TestCase):
    def test_reference_and_rfc6901_diagnostics(self):
        value = schema(type="object", required=["missing/name"], additionalProperties=False,
                       properties={"a~/b": {"$ref": "#/$defs/Count"}, "missing/name": {}},
                       **{"$defs": {"Count": {"type": "integer", "minimum": 1}}})
        self.assertEqual(evaluate(value, example={"a~/b": 2, "missing/name": None})["diagnostics"], [])
        expected = [
            {"code": "EXAMPLE_MINIMUM", "pointer": "/a~0~1b"},
            {"code": "EXAMPLE_REQUIRED", "pointer": "/missing~1name"},
            {"code": "EXAMPLE_ADDITIONAL_PROPERTIES", "pointer": "/new~0key"},
        ]
        self.assertEqual(evaluate(value, example={"a~/b": 0, "new~key": 1})["diagnostics"], expected)
        self.assertEqual(evaluate(value, example={"new~key": 1, "a~/b": 0})["diagnostics"], expected)

    def test_cycles_and_unapproved_references(self):
        for ref in ("https://example.invalid/schema", "file:///tmp/schema", "../schema.json",
                    "#/$defs/unlisted", "#/$defs/Name/type", "#/$defs/%4Eame"):
            with self.subTest(ref=ref):
                definitions = {"Name": {"type": "integer"}}
                result = evaluate(schema(**{"$defs": definitions, "$ref": ref}))
                self.assertEqual(result["diagnostics"], [
                    {"code": "UNAPPROVED_REFERENCE", "pointer": "/$ref"},
                ])
        cyclic = {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}
        self.assertEqual(evaluate(schema(**{"$defs": cyclic}))["diagnostics"], [
            {"code": "REFERENCE_CYCLE", "pointer": "/$defs/B/$ref"},
        ])

    def test_refuses_incomplete_validation(self):
        for keyword, value in (
            ("pattern", "(a+)+$"), ("format", "uri"), ("allOf", [{}]),
            ("unevaluatedProperties", False), ("$dynamicRef", "#anchor"), ("$id", "file:///tmp/x"),
        ):
            with self.subTest(keyword=keyword):
                invalid = schema(properties={"a/b": {keyword: value}})
                self.assertEqual(evaluate(invalid)["diagnostics"], [
                    {"code": "UNSUPPORTED_KEYWORD", "pointer": f"/properties/a~1b/{keyword}"},
                ])
        self.assertEqual(evaluate(schema(type="object", required=["item", "item"]))["diagnostics"], [
            {"code": "INVALID_SCHEMA", "pointer": "/required"},
        ])
        self.assertEqual(evaluate({"type": "object"})["diagnostics"], [
            {"code": "UNSUPPORTED_DIALECT", "pointer": "/$schema"},
        ])

    def test_limits_refuse_deep_documents_diagnostic_floods_and_reference_expansion(self):
        example = None
        for _ in range(33):
            example = [example]
        self.assertEqual(evaluate(schema(), example=example)["diagnostics"][0]["code"], "DOCUMENT_LIMIT")
        invalid = {f"extra-{i}": i for i in range(33)}
        self.assertEqual(evaluate(schema(additionalProperties=False), example=invalid)["diagnostics"], [
            {"code": "DIAGNOSTIC_LIMIT", "pointer": ""},
        ])
        definitions = {"A0": {}}
        for i in range(1, 9):
            ref = {"$ref": f"#/$defs/A{i - 1}"}
            definitions[f"A{i}"] = {"properties": {"x": ref, "y": ref}}
        expanded = schema(**{"$defs": definitions, "$ref": "#/$defs/A8"})
        self.assertEqual(evaluate(expanded)["diagnostics"][0]["code"], "SCHEMA_LIMIT")


if __name__ == "__main__":
    unittest.main()
