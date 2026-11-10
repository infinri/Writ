"""Self-tests for the M1-06a benchmark harness (no measurements are taken).

  python3 -m unittest discover -s m1/bench/tests
  WRIT_BENCH_RUST=... WRIT_BENCH_GO=... python3 -m unittest discover -s m1/bench/tests   # adds the dry run
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

import bench  # noqa: E402
from wbench import envinfo, stats, workloads as wl  # noqa: E402


def fake_sysfs(root: Path, *, mains_online: str, governor: str, temp_mc: str = "45000", load: str = "0.10",
               boost: str | None = "1"):
    def w(rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
    w("sys/class/power_supply/ACAD/type", "Mains")
    w("sys/class/power_supply/ACAD/online", mains_online)
    w("sys/class/power_supply/BAT1/type", "Battery")
    w("sys/class/power_supply/BAT1/status", "Discharging" if mains_online == "0" else "Full")
    for i in range(4):
        w(f"sys/devices/system/cpu/cpufreq/policy{i}/scaling_governor", governor)
        w(f"sys/devices/system/cpu/cpufreq/policy{i}/scaling_driver", "amd-pstate-epp")
    if boost is not None:
        w("sys/devices/system/cpu/cpufreq/boost", boost)
    w("sys/class/hwmon/hwmon8/name", "k10temp")
    w("sys/class/hwmon/hwmon8/temp1_input", temp_mc)
    w("proc/loadavg", f"{load} 0.20 0.30 1/500 1234")


CLEAN = {"commit": "c" * 40, "m1_tree_clean": True}


class Stats(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        s = list(range(1, 101))
        self.assertEqual(stats.percentile(s, 50), 50)
        self.assertEqual(stats.percentile(s, 95), 95)
        self.assertEqual(stats.percentile(s, 99), 99)
        self.assertEqual(stats.percentile([7], 99), 7)
        self.assertIsNone(stats.percentile([], 50))

    def test_summary_and_run_to_run(self):
        sm = stats.summarize([3, 1, 2])
        self.assertEqual((sm["n"], sm["min"], sm["p50"], sm["max"], sm["mean"], sm["stdev"]), (3, 1, 2, 3, 2, 1))
        rr = stats.run_to_run([10, 10, 10])
        self.assertEqual((rr["variance"], rr["cv"]), (0, 0))

    def test_redact_keeps_counts_only(self):
        r = stats.redact({"x": {"n": 5, "p50": 1.5, "values": [1, 2]}, "n_runs": 3, "rss": 9, "label": "ok"})
        self.assertEqual(r, {"x": {"n": 5, "p50": None, "values": [None, None]}, "n_runs": 3, "rss": None, "label": "ok"})


class Guard(unittest.TestCase):
    def guard(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            fake_sysfs(Path(d), **kw)
            return envinfo.guard(envinfo.capture(d), CLEAN, max_load=1.0, max_start_temp_c=60)

    def test_refuses_battery_and_powersave(self):
        reasons = self.guard(mains_online="0", governor="powersave")
        self.assertTrue(any("battery" in r for r in reasons))
        self.assertTrue(any("governor" in r for r in reasons))

    def test_refuses_mixed_governors(self):
        with tempfile.TemporaryDirectory() as d:
            fake_sysfs(Path(d), mains_online="1", governor="performance")
            (Path(d) / "sys/devices/system/cpu/cpufreq/policy2/scaling_governor").write_text("powersave\n")
            self.assertTrue(any("governor" in r for r in envinfo.guard(envinfo.capture(d), CLEAN, 1.0, 60)))

    def test_refuses_hot_busy_unknown_boost_and_dirty_tree(self):
        reasons = self.guard(mains_online="1", governor="performance", temp_mc="75000", load="3.5", boost=None)
        self.assertEqual(len(reasons), 3, reasons)
        with tempfile.TemporaryDirectory() as d:
            fake_sysfs(Path(d), mains_online="1", governor="performance")
            dirty = envinfo.guard(envinfo.capture(d), {"commit": "c", "m1_tree_clean": False}, 1.0, 60)
            self.assertEqual(len(dirty), 1)

    def test_allows_controlled_environment(self):
        self.assertEqual(self.guard(mains_online="1", governor="performance"), [])

    def test_measure_mode_refuses_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            fake_sysfs(Path(d), mains_online="0", governor="powersave")
            out = Path(d) / "r.json"
            code = bench.main(["--rust", "/nonexistent", "--go", "/nonexistent", "--seed", "1", "--out", str(out),
                               "--sysfs-root", d])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())


class Toolchains(unittest.TestCase):
    PINS = {"RUST_VERSION": "1.99.1", "GO_VERSION": "1.27.3"}
    GOOD = {"rustc_vV": "rustc 1.99.1 (58cf8f9c3 2026-10-13)\nhost: x", "go_version": "go version go1.27.3 linux/amd64",
            "go_binary_buildinfo": "m1/go/proto: go1.27.3\n\tpath\tx\n\tbuild\t-tags=libsqlite3,sqlite_omit_load_extension\n\tbuild\t-trimpath=true"}

    def test_pins_read_from_install_script(self):
        self.assertEqual(set(envinfo.pinned_versions()), {"RUST_VERSION", "GO_VERSION"})

    def test_pinned_toolchains_pass_and_others_refuse(self):
        self.assertEqual(envinfo.toolchain_reasons(self.GOOD, self.PINS), [])
        bad = dict(self.GOOD, rustc_vV="rustc 1.99.10 (x)", go_version=None, go_binary_buildinfo="p: go1.27.4\n")
        self.assertEqual(len(envinfo.toolchain_reasons(bad, self.PINS)), 5)
        untrimmed = dict(self.GOOD, go_binary_buildinfo=self.GOOD["go_binary_buildinfo"].replace("\n\tbuild\t-trimpath=true", ""))
        self.assertEqual(envinfo.toolchain_reasons(untrimmed, self.PINS),
                         ["Go prototype binary was not built with -trimpath (owner decision 2026-11-10)"])


class Timing(unittest.TestCase):
    def test_collect_timing_flattens_steps_tx_and_concurrent(self):
        tx = {"begin_ns": 1, "body_ns": 2, "end_ns": 3, "outcome": "commit"}
        step = {"op": "execute", "total_ns": 10, "validate_ns": 1, "decide_ns": 2, "key_lock_wait_ns": None,
                "tx": {"reserve": tx}}
        timing = {"parse_ns": 5, "open_ns": None, "final_ns": 4, "run_ns": 30,
                  "steps": [step, {"op": "concurrent", "total_ns": 20, "validate_ns": None, "decide_ns": None,
                                   "key_lock_wait_ns": None, "tx": {}, "operations": [step]}]}
        acc = {}
        self.assertEqual(bench.collect_timing(timing, acc), 35)
        self.assertEqual(acc["op.execute.total_ns"], [10])
        self.assertEqual(acc["concurrent.op.execute.total_ns"], [10])
        self.assertEqual(acc["concurrent.total_ns"], [20])
        self.assertEqual(acc["tx.reserve.end_ns"], [3])
        self.assertNotIn("op.execute.key_lock_wait_ns", acc)
        self.assertNotIn("line.open_ns", acc)


class Generators(unittest.TestCase):
    def test_seeded_and_deterministic(self):
        a1, a2, a3 = wl.gen_a(5, 50), wl.gen_a(5, 50), wl.gen_a(6, 50)
        self.assertEqual(wl.digest_lines(a1[1]), wl.digest_lines(a2[1]))
        self.assertNotEqual(wl.digest_lines(a1[1]), wl.digest_lines(a3[1]))
        self.assertEqual(wl.digest_lines(wl.gen_h(5, 20)[1]), wl.digest_lines(wl.gen_h(5, 20)[1]))
        self.assertEqual(wl.digest_lines(wl.gen_b(5, 4)[1]), wl.digest_lines(wl.gen_b(5, 4)[1]))

    def test_b_sessions_are_concurrent_groups_of_ten(self):
        _, lines, meta, cases = wl.gen_b(1, 3)
        self.assertEqual(len(lines), 3)
        for c in cases:
            self.assertEqual(len(c["input"]["steps"][-1]["operations"]), 10)

    def test_g_covers_every_kind(self):
        with tempfile.TemporaryDirectory() as d:
            _, lines, meta = wl.gen_g(1, 8, d)
            self.assertTrue(all(v == 1 for v in meta["kinds"].values()), meta)
            self.assertTrue(os.path.islink(os.path.join(d, "link-out")))
        bad = [ln for ln in lines if not _utf8(ln)]
        self.assertEqual(len(bad), 1)
        self.assertTrue(any(len(ln) > (1 << 20) for ln in lines))

    def test_every_h_template_present(self):
        self.assertTrue(all(v == 1 for v in wl.gen_h(1, len(wl.H_TEMPLATES))[2]["templates"].values()))


def _utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _numbers_under(value, key="measurements", inside=False):
    if isinstance(value, dict):
        for k, v in value.items():
            if inside and k in ("n", "n_runs"):
                continue
            yield from _numbers_under(v, key, inside or k == key)
    elif isinstance(value, list):
        for v in value:
            yield from _numbers_under(v, key, inside)
    elif inside and isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


@unittest.skipUnless(os.environ.get("WRIT_BENCH_RUST") and os.environ.get("WRIT_BENCH_GO"),
                     "set WRIT_BENCH_RUST and WRIT_BENCH_GO to prototype binaries")
class DryRun(unittest.TestCase):
    def test_dry_run_is_correct_and_records_no_measurements(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "dry.json"
            code = bench.main(["--dry-run", "--seed", "20261107", "--rust", os.environ["WRIT_BENCH_RUST"],
                               "--go", os.environ["WRIT_BENCH_GO"], "--out", str(out)])
            r = json.loads(out.read_text())
            self.assertEqual(list(Path(d).iterdir()), [out], "no samples file in a dry run")
        self.assertEqual(code, 0, r["correctness"])
        self.assertIs(r["not_a_benchmark"], True)
        self.assertIs(r["valid_for_comparison"], False)
        self.assertEqual(r["correctness"]["failed"], 0)
        self.assertGreaterEqual(len(r["correctness"]["checks"]), 10)
        self.assertEqual(list(_numbers_under(r)), [], "dry run must not record timing or memory values")
        for e in r["evidence"]:
            self.assertEqual((e["latency_ms"], e["rss_mb"], e["throughput_per_sec"]),
                             ({"p50": None, "p95": None, "p99": None}, None, None))
        schema = json.loads((BENCH / "result.schema.json").read_text())
        for key in schema["required"]:
            self.assertIn(key, r)
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.validate(r, schema)


if __name__ == "__main__":
    unittest.main()
