"""Host-only lifecycle controller. The private control host supplies the adapters."""

import fcntl
import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Approval:
    # Constructed only from an owner-reviewed, host-private approval.
    run_id: int
    attempt: int
    job_id: int
    source_sha: str
    control_sha: str
    source_archive_sha256: str
    image_sha256: str
    manifest_sha256: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name in ("run_id", "attempt", "job_id"):
                if type(value) is not int or not 0 < value < 2**63:
                    raise ValueError(name)
            else:
                length = 40 if name in ("source_sha", "control_sha") else 64
                if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
                    raise ValueError(name)


@dataclass(frozen=True)
class ControlPolicy:
    control_repo: str
    control_ref: str
    runner_group_id: int


def verify_job(record, policy, facts, *, terminal=False):
    """Compare normalized host-fetched facts; never accept workflow-supplied facts."""
    approval = record["approval"]
    expected = {
        "control_repo": policy.control_repo, "control_ref": policy.control_ref,
        "head_repo": policy.control_repo, "fork": False, "private": True, "protected": True, "workflow": ".github/workflows/writ-containment.yml",
        "control_sha": approval["control_sha"], "event": "workflow_dispatch",
        "run_id": approval["run_id"], "attempt": approval["attempt"], "job_id": approval["job_id"],
        "job_name": "Writ M5a containment", "runner_group_id": policy.runner_group_id,
        "label": f"writ-{approval['run_id']}-{approval['attempt']}",
        "source_repo": "infinri/Writ", "source_ref": "refs/heads/v2/architecture-rebuild",
        "source_sha": approval["source_sha"], "source_on_ref": True,
        "restricted_group": True, "owner_dispatch": True,
        "status": "completed" if terminal else "queued",
    }
    if terminal:
        expected.update(conclusion="success", assigned_to_this_guest=True)
    if any(type(facts.get(k)) is not type(v) or facts[k] != v for k, v in expected.items()):
        raise ValueError("unadmitted job")


@contextmanager
def exclusive_controller(directory):
    """Caller supplies a private persistent directory on the single control host."""
    with (directory / "controller.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect(directory / "controller.sqlite")
        try:
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, sha TEXT NOT NULL, run INTEGER NOT NULL,
                attempt INTEGER NOT NULL, payload TEXT NOT NULL, deadline INTEGER NOT NULL,
                stage TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                evidence TEXT, destroyed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run, attempt))""")
            yield db
        finally:
            db.close()


def update(db, row_id, **fields):
    with db:
        db.execute("UPDATE runs SET " + ", ".join(f"{key}=?" for key in fields) +
                   " WHERE id=?", [*fields.values(), row_id])


def finish(db, host, row_id, record, reason, evidence=None, *, clock=time.time):
    # Cleanup calls are independently attempted, bounded and idempotent.
    # On restart, missing confirmation permanently invalidates the old attempt.
    update(db, row_id, stage="cleanup", reason=reason, evidence=evidence)
    if reason:
        try:
            host.cancel(record)
        except Exception:
            reason = "cancel-unconfirmed"
    destroyed = False
    try:
        host.destroy(record)
        destroyed = host.absent(record) is True
    except Exception:
        pass
    if not destroyed:
        reason = "cleanup-unconfirmed"
    if not reason and clock() >= record["deadline"]:
        reason = "deadline-expired"
    update(db, row_id, reason=reason, destroyed=int(destroyed))
    state = "failure" if reason else "success"
    host.publish(record, state)
    if destroyed and reason != "cancel-unconfirmed":
        update(db, row_id, stage="finished")
    return state


def recover(db, host):
    for row in db.execute("SELECT * FROM runs WHERE stage != 'finished' ORDER BY id").fetchall():
        # A publish may have succeeded before acknowledgement. Recovery is conservative.
        finish(db, host, row["id"], json.loads(row["payload"]), "interrupted", row["evidence"])


def run(db, host, approval, manifest, policy, *, clock=time.time):
    """Invoke under exclusive_controller. All adapter I/O must enforce deadlines."""
    if db.execute("SELECT 1 FROM runs WHERE stage != 'finished'").fetchone():
        raise RuntimeError("pending lifecycle requires recovery")
    newest = db.execute("SELECT run, attempt FROM runs WHERE sha=? ORDER BY id DESC LIMIT 1",
                        (approval.source_sha,)).fetchone()
    if newest and (approval.run_id, approval.attempt) <= tuple(newest):
        raise ValueError("stale or consumed approval")
    binding = asdict(approval)
    del binding["control_sha"], binding["job_id"]
    binding["challenge"] = secrets.token_hex(32)
    start = int(clock())
    record = {"policy": asdict(policy), "approval": asdict(approval), "binding": binding, "queue_deadline": start + 300,
              "deadline": start + 3000}
    with db:
        row_id = db.execute("""INSERT INTO runs(sha,run,attempt,payload,deadline,stage)
            VALUES(?,?,?,?,?,'admission')""", (approval.source_sha, approval.run_id,
            approval.attempt, json.dumps(record), record["deadline"])).lastrowid

    reason = "admission"
    evidence = None
    try:
        host.publish(record, "pending")
        if hashlib.sha256(manifest).hexdigest() != approval.manifest_sha256:
            raise ValueError("unapproved manifest")
        verify_job(record, policy, host.authenticate_queued(record))
        if clock() >= record["queue_deadline"]:
            raise TimeoutError()
        reason = "provision"
        update(db, row_id, stage=reason)
        host.provision(record)
        reason = "runner-unavailable"
        if clock() >= record["queue_deadline"]:
            raise TimeoutError()
        host.wait_running(record)
        if clock() >= record["queue_deadline"]:
            raise TimeoutError()
        reason = "job-unsuccessful"
        # The adapter reauthenticates job identity, exact attempt, assigned runner,
        # and terminal conclusion directly against GitHub.
        verify_job(record, policy, host.wait_terminal(record), terminal=True)
        if clock() >= record["deadline"]:
            raise ValueError("job failed")
        reason = "evidence-invalid"
        raw = host.stop_and_read_evidence(record)
        from gate_evidence import validate
        validate(raw, binding, manifest)
        evidence = hashlib.sha256(raw).hexdigest()
        if clock() >= record["deadline"]:
            raise TimeoutError()
        reason = ""
    except Exception:
        pass
    return finish(db, host, row_id, record, reason, evidence, clock=clock)
