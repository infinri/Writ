import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from host_control import Approval, ControlPolicy, exclusive_controller, recover, run


class Host:
    def __init__(self):
        self.fault = None
        self.events = []
        self.present = False
        self.now = 1

    def publish(self, record, status):
        self.events.append(status)

    def authenticate_queued(self, record):
        self.events.append("authenticate")
        return self.facts()

    def facts(self):
        return {
            "control_repo": "owner/control", "control_ref": "refs/heads/main",
            "head_repo": "owner/control", "fork": self.fault == "fork",
            "private": self.fault != "public", "protected": True,
            "workflow": ".github/workflows/writ-containment.yml", "control_sha": "2"*40,
            "event": "workflow_dispatch", "run_id": 17, "attempt": 1, "job_id": 33,
            "job_name": "Writ M5a containment", "runner_group_id": 7, "label": "writ-17-1",
            "source_repo": "infinri/Writ", "source_ref": "refs/heads/v2/architecture-rebuild",
            "source_sha": "1"*40, "source_on_ref": True, "restricted_group": True,
            "owner_dispatch": True, "status": "queued",
        }

    def provision(self, record):
        self.present = True
        if self.fault == "provision":
            raise OSError()

    def wait_running(self, record):
        if self.fault == "missing-runner":
            raise TimeoutError()

    def wait_terminal(self, record):
        conclusion = "cancelled" if self.fault in ("cancel", "cleanup") else "success"
        return dict(self.facts(), assigned_to_this_guest=True, status="completed",
                    conclusion=conclusion)

    def stop_and_read_evidence(self, record):
        self.events.append("evidence")
        if self.fault == "malformed":
            return b"{"
        results = {} if self.fault == "incomplete" else {"CHECK": {"outcome": "pass", "exit_code": 0}}
        return json.dumps(dict(record["binding"], results=results)).encode("ascii")

    def cancel(self, record):
        self.events.append("cancel")

    def destroy(self, record):
        self.events.append("destroy")
        if self.fault == "late-destroy":
            self.now = record["deadline"]
        if self.fault not in ("cleanup", "cleanup-after-success"):
            self.present = False

    def absent(self, record):
        return not self.present


class ControllerTest(unittest.TestCase):
    policy = ControlPolicy("owner/control", "refs/heads/main", 7)
    manifest = b'["CHECK"]'
    approval = Approval(17, 1, 33, "1"*40, "2"*40, "3"*64, "4"*64,
                        hashlib.sha256(manifest).hexdigest())

    def test_evidence_and_destruction_required_for_success(self):
        for fault, expected in ((None, "success"), ("incomplete", "failure"), ("malformed", "failure"),
                                ("late-destroy", "failure"), ("cleanup-after-success", "failure")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                host = Host()
                host.fault = fault
                with exclusive_controller(Path(directory)) as db:
                    self.assertEqual(run(db, host, self.approval, self.manifest, self.policy,
                                         clock=lambda: host.now), expected)
                    row = db.execute("SELECT * FROM runs").fetchone()
                    self.assertEqual(row["evidence"] is not None, fault not in ("incomplete", "malformed"))
                    if expected == "failure":
                        self.assertNotIn("success", host.events)
                    if fault == "cleanup-after-success":
                        self.assertEqual((row["destroyed"], row["stage"]), (0, "cleanup"))
                    if fault == "late-destroy":
                        self.assertEqual(row["reason"], "deadline-expired")
                    self.assertEqual(host.events[-2:], ["destroy", expected])

    def test_failed_attempts_and_cleanup_recovery(self):
        for fault, expected in (("public", "admission"), ("fork", "admission"), ("missing-runner", "runner-unavailable"),
                                ("provision", "provision"), ("cancel", "job-unsuccessful"),
                                ("cleanup", "cleanup-unconfirmed")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                host = Host()
                host.fault = fault
                with exclusive_controller(Path(directory)) as db:
                    status = run(db, host, self.approval, self.manifest, self.policy)
                    self.assertEqual(status, "failure")
                    self.assertEqual(host.events, ["pending", "authenticate", "cancel", "destroy", "failure"])
                    row = db.execute("SELECT * FROM runs").fetchone()
                    self.assertEqual(row["reason"], expected)
                    self.assertEqual(row["destroyed"], fault != "cleanup")
                with exclusive_controller(Path(directory)) as db:
                    if fault == "cleanup":
                        with self.assertRaises(RuntimeError):
                            run(db, host, self.approval, self.manifest, self.policy)
                        host.fault = None
                        recover(db, host)
                        self.assertFalse(host.present)
                        self.assertEqual(host.events[-3:], ["cancel", "destroy", "failure"])
                    with self.assertRaises(ValueError):
                        run(db, host, self.approval, self.manifest, self.policy)


if __name__ == "__main__":
    unittest.main()
