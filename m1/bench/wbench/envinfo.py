"""Environment capture and the measurement guard (M1-06a).

Everything is read from sysfs, procfs and tool version output; nothing is
changed. `root` lets the self-tests point the readers at a fake tree.
"""
from __future__ import annotations

import glob
import hashlib
import os
import platform
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cmd(argv: list[str], cwd: Path | None = None) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def power(root: str = "/") -> dict:
    mains, batteries = [], []
    for d in sorted(glob.glob(os.path.join(root, "sys/class/power_supply/*"))):
        kind = _read(os.path.join(d, "type"))
        if kind == "Mains":
            mains.append({"name": os.path.basename(d), "online": _read(os.path.join(d, "online")) == "1"})
        elif kind == "Battery":
            batteries.append({"name": os.path.basename(d), "status": _read(os.path.join(d, "status")),
                              "capacity_percent": _read(os.path.join(d, "capacity"))})
    return {"mains": mains, "on_mains": any(m["online"] for m in mains), "batteries": batteries}


def cpufreq(root: str = "/") -> dict:
    base = os.path.join(root, "sys/devices/system/cpu")
    pols = sorted(glob.glob(os.path.join(base, "cpufreq/policy*")), key=lambda p: int(p.rsplit("policy", 1)[1]))
    def per(name):
        return sorted({v for v in (_read(os.path.join(p, name)) for p in pols) if v is not None})
    return {
        "policies": len(pols),
        "governors": per("scaling_governor"),
        "drivers": per("scaling_driver"),
        "energy_performance_preference": per("energy_performance_preference"),
        "scaling_min_khz": per("scaling_min_freq"),
        "scaling_max_khz": per("scaling_max_freq"),
        "boost": _read(os.path.join(base, "cpufreq/boost")),
        "platform_profile": _read(os.path.join(root, "sys/firmware/acpi/platform_profile")),
    }


def thermals(root: str = "/") -> dict:
    out = {"cpu_c": None, "cpu_sensor": None, "zones_c": {}}
    for h in sorted(glob.glob(os.path.join(root, "sys/class/hwmon/hwmon*"))):
        name = _read(os.path.join(h, "name"))
        if name in ("k10temp", "coretemp", "zenpower") and out["cpu_c"] is None:
            raw = _read(os.path.join(h, "temp1_input"))
            if raw and raw.lstrip("-").isdigit():
                out["cpu_c"], out["cpu_sensor"] = int(raw) / 1000, f"{name}/temp1"
    for z in sorted(glob.glob(os.path.join(root, "sys/class/thermal/thermal_zone*"))):
        raw = _read(os.path.join(z, "temp"))
        if raw and raw.lstrip("-").isdigit():
            out["zones_c"][f"{os.path.basename(z)}:{_read(os.path.join(z, 'type'))}"] = int(raw) / 1000
    return out


def load(root: str = "/") -> dict:
    raw = _read(os.path.join(root, "proc/loadavg"))
    parts = raw.split() if raw else []
    return {"loadavg_1m": float(parts[0]) if parts else None, "loadavg_5m": float(parts[1]) if len(parts) > 1 else None}


def hardware(root: str = "/") -> dict:
    model, mem = None, None
    info = _read(os.path.join(root, "proc/cpuinfo")) or ""
    for line in info.splitlines():
        if line.startswith("model name") and model is None:
            model = line.split(":", 1)[1].strip()
    for line in (_read(os.path.join(root, "proc/meminfo")) or "").splitlines():
        if line.startswith("MemTotal:"):
            mem = int(line.split()[1]) // 1024
    osr = {}
    for line in (_read(os.path.join(root, "etc/os-release")) or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip('"')
    return {"cpu_model": model, "logical_cpus": os.cpu_count(), "memory_mib": mem,
            "os": osr.get("PRETTY_NAME"), "kernel": platform.release(), "machine": platform.machine()}


def sha256_file(path: str) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def toolchains(rust_bin: str | None, go_bin: str | None) -> dict:
    cargo_toml = (REPO / "m1/rust/Cargo.toml").read_text()
    profile = cargo_toml[cargo_toml.index("[profile.release]"):] if "[profile.release]" in cargo_toml else None
    return {
        "rustc_vV": _cmd(["rustc", "-vV"]),
        "cargo_V": _cmd(["cargo", "-V"]),
        "rust_build": "cargo build --release --locked (m1/rust)",
        "rust_profile_release": profile,
        "rust_env": {k: os.environ.get(k) for k in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_PROFILE_RELEASE_OPT_LEVEL")},
        "go_version": _cmd(["go", "version"]),
        "go_env": _cmd(["go", "env", "GOOS", "GOARCH", "GOAMD64", "CGO_ENABLED", "GOFLAGS", "GOTOOLCHAIN"]),
        "go_build": "go build -trimpath -tags 'libsqlite3 sqlite_omit_load_extension' -o proto ./cmd/proto (m1/go)",
        "go_binary_buildinfo": _cmd(["go", "version", "-m", go_bin]) if go_bin else None,
        "python": platform.python_version(),
        "time_v": os.path.exists("/usr/bin/time"),
    }


def pinned_versions() -> dict:
    """RUST_VERSION and GO_VERSION from m1/tools/install-toolchains.sh."""
    pins = {}
    for line in (REPO / "m1/tools/install-toolchains.sh").read_text().splitlines():
        for key in ("RUST_VERSION", "GO_VERSION"):
            if line.startswith(key + "="):
                pins[key] = line.split("=", 1)[1].strip().strip('"')
    return pins


def toolchain_reasons(tc: dict, pins: dict) -> list[str]:
    reasons = []
    rust = (tc.get("rustc_vV") or "").split("\n")[0]
    if not pins.get("RUST_VERSION") or not rust.startswith(f"rustc {pins['RUST_VERSION']} "):
        reasons.append(f"rustc on PATH is {rust or 'missing'!r}, pinned {pins.get('RUST_VERSION')} (source m1/tools/env.sh)")
    go = tc.get("go_version") or ""
    if not pins.get("GO_VERSION") or not go.startswith(f"go version go{pins['GO_VERSION']} "):
        reasons.append(f"go on PATH is {go or 'missing'!r}, pinned {pins.get('GO_VERSION')} (source m1/tools/env.sh)")
    built = tc.get("go_binary_buildinfo") or ""
    if pins.get("GO_VERSION") and not built.split("\n")[0].endswith(f": go{pins['GO_VERSION']}"):
        reasons.append("Go prototype binary was not built with the pinned Go version")
    if "build\t-trimpath=true" not in built:
        reasons.append("Go prototype binary was not built with -trimpath (owner decision 2026-11-10)")
    if "build\t-tags=libsqlite3,sqlite_omit_load_extension" not in built:
        reasons.append("Go prototype binary was not built with tags libsqlite3,sqlite_omit_load_extension (D-023)")
    return reasons


def repository() -> dict:
    dirty = _cmd(["git", "status", "--porcelain", "--", "m1/"], cwd=REPO)
    return {
        "commit": _cmd(["git", "rev-parse", "HEAD"], cwd=REPO),
        "m1_tree_clean": dirty == "",
        "m1_dirty_paths": dirty.splitlines()[:20] if dirty else [],
        "fixture_manifest_sha256": sha256_file(str(REPO / "m1/fixtures/manifest.json")),
    }


def capture(root: str = "/") -> dict:
    return {"power": power(root), "cpufreq": cpufreq(root), "thermals": thermals(root),
            "load": load(root), "hardware": hardware(root)}


def guard(env: dict, repo: dict, max_load: float, max_start_temp_c: float) -> list[str]:
    """Reasons a measurement run must not start; empty means allowed.
    Protocol: mains power, fixed `performance` governor on every policy,
    quiet machine, cool start, prototype sources committed."""
    reasons = []
    p, f, t, ld = env["power"], env["cpufreq"], env["thermals"], env["load"]
    if not p["mains"]:
        reasons.append("no mains power supply found in sysfs")
    elif not p["on_mains"]:
        reasons.append("on battery: mains adapter offline")
    if f["policies"] == 0:
        reasons.append("no cpufreq policies readable")
    elif f["governors"] != ["performance"]:
        reasons.append(f"CPU governor is {f['governors']}, required ['performance'] on every policy")
    if f["boost"] is None:
        reasons.append("boost state unreadable (must be recorded)")
    if ld["loadavg_1m"] is None or ld["loadavg_1m"] > max_load:
        reasons.append(f"1-minute load average {ld['loadavg_1m']} above {max_load}")
    if t["cpu_c"] is None:
        reasons.append("CPU temperature unreadable")
    elif t["cpu_c"] > max_start_temp_c:
        reasons.append(f"CPU temperature {t['cpu_c']} C above start limit {max_start_temp_c} C")
    if not repo["m1_tree_clean"]:
        reasons.append("uncommitted changes under m1/ (binaries must be built from a recorded commit)")
    if not repo["commit"]:
        reasons.append("repository commit unknown")
    return reasons
