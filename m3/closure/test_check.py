"""Self-tests of the M3-08 closure gate (python3 -m unittest discover -s m3/closure).

They prove that the gate fails for every failure that is not an exactly matched, approved
exception, and passes only the clean case.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check  # noqa: E402

COMPARE = check._parity().compare
OK_STEP = {"steps": [{"result": "effect_completed"}], "final": {"refusals": 0}}
REFUSED = {"steps": [{"code": "RECONCILIATION_REQUIRED", "result": "refused"}], "final": {"refusals": 1}}
REPLAYED = {"steps": [{"replayed": True, "result": "effect_completed"}], "final": {"refusals": 0}}


def case(cid, expect):
    return {"id": cid, "input": {}, "expect": expect, "compare": "exact"}


SETS = {"s": [case("a", OK_STEP), case("b", REFUSED)]}
EXC_B = {"set": "s", "case": "b", "decision": "D-test", "difference": "replays", "actual": REPLAYED}


def run(outputs, exceptions):
    return check.classify(SETS, {"s": outputs}, exceptions, COMPARE)


class Gate(unittest.TestCase):
    def test_clean_case_passes(self):
        r = run({"a": OK_STEP, "b": REFUSED}, [])
        self.assertTrue(r["ok"])
        self.assertEqual(r["counts"]["exact_pass"], 2)

    def test_matched_exception_passes(self):
        r = run({"a": OK_STEP, "b": REPLAYED}, [EXC_B])
        self.assertTrue(r["ok"])
        self.assertEqual([e["case"] for e in r["matched_exception"]], ["b"])

    def test_unrelated_failure_fails_even_with_a_matched_exception(self):
        other = {"steps": [{"code": "LEDGER_UNAVAILABLE", "result": "refused"}], "final": {"refusals": 1}}
        r = run({"a": other, "b": REPLAYED}, [EXC_B])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["unexplained"]], ["a"])
        self.assertIn("step 0", r["unexplained"][0]["cause"])
        self.assertEqual(r["counts"]["matched_exception"], 1)

    def test_exception_with_a_different_actual_output_fails(self):
        drifted = {"steps": [{"replayed": True, "result": "effect_completed"}], "final": {"refusals": 2}}
        r = run({"a": OK_STEP, "b": drifted}, [EXC_B])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["exception_mismatch"]], ["b"])
        self.assertEqual(r["counts"]["matched_exception"], 0)

    def test_stale_exception_is_reported_and_fails(self):
        r = run({"a": OK_STEP, "b": REFUSED}, [EXC_B])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["stale_exception"]], ["b"])

    def test_unsupported_output_is_not_driven_and_fails(self):
        unsupported = {"unsupported": {"step": 0, "op": "x", "reason": "R", "detail": ""}, "steps": []}
        r = run({"a": unsupported, "b": REFUSED}, [])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["not_driven"]], ["a"])
        approved = dict(EXC_B, case="a", actual=None)
        r = run({"a": unsupported, "b": REFUSED}, [approved])
        self.assertFalse(r["ok"])
        self.assertTrue(r["not_driven"][0]["approved"])

    def test_an_exception_never_excuses_an_unsupported_output_with_a_recorded_actual(self):
        unsupported = {"unsupported": {"step": 0, "op": "x", "reason": "R", "detail": ""}, "steps": []}
        r = run({"a": OK_STEP, "b": unsupported}, [EXC_B])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["exception_mismatch"]], ["b"])

    def test_missing_answer_and_unknown_or_duplicate_exception_fail(self):
        r = run({"a": OK_STEP}, [])
        self.assertFalse(r["ok"])
        self.assertEqual([e["case"] for e in r["missing"]], ["b"])
        r = run({"a": OK_STEP, "b": REFUSED}, [dict(EXC_B, case="zz")])
        self.assertFalse(r["ok"])
        self.assertTrue(r["problems"])
        r = run({"a": OK_STEP, "b": REPLAYED}, [EXC_B, EXC_B])
        self.assertFalse(r["ok"])

    def test_changed_fixture_digest_fails(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "s.jsonl").write_text(json.dumps(case("a", OK_STEP)) + "\n", encoding="utf-8")
            manifest = {"sets": [{"name": "s", "file": "s.jsonl", "command": "x"}]}
            good = {"fixture_sha256": {"s": check.sha256(base / "s.jsonl")}}
            self.assertEqual(check.verify_fixtures(good, manifest, base), [])
            (base / "s.jsonl").write_text(json.dumps(case("a", REFUSED)) + "\n", encoding="utf-8")
            self.assertTrue(check.verify_fixtures(good, manifest, base))

    def test_shipped_exceptions_name_real_cases_and_pin_the_frozen_fixtures(self):
        spec = json.loads(check.DEFAULT_EXCEPTIONS.read_text(encoding="utf-8"))
        manifest = json.loads(check.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(check.verify_fixtures(spec, manifest, check.DEFAULT_MANIFEST.parent), [])
        parity = check._parity()
        by_name = {s["name"]: s for s in manifest["sets"]}
        sets = {n: parity.load_cases(check.DEFAULT_MANIFEST.parent / by_name[n]["file"]) for n in spec["sets"]}
        self.assertEqual(sum(len(c) for c in sets.values()), 127)
        known = {(s, c["id"]) for s, cases in sets.items() for c in cases}
        for e in spec["exceptions"]:
            self.assertIn((e["set"], e["case"]), known)
            self.assertTrue(e["decision"] and e["difference"])


if __name__ == "__main__":
    unittest.main()
