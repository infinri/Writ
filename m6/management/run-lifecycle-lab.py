"""Run D-056 through the fixture CLI, reopening stores on each request."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug")).resolve()
MANAGE = BIN / "writ-manage-fixture"
EVALUATOR = BIN / "writ-evaluator-authority-fixture"
WAIVER = BIN / "writ-waiver-authority-fixture"
SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}


def command(binary, *args, body=None, error=None):
    process = subprocess.run(
        [binary, *args], input="" if body is None else json.dumps(body),
        text=True, capture_output=True, check=False,
    )
    result = json.loads(process.stdout)
    if error is not None:
        assert process.returncode != 0 and result["error"]["code"] == error, result
    else:
        assert process.returncode == 0, result
    return result


def credential(lab, name):
    path = lab / name
    path.write_text(name)
    return path


def invoke(mode, db, owners):
    def operation(name, caller, error=None, scope=SCOPE, **fields):
        return command(MANAGE, mode, db, caller, *owners, error=error, body={
            "version": "/manage/v1", "scope": scope, "operation": name, **fields,
        })
    return operation


def evaluator(lab, db, viewer, agent, service):
    ledger = lab / "evaluator.sqlite"
    publisher = credential(lab, "fixture-publisher")
    step_up = credential(lab, "fixture-evaluator-human")
    command(MANAGE, "init-evaluator", ledger)
    call = invoke("v1-evaluator", db, [ledger])
    entry = json.loads((ROOT / "docs/contracts/proposed/examples/evaluator-registry-entry.valid.json").read_text())
    entry.pop("command")
    entry.update(
        evaluator_id="doc-check", trust="E", publisher="fixture-publisher",
        published_at="2026-11-01T09:00:00Z", registry_revision=1,
        scope={"tenant_id": SCOPE["tenant"], "project_id": SCOPE["project"]},
    )
    entry["class"] = "evidence"

    history = {"evaluator_id": "doc-check", "after": 0, "limit": 50}
    publish = {"expected_revision": 0, "entry": entry}
    revoke = {"expected_revision": 1, "evaluator_id": "doc-check"}
    initial = call("evaluator_inspect", viewer, **history)
    assert initial["result"]["revision"] == 0 and initial["result"]["history"] == [], initial
    call("evaluator_preview_publish", viewer, "NOT_FOUND", **publish)
    call("evaluator_preview_publish", agent, "UNAUTHORIZED", **publish)
    call("evaluator_preview_publish", service, "NOT_FOUND", **publish)
    call("evaluator_inspect", viewer, "NOT_FOUND", scope=SCOPE | {"project": "other"}, **history)

    def authorize(name, fields, ceremony):
        preview = call(f"evaluator_preview_{name}", publisher, **fields)
        authorization = command(EVALUATOR, step_up, ceremony, body=preview)
        return fields | {
            "intent_digest": preview["result"]["intent_digest"],
            "idempotency_key": ceremony,
            "presentation": authorization["presentation"],
        }

    publish = authorize("publish", publish, "evaluator-publish")
    assert call("evaluator_inspect", viewer, **history) == initial
    call("evaluator_publish", publisher, "UNAUTHORIZED",
         **(publish | {"idempotency_key": "invalid-proof", "presentation": "missing"}))
    assert call("evaluator_inspect", viewer, **history) == initial
    published = call("evaluator_publish", publisher, **publish)
    assert published["result"]["revision"] == 1 and published["result"]["status"] == "active", published
    assert call("evaluator_recover", publisher, idempotency_key="evaluator-publish") == published
    assert call("evaluator_publish", publisher, **publish) == published
    call("evaluator_preview_publish", publisher, "REVISION_CONFLICT", expected_revision=0, entry=entry)

    revoke = authorize("revoke", revoke, "evaluator-revoke")
    call("evaluator_revoke", publisher, "UNAUTHORIZED", **(revoke | {
        "idempotency_key": "changed-intent", "presentation": publish["presentation"],
    }))
    revoked = call("evaluator_revoke", publisher, **revoke)
    assert revoked["result"]["revision"] == 2 and revoked["result"]["status"] == "revoked", revoked
    assert call("evaluator_recover", publisher, idempotency_key="evaluator-revoke") == revoked
    final = call("evaluator_inspect", viewer, **history)["result"]
    assert final["entry"]["status"] == "revoked" and final["history"] == [
        published["result"], revoked["result"],
    ], final
    print("Evaluator publication, revocation, immutable history and exact recovery passed.")


def waiver(lab, db, viewer, agent, service):
    ledger, policy = lab / "waiver.sqlite", lab / "waiver-policy.sqlite"
    requester = credential(lab, "fixture-waiver-requester")
    operator = credential(lab, "fixture-waiver-operator")
    human = credential(lab, "fixture-waiver-approver-step-up")
    command(WAIVER, "seed-policy", policy)
    command(WAIVER, "init-waiver", ledger, policy)
    call = invoke("v1-waiver", db, [ledger, policy])
    transition = {"transition_ref": "fixture-transition"}
    request = transition | {
        "workflow_id": "workflow", "from_state": "verifying", "to_state": "accepted",
        "expected_revision": 0, "criterion_id": "waiver-review", "reason": "Fixture exception",
    }
    call("waiver_request", agent, "UNAUTHORIZED", **request)
    call("waiver_request", service, "NOT_FOUND", **request)
    call("waiver_request", viewer, "NOT_FOUND", **request)
    requested = call("waiver_request", requester, **request)["result"]
    waiver_id = requested["waiver_id"]
    assert call("waiver_inspect", viewer, waiver_id=waiver_id)["result"]["historical_receipt"] is None

    context = transition | {"waiver_id": waiver_id}
    begun = call(
        "waiver_begin", operator, **context, approver_issuer="fixture-waiver-human",
        approver_subject="fixture-waiver-approver", idempotency_key="waiver-ceremony",
    )
    assert begun["result"]["context"] == requested["context"], begun
    assertion = command(WAIVER, "assert-waiver", ledger, human, body=begun)
    finish = context | {
        "challenge_id": begun["result"]["challenge_id"],
        "idempotency_key": "waiver-ceremony",
        "dispatch_key": "waiver-record", "presentation": assertion["presentation"],
    }
    recorded = call("waiver_finish", operator, **finish)
    assert recorded["result"]["disposition"] == "historical_receipt", recorded
    assert recorded["result"]["result"] == "waived", recorded
    assert call("waiver_recover", operator, dispatch_key="waiver-record") == recorded
    assert call("waiver_finish", operator, **finish) == recorded
    call("waiver_recover", requester, "NOT_FOUND", dispatch_key="waiver-record")
    assert call("waiver_inspect", viewer, waiver_id=waiver_id)["result"]["historical_receipt"] == recorded["result"]
    assert call("waiver_validate", viewer, **context)["result"]["currently_valid"] is True
    print("Waiver request, human approval, historical receipt, current validation and exact recovery passed.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="writ-m6-lifecycle-") as directory:
        lab = Path(directory)
        db = lab / "management.sqlite"
        command(MANAGE, "init", db)
        viewer, agent, service = (credential(lab, name) for name in (
            "fixture-viewer", "fixture-agent", "fixture-service",
        ))
        evaluator(lab, db, viewer, agent, service)
        waiver(lab, db, viewer, agent, service)
