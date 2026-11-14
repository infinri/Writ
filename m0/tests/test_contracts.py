"""Contract registry, fixture validation and canonical digest tests (M0-02a, M0-02b)."""
from __future__ import annotations

import json

import pytest
from hypothesis import given, strategies as st

from writ_m0 import canonical, contracts

FIXTURES = contracts.FIXTURES_DIR
EXPECTED_SCHEMAS = {
    "action-request", "decision", "effect-receipt", "audit-event", "policy-manifest",
    "model-capabilities", "model-request", "model-event", "capability-pack", "task-spec",
    "artifact", "evidence", "evaluation-record",
    "principal-proof", "approval-grant", "broker-permit", "policy-bundle",
}
TASKS = ["code-review", "research", "document-review"]
CODE_ONLY_FIELDS = {"repository", "repo", "diff", "pull_request", "pr", "file_path", "commit", "branch"}


class TestSchemas:
    def test_expected_schema_set(self):
        assert set(contracts.schema_names()) == EXPECTED_SCHEMAS

    @pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMAS))
    def test_schema_is_valid_draft_2020_12_and_closed(self, name):
        schema = contracts.load_schema(name)
        contracts._validator(name)  # raises on an invalid schema
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema.get("additionalProperties") is False

    @pytest.mark.parametrize("value", ["2026-11-05", "yesterday", "2026-11-05T12:00:00", "2026-13-01T00:00:00Z"])
    def test_date_time_format_is_enforced(self, value):
        doc = contracts.load_fixture("domains/research/evidence.json")[0] | {"observed_at": value}
        assert contracts.violations("evidence", doc)

    def test_valid_date_time_with_offset_accepted(self):
        doc = contracts.load_fixture("domains/research/evidence.json")[0] | {"observed_at": "2026-11-05T12:00:00.5-06:00"}
        assert contracts.violations("evidence", doc) == []


class TestCrossDomainFixtures:
    @pytest.mark.parametrize("task", TASKS)
    def test_task_artifact_and_evidence_validate_against_shared_schemas(self, task):
        contracts.validate("task-spec", contracts.load_fixture(f"{task}.task.json"))
        contracts.validate("artifact", contracts.load_fixture(f"domains/{task}/artifact.json"))
        for item in contracts.load_fixture(f"domains/{task}/evidence.json"):
            contracts.validate("evidence", item)

    @pytest.mark.parametrize("schema", ["task-spec", "artifact", "evidence", "evaluation-record"])
    def test_core_contracts_require_no_code_specific_field(self, schema):
        required = set(contracts.load_schema(schema)["required"])
        assert not required & CODE_ONLY_FIELDS

    @pytest.mark.parametrize("task", ["research", "document-review"])
    def test_noncode_tasks_carry_no_repository_input(self, task):
        inputs = contracts.load_fixture(f"{task}.task.json")["inputs"]
        assert not set(inputs) & (CODE_ONLY_FIELDS | {"diff_ref", "repo_path"})

    def test_negative_fixtures_rejected(self):
        assert contracts.violations("capability-pack", contracts.load_fixture("negative/pack-escalation.invalid.json"))
        assert contracts.violations("task-spec", contracts.load_fixture("negative/task-code-lock.invalid.json"))

    def test_model_capability_fixtures_valid(self):
        for name in ("model-stub-limited", "model-stub-streaming"):
            contracts.validate("model-capabilities", contracts.load_fixture(f"{name}.capabilities.json"))

    def test_contract_error_lists_every_violation(self):
        with pytest.raises(contracts.ContractError) as info:
            contracts.validate("task-spec", {"contract_version": "2.0"})
        assert len(info.value.errors) >= 5


json_scalars = st.none() | st.booleans() | st.integers(-canonical.MAX_SAFE_INTEGER, canonical.MAX_SAFE_INTEGER) | st.text()
json_values = st.recursive(json_scalars, lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(), c, max_size=4), max_leaves=20)


class TestCanonical:
    @given(st.dictionaries(st.text(), json_values, max_size=6))
    def test_digest_ignores_key_order(self, value):
        reordered = dict(reversed(list(value.items())))
        assert canonical.digest(value) == canonical.digest(reordered)

    @given(json_values)
    def test_canonical_form_round_trips(self, value):
        assert json.loads(canonical.canonical_bytes(value)) == value

    @given(json_values)
    def test_whitespace_in_source_json_does_not_change_digest(self, value):
        pretty = json.loads(json.dumps(value, indent=3))
        assert canonical.digest(pretty) == canonical.digest(value)

    def test_known_vector(self):
        # Fixed vector shared with M1 implementations.
        value = {"b": [1, True, None], "a": "é\n", "c": {"z": 0, "y": "x"}}
        assert canonical.canonical_bytes(value) == '{"a":"é\\n","b":[1,true,null],"c":{"y":"x","z":0}}'.encode()
        assert canonical.digest(value) == "sha256:" + __import__("hashlib").sha256(canonical.canonical_bytes(value)).hexdigest()

    @pytest.mark.parametrize("bad", [1.5, float("nan"), 2**53, {1: "x"}, "\ud800", b"bytes", {"a": {1, 2}}])
    def test_rejects_non_canonical_values(self, bad):
        with pytest.raises(canonical.CanonicalizationError):
            canonical.digest(bad)

    def test_rejects_excessive_nesting(self):
        deep: list = []
        for _ in range(canonical.MAX_DEPTH + 2):
            deep = [deep]
        with pytest.raises(canonical.CanonicalizationError):
            canonical.digest(deep)

    def test_unicode_is_not_normalized(self):
        assert canonical.digest("é") != canonical.digest("é")
