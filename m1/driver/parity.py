#!/usr/bin/env python3
"""M1-01 language-neutral parity driver.

Runs one prototype executable against fixture sets over a JSON Lines protocol
and records exactly what matched. Standard library only; it never imports a
prototype or the M0 oracle, so the same driver judges Rust, Go and any
reference adapter identically.

Protocol (see docs/implementation/M1-PARITY-SUITE.md, "Prototype interface"):
  invocation   <proto argv...> <command>
  stdin        one JSON object per line: {"id": str, "input": any} or
               {"id": str, "input_text": str}  (raw JSON text the prototype must parse)
  stdout       one JSON object per input line: {"id": str, "output": object}
  exit status  0 when every line was answered (a refusal is an output, not a crash)

Fixture set file (JSON Lines): {"id", "input" | "input_text", "expect": object,
  "compare": "exact" | "fields" | "one_of", "fields": [..] (for "fields"), "meta": {...}}.
  one_of: expect is {"one_of": [output, ...]}; any listed output passes (serialization orders).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DRIVER_VERSION = "m1-parity-driver/1"
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_MISMATCH_DETAILS = 50
_MISSING = object()


class FixtureError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path) -> list[dict]:
    cases, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        cid = case.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise FixtureError(f"{path}:{n}: missing or duplicate id {cid!r}")
        if ("input" in case) == ("input_text" in case):
            raise FixtureError(f"{path}:{n}: exactly one of input, input_text required")
        if case.get("compare", "exact") not in ("exact", "fields", "one_of"):
            raise FixtureError(f"{path}:{n}: unknown compare mode")
        if case.get("compare") == "fields" and not case.get("fields"):
            raise FixtureError(f"{path}:{n}: fields compare needs a non-empty fields list")
        if not isinstance(case.get("expect"), dict):
            raise FixtureError(f"{path}:{n}: expect must be an object")
        if case.get("compare") == "one_of" and not (isinstance(case["expect"].get("one_of"), list)
                                                    and case["expect"]["one_of"]):
            raise FixtureError(f"{path}:{n}: one_of compare needs a non-empty expect.one_of list")
        seen.add(cid)
        cases.append(case)
    if not cases:
        raise FixtureError(f"{path}: no cases")
    return cases


def request_lines(cases: list[dict]) -> bytes:
    out = []
    for c in cases:
        line = {"id": c["id"]}
        if "input" in c:
            line["input"] = c["input"]
        else:
            line["input_text"] = c["input_text"]
        out.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(out) + "\n").encode("utf-8")


def compare(case: dict, output) -> list[str]:
    expect = case["expect"]
    if not isinstance(output, dict):
        return ["output is not an object"]
    if case.get("compare", "exact") == "exact":
        return [] if output == expect else ["output differs from expect"]
    if case["compare"] == "one_of":
        return [] if any(output == alt for alt in expect["one_of"]) else ["output matches none of the acceptable outputs"]
    diffs = []
    for field in case["fields"]:
        got, want = output.get(field, _MISSING), expect.get(field, _MISSING)
        if got != want:
            diffs.append(f"{field}: got {'<missing>' if got is _MISSING else json.dumps(got)}, "
                         f"want {'<missing>' if want is _MISSING else json.dumps(want)}")
    return diffs


def invoke(proto: list[str], command: str, payload: bytes, timeout: float) -> dict:
    started = time.perf_counter()
    try:
        proc = subprocess.run([*proto, command], input=payload, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s", "stdout": b"", "elapsed_s": timeout}
    except OSError as exc:
        return {"error": f"cannot start prototype: {exc}", "stdout": b"", "elapsed_s": 0.0}
    elapsed = time.perf_counter() - started
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        return {"error": "output exceeds limit", "stdout": b"", "elapsed_s": elapsed}
    err = None if proc.returncode == 0 else f"exit status {proc.returncode}: {proc.stderr[-500:].decode('utf-8', 'replace')}"
    return {"error": err, "stdout": proc.stdout, "elapsed_s": elapsed}


def parse_responses(stdout: bytes, case_ids: list[str]) -> tuple[dict, list[str]]:
    responses, problems = {}, []
    expected = set(case_ids)
    for n, raw in enumerate(stdout.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            problems.append(f"line {n}: not JSON")
            continue
        rid = msg.get("id") if isinstance(msg, dict) else None
        if rid not in expected:
            problems.append(f"line {n}: unknown id {rid!r}")
        elif rid in responses:
            problems.append(f"line {n}: duplicate id {rid!r}")
        elif "output" not in msg:
            problems.append(f"line {n}: no output for {rid!r}")
        else:
            responses[rid] = msg["output"]
    for cid in case_ids:
        if cid not in responses:
            problems.append(f"no response for {cid!r}")
    return responses, problems


def run_set(proto: list[str], spec: dict, base: Path, timeout: float, repeat: int) -> dict:
    path = (base / spec["file"]).resolve()
    cases = load_cases(path)
    ids = [c["id"] for c in cases]
    payload = request_lines(cases)
    runs = [invoke(proto, spec["command"], payload, timeout) for _ in range(repeat)]
    first = runs[0]
    result = {"name": spec["name"], "command": spec["command"], "file": spec["file"],
              "file_sha256": sha256_file(path), "hard_gate": bool(spec.get("hard_gate", True)),
              "cases": len(cases), "passed": 0, "failed": 0, "protocol_errors": [],
              "nondeterministic": False, "elapsed_s": [round(r["elapsed_s"], 6) for r in runs],
              "mismatches": []}
    if first["error"]:
        result["protocol_errors"].append(first["error"])
    if repeat > 1 and any(r["stdout"] != first["stdout"] or r["error"] != first["error"] for r in runs[1:]):
        result["nondeterministic"] = True
    responses, problems = parse_responses(first["stdout"], ids)
    result["protocol_errors"].extend(problems)
    for case in cases:
        if case["id"] not in responses:
            result["failed"] += 1
            continue
        diffs = compare(case, responses[case["id"]])
        if diffs:
            result["failed"] += 1
            if len(result["mismatches"]) < MAX_MISMATCH_DETAILS:
                result["mismatches"].append({"id": case["id"], "diffs": diffs})
        else:
            result["passed"] += 1
    result["ok"] = (result["failed"] == 0 and not result["protocol_errors"]
                    and not result["nondeterministic"])
    return result


def git_head(start: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=start, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=start,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "") if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def executable_digest(argv0: str) -> str | None:
    found = shutil.which(argv0)
    return sha256_file(Path(found)) if found else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proto", required=True, help="prototype command line, for example 'm1/rust/target/release/proto'")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--sets", help="comma-separated set names (default: all)")
    ap.add_argument("--out", type=Path, help="write the JSON result here")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per set invocation")
    ap.add_argument("--repeat", type=int, default=1, help="invoke each set N times and require identical output")
    args = ap.parse_args(argv)

    proto = args.proto.split()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    wanted = set(args.sets.split(",")) if args.sets else None
    specs = [s for s in manifest["sets"] if wanted is None or s["name"] in wanted]
    if wanted and wanted - {s["name"] for s in specs}:
        print(f"unknown sets: {sorted(wanted - {s['name'] for s in specs})}", file=sys.stderr)
        return 2
    started = datetime.now(timezone.utc).isoformat()
    try:
        sets = [run_set(proto, s, args.manifest.parent, args.timeout, max(1, args.repeat)) for s in specs]
    except (FixtureError, json.JSONDecodeError, OSError) as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 2
    report = {
        "driver": DRIVER_VERSION,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "repository_commit": git_head(args.manifest.parent),
        "host": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version()},
        "proto": {"argv": proto, "executable_sha256": executable_digest(proto[0])},
        "manifest": str(args.manifest), "manifest_sha256": sha256_file(args.manifest),
        "repeat": max(1, args.repeat),
        "sets": sets,
        "summary": {"sets": len(sets), "ok": all(s["ok"] for s in sets),
                    "hard_gates_ok": all(s["ok"] for s in sets if s["hard_gate"]),
                    "cases": sum(s["cases"] for s in sets), "passed": sum(s["passed"] for s in sets),
                    "failed": sum(s["failed"] for s in sets)},
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    for s in sets:
        flag = "PASS" if s["ok"] else "FAIL"
        print(f"{flag} {s['name']}: {s['passed']}/{s['cases']} passed"
              + (f", {len(s['protocol_errors'])} protocol errors" if s["protocol_errors"] else "")
              + (", NONDETERMINISTIC" if s["nondeterministic"] else ""))
    print("summary:", json.dumps(report["summary"]))
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
