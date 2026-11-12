#!/usr/bin/env python3
"""Production dependency gate for the Rust workspace (M2-01, kernel/DEPENDENCIES.md).

Fails if the workspace Cargo.lock contains a package that is not on the reviewed
allow-list (name, exact version, and checksum where recorded), a forbidden crate,
or a licence outside the approved set. Optional:
  --osv        query api.osv.dev for known advisories of every locked package
  --sbom PATH  write a minimal CycloneDX-style JSON SBOM of the locked packages

  python3 kernel/tools/check_deps.py [--osv] [--sbom kernel-sbom.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def locked(lock_path: Path) -> list[dict]:
    lock = tomllib.loads(lock_path.read_text())
    return [p for p in lock["package"] if p.get("source")]


def licences() -> dict:
    out = subprocess.run(["cargo", "metadata", "--locked", "--format-version", "1"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return {(p["name"], p["version"]): p.get("license") for p in json.loads(out)["packages"] if p.get("source")}


def check(pkgs: list[dict], allow: dict, lic: dict) -> list[str]:
    allowed, problems = allow["packages"], []
    for p in pkgs:
        name, ver = p["name"], p["version"]
        if name in allow["forbidden"]:
            problems.append(f"forbidden crate {name} {ver}")
        entry = allowed.get(name)
        if entry is None:
            problems.append(f"not on the allow-list: {name} {ver}")
            continue
        if entry["version"] != ver:
            problems.append(f"{name}: locked {ver}, approved {entry['version']}")
        if "checksum" in entry and entry["checksum"] != p.get("checksum"):
            problems.append(f"{name}: checksum differs from the reviewed one")
        if lic.get((name, ver)) not in allow["approved_licenses"]:
            problems.append(f"{name} {ver}: licence {lic.get((name, ver))!r} not approved")
    for name in allowed:
        if name not in {p["name"] for p in pkgs}:
            problems.append(f"allow-list entry {name} is not in Cargo.lock (remove it or explain)")
    return problems


def osv(pkgs: list[dict]) -> list[str]:
    body = json.dumps({"queries": [{"package": {"ecosystem": "crates.io", "name": p["name"]}, "version": p["version"]}
                                   for p in pkgs]}).encode()
    req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "writ-dependency-gate"})
    with urllib.request.urlopen(req, timeout=60) as r:
        results = json.load(r)["results"]
    return [f"{p['name']} {p['version']}: {[v['id'] for v in res.get('vulns', [])]}"
            for p, res in zip(pkgs, results) if res.get("vulns")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--osv", action="store_true")
    ap.add_argument("--sbom", type=Path)
    ap.add_argument("--lock", type=Path, default=ROOT / "Cargo.lock")
    ap.add_argument("--allowlist", type=Path, default=ROOT / "kernel/deps-allowlist.json")
    a = ap.parse_args()
    allow = json.loads(a.allowlist.read_text())
    pkgs, lic = locked(a.lock), licences()
    problems = check(pkgs, allow, lic)
    if a.osv:
        problems += [f"advisory: {x}" for x in osv(pkgs)]
    if a.sbom:
        a.sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
            {"type": "library", "name": p["name"], "version": p["version"], "purl": f"pkg:cargo/{p['name']}@{p['version']}",
             "licenses": [{"expression": lic.get((p["name"], p["version"]))}],
             "hashes": [{"alg": "SHA-256", "content": p["checksum"]}] if p.get("checksum") else []} for p in pkgs]},
            indent=2) + "\n")
    for x in problems:
        print("dependency gate:", x, file=sys.stderr)
    print(f"dependency gate: {len(pkgs)} locked packages, {len(problems)} problems{' (OSV checked)' if a.osv else ''}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
