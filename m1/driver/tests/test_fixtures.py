"""Fixture integrity tests (M1-02). These check that fixtures are built as their
sources say. They never compute or alter an expected result.

Run from the repository root:  python3 -m unittest discover -s m1/driver/tests -v
Schema checks need jsonschema (present in CI's M0 job and in m0/.venv); otherwise skipped.
"""
from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "m1/fixtures"
CONTRACTS = ROOT / "docs/contracts"

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


def load_set(name: str) -> list[dict]:
    return [json.loads(l) for l in (FIX / f"sets/{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def _reject_constant(name: str):
    raise ValueError(f"non-standard constant {name}")


def has_duplicate_key(text: str) -> bool:
    found = False

    def hook(pairs):
        nonlocal found
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            found = True
        return dict(pairs)

    json.loads(text, object_pairs_hook=hook)
    return found


class SourceFormatTests(unittest.TestCase):
    def test_every_case_is_pending_independent_review(self):
        for src in ["acceptance/acceptance-binding.cases.json", "transition/transition-decisions.cases.json",
                    "transition/transition-scenarios.cases.json", "canonical/strict-json.cases.json",
                    "refusal/external-refusal.cases.json"]:
            data = json.loads((FIX / src).read_text(encoding="utf-8"))
            self.assertEqual(data["review_status"], "PENDING INDEPENDENT REVIEW", src)

    def test_case_ids_unique_and_restricted(self):
        for name in ["strict-json", "acceptance-binding", "transition-decisions", "transition-scenarios", "external-refusal"]:
            ids = [c["id"] for c in load_set(name)]
            self.assertEqual(len(ids), len(set(ids)), name)
            for cid in ids:
                self.assertRegex(cid, r"^[a-z0-9][a-z0-9-]*$")

    def test_manifest_lists_every_set_file(self):
        manifest = json.loads((FIX / "manifest.json").read_text())
        listed = {s["file"] for s in manifest["sets"]}
        on_disk = {f"sets/{p.name}" for p in (FIX / "sets").glob("*.jsonl")}
        self.assertEqual(listed, on_disk)


class RawTextCaseTests(unittest.TestCase):
    """Raw-text cases must contain exactly the defect their expected error names."""

    def raw_cases(self):
        for name in ["strict-json", "acceptance-binding", "transition-decisions", "transition-scenarios"]:
            for case in load_set(name):
                if "input_text" in case:
                    yield name, case

    def expected_error(self, name, case):
        e = case["expect"]
        if name == "strict-json":
            return e.get("error")
        if name in ("acceptance-binding", "transition-scenarios"):
            return e.get("json_error")
        return "REQUEST_JSON_INVALID" if e.get("reason_code") == "REQUEST_JSON_INVALID" else None

    def test_duplicate_key_cases_contain_a_duplicate(self):
        n = 0
        for name, case in self.raw_cases():
            err = self.expected_error(name, case)
            if err == "DUPLICATE_KEY" or ("dup" in case["id"] and err == "REQUEST_JSON_INVALID"):
                self.assertTrue(has_duplicate_key(case["input_text"]), case["id"])
                n += 1
        self.assertGreaterEqual(n, 9)

    def test_invalid_json_cases_are_not_json(self):
        for name, case in self.raw_cases():
            if self.expected_error(name, case) == "INVALID_JSON":
                with self.assertRaises(ValueError, msg=case["id"]):
                    # Python's json accepts NaN and Infinity; RFC 8259 does not.
                    json.loads(case["input_text"], parse_constant=_reject_constant)

    def test_cases_without_text_defect_have_no_duplicates(self):
        for name, case in self.raw_cases():
            err = self.expected_error(name, case)
            if err in ("NUMBER_NOT_INTEGER", "NUMBER_NEGATIVE_ZERO", "NUMBER_OUT_OF_RANGE"):
                self.assertFalse(has_duplicate_key(case["input_text"]), case["id"])


@unittest.skipUnless(jsonschema, "jsonschema not installed")
class SchemaTests(unittest.TestCase):
    def schema(self, name):
        return json.loads((CONTRACTS / f"{name}.schema.json").read_text())

    def test_transition_bundles_validate(self):
        schema = self.schema("policy-bundle")
        for f in ["m1-transition", "m1-transition-unscoped"]:
            jsonschema.validate(json.loads((FIX / f"transition/{f}.bundle.json").read_text()), schema)

    def test_transition_requests_validate_unless_expected_invalid(self):
        schema = self.schema("action-request")
        validator = jsonschema.Draft202012Validator(schema)
        for case in load_set("transition-decisions"):
            if "input" not in case:
                continue
            errors = list(validator.iter_errors(case["input"]["request"]))
            if case["expect"]["reason_code"] == "REQUEST_SCHEMA_INVALID":
                self.assertTrue(errors, case["id"])
            else:
                self.assertFalse(errors, f"{case['id']}: {errors[:1]}")

    def test_scenario_requests_validate(self):
        validator = jsonschema.Draft202012Validator(self.schema("action-request"))
        checked = 0
        for case in load_set("transition-scenarios"):
            if "input" not in case:
                continue
            for step in case["input"]["steps"]:
                op = step.get("promote", step) if step.get("op") == "promote_concurrent" else step
                if op.get("op") in ("promote", "effect"):
                    self.assertFalse(list(validator.iter_errors(op["request"])), case["id"])
                    checked += 1
        self.assertGreater(checked, 50)

    def test_revision_bounds_in_schemas(self):
        for name, key in [("action-request", "expected_state_revision"), ("decision", "state_revision")]:
            prop = self.schema(name)["properties"][key]
            self.assertEqual((prop["minimum"], prop["maximum"]), (0, 9007199254740991), name)


class SemanticRuleTests(unittest.TestCase):
    """Structural rules from D-018 that every fixture input must already respect."""

    def test_no_request_repeats_its_kind_unless_expected_invalid(self):
        for name in ["transition-decisions"]:
            for case in load_set(name):
                if "input" not in case:
                    continue
                action = case["input"]["request"].get("action", {})
                if action.get("kind") in action.get("requested_capabilities", []):
                    self.assertEqual(case["expect"]["reason_code"], "REQUESTED_CAPABILITIES_INVALID", case["id"])

    def test_external_mapping_never_passes_through_acceptance_detail(self):
        for case in load_set("external-refusal"):
            code = case["input"]["internal_code"]
            if code.startswith(("ACCEPTANCE_", "PERMIT_", "REQUIREMENTS_")) and code != "ACCEPTANCE_RECORD_MALFORMED":
                self.assertEqual(case["expect"]["external"], "PROMOTION_REFUSED", case["id"])


class BrokerConstructionTests(unittest.TestCase):
    """Seals are built as BROKER.md says: valid seals verify, refused-at-ingress cases do not."""

    def test_seals_match_their_expected_ingress_outcome(self):
        import hashlib, hmac
        checked = 0
        for case in load_set("broker-scenarios"):
            init = case["input"]["initial"]
            results = case["expect"]["steps"]
            for step, result in zip(case["input"]["steps"], results):
                if step["op"] != "execute":
                    continue
                seal, req = step["seal"], step["request"]
                ok = False
                if seal and seal["adapter_id"] in init["ingress_keys"] and seal["adapter_id"] == req["principal"]["verified_by"]:
                    text = json.dumps(req, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    mac = hmac.new(bytes.fromhex(init["ingress_keys"][seal["adapter_id"]]), text, hashlib.sha256).hexdigest()
                    ok = hmac.compare_digest(mac, seal["mac"])
                refused_at_ingress = result.get("code") == "INGRESS_UNAUTHENTICATED"
                self.assertEqual(ok, not refused_at_ingress, case["id"])
                checked += 1
        self.assertGreater(checked, 40)


class CountTests(unittest.TestCase):
    def test_counts_by_set(self):
        counts = {name: len(load_set(name)) for name in
                  ["canonical-vector", "strict-json", "golden-decisions", "acceptance-binding",
                   "transition-decisions", "transition-scenarios"]}
        self.assertEqual(counts["golden-decisions"], 26)
        self.assertEqual(counts["canonical-vector"], 1)
        prefixes = Counter(c["id"].split("-")[0] for c in load_set("acceptance-binding"))
        self.assertIn("cmp", prefixes)
        self.assertIn("ma", prefixes)


if __name__ == "__main__":
    unittest.main()
