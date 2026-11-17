"""Structural and fixture checks for the proposed M5b HostAdapter contracts."""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
SCHEMAS = {
    name: json.loads((HERE / f"{name}.schema.json").read_text())
    for name in ("host-event", "host-coverage", "host-outcome")
}
FIXTURES = {
    name: json.loads((HERE / "examples" / f"{name}.valid.json").read_text())
    for name in SCHEMAS
}
VALIDATORS = {name: Draft202012Validator(schema) for name, schema in SCHEMAS.items()}
PORTABILITY_PATH = HERE.parents[2] / "m5" / "host-contracts" / "portability.json"
PORTABILITY_CASES = json.loads(PORTABILITY_PATH.read_text())


def claude_write_observation(payload):
    event = {"PreToolUse": "before_tool", "PermissionRequest": "permission_prompt"}.get(
        payload["hook_event_name"]
    )
    if event is None or payload["tool_name"] != "Write":
        raise ValueError("unregistered fixture event or tool")
    absolute_path = payload["tool_input"]["file_path"]
    prefix = "/fixture/tree/"
    if not isinstance(absolute_path, str) or not absolute_path.startswith(prefix):
        raise ValueError("path outside the configured fixture root")
    path = absolute_path[len(prefix):]
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("path segment refused")
    if any(ord(character) < 32 or 127 <= ord(character) < 160 or character == "\\" for character in path):
        raise ValueError("path character refused")
    return {"event": event, "intent": "file-write", "relative_path": path}


def cli_write_observation(payload):
    match (payload["phase"], payload["operation"]):
        case ("before", "file-write"):
            event = "before_tool"
        case ("prompt", "file-write"):
            event = "permission_prompt"
        case _:
            raise ValueError("unregistered fixture event or tool")

    relative_path = payload["target"]
    if not isinstance(relative_path, str) or {"", ".", ".."}.intersection(relative_path.split("/")):
        raise ValueError("path segment refused")
    control_points = (*range(32), *range(127, 160))
    if "\\" in relative_path or any(ord(char) in control_points for char in relative_path):
        raise ValueError("path character refused")
    return {"event": event, "intent": "file-write", "relative_path": relative_path}


class HostAdapterContracts(unittest.TestCase):
    def test_drafts_and_examples(self):
        for name, schema in SCHEMAS.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
                VALIDATORS[name].validate(FIXTURES[name])

    def test_event_cannot_claim_authority_or_select_effect_path(self):
        for overrides in (
            {"op": "effect.request"}, {"mode": "governed"}, {"source": "broker"},
            {"session_id": "trusted"}, {"os_principal": "uid:1"}, {"agent_id": "owner"},
            {"principal": {"id": "owner"}}, {"tenant_id": "another-tenant"},
            {"project_id": "another-project"}, {"delegation_chain": []},
            {"host_asserted": {"grant": "another-grant"}}, {"grant": "another-grant"},
            {"action": {"kind": "workflow.transition"}}, {"approval": True},
        ):
            with self.subTest(overrides=overrides):
                self.assertFalse(VALIDATORS["host-event"].is_valid(FIXTURES["host-event"] | overrides))

    def test_tool_and_lifecycle_event_shapes_differ(self):
        event = FIXTURES["host-event"]
        for member in ("tool", "input"):
            with self.subTest(member=member):
                absent = dict(event)
                del absent[member]
                self.assertFalse(VALIDATORS["host-event"].is_valid(absent))
        self.assertFalse(VALIDATORS["host-event"].is_valid(event | {"event": "session_end"}))
        lifecycle = {key: value for key, value in event.items() if key not in ("tool", "input")}
        VALIDATORS["host-event"].validate(lifecycle | {"event": "session_end"})

    def test_diagnostic_shapes_and_lengths(self):
        event = FIXTURES["host-event"]
        for overrides in (
            {"host_asserted": []}, {"host_asserted": {"agent_id": 7}},
            {"host_asserted": {"agent_id": "x" * 257}}, {"event": "unexpected"},
            {"event_id": "x" * 129}, {"tool": {"name": "Write", "variant": []}},
            {"input": []}, {"input": {str(key): 1 for key in range(33)}},
        ):
            with self.subTest(overrides=tuple(overrides)):
                self.assertFalse(VALIDATORS["host-event"].is_valid(event | overrides))

    def test_coverage_for_no_interceptor_cannot_claim_observation_or_prevention(self):
        for field in ("can_observe", "can_prevent"):
            coverage = copy.deepcopy(FIXTURES["host-coverage"])
            coverage["rows"][1][field] = True
            with self.subTest(field=field):
                self.assertFalse(VALIDATORS["host-coverage"].is_valid(coverage))

    def test_after_tool_cannot_claim_prevention_of_completed_effect(self):
        coverage = copy.deepcopy(FIXTURES["host-coverage"])
        coverage["rows"][0].update(event="after_tool", interceptor="hook", can_prevent=True)
        self.assertFalse(VALIDATORS["host-coverage"].is_valid(coverage))
        coverage["rows"][0]["can_prevent"] = False
        VALIDATORS["host-coverage"].validate(coverage)

    def test_broker_status_mapping_never_authorizes_host_dispatch(self):
        contract = {"contract_version": "writ-host-outcome/1", "source": "broker", "reason": "policy"}
        dispositions = {
            "allow": "continue_in_broker",
            "deny": "blocked",
            "require_approval": "needs_writ_approval",
            "unavailable": "blocked",
            "invalid": "blocked",
        }
        for status, disposition in dispositions.items():
            advisory = contract | dict(mode="advisory", label="advisory", status=status, disposition="observation_only")
            governed = contract | dict(mode="governed", label="governed", status=status, disposition=disposition)
            with self.subTest(status=status):
                VALIDATORS["host-outcome"].validate(advisory)
                VALIDATORS["host-outcome"].validate(governed)
                self.assertFalse(VALIDATORS["host-outcome"].is_valid(advisory | {"disposition": disposition}))
                self.assertFalse(VALIDATORS["host-outcome"].is_valid(governed | {"disposition": "observation_only"}))
                self.assertFalse(VALIDATORS["host-outcome"].is_valid(governed | {"status": None}))

    def test_adapter_misses_and_failures_never_authorize(self):
        contract = {"contract_version": "writ-host-outcome/1", "source": "adapter"}
        statuses = {
            "unregistered_tool": "deny",
            "identity_unverified": "deny",
            "unsupported_workflow_operation": "deny",
            "malformed": "invalid",
            "oversized": "invalid",
            "host_failure": "unavailable",
        }
        for reason, status in statuses.items():
            advisory = contract | dict(mode="advisory", label="ungoverned", status=None, disposition="observation_only")
            governed = contract | dict(mode="governed", label="governed", status=status, disposition="blocked")
            with self.subTest(reason=reason):
                VALIDATORS["host-outcome"].validate(advisory | {"reason": reason})
                VALIDATORS["host-outcome"].validate(governed | {"reason": reason})
                self.assertFalse(VALIDATORS["host-outcome"].is_valid(advisory | {"reason": reason, "status": "allow"}))
                self.assertFalse(VALIDATORS["host-outcome"].is_valid(governed | {
                    "reason": reason, "status": "allow", "disposition": "continue_in_broker"
                }))

    def test_portability_outcomes_and_synthetic_event_fixtures(self):
        case_ids = [case["id"] for case in PORTABILITY_CASES]
        self.assertEqual(len(set(case_ids)), len(case_ids))
        for case in PORTABILITY_CASES:
            with self.subTest(case=case["id"]):
                for event in case.get("advisory_examples", []):
                    VALIDATORS["host-event"].validate(event)
                for outcome in case.get("expected_outcomes", []):
                    VALIDATORS["host-outcome"].validate({"contract_version": "writ-host-outcome/1", **outcome})
                if case.get("repeat_utf8"):
                    payload = case["repeat_utf8"]["value"] * case["repeat_utf8"]["count"]
                    self.assertGreater(len(payload.encode("utf-8")), 64 * 1024)


    def test_independent_write_fixture_projection_is_equal(self):
        comparisons = {case["id"]: case for case in PORTABILITY_CASES if "expected_observation" in case}
        self.assertEqual(set(comparisons), {"unicode-original", "unicode-decomposed", "symlink-parent", "permission-prompt"})
        for case_id, case in comparisons.items():
            with self.subTest(case=case_id):
                claude = claude_write_observation(case["hook_input"])
                cli = cli_write_observation(case["cli_input"])
                self.assertEqual(claude, case["expected_observation"])
                self.assertEqual(cli, claude)
        original = comparisons["unicode-original"]["target_path"].encode()
        decomposed = comparisons["unicode-decomposed"]["target_path"].encode()
        self.assertNotEqual(original, decomposed)

    def test_nested_diagnostic_claims_cannot_change_projected_observation(self):
        prompt = next(case for case in PORTABILITY_CASES if case["id"] == "permission-prompt")
        hook = copy.deepcopy(prompt["hook_input"])
        cli = copy.deepcopy(prompt["cli_input"])
        claims = {"grant": "claimed-grant", "approval": True, "action": {"kind": "workflow.transition"}}
        hook["tool_input"]["diagnostics"] = claims
        cli["diagnostics"] = claims
        for view in (claude_write_observation(hook), cli_write_observation(cli)):
            self.assertEqual(view, prompt["expected_observation"])
        advisory = copy.deepcopy(FIXTURES["host-event"])
        advisory["input"]["diagnostics"] = claims
        VALIDATORS["host-event"].validate(advisory)

    def test_fixture_normalizers_reject_ambiguous_path_segments(self):
        for path in ("", "/absolute", "../escape", "nested/./file", "nested//file", "end/", "x\\y", "x\x00y", "x\x7fy", "x\x85y"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    claude_write_observation({
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Write",
                        "tool_input": {"file_path": "/fixture/tree/" + path},
                    })
                with self.assertRaises(ValueError):
                    cli_write_observation({"phase": "before", "operation": "file-write", "target": path})
        with self.assertRaises(ValueError):
            claude_write_observation({
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/fixture/tree-other/report.txt"},
            })


if __name__ == "__main__":
    unittest.main()
