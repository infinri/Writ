#!/usr/bin/env python3
"""OSV check of each young version and its pin candidate (security-fix detection).

  python3 -I docs/implementation/m4-deps/osv-candidates.py young.tsv
"""
import csv
import json
import sys
import urllib.request

rows = [r for r in csv.DictReader(open(sys.argv[1]), delimiter="\t")]
qs = []
for r in rows:
    qs.append((r["name"], r["version"]))
    if r["pin_candidate"] != "none":
        qs.append((r["name"], r["pin_candidate"]))
body = json.dumps({"queries": [{"package": {"ecosystem": "crates.io", "name": n}, "version": v} for n, v in qs]}).encode()
req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=body,
                             headers={"Content-Type": "application/json", "User-Agent": "writ-dependency-review"})
with urllib.request.urlopen(req, timeout=60) as f:
    res = json.load(f)["results"]
for (n, v), r in zip(qs, res):
    print(n, v, [x["id"] for x in r.get("vulns", [])] or "none")
