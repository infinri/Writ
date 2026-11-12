"""The M2-06 differential gate tolerates only the classified mismatch groups."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import differential  # noqa: E402


def report(clusters, invariants=None):
    return {"campaigns": {"json": {"mismatch_clusters": clusters, "invariants": invariants or {}}}}


class Gate(unittest.TestCase):
    def test_classified_groups_pass(self):
        r = report({"transition-scenario: differs": {"count": 375, "first_case_id": "x@transition-scenario"},
                    "broker-scenario: differs": {"count": 375, "first_case_id": "x@broker-scenario"},
                    "canonical: differs in answers": {"count": 1, "first_case_id": "envelope-deep_envelope"}},
                   {"json.cross_command.transition-scenario": {"rust": {"pass": 0, "fail": 375}},
                    "differential.exact_output": {"both": {"pass": 1, "fail": 750}}})
        self.assertEqual(differential.classify(r), [])

    def test_kernel_command_mismatch_fails(self):
        r = report({"decide: differs in <whole output>": {"count": 1, "first_case_id": "d-1"}})
        self.assertEqual(len(differential.classify(r)), 1)

    def test_more_than_the_known_adapter_case_fails(self):
        r = report({"canonical: differs in answers": {"count": 2, "first_case_id": "envelope-deep_envelope"}})
        self.assertEqual(len(differential.classify(r)), 1)

    def test_kernel_invariant_failure_fails(self):
        r = report({}, {"json.dup": {"rust": {"pass": 1, "fail": 1}}})
        self.assertEqual(len(differential.classify(r)), 1)


if __name__ == "__main__":
    unittest.main()
