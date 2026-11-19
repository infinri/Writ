"""Exercise the disposable custom-contract registry through the shared management CLI."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACTS = ROOT / "docs/contracts"
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug")).resolve()
MANAGE = BIN / "writ-manage-fixture"
AUTHORITY = BIN / "writ-contract-authority-fixture"
SCOPE = {"tenant": "m6-lab", "project": "offline-proof"}
DIALECT = "https://json-schema.org/draft/2020-12/schema"
END_DATE = "2026-11-19"
CONTRACT = "tenant:m6-lab:report"
DEPENDENT = "tenant:m6-lab:dependent-contract"
PACK_CONTRACT = "pack:sample-pack:report"


def command(binary, *args, body=None, error=None):
    process = subprocess.run(
        [binary, *args],
        input="" if body is None else json.dumps(body),
        text=True,
        capture_output=True,
        check=False,
    )
    response = json.loads(process.stdout)
    if error is None:
        assert process.returncode == 0, (response, process.stderr)
    else:
        assert process.returncode != 0 and response["error"]["code"] == error, response
    return response


def credential(directory, value):
    path = directory / value
    path.write_text(value)
    return path


def wire_validators():
    catalog = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in (
            json.loads(path.read_text())
            for path in (
                CONTRACTS / "decision.schema.json",
                CONTRACTS / "policy-bundle.schema.json",
                CONTRACTS / "proposed/evaluator-registry-entry.schema.json",
                ROOT / "m6/waivers/waiver-context-v1.schema.json",
                *(ROOT / "m6/publication/contracts").glob("*.schema.json"),
            )
        )
    )
    return [
        Draft202012Validator(
            json.loads((CONTRACTS / f"management/draft-{direction}.schema.json").read_text()),
            registry=catalog,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        for direction in ("request", "response")
    ]


def invoke(database, registry):
    request_schema, response_schema = wire_validators()

    def call(operation, credential_path, error=None, scope=SCOPE, **fields):
        request = {"version": "/manage/v1", "scope": scope, "operation": f"contracts.{operation}", **fields}
        request_schema.validate(request)
        response = command(MANAGE, "v1-contracts", database, credential_path, registry,
                           error=error, body=request)
        response_schema.validate(response)
        return response

    return call


def schema(field_type="string"):
    return {
        "$schema": DIALECT,
        "title": "Report",
        "type": "object",
        "$defs": {"field": {"type": field_type}},
        "properties": {"report/id~source": {"$ref": "#/$defs/field"}},
        "required": ["report/id~source"],
        "additionalProperties": False,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="writ-contract-lab-") as path:
        directory = Path(path)
        database = directory / "management.sqlite"
        registry = directory / "contracts.sqlite"
        command(MANAGE, "init", database)
        command(MANAGE, "init-contracts", registry)
        viewer = credential(directory, "fixture-viewer")
        author = credential(directory, "fixture-author")
        publisher = credential(directory, "fixture-publisher")
        service = credential(directory, "fixture-service")
        agent = credential(directory, "fixture-agent")
        adapter = credential(directory, "fixture-adapter")
        step_up = credential(directory, "fixture-contract-publisher-step-up")
        call = invoke(database, registry)
        revision = 0

        def mutation_fields(contract_id, version):
            return {"contract_id": contract_id, "contract_version": version, "expected_revision": revision}

        def create(contract_id, version, key, document, error=None):
            nonlocal revision
            reply = call("create", author, error, **mutation_fields(contract_id, version),
                         idempotency_key=key, schema=document)
            if error is None:
                revision += 1
                assert (reply["result"]["revision"], reply["result"]["status"]) == (revision, "draft"), reply
            return reply

        def preview(operation, contract_id, version, error=None, **extra):
            before = registry.read_bytes(), database.read_bytes()
            reply = call("preview", publisher, error, change=operation,
                         **mutation_fields(contract_id, version), **extra)
            assert (registry.read_bytes(), database.read_bytes()) == before, reply
            return reply

        def authorize(reply, key, issued_at=None):
            args = [step_up, key]
            if issued_at is not None:
                args.append(issued_at)
            presentation = command(AUTHORITY, *args, body=reply)["presentation"]
            return {"idempotency_key": key, "intent_digest": reply["result"]["intent_digest"],
                    "presentation": presentation}

        def commit(operation, contract_id, version, key, **extra):
            nonlocal revision
            proposal = preview(operation, contract_id, version, **extra)
            proof = authorize(proposal, key)
            fields = mutation_fields(contract_id, version) | extra | proof
            if operation in ("deprecate", "retire"):
                fields["confirmation"] = f"{operation} {contract_id}@{version}"
            receipt = call(operation, publisher, **fields)
            revision += 1
            assert receipt["result"]["revision"] == revision, receipt
            assert call(operation, publisher, **fields) == receipt
            assert call("recover", publisher, idempotency_key=key) == receipt
            return receipt, fields

        catalog = call("browse", viewer)["result"]
        core = {"action-request", "decision", "approval-grant", "broker-permit",
                "effect-receipt", "principal-proof", "policy-bundle", "audit-event"}
        assert core <= {entry["contract_id"] for entry in catalog["contracts"]}, catalog
        core_state = call("inspect", viewer, contract_id="decision")["result"]
        assert core_state["versions"][0]["schema"]["properties"], core_state
        for credential_path in (agent, adapter):
            call("browse", credential_path, "UNAUTHORIZED")
        call("browse", viewer, "NOT_FOUND", scope=SCOPE | {"tenant": "other"})
        call("browse", viewer, "NOT_FOUND", scope=SCOPE | {"project": "other"})
        call("preview", service, "NOT_FOUND", change="publish", **mutation_fields(CONTRACT, "1.0.0"))
        call("create", viewer, "NOT_FOUND", **mutation_fields(CONTRACT, "1.0.0"),
             idempotency_key="viewer-create", schema=schema())
        create("decision", "1.0.0", "core-edit", schema(), "PROTECTED_CONTRACT")
        create("tenant:m6-lab:decision", "1.0.0", "core-shadow", schema(), "PROTECTED_CONTRACT")
        create("tenant:other:report", "1.0.0", "foreign-tenant", schema(), "NOT_FOUND")
        create("pack:other:report", "1.0.0", "foreign-pack", schema(), "NOT_FOUND")
        assert call("inspect", viewer, contract_id="decision")["result"] == core_state

        valid = call("validate-schema", viewer, schema=schema())["result"]
        assert valid["valid"] and valid["diagnostics"] == [], valid
        for document, code in [
            (schema() | {"patternProperties": {}}, "UNSUPPORTED_KEYWORD"),
            (schema() | {"$defs": {"field": {"$ref": "#/$defs/field"}}}, "REFERENCE_CYCLE"),
            (schema() | {"$defs": {"field": {"$ref": "https://example.test/schema"}}},
             "UNAPPROVED_REFERENCE"),
            (schema() | {"$defs": {"field": {"$ref": "file:///fixture.json"}}},
             "UNAPPROVED_REFERENCE"),
            (schema() | {"$defs": {f"field{i}": {"type": "string"} for i in range(33)}},
             "SCHEMA_LIMIT"),
        ]:
            invalid = call("validate-schema", viewer, schema=document)["result"]
            assert invalid["valid"] is False and invalid["diagnostics"][0]["code"] == code, invalid
        create(CONTRACT, "1.0.0", "unsupported", schema() | {"allOf": []}, "INVALID_CONTRACT")
        created = create(CONTRACT, "1.0.0", "draft", schema())
        assert call("recover", author, idempotency_key="draft") == created
        assert call("recover", publisher, "NOT_FOUND", idempotency_key="draft")["ok"] is False

        example_fields = {"contract_id": CONTRACT, "contract_version": "1.0.0"}
        valid = call("validate-example", viewer, **example_fields, example={"report/id~source": "lab"})
        assert valid["result"] == {"valid": True, "diagnostics": []}, valid
        invalid = call("validate-example", viewer, **example_fields, example={"report/id~source": 12})
        assert invalid["result"] == {
            "valid": False, "diagnostics": [{"code": "EXAMPLE_TYPE", "pointer": "/report~1id~0source"}]
        }, invalid
        print("Core browse, tenant and pack boundaries, schema limits and example diagnostics passed.")

        first = preview("publish", CONTRACT, "1.0.0")
        expired = authorize(first, "expired", issued_at="2026-11-19T11:59:00Z")
        call("publish", publisher, "UNAUTHORIZED", **mutation_fields(CONTRACT, "1.0.0"), **expired)
        initial, first_fields = commit("publish", CONTRACT, "1.0.0", "publish-v1")
        assert initial["result"]["compatibility"]["status"] == "initial", initial
        call("publish", publisher, "IDEMPOTENCY_CONFLICT",
             **(first_fields | {"expected_revision": revision}))
        call("preview", publisher, "REVISION_CONFLICT", change="publish",
             **(mutation_fields(CONTRACT, "1.0.0") | {"expected_revision": 0}))
        create(CONTRACT, "1.0.0", "overwrite", schema("integer"), "REVISION_CONFLICT")
        create(CONTRACT, "1.1.0", "minor-draft", schema("integer"))
        preview("publish", CONTRACT, "1.1.0", "INCOMPATIBLE_CONTRACT")
        create(CONTRACT, "2.0.0", "major-draft", schema("integer"))
        major, _ = commit("publish", CONTRACT, "2.0.0", "publish-v2")
        assert major["result"]["compatibility"]["status"] == "breaking", major

        replacement = {"replacement_version": "2.0.0", "end_date": END_DATE}
        deprecation = preview("deprecate", CONTRACT, "1.0.0", **replacement)
        wrong_purpose = {
            "idempotency_key": "wrong-operation",
            "intent_digest": deprecation["result"]["intent_digest"],
            "presentation": first_fields["presentation"],
        }
        call("deprecate", publisher, "UNAUTHORIZED", **mutation_fields(CONTRACT, "1.0.0"),
             **replacement, **wrong_purpose, confirmation=f"deprecate {CONTRACT}@1.0.0")
        call("deprecate", publisher, "INVALID_REQUEST", **mutation_fields(CONTRACT, "1.0.0"),
             **replacement, **authorize(deprecation, "wrong-confirmation"),
             confirmation=f"deprecate {CONTRACT}@2.0.0")
        deprecated, _ = commit("deprecate", CONTRACT, "1.0.0", "deprecate-v1", **replacement)
        assert deprecated["result"]["status"] == "deprecated", deprecated
        retired, _ = commit("retire", CONTRACT, "1.0.0", "retire-v1")
        assert retired["result"]["status"] == "retired", retired
        reopened = call("inspect", viewer, contract_id=CONTRACT)["result"]
        old = next(version for version in reopened["versions"] if version["version"] == "1.0.0")
        assert old["status"] == "retired" and old["schema"] == schema(), old
        assert (old["replacement_version"], old["end_date"]) == ("2.0.0", END_DATE), old
        assert reopened["history"][-1] == retired["result"], reopened
        print("Immutable publication, exact authorization, retirement and recovered receipts passed.")

        create(DEPENDENT, "1.0.0", "dependent-v1-draft", schema())
        commit("publish", DEPENDENT, "1.0.0", "dependent-v1-publish")
        create(DEPENDENT, "2.0.0", "dependent-v2-draft", schema())
        commit("publish", DEPENDENT, "2.0.0", "dependent-v2-publish")
        commit("deprecate", DEPENDENT, "1.0.0", "dependent-deprecation", **replacement)
        preview("retire", DEPENDENT, "1.0.0", "ACTIVE_DEPENDENTS")
        call("retire", publisher, "ACTIVE_DEPENDENTS", **mutation_fields(DEPENDENT, "1.0.0"),
             confirmation=f"retire {DEPENDENT}@1.0.0", idempotency_key="retire-with-dependents",
             intent_digest=retired["result"]["intent_digest"], presentation=first_fields["presentation"])
        held = call("inspect", viewer, contract_id=DEPENDENT)["result"]
        old = next(version for version in held["versions"] if version["version"] == "1.0.0")
        assert old["status"] == "deprecated" and {
            dependency["kind"] for dependency in old["dependencies"]
        } == {"pack", "policy", "integration"}, old
        create(PACK_CONTRACT, "1.0.0", "pack-draft", schema())
        assert any(
            entry["contract_id"] == PACK_CONTRACT
            for entry in call("browse", viewer)["result"]["contracts"]
        )
        print("Active pack, policy and integration dependency refusal and scoped pack draft passed.")


if __name__ == "__main__":
    main()
