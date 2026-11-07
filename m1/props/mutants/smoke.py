#!/usr/bin/env python3
"""Mutation smoke test: run each mutant against the real prototype it wraps and
record which oracles caught it (oracles failing on the mutant but not on the
real binary), plus the differential count.

  python3 m1/props/mutants/smoke.py --real <proto binary> --seed 7 --cases 300 --out smoke.json
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run as harness  # noqa: E402

MUTATIONS = {"dup-accept": "json", "ignore-artifact": "acceptance", "double-grant": "broker",
             "redispatch": "broker", "leak-codes": "refusal", "scope-allow": "decide",
             "forbid-ignored": "decide", "replay-commit": "transition"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    summary = {"real": a.real, "seed": a.seed, "cases": a.cases, "mutations": {}}
    for m, camp in MUTATIONS.items():
        with tempfile.TemporaryDirectory() as td:
            rep = Path(td) / "r.json"
            harness.main(["--campaign", camp, "--seed", str(a.seed), "--cases", str(a.cases), "--repeats", "1",
                          "--rust", a.real, "--go", f"{sys.executable} {HERE / 'mutant.py'} --real {a.real} --mutation {m}",
                          "--out", str(rep), "--no-minimize", "--regressions", td])
            body = json.loads(rep.read_text())["campaigns"][camp]
        caught = {inv: per["go"]["fail"] for inv, per in body["invariants"].items()
                  if "go" in per and per["go"]["fail"] and not per.get("rust", {}).get("fail")}
        summary["mutations"][m] = {"campaign": camp, "caught_by": caught,
                                   "differential_mismatches": body["differential_mismatches"],
                                   "detected": bool(caught)}
        print(f"{m:16s} {camp:11s} oracles={sorted(caught)} differential={body['differential_mismatches']}", flush=True)
    a.out.write_text(json.dumps(summary, indent=1) + "\n")
    return 0 if all(v["detected"] for v in summary["mutations"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
