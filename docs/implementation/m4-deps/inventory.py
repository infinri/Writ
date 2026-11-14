#!/usr/bin/env python3
"""WV-1 inventory: every locked registry package with version, Cargo.lock checksum, licence
(cargo metadata), crates.io publication timestamp, whether it is new against the base lock, and
OSV advisories (api.osv.dev querybatch, one query per package/version).

  python3 -I docs/implementation/m4-deps/inventory.py BASE_LOCK METADATA_JSON OUT_DIR
"""
import csv
import json
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = {"User-Agent": "writ-dependency-review"}
CUTOFF = datetime(2026, 11, 14, tzinfo=timezone.utc)


def get(url, body=None):
    req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    base_lock, meta_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    lock = tomllib.loads(Path("Cargo.lock").read_text())
    pkgs = [p for p in lock["package"] if p.get("source")]
    base = {(p["name"], p["version"]) for p in tomllib.loads(base_lock.read_text())["package"] if p.get("source")}
    meta = json.loads(meta_path.read_text())
    lic = {(p["name"], p["version"]): p.get("license") for p in meta["packages"] if p.get("source")}
    rows = []
    for p in pkgs:
        v = get(f"https://crates.io/api/v1/crates/{p['name']}/{p['version']}")["version"]
        created = v["created_at"]
        age = (CUTOFF - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 86400
        rows.append({"name": p["name"], "version": p["version"], "checksum": p.get("checksum", ""),
                     "licence": lic.get((p["name"], p["version"])), "published": created,
                     "age_days_at_2026-11-14": round(age, 2), "new": (p["name"], p["version"]) not in base,
                     "yanked": v.get("yanked")})
        time.sleep(1.0)
    body = json.dumps({"queries": [{"package": {"ecosystem": "crates.io", "name": r["name"]}, "version": r["version"]}
                                   for r in rows]}).encode()
    results = get("https://api.osv.dev/v1/querybatch", body)["results"]
    for r, res in zip(rows, results):
        r["osv"] = " ".join(v["id"] for v in res.get("vulns", []))
    with open(out / "packages.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "osv.json").write_text(json.dumps({"queried_at": datetime.now(timezone.utc).isoformat(),
                                              "endpoint": "https://api.osv.dev/v1/querybatch",
                                              "queries": len(rows),
                                              "results": [{"name": r["name"], "version": r["version"], "vulns": r["osv"].split()}
                                                          for r in rows]}, indent=1) + "\n")


if __name__ == "__main__":
    main()
