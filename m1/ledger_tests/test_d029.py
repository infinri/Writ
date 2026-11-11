"""Language-neutral tests for owner decision D-029 (LEDGER-SQLITE.md "Broker
coordination gate and state conflicts"): reconcile never changes an in-flight
execution in the same process; a receipt transition that matches no row is a
STATE_CONFLICT, not corruption; cross-process reconciliation is NOT protected
(demonstration of the safety properties only).

  WRIT_RUST=m1/rust/target/release/proto WRIT_GO=m1/go/proto \\
      python3 -m unittest discover -s m1/ledger_tests -p 'test_d029*' -v
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

M1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(M1 / "props"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wprops import c_broker as cb  # noqa: E402
from test_storage_failures import PROTOS, REFUSED, add_trigger, new_ledger, rows  # noqa: E402

PROFILES = ("primary", "cold")


def scenario(steps):
    return {"initial": cb.initial(), "steps": steps}


def call(proto, scen, db, *extra, timeout=300):
    line = json.dumps({"id": "s", "input": scen}, separators=(",", ":")).encode() + b"\n"
    p = subprocess.run([proto, "broker-scenario", "--ledger", "sqlite", "--ledger-db", str(db), *extra],
                       input=line, capture_output=True, timeout=timeout)
    assert p.returncode == 0, p.stderr[-400:]
    return json.loads(p.stdout)["output"], p.stderr.decode("utf-8", "replace")


def integrity(db) -> list:
    return rows(db, "PRAGMA integrity_check") + rows(db, "PRAGMA foreign_key_check")


def mixed_group(n_exec: int, n_crash: int, n_rec: int, salt: str) -> list:
    ops = [cb.execute(cb.request("W1", f"{salt}e{i}")) for i in range(n_exec)]
    ops += [cb.execute(cb.request("W1", f"{salt}c{i}"), fault="crash_after_dispatch") for i in range(n_crash)]
    ops += [{"op": "reconcile"} for _ in range(n_rec)]
    return ops


class Base(unittest.TestCase):
    def setUp(self):
        if not PROTOS:
            self.skipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        self._tmp = tempfile.TemporaryDirectory(prefix="writ-d029-")
        self.tmp = Path(self._tmp.name)
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def db(self) -> Path:
        self.n += 1
        return self.tmp / f"l{self.n}.db"


class InProcessGate(Base):
    def test_reconcile_never_touches_an_in_flight_execute(self):
        for name, proto in PROTOS.items():
            for prof in PROFILES:
                for rep in range(15):
                    db = self.db()
                    ops = mixed_group(8, 3, 8, f"r{rep}")
                    out, err = call(proto, scenario([{"op": "concurrent", "operations": ops}]), db,
                                    "--concurrency-profile", prof)
                    with self.subTest(prototype=name, profile=prof, rep=rep):
                        oc = out["steps"][0]["outcomes"]
                        self.assertEqual(oc.get("effect_completed"), 8, (oc, err))
                        self.assertEqual(oc.get("crashed"), 3, oc)
                        self.assertEqual(oc.get("reconciled"), 8, oc)
                        self.assertNotIn("LEDGER_UNAVAILABLE", oc, err)
                        self.assertNotIn("CONFLICT", err)
                        self.assertNotIn("CORRUPT", err)
                        status = dict(rows(db, "SELECT status, count(*) FROM receipts GROUP BY status"))
                        self.assertEqual(status.get("observed_success"), 8, status)
                        self.assertEqual(status.get("dispatched", 0) + status.get("unknown_reconcile", 0), 3, status)
                        unk = rows(db, "SELECT idempotency_key FROM receipts WHERE status = 'unknown_reconcile'")
                        self.assertTrue(all(k.startswith(f"r{rep}c") for (k,) in unk), unk)
                        recon = rows(db, "SELECT count(*) FROM receipt_events WHERE cause = 'reconcile'")[0][0]
                        self.assertEqual(recon, status.get("unknown_reconcile", 0))
                        self.assertEqual(out["final"]["effects_applied"], 11)
                        self.assertEqual(out["final"]["consumed_permits"], 11)
                        self.assertEqual(integrity(db), [("ok",)])

    def test_64_way_executes_and_reconciles_do_not_deadlock(self):
        for name, proto in PROTOS.items():
            db = self.db()
            out, err = call(proto, scenario([{"op": "concurrent", "operations": mixed_group(48, 8, 8, "x")}]), db,
                            timeout=120)
            with self.subTest(prototype=name):
                oc = out["steps"][0]["outcomes"]
                self.assertEqual(sum(oc.values()), 64)
                self.assertNotIn("CONFLICT", err)
                self.assertNotIn("CORRUPT", err)
                # busy timeouts are possible under contention (D-026); a state conflict is not
                self.assertEqual(integrity(db), [("ok",)])

    def test_post_return_reconciliation_is_unchanged(self):
        for name, proto in PROTOS.items():
            db = self.db()
            w = cb.request("W1", "k1")
            out, _ = call(proto, scenario([cb.execute(w, fault="crash_after_dispatch"), {"op": "reconcile"},
                                           cb.execute(w), {"op": "reconcile"}]), db)
            with self.subTest(prototype=name):
                self.assertEqual(out["steps"], [{"result": "crashed"}, {"result": "reconciled", "count": 1},
                                                {"result": "refused", "code": "RECONCILIATION_REQUIRED"},
                                                {"result": "reconciled", "count": 0}])
                self.assertEqual(out["final"]["receipts"], {"unknown_reconcile": 1})
                self.assertEqual(out["final"]["effects_applied"], 1)


class StateConflict(Base):
    TRIGGER = ("CREATE TRIGGER foreign_reconcile AFTER UPDATE OF status ON receipts WHEN NEW.status = 'dispatched' "
               "BEGIN UPDATE receipts SET status = 'unknown_reconcile' "
               "WHERE tenant_id = NEW.tenant_id AND idempotency_key = NEW.idempotency_key; END")

    def test_zero_row_transition_is_a_state_conflict_not_corruption(self):
        for name, proto in PROTOS.items():
            db = self.db()
            new_ledger(db)
            add_trigger(db, self.TRIGGER)
            out, err = call(proto, scenario([cb.execute(cb.request("W1", "k1"))]), db)
            with self.subTest(prototype=name):
                self.assertEqual(out["steps"], [REFUSED])
                self.assertEqual(out["final"]["receipts"], {"unknown_reconcile": 1})
                self.assertEqual(out["final"]["effects_applied"], 1)  # the effect began; the receipt stays uncertain
                lines = [ln for ln in err.splitlines() if ln.strip()]
                self.assertTrue(any("observe" in ln and "STATE_CONFLICT" in ln for ln in lines), err)
                self.assertNotIn("CORRUPT", err)
                self.assertNotIn(str(db), err)
                self.assertEqual(integrity(db), [("ok",)])

    def test_state_conflict_evidence_is_category_5(self):
        for name, proto in PROTOS.items():
            db = self.db()
            new_ledger(db)
            add_trigger(db, self.TRIGGER)
            line = json.dumps({"id": "s", "input": scenario([cb.execute(cb.request("W1", "k1"))])}).encode() + b"\n"
            p = subprocess.run([proto, "bench-broker-scenario", "--ledger", "sqlite", "--ledger-db", str(db)],
                               input=line, capture_output=True, timeout=60)
            step = json.loads(p.stdout)["output"]["timing"]["steps"][0]
            with self.subTest(prototype=name):
                self.assertEqual(step["tx"]["observe"]["error"], {"code": "other", "at": "body"})
                self.assertEqual(cb.classify_op(step)["category"], 5)


class CrossProcessDemonstration(Base):
    """Not a guarantee: the gate is process-local. A reconcile in another process may
    interleave with an in-flight execution; only the fail-closed safety properties are
    checked, and whether a conflict happened is reported, not required."""

    def test_cross_process_reconcile_stays_fail_closed(self):
        observed = {}
        for name, proto in PROTOS.items():
            db = self.db()
            new_ledger(db)
            execs = scenario([{"op": "concurrent", "operations": mixed_group(16, 0, 0, f"p{i}")} for i in range(6)])
            recs = scenario([{"op": "reconcile"} for _ in range(200)])
            enc = lambda s: json.dumps({"id": "s", "input": s}).encode() + b"\n"  # noqa: E731
            args = ["broker-scenario", "--ledger", "sqlite", "--ledger-db", str(db)]
            a = subprocess.Popen([proto, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            b = subprocess.Popen([proto, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out_b, _ = b.communicate(enc(recs), timeout=300)
            out_a, err_a = a.communicate(enc(execs), timeout=300)
            ra = json.loads(out_a)["output"]
            with self.subTest(prototype=name):
                completed = sum(s["outcomes"].get("effect_completed", 0) for s in ra["steps"])
                unavailable = sum(s["outcomes"].get("LEDGER_UNAVAILABLE", 0) for s in ra["steps"])
                status = dict(rows(db, "SELECT status, count(*) FROM receipts GROUP BY status"))
                effects = rows(db, "SELECT count(*) FROM adapter_effects")[0][0]
                consumed = rows(db, "SELECT count(*) FROM adapter_consumed_permits")[0][0]
                self.assertEqual(status.get("observed_success", 0), completed, "a success was reported for a reconciled receipt")
                self.assertEqual(effects, consumed, "an effect without a consumed permit, or the reverse")
                self.assertLessEqual(effects, 96, "more effects than executions")
                self.assertEqual(completed + unavailable, 96)
                self.assertNotIn("CORRUPT", err_a.decode())
                self.assertEqual(integrity(db), [("ok",)])
                observed[name] = {"state_conflicts": err_a.decode().count("STATE_CONFLICT"),
                                  "unknown_reconcile": status.get("unknown_reconcile", 0)}
        print(f"\n  cross-process demonstration (not a guarantee): {observed}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
