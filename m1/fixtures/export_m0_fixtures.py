#!/usr/bin/env python3
"""Export existing language-neutral M0 fixtures into M1 parity sets (JSON Lines).

Inputs are the committed contract fixtures only (no M0 Python code is imported):
- docs/contracts/fixtures/decisions/m0-golden.json: 26 hand-reasoned golden decisions,
  all PENDING INDEPENDENT REVIEW (provisional oracle, risk R-ORACLE).
- the canonical-JSON known vector fixed in D-006.
- m1/fixtures/acceptance/acceptance-binding.cases.json: hand-reasoned check-acceptance cases (M1-02).

Usage: export_m0_fixtures.py            write sets/*.jsonl
       export_m0_fixtures.py --check    exit 1 if the committed sets are stale
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GOLDEN = ROOT / "docs/contracts/fixtures/decisions/m0-golden.json"


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


def line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return [line({
        "id": "d006-known-vector",
        "input_text": '{"b": [1, true, null], "a": "é\\n", "c": {"z": 0, "y": "x"}}',
        "expect": {"canonical": text, "digest": digest},
        "compare": "exact",
        "meta": {"source": "D-006; m0/tests/test_contracts.py::TestCanonical::test_known_vector"},
    })]


ACCEPTANCE = HERE / "acceptance/acceptance-binding.cases.json"


def d006_digest(value) -> str:
    """Digest for fixture construction only (keys here are ASCII, no floats in valid records)."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def acceptance_binding() -> list[str]:
    src = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    base_digest = d006_digest(src["base_record"])
    out = []
    for case in src["cases"]:
        record = merge_patch(src["base_record"], case.get("record_patch", {}))
        if "criteria_replace" in case:
            record["criteria"] = copy.deepcopy(case["criteria_replace"])
        for cid, patch in case.get("criteria_patch", {}).items():
            matches = [c for c in record["criteria"] if c["criterion_id"] == cid]
            if len(matches) != 1:
                raise SystemExit(f"{case['id']}: criteria_patch target {cid!r} not unique")
            matches[0].update(merge_patch(matches[0], patch))
        record["record_digest"] = base_digest if case.get("keep_digest") else d006_digest(record)
        current = merge_patch(src["base_current"], case.get("current_patch", {}))
        out.append(line({
            "id": case["id"],
            "input": {"record": record, "current": current},
            "expect": case["expect"],
            "compare": "exact",
            "meta": {"source": "m1/fixtures/acceptance/acceptance-binding.cases.json",
                     "review_status": src["review_status"], "rationale": case["rationale"]},
        }))
    return out


SETS = {"sets/golden-decisions.jsonl": golden_decisions, "sets/canonical-vector.jsonl": canonical_vector,
        "sets/acceptance-binding.jsonl": acceptance_binding}


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
