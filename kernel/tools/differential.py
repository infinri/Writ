#!/usr/bin/env python3
"""M2-06 differential gate: writ-kernel (through the dev-only M1 line adapter,
kernel/examples/m1_line_adapter.rs) versus an M1 prototype on the seeded M1
property campaigns json, decide, acceptance and refusal.

Passes only if every mismatch is one of the classified, documented groups
(kernel/FIXTURE-MAP.md "Differential harness"):
  - cross-command checks on transition-scenario and broker-scenario: M3 commands
    the kernel does not implement (the adapter refuses them);
  - envelope-deep_envelope: adapter limitation (object `input` re-serialized
    instead of re-parsed from raw bytes); the kernel itself answers DEPTH_EXCEEDED.
Any other mismatch or failed check fails the gate.

  python3 kernel/tools/differential.py --kernel target/release/examples/m1_line_adapter \\
      --reference m1/rust/target/release/proto --seed 20261107 --out diff.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGNS = "json,decide,acceptance,refusal"
M3_COMMANDS = ("transition-scenario", "broker-scenario")
ADAPTER_CASES = {"envelope-deep_envelope"}


def classify(report: dict) -> list[str]:
    problems = []
    for name, c in report["campaigns"].items():
        for cluster, info in (c.get("mismatch_clusters") or {}).items():
            command = cluster.split(":", 1)[0]
            if command in M3_COMMANDS:
                continue
            if info.get("first_case_id") in ADAPTER_CASES and info.get("count") == 1:
                continue
            problems.append(f"{name}: unclassified mismatch cluster {cluster!r} ({info.get('count')})")
        for inv, per in (c.get("invariants") or {}).items():
            if inv.startswith("differential.") or any(f"cross_command.{m}" in inv for m in M3_COMMANDS):
                continue
            for proto, v in per.items():
                if v.get("fail"):
                    problems.append(f"{name}: invariant {inv} failed for {proto} ({v['fail']})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    subprocess.run([sys.executable, str(ROOT / "m1/props/run.py"), "--campaign", CAMPAIGNS, "--profile", "ci",
                    "--seed", str(a.seed), "--rust", a.kernel, "--go", a.reference, "--out", str(a.out)], cwd=ROOT)
    report = json.loads(a.out.read_text())
    problems = classify(report)
    for p in problems:
        print("differential:", p, file=sys.stderr)
    counts = {n: (c.get("cases_generated"), len(c.get("mismatch_examples") or [])) for n, c in report["campaigns"].items()}
    print(f"differential (seed {a.seed}): campaigns {counts}; {len(problems)} unclassified problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
