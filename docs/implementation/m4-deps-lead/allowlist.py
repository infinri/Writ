#!/usr/bin/env python3
"""Generate approval/deps-allowlist.json from the reviewed lock (lead, D-041, D-042).

Run from the repository root with the pinned toolchain. Every entry is an exact version and
checksum; licences outside the general set carry the decision that approved them. A licence
with no recorded decision stops the script (never approved by default).
"""
import collections, json, subprocess, sys, tomllib
from pathlib import Path

STD = ["MIT", "MIT OR Apache-2.0", "MIT/Apache-2.0", "Apache-2.0 OR MIT"]
MPL = {"crypto-glue", "webauthn-rs", "webauthn-rs-core", "webauthn-rs-proto", "webauthn-attestation-ca",
       "webauthn-authenticator-rs"}
UNICODE = {"icu_collections", "icu_locale_core", "icu_normalizer", "icu_normalizer_data", "icu_properties",
           "icu_properties_data", "icu_provider", "litemap", "potential_utf", "tinystr", "writeable", "yoke",
           "zerofrom", "zerotrie", "zerovec", "yoke-derive", "zerofrom-derive", "zerovec-derive"}
DR5 = {("unicode-ident", "1.0.27"), ("unicode-normalization", "0.1.26")}
DISJ = {"MIT OR Apache-2.0 OR BSD-1-Clause", "MIT OR Apache-2.0 OR LGPL-2.1-or-later", "Unlicense OR MIT",
        "BSD-2-Clause OR Apache-2.0 OR MIT", "Zlib OR Apache-2.0 OR MIT",
        "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT"}
DIRECT = {"webauthn-rs": "direct runtime (passkey relying-party verifier; default features off; danger-allow-state-serialisation for ledger-held ceremony state)",
          "hyper": "direct runtime (HTTP/1 server only; loopback approval listener, D-041)",
          "hyper-util": "direct runtime (Tokio IO adapter for hyper)",
          "tokio": "direct runtime (rt, net, time only; approval listener; forbidden in kernel and broker closures)"}


def decision(name, ver, lic):
    if lic in STD:
        return None
    if lic == "MPL-2.0" and name in MPL:
        return "MPL-2.0 approved for this package and version only (D-041 DR-2); unmodified; notice and source availability in THIRD-PARTY-NOTICES.md"
    if lic == "Unicode-3.0" and name in UNICODE:
        return "Unicode-3.0 approved for this ICU/zerovec package and version only (D-041 DR-2); notice in THIRD-PARTY-NOTICES.md"
    if lic == "Apache-2.0" and name == "flagset":
        return "Apache-2.0 approved for flagset 0.4.7 only (D-041 DR-2); notice in THIRD-PARTY-NOTICES.md"
    if (name, ver) in DR5:
        return "(MIT OR Apache-2.0) AND Unicode-3.0 approved for this exact package and version only (D-042 DR-5); notice in THIRD-PARTY-NOTICES.md"
    if lic in DISJ:
        return "disjunctive licence used under its MIT or Apache-2.0 option; exact entry (D-041 DR-2)"
    if lic == "BSD-3-Clause" and (name, ver) == ("subtle", "2.6.1"):
        return "BSD-3-Clause approved for subtle 2.6.1 only (D-033); notice in THIRD-PARTY-NOTICES.md"
    sys.exit(f"no recorded licence decision for {name} {ver} ({lic})")


def tree(edges):
    out = subprocess.run(["cargo", "tree", "--locked", "--target", "all", "-p", "writ-approval", "-e", edges,
                          "--prefix", "none", "--format", "{p}", "--prune", "writ-broker"],
                         capture_output=True, text=True, check=True).stdout
    found = set()
    for line in out.splitlines():
        line = line.replace(" (proc-macro)", "")
        parts = line.replace(" (*)", "").split()
        if len(parts) >= 2 and parts[1].startswith("v") and "(" not in line:
            found.add((parts[0], parts[1][1:]))
    return found


meta = json.loads(subprocess.run(["cargo", "metadata", "--locked", "--format-version", "1"],
                                 capture_output=True, text=True, check=True).stdout)
lic = {(p["name"], p["version"]): p.get("license") for p in meta["packages"] if p.get("source")}
sums = {(p["name"], p["version"]): p["checksum"] for p in tomllib.loads(Path("Cargo.lock").read_text())["package"]
        if p.get("source")}
normal, test = tree("normal,build"), tree("normal,build,dev")
dev = test - normal
names = collections.Counter(n for n, _ in normal | dev)


def entries(pkgs, dev_only):
    out = {}
    for name, ver in sorted(pkgs):
        e = {"version": ver, "checksum": sums[(name, ver)],
             "role": "dev-only direct (software passkey test authenticator, non-shipping; D-041)" if name == "webauthn-authenticator-rs"
             else DIRECT.get(name, "dev-only transitive" if dev_only else "transitive")}
        d = decision(name, ver, lic[(name, ver)])
        if d:
            e["licence_approved"] = lic[(name, ver)]
            e["licence_decision"] = d
        out[f"{name}@{ver}" if names[name] > 1 else name] = e
    return out


lock_all = {k for k in sums}


def built_anywhere():
    out = subprocess.run(["cargo", "tree", "--locked", "--target", "all", "--workspace", "-e", "all", "--prefix", "none",
                          "--format", "{p}"], capture_output=True, text=True, check=True).stdout
    found = set()
    for line in out.splitlines():
        parts = line.replace(" (proc-macro)", "").replace(" (*)", "").split()
        if len(parts) >= 2 and parts[1].startswith("v"):
            found.add((parts[0], parts[1][1:]))
    return found


unbuilt = sorted(lock_all - built_anywhere())
dup = collections.Counter(n for n, _ in lock_all)
allow = {
    "comment": "Reviewed dependency allow-list for writ-approval (D-041, D-042; review: docs/implementation/M4-DEPENDENCY-REVIEW.md; generated by docs/implementation/m4-deps-lead/allowlist.py). Exact versions and checksums; licences outside the general set are approved per package and version only. Path dependencies on workspace members are not listed.",
    "approved_licenses": STD,
    "forbidden": ["openssl", "openssl-sys", "native-tls", "ring", "rustls", "aws-lc-rs", "aws-lc-sys", "reqwest",
                  "rusqlite", "libsqlite3-sys", "h2"],
    "forbidden_features": {"hyper": ["http2", "client", "full"], "tokio": ["full", "process", "fs", "signal", "io-std"],
                           "webauthn-rs": ["attestation", "resident-key-support", "conditional-ui"]},
    "packages": entries(normal, False),
    "dev_packages": entries(dev, True),
    "locked_unbuilt": {(f"{n}@{v}" if dup[n] > 1 else n): {"version": v, "checksum": sums[(n, v)], "licence": lic.get((n, v)),
                       "role": "in Cargo.lock only (optional dependency of an approved package, never enabled); no supported build compiles it"}
                       for n, v in unbuilt},
    "advisory_exceptions": {"rsa@0.10.0-rc.19": {
        "ids": ["RUSTSEC-2023-0071", "GHSA-c38w-74pg-36hr", "CVE-2023-49092"],
        "scope": "DEVELOPMENT-ONLY",
        "decision": "D-041 DR-3; reachability evidence and update plan in M4-DEPENDENCY-REVIEW.md 'Lead reproduction'",
        "path": "rsa <- crypto-glue 0.2.2 <- webauthn-attestation-ca / webauthn-rs-core 0.6.2 <- webauthn-rs <- writ-approval",
        "compensating": "enrollment refuses non-ES256 credentials; RSA private-key operations unreachable in all enabled paths",
        "update_plan": "adopt the first crypto-glue release accepting rsa >= 0.10.0 and repeat the review; re-review at the M4 gate and every dependency change; release needs an explicit disposition"}},
}
Path("approval/deps-allowlist.json").write_text(json.dumps(allow, indent=2) + "\n")
print(f"writ-approval: {len(normal)} normal/build packages, {len(dev)} dev-only packages, {len(unbuilt)} locked but never built")
