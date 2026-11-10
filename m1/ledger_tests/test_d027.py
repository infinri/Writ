"""Language-neutral tests for owner decision D-027 (LEDGER-SQLITE.md
"Concurrency profiles"): standardized concurrent-worker start.

Expectations are written from the specification before either implementation.
Timing values are never compared between prototypes or against thresholds; the
tests check the start rule, the setup accounting and that behaviour does not
depend on the profile.

  WRIT_RUST=m1/rust/target/release/proto WRIT_GO=m1/go/proto \\
      python3 -m unittest discover -s m1/ledger_tests -p 'test_d027*' -v
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

M1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(M1 / "props"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wprops import c_broker as cb  # noqa: E402
from test_storage_failures import PROTOS  # noqa: E402
from test_timing_integrity import TX_NAMES, cases as fixture_cases, nn  # noqa: E402

PROFILES = ("primary", "cold")
LEDGERS = ("memory", "sqlite")


def ledger_args(ledger: str, tmp: str) -> list:
    return [] if ledger == "memory" else ["--ledger", "sqlite", "--ledger-dir", tempfile.mkdtemp(dir=tmp)]


def run_lines(proto: str, command: str, cs: list, args: list) -> dict:
    payload = "".join(json.dumps({"id": c["id"], **({"input_text": c["input_text"]} if "input_text" in c
                                                    else {"input": c["input"]})}, separators=(",", ":")) + "\n"
                      for c in cs).encode()
    p = subprocess.run([proto, command, *args], input=payload, capture_output=True, timeout=600)
    assert p.returncode == 0, p.stderr[-400:]
    out = {}
    for ln in p.stdout.split(b"\n"):
        if ln.strip():
            m = json.loads(ln)
            out[m["id"]] = m["output"]
    return out


def groups(steps: list, nested: bool = False):
    """(concurrent step, is it itself a worker of an outer group), nested ones included."""
    for s in steps:
        if isinstance(s, dict) and s.get("op") == "concurrent":
            yield s, nested
            yield from groups(s.get("operations") or [], True)


def generated(seed: int, sizes=(2, 16, 64)) -> list:
    rng = random.Random(seed)
    return [cb.gen_concurrent(rng, i, n) for i, n in enumerate(sizes * 2)]


def contention_cases() -> list:
    """64 workers racing for one single-use grant with distinct keys, and 64 on one key."""
    p1 = cb.request("P1", "k1")
    race_grant = [cb.approve(p1, "g1"), {"op": "concurrent", "operations": [
        cb.execute(cb.request("P1", f"u{i}"), "g1") for i in range(64)]}]
    race_key = [{"op": "concurrent", "operations": [cb.execute(cb.request("W1", "k1")) for _ in range(64)]}]
    def meta(variant, grant, keys):
        return {"n": 64, "grants": {f"{variant}|0": grant} if grant else {},
                "op_types": [[variant, 0, "", grant, None, k] for k in keys]}
    nested = [{"op": "concurrent", "operations": [
        {"op": "concurrent", "operations": [cb.execute(cb.request("W1", f"n{i}{j}")) for j in range(2)]}
        for i in range(2)]}]
    return [{"id": "d027-nested", "input": {"initial": cb.initial(), "steps": nested}},
            {"id": "d027-grant", "input": {"initial": cb.initial(), "steps": race_grant},
             "meta": meta("P1", "g1", [f"u{i}" for i in range(64)])},
            {"id": "d027-key", "input": {"initial": cb.initial(), "steps": race_key},
             "meta": meta("W1", None, ["k1"] * 64)}]


class Profiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROTOS:
            raise unittest.SkipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        cls.tmp = tempfile.TemporaryDirectory(prefix="writ-d027-")
        cls.fixtures = fixture_cases()
        cls.generated = generated(20261110)
        cls.contention = contention_cases()
        cls.plain, cls.timed = {}, {}
        for name, proto in PROTOS.items():
            for ledger in LEDGERS:
                cls.plain[(name, ledger)] = run_lines(proto, "broker-scenario", cls.fixtures,
                                                      ledger_args(ledger, cls.tmp.name))
                for prof in PROFILES:
                    opt = ["--concurrency-profile", prof]
                    cls.timed[(name, ledger, prof)] = {
                        "fixtures": run_lines(proto, "bench-broker-scenario", cls.fixtures,
                                              ledger_args(ledger, cls.tmp.name) + opt),
                        "fixtures_plain": run_lines(proto, "broker-scenario", cls.fixtures,
                                                    ledger_args(ledger, cls.tmp.name) + opt),
                        "generated": run_lines(proto, "bench-broker-scenario", cls.generated,
                                               ledger_args(ledger, cls.tmp.name) + opt),
                        "contention": run_lines(proto, "bench-broker-scenario", cls.contention,
                                                ledger_args(ledger, cls.tmp.name) + opt),
                    }

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_group(self, g: dict, ledger: str, prof: str, nested: bool = False):
        self.assertEqual(set(g), {"op", "total_ns", "validate_ns", "decide_ns", "key_lock_wait_ns", "tx",
                                  "ready_ns", "operations"} | ({"setup_ns", "start_ns"} if nested else set()))
        ready, total = g["ready_ns"], g["total_ns"]
        self.assertTrue(type(ready) is int and 0 <= ready <= total, (ready, total))
        if not g["operations"]:
            self.assertEqual(ready, 0)
        for w in g["operations"]:
            keys = {"op", "total_ns", "validate_ns", "decide_ns", "key_lock_wait_ns", "tx", "setup_ns", "start_ns"}
            self.assertEqual(set(w) - {"ready_ns", "operations"}, keys)
            start, wt, setup = w["start_ns"], w["total_ns"], w["setup_ns"]
            self.assertTrue(type(start) is int and type(wt) is int and wt >= 0)
            self.assertGreaterEqual(start, ready, "a worker started before the group was ready")
            self.assertLessEqual(start + wt, total, "a worker outlived its group")
            parts = nn(w["validate_ns"]) + nn(w["decide_ns"]) + nn(w["key_lock_wait_ns"])
            self.assertTrue(set(w["tx"]) <= TX_NAMES)
            for tx in w["tx"].values():
                parts += nn(tx["begin_ns"]) + tx["body_ns"] + nn(tx["end_ns"])
            if ledger == "memory":
                self.assertIsNone(setup)
                self.assertLessEqual(parts, wt)
                continue
            self.assertTrue(type(setup) is int and setup >= 0, setup)
            if prof == "primary":
                self.assertLessEqual(setup, ready, "setup finished after release")
                self.assertLessEqual(parts, wt)
            else:
                self.assertLessEqual(parts + setup, wt, "cold setup must be inside total_ns")

    def each_group(self, kind: str):
        n = 0
        for (name, ledger, prof), runs in self.timed.items():
            for cid, out in runs[kind].items():
                self.assertEqual(set(out), {"scenario", "timing"}, (name, cid))
                for g, nested in groups(out["timing"]["steps"]):
                    n += 1
                    yield name, ledger, prof, cid, g, nested
        self.assertGreater(n, 0)

    def test_start_rule_and_setup_accounting(self):
        for kind in ("fixtures", "generated", "contention"):
            for name, ledger, prof, cid, g, nested in self.each_group(kind):
                with self.subTest(prototype=name, ledger=ledger, profile=prof, case=cid):
                    self.check_group(g, ledger, prof, nested)

    def test_top_level_steps_carry_no_worker_members(self):
        for (name, ledger, prof), runs in self.timed.items():
            for cid, out in runs["fixtures"].items():
                for s in out["timing"]["steps"]:
                    with self.subTest(prototype=name, ledger=ledger, profile=prof, case=cid):
                        self.assertNotIn("setup_ns", s)
                        self.assertNotIn("start_ns", s)

    def test_outputs_do_not_depend_on_the_profile(self):
        for (name, ledger, prof), runs in self.timed.items():
            base = self.plain[(name, ledger)]
            for c in self.fixtures:
                with self.subTest(prototype=name, ledger=ledger, profile=prof, case=c["id"]):
                    self.assertEqual(runs["fixtures_plain"][c["id"]], base[c["id"]])
                    self.assertEqual(runs["fixtures"][c["id"]]["scenario"], base[c["id"]])

    def test_generated_groups_are_serializable_in_both_profiles(self):
        by_id = {c["id"]: c for c in self.generated}
        for (name, ledger, prof), runs in self.timed.items():
            for cid, out in runs["generated"].items():
                s, t = out["scenario"], out["timing"]
                with self.subTest(prototype=name, ledger=ledger, profile=prof, case=cid):
                    grp, ev = s["steps"][-1], t["steps"][-1]
                    self.assertEqual(grp["result"], "concurrent")
                    self.assertTrue(cb.serializable(by_id[cid], grp["outcomes"], s["final"], evidence=ev))

    def test_contention_invariants_in_both_profiles(self):
        by_id = {c["id"]: c for c in self.contention}
        for (name, ledger, prof), runs in self.timed.items():
            for cid, out in runs["contention"].items():
                if cid == "d027-nested":  # shape only (test_start_rule_and_setup_accounting)
                    continue
                s, ev = out["scenario"], out["timing"]["steps"][-1]
                cats = [cb.classify_op(w)["category"] for w in ev["operations"]]
                with self.subTest(prototype=name, ledger=ledger, profile=prof, case=cid):
                    self.assertEqual(len(ev["operations"]), 64)
                    self.assertNotIn(5, cats, "only verified lock-contention aborts are accepted")
                    f = s["final"]
                    self.assertLessEqual(f["effects_applied"], 1)
                    self.assertLessEqual(f["consumed_permits"], 1)
                    if cid == "d027-grant":
                        self.assertLessEqual(f["grant_uses"]["g1"], 1)
                    self.assertTrue(cb.serializable(by_id[cid], s["steps"][-1]["outcomes"], f, evidence=ev))


class Options(unittest.TestCase):
    LINE = json.dumps({"id": "o", "input": {"initial": cb.initial(), "steps": [
        {"op": "concurrent", "operations": [cb.execute(cb.request("W1", "k1"))]}]}}).encode() + b"\n"

    def call(self, proto, *args):
        return subprocess.run([proto, "broker-scenario", *args], input=self.LINE, capture_output=True, timeout=60)

    def test_profile_option_accepted_with_either_ledger(self):
        if not PROTOS:
            self.skipTest("no prototype")
        for name, proto in PROTOS.items():
            with tempfile.TemporaryDirectory() as d:
                for args in (["--concurrency-profile", "primary"], ["--concurrency-profile", "cold"],
                             ["--concurrency-profile", "cold", "--ledger", "sqlite", "--ledger-dir", d],
                             ["--ledger", "sqlite", "--concurrency-profile", "primary", "--ledger-dir", d]):
                    with self.subTest(prototype=name, args=args):
                        p = self.call(proto, *args)
                        self.assertEqual(p.returncode, 0, p.stderr[-300:])
                        out = json.loads(p.stdout)["output"]
                        self.assertEqual(out["steps"][0]["outcomes"], {"effect_completed": 1})

    def test_bad_profile_option_is_a_usage_error(self):
        if not PROTOS:
            self.skipTest("no prototype")
        for name, proto in PROTOS.items():
            for args in (["--concurrency-profile", "warm"], ["--concurrency-profile", ""],
                         ["--concurrency-profile"],
                         ["--concurrency-profile", "cold", "--concurrency-profile", "cold"]):
                with self.subTest(prototype=name, args=args):
                    p = self.call(proto, *args)
                    self.assertEqual(p.returncode, 2)
                    self.assertEqual(p.stdout, b"")


if __name__ == "__main__":
    unittest.main()
