"""Run the headless D-055 workflow in two disposable SQLite stores."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug")).resolve()
MANAGE = BIN / "writ-manage-fixture"
AUTHORITY = BIN / "writ-publication-authority-fixture"
SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}


def command(binary, *args, body=None):
    result = subprocess.run(
        [binary, *args], input="" if body is None else json.dumps(body),
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout or result.stderr)
    return json.loads(result.stdout)


def run(lab):
    drafts, registry = lab / "drafts.sqlite", lab / "registry.sqlite"
    author, publisher, step_up = (lab / name for name in ("author", "publisher", "step-up"))
    author.write_text("fixture-author")
    publisher.write_text("fixture-publisher")
    step_up.write_text("fixture-publisher-step-up")
    command(MANAGE, "init", drafts)
    command(MANAGE, "init-publication", registry)

    def manage(operation, credential, **fields):
        return command(MANAGE, "v1", drafts, credential, registry, body={
            "version": "/manage/v1", "operation": operation, "scope": SCOPE, **fields,
        })

    def propose_and_commit(operation, key, **fields):
        fields |= {"idempotency_key": key, "acknowledge_self": False}
        preview = manage(f"preview_{operation}", publisher, **fields)
        authorization = command(AUTHORITY, "authorize-publication", step_up, key, body=preview)
        return manage(
            operation, publisher, **fields, intent_digest=preview["result"]["intent_digest"],
            presentation=authorization["presentation"],
        )["result"]

    for version, name in enumerate(("permit", "deny"), start=1):
        candidate = json.loads((HERE.parent / f"compiler/fixtures/{name}.input.json").read_text())
        created = manage(
            "create", author, id=name, idempotency_key=f"draft-{name}",
            expected_revision=0, preview=False, candidate=candidate,
        )["result"]
        draft = {"id": name, "revision": created["revision"]}
        validated = manage("validate", author, draft=draft)["result"]
        assert validated["compilable"] and validated["offline_proven"], validated
        publication = command(AUTHORITY, "sign-publication", str(version), body=candidate)
        committed = propose_and_commit(
            "publish", f"publish-{name}", expected_revision=version - 1,
            publication_version=version, draft=draft, publication=publication,
        )
        assert committed["revision"] == version, committed

    before = manage("publication_history", publisher, after=0, limit=50)["result"]
    assert [event["operation"] for event in before["events"]] == ["publish", "publish"], before
    propose_and_commit("rollback", "rollback-permit", expected_revision=2, publication_version=1)
    reopened = manage("publication_history", publisher, after=0, limit=50)["result"]
    assert reopened["selected"] == {
        "revision": 3, "publication_version": 1, "audit_id": 3, "verified": True,
    }, reopened
    assert [event["publication_version"] for event in reopened["events"]] == [1, 2, 1], reopened
    assert reopened["events"][:2] == before["events"], reopened
    print(json.dumps(reopened, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="writ-publication-lab-") as directory:
        run(Path(directory))
