#!/usr/bin/env python3
"""M1-05 property, metamorphic, concurrency and differential harness.

Generates seeded inputs, runs them through the Rust and Go prototypes over the
JSON Lines protocol (the same one m1/driver/parity.py uses), checks spec-derived
oracles on every output, compares the two prototypes byte for byte, and
minimizes what fails. Standard library only.

  python3 m1/props/run.py --campaign all --seed 1 --cases 2000 \
      --rust m1/rust/target/release/proto --go m1/go/proto --out report.json
  python3 m1/props/run.py --profile ci --rust ... --go ... --out ci.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from wprops import c_accept, c_broker, c_decide, c_json, c_refusal, c_transition  # noqa: E402
from wprops.common import Proto, Results  # noqa: E402
from wprops.minimize import minimize_findings  # noqa: E402

CAMPAIGNS = {
    "json": c_json,
    "decide": c_decide,
    "acceptance": c_accept,
    "refusal": c_refusal,
    "transition": c_transition,
    "broker": c_broker,
}
# CI profile: cases per campaign and concurrency repetitions; sized for < ~3 minutes
CI_PROFILE = {"json": 1500, "decide": 1500, "acceptance": 1500, "refusal": 600, "transition": 800, "broker": 800}
CI_REPEATS = 3


def sha256_file(path: str) -> str | None:
    found = shutil.which(path) or (path if Path(path).exists() else None)
    if not found:
        return None
    return "sha256:" + hashlib.sha256(Path(found).read_bytes()).hexdigest()


def proto_info(argv: list[str]) -> dict:
    info = {"argv": argv, "executable_sha256": sha256_file(argv[0])}
    # wrappers (mutants) name the real binary with --real
    if "--real" in argv:
        real = argv[argv.index("--real") + 1]
        info["wrapped_sha256"] = sha256_file(real)
    return info


def git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=HERE,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--campaign", default="all", help="campaign name, comma list, or all: " + ", ".join(CAMPAIGNS))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cases", type=int, default=500, help="generated cases per campaign")
    ap.add_argument("--repeats", type=int, default=10, help="repetitions of each concurrency batch")
    ap.add_argument("--rust", required=True, help="Rust prototype command line")
    ap.add_argument("--go", required=True, help="Go prototype command line")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--profile", choices=["ci"], help="ci: fixed small sizes for every campaign")
    ap.add_argument("--regressions", type=Path, default=HERE / "regressions",
                    help="directory for minimized regression cases (only spec-determined expectations)")
    ap.add_argument("--no-minimize", action="store_true")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--ledger", choices=["memory", "sqlite"], default="memory",
                    help="broker campaign ledger (sqlite: file-backed, D-023/D-026 evidence)")
    ap.add_argument("--concurrency-profile", choices=["primary", "cold"], default="primary",
                    help="D-027 concurrent-worker start profile (cold: supplemental, report separately)")
    args = ap.parse_args(argv)
    c_broker.LEDGER = "sqlite" if args.ledger == "sqlite" else None
    c_broker.PROFILE = args.concurrency_profile

    names = list(CAMPAIGNS) if args.campaign == "all" else args.campaign.split(",")
    unknown = [n for n in names if n not in CAMPAIGNS]
    if unknown:
        ap.error(f"unknown campaigns {unknown}")
    protos = [Proto("rust", args.rust.split(), args.timeout), Proto("go", args.go.split(), args.timeout)]

    started = datetime.now(timezone.utc).isoformat()
    report = {"harness": "m1-props/1", "started_at": started, "repository_commit": git_head(),
              "host": {"platform": platform.platform(), "python": platform.python_version()},
              "protos": {p.name: proto_info(p.argv) for p in protos},
              "profile": args.profile, "seed": args.seed, "ledger": args.ledger,
              "concurrency_profile": args.concurrency_profile, "campaigns": {}}
    all_ok = True
    for name in names:
        mod = CAMPAIGNS[name]
        n = CI_PROFILE[name] if args.profile == "ci" else args.cases
        repeats = CI_REPEATS if args.profile == "ci" else args.repeats
        rng = random.Random(f"{args.seed}:{name}")
        res = Results(name)
        t0 = time.perf_counter()
        kwargs = {"repeats": repeats} if name in ("broker", "transition") else {}
        cases, outs = mod.run(protos, rng, n, res, **kwargs)
        findings = []
        if not args.no_minimize and res.failed():
            findings = minimize_findings(name, protos, res, args.regressions, args.seed)
        body = res.to_json()
        body.update({"seed": args.seed, "rng_seed_string": f"{args.seed}:{name}", "cases_requested": n,
                     "cases_generated": len(cases), "repeats": repeats if name in ("broker", "transition") else None,
                     "elapsed_s": round(time.perf_counter() - t0, 2), "minimized_findings": findings,
                     "ok": not res.failed()})
        if hasattr(mod, "extra_report"):
            body.update(mod.extra_report(res))
        report["campaigns"][name] = body
        all_ok &= body["ok"]
        print(f"{'PASS' if body['ok'] else 'FAIL'} {name}: {len(cases)} cases, "
              f"{sum(v['pass'] + v['fail'] for d in body['invariants'].values() for v in d.values())} checks, "
              f"{body['differential_mismatches']} mismatches, "
              f"{sum(v['fail'] for d in body['invariants'].values() for v in d.values())} failed checks, "
              f"{body['elapsed_s']}s", flush=True)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["ok"] = all_ok
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=sorted) + "\n", encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
