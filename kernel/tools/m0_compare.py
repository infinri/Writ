#!/usr/bin/env python3
"""M2 gate evidence: full Decision comparison of writ-kernel with the live M0
reference (m0/writ_m0/decision.py) on the 26 golden decision cases, which M0
produced. Every differing field must be a documented M0 difference
(kernel/FIXTURE-MAP.md "Semantic differences and readings").

  m0/.venv/bin/python kernel/tools/m0_compare.py --kernel target/release/examples/m1_line_adapter
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "m0"))
from writ_m0 import decision as m0  # noqa: E402

# Targeted probes for the documented M0 differences (FIXTURE-MAP.md "Semantic differences"):
# (probe name, mutation of the golden case "workspace-file-write", exact set of fields allowed to differ).
PROBES = [
    ("request_id missing", lambda r: r.pop("request_id"), {"decision_id"}),
    ("request_id null", lambda r: r.__setitem__("request_id", None), {"decision_id"}),
    # the golden case decides at now = 2026-11-05T12:00:00Z; a deadline inside the 5 minute window sets expires_at
    ("deadline_at with fraction", lambda r: r.__setitem__("deadline_at", "2026-11-05T12:01:20.250Z"), {"expires_at"}),
    ("deadline_at with offset", lambda r: r.__setitem__("deadline_at", "2026-11-05T13:01:20+01:00"), {"expires_at"}),
]
# D-022 (approved): `now` must be strict UTC whole seconds; M0 predates D-022 and accepts other forms.
NOW_PROBES = [("now with fraction", "2026-11-05T12:00:00.500Z"), ("now with offset", "2026-11-05T13:00:00+01:00")]


def m0_decide(inp: dict) -> dict:
    now = datetime.fromisoformat(inp["now"].replace("Z", "+00:00"))
    try:
        bundle = m0.load_bundle(inp["bundle"]) if inp.get("bundle") is not None else None
    except m0.BundleError:
        bundle = "rejected"
    if bundle == "rejected":
        return {"m0": "bundle rejected"}
    return m0.decide(inp["request"], bundle, m0.KernelState(inp["state_revision"]), now)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", required=True)
    a = ap.parse_args()
    cases = [json.loads(l) for l in (ROOT / "m1/fixtures/sets/golden-decisions.jsonl").read_text().splitlines() if l.strip()]
    lines = "".join(json.dumps({"id": c["id"], "input": c["input"]}) + "\n" for c in cases)
    out = subprocess.run([a.kernel, "decide"], input=lines, capture_output=True, text=True, check=True).stdout
    kern = {json.loads(l)["id"]: json.loads(l)["output"] for l in out.splitlines() if l.strip()}
    undocumented, field_counts, identical = [], {}, 0
    base = next(c for c in cases if c["id"] == "workspace-file-write")
    probes = []
    for name, mutate, allowed in PROBES:
        inp = json.loads(json.dumps(base["input"]))
        mutate(inp["request"])
        probes.append((name, inp, allowed))
    plines = "".join(json.dumps({"id": f"probe-{i}", "input": inp}) + "\n" for i, (_, inp, _) in enumerate(probes))
    pout = subprocess.run([a.kernel, "decide"], input=plines, capture_output=True, text=True, check=True).stdout
    pk = {json.loads(l)["id"]: json.loads(l)["output"] for l in pout.splitlines() if l.strip()}
    for i, (name, inp, allowed) in enumerate(probes):
        k, r = pk[f"probe-{i}"], m0_decide(inp)
        diff = {f for f in set(k) | set(r) if k.get(f, "<missing>") != r.get(f, "<missing>")}
        status = "ok" if diff == allowed else "UNDOCUMENTED"
        print(f"m0 compare probe {name!r}: kernel status {k.get('status')}, differing fields {sorted(diff)} "
              f"(documented {sorted(allowed)}) {status}")
        if diff != allowed:
            undocumented.append(f"probe {name}: differing fields {sorted(diff)}, documented {sorted(allowed)}")
    for name, now in NOW_PROBES:
        inp = json.loads(json.dumps(base["input"]))
        inp["now"] = now
        line = json.dumps({"id": "n", "input": inp}) + "\n"
        k = json.loads(subprocess.run([a.kernel, "decide"], input=line, capture_output=True, text=True, check=True).stdout)["output"]
        r = m0_decide(inp)
        ok = k == {"matched_rule_ids": [], "reason_code": "REQUEST_SCHEMA_INVALID", "status": "invalid"} and r.get("status") == "allow"
        print(f"m0 compare probe {name!r}: kernel {k.get('status')} {k.get('reason_code')} (D-022 strict now), "
              f"M0 {r.get('status')} {'ok' if ok else 'UNDOCUMENTED'}")
        if not ok:
            undocumented.append(f"probe {name}: kernel {k}, M0 status {r.get('status')}")
    for c in cases:
        k, r = kern[c["id"]], m0_decide(c["input"])
        if "m0" in r:
            undocumented.append(f"{c['id']}: M0 rejected the bundle, kernel answered {k.get('status')}")
            continue
        diff = sorted(f for f in set(k) | set(r) if k.get(f, "<missing>") != r.get(f, "<missing>"))
        if not diff:
            identical += 1
        for f in diff:
            field_counts[f] = field_counts.get(f, 0) + 1
            undocumented.append(f"{c['id']}: field {f}: kernel {k.get(f)!r} versus M0 {r.get(f)!r}")
    print(f"m0 compare: {len(cases)} golden cases, {identical} identical in every field, differing fields {field_counts}")
    for u in undocumented:
        print("m0 compare: UNDOCUMENTED", u, file=sys.stderr)
    return 1 if undocumented else 0


if __name__ == "__main__":
    sys.exit(main())
