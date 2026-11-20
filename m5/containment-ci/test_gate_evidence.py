import hashlib
import json
from pathlib import Path
import unittest
from gate_evidence import validate

MANIFEST = Path(__file__).with_name("required-results.json").read_bytes()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.binding = dict(source_sha="a" * 40, source_archive_sha256="b" * 64,
                            image_sha256="c" * 64, manifest_sha256=hashlib.sha256(MANIFEST).hexdigest(),
                            challenge="d" * 64, run_id=100, attempt=1)
        self.results = dict.fromkeys(json.loads(MANIFEST), {"outcome": "pass", "exit_code": 0})
        self.record = {**self.binding, "results": self.results}

    def check(self):
        validate(json.dumps(self.record).encode(), self.binding, MANIFEST)

    def test_exact_result_set(self):
        self.check()
        self.results.pop("P-KILL-CONTROL")
        with self.assertRaises(ValueError):
            self.check()

    def test_replayed_or_wrongly_typed_identity(self):
        for key, value in [("attempt", True), ("source_sha", "1" * 40), ("challenge", "0" * 64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.record = {**self.binding, key: value, "results": self.results}
                self.check()

    def test_skipped_and_wrong_exit(self):
        for outcome, rc in [("missing", 0), ("pass", False), ("pass", 3), ("inconclusive", 0)]:
            self.results["P-KILL-CONTROL"] = {"outcome": outcome, "exit_code": rc}
            with self.subTest(outcome=outcome, rc=rc), self.assertRaises(ValueError):
                self.check()

    def test_duplicate_extra_and_oversized(self):
        valid = json.dumps(self.record).encode()
        for raw in [valid.replace(b'"attempt": 1', b'"attempt": 1, "attempt": 1'),
                    valid.replace(b'"results":', b'"log": "", "results":'), valid + b" " * 65536]:
            with self.assertRaises(ValueError):
                validate(raw, self.binding, MANIFEST)

    def test_unapproved_manifest(self):
        with self.assertRaises(ValueError):
            validate(json.dumps(self.record).encode(), self.binding, MANIFEST + b" ")


if __name__ == "__main__":
    unittest.main()
