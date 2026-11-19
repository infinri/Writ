"""Select a signed replacement and prove retirement survives reopening both stores."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
TARGET = Path(os.environ.get("CARGO_TARGET_DIR", HERE / "target"))
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", TARGET / "debug")).resolve()
MANAGE = BIN / "writ-manage-fixture"
AUTHORITY = BIN / "writ-publication-authority-fixture"
SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}


def command(binary, *args, body=None, refusal=None):
    completed = subprocess.run(
        [binary, *args], input="" if body is None else json.dumps(body),
        text=True, capture_output=True, check=False,
    )
    if refusal is not None:
        assert completed.returncode == 1, completed
        response = json.loads(completed.stdout)
        assert response["error"]["code"] == refusal, response
        return response
    if completed.returncode:
        raise RuntimeError(completed.stdout or completed.stderr)
    return json.loads(completed.stdout)


def publications(registry):
    with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT * FROM publications ORDER BY version").fetchall()


def run(lab):
    drafts, registry = lab / "drafts.sqlite", lab / "registry.sqlite"
    author, publisher, step_up = (lab / name for name in ("author", "publisher", "step-up"))
    author.write_text("fixture-author")
    publisher.write_text("fixture-publisher")
    step_up.write_text("fixture-publisher-step-up")
    command(MANAGE, "init", drafts)
    command(MANAGE, "init-publication", registry)

    def manage(request, credential=publisher, refusal=None):
        return command(
            MANAGE, "v1", drafts, credential, registry,
            body={"version": "/manage/v1", "scope": SCOPE, **request}, refusal=refusal,
        )

    def authorized(operation, key, **fields):
        fields.update(idempotency_key=key, acknowledge_self=False)
        preview = manage({"operation": f"preview_{operation}", **fields})
        authorization = command(AUTHORITY, "authorize-publication", step_up, key, body=preview)
        return {
            "operation": operation, **fields, "intent_digest": preview["result"]["intent_digest"],
            "presentation": authorization["presentation"],
        }, preview["result"]

    permit_input = None
    for version, name in enumerate(("permit", "deny"), start=1):
        candidate = json.loads((HERE.parent / f"compiler/fixtures/{name}.input.json").read_text())
        if version == 1:
            permit_input = candidate
        created = manage({
            "operation": "create", "id": name, "idempotency_key": f"draft-{name}",
            "expected_revision": 0, "preview": False, "candidate": candidate,
        }, author)["result"]
        publication = command(AUTHORITY, "sign-publication", str(version), body=candidate)
        publish, _ = authorized(
            "publish", f"publish-{name}", expected_revision=version - 1, publication_version=version,
            draft={"id": name, "revision": created["revision"]}, publication=publication,
        )
        assert manage(publish)["result"]["revision"] == version

    history = {"operation": "publication_history", "after": 0, "limit": 50}
    before = manage(history)["result"]
    original_bytes = publications(registry)
    assert [event["operation"] for event in before["events"]] == ["publish", "publish"], before
    manage({
        "operation": "preview_retire", "expected_revision": 2, "publication_version": 2,
        "idempotency_key": "active", "acknowledge_self": False, "confirmation": "retire 2",
    }, refusal="REVISION_CONFLICT")
    for operation in ("preview_deactivate", "deactivate"):
        manage({
            "operation": operation, "expected_revision": 2, "publication_version": 2,
            "policy_id": "review", "confirmation": "deactivate review",
        }, refusal="INVALID_POLICY")
    rollback, _ = authorized("rollback", "rollback-before-retirement", expected_revision=2, publication_version=1)
    retire, preview = authorized(
        "retire", "retire-permit", expected_revision=2, publication_version=1, confirmation="retire 1",
    )
    assert preview["revision"] == 2 and preview["context"]["confirmation"] == "retire 1"
    assert manage(history)["result"] == before
    assert publications(registry) == original_bytes

    first_receipt = manage(retire)["result"]
    recovered_receipt = manage(retire)["result"]
    assert first_receipt == recovered_receipt == {
        "revision": 2, "publication_version": 1, "audit_id": 3, "intent_digest": retire["intent_digest"],
    }
    reopened = manage(history)["result"]
    assert reopened["selected"] == before["selected"]
    assert reopened["events"][:2] == before["events"]
    event = reopened["events"][2]
    assert event["operation"] == "retire" and event["revision"] == 2
    assert event["intent_digest"] == retire["intent_digest"]
    assert event["context"] == preview["context"]
    manage(rollback, refusal="NOT_FOUND")
    rewrapped = command(AUTHORITY, "sign-publication", "3", body=permit_input)
    manage({
        "operation": "preview_publish", "expected_revision": 2, "publication_version": 3,
        "idempotency_key": "reactivate-retired", "acknowledge_self": False,
        "draft": {"id": "permit", "revision": 1}, "publication": rewrapped,
    }, refusal="REVISION_CONFLICT")
    assert manage(history)["result"] == reopened
    assert publications(registry) == original_bytes
    print(json.dumps(reopened, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="writ-retirement-lab-") as directory:
        run(Path(directory))
