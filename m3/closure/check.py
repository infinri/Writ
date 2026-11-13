#!/usr/bin/env python3
"""M3-08 closure gate (D-037): the frozen M1 broker and transition cases against the production broker.

Runs the four frozen sets (broker-scenarios, protocol-broker, transition-scenarios,
protocol-transition; 127 cases) through a line adapter (normally
persistence/sqlite/examples/m3_line_adapter) with the primitives of the M1 parity driver
(m1/driver/parity.py, imported read-only), reads the adapter's adaptation log
(WRIT_M3_ADAPTATIONS, one JSON line per case naming the harness adaptations it used), and
classifies every case:

  exact_pass          output equals the frozen expectation and the case used no adaptation
  adapted             output equals the frozen expectation through the approved adaptations
                      listed for it (kind "adapted")
  matched_difference  an approved production difference (kind "difference"): output equals the
                      recorded actual exactly, and the differing steps and final members are
                      exactly the ones the entry names
  deferred            an approved carry-over (kind "deferred", e.g. M4 human signoff): the
                      recorded fail-closed output matched exactly; shown separately, never a pass
                      of the original expectation

Failing categories: exception_mismatch (listed, output differs from the recorded actual),
stale_exception (a difference or deferral that now passes exactly), adaptation_mismatch
(logged adaptations differ from the listed ones), unclassified (exact output reached through
adaptations no entry lists), not_driven (adapter answered "unsupported"), unexplained,
missing. Entries are validated strictly: exact case ids, a decision and reason, the exact
steps and final members, no blanket entries. Companion sets (D-037 item 4) must pass exactly
and are reported apart from the 127. The gate passes only with every failing category empty,
pinned frozen fixtures, and identical results across repeated runs.

  python3 m3/closure/check.py --proto target/release/examples/m3_line_adapter --repeat 2 --report out.json
  python3 m3/closure/check.py --proto ... --verify-report out.json   # rejects a stale report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DEFAULT_EXCEPTIONS = HERE / "exceptions.json"
DEFAULT_MANIFEST = ROOT / "m1" / "fixtures" / "manifest.json"
PASSING = ("exact_pass", "adapted", "matched_difference", "deferred")
FAILING = ("exception_mismatch", "stale_exception", "adaptation_mismatch", "unclassified",
           "not_driven", "unexplained", "missing")
CATEGORIES = PASSING + FAILING
LABELS = frozenset({
    "C1-ingress-audit", "C2-approval-sealing", "C3-synthetic-human-authority",
    "C4-partial-transaction-unavailability", "C5-injected-crash-reopen",
    "C6-controlled-effect-failure", "C7-protocol-unknown-op", "C8-protocol-input-validation",
    "seeded-preconditions", "harness-authorization-bundle", "test-authenticated-evidence",
    "sealed-execute-effect", "production-difference",
})
COMMON_KEYS = {"set", "case", "kind", "decision", "reason", "adaptations"}
KIND_KEYS = {
    "adapted": COMMON_KEYS,
    "difference": COMMON_KEYS | {"steps", "final_members", "actual"},
    "deferred": COMMON_KEYS | {"steps", "final_members", "actual", "carry_to",
                               "intended_assertion", "acceptance_criteria"},
}
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ENV_LOG = "WRIT_M3_ADAPTATIONS"


def _parity():
    sys.dont_write_bytecode = True  # never write into the frozen m1/ tree
    sys.path.insert(0, str(ROOT / "m1" / "driver"))
    import parity  # noqa: E402
    return parity


def is_unsupported(output) -> bool:
    return isinstance(output, dict) and "unsupported" in output


def differing(expect, output):
    """(differing step indexes, differing final members) between two outputs."""
    steps, members = [], []
    ws = expect.get("steps") if isinstance(expect, dict) else None
    gs = output.get("steps") if isinstance(output, dict) else None
    if isinstance(ws, list) and isinstance(gs, list):
        for i in range(max(len(ws), len(gs))):
            if i >= len(ws) or i >= len(gs) or ws[i] != gs[i]:
                steps.append(i)
    elif ws != gs:
        steps.append(-1)
    wf = expect.get("final") if isinstance(expect, dict) else None
    gf = output.get("final") if isinstance(output, dict) else None
    if isinstance(wf, dict) and isinstance(gf, dict):
        members = sorted(k for k in set(wf) | set(gf) if wf.get(k) != gf.get(k))
    elif wf != gf:
        members = ["<final>"]
    other = {k for k in (set(expect) | set(output)) - {"steps", "final"}
             if expect.get(k) != output.get(k)} if isinstance(expect, dict) and isinstance(output, dict) else set()
    members += sorted(f"<{k}>" for k in other)
    return steps, members


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


def validate_entries(sets: dict, entries: list) -> tuple[dict, list]:
    """Strictly validate exceptions.json entries; returns ({(set, case): entry}, problems)."""
    cases = {(s, c["id"]): c for s, cs in sets.items() for c in cs}
    listed, problems = {}, []
    for n, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"entry {n} is not an object")
            continue
        key = (e.get("set"), e.get("case"))
        where = f"entry {n} {key}"
        kind = e.get("kind")
        if kind not in KIND_KEYS:
            problems.append(f"{where}: unknown kind {kind!r}")
            continue
        if set(e) != KIND_KEYS[kind]:
            problems.append(f"{where}: members {sorted(set(e) ^ KIND_KEYS[kind])} wrong for kind {kind}")
            continue
        if not isinstance(e["case"], str) or not CASE_ID.match(e["case"]):
            problems.append(f"{where}: case must be one exact case id (no patterns)")
            continue
        if key in listed:
            problems.append(f"{where}: duplicate entry")
            continue
        if key not in cases:
            problems.append(f"{where}: names no case of its set")
            continue
        for k in ("decision", "reason"):
            if not isinstance(e[k], str) or not e[k].strip():
                problems.append(f"{where}: empty {k}")
        ad = e["adaptations"]
        if not isinstance(ad, list) or len(set(ad)) != len(ad) or any(a not in LABELS for a in ad):
            problems.append(f"{where}: adaptations must be a list of distinct known labels")
        if kind == "adapted":
            if not ad:
                problems.append(f"{where}: an adapted entry must name its adaptations")
        else:
            actual = e["actual"]
            if not isinstance(actual, dict) or is_unsupported(actual):
                problems.append(f"{where}: actual must be a driven output object")
            else:
                steps, members = differing(cases[key]["expect"], actual)
                if not steps and not members:
                    problems.append(f"{where}: actual equals the frozen expectation (not a difference)")
                if e["steps"] != steps:
                    problems.append(f"{where}: steps {e['steps']} but the actual differs at {steps}")
                if e["final_members"] != members:
                    problems.append(f"{where}: final_members {e['final_members']} but the actual differs in {members}")
            if kind == "deferred":
                for k in ("carry_to", "intended_assertion", "acceptance_criteria"):
                    if not isinstance(e[k], str) or not e[k].strip():
                        problems.append(f"{where}: empty {k}")
        listed[key] = e
    return listed, problems


def classify(sets: dict, outputs: dict, entries: list, compare, adaptations: dict | None) -> dict:
    """Classify every case. `sets` maps set name to its case list, `outputs` maps set name to
    {case id: output}, `entries` is the exceptions.json list, `compare` is the driver's
    compare(case, output) -> list of differences, `adaptations` maps case id to the logged
    label list (None: the adapter logged nothing, which fails every case)."""
    listed, problems = validate_entries(sets, entries)
    report = {c: [] for c in CATEGORIES}
    for set_name, cases in sets.items():
        got = outputs.get(set_name, {})
        for case in cases:
            cid = case["id"]
            entry = {"set": set_name, "case": cid}
            exc = listed.get((set_name, cid))
            if exc is not None:
                entry["kind"], entry["decision"] = exc["kind"], exc["decision"]
            if cid not in got:
                report["missing"].append(entry)
                continue
            out = got[cid]
            logged = None if adaptations is None else adaptations.get(cid)
            if logged is None:
                entry["cause"] = "no adaptation log line for this case"
                report["unclassified"].append(entry)
                continue
            entry["adaptations"] = sorted(logged)
            if is_unsupported(out):
                entry["cause"] = one_line(case, out)
                report["not_driven"].append(entry)
                continue
            if exc is not None and sorted(exc["adaptations"]) != sorted(logged):
                entry["cause"] = f"logged adaptations {sorted(logged)} != listed {sorted(exc['adaptations'])}"
                report["adaptation_mismatch"].append(entry)
                continue
            if not compare(case, out):
                if exc is None:
                    if logged:
                        entry["cause"] = "exact output through adaptations that no entry lists"
                        report["unclassified"].append(entry)
                    else:
                        report["exact_pass"].append(entry)
                elif exc["kind"] == "adapted":
                    report["adapted"].append(entry)
                else:
                    report["stale_exception"].append(entry)
                continue
            entry["cause"] = one_line(case, out)
            if exc is None:
                entry["actual"] = out
                report["unexplained"].append(entry)
            elif exc["kind"] != "adapted" and out == exc["actual"]:
                if exc["kind"] == "deferred":
                    entry["carry_to"] = exc["carry_to"]
                    report["deferred"].append(entry)
                else:
                    entry["steps"], entry["final_members"] = exc["steps"], exc["final_members"]
                    report["matched_difference"].append(entry)
            else:
                entry["recorded_actual"] = exc.get("actual")
                entry["actual"] = out
                report["exception_mismatch"].append(entry)
    counts = {c: len(report[c]) for c in CATEGORIES}
    report["problems"] = problems
    report["counts"] = counts
    report["cases"] = sum(len(c) for c in sets.values())
    report["ok"] = not problems and all(counts[c] == 0 for c in FAILING) and sum(
        counts.values()) == report["cases"]
    return report


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_fixtures(spec: dict, manifest: dict, base: Path) -> list:
    """Problems if a set is missing from the manifest or its file digest changed."""
    problems = []
    by_name = {s["name"]: s for s in manifest["sets"]}
    if set(spec["fixture_sha256"]) != set(spec["sets"]):
        problems.append("fixture_sha256 must pin exactly the gated sets")
    for name, want in spec["fixture_sha256"].items():
        s = by_name.get(name)
        if s is None:
            problems.append(f"set {name} not in manifest")
            continue
        got = sha256(base / s["file"])
        if got != want:
            problems.append(f"fixture {s['file']} changed: {got} != recorded {want}")
    return problems


def read_log(path: Path) -> tuple[dict, list]:
    found, problems = {}, []
    if not path.exists():
        return found, ["adapter wrote no adaptation log"]
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
            cid, labels = line["case"], line["adaptations"]
        except (ValueError, KeyError, TypeError):
            problems.append(f"adaptation log line {n} malformed")
            continue
        if cid in found:
            problems.append(f"adaptation log names {cid} twice")
        if not isinstance(labels, list) or any(a not in LABELS for a in labels):
            problems.append(f"adaptation log line {n} has unknown labels")
            labels = [a for a in labels if a in LABELS] if isinstance(labels, list) else []
        found[cid] = sorted(labels)
    return found, problems


def invoke_set(parity, proto: list, command: str, cases: list, timeout: float):
    ids = [c["id"] for c in cases]
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "adaptations.jsonl"
        old = os.environ.get(ENV_LOG)
        os.environ[ENV_LOG] = str(log)
        try:
            r = parity.invoke(proto, command, parity.request_lines(cases), timeout)
        finally:
            if old is None:
                os.environ.pop(ENV_LOG, None)
            else:
                os.environ[ENV_LOG] = old
        found, problems = read_log(log)
    if r["error"]:
        problems.append(r["error"])
    resp, perr = parity.parse_responses(r["stdout"], ids)
    return resp, found, problems + perr


def run(proto: list, sets_spec: list, base: Path, repeat: int, timeout: float):
    """Run each (name, file, command) set `repeat` times; outputs and logs must agree."""
    parity = _parity()
    sets, outputs, logs, problems = {}, {}, {}, []
    for name, file, command in sets_spec:
        cases = parity.load_cases(base / file)
        first = None
        for n in range(max(1, repeat)):
            resp, found, perr = invoke_set(parity, proto, command, cases, timeout)
            problems.extend(f"{name}: {p}" for p in perr)
            if first is None:
                first = (resp, found)
                continue
            varying = {c["id"] for c in cases if c.get("compare") == "one_of"}
            for c in cases:
                cid = c["id"]
                if cid not in varying and resp.get(cid) != first[0].get(cid):
                    problems.append(f"{name}: {cid} output nondeterministic on run {n + 1}")
                if found.get(cid) != first[1].get(cid):
                    problems.append(f"{name}: {cid} adaptations nondeterministic on run {n + 1}")
        sets[name], outputs[name] = cases, first[0]
        logs.update({cid: first[1][cid] for cid in first[1] if cid in {c["id"] for c in cases}})
    return sets, outputs, logs, problems, parity.compare


def input_digests(args, spec: dict) -> dict:
    d = {"exceptions": sha256(args.exceptions), "manifest": sha256(args.manifest),
         "fixtures": dict(spec["fixture_sha256"]), "gate": sha256(Path(__file__).resolve())}
    binary = Path(args.proto.split()[0])
    d["adapter"] = sha256(binary) if binary.is_file() else None
    d["companions"] = {c["name"]: sha256(HERE / c["file"]) for c in spec.get("companions", [])}
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proto", required=True, help="adapter command line")
    ap.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--verify-report", type=Path, help="check a stored report against the current inputs")
    args = ap.parse_args(argv)
    spec = json.loads(args.exceptions.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.verify_report:
        stored = json.loads(args.verify_report.read_text(encoding="utf-8"))
        stale = stored.get("inputs") != input_digests(args, spec)
        ok = bool(stored.get("ok")) and not stale
        print("report " + ("current and passing" if ok else "STALE" if stale else "not passing"))
        return 0 if ok else 1
    base = args.manifest.parent
    problems = verify_fixtures(spec, manifest, base)
    by_name = {s["name"]: s for s in manifest["sets"]}
    frozen = [(n, by_name[n]["file"], by_name[n]["command"]) for n in spec["sets"]]
    sets, outputs, logs, run_problems, compare = run(args.proto.split(), frozen, base, args.repeat, args.timeout)
    report = classify(sets, outputs, spec["exceptions"], compare, logs)
    report["problems"] = problems + run_problems + report["problems"]
    comp = [(c["name"], c["file"], c["command"]) for c in spec.get("companions", [])]
    report["companions"] = {"exact_pass": [], "failing": []}
    if comp:
        kinds = {c["name"]: c["kind"] for c in spec["companions"]}
        csets, couts, _, cprob, _ = run(args.proto.split(), comp, HERE, args.repeat, args.timeout)
        report["problems"] += cprob
        for name, cases in csets.items():
            for c in cases:
                out = couts[name].get(c["id"])
                ok = out is not None and not compare(c, out)
                meta = c.get("meta", {})
                item = {"set": name, "case": c["id"], "kind": kinds[name],
                        "companion_of": meta.get("companion_of") or meta.get("derived_from")}
                if not ok:
                    item["cause"] = "missing" if out is None else one_line(c, out)
                if kinds[name] == "mechanical-companion":
                    labelled = c["id"].startswith("m3c-") and meta.get("companion_of") and \
                        "not human-signoff coverage" in meta.get("label", "")
                elif kinds[name] == "seed-negative":
                    labelled = c["id"].startswith("m3n-") and meta.get("derived_from") and \
                        set(c["expect"]) == {"refused_setup"}
                else:
                    labelled = False
                if not labelled:
                    item["cause"] = f"companion case does not meet the {kinds[name]} labelling rules"
                    ok = False
                report["companions"]["exact_pass" if ok else "failing"].append(item)
    report["ok"] = report["ok"] and not report["problems"] and not report["companions"]["failing"]
    report["inputs"] = input_digests(args, spec)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    c = report["counts"]
    print(f"cases {report['cases']}: exact {c['exact_pass']}, adapted {c['adapted']}, approved differences "
          f"{c['matched_difference']}, deferred (carry-over) {c['deferred']}; failing: "
          + ", ".join(f"{k} {c[k]}" for k in FAILING))
    for e in report["deferred"]:
        print(f"  deferred to {e['carry_to']}: {e['case']}")
    for cat in FAILING:
        for e in report[cat]:
            print(f"  {cat}: {e['case']}: {e.get('cause', '')}")
    cs = report["companions"]
    print(f"companions: exact {len(cs['exact_pass'])}, failing {len(cs['failing'])}")
    for e in cs["failing"]:
        print(f"  companion failing: {e['case']}: {e.get('cause', '')}")
    for p in report["problems"]:
        print(f"  problem: {p}")
    print("GATE PASS (scoped M3; not 127/127 exact parity)" if report["ok"] else "GATE FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
