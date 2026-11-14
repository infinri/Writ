#!/usr/bin/env python3
"""Exact counts for summary.md from packages.csv and the cargo tree outputs in tree/.

  python3 -I docs/implementation/m4-deps/counts.py docs/implementation/m4-deps
"""
import csv
import sys
from collections import Counter
from pathlib import Path

E = Path(sys.argv[1])
APPROVED = {"MIT", "MIT OR Apache-2.0", "MIT/Apache-2.0", "Apache-2.0 OR MIT"}


def tree(name):
    out = set()
    for line in (E / "tree" / name).read_text().splitlines():
        s = line.lstrip(" │├└─").replace(" (*)", "").replace(" (proc-macro)", "")
        parts = s.split()
        if len(parts) >= 2 and parts[1].startswith("v") and "(/" not in s and not s.startswith("["):
            out.add((parts[0], parts[1][1:]))
    return out


rows = {(r["name"], r["version"]): r for r in csv.DictReader(open(E / "packages.csv"))}
print("locked registry packages:", len(rows), "new vs 4c07327:", sum(r["new"] == "True" for r in rows.values()))
for c in ["writ-kernel", "writ-broker", "writ-ledger-sqlite", "writ-approval", "combined"]:
    nb, t = tree(f"{c}.normal-build.txt"), tree(f"{c}.test.txt")
    print(f"{c}: normal/build {len(nb)}, test {len(t)}, test-only {len(t - nb)}")
comb_nb, comb_t = tree("combined.normal-build.txt"), tree("combined.test.txt")
inactive = sorted(set(rows) - comb_t)
print("locked but in no build or test graph:", len(inactive), " ".join(f"{n} {v}" for n, v in inactive))
print("licences of new packages in the combined release graph:",
      dict(Counter(rows[k]["licence"] for k in comb_nb if rows[k]["new"] == "True")))
print("needing approval (combined release graph):")
for k in sorted(comb_nb):
    if rows[k]["licence"] not in APPROVED:
        print("  ", k[0], k[1], rows[k]["licence"])
print("needing approval (test-only):")
for k in sorted(comb_t - comb_nb):
    if rows[k]["licence"] not in APPROVED:
        print("  ", k[0], k[1], rows[k]["licence"])
print("needing approval (lock only, inactive):")
for k in inactive:
    if rows[k]["licence"] not in APPROVED:
        print("  ", k[0], k[1], rows[k]["licence"])
young = sorted(k for k, r in rows.items() if r["new"] == "True" and float(r["age_days_at_2026-11-14"]) < 14)
print("new packages younger than 14 days at 2026-11-14T00:00Z after pins:", len(young))
for k in young:
    where = "release" if k in comb_nb else "test-only" if k in comb_t else "lock-only"
    print("  ", k[0], k[1], rows[k]["published"], rows[k]["age_days_at_2026-11-14"], where)
print("OSV:", [(k, r["osv"]) for k, r in rows.items() if r["osv"]])
