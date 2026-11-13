"""Self-tests of the M3-08 property gate (python3 -m unittest discover -s m3/props).

Pure tests need no binary. The end-to-end tests run the adapter at
target/release/examples/m3_line_adapter (or $WRIT_M3_ADAPTER) and are skipped when it is
missing; with a mutant proxy they show the oracle catches an injected wrong answer.
"""
from __future__ import annotations

import os
import random
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker_oracle as bo  # noqa: E402
from broker_oracle import Proto, Results, m1  # noqa: E402

ADAPTER = Path(os.environ.get("WRIT_M3_ADAPTER", bo.ROOT / "target" / "release" / "examples" / "m3_line_adapter"))
OK = {"result": "effect_completed"}
REPLAY = {"replayed": True, "result": "effect_completed"}


def failed(res: Results, inv: str) -> int:
    return sum(v["fail"] for v in res.counts.get(inv, {}).values())


def passed(res: Results, inv: str) -> int:
    return sum(v["pass"] for v in res.counts.get(inv, {}).values())


def seq(steps_info, cid="t-1"):
    steps = [s for s, _ in steps_info]
    info = [i for _, i in steps_info]
    return {"id": cid, "command": "broker-scenario", "input": {"initial": m1.initial(), "steps": steps},
            "meta": {"family": "sequence", "info": info}}


def ex(req, fault=None, now=m1.T0):
    return (m1.execute(req, None, fault), {"kind": "execute", "variant": "W1", "seal": "valid",
                                           "deps": {"ledger": "up", "policy": "up"}, "grant": None,
                                           "fault": fault, "now": now})


def b36_case():
    req = m1.request("W1", "k1")
    return seq([ex(req, "crash_after_dispatch"), ex(req), ({"op": "reconcile"}, {"kind": "reconcile"}),
                ex(req), ({"op": "reconcile"}, {"kind": "reconcile"})])


def out(steps, receipts, refusals):
    return {"steps": steps, "final": {"consumed_permits": 1, "effects_applied": 1, "grant_uses": {},
                                      "receipts": receipts, "refusals": refusals}}


class Generation(unittest.TestCase):
    def test_m1_tree_is_imported_without_bytecode(self):
        self.assertTrue(sys.dont_write_bytecode)
        self.assertTrue(Path(m1.__file__).is_relative_to(bo.M1_PROPS))

    def test_excluded_operations_never_generated(self):
        rng = random.Random("5:broker")
        base = bo.gen_base(rng, 300) + [bo.gen_b36(rng, i) for i in range(40)]
        for c in base:
            for i, s in enumerate(c["input"]["steps"]):
                self.assertNotIn(s.get("op"), bo.EXCLUDED_OPS)
                if s.get("op") == "present_permit":
                    self.assertEqual(c["input"]["steps"][s["of_step"]]["op"], "execute")
                    self.assertEqual(c["meta"]["info"][i]["of"], s["of_step"])
            self.assertEqual(len(c["input"]["steps"]), len(c["meta"]["info"]))
        self.assertTrue(all(p["id"] not in ("brp-present_bad_index", "brp-present_negative_index")
                            for p in bo.probes()))

    def test_generation_is_seeded(self):
        a = bo.gen_base(random.Random("9:broker"), 50)
        b = bo.gen_base(random.Random("9:broker"), 50)
        self.assertEqual(a, b)

    def test_concurrent_groups_carry_no_faults(self):
        rng = random.Random("2:broker")
        for i in range(30):
            c = bo.gen_concurrent(rng, i, 16)
            self.assertTrue(all("fault" not in op for op in c["input"]["steps"][-1]["operations"]))
            self.assertTrue(all(t[4] is None for t in c["meta"]["op_types"]))

    def test_remove_steps_remaps_presentations(self):
        req = m1.request("W1", "k1")
        c = seq([({"op": "apply_without_permit", "request": req}, {"kind": "apply_without_permit"}), ex(req),
                 ({"op": "present_permit", "of_step": 1, "request": req},
                  {"kind": "present", "of": 1, "tamper": None, "now": m1.T0}),
                 ({"op": "present_permit", "of_step": 0, "request": req},
                  {"kind": "present", "of": 0, "tamper": None, "now": m1.T0})])
        a = bo.adapt_sequence(c)
        self.assertEqual([s["op"] for s in a["input"]["steps"]], ["execute", "present_permit"])
        self.assertEqual(a["input"]["steps"][1]["of_step"], 0)
        self.assertEqual(a["meta"]["info"][1]["of"], 0)


class Oracles(unittest.TestCase):
    def test_b36_production_answer_passes(self):
        res = Results("t")
        steps = [{"result": "crashed"}, {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"count": 1, "result": "reconciled"}, REPLAY, {"count": 0, "result": "reconciled"}]
        bo.check_sequence(res, "p", b36_case(), out(steps, {"observed_success": 1}, 1))
        self.assertFalse(res.failed(), res.to_json()["violations"])
        self.assertEqual(passed(res, "broker.m3.b36_identical_retry_after_evidence_reconcile_replays"), 1)
        self.assertEqual(passed(res, "broker.m3.b36_identical_retry_before_reconcile_requires_reconciliation"), 1)

    def test_b36_frozen_m1_expectation_is_rejected(self):
        res = Results("t")
        steps = [{"result": "crashed"}, {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"count": 1, "result": "reconciled"}, {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"count": 0, "result": "reconciled"}]
        bo.check_sequence(res, "p", b36_case(), out(steps, {"unknown_reconcile": 1}, 2))
        self.assertEqual(failed(res, "broker.m3.b36_identical_retry_after_evidence_reconcile_replays"), 1)
        self.assertEqual(failed(res, "broker.m3.final_receipt_statuses"), 1)

    def test_reconcile_count_and_redispatch_are_caught(self):
        res = Results("t")
        steps = [{"result": "crashed"}, OK, {"count": 0, "result": "reconciled"}, REPLAY,
                 {"count": 0, "result": "reconciled"}]
        o = out(steps, {"observed_success": 1}, 0)
        o["final"].update(consumed_permits=2, effects_applied=2)
        bo.check_sequence(res, "p", b36_case(), o)
        self.assertEqual(failed(res, "broker.m3.b36_identical_retry_before_reconcile_requires_reconciliation"), 1)
        self.assertEqual(failed(res, "broker.uncertain_effect_never_redispatched"), 1)
        res = Results("t")
        steps = [{"result": "crashed"}, {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"count": 0, "result": "reconciled"}, {"code": "RECONCILIATION_REQUIRED", "result": "refused"},
                 {"count": 0, "result": "reconciled"}]
        bo.check_sequence(res, "p", b36_case(), out(steps, {"dispatched": 1}, 2))
        self.assertGreaterEqual(failed(res, "broker.m3.b36_reconcile_resolves_every_dispatched_receipt_from_evidence"), 1)

    def test_permit_presented_after_bundle_change_is_stale(self):
        req = m1.request("W1", "k1")
        c = seq([ex(req), ({"op": "activate_bundle", "bundle": m1.ALT}, {"kind": "bundle"}),
                 ({"op": "present_permit", "of_step": 0, "request": req},
                  {"kind": "present", "of": 0, "tamper": None, "now": m1.T0})])
        for got, bad in (({"code": "PERMIT_STALE", "result": "refused"}, 0),
                         ({"code": "PERMIT_REPLAYED", "result": "refused"}, 1)):
            res = Results("t")
            bo.check_sequence(res, "p", c, out([OK, {"result": "bundle_activated"}, got],
                                               {"observed_success": 1}, 1))
            self.assertEqual(failed(res, "broker.m3.present_permit_production_order"), bad)

    def test_grant_backed_permit_expires_with_its_grant(self):
        req = m1.request("P1", "k1")
        t = m1.T0
        steps = [(m1.approve(req, "g1", human="human:bob", ttl=5), {"kind": "approve", "mode": "bob", "variant": "P1"}),
                 (m1.execute(req, "g1"), {"kind": "execute", "variant": "P1", "seal": "valid",
                                          "deps": {"ledger": "up", "policy": "up"}, "grant": "g1", "fault": None,
                                          "now": t}),
                 ({"op": "advance_time", "now": m1.ts(t + 10)}, {"kind": "time"}),
                 ({"op": "present_permit", "of_step": 1, "request": req},
                  {"kind": "present", "of": 1, "tamper": None, "now": t + 10})]
        res = Results("t")
        o = out([{"result": "grant_issued"}, OK, {"result": "time_advanced"},
                 {"code": "PERMIT_REPLAYED", "result": "refused"}], {"observed_success": 1}, 1)
        o["final"]["grant_uses"] = {"g1": 1}
        bo.check_sequence(res, "p", seq(steps), o)
        self.assertEqual(failed(res, "broker.m3.present_permit_production_order"), 1)

    def test_c4_effect_under_ledger_down_is_caught(self):
        req = m1.request("D1", "k1")
        c = seq([({"op": "set_dependency", "name": "ledger", "status": "down"}, {"kind": "deps"}),
                 (m1.execute(req), {"kind": "execute", "variant": "D1", "seal": "valid",
                                    "deps": {"ledger": "down", "policy": "up"}, "grant": None, "fault": None,
                                    "now": m1.T0})])
        for got, bad in (({"code": "SECRETS_PATH_FORBIDDEN", "result": "refused"}, 0),
                         ({"code": "LEDGER_UNAVAILABLE", "result": "refused"}, 0),
                         ({"code": "APPROVAL_REQUIRED", "result": "refused"}, 1), (OK, 1)):
            res = Results("t")
            bo.check_c4(res, "p", c, {"steps": [{"result": "dependency_set"}, got]})
            self.assertEqual(failed(res, "broker.m3.c4_ledger_down_fails_closed"), bad, got)

    def test_wrong_concurrent_outcome_is_not_serializable(self):
        c = {"id": "g", "input": {"steps": [{"op": "concurrent", "operations": []}]},
             "meta": {"op_types": [["W1", 0, "", None, None, "k1"]] * 3, "grants": {}}}
        fin = {"consumed_permits": 1, "effects_applied": 1, "grant_uses": {}, "receipts": {"observed_success": 1},
               "refusals": 0}
        self.assertTrue(m1.serializable(c, {"effect_completed": 1, "replayed": 2}, fin))
        self.assertFalse(m1.serializable(c, {"effect_completed": 2, "replayed": 1}, fin))

    def test_twin_that_changes_another_step_is_caught(self):
        base = {"steps": [OK], "final": {"refusals": 0}}
        good = {"steps": [{"code": "INGRESS_UNAUTHENTICATED", "result": "refused"}, OK], "final": {"refusals": 1}}
        bad = {"steps": [{"code": "INGRESS_UNAUTHENTICATED", "result": "refused"}, REPLAY], "final": {"refusals": 1}}
        case = {"id": "t-refusal0", "meta": {"family": "inserted_refusal", "twin_of": "t", "pos": 0}}
        for o, n in ((good, 0), (bad, 1)):
            res = Results("t")
            bo.check_twin(res, "p", case, o, base)
            self.assertEqual(failed(res, "broker.inserted_refusal_changes_nothing_else"), n)


@unittest.skipUnless(ADAPTER.is_file(), f"adapter not built: {ADAPTER}")
class EndToEnd(unittest.TestCase):
    def campaign(self, argv, n=60, seed=11):
        res = Results("broker")
        stats = bo.run(Proto("p", argv), random.Random(f"{seed}:broker"), n, res, repeats=1)
        return res, stats

    def mutant(self, mode):
        return [sys.executable, str(HERE / "mutant.py"), "--real", str(ADAPTER), "--mode", mode]

    def test_real_adapter_passes(self):
        res, stats = self.campaign([str(ADAPTER)])
        self.assertFalse(res.failed(), res.to_json()["violations"][:3])
        self.assertGreater(passed(res, "broker.m3.b36_identical_retry_after_evidence_reconcile_replays"), 0)
        self.assertGreater(passed(res, "broker.concurrent_group_equals_a_serialization"), 0)

    def test_injected_m1_b36_expectation_fails(self):
        res, _ = self.campaign(self.mutant("m1_b36"))
        self.assertTrue(res.failed())
        self.assertGreater(failed(res, "broker.m3.b36_identical_retry_after_evidence_reconcile_replays"), 0)

    def test_injected_concurrent_outcome_fails(self):
        res, _ = self.campaign(self.mutant("conc_extra"))
        self.assertGreater(failed(res, "broker.concurrent_group_equals_a_serialization"), 0)

    def test_injected_refusal_count_fails(self):
        res, _ = self.campaign(self.mutant("refusals"))
        self.assertGreater(failed(res, "broker.refusals_counted_exactly"), 0)


if __name__ == "__main__":
    unittest.main()
