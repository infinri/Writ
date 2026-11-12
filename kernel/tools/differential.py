#!/usr/bin/env python3
"""M2-06 differential gate: writ-kernel (through the dev-only M1 line adapter,
kernel/examples/m1_line_adapter.rs) versus an M1 prototype on the seeded M1
property campaigns json, decide, acceptance and refusal (CI profile).

The report must belong to this execution (fresh output file, requested seed,
start time after this run began), contain exactly the expected campaigns with
positive case counts, and every failure must reconcile exactly with a documented
exception (kernel/FIXTURE-MAP.md "Differential harness"):

  M3 commands (json campaign cross-command checks on `transition-scenario` and
  `broker-scenario`): the kernel side must answer none of the N generated cases,
  with the adapter's usage exit (status 2) and "N ids unanswered"; the reference
  must pass its own `json.cross_command.transition-scenario` check for all N; each
  command contributes exactly N whole-output mismatches (2N `differential.exact_output`
  failures in total) and nothing else.

  Adapter envelope case (json campaign, command `canonical`, case
  `envelope-deep_envelope`, count 1): the kernel side answers
  {null: {"error": "ENVELOPE_INVALID"}}, the reference {"e": {"error": "DEPTH_EXCEEDED"}};
  exactly one `differential.envelope_answers` failure.

Anything else (another failed check, another mismatch cluster, a different count,
a reference failure, a missing or empty campaign, a runner error) fails the gate.

  python3 kernel/tools/differential.py --kernel target/release/examples/m1_line_adapter \\
      --reference m1/rust/target/release/proto --seed 20261107 --out diff.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGNS = ("json", "decide", "acceptance", "refusal")
M3_COMMANDS = ("transition-scenario", "broker-scenario")
KERNEL, REFERENCE = "rust", "go"  # positions passed to m1/props/run.py (--rust kernel, --go reference)
ADAPTER_CASE = {"campaign": "json", "cluster": "canonical: differs in answers", "case": "envelope-deep_envelope",
                "count": 1, "kernel": {"null": {"error": "ENVELOPE_INVALID"}},
                "reference": {"e": {"error": "DEPTH_EXCEEDED"}}}
USAGE_PREFIX = "exit status 2: usage: m1_line_adapter"


def structure(report: dict, seed: int, started: str | None) -> list[str]:
    problems = []
    if not isinstance(report, dict) or not isinstance(report.get("campaigns"), dict):
        return ["report has no campaigns object"]
    if report.get("seed") != seed:
        problems.append(f"report seed {report.get('seed')!r} is not the requested {seed}")
    if started is not None and not (isinstance(report.get("started_at"), str) and report["started_at"] >= started):
        problems.append("report predates this execution (stale)")
    names = set(report["campaigns"])
    if names != set(CAMPAIGNS):
        problems.append(f"campaigns {sorted(names)} differ from the expected {sorted(CAMPAIGNS)}")
    for name in CAMPAIGNS:
        c = report["campaigns"].get(name)
        if not isinstance(c, dict):
            continue
        if not isinstance(c.get("cases_generated"), int) or c["cases_generated"] <= 0:
            problems.append(f"{name}: no generated cases")
        if not c.get("invariants"):
            problems.append(f"{name}: no checks recorded")
        if c.get("seed") != seed:
            problems.append(f"{name}: campaign seed {c.get('seed')!r} is not {seed}")
    return problems


def fails(c: dict, inv: str, proto: str) -> int:
    return ((c.get("invariants") or {}).get(inv) or {}).get(proto, {}).get("fail", 0)


def reconcile(report: dict) -> list[str]:
    """Every failure must be an exact documented exception."""
    problems = []
    for name in CAMPAIGNS:
        c = report["campaigns"].get(name) or {}
        allowed_fail: dict[tuple[str, str], int] = {}
        allowed_clusters: dict[str, int] = {}
        allowed_protocol: dict[str, list[str]] = {}
        if name == "json":
            per_proto = (c.get("invariants") or {}).get("json.cross_command.check-acceptance", {})
            n = sum(per_proto.get(KERNEL, {}).values()) if per_proto else 0
            if n <= 0:
                problems.append("json: cross-command case count unavailable")
            ref_ts = (c.get("invariants") or {}).get("json.cross_command.transition-scenario", {}).get(REFERENCE, {})
            if ref_ts != {"pass": n, "fail": 0}:
                problems.append(f"json: reference transition-scenario cross-command check is {ref_ts}, expected {n} passes")
            allowed_fail[("json.cross_command.transition-scenario", KERNEL)] = n
            allowed_fail[("differential.exact_output", "both")] = 2 * n
            allowed_fail[("differential.envelope_answers", "both")] = ADAPTER_CASE["count"]
            for cmd in M3_COMMANDS:
                allowed_clusters[f"{cmd}: differs in <whole output>"] = n
            allowed_clusters[ADAPTER_CASE["cluster"]] = ADAPTER_CASE["count"]
            allowed_protocol[KERNEL] = sorted([f"{cmd}: {USAGE_PREFIX}" for cmd in M3_COMMANDS] +
                                              [f"{cmd}: {n} ids unanswered" for cmd in M3_COMMANDS])
            ex = (c.get("mismatch_clusters") or {}).get(ADAPTER_CASE["cluster"]) or {}
            outs = (ex.get("example") or {}).get("outputs") or {}
            if ex and not (ex.get("first_case_id") == ADAPTER_CASE["case"]
                           and ((ex.get("example") or {}).get("case") or {}).get("command") == "canonical"
                           and (outs.get(KERNEL) or {}).get("answers") == ADAPTER_CASE["kernel"]
                           and (outs.get(REFERENCE) or {}).get("answers") == ADAPTER_CASE["reference"]):
                problems.append("json: canonical envelope mismatch is not the documented adapter discrepancy")
        for inv, per in (c.get("invariants") or {}).items():
            for proto, v in per.items():
                got, want = v.get("fail", 0), allowed_fail.get((inv, proto), 0)
                if got != want:
                    problems.append(f"{name}: {inv} [{proto}] failed {got}, documented exception allows exactly {want}")
        for (inv, proto), want in allowed_fail.items():
            if want and fails(c, inv, proto) != want:
                problems.append(f"{name}: expected exactly {want} documented failures of {inv} [{proto}]")
        clusters = c.get("mismatch_clusters") or {}
        for sig, info in clusters.items():
            if info.get("count") != allowed_clusters.get(sig):
                problems.append(f"{name}: mismatch cluster {sig!r} count {info.get('count')} is not documented")
        for sig, want in allowed_clusters.items():
            if want and (clusters.get(sig) or {}).get("count") != want:
                problems.append(f"{name}: documented cluster {sig!r} missing or not exactly {want}")
        proto_problems = c.get("protocol_problems") or {}
        for proto, items in proto_problems.items():
            norm = sorted(p if not p.split(": ", 1)[-1].startswith("exit status 2: usage:")
                          else p.split(": ", 1)[0] + ": " + USAGE_PREFIX for p in items)
            norm = sorted(x.split(", e.g.")[0] for x in norm)
            if norm != allowed_protocol.get(proto, []):
                problems.append(f"{name}: protocol problems for {proto} not documented: {items[:4]}")
        for proto, want in allowed_protocol.items():
            if want and proto not in proto_problems:
                problems.append(f"{name}: expected unsupported-command protocol evidence for {proto} is missing")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists():
        a.out.unlink()
    started = datetime.now(timezone.utc).isoformat()
    proc = subprocess.run([sys.executable, str(ROOT / "m1/props/run.py"), "--campaign", ",".join(CAMPAIGNS),
                           "--profile", "ci", "--no-minimize", "--seed", str(a.seed),
                           "--rust", a.kernel, "--go", a.reference, "--out", str(a.out)], cwd=ROOT,
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    problems = []
    if proc.returncode not in (0, 1):
        problems.append(f"runner failed with exit status {proc.returncode}: {proc.stderr[-400:]}")
    if not a.out.exists():
        problems.append("runner wrote no report")
        report = None
    else:
        try:
            report = json.loads(a.out.read_text())
        except ValueError as e:
            problems.append(f"report is not valid JSON: {e}")
            report = None
    if report is not None:
        problems += structure(report, a.seed, started)
        if not problems:
            problems += reconcile(report)
            explained_fail = not report.get("ok") and proc.returncode == 1
            if proc.returncode == 0 and not report.get("ok"):
                problems.append("runner exit 0 but report not ok")
            if proc.returncode == 1 and report.get("ok"):
                problems.append("runner exit 1 but report ok")
            if explained_fail and not problems:
                print("differential: runner exit 1 fully reconciled with the documented exceptions")
    for p in problems:
        print("differential:", p, file=sys.stderr)
    counts = {n: c.get("cases_generated") for n, c in (report or {}).get("campaigns", {}).items()}
    print(f"differential (seed {a.seed}): cases {counts}; {len(problems)} unreconciled problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
