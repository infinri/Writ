"""Language-neutral SQLite ledger tests for owner decision D-025
(LEDGER-SQLITE.md "Storage failures" rules 4 to 6, S-1, S-2; C-3, C-4; A, B).

Expectations are written from the specification. Failures are injected only
from outside the prototypes (editing the ledger file between processes, file
system state); results are checked in the database and on stderr.

  WRIT_RUST=m1/rust/target/release/proto WRIT_GO=m1/go/proto \\
      python3 -m unittest discover -s m1/ledger_tests -p 'test_d025*' -v
"""
from __future__ import annotations

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wprops import c_broker as cb  # noqa: E402
from test_storage_failures import SCHEMA, UNAVAILABLE, REFUSED, PROTOS, per_proto, new_ledger  # noqa: E402

SECRETS = ["cred-alice", "cred-bob", "cred-agent", cb.FIX_KEY, cb.OTHER_KEY, "4" * 64]
P1 = cb.request("P1", "k1")
W1 = cb.request("W1", "k1")


def run(proto: str, scen: dict, *ledger_args: str) -> tuple[dict, str]:
    line = json.dumps({"id": "s", "input": scen}, separators=(",", ":")).encode() + b"\n"
    p = subprocess.run([proto, "broker-scenario", "--ledger", "sqlite", *ledger_args], input=line,
                       capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr[-400:]
    out = [json.loads(ln) for ln in p.stdout.split(b"\n") if ln.strip()]
    assert len(out) == 1 and out[0]["id"] == "s", p.stdout[:300]
    return out[0]["output"], p.stderr.decode("utf-8", "replace")


def scenario(steps):
    return {"initial": cb.initial(), "steps": steps}


def q(db: Path, sql: str):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def exec_sql(db: Path, sql: str, *args):
    c = sqlite3.connect(db, isolation_level=None)
    try:
        c.execute(sql, args)
    finally:
        c.close()


def durable_state(db: Path) -> dict:
    return {t: q(db, f"SELECT * FROM {t} ORDER BY 1") for t in
            ("grants", "grant_uses", "receipts", "receipt_events", "adapter_consumed_permits", "adapter_effects",
             "counters")}


class Base(unittest.TestCase):
    def setUp(self):
        if not PROTOS:
            self.skipTest("set WRIT_RUST and/or WRIT_GO to prototype binaries")
        self._tmp = tempfile.TemporaryDirectory(prefix="writ-d025-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def assert_clean_stderr(self, err: str, *paths: Path):
        for p in paths:
            for part in {str(p), p.name, str(p.parent)}:
                self.assertNotIn(part, err, "stderr reveals a path")
        for s in SECRETS:
            self.assertNotIn(s, err, "stderr reveals a credential or key")


class StoredGrantIntegrity(Base):
    """C-4: a corrupted stored grant is a ledger-integrity failure, never an allow."""

    def approved(self, d: Path) -> tuple[Path, str]:
        db = d / "l.db"
        out, _ = run(self.proto, scenario([cb.approve(P1, "g1")]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "grant_issued"}])
        return db, q(db, "SELECT binding FROM grants WHERE label = 'g1'")[0][0]

    def corrupt_then_execute(self, d: Path, mutate) -> tuple[dict, str, Path]:
        db, binding = self.approved(d)
        exec_sql(db, "UPDATE grants SET binding = ? WHERE label = 'g1'", mutate(binding))
        out, err = run(self.proto, scenario([cb.execute(P1, "g1")]), "--ledger-db", str(db))
        return out, err, db

    def assert_integrity_refusal(self, out, err, db):
        self.assertEqual(out["steps"], [REFUSED])
        self.assertEqual(q(db, "SELECT uses FROM grants WHERE label = 'g1'"), [(0,)])
        for table in ("grant_uses", "receipts", "receipt_events", "adapter_effects"):
            self.assertEqual(q(db, f"SELECT count(*) FROM {table}"), [(0,)], table)
        self.assert_clean_stderr(err, db)

    @per_proto
    def test_unparsable_binding(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(d, lambda b: b[:-5]))

    @per_proto
    def test_wrong_members(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(d, lambda b: '{"verdict":"approved"}'))

    @per_proto
    def test_extra_member(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(d, lambda b: b[:-1] + ',"zz":"x"}'))

    @per_proto
    def test_duplicate_member(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(d, lambda b: b[:-1] + ',"verdict":"approved"}'))

    @per_proto
    def test_not_canonical_text(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(d, lambda b: b.replace('","', '", "', 1)))

    @per_proto
    def test_wrong_member_type(self, proto, d):
        self.proto = proto
        self.assert_integrity_refusal(*self.corrupt_then_execute(
            d, lambda b: json.dumps(dict(json.loads(b), requested_capabilities="none"), sort_keys=True,
                                    separators=(",", ":"))))

    @per_proto
    def test_well_formed_but_different_binding_is_mismatch(self, proto, d):
        self.proto = proto
        out, _err, db = self.corrupt_then_execute(
            d, lambda b: json.dumps(dict(json.loads(b), tenant_id="other-tenant"), sort_keys=True,
                                    separators=(",", ":")))
        self.assertEqual(out["steps"], [{"result": "refused", "code": "GRANT_MISMATCH"}])
        self.assertEqual(q(db, "SELECT uses FROM grants WHERE label = 'g1'"), [(0,)])


class RetryAndPermit(Base):
    @per_proto
    def test_resumed_reservation_changes_no_durable_state_and_uses_no_second_grant(self, proto, d):
        """D-025 A and C-3: resume from `reserved` writes nothing in T-reserve."""
        db = d / "l.db"
        out, _ = run(proto, scenario([cb.approve(P1, "g1"), cb.execute(P1, "g1", fault="crash_after_reserve")]),
                     "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "grant_issued"}, {"result": "crashed"}])
        before = durable_state(db)
        self.assertEqual(q(db, "SELECT status FROM receipts"), [("reserved",)])
        out, _ = run(proto, scenario([cb.execute(P1, "g1")]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "effect_completed"}])
        after = durable_state(db)
        self.assertEqual(after["grants"], before["grants"], "grant uses changed on resume")
        self.assertEqual(after["grant_uses"], before["grant_uses"], "a second grant use was recorded")
        self.assertEqual(q(db, "SELECT cause FROM receipt_events ORDER BY seq"),
                         [("reserve",), ("dispatch",), ("observe",)])
        self.assertEqual(q(db, "SELECT count(*) FROM adapter_effects"), [(1,)])

    @per_proto
    def test_unverified_permit_applies_no_effect(self, proto, d):
        """D-025 B: only a fully verified permit inserts a consumed id and an effect."""
        db = d / "l.db"
        steps = [cb.execute(W1),
                 {"op": "present_permit", "of_step": 0, "request": W1, "tamper": "mac"},
                 {"op": "present_permit", "of_step": 0, "request": W1, "tamper": "expiry"},
                 {"op": "present_permit", "of_step": 0, "request": cb.request("W2", "k9")},
                 {"op": "present_permit", "of_step": 0, "request": W1}]
        out, _ = run(proto, scenario(steps), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "effect_completed"},
                                        {"result": "refused", "code": "PERMIT_INVALID"},
                                        {"result": "refused", "code": "PERMIT_INVALID"},
                                        {"result": "refused", "code": "PERMIT_REQUEST_MISMATCH"},
                                        {"result": "refused", "code": "PERMIT_REPLAYED"}])
        self.assertEqual(q(db, "SELECT count(*) FROM adapter_effects"), [(1,)])
        self.assertEqual(q(db, "SELECT count(*) FROM adapter_consumed_permits"), [(1,)])


class OpenOrder(Base):
    @per_proto
    def test_rejected_non_wal_file_is_byte_identical(self, proto, d):
        """S-1: verification precedes journal_mode=WAL, so a rejected file keeps its header."""
        db = d / "foreign.db"
        c = sqlite3.connect(db, isolation_level=None)
        c.execute("PRAGMA journal_mode=DELETE")
        c.executescript("BEGIN IMMEDIATE;" + SCHEMA + "UPDATE meta SET value = '2' WHERE key = 'schema_version';COMMIT;")
        c.close()
        before = db.read_bytes()
        out, err = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out, UNAVAILABLE)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["foreign.db"])
        self.assert_clean_stderr(err, db)

    @per_proto
    def test_valid_non_wal_ledger_is_accepted_and_switched_to_wal(self, proto, d):
        db = d / "l.db"
        c = sqlite3.connect(db, isolation_level=None)
        c.execute("PRAGMA journal_mode=DELETE")
        c.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
        c.close()
        out, _ = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out["steps"], [{"result": "effect_completed"}])
        self.assertEqual(q(db, "PRAGMA journal_mode"), [("wal",)])

    @per_proto
    def test_created_file_removed_when_initialization_fails(self, proto, d):
        """S-2: the run removes only what it created; a pre-existing obstacle is kept."""
        db = d / "new.db"
        (d / "new.db-wal").mkdir()
        out, err = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out, UNAVAILABLE)
        self.assertFalse(db.exists(), "created database file left behind")
        self.assertTrue((d / "new.db-wal").is_dir(), "pre-existing path removed")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["new.db-wal"])
        self.assert_clean_stderr(err, db)

    @per_proto
    def test_pre_existing_file_never_removed(self, proto, d):
        db = d / "garbage.db"
        db.write_bytes(b"x" * 4096)
        out, err = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out, UNAVAILABLE)
        self.assertEqual(db.read_bytes(), b"x" * 4096)
        self.assert_clean_stderr(err, db)

    @per_proto
    def test_missing_directory_stderr_is_clean(self, proto, d):
        db = d / "secret-dir-name" / "l.db"
        out, err = run(proto, scenario([cb.execute(W1)]), "--ledger-db", str(db))
        self.assertEqual(out, UNAVAILABLE)
        self.assert_clean_stderr(err, db)
        self.assertNotIn("secret-dir-name", err)


if __name__ == "__main__":
    unittest.main()
