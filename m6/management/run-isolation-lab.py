"""Exercise the common authorization boundary with real disposable owners attached."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import secrets
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug"))
MANAGE = BIN / "writ-manage-fixture"
SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}
SECRET = "private-presentation-canary"
DIGEST = "sha256:" + "a" * 64


def command(binary, args, data=None):
    process = subprocess.run([binary, *args], input=json.dumps(data), capture_output=True,
                             text=True, timeout=10)
    assert not process.stderr and SECRET not in process.stdout
    value = json.loads(process.stdout)
    if "ok" in value:
        assert value["ok"] == (process.returncode == 0)
    else:
        assert process.returncode == 0
    return value


def requests():
    page = {"after": 0, "limit": 50}
    draft = {"id": "existing", "revision": 1}
    key = {"idempotency_key": "attempt"}
    presentation = {"intent_digest": DIGEST, "presentation": SECRET}
    proof = key | presentation
    candidate = json.loads((HERE.parent / "compiler/fixtures/permit.input.json").read_text())
    pair = command(BIN / "writ-publication-authority-fixture", ["sign-publication", "1"], candidate)
    mutate = {"id": "existing", "expected_revision": 0, "preview": False} | key
    corpus = [{"kind": "synthetic.inspect", "resource": "synthetic:m6-lab/offline-proof/inventory"}]
    policy = {
        "create": mutate | {"candidate": candidate},
        "edit": mutate | {"expected_revision": 1, "candidate": candidate},
        "clone": mutate | {"id": "copy", "source": draft},
        "get": {"draft": draft}, "history": {"id": "existing"} | page,
        "validate": {"draft": draft}, "simulate": {"draft": draft, "corpus": corpus},
        "compare": {"before": draft, "after": draft, "corpus": corpus},
        "publication_history": page, "policy.lifecycle.inspect": page,
    }
    for operation, extra in [
        ("publish", {"draft": draft, "publication": pair}), ("rollback", {}),
        ("retire", {"confirmation": "retire 1"}),
    ]:
        base = {"expected_revision": 0, "publication_version": 1, "acknowledge_self": False} | key | extra
        policy["preview_" + operation] = base
        policy[operation] = base | presentation
    for operation in ("preview_deactivate", "deactivate"):
        policy[operation] = {"expected_revision": 0, "publication_version": 1,
                             "policy_id": "m6.synthetic.permit", "confirmation": "deactivate m6.synthetic.permit"}

    optional = json.loads((HERE.parent / "compiler/fixtures/optional-inspect.input.json").read_text())
    original = command(BIN / "writ-publication-authority-fixture", ["sign-publication", "1"], optional)
    replacement = command(BIN / "writ-publication-authority-fixture", ["sign-removal", "2"], original)
    for operation in ("preview_deactivate", "deactivate"):
        policy[operation + "/replacement"] = policy[operation] | key | {
            "draft": draft, "publication_version": 2, "publication": replacement,
            "acknowledge_self": False, "policy_id": "m6.synthetic.optional-inspect",
        } | (presentation if operation == "deactivate" else {})

    entry = json.loads((ROOT / "docs/contracts/proposed/examples/evaluator-registry-entry.valid.json").read_text())
    evaluator = {"evaluator_inspect": {"evaluator_id": "sample"} | page, "evaluator_recover": key}
    for operation, target in [("publish", {"entry": entry}), ("revoke", {"evaluator_id": "sample"})]:
        base = target | {"expected_revision": 0}
        evaluator["evaluator_preview_" + operation] = base
        evaluator["evaluator_" + operation] = base | proof
    context = {"waiver_id": DIGEST, "transition_ref": "fixture-transition"}
    waiver = {
        "waiver_request": {"transition_ref": "fixture-transition", "workflow_id": "workflow",
                          "from_state": "verifying", "to_state": "accepted", "expected_revision": 0,
                          "criterion_id": "waiver-review", "reason": SECRET},
        "waiver_begin": context | key | {"approver_issuer": "fixture-human", "approver_subject": "other"},
        "waiver_finish": context | key | {"challenge_id": "challenge", "dispatch_key": "record", "presentation": SECRET},
        "waiver_inspect": {"waiver_id": DIGEST}, "waiver_validate": context,
        "waiver_recover": {"dispatch_key": "record"},
    }
    target = {"contract_id": "tenant:m6-lab:sample", "contract_version": "1.0.0"}
    change = target | {"expected_revision": 0}
    contracts = {"contracts.browse": {}, "contracts.inspect": {"contract_id": "action-request"},
                 "contracts.validate-schema": {"schema": {}}, "contracts.validate-example": target | {"example": 4},
                 "contracts.create": change | key | {"schema": {}}, "contracts.preview": change | {"change": "publish"},
                 "contracts.publish": change | proof, "contracts.recover": key,
                 "contracts.retire": change | proof | {"confirmation": "retire"}}
    contracts["contracts.deprecate"] = contracts["contracts.retire"] | {"replacement_version": "2.0.0", "end_date": "2026-11-19"}
    target = {"pack_id": "sample", "pack_version": "1.0.0"}
    change = target | {"expected_revision": 0}
    packs = {"packs.history": {"pack_id": "sample"}, "packs.inspect": target, "packs.verify": target,
             "packs.preview": change | {"pack_operation": "enable"}, "packs.recover": key,
             "packs.enable": change | proof, "packs.install": change | proof | {"envelope": {}}}
    for name in ("disable", "upgrade"):
        packs["packs." + name] = change | proof | {"confirmation": name}
    return {"v1": policy, "v1-evaluator": evaluator, "v1-waiver": waiver,
            "v1-contracts": contracts, "v1-packs": packs}


def epoch(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return int(db.execute("SELECT value FROM meta WHERE key='owner_epoch'").fetchone()[0])


def snapshot(databases, waiver):
    results = []
    for path in databases:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.iterdump()
            if path == waiver:
                # Each authorized fixture owner reopen fences the prior epoch. Check its count separately.
                rows = (row for row in rows if not row.startswith("""INSERT INTO "meta" VALUES('owner_epoch',"""))
            results.append(hashlib.sha256("\n".join(rows).encode()).digest())
    return results


def run(lab, authenticated=False):
    credentials = {}
    if authenticated:
        catalog = lab / "identity.sqlite"
        with sqlite3.connect(catalog) as db:
            db.executescript((HERE / "src/identity.sql").read_text())
            for who, realm, roles in [
                    ("viewer", "management", ["viewer"]), ("author", "management", ["policy-author"]),
                    ("publisher", "management", ["publisher"]),
                    ("author-publisher", "management", ["policy-author", "publisher"]),
                    ("service", "management-service", ["publisher"]),
                    ("agent", "agent", ["publisher"]), ("adapter", "adapter", ["publisher"])]:
                subject = "fixture-" + who
                credentials[who] = secrets.token_hex(32)
                digest = "sha256:" + hashlib.sha256(credentials[who].encode()).hexdigest()
                db.execute("INSERT INTO principals VALUES(?,?,1)", (subject, realm))
                db.execute("INSERT INTO credentials VALUES(?,?,unixepoch()-1,unixepoch()+1800,0)", (digest, subject))
                db.executemany("INSERT INTO scoped_grants VALUES(?,?,?,?)",
                               [(subject, SCOPE["tenant"], SCOPE["project"], role) for role in roles])
        os.environ["WRIT_MANAGEMENT_IDENTITY_DB"] = str(catalog)
    replica = lab / "audit.sqlite"
    with sqlite3.connect(replica) as db:
        db.executescript((HERE / "src/audit_replica.sql").read_text())
        db.execute("INSERT INTO audit_events VALUES(?,?,1,'evt-1','decision','2026-11-05T12:00:00Z','trace-1','req-1',null,null,'deny',null,?)",
                   (SCOPE["tenant"], SCOPE["project"], json.dumps({"reason_code":"POLICY_DENIED"})))
    os.environ["WRIT_MANAGEMENT_AUDIT_DB"] = str(replica)
    os.environ["WRIT_MANAGEMENT_TRANSITIONS"] = str(HERE / "fixtures/recorded-transitions.json")
    management, publication, contracts, evaluator, waiver, waiver_policy = [
        lab / (name + ".sqlite") for name in ("management", "publication", "contracts", "evaluator", "waiver", "policy")]
    for args in [("init", management), ("init-publication", publication), ("init-evaluator", evaluator),
                 ("init-packs", contracts, evaluator)]:
        command(MANAGE, args)
    authority = BIN / "writ-waiver-authority-fixture"
    command(authority, ["seed-policy", waiver_policy])
    command(authority, ["init-waiver", waiver, waiver_policy])
    owners = {"v1": [publication], "v1-evaluator": [evaluator], "v1-waiver": [waiver, waiver_policy],
              "v1-contracts": [contracts], "v1-packs": [contracts, evaluator]}
    cases = requests()
    cases["v1"].update({"audit.search": {}, "audit.timeline": {"trace_id":"trace-1"},
                        "audit.explain":{"event_id":"evt-1"},
                        "compare_active":{"draft":{"id":"existing","revision":1}, "corpus":cases["v1"]["simulate"]["corpus"]},
                        "compare_recorded":{"draft":{"id":"existing","revision":1}, "record_ids":["sample"]}})
    if authenticated:
        cases["v1"]["identity.credential"] = {}

    def call(mode, who, operation, fields, scope=SCOPE, offline=False):
        credential = lab / who
        credential.write_text(credentials.get(who, "fixture-" + who))
        links = [lab / "missing-owner"] * len(owners[mode]) if offline else owners[mode]
        store = lab / "missing-management" if offline else management
        return command(MANAGE, [*(["authenticated"] if authenticated else []), mode, store, credential, *links],
                       {"version": "/manage/v1", "scope": scope, "operation": operation} | fields)

    assert call("v1", "author", "create", cases["v1"]["create"])["ok"]
    absent = call("v1", "viewer", "get", {"draft": {"id": "absent", "revision": 1}})
    assert absent["error"]["code"] == "NOT_FOUND"
    unauthorized = call("v1", "unknown", "get", cases["v1"]["get"])
    assert unauthorized["error"]["code"] == "UNAUTHORIZED"
    receipt = call("v1", "author", "create", cases["v1"]["create"])
    assert receipt["result"]["audit_id"] == 1
    assert call("v1", "author-publisher", "create", cases["v1"]["create"])["error"]["code"] == "REVISION_CONFLICT"
    databases = [management, publication, contracts, evaluator, waiver, waiver_policy, waiver.with_suffix(".waiver-effects.sqlite")]
    proposal = cases["v1"]["preview_publish"]
    before = snapshot(databases, waiver)
    shown = call("v1", "publisher", "preview_publish", proposal)
    assert shown["ok"]
    step_up = lab / "step-up"
    step_up.write_bytes(b"fixture-publisher-step-up")
    authorized = command(BIN / "writ-publication-authority-fixture",
                         ["authorize-publication", step_up, "matrix-ceremony"], shown)
    assert snapshot(databases, waiver) == before
    expected_epoch = epoch(waiver)
    missing = {"waiver_inspect", "waiver_validate", "contracts.validate-example", "packs.inspect", "packs.verify"}
    reads = {"audit.search","audit.timeline","audit.explain","identity.credential","get", "history", "publication_history", "policy.lifecycle.inspect", "evaluator_inspect",
             "waiver_inspect", "waiver_validate", "contracts.browse", "contracts.inspect",
             "contracts.validate-schema", "contracts.validate-example", "packs.history", "packs.inspect", "packs.verify"}
    for mode, operations in cases.items():
        for variant, fields in operations.items():
            operation = variant.split("/")[0]
            for who in ("agent", "adapter"):
                assert call(mode, who, operation, fields) == unauthorized, (operation, who)
                assert call(mode, who, operation, fields, offline=True) == unauthorized, (operation, who)
            for dimension in ("tenant", "project"):
                scope = SCOPE | {dimension: "foreign"}
                assert call(mode, "author-publisher", operation, fields, scope) == absent, (operation, dimension)
                assert call(mode, "author-publisher", operation, fields, scope, offline=True) == absent
            if operation not in reads:
                assert call(mode, "viewer", operation, fields) == absent, operation
                assert call(mode, "service", operation, fields) == absent, operation
            else:
                response = call(mode, "viewer", operation, fields)
                if operation in missing:
                    assert response == absent, operation
                else:
                    assert response["ok"], operation
                if mode == "v1-waiver":
                    expected_epoch += 1
            oversized = call(mode, "viewer", operation, fields | {"overflow": "x" * 41000})
            assert oversized["error"]["code"] == "LIMIT_EXCEEDED", operation
            if "limit" in fields:
                for limit in (0, 51):
                    assert call(mode, "viewer", operation, fields | {"limit": limit})["error"]["code"] == "INVALID_REQUEST"
    preview = cases["v1"]["create"] | {"id": "preview-only", "idempotency_key": "preview-only", "preview": True}
    for _ in range(2):
        assert call("v1", "author", "create", preview)["result"]["audit_id"] is None
    assert call("v1", "publisher", "preview_publish", proposal) == shown
    assert epoch(waiver) == expected_epoch, "refusal opened the fenced waiver owner"
    assert snapshot(databases, waiver) == before, "read or refusal changed durable owner or audit state"
    commit = proposal | {"intent_digest": shown["result"]["intent_digest"],
                         "presentation": authorized["presentation"]}
    published = call("v1", "publisher", "publish", commit)
    assert published["ok"] and published["result"]["audit_id"] == 1
    assert call("v1", "publisher", "publish", commit) == published
    if authenticated:
        assert call("v1", "viewer", "identity.credential", {})["result"]["subject"] == "fixture-viewer"
        with sqlite3.connect(catalog) as db:
            db.execute("UPDATE credentials SET revoked=1 WHERE subject='fixture-viewer'")
        assert call("v1", "viewer", "audit.search", {}) == unauthorized
        os.environ["WRIT_MANAGEMENT_IDENTITY_DB"] = str(lab / "missing-auth")
        assert call("v1", "publisher", "publish", commit) == unauthorized
    print(sum(map(len, cases.values())), "operation forms passed across five disposable resource families")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="writ-isolation-") as directory:
        for profile in ("fixture", "authenticated"):
            lab = Path(directory) / profile
            lab.mkdir()
            run(lab, authenticated=profile == "authenticated")
