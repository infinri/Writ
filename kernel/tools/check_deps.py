#!/usr/bin/env python3
"""Production dependency gate for the Rust workspace (M2-01, M3-00).

Per crate: the dependency closure of each workspace member (normal and build
dependencies, from `cargo metadata`) must match that crate's reviewed allow-list
(name, exact version, checksum where recorded), contain no forbidden crate, and
use only approved licences. The kernel closure therefore stays exactly as
reviewed in kernel/DEPENDENCIES.md even when other crates add dependencies.
Every registry package in Cargo.lock must belong to some member's closure. Optional:
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


MEMBERS = {"writ-kernel": "kernel/deps-allowlist.json", "writ-broker": "broker/deps-allowlist.json",
           "writ-ledger-sqlite": "persistence/sqlite/deps-allowlist.json"}


def metadata() -> dict:
    out = subprocess.run(["cargo", "metadata", "--locked", "--format-version", "1"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def licences(meta: dict) -> dict:
    return {(p["name"], p["version"]): p.get("license") for p in meta["packages"] if p.get("source")}


def forbidden_features(meta: dict, allow: dict) -> list[str]:
    """Resolved features that the allow-list forbids (for example a bundled C library)."""
    pkgs = {p["id"]: p for p in meta["packages"]}
    out = []
    for node in meta["resolve"]["nodes"]:
        name = pkgs[node["id"]]["name"]
        bad = set(node.get("features", [])) & set(allow.get("forbidden_features", {}).get(name, []))
        if bad:
            out.append(f"{name}: forbidden features enabled {sorted(bad)}")
    return out


def closures(meta: dict) -> dict:
    """{member name: set of (name, version) registry packages}: each member's own closure of
    normal and build dependencies, with that member's own feature resolution (`cargo tree -p`,
    not the workspace-unified resolve, where another crate's features would leak in), and with
    other workspace members pruned (their dependencies are gated by their own allow-lists)."""
    pkgs = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    member_ids = set(meta["workspace_members"])
    out = {}
    for mid in meta["workspace_members"]:
        name = pkgs[mid]["name"]
        argv = ["cargo", "tree", "--locked", "--target", "all", "-p", name, "-e", "normal,build", "--prefix", "none", "--format", "{p}"]
        for dep in nodes[mid]["deps"]:
            if dep["pkg"] in member_ids:
                argv += ["--prune", pkgs[dep["pkg"]]["name"]]
        lines = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        found = set()
        for line in lines:
            parts = line.replace(" (*)", "").split()
            if len(parts) >= 2 and parts[1].startswith("v") and "(" not in line:
                found.add((parts[0], parts[1][1:]))
        out[name] = found
    return out


def combined(edges: str) -> dict:
    """Effective graph of a supported combined build (all production members built together,
    so Cargo unifies features across them): {root member: set of (name, version)} for the
    given edge kinds (`normal,build` for release builds, `normal,build,dev` for test builds)."""
    names = list(MEMBERS)
    argv = ["cargo", "tree", "--locked", "--target", "all", "--no-dedupe", "-e", edges, "--prefix", "depth", "--format", "{p}"]
    for n in names:
        argv += ["-p", n]
    out, root = {}, None
    for line in subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines():
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip("0123456789"))
        level, rest = int(line[:depth]), line[depth:].replace(" (*)", "").split()
        if level == 0:
            root = rest[0]
            out.setdefault(root, set())
        elif root and len(rest) >= 2 and "(" not in " ".join(rest[2:3]) and not line.rstrip().endswith(")"):
            out[root].add((rest[0], rest[1][1:]))
    return out


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
        licence = lic.get((name, ver))
        if licence not in allow["approved_licenses"] and licence != entry.get("licence_approved"):
            problems.append(f"{name} {ver}: licence {licence!r} not approved")
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
    a = ap.parse_args()
    meta = metadata()
    lic, clos = licences(meta), closures(meta)
    pkgs = locked(a.lock)
    by_key = {(p["name"], p["version"]): p for p in pkgs}
    problems = []
    if set(clos) != set(MEMBERS):
        problems.append(f"workspace members {sorted(clos)} differ from the gated set {sorted(MEMBERS)}")
    covered = set()
    for member, allow_path in MEMBERS.items():
        allow = json.loads((ROOT / allow_path).read_text())
        member_pkgs = [by_key.get(k, {"name": k[0], "version": k[1]}) for k in sorted(clos.get(member, set()))]
        covered |= clos.get(member, set())
        problems += [f"{member}: {x}" for x in check(member_pkgs, allow, lic)]
        problems += [f"{member}: {x}" for x in forbidden_features(meta, allow)]
    for p in pkgs:
        if (p["name"], p["version"]) not in covered:
            problems.append(f"locked package {p['name']} {p['version']} is in no member's closure")
    # Supported combined builds (release and test configurations): every package must be approved
    # by some member's allow-list (dev-only packages by a `dev_packages` entry), and the kernel's
    # effective subtree is reported against its standalone closure.
    approved, dev_approved = set(), set()
    for allow_path in MEMBERS.values():
        allow = json.loads((ROOT / allow_path).read_text())
        approved |= {(n, e["version"]) for n, e in allow["packages"].items()}
        dev_approved |= {(n, e["version"]) for n, e in allow.get("dev_packages", {}).items()}
    release, test = combined("normal,build"), combined("normal,build,dev")
    for p in sorted(set().union(*release.values()) - approved):
        problems.append(f"combined release build: {p[0]} {p[1]} not approved by any member")
    for p in sorted(set().union(*test.values()) - approved - dev_approved):
        problems.append(f"combined test build: {p[0]} {p[1]} not approved by any member (normal or dev)")
    kernel_extra = sorted(release.get("writ-kernel", set()) - clos.get("writ-kernel", set()))
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
    sizes = {m: len(c) for m, c in clos.items()}
    print(f"dependency gate: {len(pkgs)} locked packages; standalone closures {sizes}; combined release build "
          f"{len(set().union(*release.values()))} packages, test build {len(set().union(*test.values()))}; kernel subtree in "
          f"the combined build adds {[f'{n} {v}' for n, v in kernel_extra] or 'nothing'} (feature unification); "
          f"{len(problems)} problems{' (OSV checked)' if a.osv else ''}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
