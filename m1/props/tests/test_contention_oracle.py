"""Negative and regression tests for the D-026 concurrency oracle
(m1/props/wprops/c_broker.py; LEDGER-SQLITE.md "Contention and the concurrency
oracle"). Synthetic evidence only: these prove the checker rejects what the
allowance must never hide, independently of either prototype.

  python3 -m unittest discover -s m1/props/tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROPS))
from wprops import c_broker as cb  # noqa: E402

SCHEMA = (PROPS.parent / "fixtures/broker/ledger.sql").read_text()
BUSY = {"code": "busy", "at": "begin"}


def tx(outcome="commit", error=None):
    return {"begin_ns": 1, "body_ns": 1, "end_ns": 1, "outcome": outcome, "error": error}


def completed():
    return {"op": "execute", "tx": {n: tx() for n in cb.LEDGER_TX}}


def aborted(audit=True):
    t = {"reserve": tx("rollback", BUSY)}
    if audit:
        t["audit"] = tx()
    return {"op": "execute", "tx": t}


def busy_after(stage):
    chain = list(cb.LEDGER_TX[:cb.LEDGER_TX.index(stage)])
    t = {n: tx() for n in chain}
    t[stage] = tx("rollback", BUSY)
    t["audit"] = tx()
    return {"op": "execute", "tx": t}


def group(*ops):
    return {"op": "concurrent", "operations": list(ops)}


def case(n=3, variant="W1", grant=None, shared_key=False):
    keys = ["k1"] * n if shared_key else [f"u{i}" for i in range(n)]
    grants = {f"{variant}|0": grant} if grant else {}
    return {"id": "t", "meta": {"n": n, "grants": grants,
                                "op_types": [[variant, 0, "", grant, None, k] for k in keys]}}


def final(effects, consumed, receipts, grant_uses=None):
    return {"effects_applied": effects, "consumed_permits": consumed, "receipts": receipts,
            "grant_uses": grant_uses or {}}


class Oracle(unittest.TestCase):
    def test_verified_abort_is_an_aborted_no_op(self):
        ok = cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                             final(2, 2, {"observed_success": 2}), group(aborted(), completed(), completed()))
        self.assertTrue(ok)

    def test_abort_cannot_mask_an_unauthorized_effect(self):
        ok = cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                             final(3, 3, {"observed_success": 3}), group(aborted(), completed(), completed()))
        self.assertFalse(ok)

    def test_abort_cannot_mask_a_receipt(self):
        ok = cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                             final(2, 2, {"observed_success": 2, "reserved": 1}), group(aborted(), completed(), completed()))
        self.assertFalse(ok)

    def test_abort_cannot_mask_a_grant_use(self):
        c = case(n=1, variant="P1", grant="g")
        self.assertTrue(cb.serializable(c, {"LEDGER_UNAVAILABLE": 1}, final(0, 0, {}, {"g": 0}), group(aborted())))
        self.assertFalse(cb.serializable(c, {"LEDGER_UNAVAILABLE": 1}, final(0, 0, {}, {"g": 1}), group(aborted())))

    def test_storage_refusal_without_evidence_is_never_harmless(self):
        ok = cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1}, final(2, 2, {"observed_success": 2}))
        self.assertFalse(ok)

    def test_every_storage_refusal_must_be_matched(self):
        ev = group(completed(), completed(), completed())
        self.assertFalse(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                                         final(2, 2, {"observed_success": 2}), ev))
        self.assertFalse(cb.serializable(case(), {"effect_completed": 3},
                                         final(3, 3, {"observed_success": 3}), group(aborted(), completed(), completed())))

    def test_timeout_after_committed_dispatch_stays_in_the_model(self):
        ev = group(busy_after("adapter"), completed(), completed())
        good = final(2, 2, {"observed_success": 2, "dispatched": 1})
        self.assertTrue(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1}, good, ev))
        excluded = final(2, 2, {"observed_success": 2})  # the committed dispatch marker silently dropped
        self.assertFalse(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1}, excluded, ev))
        with_effect = final(3, 3, {"observed_success": 2, "dispatched": 1})  # adapter never began: no effect allowed
        self.assertFalse(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1}, with_effect, ev))

    def test_timeout_after_reserve_keeps_reservation_and_grant_use(self):
        c = case(n=1, variant="P1", grant="g")
        ev = group(busy_after("dispatch"))
        self.assertTrue(cb.serializable(c, {"LEDGER_UNAVAILABLE": 1}, final(0, 0, {"reserved": 1}, {"g": 1}), ev))
        self.assertFalse(cb.serializable(c, {"LEDGER_UNAVAILABLE": 1}, final(0, 0, {}, {"g": 0}), ev))

    def test_reserved_receipt_resumes_without_second_grant_use(self):
        c = case(n=2, variant="P1", grant="g", shared_key=True)
        ev = group(busy_after("dispatch"), completed())
        ok = cb.serializable(c, {"LEDGER_UNAVAILABLE": 1, "effect_completed": 1},
                             final(1, 1, {"observed_success": 1}, {"g": 1}), ev)
        self.assertTrue(ok)

    def test_timeout_after_effect_is_uncertain_with_effect(self):
        ev = group(busy_after("observe"), completed(), completed())
        self.assertTrue(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                                        final(3, 3, {"observed_success": 2, "dispatched": 1}), ev))

    def test_unexpected_errors_always_fail(self):
        for err in ({"code": "other", "at": "begin"}, {"code": "busy", "at": "body"}, {"code": "busy", "at": "end"}):
            ev = group({"op": "execute", "tx": {"reserve": tx("rollback", err), "audit": tx()}}, completed(), completed())
            with self.subTest(err=err):
                self.assertFalse(cb.serializable(case(), {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1},
                                                 final(2, 2, {"observed_success": 2}), ev))

    def test_inconsistent_evidence_fails(self):
        bad = {"op": "execute", "tx": {"reserve": tx("rollback", BUSY), "dispatch": tx(), "audit": tx()}}
        self.assertEqual(cb.classify_op(bad)["category"], 5)
        self.assertEqual(cb.classify_op({"op": "execute"})["category"], 5)

    def test_aborted_operation_with_ledger_rows_fails(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "l.db"
            c = sqlite3.connect(db, isolation_level=None)
            c.executescript("BEGIN;" + SCHEMA + "COMMIT;")
            ev = group(aborted(), completed(), completed())
            obs, fin = {"effect_completed": 2, "LEDGER_UNAVAILABLE": 1}, final(2, 2, {"observed_success": 2})
            self.assertTrue(cb.serializable(case(), obs, fin, ev, db=str(db)))
            c.execute("INSERT INTO receipts VALUES ('fixture-tenant', 'u0', 'sha256:x', NULL, 'reserved')")
            c.close()
            self.assertFalse(cb.serializable(case(), obs, fin, ev, db=str(db)))

    def test_audit_failures_counted_only_from_evidence(self):
        timing = {"steps": [group(aborted(audit=False) | {"tx": {"reserve": tx("rollback", BUSY),
                                                                 "audit": tx("rollback", BUSY)}}, completed())]}
        self.assertEqual(cb.audit_failures(timing), 1)
        self.assertEqual(cb.audit_failures({"steps": [group(aborted(), completed())]}), 0)
        self.assertEqual(cb.classify_op({"op": "execute", "tx": {"audit": tx("rollback", {"code": "other", "at": "body"})}})
                         ["category"], 5)


if __name__ == "__main__":
    unittest.main()
