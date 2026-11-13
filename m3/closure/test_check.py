"""Self-tests of the M3-08 closure gate (python3 -m unittest discover -s m3/closure).

They prove the gate fails for every outcome that is not an exact pass, a listed adaptation, an
exactly matched approved difference or an exactly matched deferral, and that passing these
self-tests says nothing about the real gate run (which needs the production adapter).
"""
from __future__ import annotations

import json
import os
import stat
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
HUMAN = {"steps": [{"code": "ACCEPTANCE_HUMAN_UNAVAILABLE", "result": "refused"}], "final": {"refusals": 1}}


def case(cid, expect):
    return {"id": cid, "input": {}, "expect": expect, "compare": "exact"}


SETS = {"s": [case("a", OK_STEP), case("b", REFUSED), case("c", OK_STEP)]}
DIFF_B = {"set": "s", "case": "b", "kind": "difference", "decision": "D-test", "reason": "replays",
          "adaptations": [], "steps": [0], "final_members": ["refusals"], "actual": REPLAYED}
DEFER_C = {"set": "s", "case": "c", "kind": "deferred", "decision": "D-037 item 4", "reason": "human",
           "adaptations": ["seeded-preconditions"], "steps": [0], "final_members": ["refusals"],
           "actual": HUMAN, "carry_to": "M4", "intended_assertion": "commits with signoff",
           "acceptance_criteria": "authenticated human signoff accepted",
           "blocking_dependency": "M4 authenticated human signoff",
           "observed_m3_refusal": HUMAN["steps"][0], "companions": []}
ADAPT_A = {"set": "s", "case": "a", "kind": "adapted", "decision": "D-037 item 1", "reason": "seeded",
           "adaptations": ["seeded-preconditions"]}
NONE = {"a": [], "b": [], "c": []}


def run(outputs, entries, logs=NONE):
    return check.classify(SETS, {"s": outputs}, entries, COMPARE, logs)


def cases_of(r, cat):
    return [e["case"] for e in r[cat]]


class Classification(unittest.TestCase):
    def test_clean_run_passes(self):
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [])
        self.assertTrue(r["ok"])
        self.assertEqual(r["counts"]["exact_pass"], 3)

    def test_matched_difference_and_deferral_pass_and_are_reported_apart(self):
        r = run({"a": OK_STEP, "b": REPLAYED, "c": HUMAN}, [DIFF_B, DEFER_C],
                {"a": [], "b": [], "c": ["seeded-preconditions"]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(cases_of(r, "matched_difference"), ["b"])
        self.assertEqual(cases_of(r, "deferred"), ["c"])
        self.assertEqual(r["deferred"][0]["carry_to"], "M4")
        self.assertEqual(r["counts"]["exact_pass"], 1)

    def test_unrelated_mismatch_fails_even_with_matched_entries(self):
        other = {"steps": [{"code": "LEDGER_UNAVAILABLE", "result": "refused"}], "final": {"refusals": 1}}
        r = run({"a": other, "b": REPLAYED, "c": OK_STEP}, [DIFF_B])
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "unexplained"), ["a"])
        self.assertIn("step 0", r["unexplained"][0]["cause"])

    def test_drifted_output_of_a_listed_case_fails(self):
        drifted = {"steps": [{"replayed": True, "result": "effect_completed"}], "final": {"refusals": 2}}
        r = run({"a": OK_STEP, "b": drifted, "c": OK_STEP}, [DIFF_B])
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "exception_mismatch"), ["b"])

    def test_stale_difference_or_deferral_fails(self):
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [DIFF_B, DEFER_C],
                {"a": [], "b": [], "c": ["seeded-preconditions"]})
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "stale_exception"), ["b", "c"])

    def test_unsupported_output_fails_even_when_listed(self):
        unsupported = {"unsupported": {"step": 0, "op": "x", "reason": "R"}, "steps": []}
        r = run({"a": OK_STEP, "b": unsupported, "c": OK_STEP}, [DIFF_B])
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "not_driven"), ["b"])

    def test_missing_answer_fails(self):
        r = run({"a": OK_STEP, "b": REFUSED}, [])
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "missing"), ["c"])


class Adaptations(unittest.TestCase):
    def test_listed_adaptation_is_adapted_coverage(self):
        logs = dict(NONE, a=["seeded-preconditions"])
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [ADAPT_A], logs)
        self.assertTrue(r["ok"])
        self.assertEqual(cases_of(r, "adapted"), ["a"])

    def test_unlisted_adaptation_is_unclassified(self):
        logs = dict(NONE, a=["harness-authorization-bundle"])
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [], logs)
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "unclassified"), ["a"])

    def test_adaptations_differing_from_the_entry_fail(self):
        logs = dict(NONE, a=["seeded-preconditions", "test-authenticated-evidence"])
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [ADAPT_A], logs)
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "adaptation_mismatch"), ["a"])

    def test_an_adapted_entry_never_excuses_a_wrong_output(self):
        logs = dict(NONE, a=["seeded-preconditions"])
        r = run({"a": REFUSED, "b": REFUSED, "c": OK_STEP}, [ADAPT_A], logs)
        self.assertFalse(r["ok"])
        self.assertEqual(cases_of(r, "exception_mismatch"), ["a"])

    def test_missing_log_fails_every_case(self):
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [], None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["counts"]["unclassified"], 3)
        r = run({"a": OK_STEP, "b": REFUSED, "c": OK_STEP}, [], {"a": [], "b": []})
        self.assertEqual(cases_of(r, "unclassified"), ["c"])

    def test_unknown_labels_in_the_log_are_problems(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_text('{"case": "a", "adaptations": ["allow-all"]}\n{"case": "a", "adaptations": []}\n')
            _, problems = check.read_log(p)
            self.assertEqual(len(problems), 2)


class EntryValidation(unittest.TestCase):
    def problems(self, entry):
        _, p = check.validate_entries(SETS, [entry])
        return p

    def test_shipped_style_entries_are_valid(self):
        for e in (DIFF_B, DEFER_C, ADAPT_A):
            self.assertEqual(self.problems(e), [], e)

    def test_wrong_or_blanket_step_lists_are_refused(self):
        self.assertTrue(self.problems(dict(DIFF_B, steps=[])))
        self.assertTrue(self.problems(dict(DIFF_B, steps=[0, 1, 2])))
        self.assertTrue(self.problems(dict(DIFF_B, final_members=[])))
        self.assertTrue(self.problems(dict(DIFF_B, final_members=["refusals", "receipts"])))

    def test_patterns_unknown_kinds_extra_members_and_empty_reasons_are_refused(self):
        self.assertTrue(self.problems(dict(DIFF_B, case="*")))
        self.assertTrue(self.problems(dict(DIFF_B, case="b*")))
        self.assertTrue(self.problems(dict(DIFF_B, kind="suppress")))
        self.assertTrue(self.problems(dict(DIFF_B, all=True)))
        self.assertTrue(self.problems(dict(DIFF_B, reason=" ")))
        self.assertTrue(self.problems(dict(DIFF_B, decision="")))
        self.assertTrue(self.problems(dict(DIFF_B, set="other")))
        self.assertTrue(self.problems(dict(DIFF_B, case="zz")))

    def test_difference_must_be_a_driven_differing_output(self):
        self.assertTrue(self.problems(dict(DIFF_B, actual=None)))
        self.assertTrue(self.problems(dict(DIFF_B, actual={"unsupported": {}})))
        self.assertTrue(self.problems(dict(DIFF_B, actual=REFUSED, steps=[], final_members=[])))

    def test_deferral_needs_its_carry_over_fields(self):
        for k in ("carry_to", "intended_assertion", "acceptance_criteria", "blocking_dependency"):
            self.assertTrue(self.problems(dict(DEFER_C, **{k: ""})), k)
        for k in ("acceptance_criteria", "blocking_dependency", "observed_m3_refusal", "companions"):
            bad = dict(DEFER_C)
            del bad[k]
            self.assertTrue(self.problems(bad), k)

    def test_deferral_observed_refusal_is_the_exact_first_differing_answer(self):
        self.assertFalse(self.problems(DEFER_C))
        self.assertTrue(self.problems(dict(DEFER_C, observed_m3_refusal={"result": "refused", "code": "DEFAULT_DENY"})))
        self.assertTrue(self.problems(dict(DEFER_C, companions="m3c-01")))

    def test_deferral_must_be_the_m4_human_signoff_refusal(self):
        self.assertTrue(self.problems(dict(DEFER_C, decision="D-036 item 2")))
        self.assertTrue(self.problems(dict(DEFER_C, carry_to="M7")))
        other = {"steps": [{"code": "DEFAULT_DENY", "result": "refused"}], "final": {"refusals": 1}}
        self.assertTrue(self.problems(dict(DEFER_C, actual=other)))

    def test_adapted_entry_needs_labels_and_no_actual(self):
        self.assertTrue(self.problems(dict(ADAPT_A, adaptations=[])))
        self.assertTrue(self.problems(dict(ADAPT_A, adaptations=["allow-all"])))
        self.assertTrue(self.problems(dict(ADAPT_A, actual=OK_STEP)))

    def test_duplicate_entries_are_refused(self):
        _, p = check.validate_entries(SETS, [DIFF_B, DIFF_B])
        self.assertTrue(p)
        r = run({"a": OK_STEP, "b": REPLAYED, "c": OK_STEP}, [DIFF_B, DIFF_B])
        self.assertFalse(r["ok"])


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, random
mode = os.environ.get("FAKE_MODE", "")
log = os.environ.get("WRIT_M3_ADAPTATIONS")
for raw in sys.stdin:
    if not raw.strip():
        continue
    cid = json.loads(raw)["id"]
    out = {"steps": [{"result": "effect_completed"}], "final": {"refusals": 0}}
    if mode == "random" and random.random() < 0.5:
        out["final"]["refusals"] = 1
    print(json.dumps({"id": cid, "output": out}))
    if log and mode != "nolog":
        with open(log, "a") as f:
            f.write(json.dumps({"command": sys.argv[1], "case": cid, "adaptations": []}) + "\n")
'''


class Coverage(unittest.TestCase):
    def spec(self, d, lines, pinned, waivers=None, deferred=True, listed=None):
        f = Path(d) / "comp.jsonl"
        f.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return {"companions": [{"name": "comp", "file": str(f), "command": "x",
                                "kind": "mechanical-companion", "cases": pinned}],
                "companion_waivers": waivers or {},
                "exceptions": [dict(DEFER_C, companions=listed or [])] if deferred else []}

    def comp(self, cid, of):
        return dict(case(cid, OK_STEP), meta={"companion_of": of})

    def test_deferred_case_needs_a_companion_or_waiver(self):
        with tempfile.TemporaryDirectory() as d:
            ok = self.spec(d, [self.comp("m3c-1", "c")], ["m3c-1"], listed=["m3c-1"])
            self.assertEqual(check.coverage_problems(ok, SETS), [])
            # D-038 item 6: the entry lists exactly the companions that name it
            unlisted = self.spec(d, [self.comp("m3c-1", "c")], ["m3c-1"])
            self.assertTrue(check.coverage_problems(unlisted, SETS))
            wrong = self.spec(d, [self.comp("m3c-1", "c")], ["m3c-1"], listed=["m3c-1", "m3c-9"])
            self.assertTrue(check.coverage_problems(wrong, SETS))
            none = self.spec(d, [self.comp("m3c-1", "a")], ["m3c-1"])
            self.assertTrue(check.coverage_problems(none, SETS))
            waived = self.spec(d, [self.comp("m3c-1", "a")], ["m3c-1"], {"c": "documented reason"})
            self.assertEqual(check.coverage_problems(waived, SETS), [])

    def test_deleted_companion_or_duplicate_id_fails(self):
        with tempfile.TemporaryDirectory() as d:
            gone = self.spec(d, [self.comp("m3c-1", "c")], ["m3c-1", "m3c-2"])
            self.assertTrue(check.coverage_problems(gone, SETS))
            dup = self.spec(d, [self.comp("a", "c")], ["a"])
            self.assertTrue(check.coverage_problems(dup, SETS))


class EndToEnd(unittest.TestCase):
    """main() against a fake adapter: nondeterminism, missing logs and stale reports fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        cases = [case(f"x-{i}", OK_STEP) for i in range(8)]
        (d / "s.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
        self.manifest = d / "manifest.json"
        self.manifest.write_text(json.dumps({"sets": [{"name": "s", "file": "s.jsonl", "command": "cmd"}]}))
        self.exceptions = d / "exceptions.json"
        self.exceptions.write_text(json.dumps({"sets": ["s"], "fixture_sha256": {"s": check.sha256(d / "s.jsonl")},
                                               "exceptions": []}))
        self.proto = d / "fake.py"
        self.proto.write_text(FAKE)
        self.proto.chmod(self.proto.stat().st_mode | stat.S_IEXEC)
        self.report = d / "report.json"

    def tearDown(self):
        os.environ.pop("FAKE_MODE", None)
        self.tmp.cleanup()

    def gate(self, *extra):
        return check.main(["--proto", f"{sys.executable} {self.proto}", "--exceptions", str(self.exceptions),
                           "--manifest", str(self.manifest), "--report", str(self.report), *extra])

    def test_deterministic_fake_passes_and_its_report_verifies(self):
        self.assertEqual(self.gate("--repeat", "2"), 0)
        self.assertEqual(self.gate("--verify-report", str(self.report)), 0)

    def test_nondeterministic_output_fails(self):
        os.environ["FAKE_MODE"] = "random"
        results = {self.gate("--repeat", "4") for _ in range(3)}
        self.assertEqual(results, {1})

    def test_missing_adaptation_log_fails(self):
        os.environ["FAKE_MODE"] = "nolog"
        self.assertEqual(self.gate(), 1)

    def test_stale_report_is_rejected(self):
        self.assertEqual(self.gate(), 0)
        spec = json.loads(self.exceptions.read_text())
        spec["description"] = "changed after the report"
        self.exceptions.write_text(json.dumps(spec))
        self.assertEqual(self.gate("--verify-report", str(self.report)), 1)

    def test_changed_fixture_fails(self):
        d = Path(self.tmp.name)
        (d / "s.jsonl").write_text(json.dumps(case("x-0", REFUSED)) + "\n")
        self.assertEqual(self.gate(), 1)


class Shipped(unittest.TestCase):
    def test_shipped_entries_validate_against_the_frozen_127_cases(self):
        spec = json.loads(check.DEFAULT_EXCEPTIONS.read_text(encoding="utf-8"))
        manifest = json.loads(check.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(check.verify_fixtures(spec, manifest, check.DEFAULT_MANIFEST.parent), [])
        parity = check._parity()
        by_name = {s["name"]: s for s in manifest["sets"]}
        sets = {n: parity.load_cases(check.DEFAULT_MANIFEST.parent / by_name[n]["file"]) for n in spec["sets"]}
        self.assertEqual(sum(len(c) for c in sets.values()), 127)
        _, problems = check.validate_entries(sets, spec["exceptions"])
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
