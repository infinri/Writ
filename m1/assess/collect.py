#!/usr/bin/env python3
"""M1-07 build, dependency and packaging facts for both prototypes.

  source m1/tools/env.sh
  python3 m1/assess/collect.py facts --out m1-07-facts.json
  python3 m1/assess/collect.py build-times --out m1-07-build-times.json   # guarded

`facts` records only things that do not depend on machine speed: source size,
resolved dependency graphs, licences, OSV advisories, clean-build
reproducibility (two builds from copies at different paths), binary sizes and
linkage, and cross-target feasibility. `build-times` measures clean and
incremental build wall time and refuses to run unless the M1-06 benchmark guard
passes (m1/bench/wbench/envinfo.py), because build times are comparative
measurements. Standard library only; network is used for OSV and licence
lookups only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "m1/bench"))
from wbench import envinfo  # noqa: E402

GO_TAGS = "libsqlite3 sqlite_omit_load_extension"
UA = {"User-Agent": "writ-m1-assessment"}


def sh(argv, cwd=None, env=None, timeout=1800):
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ source

def source_lines() -> dict:
    out = {}
    for lang, root, exts in (("rust", REPO / "m1/rust", (".rs",)), ("go", REPO / "m1/go", (".go",))):
        files = {}
        for f in sorted(root.rglob("*")):
            if f.suffix not in exts or "target" in f.parts:
                continue
            lines = f.read_text().splitlines()
            rel = str(f.relative_to(root))
            if lang == "go":
                test = len(lines) if f.name.endswith("_test.go") else 0
            elif "tests" in f.relative_to(root).parts:
                test = len(lines)
            else:
                cut = next((i for i, ln in enumerate(lines) if ln.strip() == "#[cfg(test)]"), len(lines))
                test = len(lines) - cut
            files[rel] = {"lines": len(lines), "test_lines": test}
        out[lang] = {"files": files, "total_lines": sum(v["lines"] for v in files.values()),
                     "test_lines": sum(v["test_lines"] for v in files.values()),
                     "non_test_lines": sum(v["lines"] - v["test_lines"] for v in files.values())}
    out["method"] = "physical lines including comments and blanks; Rust tests = lines from #[cfg(test)] to end of file plus tests/; Go tests = *_test.go"
    return out


# ------------------------------------------------------------ dependencies

def rust_deps() -> dict:
    code, out, err = sh(["cargo", "metadata", "--format-version", "1", "--locked"], cwd=REPO / "m1/rust")
    if code:
        return {"error": err[-500:]}
    meta = json.loads(out)
    pkgs = {p["id"]: p for p in meta["packages"]}
    root = meta["resolve"]["root"]
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    seen, kinds = {}, {}
    stack = [(root, "normal")]
    while stack:
        pid, kind = stack.pop()
        for d in nodes[pid]["deps"]:
            dk = {k["kind"] or "normal" for k in d["dep_kinds"]}
            if dk <= {"dev"}:
                continue
            k = "build" if kind == "build" or dk == {"build"} else "normal"
            if d["pkg"] not in seen or (seen[d["pkg"]] == "build" and k == "normal"):
                seen[d["pkg"]] = k
                stack.append((d["pkg"], k))
    crates = sorted(({"name": pkgs[i]["name"], "version": pkgs[i]["version"], "license": pkgs[i]["license"],
                      "kind": k, "links": pkgs[i].get("links"),
                      "direct": i in {d["pkg"] for d in nodes[root]["deps"]}} for i, k in seen.items()),
                    key=lambda c: c["name"])
    return {"crates": crates, "count": len(crates), "direct": [c["name"] for c in crates if c["direct"]],
            "build_only": [c["name"] for c in crates if c["kind"] == "build"]}


def go_deps() -> dict:
    env = dict(os.environ)
    code, out, err = sh(["go", "list", "-m", "-json", "all"], cwd=REPO / "m1/go", env=env)
    if code:
        return {"error": err[-500:]}
    mods, dec = [], json.JSONDecoder()
    s = out.strip()
    while s:
        m, i = dec.raw_decode(s)
        mods.append(m)
        s = s[i:].strip()
    deps = []
    for m in mods:
        if m.get("Main"):
            continue
        lic = None
        d = m.get("Dir")
        if d:
            for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
                if Path(d, name).exists():
                    lic = {"file": name, "first_line": Path(d, name).read_text().strip().splitlines()[0]}
                    break
        deps.append({"path": m["Path"], "version": m.get("Version"), "license_file": lic})
    code, used, _ = sh(["go", "list", "-deps", "-tags", GO_TAGS, "-f", "{{if not .Standard}}{{.ImportPath}}{{end}}",
                        "./cmd/proto"], cwd=REPO / "m1/go", env=env)
    return {"modules": deps, "count": len(deps),
            "non_std_packages_linked": sorted(x for x in used.split() if not x.startswith("writ.dev/"))}


def osv(eco: str, name: str, version: str) -> list | str:
    q = json.dumps({"package": {"ecosystem": eco, "name": name}, "version": version}).encode()
    try:
        req = urllib.request.Request("https://api.osv.dev/v1/query", data=q, headers={**UA, "Content-Type": "application/json"})
        return [v["id"] for v in json.load(urllib.request.urlopen(req, timeout=30)).get("vulns", [])]
    except OSError as e:
        return f"lookup failed: {e}"


# ------------------------------------------------------------ builds

def copy_tree(src: Path, dst: Path):
    def ignore(d, names):
        return [n for n in names if n == "target" or n.endswith((".db", ".db-wal", ".db-shm"))
                or (n == "proto" and Path(d, n).is_file())]
    shutil.copytree(src, dst, ignore=ignore)
    shutil.copytree(REPO / "m1/fixtures", dst.parent / "fixtures")  # include_str! and drift tests


def build_rust(src: Path) -> tuple[int, Path, str]:
    code, _, err = sh(["cargo", "build", "--release", "--locked"], cwd=src)
    return code, src / "target/release/proto", err[-800:]


def build_go(src: Path, extra=(), env_over=None) -> tuple[int, Path, str]:
    env = dict(os.environ, **(env_over or {}))
    code, _, err = sh(["go", "build", *extra, "-tags", GO_TAGS, "-o", "proto", "./cmd/proto"], cwd=src, env=env)  # extra: build flags
    return code, src / "proto", err[-800:]


def binary_facts(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    stripped = path.with_name(path.name + ".stripped")
    shutil.copy(path, stripped)
    sh(["strip", str(stripped)])
    code, ldd, _ = sh(["ldd", str(path)])
    return {"bytes": path.stat().st_size, "stripped_bytes": stripped.stat().st_size, "sha256": sha256(path),
            "dynamic_libraries": [ln.split()[0] for ln in ldd.splitlines() if ln.strip()],
            "links_libsqlite3_dynamically": "libsqlite3" in ldd}


def reproducibility() -> dict:
    """Two clean builds from copies at different absolute paths."""
    res = {}
    with tempfile.TemporaryDirectory(prefix="writ-m1-07-") as d:
        for lang in ("rust", "go"):
            variants = {"rust": [("default", {})], "go": [("release -trimpath", ("-trimpath",)), ("without -trimpath", ())]}[lang]
            for name, extra in variants:
                hashes, facts, errors = [], None, []
                for i in (1, 2):
                    dst = Path(d, f"{lang}-{name}-{i}", "m1", lang)
                    copy_tree(REPO / "m1" / lang, dst)
                    code, binp, err = build_rust(dst) if lang == "rust" else build_go(dst, extra)
                    if code:
                        errors.append(err)
                        continue
                    hashes.append(sha256(binp))
                    facts = facts or binary_facts(binp)
                res[f"{lang}:{name}"] = {"identical": len(hashes) == 2 and hashes[0] == hashes[1], "sha256": hashes,
                                         "binary": facts, "errors": errors}
    res["method"] = "same machine, same toolchains and caches, source copied to two different absolute paths, clean target dirs"
    return res


def cross_targets() -> dict:
    out = {}
    with tempfile.TemporaryDirectory(prefix="writ-m1-07-x-") as d:
        dst = Path(d, "m1", "go")
        copy_tree(REPO / "m1/go", dst)
        for goos, goarch in (("linux", "arm64"), ("darwin", "arm64"), ("windows", "amd64")):
            code, _, err = build_go(dst, ("-trimpath",), {"GOOS": goos, "GOARCH": goarch})
            out[f"go:{goos}/{goarch} (cgo, host cc)"] = {"ok": code == 0, "error_tail": None if code == 0 else err[-300:]}
        rdst = Path(d, "r", "m1", "rust")
        copy_tree(REPO / "m1/rust", rdst)
        code, _, err = sh(["cargo", "build", "--release", "--locked", "--target", "aarch64-unknown-linux-gnu"], cwd=rdst)
        out["rust:aarch64-unknown-linux-gnu (installed std only)"] = {"ok": code == 0, "error_tail": None if code == 0 else err[-300:]}
    code, tl, _ = sh(["rustc", "--print", "target-list"])
    code2, gl, _ = sh(["go", "tool", "dist", "list"])
    out["supported_target_counts"] = {"rustc_target_list": len(tl.split()), "go_dist_list": len(gl.split())}
    out["installed_rust_std_targets"] = sorted(p.name for p in (Path(sh(["rustc", "--print", "sysroot"])[1].strip()) / "lib/rustlib").iterdir()
                                               if (p / "lib").is_dir())
    return out


def facts(_args) -> dict:
    rd, gd = rust_deps(), go_deps()
    advis = {f"crates.io/{c['name']}@{c['version']}": osv("crates.io", c["name"], c["version"]) for c in rd.get("crates", [])}
    for m in gd.get("modules", []):
        advis[f"Go/{m['path']}@{m['version']}"] = osv("Go", m["path"], (m["version"] or "").lstrip("v"))
    gov = (sh(["go", "version"])[1].split() or ["", "", "go?"])[2]
    advis[f"Go/stdlib@{gov}"] = osv("Go", "stdlib", gov.lstrip("go"))
    return {"source": source_lines(), "rust_dependencies": rd, "go_dependencies": gd, "osv_advisories": advis,
            "reproducibility": reproducibility(), "cross_targets": cross_targets(),
            "toolchains": envinfo.toolchains(None, None), "pinned_versions": envinfo.pinned_versions(),
            "sqlite_build_info": _read(Path(os.environ.get("SQLITE3_LIB_DIR", "/nonexistent")).parent / "BUILD-INFO")}


def _read(p: Path):
    try:
        return p.read_text().strip().splitlines()
    except OSError:
        return None


def build_times(args) -> dict | None:
    env, repo = envinfo.capture(), envinfo.repository()
    tc = envinfo.toolchains(None, str(REPO / "m1/go/proto"))
    reasons = envinfo.guard(env, repo, 1.0, 60.0) + envinfo.toolchain_reasons(tc, envinfo.pinned_versions())
    if reasons:
        print("collect: REFUSED to time builds (benchmark guard):", *reasons, sep="\n  - ", file=sys.stderr)
        return None
    res = {"environment": env, "repository": repo, "runs": args.runs, "samples": {}, "evidence": {}}
    edits = {
        # Behaviour-preserving but compiler-relevant: a new private function with a per-run constant in the
        # kernel module; the kernel crate (Rust) or package (Go) must be recompiled and the binary relinked.
        "rust": ("src/kernel.rs", "\n#[allow(dead_code)]\n#[inline(never)]\nfn m1_07_edit_probe() -> u64 {{\n    {n}\n}}\n"),
        "go": ("internal/kernel/schema.go", "\n//go:noinline\nfunc m107EditProbe() uint64 {{ return {n} }}\n"),
    }
    with tempfile.TemporaryDirectory(prefix="writ-m1-07-t-") as d:
        for lang in ("rust", "go"):
            clean, incr, proof = [], [], []
            for i in range(args.runs):
                dst = Path(d, f"{lang}-{i}", "m1", lang)
                copy_tree(REPO / "m1" / lang, dst)
                env_over = {"GOCACHE": str(Path(d, f"gocache-{i}"))} if lang == "go" else None  # isolated, empty
                t = time.perf_counter()
                code, _, err = build_rust(dst) if lang == "rust" else build_go(dst, ("-trimpath",), env_over)
                clean.append(time.perf_counter() - t if code == 0 else None)
                before = sha256(dst / ("target/release/proto" if lang == "rust" else "proto")) if code == 0 else None
                rel, snippet = edits[lang]
                (dst / rel).write_text((dst / rel).read_text() + snippet.format(n=1000 + i))
                t = time.perf_counter()
                if lang == "rust":
                    code, _, err = build_rust(dst)
                    compiled = "Compiling writ-m1-proto" in err
                else:
                    code, _, err = build_go(dst, ("-trimpath", "-v"), env_over)
                    compiled = "writ.dev/m1/proto/internal/kernel" in err
                incr.append(time.perf_counter() - t if code == 0 else None)
                after = sha256(dst / ("target/release/proto" if lang == "rust" else "proto")) if code == 0 else None
                proof.append({"recompiled_kernel": compiled, "binary_rewritten": after is not None,
                              "binary_hash_changed": before != after, "build_log_tail": err[-300:]})
            res["samples"][lang] = {"clean_s": clean, "incremental_real_edit_s": incr}
            res["evidence"][lang] = proof
    res["method"] = ("clean = fresh source copy, empty target directory (Rust registry sources cached in CARGO_HOME) "
                     "or empty isolated GOCACHE per run (the owner's global Go cache is never touched); "
                     "incremental = after the clean build, append a private function with a per-run constant to the "
                     "kernel source (src/kernel.rs, internal/kernel/schema.go) and rebuild; evidence records whether "
                     "the kernel was recompiled (cargo 'Compiling writ-m1-proto', go -v listing the kernel package)")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["facts", "build-times"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = facts(a) if a.mode == "facts" else build_times(a)
    if data is None:
        return 3
    data.update({"mode": a.mode, "collected_at": started, "commit": envinfo.repository()["commit"],
                 "m1_tree_clean": envinfo.repository()["m1_tree_clean"]})
    Path(a.out).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"collect: {a.mode} written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
