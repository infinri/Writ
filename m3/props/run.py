#!/usr/bin/env python3
"""M3-08 property gate: the M1 `broker` campaign, oracle-only, against the production adapter.

Seeded and deterministic (random.Random("<seed>:broker"), the M1 seed string). Writes a
JSON report and exits nonzero on any failed check, protocol problem or adapter error.
Standard library only; never writes into m1/ (see broker_oracle.py for the oracle
domain, the approved differences applied and the adapter limitation).

  cargo build --release --locked -p writ-ledger-sqlite --example m3_line_adapter
  python3 m3/props/run.py --profile ci --proto target/release/examples/m3_line_adapter --out report.json
  python3 m3/props/run.py --seed 7 --cases 3000 --repeats 5 --proto ... --out report.json
  python3 -m unittest discover -s m3/props

The M1 `transition` campaign is not run: see TRANSITION_NOT_APPLICABLE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker_oracle  # noqa: E402
from broker_oracle import Proto, Results  # noqa: E402

CI_CASES = 800
CI_REPEATS = 3
DEFAULT_SEED = 20261113

TRANSITION_NOT_APPLICABLE = (
    "The M1 `transition` campaign (m1/props/wprops/c_transition.py) is not applicable to the production "
    "broker without re-specification, which this gate does not invent. Its generators and oracles depend on: "
    "(1) a client-held promotion permit presented with every promote and an oracle built on the permit check "
    "order (PERMIT_MISSING, PERMIT_REQUEST_MISMATCH, PERMIT_BINDING_MISMATCH, PERMIT_POLICY_STALE); production "
    "has no client promotion permit (D-036 item 2); "
    "(2) inline, unauthenticated acceptance records whose consumption and replay the oracle tracks; production "
    "reads authenticated records by id from a trusted source and never reads inline records (D-036 item 1; "
    "OQ-6), and every generated requirement set includes the human criterion, which production refuses until "
    "M4 (D-037 item 4); "
    "(3) initial workflows in `verifying`, `planning` or `reviewing` given as scenario state, which production "
    "administration refuses to create (WORKFLOW_INITIAL_STATE_PROTECTED, D-036 item 5); the approved "
    "test-only seeding (D-037 item 1) does not exist in the adapter; "
    "(4) client-asserted `reconcile {observed}`, which production does not provide (D-036 item 3); "
    "(5) `effect` steps carrying a client permit, and `activate_authorization_bundle` with a bare digest "
    "(D-037 items 2 and 5). The adapter answers these steps `unsupported`. Coverage of the production "
    "transition service stays with the closure gate (m3/closure) and the production tests.")


def sha256_file(path: str) -> str | None:
    p = Path(path)
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=HERE,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def campaign(proto_argv: list[str], seed: int, cases: int, repeats: int, timeout: float = 900.0) -> dict:
    proto = Proto("production", proto_argv, timeout)
    res = Results("broker")
    rng = random.Random(f"{seed}:broker")
    t0 = time.perf_counter()
    stats = broker_oracle.run(proto, rng, cases, res, repeats)
    body = res.to_json()
    body.pop("differential_mismatches", None)
    body.pop("mismatch_clusters", None)
    body.pop("mismatch_examples", None)
    body.update(stats)
    body.update({"seed": seed, "rng_seed_string": f"{seed}:broker", "cases_requested": cases,
                 "elapsed_s": round(time.perf_counter() - t0, 2), "ok": not res.failed()})
    body["checks"] = sum(v["pass"] + v["fail"] for d in body["invariants"].values() for v in d.values())
    body["failed_checks"] = sum(v["fail"] for d in body["invariants"].values() for v in d.values())
    return body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proto", required=True, help="adapter command line (m3_line_adapter)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--cases", type=int, default=500, help="generated base sequences")
    ap.add_argument("--repeats", type=int, default=5, help="repetitions of each concurrent group")
    ap.add_argument("--profile", choices=["ci"], help=f"ci: {CI_CASES} sequences, {CI_REPEATS} repeats")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args(argv)
    cases = CI_CASES if args.profile == "ci" else args.cases
    repeats = CI_REPEATS if args.profile == "ci" else args.repeats
    argv_p = args.proto.split()
    report = {"harness": "m3-props/1", "started_at": datetime.now(timezone.utc).isoformat(),
              "repository_commit": git_head(),
              "host": {"platform": platform.platform(), "python": platform.python_version()},
              "proto": {"argv": argv_p, "executable_sha256": sha256_file(argv_p[0])},
              "m1_reused_read_only": broker_oracle.m1_digests(),
              "profile": args.profile, "seed": args.seed, "mode": "oracle-only (no differential)",
              "campaigns": {}}
    body = campaign(argv_p, args.seed, cases, repeats, args.timeout)
    report["campaigns"]["broker"] = body
    report["campaigns"]["transition"] = {"run": False, "applicable": False, "reason": TRANSITION_NOT_APPLICABLE}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["ok"] = body["ok"]
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=sorted) + "\n", encoding="utf-8")
    print(f"{'PASS' if body['ok'] else 'FAIL'} broker: {body['sequences']} sequences, {body['twins']} twins, "
          f"{body['concurrent_groups']} concurrent groups x {repeats}, {body['checks']} checks, "
          f"{body['failed_checks']} failed, protocol problems {sum(map(len, body['protocol_problems'].values()))}, "
          f"{body['elapsed_s']}s", flush=True)
    return 0 if body["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
