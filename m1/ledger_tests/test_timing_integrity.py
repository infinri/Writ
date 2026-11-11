"""Language-neutral integrity tests for `bench-broker-scenario` (M1-06d,
m1/fixtures/broker/LEDGER-SQLITE.md "Per-operation timing").

Timing values are never compared between prototypes or against thresholds;
these tests check only that instrumentation is present, well formed, internally
consistent, names the transactions the specification says ran, and never
changes the scenario output.

  WRIT_RUST=m1/rust/target/release/proto WRIT_GO=m1/go/proto \\
      python3 -m unittest discover -s m1/ledger_tests -p 'test_timing*' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

M1 = Path(__file__).resolve().parents[1]
PROTOS = {k: os.environ.get(v) for k, v in (("rust", "WRIT_RUST"), ("go", "WRIT_GO"))}
PROTOS = {k: str(Path(v).resolve()) for k, v in PROTOS.items() if v}
SETS = ["broker-scenarios", "protocol-broker"]
TX_NAMES = {"reserve", "dispatch", "adapter", "observe", "approve", "reconcile", "audit"}
STEP_KEYS = {"op", "total_ns", "validate_ns", "decide_ns", "key_lock_wait_ns", "tx"}
RESERVE_REFUSALS = {"IDEMPOTENCY_CONFLICT", "RECONCILIATION_REQUIRED", "GRANT_UNKNOWN", "GRANT_MISMATCH",
                    "GRANT_EXPIRED", "GRANT_EXHAUSTED"}
ADAPTER_REFUSALS = {"PERMIT_REQUEST_MISMATCH", "PERMIT_INVALID", "PERMIT_EXPIRED", "PERMIT_REPLAYED"}


def cases():
    out = []
    for name in SETS:
        for ln in (M1 / "fixtures/sets" / f"{name}.jsonl").read_text().splitlines():
            if ln.strip():
                c = json.loads(ln)
                out.append(c)
    return out


def run_lines(proto: str, command: str, cs: list, ledger_dir: str | None) -> dict:
    payload = "".join(json.dumps({"id": c["id"], **({"input_text": c["input_text"]} if "input_text" in c
                                                    else {"input": c["input"]})}, separators=(",", ":")) + "\n"
                      for c in cs).encode()
    args = [proto, command] + (["--ledger", "sqlite", "--ledger-dir", ledger_dir] if ledger_dir else [])
    p = subprocess.run(args, input=payload, capture_output=True, timeout=600)
    assert p.returncode == 0, p.stderr[-400:]
    res = {}
    for ln in p.stdout.split(b"\n"):
        if ln.strip():
            m = json.loads(ln)
            res[m["id"]] = m["output"]
    return res


def nn(v) -> int:
    return 0 if v is None else v


def is_ns(v) -> bool:
    return type(v) is int and v >= 0


def expected_tx(op: dict, result: dict) -> dict | None:
    """{name: outcome} the specification implies for one non-concurrent step, or
    None when the specification leaves it to the step's history."""
    kind = op.get("op")
    code = result.get("code") if result.get("result") == "refused" else None
    if code == "LEDGER_UNAVAILABLE":
        return None
    audit = {"audit": "commit"}
    if kind == "execute":
        if code == "INGRESS_UNAUTHENTICATED" or code == "APPROVAL_REQUIRED":
            return audit
        if code in RESERVE_REFUSALS:
            return {"reserve": "rollback", **audit}
        if code in ADAPTER_REFUSALS:
            return None  # adapter refusal after a committed dispatch: history dependent
        if code is not None:
            return audit  # decision refusal
        if result.get("replayed"):
            return {"reserve": "rollback"}
        r = result.get("result")
        fault = op.get("fault")
        if r == "crashed" and fault == "crash_before_reserve":
            return {"reserve": "rollback"}
        if r == "crashed" and fault == "crash_after_reserve":
            return {"reserve": "commit"}
        if r == "crashed" and fault == "crash_after_dispatch":
            return {"reserve": "commit", "dispatch": "commit", "adapter": "commit"}
        if r in ("effect_completed", "effect_failed"):
            return {"reserve": "commit", "dispatch": "commit", "adapter": "commit", "observe": "commit"}
        return None
    if kind == "approve":
        if result.get("result") == "grant_issued":
            return {"approve": "commit"}
        if code == "GRANT_REF_IN_USE":
            return {"approve": "rollback", **audit}
        return audit
    if kind == "present_permit":
        if result.get("result") == "effect_applied":
            return {"adapter": "commit"}
        if code == "PERMIT_MISSING":
            return audit
        if code in ADAPTER_REFUSALS:
            return {"adapter": "rollback", **audit}
        return None
    if kind == "apply_without_permit" or code == "OPERATION_UNKNOWN":
        return audit
    if kind == "reconcile":
        return {"reconcile": "commit"}
    if kind in ("restart_broker", "activate_bundle", "advance_time", "set_dependency"):
        return {} if code is None else audit
    return None


class TimingIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROTOS:
            raise unittest.SkipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        cls.cases = cases()
        cls.tmp = tempfile.TemporaryDirectory(prefix="writ-timing-")
        cls.runs = {}
        for name, proto in PROTOS.items():
            for ledger in ("memory", "sqlite"):
                dirs = [None, None] if ledger == "memory" else [tempfile.mkdtemp(dir=cls.tmp.name) for _ in range(2)]
                plain = run_lines(proto, "broker-scenario", cls.cases, dirs[0])
                timed = run_lines(proto, "bench-broker-scenario", cls.cases, dirs[1])
                cls.runs[(name, ledger)] = (plain, timed)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def each(self):
        for (name, ledger), (plain, timed) in self.runs.items():
            for c in self.cases:
                yield name, ledger, c, plain.get(c["id"]), timed.get(c["id"])

    def test_scenario_output_is_unchanged(self):
        for name, ledger, c, plain, timed in self.each():
            with self.subTest(prototype=name, ledger=ledger, case=c["id"]):
                self.assertIsInstance(timed, dict)
                self.assertEqual(set(timed), {"scenario", "timing"})
                self.assertEqual(timed["scenario"], plain)

    def check_step(self, step: dict, ledger: str, top: bool = True):
        worker = set() if top else {"setup_ns", "ready_at_ns", "start_ns", "release_delay_ns"}  # D-028 worker members
        if step.get("op") == "concurrent":
            self.assertEqual(set(step), STEP_KEYS | {"ready_ns", "release_ns", "start_skew_ns", "operations"} | worker)
            self.assertTrue(is_ns(step["total_ns"]))
            self.assertEqual((step["validate_ns"], step["decide_ns"], step["key_lock_wait_ns"], step["tx"]),
                             (None, None, None, {}))
            for s in step["operations"]:
                self.check_step(s, ledger, top=False)
            return
        self.assertEqual(set(step), STEP_KEYS | worker)
        self.assertTrue(is_ns(step["total_ns"]))
        for k in ("validate_ns", "decide_ns", "key_lock_wait_ns"):
            self.assertTrue(step[k] is None or is_ns(step[k]), k)
        if ledger == "memory":
            self.assertIsNone(step["key_lock_wait_ns"])
        if step["op"] not in ("execute", "approve"):
            self.assertIsNone(step["validate_ns"])
            self.assertIsNone(step["decide_ns"])
        inside = nn(step["validate_ns"]) + nn(step["decide_ns"]) + nn(step["key_lock_wait_ns"])
        self.assertTrue(set(step["tx"]) <= TX_NAMES, step["tx"])
        for tname, tx in step["tx"].items():
            self.assertEqual(set(tx), {"begin_ns", "body_ns", "end_ns", "outcome", "error"}, tname)
            self.assertIn(tx["outcome"], ("commit", "rollback"))
            err = tx["error"]
            if err is not None:  # D-026: database error evidence; always a rollback
                self.assertEqual(set(err), {"code", "at"})
                self.assertIn(err["code"], ("busy", "other"))
                self.assertIn(err["at"], ("begin", "body", "end"))
                self.assertEqual(tx["outcome"], "rollback")
                self.assertNotEqual(ledger, "memory", "the in-memory ledger never reports a database error")
            self.assertTrue(is_ns(tx["body_ns"]))
            if ledger == "memory":
                self.assertEqual((tx["begin_ns"], tx["end_ns"]), (None, None))
            else:
                self.assertTrue(is_ns(tx["begin_ns"]) and is_ns(tx["end_ns"]), tname)
            inside += nn(tx["begin_ns"]) + tx["body_ns"] + nn(tx["end_ns"])
        self.assertLessEqual(inside, step["total_ns"], "parts exceed total")

    def test_timing_shape_and_consistency(self):
        for name, ledger, c, _plain, timed in self.each():
            with self.subTest(prototype=name, ledger=ledger, case=c["id"]):
                t = timed["timing"]
                self.assertEqual(set(t), {"parse_ns", "open_ns", "steps", "final_ns", "run_ns"})
                self.assertTrue(is_ns(t["parse_ns"]) and is_ns(t["run_ns"]))
                if ledger == "memory":
                    self.assertIsNone(t["open_ns"])
                s = timed["scenario"]
                if "steps" not in s:
                    self.assertEqual((t["steps"], t["final_ns"]), ([], None))
                    continue
                self.assertTrue(is_ns(t["final_ns"]))
                self.assertEqual(len(t["steps"]), len(s["steps"]))
                for step in t["steps"]:
                    self.check_step(step, ledger)
                total = sum(st["total_ns"] for st in t["steps"]) + nn(t["open_ns"]) + t["final_ns"]
                self.assertLessEqual(total, t["run_ns"], "steps exceed run")

    def test_transactions_named_by_the_specification(self):
        checked = 0
        for name, ledger, c, _plain, timed in self.each():
            s, t = timed["scenario"], timed["timing"]
            if "steps" not in s:
                continue
            ops = c["input"]["steps"]
            for i, (op, res, step) in enumerate(zip(ops, s["steps"], t["steps"])):
                exp = expected_tx(op, res) if op.get("op") != "concurrent" else None
                if exp is None:
                    continue
                with self.subTest(prototype=name, ledger=ledger, case=c["id"], step=i, op=op.get("op"), result=res):
                    self.assertEqual({k: v["outcome"] for k, v in step["tx"].items()}, exp)
                    self.assertTrue(all(v["error"] is None for v in step["tx"].values()),
                                    "fixtures run without contention or injected faults")
                    if op.get("op") == "execute" and res.get("code") == "INGRESS_UNAUTHENTICATED":
                        self.assertIsNone(step["decide_ns"])
                    checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
