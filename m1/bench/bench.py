#!/usr/bin/env python3
"""M1-06 benchmark harness (M1-06a: harness only; see README).

  # plumbing check, any environment: runs every workload on tiny inputs,
  # checks correctness, records the environment, and writes NO timing or
  # memory values (result marked not_a_benchmark: true)
  python3 m1/bench/bench.py --dry-run --seed 20261107 \
      --rust m1/rust/target/release/proto --go m1/go/proto --out dry.json

  # measurement per the M1-PARITY-SUITE protocol; refuses to start unless the
  # guard passes (mains power, performance governor, quiet, cool, clean m1/)
  python3 m1/bench/bench.py --seed <seed> --rust ... --go ... --out run.json

Standard library only. Exit 0: done and every correctness check passed;
2: usage; 3: guard refused (nothing written); 4: a correctness check failed
(result written, marked invalid for comparison).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from wbench import envinfo, runner, stats, workloads as wl  # noqa: E402
from wbench.paths import PROPS  # noqa: E402,F401  (puts m1/props on sys.path)
from wprops import c_broker  # noqa: E402
from wprops.common import Results  # noqa: E402

FORMAT = "writ-m1-bench/1"
BENCHMARK_VERSION = "v2-0.1"
PROFILES = {
    "dry-run": {"a_warmup": 5, "a_n": 20, "a_runs": 2, "b_sessions": 3, "b_runs": 2, "g_n": 12, "g_runs": 2,
                "h_n": 8, "h_runs": 2, "cold": 3, "conc_levels": [1, 2], "conc_total": 8},
    "protocol": {"a_warmup": 1000, "a_n": 10000, "a_runs": 10, "b_sessions": 100, "b_runs": 10, "g_n": 1000,
                 "g_runs": 5, "h_n": 1000, "h_runs": 5, "cold": 30, "conc_levels": [1, 10, 100, 1000],
                 "conc_total": 10000},
}


class Check:
    """Correctness bookkeeping; any failure invalidates the run for comparison."""

    def __init__(self):
        self.counts = Counter()
        self.examples = []

    def fail(self, kind: str, detail):
        self.counts[kind] += 1
        if len(self.examples) < 20:
            self.examples.append({"kind": kind, "detail": detail})

    def ok(self, kind: str):
        self.counts.setdefault(kind, 0)

    def failed(self) -> int:
        return sum(self.counts.values())


def order(protos: list, run: int) -> list:
    """Alternate which prototype runs first in each repetition."""
    return protos if run % 2 == 0 else protos[::-1]


def outputs_of(proto, command, path, check: Check, expect_ids: set, expect_nulls: int) -> dict:
    r = runner.run_batch(proto, command, path)
    out, nulls, problems = runner.parse(r["stdout"])
    if r["returncode"] != 0:
        check.fail("nonzero_exit", {"proto": proto.name, "command": command, "stderr": r["stderr"].decode("utf-8", "replace")})
    for p in problems:
        check.fail("protocol_problem", {"proto": proto.name, "problem": p})
    if set(out) != expect_ids or nulls != expect_nulls:
        check.fail("answers_incomplete", {"proto": proto.name, "missing": len(expect_ids - set(out)),
                                          "unknown": len(set(out) - expect_ids), "null_ids": nulls})
    return {"out": out, "wall_s": r["wall_s"]}


def ids_of(lines: list[bytes]) -> tuple[set, int]:
    ids, nulls = set(), 0
    for ln in lines:
        try:
            ids.add(json.loads(ln)["id"])
        except ValueError:
            nulls += 1
    return ids, nulls


def differential(name: str, a: dict, b: dict, check: Check):
    for cid in sorted(set(a) | set(b)):
        if a.get(cid) != b.get(cid):
            check.fail(f"{name}.differential", {"id": cid})
    check.ok(f"{name}.differential")


# ---------------------------------------------------------------- workloads

def workload_a(protos, prof, seed, tmp: Path, check: Check, samples: dict) -> dict:
    _, warm, _ = wl.gen_a(seed + 1, prof["a_warmup"], prefix="w")
    command, lines, meta = wl.gen_a(seed, prof["a_n"])
    all_lines = warm + lines
    path = tmp / "a.jsonl"
    wl.write_lines(path, all_lines)
    ids, nulls = ids_of(all_lines)
    ref = {p.name: outputs_of(p, command, path, check, ids, nulls)["out"] for p in protos}
    differential("A", *ref.values(), check)
    measured_ids = ids_of(lines)[0]
    per = {p.name: {"kernel_ns": [], "batch_wall_s": [], "per_run_p50_ns": []} for p in protos}
    for run in range(prof["a_runs"]):
        for p in order(protos, run):
            timed = outputs_of(p, "bench-decide", path, check, ids, nulls)
            ns = []
            for cid, o in timed["out"].items():
                if not isinstance(o, dict) or o.get("decision") != ref[p.name].get(cid) or not isinstance(o.get("kernel_ns"), int):
                    check.fail("A.bench_decide_equals_decide", {"proto": p.name, "id": cid})
                elif cid in measured_ids:
                    ns.append(o["kernel_ns"])
            per[p.name]["kernel_ns"].extend(ns)
            per[p.name]["per_run_p50_ns"].append(stats.percentile(sorted(ns), 50))
            per[p.name]["batch_wall_s"].append(outputs_of(p, command, path, check, ids, nulls)["wall_s"])
    check.ok("A.bench_decide_equals_decide")
    meas = {}
    for p in protos:
        d = per[p.name]
        samples[f"A.{p.name}"] = d
        n_lines = len(all_lines)
        meas[p.name] = {
            "kernel_decision_ns": stats.summarize(d["kernel_ns"]),
            "kernel_p50_run_to_run": stats.run_to_run([v for v in d["per_run_p50_ns"] if v is not None]),
            "batch_wall_s_incl_start_and_warmup": stats.summarize(d["batch_wall_s"]),
            "throughput_per_sec": (n_lines * len(d["batch_wall_s"]) / sum(d["batch_wall_s"])) if d["batch_wall_s"] else None,
            "peak_rss_kb": runner.peak_rss(p, command, path, tmp)["kb"],
        }
    return {"command": command, "seed": seed, "warmup_seed": seed + 1, "warmup": prof["a_warmup"],
            "decisions_per_run": prof["a_n"], "runs": prof["a_runs"], "digest": wl.digest_lines(all_lines),
            "meta": meta, "measurements": meas,
            "method": "kernel_decision_ns: per-decision time inside the prototype (bench-decide), excluding JSON "
                      "parsing, encoding and I/O; batch wall: process launch to exit over warm-up plus measured lines"}


class Ledger:
    """Command line per invocation: the in-memory ledger, or a fresh SQLite
    database directory for every single prototype process (D-023)."""

    def __init__(self, kind: str | None, tmp: Path, name: str, profile: str | None = None):
        self.kind, self.root, self.n, self.profile = kind, tmp / "ledgers" / name, 0, profile

    def cmd(self, base: str, proto) -> str:
        opt = f" --concurrency-profile {self.profile}" if self.profile else ""  # D-027
        if self.kind != "sqlite":
            return base + opt
        self.n += 1
        d = self.root / f"{proto.name}-{self.n}"
        d.mkdir(parents=True)
        return f"{base} --ledger sqlite --ledger-dir {d}{opt}"


def collect_timing(timing: dict, acc: dict) -> int:
    """Flatten one bench-broker-scenario timing object into sample lists
    (LEDGER-SQLITE.md "Per-operation timing"); returns parse_ns + run_ns."""
    def add(k, v):
        if isinstance(v, int):
            acc.setdefault(k, []).append(v)

    def step(s, prefix):
        op = s.get("op", "unknown")
        if op == "concurrent":
            add(f"{prefix}concurrent.total_ns", s.get("total_ns"))
            add(f"{prefix}concurrent.ready_ns", s.get("ready_ns"))  # D-028: connection-preparation wall time
            add(f"{prefix}concurrent.start_skew_ns", s.get("start_skew_ns"))
            for o in s.get("operations") or []:
                add("concurrent.worker.setup_ns", o.get("setup_ns"))
                add("concurrent.worker.release_delay_ns", o.get("release_delay_ns"))
                step(o, "concurrent.")
            return
        for k in ("total_ns", "validate_ns", "decide_ns", "key_lock_wait_ns"):
            add(f"{prefix}op.{op}.{k}", s.get(k))
        for tname, tx in (s.get("tx") or {}).items():
            for k in ("begin_ns", "body_ns", "end_ns"):
                add(f"{prefix}tx.{tname}.{k}", tx.get(k))
    for k in ("parse_ns", "open_ns", "final_ns", "run_ns"):
        add(f"line.{k}", timing.get(k))
    for s in timing.get("steps") or []:
        step(s, "")
    return (timing.get("parse_ns") or 0) + (timing.get("run_ns") or 0)


def timed_broker_runs(name, protos, command, path, led, runs, check, ids, nulls, verify) -> dict:
    """Runs through bench-broker-scenario; `verify(proto, scenario_outputs)` applies the
    workload's correctness checks to the unwrapped scenario outputs. Protocol and process
    overhead per run = process wall time minus the in-process parse_ns + run_ns of all lines."""
    acc = {p.name: {} for p in protos}
    walls = {p.name: [] for p in protos}
    for run in range(runs):
        for p in order(protos, run):
            r = outputs_of(p, led.cmd("bench-" + command, p), path, check, ids, nulls)
            walls[p.name].append(r["wall_s"])
            scen, tims, inproc = {}, {}, 0
            for cid, o in r["out"].items():
                if not isinstance(o, dict) or set(o) != {"scenario", "timing"}:
                    check.fail(f"{name}.timed_output_shape", {"proto": p.name, "id": cid})
                    continue
                scen[cid], tims[cid] = o["scenario"], o["timing"]
                inproc += collect_timing(o["timing"], acc[p.name])
            verify(p, scen, tims, run)
            acc[p.name].setdefault("process.overhead_ns", []).append(int(r["wall_s"] * 1e9) - inproc)
    check.ok(f"{name}.timed_output_shape")
    return {pn: {k: stats.summarize(v) for k, v in sorted(a.items())} for pn, a in acc.items()}, walls


def workload_b(protos, prof, seed, tmp: Path, check: Check, samples: dict, ledger: str | None = "sqlite",
               name: str = "B", profile: str = "primary") -> dict:
    """Every run goes through bench-broker-scenario: the per-operation transaction
    evidence is required to classify storage refusals (D-026), so B has no separate
    untimed runs; wall time includes the in-process timing reads."""
    command, lines, meta, cases = wl.gen_b(seed, prof["b_sessions"])
    led = Ledger(ledger, tmp, name, profile)
    meta = dict(meta, ledger="sqlite (file-backed, D-023)" if ledger == "sqlite" else
                "in-memory model (simulated; not a substitute for the SQLite workload)",
                concurrency_profile=profile if profile == "primary" else
                f"{profile} (supplemental, D-027; reported separately, never combined with primary)")
    path = tmp / f"{name.lower()}.jsonl"
    wl.write_lines(path, lines)
    ids, nulls = ids_of(lines)
    res = Results("bench-B")
    by_id = {c["id"]: c for c in cases}
    avail = {p.name: [] for p in protos}

    def verify_b(p, outs, tims, run):
        av = {}
        for c in cases:
            out, tm = outs.get(c["id"]), tims.get(c["id"])
            c_broker.check_run(res, p.name, c, out, tm)
            grp = out["steps"][-1] if isinstance(out, dict) and out.get("steps") else None
            gtim = tm["steps"][-1] if isinstance(tm, dict) and tm.get("steps") else None
            if isinstance(out, dict) and out.get("steps"):
                c_broker.availability_add(av, by_id[c["id"]], out, gtim)
            ok = isinstance(grp, dict) and grp.get("result") == "concurrent" and \
                c_broker.serializable(c, grp.get("outcomes") or {}, out.get("final") or {}, evidence=gtim)
            if not ok:
                check.fail(f"{name}.concurrent_group_equals_a_serialization", {"proto": p.name, "id": c["id"], "ok": ok})
        for lvl, a in av.items():
            a = dict(a)
            a.pop("reserve_wait_ns")
            avail[p.name].append({"run": run, "concurrency": lvl, **a,
                                  "lock_timeout_percent": (100 * a["lock_timeout_refusals"] / a["operations"])
                                  if a["operations"] else None})
    timing, walls = timed_broker_runs(name, protos, command, path, led, prof["b_runs"], check, ids, nulls, verify_b)
    check.ok(f"{name}.concurrent_group_equals_a_serialization")
    for inv, per in res.counts.items():
        check.ok(f"{name}.{inv}")
        for pname, v in per.items():
            if v["fail"]:
                check.fail(f"{name}.{inv}", {"proto": pname, "fails": v["fail"]})
    meas = {}
    for p in protos:
        samples[f"{name}.{p.name}"] = walls[p.name]
        runs_ok = [sum(x["successful"] for x in avail[p.name] if x["run"] == r) for r in range(prof["b_runs"])]
        meas[p.name] = {"batch_wall_s": stats.summarize(walls[p.name]),
                        "sessions_per_sec": (len(lines) * len(walls[p.name]) / sum(walls[p.name])) if walls[p.name] else None,
                        "successful_operations_per_sec": (sum(runs_ok) / sum(walls[p.name])) if walls[p.name] else None,
                        "peak_rss_kb": runner.peak_rss(p, led.cmd(command, p), path, tmp)["kb"],
                        "broker_timing_ns": timing[p.name]}
    return {"command": "bench-" + command, "seed": seed, "runs": prof["b_runs"], "digest": wl.digest_lines(lines),
            "meta": meta, "measurements": meas,
            "availability": {pn: a for pn, a in avail.items()},
            "method": "one broker-scenario per session, each ending in one concurrent group of 10 executes "
                      "(in-process threads); checked against every permitted serialization with D-026 evidence; "
                      "successful throughput counts only successful operations; refusals are reported under "
                      "availability, never as work"}


def workload_plain(name, gen, protos, runs, tmp: Path, check: Check, samples: dict, ledger: str | None = None) -> dict:
    """G and H: deterministic outputs; exact differential, then timed batches."""
    command, lines, meta = gen
    led = Ledger(ledger, tmp, name)
    if command == "broker-scenario":
        meta = dict(meta, ledger="sqlite (file-backed, D-023)" if ledger == "sqlite" else "in-memory model (simulated)")
    path = tmp / f"{name.lower()}.jsonl"
    wl.write_lines(path, lines)
    ids, nulls = ids_of(lines)
    ref = {p.name: outputs_of(p, led.cmd(command, p), path, check, ids, nulls)["out"] for p in protos}
    differential(name, *ref.values(), check)
    walls = {p.name: [] for p in protos}
    for run in range(runs):
        for p in order(protos, run):
            r = outputs_of(p, led.cmd(command, p), path, check, ids, nulls)
            walls[p.name].append(r["wall_s"])
            if r["out"] != ref[p.name]:
                check.fail(f"{name}.repeat_identical", {"proto": p.name, "run": run})
    check.ok(f"{name}.repeat_identical")
    timing = None
    if command == "broker-scenario":
        def verify_plain(p, outs, _tims, _run):
            if outs != ref[p.name]:
                check.fail(f"{name}.timed_scenario_equals_plain", {"proto": p.name})
        timing, _ = timed_broker_runs(name, protos, command, path, led, runs, check, ids, nulls, verify_plain)
        check.ok(f"{name}.timed_scenario_equals_plain")
    meas = {}
    for p in protos:
        samples[f"{name}.{p.name}"] = walls[p.name]
        meas[p.name] = {"batch_wall_s": stats.summarize(walls[p.name]),
                        "lines_per_sec": (len(lines) * runs / sum(walls[p.name])) if walls[p.name] else None,
                        "peak_rss_kb": runner.peak_rss(p, led.cmd(command, p), path, tmp)["kb"]}
        if timing is not None:
            meas[p.name]["broker_timing_ns"] = timing[p.name]
    return {"command": command, "runs": runs, "lines": len(lines), "digest": wl.digest_lines(lines), "meta": meta,
            "measurements": meas}


def cold(protos, prof, check: Check, samples: dict) -> dict:
    line = wl.cold_line()
    secs = {p.name: [] for p in protos}
    first = {}
    for i in range(prof["cold"]):
        for p in order(protos, i):
            r = runner.cold_start(p, "decide", line)
            out, _, problems = runner.parse(r["stdout"])
            if r["returncode"] != 0 or problems or "cold-0" not in out:
                check.fail("cold.answer", {"proto": p.name, "launch": i})
            first.setdefault(p.name, out.get("cold-0"))
            if out.get("cold-0") != first[p.name]:
                check.fail("cold.repeat_identical", {"proto": p.name, "launch": i})
            secs[p.name].append(r["s"])
    differential("cold", *({"cold-0": v} for v in first.values()), check)
    samples["cold"] = secs
    return {"launches": prof["cold"], "measurements": {k: {"to_first_answer_s": stats.summarize(v)} for k, v in secs.items()},
            "method": "process launch (decide, one golden case) to first answered line"}


def concurrency(protos, prof, seed, tmp: Path, check: Check, samples: dict) -> dict:
    command, lines, meta = wl.gen_a(seed + 2, prof["conc_total"], prefix="c")
    ids, nulls = ids_of(lines)
    levels = {}
    for k in prof["conc_levels"]:
        d = tmp / f"conc-{k}"
        d.mkdir()
        paths = []
        for i, part in enumerate(wl.split(lines, k)):
            paths.append(d / f"s{i}.jsonl")
            wl.write_lines(paths[-1], part)
        outs, meas = {}, {}
        for p in protos:
            r = runner.run_concurrent(p, command, paths, d)
            out, n_null, problems = runner.parse(r["stdout"])
            if r["returncode"] != 0 or problems or set(out) != ids or n_null != nulls:
                check.fail("concurrency.answers", {"proto": p.name, "sessions": k})
            outs[p.name] = out
            samples[f"concurrency.{k}.{p.name}"] = r["wall_s"]
            meas[p.name] = {"wall_s": r["wall_s"], "decisions_per_sec": len(lines) / r["wall_s"] if r["wall_s"] else None}
        differential(f"concurrency.{k}", *outs.values(), check)
        levels[str(k)] = {"sessions": k, "decisions": len(lines), "measurements": meas}
        shutil.rmtree(d)
    return {"seed": seed + 2, "digest": wl.digest_lines(lines), "levels": levels,
            "method": "decisions split across k simultaneously started single-threaded prototype processes "
                      "(one per simulated session); wall time until all exit"}


REQUIRED_SQLITE = {"sqlite_version": "3.54.1", "journal_mode": "wal", "synchronous": 2, "busy_timeout": 5000,
                   "foreign_keys": 1, "trusted_schema": 0}


def engine_check(protos, check: Check) -> dict:
    """Both prototypes must report the identical SQLite engine and settings (D-023)."""
    info = {}
    for p in protos:
        r = runner.subprocess.run(p.argv("sqlite-info"), capture_output=True, timeout=60)
        try:
            info[p.name] = json.loads(r.stdout)
        except ValueError:
            info[p.name] = None
            check.fail("sqlite.info_available", {"proto": p.name, "stderr": r.stderr[-300:].decode("utf-8", "replace")})
    check.ok("sqlite.info_available")
    vals = [v for v in info.values() if isinstance(v, dict)]
    same = len(vals) == len(protos) and all(
        (v.get("sqlite_version"), v.get("source_id"), v.get("compile_options"), v.get("settings"))
        == (vals[0].get("sqlite_version"), vals[0].get("source_id"), vals[0].get("compile_options"), vals[0].get("settings"))
        for v in vals)
    if not same:
        check.fail("sqlite.engine_and_settings_identical", {"info": info})
    check.ok("sqlite.engine_and_settings_identical")
    for v in vals:
        flat = dict(v.get("settings") or {}, sqlite_version=v.get("sqlite_version"))
        for k, want in REQUIRED_SQLITE.items():
            if str(flat.get(k)).lower() != str(want).lower():
                check.fail("sqlite.required_settings", {"key": k, "got": flat.get(k), "want": want})
    check.ok("sqlite.required_settings")
    return info


# ------------------------------------------------------------------- report

def evidence(result: dict) -> list[dict]:
    """Per (prototype, workload) records in the testing-doc evidence format."""
    recs = []
    tc = result["toolchains"]
    runtime = {"rust": (tc["rustc_vV"] or "").split("\n")[0] or None, "go": tc["go_version"]}
    hw = result["environment"]["hardware"]
    hardware = f"{hw['cpu_model']}, {hw['memory_mib']} MiB, {hw['os']}, Linux {hw['kernel']}"
    for wname, w in result["workloads"].items():
        for pname, m in w["measurements"].items():
            k = m.get("kernel_decision_ns")
            ms = (lambda v: v / 1e6 if isinstance(v, (int, float)) else None)
            thr = next((m[k] for k in ("throughput_per_sec", "successful_operations_per_sec", "lines_per_sec")
                        if k in m), None)
            recs.append({
                "commit": result["repository"]["commit"], "benchmark_version": BENCHMARK_VERSION,
                "hardware": hardware, "runtime": runtime.get(pname), "prototype": pname, "workload_name": wname,
                "workload": w["digest"], "sessions": w.get("meta", {}).get("sessions", 1),
                "latency_ms": {q: ms(k[q]) if k else None for q in ("p50", "p95", "p99")},
                "rss_mb": m["peak_rss_kb"] / 1024 if isinstance(m.get("peak_rss_kb"), int) else None,
                "throughput_per_sec": thr,
                "test_results": {"passed": sum(1 for v in result["correctness"]["checks"].values() if v == 0),
                                 "failed": sum(1 for v in result["correctness"]["checks"].values() if v)},
                "notes": "kernel-only latency" if k else "no per-operation latency for this workload; batch wall time only",
            })
    return recs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rust", required=True)
    ap.add_argument("--go", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cpu-list", help="pin prototypes with taskset -c (recorded)")
    ap.add_argument("--max-load", type=float, default=1.0)
    ap.add_argument("--max-start-temp-c", type=float, default=60.0)
    ap.add_argument("--sysfs-root", default="/", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    started = datetime.now(timezone.utc)
    env = envinfo.capture(a.sysfs_root)
    repo = envinfo.repository()
    tc = envinfo.toolchains(a.rust, a.go)
    reasons = envinfo.guard(env, repo, a.max_load, a.max_start_temp_c) + \
        envinfo.toolchain_reasons(tc, envinfo.pinned_versions())
    if not a.dry_run and reasons:
        print("bench: REFUSED to measure (M1-PARITY-SUITE measurement protocol):", file=sys.stderr)
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        print("Nothing was run or written. Use --dry-run for a plumbing check without measurements.", file=sys.stderr)
        return 3
    for b in (a.rust, a.go):
        if not Path(b).is_file():
            print(f"bench: prototype binary not found: {b}", file=sys.stderr)
            return 2
    prof_name = "dry-run" if a.dry_run else "protocol"
    prof = PROFILES[prof_name]
    protos = [runner.Proto("rust", str(Path(a.rust).resolve()), a.cpu_list),
              runner.Proto("go", str(Path(a.go).resolve()), a.cpu_list)]
    check, samples = Check(), {}
    tmp = Path(tempfile.mkdtemp(prefix="writ-m1-bench-"))
    try:
        sqlite_info = engine_check(protos, check)
        result_w = {"A": workload_a(protos, prof, a.seed, tmp, check, samples),
                    "B": workload_b(protos, prof, a.seed, tmp, check, samples, "sqlite", "B"),
                    "B-cold": workload_b(protos, prof, a.seed, tmp, check, samples, "sqlite", "B-cold", "cold"),
                    "B-mem": workload_b(protos, prof, a.seed, tmp, check, samples, None, "B-mem")}
        gdir = tmp / "g-symlinks"
        gdir.mkdir()
        result_w["G"] = workload_plain("G", wl.gen_g(a.seed, prof["g_n"], str(gdir)), protos, prof["g_runs"], tmp, check, samples)
        result_w["H"] = workload_plain("H", wl.gen_h(a.seed, prof["h_n"]), protos, prof["h_runs"], tmp, check, samples, "sqlite")
        result_w["H-mem"] = workload_plain("H-mem", wl.gen_h(a.seed, prof["h_n"]), protos, prof["h_runs"], tmp, check, samples)
        cold_r = cold(protos, prof, check, samples)
        conc_r = concurrency(protos, prof, a.seed, tmp, check, samples)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    env_end = {"thermals": envinfo.thermals(a.sysfs_root), "cpufreq": envinfo.cpufreq(a.sysfs_root),
               "power": envinfo.power(a.sysfs_root), "load": envinfo.load(a.sysfs_root)}

    if a.dry_run:
        for w in result_w.values():
            w["measurements"] = stats.redact(w["measurements"])
        cold_r["measurements"] = stats.redact(cold_r["measurements"])
        for lv in conc_r["levels"].values():
            lv["measurements"] = stats.redact(lv["measurements"])

    result = {
        "format": FORMAT, "benchmark_version": BENCHMARK_VERSION,
        "not_a_benchmark": a.dry_run,
        "mode": "dry-run" if a.dry_run else "measure",
        "valid_for_comparison": (not a.dry_run) and check.failed() == 0 and not reasons,
        "profile": {"name": prof_name, **prof},
        "seed": a.seed,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": repo,
        "prototypes": {p.name: {"argv_prefix": p.prefix, "binary": p.binary, "sha256": envinfo.sha256_file(p.binary)}
                       for p in protos},
        "toolchains": tc, "pinned_versions": envinfo.pinned_versions(),
        "environment": env, "environment_end": env_end,
        "guard": {"enforced": not a.dry_run, "passed": not reasons, "reasons": reasons,
                  "max_load": a.max_load, "max_start_temp_c": a.max_start_temp_c},
        "correctness": {"failed": check.failed(), "checks": dict(check.counts), "examples": check.examples},
        "workloads": result_w, "cold_start": cold_r, "concurrency": conc_r, "sqlite": sqlite_info,
        "not_measured": {
            "E": "chaos (ledger and broker kill) not implemented in M1-06a",
            "ledgers": "B and H use the file-backed SQLite ledger (D-023, fresh database per process); B-mem and "
                       "H-mem use the in-memory model and are not substitutes for the SQLite results",
            "broker_timing": "B, B-mem, H and H-mem report per-operation broker timing from bench-broker-scenario "
                             "(validation, kernel decision, key-lock wait, each transaction's begin/body/end, "
                             "parse and run per line, process and protocol overhead) in separate timed runs",
            "G_symlinks": "M1 kernels perform no filesystem access; symlink paths exercise string handling only",
        },
        "samples_file": None,
    }
    result["evidence"] = evidence(result)
    out = Path(a.out)
    if not a.dry_run:
        sp = out.with_name(out.name + ".samples.json.gz")
        sp.write_bytes(gzip.compress(json.dumps(samples, separators=(",", ":")).encode()))
        result["samples_file"] = {"path": sp.name, "sha256": "sha256:" + hashlib.sha256(sp.read_bytes()).hexdigest()}
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    label = "DRY RUN (not a benchmark, no timing values recorded)" if a.dry_run else "MEASUREMENT"
    print(f"bench: {label}; correctness failures: {check.failed()}; wrote {out}")
    return 4 if check.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
