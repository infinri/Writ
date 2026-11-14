"""Negative and positive tests for the hardened M2-06 differential gate."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import differential as d  # noqa: E402

N = 375
SEED = 7


def good_report():
    def campaign(cases, invariants=None, clusters=None, protocol=None):
        return {"seed": SEED, "cases_generated": cases, "invariants": invariants or {"x.ok": {"rust": {"pass": 3, "fail": 0}}},
                "mismatch_clusters": clusters or {}, "protocol_problems": protocol or {}}
    usage = f"{d.USAGE_PREFIX} canonical|decide|decide-batch|check-acceptance|external-refusal\n"
    json_c = campaign(1500, {
        "json.cross_command.check-acceptance": {"rust": {"pass": N, "fail": 0}, "go": {"pass": N, "fail": 0}},
        "json.cross_command.transition-scenario": {"rust": {"pass": 0, "fail": N}, "go": {"pass": N, "fail": 0}},
        "differential.exact_output": {"both": {"pass": 100, "fail": 2 * N}},
        "differential.envelope_answers": {"both": {"pass": 17, "fail": 1}},
    }, {
        "transition-scenario: differs in <whole output>": {"count": N, "first_case_id": "j@transition-scenario"},
        "broker-scenario: differs in <whole output>": {"count": N, "first_case_id": "j@broker-scenario"},
        "canonical: differs in answers": {"count": 1, "first_case_id": "envelope-deep_envelope", "example": {
            "case": {"id": "envelope-deep_envelope", "command": "canonical"},
            "outputs": {"rust": {"answers": {"null": {"error": "ENVELOPE_INVALID"}}},
                        "go": {"answers": {"e": {"error": "DEPTH_EXCEEDED"}}}}}},
    }, {"rust": [f"transition-scenario: {usage}", f"transition-scenario: {N} ids unanswered, e.g. ['a']",
                 f"broker-scenario: {usage}", f"broker-scenario: {N} ids unanswered, e.g. ['b']"]})
    return {"seed": SEED, "started_at": "2026-11-12T10:00:00+00:00", "ok": False,
            "campaigns": {"json": json_c, "decide": campaign(1500), "acceptance": campaign(1500), "refusal": campaign(600)}}


def problems(r, started="2026-11-12T09:00:00+00:00"):
    p = d.structure(r, SEED, started)
    return p or d.reconcile(r)


class Gate(unittest.TestCase):
    def test_documented_exceptions_reconcile(self):
        self.assertEqual(problems(good_report()), [])

    def test_missing_and_empty_campaigns_fail(self):
        r = good_report(); del r["campaigns"]["refusal"]
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["decide"]["cases_generated"] = 0
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["acceptance"]["invariants"] = {}
        self.assertTrue(problems(r))
        self.assertTrue(problems({"seed": SEED}))

    def test_wrong_seed_and_stale_report_fail(self):
        r = good_report(); r["seed"] = SEED + 1
        self.assertTrue(problems(r))
        self.assertTrue(problems(good_report(), started="2026-11-12T11:00:00+00:00"))

    def test_unexplained_differential_failures_fail(self):
        r = good_report(); r["campaigns"]["json"]["invariants"]["differential.exact_output"]["both"]["fail"] = 2 * N + 1
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["decide"]["invariants"]["differential.exact_output"] = {"both": {"pass": 1, "fail": 1}}
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["decide"]["mismatch_clusters"] = {"decide: differs in status": {"count": 1}}
        self.assertTrue(problems(r))

    def test_wrong_command_or_discrepancy_for_the_adapter_exception_fails(self):
        r = good_report()
        ex = r["campaigns"]["json"]["mismatch_clusters"]["canonical: differs in answers"]["example"]
        ex["case"]["command"] = "decide"
        self.assertTrue(problems(r))
        r = good_report()
        r["campaigns"]["json"]["mismatch_clusters"]["canonical: differs in answers"]["example"]["outputs"]["go"]["answers"] = {"e": {"error": "ENVELOPE_INVALID"}}
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["json"]["mismatch_clusters"]["canonical: differs in answers"]["count"] = 2
        self.assertTrue(problems(r))

    def test_m3_exception_is_exact(self):
        r = good_report(); r["campaigns"]["json"]["invariants"]["json.cross_command.transition-scenario"]["go"] = {"pass": N - 1, "fail": 1}
        self.assertTrue(problems(r), "a reference failure under an M3 command must not be accepted")
        r = good_report(); r["campaigns"]["json"]["mismatch_clusters"]["broker-scenario: differs in <whole output>"]["count"] = N - 1
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["json"]["protocol_problems"]["rust"][1] = "transition-scenario: 3 ids unanswered"
        self.assertTrue(problems(r))
        r = good_report(); r["campaigns"]["json"]["protocol_problems"]["go"] = ["broker-scenario: exit status 1"]
        self.assertTrue(problems(r))


class Runner(unittest.TestCase):
    def run_main(self, rc, write):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"

            def fake_run(argv, **kw):
                if write is not None:
                    out.write_text(write)
                return subprocess.CompletedProcess(argv, rc, "", "boom")
            argv = ["differential.py", "--kernel", "k", "--reference", "r", "--seed", str(SEED), "--out", str(out)]
            with mock.patch.object(d.subprocess, "run", fake_run), mock.patch.object(sys, "argv", argv):
                return d.main()

    def test_runner_errors_and_incomplete_output_fail(self):
        self.assertEqual(self.run_main(3, None), 1)
        self.assertEqual(self.run_main(1, None), 1)
        self.assertEqual(self.run_main(1, "{not json"), 1)
        incomplete = good_report(); del incomplete["campaigns"]["json"]
        self.assertEqual(self.run_main(1, json.dumps(incomplete)), 1)

    def test_exit_status_must_match_report(self):
        r = good_report(); r["started_at"] = "9999-01-01T00:00:00+00:00"
        self.assertEqual(self.run_main(1, json.dumps(r)), 0)
        r_ok = copy.deepcopy(r); r_ok["ok"] = True
        self.assertEqual(self.run_main(1, json.dumps(r_ok)), 1)
        self.assertEqual(self.run_main(0, json.dumps(r)), 1)


if __name__ == "__main__":
    unittest.main()
