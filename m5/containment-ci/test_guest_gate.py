import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LOADER = importlib.machinery.SourceFileLoader("guest_gate", str(Path(__file__).with_name("guest-gate")))
GATE = importlib.util.module_from_spec(importlib.util.spec_from_loader("guest_gate", LOADER))
LOADER.exec_module(GATE)


class GuestGateTests(unittest.TestCase):
    def exercise(self, fail):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = b'["P-TEST-POSITIVE","P-TEST-CONTROL"]'
            binding = dict(source_sha="1"*40, source_archive_sha256="2"*64, image_sha256="3"*64,
                           challenge="4"*64, manifest_sha256=hashlib.sha256(spec).hexdigest(),
                           run_id=17, attempt=1)
            (root / "required-results.json").write_bytes(spec)
            (root / "binding.json").write_text(json.dumps(binding))
            driver = root / "containment-matrix"
            driver.write_text('#!/bin/sh\necho "out"\necho "err" >&2\n'
                              f'test "$*" != "{fail}" || exit 7\n')
            driver.chmod(0o700)
            with (patch.object(GATE, "profile"), patch.object(GATE, "extract"),
                  patch.object(GATE, "trusted", side_effect=lambda path, **kw: path),
                  patch.object(GATE, "ROOT", root), patch.object(GATE, "BIN", root),
                  patch.object(GATE, "PRIVATE", root), patch.object(sys, "argv", ["gate", "1"*40]),
                  patch.object(GATE.os, "umask")):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        GATE.main()
                else:
                    GATE.main()
            attempt = root / "attempt"
            self.assertEqual((attempt / "evidence.json").exists(), not fail)
            if fail:
                prefix = fail.replace(" ", "-")
                self.assertEqual((attempt / f"{prefix}.exit").read_text(), "7\n")
            self.assertTrue((attempt / "teardown.exit").is_file())
            self.assertEqual((attempt / "prepare.stdout").read_text(), "out\n")
            self.assertEqual((attempt / "prepare.stderr").read_text(), "err\n")

    def test_success_and_each_failure_phase(self):
        for fail in ["", "prepare", "verify P-TEST-CONTROL", "teardown"]:
            with self.subTest(fail=fail):
                self.exercise(fail)


if __name__ == "__main__":
    unittest.main()
