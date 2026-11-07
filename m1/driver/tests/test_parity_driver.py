"""Self-tests for the M1 parity driver (M1-01 acceptance).

Run from the repository root:  python3 -m unittest discover -s m1/driver/tests -v
The driver must pass a correct stub and fail every faulty one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DRIVER = ROOT / "m1/driver/parity.py"
MANIFEST = ROOT / "m1/fixtures/manifest.json"
STUB = Path(__file__).with_name("stub_proto.py")
M0_PY = Path(os.environ.get("WRIT_M0_PYTHON", ROOT / "m0/.venv/bin/python"))
ADAPTER = ROOT / "m1/adapters/m0_oracle_proto.py"


def expected_answers() -> dict:
    manifest = json.loads(MANIFEST.read_text())
    answers = {}
    for spec in manifest["sets"]:
        for raw in (MANIFEST.parent / spec["file"]).read_text().splitlines():
            if raw.strip():
                case = json.loads(raw)
                answers[case["id"]] = case["expect"]
    return answers


class DriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.answers = Path(cls.tmp.name) / "answers.json"
        cls.answers.write_text(json.dumps(expected_answers()))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_driver(self, proto: str, mode: str = "echo", *extra: str) -> tuple[int, dict]:
        out = Path(self.tmp.name) / f"result-{mode}.json"
        env = dict(os.environ, STUB_ANSWERS=str(self.answers), STUB_MODE=mode)
        proc = subprocess.run([sys.executable, str(DRIVER), "--proto", proto, "--manifest", str(MANIFEST),
                               "--out", str(out), *extra], capture_output=True, text=True, env=env, timeout=120)
        report = json.loads(out.read_text()) if out.exists() else {}
        return proc.returncode, report

    def stub(self) -> str:
        return f"{sys.executable} {STUB}"

    def test_correct_stub_passes_every_set(self):
        code, report = self.run_driver(self.stub())
        self.assertEqual(code, 0)
        self.assertTrue(report["summary"]["ok"])
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["summary"]["passed"], report["summary"]["cases"])

    def assert_fails(self, mode: str, *extra: str) -> dict:
        code, report = self.run_driver(self.stub(), mode, *extra)
        self.assertEqual(code, 1, mode)
        self.assertFalse(report["summary"]["ok"], mode)
        return report

    def test_wrong_field_is_detected_and_named(self):
        report = self.assert_fails("wrong-field")
        mismatched = [m["id"] for s in report["sets"] for m in s["mismatches"]]
        self.assertTrue(mismatched)

    def test_missing_answer_fails(self):
        report = self.assert_fails("missing")
        self.assertTrue(any("no response" in e for s in report["sets"] for e in s["protocol_errors"]))

    def test_non_json_output_fails(self):
        self.assert_fails("not-json")

    def test_crash_fails(self):
        report = self.assert_fails("crash")
        self.assertTrue(any("exit status 3" in e for s in report["sets"] for e in s["protocol_errors"]))

    def test_hang_times_out(self):
        report = self.assert_fails("hang", "--timeout", "2")
        self.assertTrue(any("timeout" in e for s in report["sets"] for e in s["protocol_errors"]))

    def test_duplicate_id_fails(self):
        self.assert_fails("duplicate")

    def test_nondeterminism_detected_with_repeat(self):
        report = self.assert_fails("nondeterministic", "--repeat", "3")
        self.assertTrue(any(s["nondeterministic"] for s in report["sets"]))

    def test_report_records_provenance(self):
        _, report = self.run_driver(self.stub())
        self.assertTrue(report["manifest_sha256"].startswith("sha256:"))
        self.assertTrue(all(s["file_sha256"].startswith("sha256:") for s in report["sets"]))
        self.assertIn("argv", report["proto"])

    @unittest.skipUnless(M0_PY.exists(), "M0 Python (WRIT_M0_PYTHON or m0/.venv) not present")
    def test_m0_reference_adapter_through_protocol(self):
        # The M0 harness has no check-acceptance; only the sets it implements are run.
        code, report = self.run_driver(f"{M0_PY} {ADAPTER}", "echo", "--sets", "canonical-vector,golden-decisions")
        self.assertEqual(code, 0, json.dumps(report.get("sets"), indent=1)[:2000])
        self.assertEqual(report["summary"]["passed"], 27)


class FixtureExportTests(unittest.TestCase):
    def test_exported_sets_are_current(self):
        proc = subprocess.run([sys.executable, str(ROOT / "m1/fixtures/export_m0_fixtures.py"), "--check"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
