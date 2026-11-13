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

    def test_new_adapter_ops_unused_by_frozen_fixtures(self):
        # D-038 I-4: generated-only op names
        for p in (bo.ROOT / "m1" / "fixtures").rglob("*.json*"):
            text = p.read_text(encoding="utf-8", errors="replace")
            for name in ("concurrent_faults", "ledger_detail", "storage_fault"):
                self.assertNotIn(name, text, p)

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

    def c4_case(self, variant, grant=None, policy="up", restart=False):
        req = m1.request(variant, "k1")
        deps = {"ledger": "down", "policy": policy}
        steps = [({"op": "set_dependency", "name": "ledger", "status": "down"}, {"kind": "deps"})]
        if policy == "down":
            steps.append(({"op": "set_dependency", "name": "policy", "status": "down"}, {"kind": "deps"}))
        if restart:
            steps.append(({"op": "restart_broker"}, {"kind": "restart"}))
        steps.append((m1.execute(req, grant), {"kind": "execute", "variant": variant, "seal": "valid",
                                               "deps": deps, "grant": grant, "fault": None, "now": m1.T0}))
        return seq(steps)

    def c4_answers(self, case, got):
        return {"steps": [{"result": "dependency_set"}] * (len(case["input"]["steps"]) - 1) + [got]}

    def test_c4_expected_code_is_stage_aware(self):
        lu, sec = {"code": "LEDGER_UNAVAILABLE", "result": "refused"}, {"code": "SECRETS_PATH_FORBIDDEN",
                                                                       "result": "refused"}
        appr, pol = {"code": "APPROVAL_REQUIRED", "result": "refused"}, {"code": "POLICY_UNAVAILABLE",
                                                                         "result": "refused"}
        rows = [(("D1",), sec, 0), (("D1",), lu, 1),            # decision-stage deny: never ledger-first
                (("P1",), appr, 0), (("P1",), lu, 1),           # require_approval without grant
                (("P1", "g1"), lu, 0), (("P1", "g1"), appr, 1),  # grant given: reaches T-reserve
                (("W1",), lu, 0), (("W1",), sec, 1), (("W1",), OK, 1), (("W1",), REPLAY, 1),
                (("W1", None, "down"), pol, 0), (("W1", None, "down"), lu, 1),
                (("D1", None, "down"), pol, 0)]
        for code in ("GRANT_UNKNOWN", "GRANT_EXHAUSTED", "IDEMPOTENCY_CONFLICT", "RECONCILIATION_REQUIRED"):
            rows.append((("P1", "g1"), {"code": code, "result": "refused"}, 1))
        for args, got, bad in rows:
            res = Results("t")
            case = self.c4_case(*args)
            bo.check_c4(res, "p", case, self.c4_answers(case, got))
            self.assertEqual(failed(res, "broker.m3.c4_stage_aware_precedence"), bad, (args, got))
            self.assertEqual(passed(res, "broker.m3.c4_stage_aware_precedence"), 1 - bad, (args, got))

    def test_restart_does_not_heal_outage(self):
        lu = {"code": "LEDGER_UNAVAILABLE", "result": "refused"}
        for got, bad in ((lu, 0), (OK, 1)):
            res = Results("t")
            case = self.c4_case("W1", restart=True)
            bo.check_c4(res, "p", case, self.c4_answers(case, got))
            self.assertEqual(failed(res, "broker.m3.restart_does_not_heal_ledger_outage"), bad)
        req = m1.request("W1", "k1")
        case = seq([({"op": "set_dependency", "name": "policy", "status": "down"}, {"kind": "deps"}),
                    ({"op": "restart_broker"}, {"kind": "restart"}),
                    (m1.execute(req), {"kind": "execute", "variant": "W1", "seal": "valid",
                                       "deps": {"ledger": "up", "policy": "down"}, "grant": None, "fault": None,
                                       "now": m1.T0})])
        for got, bad in (({"code": "POLICY_UNAVAILABLE", "result": "refused"}, 0), (OK, 1)):
            res = Results("t")
            bo.check_c4(res, "p", case, {"steps": [{"result": "dependency_set"}, {"result": "restarted"}, got]})
            self.assertEqual(failed(res, "broker.m3.restart_does_not_heal_policy_outage"), bad)

    def test_restarts_under_outage_are_generated(self):
        rng = random.Random("4:broker")
        base = bo.gen_base(rng, 400) + [bo.gen_outage_restart(rng, i) for i in range(20)]
        self.assertGreater(sum(c["meta"].get("restart_under_outage", 0) for c in base), 20)

    def inspection(self, got, before, after):
        case = self.c4_case("W1")
        twin = bo.insert_c4_inspection(case)
        self.assertEqual([s["op"] for s in twin["input"]["steps"]],
                         ["set_dependency", "ledger_detail", "execute", "ledger_detail"])
        base = {"steps": [{"result": "dependency_set"}, got], "final": {"refusals": 1}}
        out = {"steps": [{"result": "dependency_set"}, before, got, after], "final": {"refusals": 1}}
        res = Results("t")
        bo.check_c4_inspection(res, "p", twin, out, base)
        return res

    def detail(self, **kw):
        d = {"result": "ledger_detail", "receipts": {}, "grant_uses": {}, "grant_use_rows": {}, "consumed": {},
             "effects": {}, "dispatch_events": 0, "failures": [], "refusals": 0}
        d.update(kw)
        return d

    def test_c4_refusal_hiding_progress_is_rejected(self):
        lu = {"code": "LEDGER_UNAVAILABLE", "result": "refused"}
        rec = dict(bo.C4_FAILURE)
        good = self.detail(refusals=1, failures=[rec])
        res = self.inspection(lu, self.detail(), good)
        self.assertFalse(res.failed(), res.to_json()["violations"])
        k = "fixture-tenant/k1"
        hidden = {
            "effect": dict(good, effects={k: 1}),
            "consumption": dict(good, consumed={k + "/applied": 1}),
            "dispatch": dict(good, dispatch_events=1,
                             receipts={k: {"dispatches": 1, "permit": 1, "status": "dispatched"}}),
            "reservation": dict(good, receipts={k: {"dispatches": 0, "permit": None, "status": "reserved"}}),
            "grant use": dict(good, grant_uses={"g1": 1}, grant_use_rows={"g1": 1}),
        }
        for what, after in hidden.items():
            res = self.inspection(lu, self.detail(), after)
            self.assertEqual(failed(res, "broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing"), 1,
                             what)
        for f in (dict(rec, outcome_unknown=1, stage="commit", possible_progress="dispatched_with_effect"),
                  dict(rec, transaction="dispatch", progress="reserved", possible_progress="reserved")):
            res = self.inspection(lu, self.detail(), dict(good, failures=[f]))
            self.assertEqual(failed(res, "broker.m3.c4_refusal_leaves_no_possible_progress"), 1, f)
        res = self.inspection(lu, self.detail(), dict(good, refusals=0))
        self.assertEqual(failed(res, "broker.m3.c4_refusal_audited"), 1)


class FaultModel(unittest.TestCase):
    """The extended serial model of storage faults in concurrent groups."""

    def group(self, types):
        return {"id": "f", "input": {"steps": [{"op": "concurrent_faults", "operations": []}]},
                "meta": {"op_types": [list(t) for t in types], "grants": {"P1|0": "g1"}}}

    def fin(self, case, entries, g1=0):
        """The tagged final of a hand-built detail; entries: key -> (variant, principal, status,
        effect, consumed, grant)."""
        from wprops.common import digest
        d = {"receipts": {}, "consumed": {}, "effects": {}, "grant_uses": {"g1": g1}}
        for key, (v, p, status, eff, cons, grant) in entries.items():
            k = bo.TENANT + key
            d["receipts"][k] = {"status": status, "fingerprint": digest(m1.request(v, key, p)), "grant": grant}
            if cons:
                d["consumed"][k + "/applied"] = 1
            if eff:
                d["effects"][k] = 1
        return bo.detail_final(case, d)

    def test_each_fault_maps_to_its_crash_state(self):
        lu = "LEDGER_UNAVAILABLE"
        rows = [("reserve_unavailable", None, 0, 0, 0), ("reserve_commit_lost", None, 0, 0, 0),
                ("reserve_commit_unknown", "reserved", 0, 0, 1),
                ("dispatch_unavailable", "reserved", 0, 0, 1), ("dispatch_commit_lost", "reserved", 0, 0, 1),
                ("dispatch_commit_unknown", "dispatched", 0, 0, 1),
                ("observe_unavailable", "dispatched", 1, 1, 1), ("observe_commit_lost", "dispatched", 1, 1, 1),
                ("observe_commit_unknown", "observed_success", 1, 1, 1)]
        for fault, status, eff, perm, g in rows:
            case = self.group([("P1", 0, "", "g1", fault, "k1")])
            entries = {"k1": ("P1", 0, status, eff, perm, "g1")} if status else {}
            self.assertTrue(bo.fault_serializable(case, {lu: 1}, self.fin(case, entries, g)), fault)
            self.assertFalse(bo.fault_serializable(case, {"effect_completed": 1}, self.fin(case, entries, g)), fault)
            # the answer hides nothing: a different durable state is not a serialization
            other = {"k1": ("P1", 0, "observed_success", 1, 1, "g1") if status != "observed_success"
                     else ("P1", 0, "observed_success", 0, 1, "g1")}
            self.assertFalse(bo.fault_serializable(case, {lu: 1}, self.fin(case, other, 1)), fault)

    def test_unfaulted_storage_refusal_is_not_serializable(self):
        case = self.group([("W1", 0, "", None, None, "k1")] * 2)
        e = {"k1": ("W1", 0, "observed_success", 1, 1, None)}
        self.assertFalse(bo.fault_serializable(case, {"LEDGER_UNAVAILABLE": 1, "replayed": 1}, self.fin(case, e)))
        self.assertTrue(bo.fault_serializable(case, {"effect_completed": 1, "replayed": 1}, self.fin(case, e)))

    def test_per_key_status_swap_is_not_serializable(self):
        # review M2: aggregates equal, per-key state swapped
        case = self.group([("W1", 0, "", None, "reserve_commit_unknown", "u1"), ("W2", 0, "", None, None, "u2")])
        obs = {"LEDGER_UNAVAILABLE": 1, "effect_completed": 1}
        good = {"u1": ("W1", 0, "reserved", 0, 0, None), "u2": ("W2", 0, "observed_success", 1, 1, None)}
        swapped = {"u1": ("W1", 0, "observed_success", 0, 0, None), "u2": ("W2", 0, "reserved", 1, 1, None)}
        moved = {"u1": ("W1", 0, "observed_success", 1, 1, None), "u2": ("W2", 0, "reserved", 0, 0, None)}
        self.assertTrue(bo.fault_serializable(case, obs, self.fin(case, good)))
        self.assertFalse(bo.fault_serializable(case, obs, self.fin(case, swapped)))
        self.assertFalse(bo.fault_serializable(case, obs, self.fin(case, moved)))
        # a receipt whose fingerprint belongs to another request is not the model's either
        shared = self.group([("W1", 0, "", None, None, "k1"), ("W1", 1, "", None, None, "k1")])
        mine = {"k1": ("W1", 0, "observed_success", 1, 1, None)}
        theirs = {"k1": ("W1", 1, "observed_success", 1, 1, None)}
        obs = {"effect_completed": 1, "IDEMPOTENCY_CONFLICT": 1}
        self.assertTrue(bo.fault_serializable(shared, obs, self.fin(shared, mine)))
        self.assertTrue(bo.fault_serializable(shared, obs, self.fin(shared, theirs)))
        self.assertFalse(bo.fault_serializable(shared, obs, self.fin(shared, {
            "k1": ("W2", 0, "observed_success", 1, 1, None)})))

    def test_decision_stage_refusal_precedes_a_storage_fault(self):
        # review L3: a deny never reaches T-reserve, so its storage fault never fires
        for fault in ("reserve_unavailable", "reserve_commit_unknown", "dispatch_commit_unknown"):
            case = self.group([("D1", 0, "", None, fault, "d1")])
            self.assertTrue(bo.fault_serializable(case, {"SECRETS_PATH_FORBIDDEN": 1}, self.fin(case, {})), fault)
            self.assertFalse(bo.fault_serializable(case, {"LEDGER_UNAVAILABLE": 1}, self.fin(case, {})), fault)
            reserved = {"d1": ("D1", 0, "reserved", 0, 0, None)}
            self.assertFalse(bo.fault_serializable(case, {"SECRETS_PATH_FORBIDDEN": 1}, self.fin(case, reserved)))

    def test_detail_safety_rejects_each_violation(self):
        k = "fixture-tenant/k1"
        good = {"receipts": {k: {"dispatches": 1, "permit": 1, "status": "observed_success"}},
                "grant_uses": {"g1": 1}, "grant_use_rows": {"g1": 1}, "consumed": {k + "/applied": 1},
                "effects": {k: 1}, "dispatch_events": 1}
        res = Results("t")
        bo.check_detail_safety(res, "p", {"id": "x"}, good, "t")
        self.assertFalse(res.failed())
        bad = {
            "broker.m3.fault_no_grant_overuse": dict(good, grant_uses={"g1": 2}, grant_use_rows={"g1": 2}),
            "broker.m3.fault_at_most_one_dispatch_per_key": dict(
                good, dispatch_events=2, receipts={k: dict(good["receipts"][k], dispatches=2)}),
            "broker.m3.fault_no_duplicate_effect": dict(good, effects={k: 2}),
            "broker.m3.fault_effect_only_with_dispatched_receipt_and_consumed_permit": dict(
                good, effects={k: 1, "fixture-tenant/k2": 1}),
        }
        for inv, d in bad.items():
            res = Results("t")
            bo.check_detail_safety(res, "p", {"id": "x"}, d, "t")
            self.assertGreaterEqual(failed(res, inv), 1, inv)
        res = Results("t")
        bo.check_detail_safety(res, "p", {"id": "x"}, dict(
            good, receipts={k: dict(good["receipts"][k], status="reserved")}), "t")
        self.assertGreaterEqual(failed(res, "broker.m3.fault_effect_only_with_dispatched_receipt_and_consumed_permit"), 1)

    def test_fault_groups_are_seeded_and_use_new_op_only(self):
        a = bo.gen_fault_group(random.Random("3:broker"), 1, 16)
        b = bo.gen_fault_group(random.Random("3:broker"), 1, 16)
        self.assertEqual(a, b)
        ops = [s["op"] for s in a["input"]["steps"]]
        self.assertIn("concurrent_faults", ops)
        self.assertNotIn("concurrent", ops)
        grp = a["input"]["steps"][a["meta"]["group_index"]]["operations"]
        self.assertTrue(all("fault" not in o and o.get("storage_fault", bo.STORAGE_FAULTS[0]) in bo.STORAGE_FAULTS
                            for o in grp))

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

    def c4_campaign(self, argv):
        """Focused outage sequences (restarts under outage) and their C4 inspection twins."""
        rng = random.Random("21:broker")
        base = [bo.gen_outage_restart(rng, i) for i in range(1, 31)]
        for c in base:
            c["meta"].pop("simple")
        cases = base + [t for t in map(bo.insert_c4_inspection, base) if t is not None]
        res = Results("broker")
        out = bo.run_cases(Proto("p", argv), cases, res)
        for c in cases:
            if c["meta"]["family"] == "sequence":
                bo.check_sequence(res, "p", c, out.get(c["id"]))
            else:
                bo.check_c4_inspection(res, "p", c, out.get(c["id"]), out.get(c["meta"]["twin_of"]))
        return res

    def test_real_adapter_passes_c4_and_restart_under_outage(self):
        res = self.c4_campaign([str(ADAPTER)])
        self.assertFalse(res.failed(), res.to_json()["violations"][:3])
        self.assertEqual(set(res.c4_stage_tally) >= {"decision:APPROVAL_REQUIRED", "reserve_or_later:LEDGER_UNAVAILABLE"},
                         True, res.c4_stage_tally)
        for inv in ("broker.m3.restart_does_not_heal_ledger_outage", "broker.m3.restart_does_not_heal_policy_outage",
                    "broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing",
                    "broker.m3.c4_refusal_leaves_no_possible_progress", "broker.m3.c4_refusal_audited"):
            self.assertGreater(passed(res, inv), 0, inv)

    def test_c4_and_restart_mutants_fail(self):
        expect = {
            "c4_ledger_first": "broker.m3.c4_stage_aware_precedence",
            "c4_decision_first": "broker.m3.c4_stage_aware_precedence",
            "c4_reserve_code": "broker.m3.c4_stage_aware_precedence",
            "heal_on_restart": "broker.m3.restart_does_not_heal_ledger_outage",
            "c4_hidden_effect": "broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing",
            "c4_hidden_consumption": "broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing",
            "c4_hidden_dispatch": "broker.m3.c4_refusal_reserves_consumes_dispatches_applies_nothing",
            "c4_hidden_uncertain": "broker.m3.c4_refusal_leaves_no_possible_progress",
        }
        for mode, inv in expect.items():
            with self.subTest(mode=mode):
                res = self.c4_campaign(self.mutant(mode))
                self.assertGreater(failed(res, inv), 0, mode)
        res = self.c4_campaign(self.mutant("heal_on_restart"))
        self.assertGreater(failed(res, "broker.m3.restart_does_not_heal_policy_outage"), 0)

    def fault_campaign(self, argv):
        res = Results("broker")
        stats = bo.run_fault_groups(Proto("p", argv), random.Random("31:broker"), 12, res, 1)
        return res, stats

    def test_real_adapter_passes_fault_groups(self):
        res, stats = self.fault_campaign([str(ADAPTER)])
        self.assertFalse(res.failed(), res.to_json()["violations"][:3])
        self.assertGreater(stats["fault_group_runs_with_storage_refusals"], 0)
        for inv in ("broker.m3.fault_group_equals_a_serialization", "broker.m3.fault_no_grant_overuse",
                    "broker.m3.fault_at_most_one_dispatch_per_key", "broker.m3.fault_no_duplicate_effect",
                    "broker.m3.fault_effect_only_with_dispatched_receipt_and_consumed_permit",
                    "broker.m3.fault_uncertain_receipt_never_redispatched",
                    "broker.m3.fault_reconcile_resolves_only_from_evidence", "broker.m3.fault_recovery_final_state"):
            self.assertGreater(passed(res, inv), 0, inv)

    def test_fault_group_mutants_fail(self):
        expect = {
            "f_group_outcome": "broker.m3.fault_group_equals_a_serialization",
            "f_grant_overuse": "broker.m3.fault_no_grant_overuse",
            "f_double_dispatch": "broker.m3.fault_at_most_one_dispatch_per_key",
            "f_duplicate_effect": "broker.m3.fault_no_duplicate_effect",
            "f_effect_without_permit": "broker.m3.fault_effect_only_with_dispatched_receipt_and_consumed_permit",
            "f_unsafe_reconcile": "broker.m3.fault_recovery_final_state",
            "f_redispatch": "broker.m3.fault_uncertain_receipt_never_redispatched",
            "f_status_swap": "broker.m3.fault_group_equals_a_serialization",
            "f_resume_conflict": "broker.m3.fault_recovery_retry_answer",
        }
        for mode, inv in expect.items():
            with self.subTest(mode=mode):
                res, _ = self.fault_campaign(self.mutant(mode))
                self.assertGreater(failed(res, inv), 0, mode)


if __name__ == "__main__":
    unittest.main()
