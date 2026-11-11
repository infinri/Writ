"""M1 chaos workload E (owner decision 2026-11-11, M1-08 confirmation D): real
child-process termination (SIGKILL at a seeded random instant) of a prototype
using a file-backed SQLite ledger, then a restart in a new process on the same
file. Not an early-return simulation. Language-neutral; results are checked in
the database and in the restarted process's answers.

Invariants after the kill and after the restart:
  - SQLite integrity_check ok, foreign keys consistent;
  - no grant overuse (uses <= max_uses; grant_uses rows equal uses);
  - no duplicate dispatch (at most one dispatch event per key; next_permit equals
    the number of dispatch events) and no unauthorized effect (effects only for
    consumed permits, at most one per permit, consumed permits at most dispatches);
  - receipt state matches its events;
  - after restart: a dispatched or unknown_reconcile receipt answers
    RECONCILIATION_REQUIRED and never dispatches again; a reserved receipt resumes
    without a second grant use; an observed receipt replays;
  - a ledger made unwritable fails closed (whole scenario LEDGER_UNAVAILABLE, file
    unchanged).

Not covered (recorded limitations): power loss and OS crash durability; a
reconcile in another live process (D-029 gate is process-local); real external
effects (the adapter is simulated in the same file).

  WRIT_RUST=... WRIT_GO=... python3 -m unittest discover -s m1/ledger_tests -p 'test_chaos*' -v
  WRIT_CHAOS_ITERATIONS=50 WRIT_CHAOS_SEED=20261111 ... (campaign size; default 8, seed 20261111)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

M1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(M1 / "props"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wprops import c_broker as cb  # noqa: E402
from test_storage_failures import PROTOS, new_ledger, rows  # noqa: E402

ITERATIONS = int(os.environ.get("WRIT_CHAOS_ITERATIONS", "8"))
SEED = int(os.environ.get("WRIT_CHAOS_SEED", "20261111"))
UNAVAILABLE = {"error": "LEDGER_UNAVAILABLE"}


def line(i: int, steps: list) -> bytes:
    return json.dumps({"id": f"c{i}", "input": {"initial": cb.initial(), "steps": steps}},
                      separators=(",", ":")).encode() + b"\n"


def workload(rng: random.Random, it: int) -> tuple[list[bytes], list]:
    """Lines of approvals, sequential and concurrent executes over a small key pool, so
    a kill can land in any transaction. Returns the lines and every execute issued."""
    lines, issued = [], []
    for i in range(rng.randint(6, 12)):
        steps = []
        g = f"g{it}-{i}"
        steps.append(cb.approve(cb.request("P1", f"k{it}-{i}-p"), g))
        ex = cb.execute(cb.request("P1", f"k{it}-{i}-p"), g)
        steps.append(ex)
        issued.append(ex)
        group = []
        for j in range(rng.choice([8, 16, 32])):
            ex = cb.execute(cb.request("W1", f"k{it}-{i}-{rng.randint(0, 9)}"))
            group.append(ex)
            issued.append(ex)
        steps.append({"op": "concurrent", "operations": group})
        for _ in range(rng.randint(1, 4)):
            ex = cb.execute(cb.request("W1", f"k{it}-{i}-s{rng.randint(0, 3)}"))
            steps.append(ex)
            issued.append(ex)
        lines.append(line(i, steps))
    return lines, issued


def snapshot(db: Path) -> dict:
    st = {k: s for k, s in rows(db, "SELECT idempotency_key, status FROM receipts")}
    ev = {}
    for k, cause, n in rows(db, "SELECT idempotency_key, cause, count(*) FROM receipt_events GROUP BY 1, 2"):
        ev.setdefault(k, {})[cause] = n
    return {"status": st, "events": ev,
            "grants": {l: (u, m) for l, u, m in rows(db, "SELECT label, uses, max_uses FROM grants")},
            "grant_use_rows": dict(rows(db, "SELECT label, count(*) FROM grant_uses GROUP BY label")),
            "next_permit": (rows(db, "SELECT value FROM counters WHERE name = 'next_permit'") or [(0,)])[0][0],
            "consumed": rows(db, "SELECT count(*) FROM adapter_consumed_permits")[0][0],
            "effects": rows(db, "SELECT count(*) FROM adapter_effects")[0][0],
            "effect_permits_distinct": rows(db, "SELECT count(DISTINCT permit_id) FROM adapter_effects")[0][0],
            "orphan_effects": rows(db, "SELECT count(*) FROM adapter_effects WHERE permit_id NOT IN "
                                       "(SELECT permit_id FROM adapter_consumed_permits)")[0][0]}


class Chaos(unittest.TestCase):
    def setUp(self):
        if not PROTOS:
            self.skipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        self._tmp = tempfile.TemporaryDirectory(prefix="writ-chaos-")
        self.tmp = Path(self._tmp.name)
        self.report = {}

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        self._tmp.cleanup()
        if self.report:
            print(f"\n  chaos report (seed {SEED}, {ITERATIONS} iterations): {json.dumps(self.report)}", file=sys.stderr)

    def check_invariants(self, db: Path, where: str) -> dict:
        self.assertEqual(rows(db, "PRAGMA integrity_check"), [("ok",)], where)
        self.assertEqual(rows(db, "PRAGMA foreign_key_check"), [], where)
        s = snapshot(db)
        for label, (uses, mx) in s["grants"].items():
            self.assertLessEqual(uses, mx, f"{where}: grant overuse {label}")
            self.assertEqual(s["grant_use_rows"].get(label, 0), uses, f"{where}: grant_uses rows {label}")
        dispatches = 0
        for key, status in s["status"].items():
            e = s["events"].get(key, {})
            self.assertEqual(e.get("reserve"), 1, f"{where}: {key} reserve events")
            self.assertLessEqual(e.get("dispatch", 0), 1, f"{where}: duplicate dispatch {key}")
            dispatches += e.get("dispatch", 0)
            if status == "reserved":
                self.assertEqual((e.get("dispatch", 0), e.get("observe", 0)), (0, 0), f"{where}: {key}")
            elif status == "dispatched":
                self.assertEqual((e.get("dispatch", 0), e.get("observe", 0)), (1, 0), f"{where}: {key}")
            elif status in ("observed_success", "observed_failure"):
                self.assertEqual((e.get("dispatch", 0), e.get("observe", 0)), (1, 1), f"{where}: {key}")
            elif status == "unknown_reconcile":
                self.assertEqual((e.get("dispatch", 0), e.get("reconcile", 0)), (1, 1), f"{where}: {key}")
        self.assertEqual(s["next_permit"], dispatches, f"{where}: permits issued versus dispatch events")
        self.assertLessEqual(s["consumed"], dispatches, f"{where}: consumed permit without dispatch")
        self.assertLessEqual(s["effects"], s["consumed"], f"{where}: effect without consumed permit")
        self.assertEqual(s["effects"], s["effect_permits_distinct"], f"{where}: two effects for one permit")
        self.assertEqual(s["orphan_effects"], 0, where)
        return s

    def run_until_killed(self, proto: str, db: Path, payload: bytes, delay_s: float) -> str:
        p = subprocess.Popen([proto, "broker-scenario", "--ledger", "sqlite", "--ledger-db", str(db)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            p.stdin.write(payload)
            p.stdin.close()
        except BrokenPipeError:
            pass
        time.sleep(delay_s)
        if p.poll() is None:
            p.send_signal(signal.SIGKILL)
            p.wait(timeout=30)
            return "killed"
        p.wait(timeout=30)
        return "finished_before_kill"

    def restart(self, proto: str, db: Path, steps: list) -> tuple[dict, str]:
        p = subprocess.run([proto, "broker-scenario", "--ledger", "sqlite", "--ledger-db", str(db)],
                           input=line(0, steps), capture_output=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return json.loads(p.stdout)["output"], p.stderr.decode("utf-8", "replace")

    def test_kill_restart_reconcile(self):
        for name, proto in PROTOS.items():
            rng = random.Random(f"{SEED}:{name}")
            stats = {"iterations": 0, "killed": 0, "finished_before_kill": 0, "receipts_at_kill": {},
                     "restart_reconciliation_required": 0, "restart_resumed": 0, "restart_replayed": 0}
            for it in range(ITERATIONS):
                db = self.tmp / f"{name}-{it}.db"
                new_ledger(db)
                lines, issued = workload(rng, it)
                outcome = self.run_until_killed(proto, db, b"".join(lines), rng.uniform(0.02, 0.6))
                stats[outcome] += 1
                stats["iterations"] += 1
                with self.subTest(prototype=name, iteration=it, phase="after kill"):
                    before = self.check_invariants(db, f"{name} it{it} after kill")
                for s in before["status"].values():
                    stats["receipts_at_kill"][s] = stats["receipts_at_kill"].get(s, 0) + 1
                # restart: retry every request once (deduplicated by key), then reconcile, then retry again
                seen, retries = set(), []
                for ex in issued:
                    k = ex["request"]["idempotency_key"] if "request" in ex else json.dumps(ex, sort_keys=True)
                    if k not in seen:
                        seen.add(k)
                        retries.append(ex)
                out, err = self.restart(proto, db, retries + [{"op": "reconcile"}] + retries)
                with self.subTest(prototype=name, iteration=it, phase="after restart"):
                    self.assertNotIn("CORRUPT", err)
                    self.assertNotIn("STATE_CONFLICT", err)
                    after = self.check_invariants(db, f"{name} it{it} after restart")
                    first = out["steps"][:len(retries)]
                    for ex, res in zip(retries, first):
                        key = ex["request"]["idempotency_key"]
                        was = before["status"].get(key)
                        if was in ("dispatched", "unknown_reconcile"):
                            self.assertEqual(res, {"result": "refused", "code": "RECONCILIATION_REQUIRED"}, key)
                            stats["restart_reconciliation_required"] += 1
                        elif was == "reserved":
                            self.assertIn(res.get("result"), ("effect_completed", "effect_failed"), (key, res))
                            stats["restart_resumed"] += 1
                        elif was in ("observed_success", "observed_failure"):
                            self.assertTrue(res.get("replayed"), (key, res))
                            stats["restart_replayed"] += 1
                        e_before = before["events"].get(key, {}).get("dispatch", 0)
                        self.assertLessEqual(after["events"].get(key, {}).get("dispatch", 0) - e_before,
                                             0 if was in ("dispatched", "unknown_reconcile", "observed_success",
                                                          "observed_failure") else 1, f"redispatch {key}")
                    for label, (uses, _mx) in before["grants"].items():
                        if uses:
                            self.assertEqual(after["grants"][label][0], uses, f"second grant use {label}")
            self.report[name] = stats
            with self.subTest(prototype=name):
                self.assertGreater(stats["killed"], 0, "no iteration was actually killed")

    def test_unwritable_ledger_fails_closed_after_kill(self):
        for name, proto in PROTOS.items():
            rng = random.Random(f"{SEED}:{name}:ro")
            db = self.tmp / f"{name}-ro.db"
            new_ledger(db)
            lines, issued = workload(rng, 99)
            self.run_until_killed(proto, db, b"".join(lines), 0.2)
            for suffix in ("", "-wal", "-shm"):
                f = Path(str(db) + suffix)
                if f.exists():
                    f.chmod(0o444)
            digest = hashlib.sha256(db.read_bytes()).hexdigest()
            out, _ = self.restart(proto, db, [issued[0]])
            with self.subTest(prototype=name):
                self.assertEqual(out, UNAVAILABLE)
                self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), digest, "ledger changed while unwritable")


if __name__ == "__main__":
    unittest.main()
