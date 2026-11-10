"""Language-neutral SQLite ledger storage-failure tests (D-024,
m1/fixtures/broker/LEDGER-SQLITE.md "Storage failures").

Expected outputs are written from the specification, not from either
prototype. Failures are injected without touching prototype code: through the
file system, a lock held by another connection, or RAISE(ABORT) triggers added
to the ledger file; the database is inspected afterwards.

  WRIT_RUST=m1/rust/target/release/proto WRIT_GO=m1/go/proto \\
      python3 -m unittest discover -s m1/ledger_tests -v
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

M1 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(M1 / "props"))
from wprops import c_broker as cb  # noqa: E402  (fixture keys, requests and seals)

SCHEMA = (M1 / "fixtures/broker/ledger.sql").read_text()
UNAVAILABLE = {"error": "LEDGER_UNAVAILABLE"}
REFUSED = {"result": "refused", "code": "LEDGER_UNAVAILABLE"}
PROTOS = {k: os.environ.get(v) for k, v in (("rust", "WRIT_RUST"), ("go", "WRIT_GO"))}
PROTOS = {k: str(Path(v).resolve()) for k, v in PROTOS.items() if v}


def scenario(steps):
    return {"initial": cb.initial(), "steps": steps}


def run(proto: str, scen: dict, *ledger_args: str, timeout=120) -> dict:
    line = json.dumps({"id": "s", "input": scen}, separators=(",", ":")).encode() + b"\n"
    p = subprocess.run([proto, "broker-scenario", "--ledger", "sqlite", *ledger_args], input=line,
                       capture_output=True, timeout=timeout)
    assert p.returncode == 0, p.stderr[-400:]
    lines = [ln for ln in p.stdout.split(b"\n") if ln.strip()]
    assert len(lines) == 1, p.stdout[:400]
    msg = json.loads(lines[0])
    assert msg["id"] == "s", msg
    return msg["output"]


def new_ledger(path: Path) -> None:
    """A valid ledger created from ledger.sql by Python's sqlite3 (WAL)."""
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
    c.close()


def content(path: Path) -> str:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
    finally:
        c.close()


def rows(path: Path, sql: str):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def add_trigger(path: Path, sql: str) -> None:
    c = sqlite3.connect(path, isolation_level=None)
    c.execute(sql)
    c.close()


W1 = cb.request("W1", "k1")
P1 = cb.request("P1", "k1")


class Base(unittest.TestCase):
    def setUp(self):
        if not PROTOS:
            self.skipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        self._tmp = tempfile.TemporaryDirectory(prefix="writ-ledger-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        self.tmp.chmod(0o755)
        self._tmp.cleanup()


def per_proto(fn):
    """Run the test body once per prototype, each in its own subTest."""
    def wrapper(self):
        for name, proto in PROTOS.items():
            with self.subTest(prototype=name):
                d = self.tmp / name
                d.mkdir()
                fn(self, proto, d)
    wrapper.__name__ = fn.__name__
    return wrapper


class OpenFailures(Base):
    """Rule 1: the whole scenario answers exactly {"error": "LEDGER_UNAVAILABLE"}; no step runs."""

    SCEN = scenario([cb.execute(W1)])

    @per_proto
    def test_missing_directory(self, proto, d):
        db = d / "missing" / "l.db"
        self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        self.assertFalse((d / "missing").exists())

    @per_proto
    def test_not_a_database(self, proto, d):
        db = d / "garbage.db"
        db.write_bytes(b"not a database " * 300)
        before = db.read_bytes()
        self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        self.assertEqual(db.read_bytes(), before)

    @per_proto
    def test_wrong_schema_version(self, proto, d):
        db = d / "v2.db"
        new_ledger(db)
        add_trigger(db, "UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        before = content(db)
        self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        self.assertEqual(content(db), before)

    @per_proto
    def test_missing_table(self, proto, d):
        db = d / "partial.db"
        new_ledger(db)
        add_trigger(db, "DROP TABLE adapter_effects")
        before = content(db)
        self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        self.assertEqual(content(db), before)

    @per_proto
    def test_read_only_file(self, proto, d):
        db = d / "ro.db"
        new_ledger(db)
        before = content(db)
        db.chmod(0o444)
        self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        self.assertEqual(content(db), before)

    @per_proto
    def test_read_only_ledger_dir(self, proto, d):
        ro = d / "ro-dir"
        ro.mkdir()
        ro.chmod(0o555)
        self.assertEqual(run(proto, self.SCEN, "--ledger-dir", str(ro)), UNAVAILABLE)
        self.assertEqual(list(ro.iterdir()), [])

    @per_proto
    def test_lock_held_beyond_busy_timeout(self, proto, d):
        db = d / "locked.db"
        new_ledger(db)
        before = content(db)
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(run(proto, self.SCEN, "--ledger-db", str(db)), UNAVAILABLE)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(content(db), before)


def fresh(d: Path, trigger: str | None = None) -> Path:
    db = d / "l.db"
    new_ledger(db)
    if trigger:
        add_trigger(db, trigger)
    return db


class OperationFailures(Base):
    """Rule 2: the failing transaction rolls back, the operation is refused
    LEDGER_UNAVAILABLE, earlier commits stay, and the scenario continues."""

    @per_proto
    def test_reserve_failure_rolls_back_grant_use(self, proto, d):
        trig = "CREATE TRIGGER inj BEFORE INSERT ON receipt_events BEGIN SELECT RAISE(ABORT, 'injected'); END"
        db = fresh(d, trig)
        out = run(proto, scenario([cb.approve(P1, "g1"), cb.execute(P1, "g1"), cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "grant_issued"}, REFUSED, REFUSED])
        self.assertEqual(out["final"], {"effects_applied": 0, "grant_uses": {"g1": 0}, "receipts": {},
                                        "consumed_permits": 0, "refusals": 2})
        self.assertEqual(rows(db, "SELECT uses FROM grants WHERE label = 'g1'"), [(0,)])
        for table in ("receipts", "receipt_events", "grant_uses", "adapter_effects"):
            self.assertEqual(rows(db, f"SELECT count(*) FROM {table}"), [(0,)], table)

    @per_proto
    def test_adapter_failure_leaves_dispatched_and_never_redispatches(self, proto, d):
        trig = "CREATE TRIGGER inj BEFORE INSERT ON adapter_consumed_permits BEGIN SELECT RAISE(ABORT, 'injected'); END"
        db = fresh(d, trig)
        out = run(proto, scenario([cb.execute(W1), cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [REFUSED, {"result": "refused", "code": "RECONCILIATION_REQUIRED"}])
        self.assertEqual(out["final"], {"effects_applied": 0, "grant_uses": {}, "receipts": {"dispatched": 1},
                                        "consumed_permits": 0, "refusals": 2})
        add_trigger(db, "DROP TRIGGER inj")
        out = run(proto, scenario([cb.execute(W1), {"op": "reconcile"}, cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "refused", "code": "RECONCILIATION_REQUIRED"},
                                        {"result": "reconciled", "count": 1},
                                        {"result": "refused", "code": "RECONCILIATION_REQUIRED"}])
        self.assertEqual(out["final"]["effects_applied"], 0)
        self.assertEqual(out["final"]["receipts"], {"unknown_reconcile": 1})

    @per_proto
    def test_observe_failure_after_effect_is_uncertain(self, proto, d):
        trig = ("CREATE TRIGGER inj BEFORE UPDATE OF status ON receipts WHEN NEW.status = 'observed_success' "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        db = fresh(d, trig)
        out = run(proto, scenario([cb.execute(W1), cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [REFUSED, {"result": "refused", "code": "RECONCILIATION_REQUIRED"}])
        self.assertEqual(out["final"], {"effects_applied": 1, "grant_uses": {}, "receipts": {"dispatched": 1},
                                        "consumed_permits": 1, "refusals": 2})

    @per_proto
    def test_audit_failure_keeps_refusal_and_continues(self, proto, d):
        trig = "CREATE TRIGGER inj BEFORE INSERT ON audit_refusals BEGIN SELECT RAISE(ABORT, 'injected'); END"
        db = fresh(d, trig)
        out = run(proto, scenario([cb.execute(W1, seal_kind="absent"), cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "refused", "code": "INGRESS_UNAUTHENTICATED"},
                                        {"result": "effect_completed"}])
        self.assertEqual(out["final"], {"effects_applied": 1, "grant_uses": {}, "receipts": {"observed_success": 1},
                                        "consumed_permits": 1, "refusals": 0})

    @per_proto
    def test_ledger_created_from_schema_file_is_accepted(self, proto, d):
        db = fresh(d)
        out = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "effect_completed"}])


class ExternalResponse(Base):
    @per_proto
    def test_ledger_unavailable_maps_to_promotion_refused(self, proto, d):
        p = subprocess.run([proto, "external-refusal"], input=b'{"id":"e","input":{"internal_code":"LEDGER_UNAVAILABLE"}}\n',
                           capture_output=True, timeout=30)
        self.assertEqual(json.loads(p.stdout)["output"], {"external": "PROMOTION_REFUSED"})


if __name__ == "__main__":
    unittest.main()
