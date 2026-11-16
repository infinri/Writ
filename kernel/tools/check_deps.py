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
  --release    treat recorded development-only advisory exceptions as problems (D-041 DR-3:
               release needs an explicit disposition)

Allow-list keys are a package name, or `name@version` where a closure holds several versions
of one package (each version is a separate exact approval). `locked_unbuilt` records packages
that Cargo.lock holds but no supported build compiles (checked against the combined test
graph; one that becomes built fails until reviewed). An `advisory_exceptions` entry
(`name@version` -> advisory ids, scope, decision) keeps a recorded advisory visible in the
output without failing a development gate run; any other advisory fails.

Source rules (M5-INTERFACES J-0 feature-unification note; M5a-11): Cargo builds one copy of
`rustix` and `tokio` per target with the union of the features of every member built
together, so "the broker uses rustix `time` only" and "`writ-approval` never uses
`tokio::signal`" are checked on the members' sources and manifests (`source_rules`), not as
forbidden features of the unified resolve.

  python3 kernel/tools/check_deps.py [--osv] [--release] [--sbom kernel-sbom.json]
"""
from __future__ import annotations

import argparse
import json
import re
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
           "writ-ledger-sqlite": "persistence/sqlite/deps-allowlist.json",
           "writ-approval": "approval/deps-allowlist.json",
           "writ-approval-server": "approval-server/deps-allowlist.json",
           "writ-keys": "keys/deps-allowlist.json",
           "writ-hostsys": "hostsys/deps-allowlist.json",
           "writ-rpc": "rpc/deps-allowlist.json",
           "writ-service": "service/deps-allowlist.json",
           "writ-containment": "containment/deps-allowlist.json",
           "writ-fs-effects": "fs-effects/deps-allowlist.json"}


def entry_for(allowed: dict, name: str, ver: str):
    """The exact approval for (name, version): a `name@version` key, else a `name` key."""
    return allowed.get(f"{name}@{ver}") or allowed.get(name)


def approved_keys(allowed: dict) -> set:
    return {(k.split("@", 1)[0], e["version"]) for k, e in allowed.items()}


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
            line = line.replace(" (proc-macro)", "")
            parts = line.replace(" (*)", "").split()
            if len(parts) >= 2 and parts[1].startswith("v") and "(" not in line:
                found.add((parts[0], parts[1][1:]))
        out[name] = found
    return out


def full_closures(meta: dict) -> dict:
    """{member name: set of registry package names}: each member's complete normal and build
    closure INCLUDING what it reaches through other workspace members (not pruned), so a crate
    forbidden for a member is caught even when it arrives through a path dependency."""
    out = {}
    for mid in meta["workspace_members"]:
        name = next(p["name"] for p in meta["packages"] if p["id"] == mid)
        argv = ["cargo", "tree", "--locked", "--target", "all", "-p", name, "-e", "normal,build", "--prefix", "none",
                "--format", "{p}"]
        found = set()
        for line in subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines():
            parts = line.replace(" (proc-macro)", "").replace(" (*)", "").split()
            if len(parts) >= 2 and parts[1].startswith("v"):
                found.add(parts[0])
        out[name] = found
    return out


def forbidden_through_members(full: dict, allows: dict) -> list[str]:
    """Crates in a member's `forbidden_anywhere` list found anywhere in its complete closure,
    including through workspace members (`forbidden` itself applies to the member's own closure,
    where a crate legitimately reached through another member, such as hmac through the broker
    for the ledger, is not counted)."""
    return [f"{member}: forbidden crate {crate} reached (directly or through a workspace member)"
            for member, allow in allows.items()
            for crate in sorted(full.get(member, set()) & set(allow.get("forbidden_anywhere", [])))]


def strip_rust(text: str) -> str:
    """Rust source with comments, string literals and character literals blanked (newlines kept),
    so the source rules below see only code."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    out.append("\n" if text[i] == "\n" else "")
                    i += 1
        elif c in "rb" and re.match(r'(?:br|r|b)?#*"', text[i:i + 8]) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            m = re.match(r'(b?r)(#*)"', text[i:])
            if m:
                end = text.find('"' + m.group(2), i + len(m.group(0)))
                i = n if end < 0 else end + 1 + len(m.group(2))
            else:
                out.append(c)
                i += 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            out.append('""')
        elif c == "'" and re.match(r"'(\\.|[^\\'])'", text[i:i + 4]):
            i += len(re.match(r"'(\\.|[^\\'])'", text[i:i + 4]).group(0))
        else:
            out.append(c)
            i += 1
    return "".join(out)


def rustix_time_only(text: str) -> list[str]:
    """J-0: the broker's source uses `rustix` for `time` only (with feature unification the
    broker links a rustix built with every member's features, so this is a source rule)."""
    code, out = strip_rust(text), []
    for m in re.finditer(r"\brustix\b", code):
        before, after = code[max(0, m.start() - 20):m.start()], code[m.end():]
        if re.search(r"extern\s+crate\s+$", before) or not re.match(r"\s*::\s*time\b", after):
            out.append(f"rustix used beyond `time`: {code[m.start():m.end() + 24].splitlines()[0]!r}")
    return out


def no_tokio_signal(text: str) -> list[str]:
    """J-0: `writ-approval` source never uses `tokio::signal` (the feature is unified into its
    Tokio by writ-approval-server and writ-service, so this is a source rule). Glob imports and
    renames of `tokio` are refused too, since they would hide a use."""
    code, out = strip_rust(text), []
    for m in re.finditer(r"\btokio\b", code):
        after = code[m.end():]
        group = re.match(r"\s*::\s*\{", after)
        if group:
            depth, j = 1, group.end()
            while j < len(after) and depth:
                depth += {"{": 1, "}": -1}.get(after[j], 0)
                j += 1
            bad = re.search(r"\bsignal\b", after[group.end():j]) is not None
        else:
            bad = re.match(r"\s*::\s*(signal\b|\*)|\s+as\b", after) is not None
        if bad:
            out.append(f"tokio::signal (or a glob or rename of tokio): {code[m.start():m.end() + 24].splitlines()[0]!r}")
    return out


def manifest_features(toml_text: str, dep: str, allowed: set) -> list[str]:
    """`dep` in `[dependencies]` must have default features off and only `allowed` features."""
    spec = tomllib.loads(toml_text).get("dependencies", {}).get(dep)
    if not isinstance(spec, dict):
        return [f"{dep}: not a dependency table with explicit features"]
    out = []
    if spec.get("default-features", True):
        out.append(f"{dep}: default features not disabled")
    extra = sorted(set(spec.get("features", [])) - allowed)
    if extra:
        out.append(f"{dep}: features {extra} beyond {sorted(allowed)}")
    return out


# (member directory, rule, reviewed features of the dependency in that member's manifest).
SOURCE_RULES = [
    ("broker", "rustix", rustix_time_only, {"time"}),
    ("approval", "tokio", no_tokio_signal, {"rt", "net", "time"}),
]


def source_rules(root: Path) -> list[str]:
    """Source-level replacements for features that workspace feature unification makes
    uncheckable per member (M5-INTERFACES J-0; M5a-11): every `.rs` file of the member
    (sources, tests, examples, benches, build script) and its manifest."""
    out = []
    for member, dep, rule, allowed in SOURCE_RULES:
        base = root / member
        files = sorted(p for p in base.rglob("*.rs") if "target" not in p.relative_to(base).parts)
        if not files:
            out.append(f"source rule {member}: no Rust source found")
        for path in files:
            out += [f"source rule {path.relative_to(root)}: {x}" for x in rule(path.read_text())]
        out += [f"source rule {member}/Cargo.toml: {x}"
                for x in manifest_features((base / "Cargo.toml").read_text(), dep, allowed)]
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
        line = line.replace(" (proc-macro)", "")
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
        entry = entry_for(allowed, name, ver)
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
    present = {(p["name"], p["version"]) for p in pkgs}
    for key, entry in allowed.items():
        name = key.split("@", 1)[0]
        if "@" in key and key.split("@", 1)[1] != entry["version"]:
            problems.append(f"allow-list key {key} disagrees with its version {entry['version']}")
        if (name, entry["version"]) not in present and not ("@" not in key and any(n == name for n, _ in present)):
            problems.append(f"allow-list entry {key} is not in Cargo.lock (remove it or explain)")
    return problems


def advisories(hits: dict, exceptions: dict, release: bool) -> tuple[list[str], list[str]]:
    """(problems, visible notes): a hit is excepted only when every advisory id it reports is
    recorded for that exact `name@version`; with `release`, recorded exceptions are problems too."""
    problems, notes = [], []
    for (name, ver), ids in sorted(hits.items()):
        exc = exceptions.get(f"{name}@{ver}")
        if exc and set(ids) <= set(exc["ids"]):
            line = f"{name} {ver}: {ids} under recorded {exc['scope']} exception ({exc['decision']})"
            (problems if release else notes).append(line)
        else:
            problems.append(f"{name} {ver}: {ids}")
    return problems, notes


def osv(pkgs: list[dict]) -> dict:
    body = json.dumps({"queries": [{"package": {"ecosystem": "crates.io", "name": p["name"]}, "version": p["version"]}
                                   for p in pkgs]}).encode()
    req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "writ-dependency-gate"})
    with urllib.request.urlopen(req, timeout=60) as r:
        results = json.load(r)["results"]
    return {(p["name"], p["version"]): sorted(v["id"] for v in res["vulns"])
            for p, res in zip(pkgs, results) if res.get("vulns")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--osv", action="store_true")
    ap.add_argument("--sbom", type=Path)
    ap.add_argument("--release", action="store_true")
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
    unbuilt = {}
    for allow_path in MEMBERS.values():
        allow = json.loads((ROOT / allow_path).read_text())
        covered |= approved_keys(allow.get("dev_packages", {}))
        for key, entry in allow.get("locked_unbuilt", {}).items():
            unbuilt[(key.split("@", 1)[0], entry["version"])] = (allow_path, key, entry)
    for k, (allow_path, key, entry) in unbuilt.items():
        pkg = by_key.get(k)
        if pkg is None or entry.get("checksum") != pkg.get("checksum"):
            problems.append(f"{allow_path}: locked_unbuilt {key} is not in Cargo.lock with the reviewed checksum")
    covered |= set(unbuilt)
    allows = {m: json.loads((ROOT / ap).read_text()) for m, ap in MEMBERS.items()}
    problems += forbidden_through_members(full_closures(meta), allows)
    problems += source_rules(ROOT)
    for p in pkgs:
        if (p["name"], p["version"]) not in covered:
            problems.append(f"locked package {p['name']} {p['version']} is in no member's closure")
    # Supported combined builds (release and test configurations): every package must be approved
    # by some member's allow-list (dev-only packages by a `dev_packages` entry), and the kernel's
    # effective subtree is reported against its standalone closure.
    approved, dev_approved, exceptions = set(), set(), {}
    for allow_path in MEMBERS.values():
        allow = json.loads((ROOT / allow_path).read_text())
        approved |= approved_keys(allow["packages"])
        dev_approved |= approved_keys(allow.get("dev_packages", {}))
        exceptions.update(allow.get("advisory_exceptions", {}))
        for key, entry in allow.get("dev_packages", {}).items():
            lic_ = lic.get((key.split("@", 1)[0], entry["version"]))
            if lic_ is not None and lic_ not in allow["approved_licenses"] and lic_ != entry.get("licence_approved"):
                problems.append(f"{allow_path}: dev package {key} licence {lic_!r} not approved")
            pkg = by_key.get((key.split("@", 1)[0], entry["version"]))
            if pkg is None:
                problems.append(f"{allow_path}: dev package {key} is not in Cargo.lock")
            elif "checksum" in entry and entry["checksum"] != pkg.get("checksum"):
                problems.append(f"{allow_path}: dev package {key} checksum differs from the reviewed one")
    release, test = combined("normal,build"), combined("normal,build,dev")
    for p in sorted(set().union(*release.values()) - approved):
        problems.append(f"combined release build: {p[0]} {p[1]} not approved by any member")
    for p in sorted(set().union(*test.values()) - approved - dev_approved):
        problems.append(f"combined test build: {p[0]} {p[1]} not approved by any member (normal or dev)")
    for k in sorted(set(unbuilt) & set().union(*test.values())):
        problems.append(f"{k[0]} {k[1]} is recorded as locked but never built, yet a supported build uses it (review it)")
    kernel_extra = sorted(release.get("writ-kernel", set()) - clos.get("writ-kernel", set()))
    notes = []
    if a.osv:
        adv_problems, notes = advisories(osv(pkgs), exceptions, a.release)
        problems += [f"advisory: {x}" for x in adv_problems]
    if a.sbom:
        props = {}
        for allow_path in MEMBERS.values():
            allow = json.loads((ROOT / allow_path).read_text())
            for group in ("packages", "dev_packages", "locked_unbuilt"):
                for key, entry in allow.get(group, {}).items():
                    k = (key.split("@", 1)[0], entry["version"])
                    out = props.setdefault(k, [])
                    out.append({"name": "writ:allow-list", "value": f"{allow_path} {group}"})
                    if entry.get("licence_decision"):
                        out.append({"name": "writ:licence-decision", "value": entry["licence_decision"]})
            for key, exc in allow.get("advisory_exceptions", {}).items():
                name, ver = key.split("@", 1)
                props.setdefault((name, ver), []).append(
                    {"name": "writ:advisory-exception", "value": f"{exc['scope']} {exc['ids']} ({exc['decision']})"})
        a.sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
            {"type": "library", "name": p["name"], "version": p["version"], "purl": f"pkg:cargo/{p['name']}@{p['version']}",
             "licenses": [{"expression": lic.get((p["name"], p["version"]))}],
             "hashes": [{"alg": "SHA-256", "content": p["checksum"]}] if p.get("checksum") else [],
             "properties": props.get((p["name"], p["version"]), [])} for p in pkgs]},
            indent=2) + "\n")
    for x in problems:
        print("dependency gate:", x, file=sys.stderr)
    for x in notes:
        print("dependency gate: ADVISORY PRESENT (development only, not a release disposition):", x)
    sizes = {m: len(c) for m, c in clos.items()}
    print(f"dependency gate: {len(pkgs)} locked packages; standalone closures {sizes}; combined release build "
          f"{len(set().union(*release.values()))} packages, test build {len(set().union(*test.values()))}; kernel subtree in "
          f"the combined build adds {[f'{n} {v}' for n, v in kernel_extra] or 'nothing'} (feature unification); "
          f"{len(problems)} problems{' (OSV checked)' if a.osv else ''}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
