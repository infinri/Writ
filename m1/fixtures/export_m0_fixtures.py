#!/usr/bin/env python3
"""Export language-neutral fixtures into M1 parity sets (JSON Lines).

Inputs are committed fixture sources only (no M0 Python code is imported):
- docs/contracts/fixtures/decisions/m0-golden.json: 26 hand-reasoned golden decisions,
  all PENDING INDEPENDENT REVIEW (provisional oracle, risk R-ORACLE).
- the canonical-JSON known vector fixed in D-006.
- m1/fixtures/canonical/strict-json.cases.json: strict parsing and canonical cases (D-017).
- m1/fixtures/acceptance/acceptance-binding.cases.json: check-acceptance cases (m1-draft-2).
- m1/fixtures/transition/*.cases.json: workflow.transition decisions and scenarios (m1-draft-4).
- m1/fixtures/refusal/external-refusal.cases.json: internal to external refusal mapping (D-018).

This script only assembles inputs (patches, digests of fixture-construction values,
raw-text edits). Every expected result is copied verbatim from a source file.

Usage: export_m0_fixtures.py            write sets/*.jsonl
       export_m0_fixtures.py --check    exit 1 if the committed sets are stale
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GOLDEN = ROOT / "docs/contracts/fixtures/decisions/m0-golden.json"
ACCEPTANCE = HERE / "acceptance/acceptance-binding.cases.json"
TRANSITION = HERE / "transition"
STRICT = HERE / "canonical/strict-json.cases.json"
REFUSAL = HERE / "refusal/external-refusal.cases.json"
BROKER = HERE / "broker/broker-scenarios.cases.json"
M0_BUNDLE = ROOT / "docs/contracts/fixtures/policy/m0-reference.bundle.json"


def nest_text(pattern: str, count: int, leaf: str) -> str:
    """Wrap leaf in count containers from the inside out (array: [x]; object: {"k":x}; alternate starts with array)."""
    text = leaf
    for i in range(count):
        kind = pattern if pattern != "alternate" else ("array" if i % 2 == 0 else "object")
        text = f"[{text}]" if kind == "array" else '{"k":' + text + "}"
    return text


def merge_patch(target, patch):
    """RFC 7396 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


line = compact


def d006_digest(value) -> str:
    """Digest for fixture construction (ASCII keys, integers only in hashed values)."""
    return "sha256:" + hashlib.sha256(compact(value).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def edit_once(text: str, old: str, new: str, case_id: str) -> str:
    if old not in text:
        raise SystemExit(f"{case_id}: text_edit target not found: {old!r}")
    return text.replace(old, new, 1)


def object_text(parts: dict[str, str]) -> str:
    """Assemble an object from already-serialized member values, keys sorted."""
    return "{" + ",".join(f"{json.dumps(k)}:{parts[k]}" for k in sorted(parts)) + "}"


def apply_criteria(record: dict, spec: dict, case_id: str) -> dict:
    if "criteria_replace" in spec:
        record["criteria"] = copy.deepcopy(spec["criteria_replace"])
    for cid, patch in spec.get("criteria_patch", {}).items():
        matches = [c for c in record["criteria"] if c["criterion_id"] == cid]
        if len(matches) != 1:
            raise SystemExit(f"{case_id}: criteria_patch target {cid!r} not unique")
        patched = merge_patch(matches[0], patch)
        matches[0].clear()
        matches[0].update(patched)
    return record


def build_record(base: dict, spec: dict, case_id: str, keep_digest: bool = False) -> dict:
    record = apply_criteria(merge_patch(base, spec.get("record_patch", {})), spec, case_id)
    record["record_digest"] = d006_digest(base) if keep_digest else d006_digest(record)
    return record


def golden_decisions() -> list[str]:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    bundle = json.loads((GOLDEN.parent / golden["bundle"]).read_text(encoding="utf-8"))
    out = []
    for case in golden["cases"]:
        out.append(line({
            "id": case["case_id"],
            "input": {"bundle": bundle if case["bundle_loaded"] else None,
                      "state_revision": golden["state_revision"], "now": golden["now"],
                      "request": merge_patch(golden["base_request"], case["patch"])},
            "expect": case["expected"],
            "compare": "fields",
            "fields": ["status", "reason_code", "matched_rule_ids"],
            "meta": {"source": "docs/contracts/fixtures/decisions/m0-golden.json",
                     "review_status": case["review_status"], "rationale": case["rationale"]},
        }))
    return out


def canonical_vector() -> list[str]:
    # D-006 known vector; the expected canonical text is written by hand, not computed.
    text = '{"a":"é\\n","b":[1,true,null],"c":{"y":"x","z":0}}'
    return [line({
        "id": "d006-known-vector",
        "input_text": '{"b": [1, true, null], "a": "é\\n", "c": {"z": 0, "y": "x"}}',
        "expect": {"canonical": text, "digest": text_digest(text)},
        "compare": "exact",
        "meta": {"source": "D-006; m0/tests/test_contracts.py::TestCanonical::test_known_vector"},
    })]


def strict_json() -> list[str]:
    src = json.loads(STRICT.read_text(encoding="utf-8"))
    out = []
    for case in src["cases"]:
        if "input_text_repeat" in case:
            r = case["input_text_repeat"]
            text = r["open"] * r["count"] + r["close"] * r["count"]
        elif "nest" in case:
            n = case["nest"]
            text = nest_text(n["pattern"], n["count"], n["leaf"])
        else:
            text = case["input_text"]
        if "error" in case:
            expect = {"error": case["error"]}
        else:
            if case.get("expected_canonical_nest"):
                n = case["nest"]
                canon = nest_text(n["pattern"], n["count"], n["leaf"])
            elif "expected_canonical_repeat" in case:
                r = case["expected_canonical_repeat"]
                canon = r["open"] * r["count"] + r["close"] * r["count"]
            else:
                canon = case["expected_canonical"]
            expect = {"canonical": canon, "digest": text_digest(canon)}
        out.append(line({"id": case["id"], "input_text": text, "expect": expect, "compare": "exact",
                         "meta": {"source": "m1/fixtures/canonical/strict-json.cases.json",
                                  "review_status": src["review_status"], "rationale": case["rationale"]}}))
    return out


def acceptance_binding() -> list[str]:
    src = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    out = []
    for case in src["cases"]:
        record = build_record(src["base_record"], case, case["id"], case.get("keep_digest", False))
        current = merge_patch(src["base_current"], case.get("current_patch", {}))
        requirements = merge_patch(src["base_requirements"], case.get("requirements_patch", {}))
        if "requirements_criteria" in case:
            requirements["criteria"] = copy.deepcopy(case["requirements_criteria"])
        entry = {"id": case["id"], "expect": case["expect"], "compare": "exact",
                 "meta": {"source": "m1/fixtures/acceptance/acceptance-binding.cases.json",
                          "review_status": src["review_status"], "rationale": case["rationale"]}}
        value = {"record": record, "current": current, "requirements": requirements}
        if "text_edit" in case:
            e = case["text_edit"]
            parts = {k: compact(v) for k, v in value.items()}
            parts[e["target"]] = edit_once(parts[e["target"]], e["old"], e["new"], case["id"])
            entry["input_text"] = object_text(parts)
        else:
            entry["input"] = value
        out.append(line(entry))
    return out


def transition_decisions() -> list[str]:
    src = json.loads((TRANSITION / "transition-decisions.cases.json").read_text(encoding="utf-8"))
    bundles = {None: json.loads((TRANSITION / src["bundle"]).read_text(encoding="utf-8"))}
    for name, file in src.get("bundle_variants", {}).items():
        bundles[name] = json.loads((TRANSITION / file).read_text(encoding="utf-8"))
    out = []
    for case in src["cases"]:
        bundle = bundles[case.get("bundle_variant")]
        request = merge_patch(src["base_request"], case.get("patch", {}))
        if case.get("pin_actual_bundle_sha"):
            request["policy_bundle_sha"] = d006_digest({k: v for k, v in bundle.items() if k != "publisher_signature"})
        value = {"bundle": bundle if case.get("bundle_loaded", True) else None,
                 "state_revision": src["state_revision"], "now": src["now"], "request": request}
        entry = {"id": case["id"], "expect": case["expected"], "compare": "fields",
                 "fields": ["status", "reason_code", "matched_rule_ids"],
                 "meta": {"source": "m1/fixtures/transition/transition-decisions.cases.json",
                          "review_status": src["review_status"], "rationale": case["rationale"]}}
        if "text_edit" in case:
            e = case["text_edit"]
            parts = {k: compact(v) for k, v in value.items()}
            parts["request"] = edit_once(parts["request"], e["old"], e["new"], case["id"])
            entry["input_text"] = object_text(parts)
        else:
            entry["input"] = value
        out.append(line(entry))
    return out


def transition_scenarios() -> list[str]:
    src = json.loads((TRANSITION / "transition-scenarios.cases.json").read_text(encoding="utf-8"))
    base_record = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))["base_record"]

    def record_for(spec: dict, case_id: str):
        if "record" in spec and spec["record"] is None:
            return None
        return build_record(base_record, spec.get("record", {}), case_id)

    def permit_for(spec: dict, request: dict, record, auth_sha: str, case_id: str, base_request: dict):
        if "permit" in spec and spec["permit"] is None:
            return None
        basis = spec.get("permit_for")
        if basis is None:
            p_request, p_record = request, record
        else:
            p_request = merge_patch(base_request, basis.get("request_patch", {}))
            p_record = record_for(basis, case_id)
        return {"request_fingerprint": d006_digest(p_request),
                "record_digest": p_record["record_digest"] if p_record else None,
                "authorization_bundle_sha": spec.get("permit_authorization_bundle_sha", auth_sha)}

    def expand(kind: str, spec: dict, case_id: str, auth_sha: str) -> dict:
        base_request = src["base_request"] if kind == "promote" else src["base_effect_request"]
        request = merge_patch(base_request, spec.get("request_patch", {}))
        op = {"op": kind, "idempotency_key": spec.get("idempotency_key", src["base_key"]), "request": request}
        if kind == "promote":
            record = record_for(spec, case_id)
            op["record"] = record
        else:
            record = None
        op["permit"] = permit_for(spec, request, record if kind == "promote" else None, auth_sha, case_id, base_request)
        if kind == "effect" and op["permit"] is not None:
            op["permit"].pop("record_digest")
        if "fault" in spec:
            op["fault"] = spec["fault"]
        return op

    out = []
    for case in src["cases"]:
        initial = merge_patch(src["base_initial"], case.get("initial_patch", {}))
        auth_sha = initial["authorization_bundle_sha"]
        steps = []
        for step in case["steps"]:
            if "promote" in step and "op" not in step:
                steps.append(expand("promote", step["promote"], case["id"], auth_sha))
            elif "effect" in step and "op" not in step:
                steps.append(expand("effect", step["effect"], case["id"], auth_sha))
            elif step.get("op") == "promote_concurrent":
                steps.append({"op": "promote_concurrent", "count": step["count"],
                              "promote": expand("promote", step["promote"], case["id"], auth_sha)})
            elif step.get("op") == "concurrent":
                ops = []
                for sub in step["operations"]:
                    if "effect" in sub and "op" not in sub:
                        ops.append(expand("effect", sub["effect"], case["id"], auth_sha))
                    elif "promote" in sub and "op" not in sub:
                        ops.append(expand("promote", sub["promote"], case["id"], auth_sha))
                    else:
                        ops.append(sub)
                steps.append({"op": "concurrent", "operations": ops})
            else:
                steps.append(step)
        value = {"initial": initial, "steps": steps}
        entry = {"id": case["id"], "expect": case["expect"], "compare": case.get("compare", "exact"),
                 "meta": {"source": "m1/fixtures/transition/transition-scenarios.cases.json",
                          "review_status": src["review_status"], "rationale": case["rationale"]}}
        if "text_edit" in case:
            e = case["text_edit"]
            entry["input_text"] = edit_once(compact(value), e["old"], e["new"], case["id"])
        else:
            entry["input"] = value
        out.append(line(entry))
    return out


def broker_scenarios() -> list[str]:
    from datetime import datetime, timedelta
    src = json.loads(BROKER.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    bundle = json.loads(M0_BUNDLE.read_text(encoding="utf-8"))
    keys = dict(src["ingress_keys"])
    all_keys = dict(keys, **src["unregistered_key"])

    def mac(key_hex: str, request: dict) -> str:  # simulated ingress seal (BROKER.md)
        return hmac.new(bytes.fromhex(key_hex), compact(request).encode("utf-8"), hashlib.sha256).hexdigest()

    def seal(kind, request: dict, base: dict):
        if kind == "none":
            return None
        if kind == "forged":
            return {"adapter_id": "writ-host-adapter:fixture", "mac": "0" * 64}
        if kind == "unregistered":
            return {"adapter_id": "writ-host-adapter:rogue", "mac": mac(all_keys["writ-host-adapter:rogue"], request)}
        if kind == "other_adapter":
            return {"adapter_id": "writ-host-adapter:other", "mac": mac(keys["writ-host-adapter:other"], request)}
        target = base if kind == "for_base" else request
        return {"adapter_id": "writ-host-adapter:fixture", "mac": mac(keys["writ-host-adapter:fixture"], target)}

    def build(op: dict, now: datetime) -> tuple[dict, datetime]:
        (name, spec), = op.items()
        if name == "concurrent":
            return {"op": "concurrent", "operations": [build(sub, now)[0] for sub in spec]}, now
        base = golden["base_request"]
        request = merge_patch(base, spec.get("request_patch", {}))
        if name == "execute":
            out = {"op": "execute", "request": request, "seal": seal(spec.get("seal", "valid"), request, base)}
            for k in ("grant_ref", "fault"):
                if k in spec:
                    out[k] = spec[k]
            return out, now
        if name == "approve":
            out = {"op": "approve", "for_request": request}
            out.update({k: v for k, v in spec.items() if k != "request_patch"})
            return out, now
        if name == "present_permit":
            out = {"op": "present_permit", "of_step": spec["of_step"], "request": request}
            if "tamper" in spec:
                out["tamper"] = spec["tamper"]
            return out, now
        if name == "apply_without_permit":
            return {"op": "apply_without_permit", "request": request}, now
        if name == "advance_time":
            now = now + timedelta(seconds=spec["seconds"])
            return {"op": "advance_time", "now": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, now
        if name == "activate_bundle":
            return {"op": "activate_bundle", "bundle": merge_patch(bundle, spec["bundle_patch"])}, now
        if name == "set_dependency":
            return dict({"op": "set_dependency"}, **spec), now
        if name == "reconcile":
            return {"op": "reconcile"}, now
        raise SystemExit(f"unknown broker op {name}")

    out = []
    for case in src["cases"]:
        now = datetime.fromisoformat(src["now"].replace("Z", "+00:00"))
        steps = []
        for op in case["steps"]:
            built, now = build(op, now)
            steps.append(built)
        initial = {"now": src["now"], "state_revision": src["state_revision"], "bundle": bundle,
                   "ingress_keys": keys, "permit_key": src["permit_key"], "channels": src["channels"],
                   "dependencies": {"policy": "up", "ledger": "up"}}
        out.append(line({"id": case["id"], "input": {"initial": initial, "steps": steps}, "expect": case["expect"],
                         "compare": "exact",
                         "meta": {"source": "m1/fixtures/broker/broker-scenarios.cases.json",
                                  "review_status": src["review_status"], "rationale": case["rationale"]}}))
    return out


def external_refusal() -> list[str]:
    src = json.loads(REFUSAL.read_text(encoding="utf-8"))
    return [line({"id": c["id"], "input": {"internal_code": c["internal_code"]}, "expect": {"external": c["external"]},
                  "compare": "exact",
                  "meta": {"source": "m1/fixtures/refusal/external-refusal.cases.json",
                           "review_status": src["review_status"], "rationale": c["rationale"]}})
            for c in src["cases"]]


SETS = {
    "sets/canonical-vector.jsonl": canonical_vector,
    "sets/strict-json.jsonl": strict_json,
    "sets/golden-decisions.jsonl": golden_decisions,
    "sets/acceptance-binding.jsonl": acceptance_binding,
    "sets/transition-decisions.jsonl": transition_decisions,
    "sets/transition-scenarios.jsonl": transition_scenarios,
    "sets/external-refusal.jsonl": external_refusal,
    "sets/broker-scenarios.jsonl": broker_scenarios,
}


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for rel, build in SETS.items():
        content = "\n".join(build()) + "\n"
        path = HERE / rel
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(rel)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if stale:
        print("stale:", ", ".join(stale), file=sys.stderr)
        return 1
    print("M1 exported fixture sets", "current" if check else "written", f"({len(SETS)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
