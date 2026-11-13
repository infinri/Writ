#!/usr/bin/env python3
"""M3-08 closure gate: the frozen M1 broker and transition cases against the production broker.

Runs the four frozen sets (broker-scenarios, protocol-broker, transition-scenarios,
protocol-transition; 127 cases) through a line adapter (normally
persistence/sqlite/examples/m3_line_adapter.rs) with the primitives of the M1 parity
driver (m1/driver/parity.py, imported read-only), and classifies every case:

  exact_pass          output equals the frozen expectation (the driver's compare)
  matched_exception   listed in exceptions.json and the output equals its recorded actual
  exception_mismatch  listed, but the output differs from the recorded actual (FAIL)
  stale_exception     listed, but the case now passes exactly (reported; FAIL until removed)
  not_driven          the adapter answered {"unsupported": ...} (FAIL; approved ones are marked)
  unexplained         differs from the expectation and is not listed (FAIL)
  missing             no answer for the case (FAIL)

The gate passes only when every case is an exact pass or a matched exception, the fixture
files still have their recorded digests (frozen expectations never change), every listed
exception names a case of its set, and repeated runs agree. Standard library only.

  python3 m3/closure/check.py --proto target/release/examples/m3_line_adapter --report out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parent / "exceptions.json"
DEFAULT_MANIFEST = ROOT / "m1" / "fixtures" / "manifest.json"
FAILING = ("exception_mismatch", "stale_exception", "not_driven", "unexplained", "missing")
CATEGORIES = ("exact_pass", "matched_exception") + FAILING


def _parity():
    sys.dont_write_bytecode = True  # never write into the frozen m1/ tree
    sys.path.insert(0, str(ROOT / "m1" / "driver"))
    import parity  # noqa: E402
    return parity


def is_unsupported(output) -> bool:
    return isinstance(output, dict) and "unsupported" in output


def one_line(case: dict, output) -> str:
    """A one-line cause: the first differing step or final member."""
    if is_unsupported(output):
        u = output["unsupported"]
        return f"unsupported at step {u.get('step')} ({u.get('op')}): {u.get('reason')}" + (
            f" [{u['production_refusal']}]" if u.get("production_refusal") else "")
    want = case["expect"]
    if case.get("compare") == "one_of":
        return "matches none of the acceptable outputs"
    if not isinstance(output, dict):
        return "output is not an object"
    ws, gs = want.get("steps"), output.get("steps")
    if isinstance(ws, list) and isinstance(gs, list):
        for i, (w, g) in enumerate(zip(ws, gs)):
            if w != g:
                return f"step {i}: got {json.dumps(g, sort_keys=True)}, want {json.dumps(w, sort_keys=True)}"
        if len(ws) != len(gs):
            return f"{len(gs)} steps, want {len(ws)}"
    wf, gf = want.get("final"), output.get("final")
    if isinstance(wf, dict) and isinstance(gf, dict):
        for k in sorted(set(wf) | set(gf)):
            if wf.get(k) != gf.get(k):
                return f"final.{k}: got {json.dumps(gf.get(k), sort_keys=True)}, want {json.dumps(wf.get(k), sort_keys=True)}"
    return f"got {json.dumps(output, sort_keys=True)[:160]}"


def classify(sets: dict, outputs: dict, exceptions: list, compare) -> dict:
    """Classify every case. `sets` maps set name to its case list, `outputs` maps set name
    to {case id: output}, `exceptions` is the list from exceptions.json, `compare` is the
    driver's compare(case, output) -> list of differences."""
    listed = {}
    problems = []
    for e in exceptions:
        key = (e.get("set"), e.get("case"))
        if key in listed:
            problems.append(f"duplicate exception {key}")
        listed[key] = e
    known = {(s, c["id"]) for s, cases in sets.items() for c in cases}
    for key in listed:
        if key not in known:
            problems.append(f"exception names no case of its set: {key}")
    report = {c: [] for c in CATEGORIES}
    for set_name, cases in sets.items():
        got = outputs.get(set_name, {})
        for case in cases:
            cid = case["id"]
            entry = {"set": set_name, "case": cid}
            exc = listed.get((set_name, cid))
            if exc is not None:
                entry["decision"] = exc.get("decision")
            if cid not in got:
                report["missing"].append(entry)
                continue
            out = got[cid]
            if not compare(case, out):
                report["stale_exception" if exc is not None else "exact_pass"].append(entry)
                continue
            entry["cause"] = one_line(case, out)
            if exc is not None:
                if exc.get("actual") is not None and out == exc["actual"]:
                    report["matched_exception"].append(entry)
                elif exc.get("actual") is None and is_unsupported(out):
                    entry["approved"] = True
                    report["not_driven"].append(entry)
                else:
                    entry["recorded_actual"] = exc.get("actual")
                    entry["actual"] = out
                    report["exception_mismatch"].append(entry)
            elif is_unsupported(out):
                report["not_driven"].append(entry)
            else:
                entry["actual"] = out
                report["unexplained"].append(entry)
    counts = {c: len(report[c]) for c in CATEGORIES}
    report["problems"] = problems
    report["counts"] = counts
    report["cases"] = sum(len(c) for c in sets.values())
    report["ok"] = not problems and all(counts[c] == 0 for c in FAILING)
    return report


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_fixtures(spec: dict, manifest: dict, base: Path) -> list:
    """Problems if a set is missing from the manifest or its file digest changed."""
    problems = []
    by_name = {s["name"]: s for s in manifest["sets"]}
    for name, want in spec["fixture_sha256"].items():
        s = by_name.get(name)
        if s is None:
            problems.append(f"set {name} not in manifest")
            continue
        got = sha256(base / s["file"])
        if got != want:
            problems.append(f"fixture {s['file']} changed: {got} != recorded {want}")
    return problems


def run(proto: list, spec: dict, manifest: dict, base: Path, repeat: int, timeout: float):
    parity = _parity()
    by_name = {s["name"]: s for s in manifest["sets"]}
    sets, outputs, problems = {}, {}, []
    for name in spec["sets"]:
        s = by_name[name]
        cases = parity.load_cases(base / s["file"])
        ids = [c["id"] for c in cases]
        payload = parity.request_lines(cases)
        first = None
        for n in range(max(1, repeat)):
            r = parity.invoke(proto, s["command"], payload, timeout)
            if r["error"]:
                problems.append(f"{name}: {r['error']}")
            resp, perr = parity.parse_responses(r["stdout"], ids)
            problems.extend(f"{name}: {p}" for p in perr)
            if first is None:
                first = resp
            else:
                varying = {c["id"] for c in cases if c.get("compare") == "one_of"}
                for cid in ids:
                    if cid not in varying and resp.get(cid) != first.get(cid):
                        problems.append(f"{name}: {cid} nondeterministic on run {n + 1}")
        sets[name], outputs[name] = cases, first or {}
    return sets, outputs, problems, parity.compare


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proto", required=True, help="adapter command line")
    ap.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    spec = json.loads(args.exceptions.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    base = args.manifest.parent
    problems = verify_fixtures(spec, manifest, base)
    sets, outputs, run_problems, compare = run(args.proto.split(), spec, manifest, base, args.repeat, args.timeout)
    report = classify(sets, outputs, spec["exceptions"], compare)
    report["problems"] = problems + run_problems + report["problems"]
    report["ok"] = report["ok"] and not report["problems"]
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    c = report["counts"]
    print(f"cases {report['cases']}: exact {c['exact_pass']}, matched exceptions {c['matched_exception']}, "
          f"not driven {c['not_driven']}, unexplained {c['unexplained']}, exception mismatches "
          f"{c['exception_mismatch']}, stale exceptions {c['stale_exception']}, missing {c['missing']}")
    for cat in ("unexplained", "exception_mismatch", "stale_exception", "missing", "not_driven"):
        for e in report[cat]:
            mark = " (approved exception, not driven)" if e.get("approved") else ""
            print(f"  {cat}: {e['case']}{mark}: {e.get('cause', '')}")
    for p in report["problems"]:
        print(f"  problem: {p}")
    print("GATE PASS" if report["ok"] else "GATE FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
