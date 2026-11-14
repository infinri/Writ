#!/usr/bin/env python3
"""M2 gate evidence: every full Decision the kernel emits for the decide fixture
inputs (golden-decisions, transition-decisions, protocol-decide) validates against
docs/contracts/decision.schema.json. The three-member D-022 refusal
(`REQUEST_SCHEMA_INVALID`, `REQUEST_JSON_INVALID`) is not a Decision and is counted
separately.

  python3 kernel/tools/decision_schema_check.py --kernel target/release/examples/m1_line_adapter
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
SETS = ("golden-decisions", "transition-decisions", "protocol-decide")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", required=True)
    a = ap.parse_args()
    schema = json.loads((ROOT / "docs/contracts/decision.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    lines = ""
    for s in SETS:
        for raw in (ROOT / f"m1/fixtures/sets/{s}.jsonl").read_text().splitlines():
            if raw.strip():
                c = json.loads(raw)
                env = {"id": f"{s}/{c['id']}"}
                env.update({"input_text": c["input_text"]} if "input_text" in c else {"input": c["input"]})
                lines += json.dumps(env) + "\n"
    out = subprocess.run([a.kernel, "decide"], input=lines, capture_output=True, text=True, check=True).stdout
    full, refusals, errors = 0, 0, []
    for raw in out.splitlines():
        msg = json.loads(raw)
        o = msg["output"]
        if set(o) == {"status", "reason_code", "matched_rule_ids"}:
            refusals += 1
            continue
        full += 1
        errs = sorted(validator.iter_errors(o), key=lambda e: list(e.path))
        if errs:
            errors.append(f"{msg['id']}: {errs[0].message}")
    print(f"decision schema: {full} full Decisions valid against decision.schema.json, {len(errors)} invalid; "
          f"{refusals} three-member D-022 refusals")
    for e in errors[:10]:
        print("decision schema:", e, file=sys.stderr)
    return 1 if errors or full == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
