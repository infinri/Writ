#!/usr/bin/env python3
"""Self-test proxy: runs the real adapter and injects one wrong answer into its output.

  python3 m3/props/mutant.py --real target/release/examples/m3_line_adapter --mode MODE broker-scenario

Modes:
  m1_b36      the frozen M1 b-36 expectation instead of the approved difference: after a
              reconcile that resolved receipts, an identical retry is refused
              RECONCILIATION_REQUIRED (and the refusal is counted) instead of replaying
  conc_extra  one IDEMPOTENCY_CONFLICT of a concurrent group is reported as effect_completed
  refusals    the final refusal count is one higher
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def mutate(mode: str, out):
    if not isinstance(out, dict) or not isinstance(out.get("steps"), list):
        return out
    steps = out["steps"]
    if mode == "m1_b36":
        resolved = False
        for i, s in enumerate(steps):
            if isinstance(s, dict) and s.get("result") == "reconciled" and s.get("count", 0) > 0:
                resolved = True
            elif resolved and s == {"replayed": True, "result": "effect_completed"}:
                steps[i] = {"code": "RECONCILIATION_REQUIRED", "result": "refused"}
                if isinstance(out.get("final"), dict):
                    out["final"]["refusals"] = out["final"].get("refusals", 0) + 1
    elif mode == "conc_extra":
        for s in steps:
            o = s.get("outcomes") if isinstance(s, dict) else None
            if isinstance(o, dict) and o.get("IDEMPOTENCY_CONFLICT"):
                o["IDEMPOTENCY_CONFLICT"] -= 1
                if not o["IDEMPOTENCY_CONFLICT"]:
                    del o["IDEMPOTENCY_CONFLICT"]
                o["effect_completed"] = o.get("effect_completed", 0) + 1
                break
    elif mode == "refusals" and isinstance(out.get("final"), dict):
        out["final"]["refusals"] = out["final"].get("refusals", 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--mode", required=True, choices=["m1_b36", "conc_extra", "refusals"])
    ap.add_argument("command")
    args = ap.parse_args()
    proc = subprocess.run([args.real, args.command], input=sys.stdin.buffer.read(), capture_output=True)
    for raw in proc.stdout.decode("utf-8").split("\n"):
        if not raw.strip():
            continue
        msg = json.loads(raw)
        msg["output"] = mutate(args.mode, msg.get("output"))
        sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
