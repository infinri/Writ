"""The dev-only M1 line adapter keeps envelopes closed and strict (D-021, D-031).
Skipped unless the example is built: cargo build --release --locked --example m1_line_adapter"""
import json
import subprocess
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "target/release/examples/m1_line_adapter"
OK = {"matched_rule_ids": [], "reason_code": "REQUEST_JSON_INVALID", "status": "invalid"}


@unittest.skipUnless(BIN.exists(), "adapter example not built")
class Envelope(unittest.TestCase):
    def answers(self, *lines, command="decide"):
        p = subprocess.run([str(BIN), command], input=b"".join(l + b"\n" for l in lines), capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(x) for x in p.stdout.splitlines()]

    def test_closed_envelope_and_strict_lines(self):
        cases = [
            (b'{"id":"a","input_text":"{","extra":1}', {"id": "a", "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"b","input_text":"{","input":{}}', {"id": "b", "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"c"}', {"id": "c", "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"d","input_text":7}', {"id": "d", "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"e","id":"e","input_text":"{"}', {"id": None, "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":7,"input_text":"{"}', {"id": None, "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"f","input":1.5}', {"id": None, "output": {"error": "ENVELOPE_INVALID"}}),
            (b'{"id":"g","input_text":"{"}', {"id": "g", "output": OK}),
        ]
        got = self.answers(*[c[0] for c in cases])
        self.assertEqual(got, [c[1] for c in cases])

    def test_m3_commands_are_refused(self):
        for cmd in ("transition-scenario", "broker-scenario"):
            p = subprocess.run([str(BIN), cmd], input=b'{"id":"a","input":{}}\n', capture_output=True, timeout=30)
            self.assertEqual((p.returncode, p.stdout), (2, b""), cmd)


if __name__ == "__main__":
    unittest.main()
